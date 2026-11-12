"""Sequential task stages, with task-owned gates and no implicit submissions.

The execution backend supplies isolated setup, agent and verifier operations.
This module never rewrites instructions, rewards, time limits or online quota.
"""
from dataclasses import dataclass
from pathlib import Path
import math
import re


@dataclass(frozen=True)
class Stage:
    name: str
    instruction: Path
    workdir: Path
    agent_timeout: float
    verifier_timeout: float
    verifier_env: dict
    thresholds: dict


def stage_plan(task, spec):
    task = Path(task)
    if spec.get('multi_step_reward_strategy') != 'final':
        raise ValueError('Only the task-declared final-stage reward strategy is supported')
    stages, seen = [], set()
    for entry in spec.get('steps', []):
        name = entry.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]*', name) or name in seen:
            raise ValueError('Invalid or duplicate stage name')
        seen.add(name)
        folder = task / 'steps' / name
        if not (folder / 'instruction.md').is_file() or not (folder / 'workdir').is_dir():
            raise ValueError(f'{name}: missing instruction or stage workdir')
        if folder.is_symlink() or not folder.resolve().is_relative_to(task.resolve()):
            raise ValueError('Stage escapes task directory')
        agent_timeout = entry.get('agent', {}).get('timeout_sec', spec['agent']['timeout_sec'])
        verifier_timeout = entry.get('verifier', {}).get('timeout_sec', spec['verifier']['timeout_sec'])
        for value in (agent_timeout, verifier_timeout):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError('Invalid stage timeout')
        thresholds = entry.get('min_reward', {})
        if isinstance(thresholds, (int, float)) and not isinstance(thresholds, bool):
            thresholds = {'reward': thresholds}
        if not isinstance(thresholds, dict) or any(
            not isinstance(key, str) or isinstance(value, bool)
            or not isinstance(value, (int, float)) or not math.isfinite(value)
            for key, value in thresholds.items()
        ):
            raise ValueError('Invalid stage gate')
        stages.append(Stage(name, folder / 'instruction.md', folder / 'workdir',
                            agent_timeout, verifier_timeout,
                            dict(entry.get('verifier', {}).get('env', {})), dict(thresholds)))
    if not stages:
        raise ValueError('No stages declared')
    return stages


def gate_passed(rewards, thresholds):
    """Missing/non-finite metrics must never unlock private later-stage inputs."""
    for key, threshold in thresholds.items():
        actual = rewards.get(key)
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) or not math.isfinite(actual):
            return False
        if actual < threshold:
            return False
    return True


def execute_stages(plan, backend):
    """Use one workspace/session; stop on a failed gate, never retry implicitly.

    backend.verify must run the expert's end-of-stage verifier, NOT the online
    submit_score scorer. In particular, Three.js online scores target L3 even
    while the progression verifier evaluates an earlier stage's rung_score.
    The backend retains one online ledger across stages. No baseline call is
    made here. Errors propagate so infrastructure failures cannot become zero.
    """
    history = []
    for index, stage in enumerate(plan):
        backend.settle_online()
        backend.setup(stage)
        terminal = backend.agent(stage, resume=index > 0)
        if terminal not in {'finished', 'timeout', 'agent_error'}:
            raise ValueError('Unknown agent terminal state')
        backend.settle_online()
        rewards = backend.verify(stage)
        score = rewards.get('reward')
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('Invalid final verifier reward')
        passed = gate_passed(rewards, stage.thresholds)
        record = {'stage': stage.name, 'agent_status': terminal,
                  'rewards': rewards, 'gate_passed': passed}
        history.append(record)
        backend.record(record)
        if not passed:
            break
    # Task contract says FINAL: last attempted stage, not best/mean/L3 forced.
    return {'score': history[-1]['rewards']['reward'], 'stages': history,
            'valid_for_ranking': all(r['agent_status'] in {'finished', 'timeout'} for r in history)}


class DockerStageBackend:
    """Local/Nex-Docker backend using the same core agent and evaluator."""
    def __init__(self, config, task, spec, environment, service, verifier_image):
        self.config, self.task, self.spec = config, Path(task), spec
        self.environment, self.service = environment, service
        self.verifier_image = verifier_image
        self.base_scoring = service.tasks[self.task.name]
        self.output = environment.output
        self.current = None

    def settle_online(self):
        import time
        timeout = self.base_scoring.apply_timeout_sec + self.base_scoring.score_timeout_sec + 60
        deadline = time.monotonic() + timeout
        while True:
            with self.service._lock:
                busy = any(s.in_flight for s in self.service._states.values())
            if not busy:
                return
            if time.monotonic() > deadline:
                raise RuntimeError('Online evaluation did not settle before stage transition')
            time.sleep(.2)

    def setup(self, stage):
        import json
        import tarfile
        from .environment import STOP
        from .rendering import configuration
        if STOP.is_set():
            raise InterruptedError('Run interrupted before stage setup')
        self.current = self.output / 'stages' / stage.name
        self.current.mkdir(parents=True, exist_ok=False)
        archive = self.current / 'public-stage.tar'
        with tarfile.open(archive, 'w') as out:
            for path in stage.workdir.rglob('*'):
                if path.is_symlink() or not (path.is_file() or path.is_dir()):
                    raise ValueError('Stage inputs must be ordinary files/directories')
                out.add(path, arcname=str(path.relative_to(stage.workdir)), recursive=False)
        # docker cp rejects read-only-root containers, even for tmpfs targets.
        # Stream only the trusted stage payload into the writable work volume.
        import subprocess
        with archive.open('rb') as source:
            transfer = subprocess.run(
                ['docker', 'exec', '-i', '--user', '1000:1000', self.environment.cid,
                 'tar', '--no-same-owner', '-xf', '-', '-C', '/app/workspace'],
                stdin=source, capture_output=True, timeout=120)
        if transfer.returncode:
            raise RuntimeError('Could not install public stage inputs')
        if (stage.workdir / 'setup.sh').is_file():
            self.environment.exec('cd /app/workspace && bash setup.sh', timeout=120)
        archive.unlink()
        self.service.tasks[self.task.name] = configuration(
            self.base_scoring, self.current / 'online-config', stage=stage)
        (self.current / 'stage.json').write_text(json.dumps({
            'name': stage.name, 'agent_timeout': stage.agent_timeout,
            'verifier_timeout': stage.verifier_timeout, 'thresholds': stage.thresholds,
            'verifier_env': stage.verifier_env}, indent=2))

    def agent(self, stage, resume):
        import shutil
        from .agent import Agent
        try:
            return Agent(self.config).run(self.environment, stage.instruction.read_text(),
                                          stage.agent_timeout, resume=resume)
        finally:
            logs = self.current / 'agent'
            logs.mkdir(exist_ok=True)
            for name in ['pi.txt', 'usage-summary.json', 'instruction.txt', 'provider.ts', 'settings.json']:
                source = self.output / 'agent' / name
                if source.exists():
                    shutil.copy2(source, logs / name)

    def verify(self, stage):
        import shutil
        import json
        from .evaluator import final_score
        shutil.copy2(self.output / 'tasks.jsonl', self.current / 'tasks.jsonl')
        result = final_score(self.task.parent.parent, self.current, self.task.name,
                             self.environment.cid, self.verifier_image, stage=stage)
        (self.current / 'final-score.json').write_text(json.dumps(result, indent=2))
        return result['rewards']

    def record(self, record):
        import json
        (self.current / 'result.json').write_text(json.dumps(record, indent=2, allow_nan=False))
