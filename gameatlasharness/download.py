"""Fetch and verify one task or the entire pinned GameAtlas collection."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "docs" / "dataset-lock.json"


def verify(root, checksum_file, task, executables):
    prefix = f"tasks/{task}/" if task else "tasks/"
    checked = set()
    for line in checksum_file.read_text().splitlines():
        digest, name = line.split("  ", 1)
        if not name.startswith(prefix):
            continue
        path = (root / name).resolve()
        if not path.is_relative_to((root / "tasks").resolve()):
            raise ValueError(f"Invalid dataset path: {name}")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
                raise ValueError(f"Checksum mismatch: {name}")
        if name in executables:
            path.chmod(path.stat().st_mode | 0o111)
        checked.add(name)
    specs = [root / name for name in checked if name.count("/") == 2 and name.endswith("/task.toml")]
    for spec in specs:
        tomllib.loads(spec.read_text())
    return len(specs), len(checked)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", help="Download one task ID; omit to download the full collection")
    args = parser.parse_args()
    if args.task and not re.fullmatch(r"[a-zA-Z0-9_-]+", args.task):
        parser.error("Invalid task ID")
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        parser.exit(1, "Install the downloader with python3 -m pip install huggingface_hub\n")
    lock = json.loads(LOCK.read_text())
    prefix = f"tasks/{args.task}/" if args.task else "tasks/"
    snapshot_download(repo_id=lock["repo"], repo_type="dataset", revision=lock["revision"],
                      local_dir=ROOT, allow_patterns=[prefix + "*", "SHA256SUMS"])
    checksum_file = ROOT / "SHA256SUMS"
    if hashlib.sha256(checksum_file.read_bytes()).hexdigest() != lock["manifest_sha256"]:
        raise ValueError("The downloaded manifest differs from the pinned collection")
    count, files = verify(ROOT, checksum_file, args.task, set(lock["executables"]))
    if count != (1 if args.task else lock["tasks"]):
        raise ValueError(f"Unexpected task count: {count}")
    print(f"Verified {count} task(s), {files} files at {lock['revision']}")


if __name__ == "__main__":
    main()
