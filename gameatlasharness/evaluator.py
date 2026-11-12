#!/usr/bin/env python3
"""Trusted scalar-feedback coordinator for GameBench.

Derived from GameBench dev/sys 3b1dbd0c autoresearch/server.py.
This evaluator is independent of external orchestration frameworks. It snapshots only task-declared
workspace files or directory trees from a hermetic Harbor Work container, runs task-owned Apply
code in a clean copy of the task image, then scores inert artifacts in a
separate trusted container.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import shutil
import stat
import subprocess
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass, asdict, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

MAX_REQUEST_BYTES = 16 * 1024
SUBMISSION_ERROR_MARKER = "GAMEBENCH_SUBMISSION_ERROR:"
DEFAULT_TASK_MANIFEST = Path("bench_data/manifests/dev_sys_current8.jsonl")
DEFAULT_CAMERA_LAYOUT_MANIFESTS = (
    Path("bench_data/manifests/camera_reconstruction_v2_localpool_banana10.jsonl"),
    Path("bench_data/manifests/sceneactbench_layout_localpool_banana10.jsonl"),
)
# Optional verifier-written notices about the submission's own geometry. Only
# the sanitized ``notices`` strings are forwarded; every other field stays in
# the trusted run directory.
FEEDBACK_FILENAME = "feedback.json"
FEEDBACK_SCHEMA = "mesh-feedback-1"
MAX_FEEDBACK_BYTES = 64 * 1024
MAX_NOTICES = 5
MAX_NOTICE_CHARS = 200


class CoordinatorError(RuntimeError):
    def __init__(
        self,
        category: str,
        message: str,
        *,
        submission_failure: bool = False,
        remaining_submissions: int | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.submission_failure = submission_failure
        self.remaining_submissions = remaining_submissions


@dataclass(frozen=True)
class Mount:
    source: Path
    target: str


@dataclass(frozen=True)
class Export:
    source: str
    target: str


@dataclass(frozen=True)
class TaskConfig:
    task_id: str
    task_dir: Path
    apply_script: Path
    score_script: Path
    apply_timeout_sec: int
    score_timeout_sec: int
    max_submissions: int
    cooldown_sec: float
    cpus: float
    memory_mb: int
    pids_limit: int
    artifact_max_mb: int
    submission_max_mb: int
    cache_version: str
    verifier_image: str | None
    strict_work_isolation: bool
    apply_user: str
    submission_source_dir: Path
    submission_files: tuple[str, ...]
    apply_mounts: tuple[Mount, ...]
    fixed_mounts: tuple[Mount, ...]
    trusted_exports: tuple[Export, ...]
    apply_tmp_exec: bool = False
    evaluation_env: tuple[tuple[str, str], ...] = ()
    evaluation_timeout_sec: float | None = None
    evaluation_deadline: float | None = None


@dataclass
class RunState:
    count: int = 0
    best_score: float | None = None
    task_id: str | None = None
    best_submission_id: str | None = None
    best_snapshot_path: str | None = None
    last_started: float = 0.0
    in_flight: bool = False


@dataclass(frozen=True)
class SubmissionTicket:
    submission_index: int
    remaining_submissions: int


@dataclass(frozen=True)
class EvaluationResult:
    score: float
    best_score: float
    is_best: bool
    baseline: bool
    submission_index: int
    remaining_submissions: int
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class SubmissionSettlement:
    best_score: float
    is_best: bool
    keep_snapshot: bool
    obsolete_snapshot_path: str | None


class EvaluationPool:
    """FIFO admission control for heterogeneous Apply/Verifier workloads."""

    def __init__(self, max_cpus: float | None, max_memory_mb: int | None) -> None:
        self.max_cpus = max_cpus
        self.max_memory_mb = max_memory_mb
        self.used_cpus = 0.0
        self.used_memory_mb = 0
        self._condition = threading.Condition()
        self._queue: list[tuple[object, float, int]] = []

    def acquire(self, cpus: float, memory_mb: int) -> object:
        if self.max_cpus is not None and cpus > self.max_cpus:
            raise CoordinatorError(
                "infrastructure", "task CPU requirement exceeds evaluation pool"
            )
        if self.max_memory_mb is not None and memory_mb > self.max_memory_mb:
            raise CoordinatorError(
                "infrastructure", "task memory requirement exceeds evaluation pool"
            )
        ticket = object()
        with self._condition:
            self._queue.append((ticket, cpus, memory_mb))
            while True:
                cpu_available = (
                    self.max_cpus is None
                    or self.used_cpus + cpus <= self.max_cpus + 1.0e-9
                )
                memory_available = (
                    self.max_memory_mb is None
                    or self.used_memory_mb + memory_mb <= self.max_memory_mb
                )
                if self._queue[0][0] is ticket and cpu_available and memory_available:
                    self._queue.pop(0)
                    self.used_cpus += cpus
                    self.used_memory_mb += memory_mb
                    return ticket
                self._condition.wait()

    def release(self, ticket: object, cpus: float, memory_mb: int) -> None:
        del ticket
        with self._condition:
            self.used_cpus = max(0.0, self.used_cpus - cpus)
            self.used_memory_mb = max(0, self.used_memory_mb - memory_mb)
            self._condition.notify_all()


def _resolve_mounts(task_dir: Path, values: list[dict[str, Any]]) -> tuple[Mount, ...]:
    mounts: list[Mount] = []
    for value in values:
        source = (task_dir / str(value["source"])).resolve()
        if not source.exists():
            raise ValueError(f"trusted mount does not exist: {source}")
        mounts.append(Mount(source=source, target=str(value["target"])))
    return tuple(mounts)


def classify_apply_failure(
    result: subprocess.CompletedProcess[str],
) -> tuple[bool, str]:
    detail = (result.stderr or result.stdout or "").strip()
    return (
        result.returncode == 2 and SUBMISSION_ERROR_MARKER in detail,
        detail[-2000:] or "Apply failed",
    )


def sanitize_notices(value: object) -> tuple[str, ...]:
    """Keep at most MAX_NOTICES short printable-ASCII strings; drop the rest."""
    if not isinstance(value, list):
        return ()
    notices: list[str] = []
    for item in value:
        if (
            isinstance(item, str)
            and 0 < len(item) <= MAX_NOTICE_CHARS
            and item.strip()
            and all(" " <= character <= "~" for character in item)
        ):
            notices.append(item)
            if len(notices) == MAX_NOTICES:
                break
    return tuple(notices)


def read_feedback_notices(result_dir: Path) -> tuple[str, ...]:
    """Return sanitized notices from an optional verifier feedback file.

    Feedback is advisory: a missing, oversized, non-regular or malformed file
    yields no notices and never changes the score.
    """
    try:
        path = result_dir / FEEDBACK_FILENAME
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_size > MAX_FEEDBACK_BYTES:
            return ()
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        )
        with os.fdopen(descriptor, "rb") as stream:
            raw = stream.read(MAX_FEEDBACK_BYTES + 1)
        if len(raw) > MAX_FEEDBACK_BYTES:
            return ()
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("schema") != FEEDBACK_SCHEMA:
            return ()
        return sanitize_notices(payload.get("notices"))
    except Exception:
        return ()


def source_provenance(repo_root: Path) -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                text=True,
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            # Frozen deployment bundles need not include git or a checkout.
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    status = git("status", "--porcelain=v1")
    return {
        "git_commit_id": git("rev-parse", "HEAD"),
        "git_branch": git("branch", "--show-current"),
        "git_dirty": bool(status),
        "git_status_sha256": (
            hashlib.sha256(status.encode()).hexdigest() if status is not None else None
        ),
    }


def _submission_files(source_dir: Path, values: list[object], *, generated: tuple[str, ...] = ()) -> tuple[str, ...]:
    files: list[str] = []
    for value in values:
        relative = PurePosixPath(str(value))
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in ("", ".", "..") for part in relative.parts)
        ):
            raise ValueError(f"invalid submission file: {value}")
        normalized = relative.as_posix()
        source = source_dir / normalized
        if source.is_symlink() or (not (source.is_file() or source.is_dir()) and normalized not in generated):
            raise ValueError(f"submission path is not a regular task file/directory: {source}")
        if any(relative == PurePosixPath(other) or relative in PurePosixPath(other).parents
               or PurePosixPath(other) in relative.parents for other in files):
            raise ValueError(f"overlapping submission paths: {normalized}")
        if any(parent.is_symlink() for parent in (source, *source.parents)
               if parent != source_dir and source_dir in parent.parents):
            raise ValueError(f"linked submission path: {source}")
        if source.exists():
            _submission_path_size(source)
        files.append(normalized)
    if not set(generated).issubset(files):
        raise ValueError("generated files must be declared submission files")
    if not files:
        raise ValueError(f"no submission_files configured below {source_dir}")
    return tuple(files)


def _submission_path_size(root: Path) -> int:
    """Validate source-only trees without following links or special files."""
    total = 0
    pending = [root]
    count = 0
    while pending:
        path = pending.pop()
        count += 1
        if count > 10000:
            raise ValueError("submission contains too many entries")
        if path.is_symlink():
            raise ValueError(f"symbolic link in submission: {path}")
        if path.is_dir():
            pending.extend(path.iterdir())
        elif path.is_file():
            total += path.stat().st_size
        else:
            raise ValueError("submission contains a non-regular entry")
    return total


def load_task_allowlist(
    repo_root: Path, manifest_paths: tuple[Path, ...] | None = None
) -> frozenset[str]:
    manifest_paths = manifest_paths or DEFAULT_CAMERA_LAYOUT_MANIFESTS
    if not manifest_paths:
        raise ValueError("at least one autoresearch task manifest is required")
    task_ids: set[str] = set()
    for path in manifest_paths:
        resolved = path if path.is_absolute() else repo_root / path
        if not resolved.is_file():
            raise ValueError(f"autoresearch task manifest does not exist: {resolved}")
        for line_number, raw_line in enumerate(
            resolved.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"invalid JSON in {resolved}:{line_number}"
                ) from error
            task_id = record.get("task_name") if isinstance(record, dict) else None
            if not isinstance(task_id, str) or not task_id:
                raise ValueError(f"missing task_name in {resolved}:{line_number}")
            if task_id in task_ids:
                raise ValueError(f"duplicate autoresearch task in manifests: {task_id}")
            task_ids.add(task_id)
    if not task_ids:
        raise ValueError("autoresearch task manifests selected no tasks")
    return frozenset(task_ids)


def submission_config(task_dir: Path) -> dict:
    spec = task_dir / "task.toml"
    if spec.exists():
        submission = tomllib.loads(spec.read_text()).get("harness", {}).get("submission")
        if submission is not None:
            return submission
    return json.loads((task_dir / "autoresearch.json").read_text())


def load_tasks(
    repo_root: Path,
    allowed_task_ids: frozenset[str] | None = None,
) -> dict[str, TaskConfig]:
    tasks: dict[str, TaskConfig] = {}
    root = repo_root / "tasks"
    directories = {p.parent for pattern in ("*/task.toml", "*/autoresearch.json") for p in root.glob(pattern)}
    for directory in sorted(directories):
        task_dir = directory.resolve()
        if allowed_task_ids is not None and task_dir.name not in allowed_task_ids:
            continue
        data = submission_config(task_dir)
        task_id = str(data["task_id"])
        if task_id != task_dir.name:
            raise ValueError(
                f"autoresearch task_id must match its directory: {task_id}"
            )
        if task_id in tasks:
            raise ValueError(f"duplicate autoresearch task id: {task_id}")
        submission_source_dir = (
            task_dir / str(data.get("submission_source_dir", "environment/workspace"))
        ).resolve()
        if not submission_source_dir.is_relative_to(task_dir):
            raise ValueError(
                f"submission_source_dir escapes the task directory: {task_id}"
            )
        task = TaskConfig(
            task_id=task_id,
            task_dir=task_dir,
            apply_script=(
                task_dir / str(data.get("apply_script", "tests/autoresearch_apply.py"))
            ).resolve(),
            score_script=(
                task_dir / str(data.get("score_script", "tests/autoresearch_score.py"))
            ).resolve(),
            apply_timeout_sec=int(data.get("apply_timeout_sec", 900)),
            score_timeout_sec=int(data.get("score_timeout_sec", 900)),
            max_submissions=int(data.get("max_submissions", 20)),
            cooldown_sec=float(data.get("cooldown_sec", 1.0)),
            cpus=float(data.get("cpus", 2.0)),
            memory_mb=int(data.get("memory_mb", 4096)),
            pids_limit=int(data.get("pids_limit", 512)),
            artifact_max_mb=int(data.get("artifact_max_mb", 512)),
            submission_max_mb=int(data.get("submission_max_mb", 32)),
            cache_version=str(data.get("cache_version", "v1")),
            verifier_image=(
                str(data["verifier_image"]) if data.get("verifier_image") else None
            ),
            strict_work_isolation=bool(data.get("strict_work_isolation", True)),
            apply_user=str(data.get("apply_user", "root")),
            submission_source_dir=submission_source_dir,
            submission_files=_submission_files(
                submission_source_dir, list(data.get("submission_files", [])),
                generated=tuple(data.get("generated_submission_files", [])),
            ),
            apply_mounts=_resolve_mounts(task_dir, list(data.get("apply_mounts", []))),
            fixed_mounts=_resolve_mounts(task_dir, list(data.get("fixed_mounts", []))),
            trusted_exports=tuple(
                Export(source=str(item["source"]), target=str(item["target"]))
                for item in data.get("trusted_exports", [])
            ),
        )
        if not task.apply_script.is_file() or not task.score_script.is_file():
            raise ValueError(f"missing Apply/Verifier script for {task_id}")
        if task.max_submissions <= 0:
            raise ValueError(f"max_submissions must be positive for {task_id}")
        if task.cooldown_sec < 0.0:
            raise ValueError(f"cooldown_sec must be non-negative for {task_id}")
        if task.submission_max_mb <= 0:
            raise ValueError(f"submission_max_mb must be positive for {task_id}")
        if task.apply_user not in {"root", "agent", "camera"}:
            raise ValueError(f"unsupported apply_user for {task_id}")
        tasks[task_id] = task
    if allowed_task_ids is not None:
        missing = sorted(allowed_task_ids - tasks.keys())
        if missing:
            raise ValueError(
                "missing submission configuration for allowlisted tasks: " + ", ".join(missing)
            )
    if not tasks:
        raise ValueError(f"no configured tasks found below {repo_root}")
    return tasks


class Evaluator:
    def __init__(
        self,
        repo_root: Path,
        run_root: Path,
        token: str | None = None,
        keep_runs: bool = False,
        max_evaluation_cpus: float | None = None,
        max_evaluation_memory_mb: int | None = None,
        task_manifest_paths: tuple[Path, ...] | None = None,
        container_engine: str = "podman",
        work_environment_dir: Path | None = None,
    ) -> None:
        if container_engine not in {"docker", "podman"}:
            raise ValueError("container_engine must be docker or podman")
        self.repo_root = repo_root.resolve()
        self.run_root = run_root.resolve()
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.snapshot_root = self.run_root / "snapshots"
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        selected_manifests = task_manifest_paths or (
            self.repo_root / DEFAULT_TASK_MANIFEST,
        )
        self.task_allowlist = load_task_allowlist(
            self.repo_root, selected_manifests
        )
        self.tasks = load_tasks(self.repo_root, self.task_allowlist)
        # A trusted launcher may stage a single task's Compose project apart
        # from its private source. Never infer this path from agent requests.
        if work_environment_dir is not None:
            if not work_environment_dir.is_absolute() or len(self.tasks) != 1:
                raise ValueError("work_environment_dir requires an absolute path and exactly one task")
        self.work_environment_dir = work_environment_dir.resolve() if work_environment_dir else None
        self.token = token
        self.keep_runs = keep_runs
        self.container_engine = container_engine
        self.evaluation_pool = EvaluationPool(
            max_evaluation_cpus, max_evaluation_memory_mb
        )
        self._states: dict[str, RunState] = {}
        self._lock = threading.Lock()
        self._event_lock = threading.Lock()
        self._state_path = self.run_root / "submission-state.json"
        self._event_path = self.run_root / "evaluation-events.jsonl"
        self._source_provenance = source_provenance(self.repo_root)
        self._load_states()

    def _load_states(self) -> None:
        if not self._state_path.is_file():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            schema_version = payload.get("schema_version")
            if schema_version not in (1, 2, 3, 4, 5):
                raise ValueError("unsupported state schema")
            records = payload.get("containers", {})
            if not isinstance(records, dict):
                raise TypeError("invalid state records")
            for container_id, record in records.items():
                if not isinstance(record, dict):
                    raise TypeError("invalid state record")
                best_score = (
                    record.get("best_score") if schema_version in (2, 3, 4, 5) else None
                )
                interrupted = (
                    record.get("in_flight", False)
                    if schema_version in (3, 4, 5)
                    else False
                )
                task_id = record.get("task_id") if schema_version in (4, 5) else None
                best_submission_id = (
                    record.get("best_submission_id") if schema_version == 5 else None
                )
                best_snapshot_path = (
                    record.get("best_snapshot_path") if schema_version == 5 else None
                )
                if (
                    not isinstance(container_id, str)
                    or len(container_id) != 64
                    or any(c not in "0123456789abcdef" for c in container_id)
                    or not isinstance(record.get("count"), int)
                    or isinstance(record.get("count"), bool)
                    or record["count"] < 0
                    or not isinstance(interrupted, bool)
                    or (task_id is not None and not isinstance(task_id, str))
                    or (
                        best_submission_id is not None
                        and (
                            not isinstance(best_submission_id, str)
                            or len(best_submission_id) != 32
                            or any(
                                c not in "0123456789abcdef" for c in best_submission_id
                            )
                        )
                    )
                    or (
                        best_snapshot_path is not None
                        and (
                            not isinstance(best_snapshot_path, str)
                            or not Path(best_snapshot_path).is_absolute()
                            or not Path(best_snapshot_path)
                            .resolve()
                            .is_relative_to(self.snapshot_root)
                        )
                    )
                    or ((best_submission_id is None) != (best_snapshot_path is None))
                    or (
                        best_score is not None
                        and (
                            not isinstance(best_score, (int, float))
                            or isinstance(best_score, bool)
                            or not math.isfinite(float(best_score))
                            or not 0.0 <= float(best_score) <= 1.0
                        )
                    )
                ):
                    raise ValueError("invalid state record")
                self._states[container_id] = RunState(
                    count=max(0, record["count"] - int(interrupted)),
                    best_score=(
                        round(float(best_score), 6) if best_score is not None else None
                    ),
                    task_id=task_id,
                    best_submission_id=best_submission_id,
                    best_snapshot_path=best_snapshot_path,
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(
                f"invalid coordinator state: {self._state_path}"
            ) from error

    def _persist_states_locked(self) -> None:
        payload = {
            "schema_version": 5,
            "containers": {
                container_id: {
                    "count": state.count,
                    "best_score": state.best_score,
                    "task_id": state.task_id,
                    "best_submission_id": state.best_submission_id,
                    "best_snapshot_path": state.best_snapshot_path,
                    "in_flight": state.in_flight,
                }
                for container_id, state in sorted(self._states.items())
                if state.count > 0
                or state.best_score is not None
                or state.best_snapshot_path is not None
            },
        }
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, separators=(",", ":"), sort_keys=True),
                encoding="utf-8",
            )
            temporary.replace(self._state_path)
        except OSError as error:
            temporary.unlink(missing_ok=True)
            raise CoordinatorError(
                "infrastructure", "failed to persist submission quota"
            ) from error

    def _podman(
        self,
        args: list[str],
        *,
        timeout: float = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.container_engine, *args],
                text=True,
                capture_output=True,
                timeout=timeout,
                check=check,
            )
        except subprocess.TimeoutExpired as error:
            raise CoordinatorError(
                "engine_timeout", "container engine command timed out"
            ) from error
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "").strip()
            raise CoordinatorError(
                "engine_error", detail[-2000:] or "container engine command failed"
            ) from error

    def _podman_best_effort(self, args: list[str], *, timeout: float) -> None:
        try:
            self._podman(args, timeout=timeout, check=False)
        except CoordinatorError:
            pass

    def _record_evaluation_event(self, event: dict[str, Any]) -> None:
        payload = {
            "schema_version": 1,
            "recorded_at_unix": round(time.time(), 6),
            **self._source_provenance,
            **event,
        }
        line = json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n"
        try:
            with self._event_lock:
                with self._event_path.open("a", encoding="utf-8") as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
        except OSError:
            # Audit logging must not turn a completed evaluation into a false score.
            pass

    def authorize(self, supplied: str | None) -> None:
        if self.token is None:
            return
        if supplied is None or not hmac.compare_digest(self.token, supplied):
            raise CoordinatorError("unauthorized", "invalid submission credential")

    def _inspect_work(self, task: TaskConfig, container_id: str) -> dict[str, Any]:
        if (
            not container_id
            or len(container_id) > 128
            or any(c not in "0123456789abcdef" for c in container_id.lower())
        ):
            raise CoordinatorError("invalid_request", "invalid Work container identity")
        result = self._podman(["inspect", container_id], timeout=30)
        records = json.loads(result.stdout)
        if len(records) != 1:
            raise CoordinatorError("invalid_request", "Work container was not found")
        submitted_info = records[0]
        if not bool(submitted_info.get("State", {}).get("Running")):
            raise CoordinatorError("invalid_request", "Work container is not running")
        submitted_labels = submitted_info.get("Config", {}).get("Labels") or {}
        submitted_service = submitted_labels.get("com.docker.compose.service")
        network_sidecar_id: str | None = None
        info = submitted_info
        if submitted_service != "main":
            if submitted_service != "harbor-docker-egress-control-sidecar":
                raise CoordinatorError(
                    "invalid_request", "container is not a Harbor main service"
                )
            project = submitted_labels.get("com.docker.compose.project")
            if not project:
                raise CoordinatorError(
                    "invalid_request", "Harbor sidecar is missing its compose project"
                )
            main_result = self._podman(
                [
                    "ps",
                    "--filter",
                    f"label=com.docker.compose.project={project}",
                    "--filter",
                    "label=com.docker.compose.service=main",
                    "--format",
                    "{{.ID}}",
                ],
                timeout=30,
            )
            main_ids = [line.strip() for line in main_result.stdout.splitlines() if line.strip()]
            if len(main_ids) != 1:
                raise CoordinatorError(
                    "invalid_request", "Harbor project does not have one running main service"
                )
            main_inspect = self._podman(["inspect", main_ids[0]], timeout=30)
            main_records = json.loads(main_inspect.stdout)
            if len(main_records) != 1:
                raise CoordinatorError("invalid_request", "Harbor main service was not found")
            info = main_records[0]
            network_sidecar_id = str(submitted_info.get("Id", ""))
            if not bool(info.get("State", {}).get("Running")):
                raise CoordinatorError("invalid_request", "Work container is not running")
        labels = info.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.service") != "main":
            raise CoordinatorError("invalid_request", "resolved container is not main")
        working_dir = labels.get("com.docker.compose.project.working_dir")
        expected = str(self.work_environment_dir or (task.task_dir / "environment").resolve())
        if not working_dir or Path(working_dir).resolve() != Path(expected):
            raise CoordinatorError(
                "invalid_request", "container does not belong to the requested task"
            )
        if task.strict_work_isolation:
            host_config = info.get("HostConfig") or {}
            if not bool(host_config.get("ReadonlyRootfs")):
                raise CoordinatorError(
                    "invalid_request", "Work container root filesystem is not read-only"
                )
            network_mode = str(host_config.get("NetworkMode", ""))
            sidecar_network_modes = (
                {
                    f"container:{network_sidecar_id}",
                    f"container:{network_sidecar_id[:12]}",
                }
                if network_sidecar_id
                else set()
            )
            if network_mode != "none" and network_mode not in sidecar_network_modes:
                raise CoordinatorError(
                    "invalid_request", "Work container network is not disabled"
                )
            workspace_mounts = [
                mount
                for mount in info.get("Mounts", [])
                if mount.get("Destination") == "/app/workspace"
            ]
            if len(workspace_mounts) != 1 or not bool(workspace_mounts[0].get("RW")):
                raise CoordinatorError(
                    "invalid_request",
                    "Work container must use one writable workspace volume",
                )
        return info

    def _begin_submission(
        self, container_id: str, task: TaskConfig
    ) -> SubmissionTicket:
        now = time.monotonic()
        with self._lock:
            state = self._states.setdefault(container_id, RunState())
            if state.task_id is not None and state.task_id != task.task_id:
                raise CoordinatorError(
                    "invalid_request", "Work container changed autoresearch task"
                )
            remaining = max(0, task.max_submissions - state.count)
            if state.in_flight:
                raise CoordinatorError(
                    "policy",
                    "a submission is already running",
                    remaining_submissions=remaining,
                )
            if state.count >= task.max_submissions:
                raise CoordinatorError(
                    "policy", "submission limit reached", remaining_submissions=0
                )
            if state.count and now - state.last_started < task.cooldown_sec:
                raise CoordinatorError(
                    "policy",
                    "submission cooldown is active",
                    remaining_submissions=remaining,
                )
            state.in_flight = True
            state.count += 1
            state.task_id = task.task_id
            state.last_started = now
            try:
                self._persist_states_locked()
            except CoordinatorError:
                state.in_flight = False
                state.count -= 1
                state.last_started = 0.0
                raise
            return SubmissionTicket(
                submission_index=state.count,
                remaining_submissions=task.max_submissions - state.count,
            )

    def _complete_submission(
        self,
        container_id: str,
        score: float,
        snapshot_path: str,
        submission_id: str,
    ) -> SubmissionSettlement:
        with self._lock:
            state = self._states.setdefault(container_id, RunState())
            if not state.in_flight:
                raise CoordinatorError(
                    "infrastructure", "submission state is not active"
                )
            previous_best = state.best_score
            previous_submission_id = state.best_submission_id
            previous_snapshot_path = state.best_snapshot_path
            is_best = previous_best is None or score >= previous_best
            keep_snapshot = previous_best is None or score > previous_best
            if previous_best is None or score > previous_best:
                state.best_score = score
                state.best_submission_id = submission_id
                state.best_snapshot_path = snapshot_path
            state.in_flight = False
            try:
                self._persist_states_locked()
            except CoordinatorError:
                state.best_score = previous_best
                state.best_submission_id = previous_submission_id
                state.best_snapshot_path = previous_snapshot_path
                state.in_flight = True
                raise
            return SubmissionSettlement(
                best_score=float(state.best_score),
                is_best=is_best,
                keep_snapshot=keep_snapshot,
                obsolete_snapshot_path=(
                    previous_snapshot_path if keep_snapshot else None
                ),
            )

    def _abort_submission(self, container_id: str, *, refund: bool) -> None:
        with self._lock:
            state = self._states.setdefault(container_id, RunState())
            if not state.in_flight:
                return
            previous_count = state.count
            previous_started = state.last_started
            state.in_flight = False
            if refund:
                state.count = max(0, state.count - 1)
                state.last_started = 0.0
            try:
                self._persist_states_locked()
            except CoordinatorError:
                state.count = previous_count
                state.last_started = previous_started
                state.in_flight = True
                raise

    @staticmethod
    def _mount_arg(source: Path, target: str, read_only: bool) -> str:
        mode = "ro" if read_only else "rw"
        return f"{source}:{target}:{mode}"

    def _export_trusted_inputs(
        self,
        base_image: str,
        task: TaskConfig,
        export_root: Path,
    ) -> list[Mount]:
        if not task.trusted_exports:
            return []
        export_root.mkdir(parents=True, exist_ok=True)
        container_name = f"gb-export-{uuid.uuid4().hex[:12]}"
        self._podman(
            ["create", "--name", container_name, base_image, "true"], timeout=60
        )
        mounts: list[Mount] = []
        try:
            for index, item in enumerate(task.trusted_exports):
                destination = export_root / f"input-{index}"
                self._podman(
                    ["cp", f"{container_name}:{item.source}", str(destination)],
                    timeout=180,
                )
                mounts.append(Mount(source=destination, target=item.target))
        finally:
            self._podman(["rm", "-f", container_name], timeout=30, check=False)
        return mounts

    def _snapshot_submission_files(
        self,
        container_id: str,
        task: TaskConfig,
        submission_id: str,
    ) -> Path:
        """Capture an atomic, bounded source snapshot without committing rootfs."""
        temporary = self.snapshot_root / f".{submission_id}.tmp"
        destination_root = self.snapshot_root / submission_id
        shutil.rmtree(temporary, ignore_errors=True)
        temporary.mkdir(parents=True)
        paused = False
        try:
            self._podman(["pause", container_id], timeout=30)
            paused = True
            total = 0
            for relative in task.submission_files:
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self._podman(
                        [
                            "cp",
                            f"{container_id}:/app/workspace/{relative}",
                            str(destination),
                        ],
                        timeout=120,
                    )
                except CoordinatorError as error:
                    raise CoordinatorError(
                        "submission",
                        f"missing declared submission file: {relative}",
                        submission_failure=True,
                    ) from error
                try:
                    starter = task.submission_source_dir / relative
                    if starter.is_dir() != destination.is_dir():
                        raise ValueError("submission path changed file/directory kind")
                    total += _submission_path_size(destination)
                except (OSError, ValueError) as error:
                    raise CoordinatorError(
                        "submission",
                        f"invalid declared submission path: {relative}",
                        submission_failure=True,
                    ) from error
                if total > task.submission_max_mb * 1024 * 1024:
                    raise CoordinatorError(
                        "submission",
                        "declared submission files exceed the size limit",
                        submission_failure=True,
                    )
            temporary.replace(destination_root)
            return destination_root
        finally:
            if paused:
                self._podman_best_effort(["unpause", container_id], timeout=30)
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _snapshot_canonical_baseline(
        self,
        task: TaskConfig,
        submission_id: str,
    ) -> Path:
        """Snapshot immutable starter sources for the first scored submission.

        The baseline must not depend on whether an agent followed the prompt
        before its first call to submit_score.  Source paths were already
        validated as regular task files/directories by _submission_files().
        """
        temporary = self.snapshot_root / f".{submission_id}.tmp"
        destination_root = self.snapshot_root / submission_id
        shutil.rmtree(temporary, ignore_errors=True)
        temporary.mkdir(parents=True)
        total = 0
        try:
            for relative in task.submission_files:
                source = task.submission_source_dir / relative
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, destination, symlinks=True)
                else:
                    shutil.copyfile(source, destination, follow_symlinks=False)
                total += _submission_path_size(destination)
                if total > task.submission_max_mb * 1024 * 1024:
                    raise CoordinatorError(
                        "infrastructure",
                        "canonical baseline exceeds the submission size limit",
                    )
            temporary.replace(destination_root)
            return destination_root
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    def _run_container(
        self,
        *,
        name: str,
        image: str,
        command: list[str],
        mounts: list[tuple[Path, str, bool]],
        timeout: int,
        task: TaskConfig,
        read_only: bool,
        user: str | None = None,
        executable_tmp: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        args = [
            "run",
            "--name",
            name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(task.pids_limit),
            "--cpus",
            str(task.cpus),
            "--memory",
            f"{task.memory_mb}m",
        ]
        if read_only:
            args.extend(
                [
                    "--read-only",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=2g" + (",exec" if executable_tmp else ""),
                    "--tmpfs",
                    "/app/output:rw,nosuid,nodev,size=2g",
                    "--tmpfs",
                    "/run:rw,nosuid,nodev,size=64m",
                    "--tmpfs",
                    "/root:rw,nosuid,nodev,size=256m",
                ]
            )
        if user is not None:
            args.extend(["--user", user])
        for source, target, mount_read_only in mounts:
            args.extend(["--volume", self._mount_arg(source, target, mount_read_only)])
        for key, value in task.evaluation_env:
            args.extend(["--env", key + "=" + value])
        if task.evaluation_deadline is not None:
            timeout = min(timeout, task.evaluation_deadline - time.monotonic())
            if timeout <= 0:
                raise CoordinatorError("verifier_timeout", "Final evaluation time budget exhausted")
        args.extend([image, *command])
        try:
            return self._podman(args, timeout=timeout, check=False)
        finally:
            self._podman(["rm", "-f", name], timeout=30, check=False)

    @staticmethod
    def _artifact_size(root: Path) -> int:
        total = 0
        for path in root.rglob("*"):
            if path.is_symlink():
                raise CoordinatorError(
                    "submission",
                    "artifact contains a symbolic link",
                    submission_failure=True,
                )
            if path.is_file():
                total += path.stat().st_size
        return total

    def evaluate(self, task_id: str, container_id: str) -> EvaluationResult:
        task = self.tasks.get(task_id)
        if task is None:
            raise CoordinatorError("invalid_request", "unknown task")
        if task.evaluation_timeout_sec is not None:
            task = replace(task, evaluation_deadline=time.monotonic() + task.evaluation_timeout_sec)
        info = self._inspect_work(task, container_id)
        canonical_id = str(info["Id"])
        ticket = self._begin_submission(canonical_id, task)
        pool_ticket: object | None = None
        settled = False
        submission_id = uuid.uuid4().hex
        started_at = time.monotonic()
        event: dict[str, Any] = {
            "evaluation_id": submission_id,
            "task_id": task_id,
            "container_id_sha256": hashlib.sha256(canonical_id.encode()).hexdigest(),
            "submission_index": ticket.submission_index,
            "baseline": False,
            "status": "infrastructure_error",
            "score": None,
            "best_score": None,
            "apply_returncode": None,
            "verify_returncode": None,
            "artifact_bytes": None,
            "base_image_id": str(info.get("Image") or ""),
            "verifier_image": task.verifier_image,
            "task_config_sha256": hashlib.sha256(
                json.dumps(submission_config(task.task_dir), sort_keys=True).encode()
            ).hexdigest(),
        }
        run_dir = self.run_root / f"{task.task_dir.name}-{submission_id}"
        artifact_dir = run_dir / "artifact"
        result_dir = run_dir / "result"
        export_dir = run_dir / "trusted-inputs"
        snapshot_path: Path | None = None
        apply_name = f"gb-apply-{submission_id[:12]}"
        verify_name = f"gb-verify-{submission_id[:12]}"
        keep_snapshot = False
        try:
            pool_ticket = self.evaluation_pool.acquire(task.cpus, task.memory_mb)
            artifact_dir.mkdir(parents=True, mode=0o777)
            result_dir.mkdir(parents=True, mode=0o777)
            artifact_dir.chmod(0o777)
            result_dir.chmod(0o777)
            base_image = str(info["Image"])
            try:
                snapshot_path = self._snapshot_submission_files(
                    canonical_id, task, submission_id
                )
            except CoordinatorError as error:
                if error.submission_failure:
                    snapshot_path = self.snapshot_root / submission_id
                    snapshot_path.mkdir(parents=True, exist_ok=True)
                raise
            exported = self._export_trusted_inputs(base_image, task, export_dir)
            apply_mounts: list[tuple[Path, str, bool]] = [
                (task.apply_script, "/opt/gamebench/apply.py", True),
                (artifact_dir, "/artifact", False),
            ]
            apply_mounts.extend(
                (
                    snapshot_path / relative,
                    f"/app/workspace/{relative}",
                    True,
                )
                for relative in task.submission_files
            )
            apply_mounts.extend((m.source, m.target, True) for m in task.apply_mounts)
            apply_mounts.extend((m.source, m.target, True) for m in task.fixed_mounts)
            apply_mounts.extend((m.source, m.target, True) for m in exported)
            apply = self._run_container(
                name=apply_name,
                image=base_image,
                command=["python3", "/opt/gamebench/apply.py"],
                mounts=apply_mounts,
                timeout=task.apply_timeout_sec,
                task=task,
                read_only=True,
                user=task.apply_user,
                executable_tmp=task.apply_tmp_exec,
            )
            if task.evaluation_env:
                (run_dir / "apply.log").write_text(((apply.stdout or "") + "\n" + (apply.stderr or ""))[-1048576:])
            event["apply_returncode"] = apply.returncode
            if apply.returncode != 0:
                submission_failure, detail = classify_apply_failure(apply)
                if not submission_failure:
                    event["error_category"] = "apply_infrastructure"
                    event["error_detail"] = detail
                    raise CoordinatorError("apply_infrastructure", detail)
                score = 0.0
                settlement = self._complete_submission(
                    canonical_id, score, str(snapshot_path), submission_id
                )
                keep_snapshot = settlement.keep_snapshot
                if settlement.obsolete_snapshot_path:
                    shutil.rmtree(settlement.obsolete_snapshot_path, ignore_errors=True)
                settled = True
                event.update(
                    status="submission_failure",
                    score=score,
                    best_score=settlement.best_score,
                    error_category="submission",
                    error_detail=detail,
                )
                return EvaluationResult(
                    score=score,
                    best_score=settlement.best_score,
                    is_best=settlement.is_best,
                    baseline=False,
                    submission_index=ticket.submission_index,
                    remaining_submissions=ticket.remaining_submissions,
                )
            artifact_bytes = self._artifact_size(artifact_dir)
            event["artifact_bytes"] = artifact_bytes
            if artifact_bytes == 0:
                event["error_category"] = "apply_infrastructure"
                event["error_detail"] = "Apply returned success without an artifact"
                raise CoordinatorError(
                    "apply_infrastructure",
                    "Apply returned success without an artifact",
                )
            if artifact_bytes > task.artifact_max_mb * 1024 * 1024:
                score = 0.0
                settlement = self._complete_submission(
                    canonical_id, score, str(snapshot_path), submission_id
                )
                keep_snapshot = settlement.keep_snapshot
                if settlement.obsolete_snapshot_path:
                    shutil.rmtree(settlement.obsolete_snapshot_path, ignore_errors=True)
                settled = True
                event.update(
                    status="submission_failure",
                    score=score,
                    best_score=settlement.best_score,
                    error_category="submission",
                    error_detail="artifact exceeds the configured size limit",
                )
                return EvaluationResult(
                    score=score,
                    best_score=settlement.best_score,
                    is_best=settlement.is_best,
                    baseline=False,
                    submission_index=ticket.submission_index,
                    remaining_submissions=ticket.remaining_submissions,
                )

            verify_mounts: list[tuple[Path, str, bool]] = [
                (task.task_dir / "tests", "/tests", True),
                (task.score_script, "/opt/gamebench/score.py", True),
                (artifact_dir, "/artifact", True),
                (result_dir, "/logs/verifier", False),
            ]
            cache_key = hashlib.sha256(
                f"{task.cache_version}:{task.verifier_image or base_image}".encode()
            ).hexdigest()[:20]
            cache_dir = (
                self.repo_root / ".autoresearch-cache" / task.task_dir.name / cache_key
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            verify_mounts.append((cache_dir, "/opt/gamebench/cache", False))
            verify = self._run_container(
                name=verify_name,
                image=task.verifier_image or base_image,
                command=["python3", "/opt/gamebench/score.py"],
                mounts=verify_mounts,
                timeout=task.score_timeout_sec,
                task=task,
                read_only=True,
            )
            event["verify_returncode"] = verify.returncode
            if verify.returncode != 0:
                detail = (verify.stderr or verify.stdout or "").strip()
                raise CoordinatorError("verifier", detail[-2000:] or "verifier failed")
            reward_path = result_dir / "reward.txt"
            if not reward_path.is_file():
                raise CoordinatorError("verifier", "verifier did not produce a reward")
            try:
                score = float(reward_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError) as error:
                raise CoordinatorError(
                    "verifier", "verifier produced an invalid reward"
                ) from error
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise CoordinatorError(
                    "verifier", "verifier produced a reward outside finite [0, 1]"
                )
            score = round(max(0.0, min(1.0, score)), 6)
            notices = read_feedback_notices(result_dir)
            settlement = self._complete_submission(
                canonical_id, score, str(snapshot_path), submission_id
            )
            keep_snapshot = settlement.keep_snapshot
            if settlement.obsolete_snapshot_path:
                shutil.rmtree(settlement.obsolete_snapshot_path, ignore_errors=True)
            settled = True
            event.update(
                status="scored",
                score=score,
                best_score=settlement.best_score,
            )
            if notices:
                event["notices"] = list(notices)
            return EvaluationResult(
                score=score,
                best_score=settlement.best_score,
                is_best=settlement.is_best,
                baseline=False,
                submission_index=ticket.submission_index,
                remaining_submissions=ticket.remaining_submissions,
                notices=notices,
            )
        except CoordinatorError as error:
            event.setdefault("error_category", error.category)
            event.setdefault("error_detail", str(error)[-2000:])
            if error.submission_failure:
                score = 0.0
                if snapshot_path is None:
                    self._abort_submission(canonical_id, refund=True)
                    settled = True
                    raise
                settlement = self._complete_submission(
                    canonical_id, score, str(snapshot_path), submission_id
                )
                keep_snapshot = settlement.keep_snapshot
                if settlement.obsolete_snapshot_path:
                    shutil.rmtree(settlement.obsolete_snapshot_path, ignore_errors=True)
                settled = True
                event.update(
                    status="submission_failure",
                    score=score,
                    best_score=settlement.best_score,
                )
                return EvaluationResult(
                    score=score,
                    best_score=settlement.best_score,
                    is_best=settlement.is_best,
                    baseline=False,
                    submission_index=ticket.submission_index,
                    remaining_submissions=ticket.remaining_submissions,
                )
            if not settled:
                self._abort_submission(canonical_id, refund=True)
                settled = True
            raise
        except Exception:
            if not settled:
                self._abort_submission(canonical_id, refund=True)
                settled = True
            raise
        finally:
            event["duration_sec"] = round(time.monotonic() - started_at, 6)
            self._record_evaluation_event(event)
            if pool_ticket is not None:
                self.evaluation_pool.release(pool_ticket, task.cpus, task.memory_mb)
            self._podman_best_effort(["rm", "-f", apply_name, verify_name], timeout=30)
            if snapshot_path is not None and not keep_snapshot:
                shutil.rmtree(snapshot_path, ignore_errors=True)
            if not self.keep_runs:
                shutil.rmtree(run_dir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "GameBenchAutoresearch/1"

    @property
    def coordinator(self) -> Evaluator:
        return self.server.coordinator  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        peer = (
            self.client_address[0]
            if isinstance(self.client_address, tuple) and self.client_address
            else "local-socket"
        )
        print(f"{peer} - {fmt % args}", flush=True)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True, "protocol": 1})
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/v1/submit":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise CoordinatorError("invalid_request", "invalid request size")
            request = json.loads(self.rfile.read(length))
            if request.get("protocol") != 1:
                raise CoordinatorError("invalid_request", "unsupported protocol")
            authorization = self.headers.get("Authorization")
            supplied = authorization.removeprefix("Bearer ") if authorization else None
            self.coordinator.authorize(supplied)
            result = self.coordinator.evaluate(
                task_id=str(request.get("task_id", "")),
                container_id=str(request.get("container_id", "")),
            )
            response: dict[str, Any] = {
                "score": result.score,
                "best_score": result.best_score,
                "is_best": result.is_best,
                "baseline": result.baseline,
                "submission_index": result.submission_index,
                "remaining_submissions": result.remaining_submissions,
            }
            notices = sanitize_notices(list(result.notices))
            if notices:
                response["notices"] = list(notices)
            self._json(HTTPStatus.OK, response)
        except CoordinatorError as error:
            print(
                json.dumps(
                    {"coordinator_error": error.category, "detail": str(error)},
                    sort_keys=True,
                ),
                flush=True,
            )
            status = (
                HTTPStatus.UNAUTHORIZED
                if error.category == "unauthorized"
                else HTTPStatus.BAD_REQUEST
            )
            payload: dict[str, Any] = {"error": error.category}
            if error.remaining_submissions is not None:
                payload["remaining_submissions"] = error.remaining_submissions
            self._json(status, payload)
        except (json.JSONDecodeError, TypeError, ValueError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "infrastructure"})



def coordinator(repo, output, task_name, token):
    manifest = output / 'tasks.jsonl'
    import json
    manifest.write_text(json.dumps({'task_name': task_name}) + '\n')
    return Evaluator(repo, output / 'online', token=token,
                            keep_runs=True, task_manifest_paths=(manifest,),
                            container_engine='docker')


def final_score(repo, output, task_name, cid, verifier_image, *, stage=None):
    instance = Evaluator(repo, output / 'final', keep_runs=True,
                                task_manifest_paths=(output / 'tasks.jsonl',),
                                container_engine='docker')
    # Final scoring has its own ledger and does not consume online quota.
    instance.tasks[task_name] = replace(instance.tasks[task_name], max_submissions=1, verifier_image=verifier_image)
    from .rendering import TASKS, configuration
    if task_name in TASKS:
        instance.tasks[task_name] = configuration(instance.tasks[task_name], output / 'render-final', phase='final', stage=stage)
    started = time.monotonic()
    result = asdict(instance.evaluate(task_name, cid))
    if task_name in TASKS:
        rewards = list((output / 'final').glob('*/result/reward.json'))
        if not rewards:
            event = json.loads(instance._event_path.read_text().splitlines()[-1])
            if event.get('status') != 'submission_failure':
                raise CoordinatorError('verifier', 'Expected one private final reward record')
            from .rendering import rejected_candidate_reward
            task = instance.tasks[task_name]
            result['rewards'] = rejected_candidate_reward(
                instance, task, output, started + task.evaluation_timeout_sec)
            result['score'] = result['best_score'] = round(result['rewards']['reward'], 6)
            result['candidate_rejected'] = True
        elif len(rewards) == 1:
            result['rewards'] = json.loads(rewards[0].read_text())
        else:
            raise CoordinatorError('verifier', 'Ambiguous private final rewards')
    return result
