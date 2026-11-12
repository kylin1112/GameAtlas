# Run your first task

For a collaborator or coding agent taking over this repository. Run commands
from the repository root. You need **Python 3.11+, Docker, and an
OpenAI-compatible Chat Completions endpoint**. No internal platform is required.

## 1. Check the task

Download the [task packages](../docs/dataset.md) into `tasks/`, then check the host:

```bash
python3 --version
docker info
```

The runner builds Linux AMD64 images. Use an AMD64 machine or configure AMD64
emulation on your Docker host. For a first run, select `psd-banner-bridge-v1`.
Docker needs capacity for the work container, scoring container and relay
overhead. Start with concurrency 1; do not silently reduce task resource limits.

## 2. Configure your model

```bash
cp gameatlasharness/model.example.json model.local.json
```

Edit `model.local.json`:

- Set `model` and `base_url` (for example, `https://your-provider.example/v1`,
  not the full `/chat/completions` URL).
- Set `"tasks": ["psd-banner-bridge-v1"]` and `"concurrency": 1` for this first run.
- Set context/output limits, `vision` and `thinking` to supported model values.
  Pi accepts `off`, `minimal`, `low`, `medium`, `high`, or `xhigh`. Map provider
  levels explicitly when needed, e.g. `"thinking_level_map": {"xhigh": "max"}`.
  Example limits are not recommendations for every model; smaller contexts may
  also need smaller `reserve_tokens` and `keep_recent_tokens`.
- Supply `OPENAI_API_KEY` through your shell or secret manager, never the JSON
  file. An unauthenticated endpoint can use a dummy nonempty value.

The endpoint must be reachable from Docker. For a server on the Docker Desktop
host, use `host.docker.internal` instead of `localhost`.

JSON paths are relative to the config file. Keep this copy at the repository
root. Keep local settings and run outputs outside commits.

## 3. Prepare, check, run

```bash
python3 -m gameatlasharness.run --config model.local.json --prepare
python3 -m gameatlasharness.run --config model.local.json --check
python3 -m gameatlasharness.run --config model.local.json --output runs/first-task
```

`--prepare` creates the pinned Pi runtime if missing, builds missing task images from their Dockerfiles,
and runs preflight. `--check` repeats preflight without downloading anything.
Neither sends model requests. Only the third command starts the evaluation;
leave it running until it returns. Use a fresh output directory for a new run.

## 4. Verify the result

An allocated container or `running` status alone does not prove success. Check
the actual model responses and tool events, online feedback, and final receipt:

```text
runs/first-task/
  manifest.json                         # config and code/task/image identities
  YOUR_TASK_ID/attempt-001/
    result.json                         # terminal status, score, validity
    agent/pi.txt                        # messages and tool events
    agent/sessions/                     # native Pi sessions
    audit/requests.jsonl                # model requests and responses
    online/                             # agent-initiated online evaluations
    final/                              # independent final scoring outputs
```

Print the receipts without dumping prompts or credentials:

```bash
python3 - <<'PY'
import json
from pathlib import Path
for path in sorted(Path("runs/first-task").glob("*/attempt-*/result.json")):
    result = json.loads(path.read_text())
    print(path.parent.parent.name, path.parent.name,
          result.get("status"), result.get("score"),
          "valid:", result.get("valid_for_ranking"))
PY
```

`completed` and `timeout_scored` can carry valid final scores; inspect
`valid_for_ranking`. `agent_error_scored` is diagnostic, not a ranking result.
`infra_error` and `interrupted` are not model zeroes. An online submission is
not the model's final result. Check cleanup fields before declaring the run done.

## 5. Resume or expand

After the previous process has exited, resume the same configuration and output:

```bash
python3 -m gameatlasharness.run --config model.local.json --output runs/first-task --resume
```

This skips terminal attempts. To explicitly retry unsuccessful attempts, use
`--retry-failed` instead; old attempts remain intact. An unfinished attempt or
remaining containers requires inspection before retrying. Do not launch a second
runner against an active output directory or delete its records to bypass a lock.

Once one task passes end-to-end, expand `tasks` (or omit it to discover all
`local-v1` tasks), check resource capacity, and run into a new directory.
Changed config, task files, images, runtime or harness code require a new run.

## When something fails

- **Build fails:** inspect the Docker build output and check access to the public base image and package sources.
- **Preflight fails:** resolve the reported contract, runtime or resource issue
  before calling the model. Ask the maintainer if task changes are needed.
- **Runtime fails:** inspect the receipt, audit and tool events. Distinguish a
  model's failed shell command from a broken environment or model connection.

For an executing agent: do not change instructions, GT, scoring, quotas or time
limits to obtain a pass. Do not expose private tests or judge credentials to the
contestant. Do not start duplicate runs or retry indefinitely. Report the task
and harness versions, model settings, status, final score/validity, unresolved
errors and output path. Share the complete run directory through team storage;
keep experiment records out of Git.
