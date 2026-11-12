# Run GameAtlas locally

**Python 3.11+ · Docker · an OpenAI-compatible Chat Completions endpoint.**
No internal platform, Harbor fork, or platform account is needed. Pi is pinned
to 0.84.2; host execution uses Python’s standard library.

**First time here? Follow the [Quick Start](QUICKSTART.md)** to run and verify
one real task before expanding to a full collection.

## Evaluate your model

From the repository root, copy the example and edit the model name and API URL:

```bash
cp gameatlasharness/model.example.json model.local.json
```

Set `OPENAI_API_KEY` in your environment (do not put it in the file). Adjust
context/output limits, thinking level and `vision` for your actual model.
`api` defaults to `openai-completions`; use `openai-responses` for native
Responses or `anthropic-messages` for native Anthropic. `base_url` includes
`/v1`; the runner adds the endpoint path. `auth_header` accepts `authorization`
(default Bearer), `api-key`, or `x-api-key` (Anthropic default).
Outgoing model requests retain the newest 50 images; older images are replaced
with an omission marker. The original session and trajectory keep every image.
This request budget does not limit how many images an agent may inspect.

`thinking` uses Pi levels; provider-specific `thinking_level_map` and
`chat_template_kwargs` are optional. The example parameters are not universal
model recommendations. An unauthenticated endpoint can use a dummy nonempty key.

```bash
# First run: build the pinned Pi runtime and build the selected task environments.
# This does not call your model or overwrite an existing runtime.
python3 -m gameatlasharness.run --config model.local.json --prepare

# Run one task first.
python3 -m gameatlasharness.run --config model.local.json --task psd-banner-bridge-v1 --output runs/quick-check

# Run all reviewed tasks (or the explicit "tasks" list in your config).
python3 -m gameatlasharness.run --config model.local.json
```

Start with concurrency 1. Increase `concurrency` after checking the single-task
result; preflight includes memory for both agent and scoring containers. First builds fetch public base layers and dependencies.

Paths in JSON are relative to that JSON file; `--output` is relative to the current
directory. Copy the example to the repository root as shown above. `--check` runs
preflight without building, pulling or calling the model.

## Online submissions

The runner does not submit a baseline or prepend baseline feedback. Every
agent-initiated `submit_score` call, including the first, evaluates the current
candidate and consumes the task's configured quota. The retained `baseline`
feedback field is always `false`. Final grading evaluates the final working tree
independently of this quota; it does not select the best earlier submission.
Baseline/GT controls belong to environment smoke validation, outside model runs.

## Results and recovery

Each run records configuration, task hashes, immutable image IDs, runtime contents
and harness code identity. Each task has its own attempt directory:

```text
runs/my-model/TASK_ID/attempt-001/
  result.json              # status, final score, validity, error phase and cleanup
  agent/pi.txt             # actual Pi messages and tool events
  agent/sessions/          # native Pi sessions
  audit/requests.jsonl     # actual model requests/responses, without auth headers
  online/                 # online feedback, snapshots and scoring outputs
  final/                  # independent final artifacts and verifier outputs
```

`completed` and `timeout_scored` have valid final scores. `agent_error_scored`
retains a diagnostic score but is excluded from ranking. `infra_error` and
`interrupted` are not model zeroes. Inspect `valid_for_ranking`, not just `score`.

```bash
# Skip all terminal attempts; no duplicated model calls.
python3 -m gameatlasharness.run --config model.local.json --resume

# Explicitly retry unsuccessful attempts, keeping their original records.
python3 -m gameatlasharness.run --config model.local.json --retry-failed
```

Use the same `--task`/`--output` selection when resuming a selected run. Changed
config, tasks, images or runtime require a new output directory. A run lock rejects
duplicate writers. Unfinished attempts after a host crash require inspection;
the runner will not assume their containers have disappeared. SIGINT/SIGTERM stop
new admission and clean up owned resources. Transport retries are bounded.

## For task maintainers

No per-task launch script is needed. Publish a reviewed task with:

```text
tasks/TASK_ID/
  instruction.md
  task.toml
  environment/workspace/       # immutable starter submission sources
  tests/autoresearch_apply.py
  tests/autoresearch_score.py
  tests/...                   # private scoring data; never mounted in agent
```

Use `[harness] contract = "local-v1"` in `task.toml` only after validating the
following interface. These reviewed tasks and the three explicitly supported rendering tasks are
auto-discovered; other placeholders and drafts without the contract are excluded. Explicit unsupported tasks fail
preflight rather than being silently skipped.

- `task.toml` declares CPU/memory limits and agent time limit.
- The agent image provides Python 3, Bash, CA certificates and UID/GID 1000 with
  writable `/app/workspace` and `/app/output`. Its initial workspace must match the
  published task version.
- Build contexts are `environment/` for work and `tests/` for separate verification.
  Shared verification uses the work image in a fresh container.
- `[harness.submission]` declares the delivery files, quota, scoring scripts and limits.
- The runner mounts Pi read-only at `/opt/node`, uses Linux AMD64 by default,
  disables agent networking and supplies `submit_score` plus a fixed model relay.
  The agent receives no Docker socket, private tests or real API key.
- Apply runs submitted code without GT; an independent Score container evaluates
  inert artifacts with private references and writes a finite `[0,1]` reward to
  `/logs/verifier/reward.txt`. Final scoring uses the final working tree, not the
  highest online submission. Existing Apply/Score semantics are preserved.

**Rendering tasks:** the two Sponza ladders retain their task-defined stages,
instructions, time limits and progression gates. One workspace, Pi session and
submission budget continue across stages; a failed gate stops progression. Stage
records are saved under `stages/STAGE/`. Final reward uses the last attempted
stage's original final verifier, which may differ from online feedback. No stage
submits a baseline automatically. Beautiful Game uses its original single-stage
final verifier. Candidate rendering runs without private references; only inert
captures and source inspection reach the private scorer.

**Scope:** CPU tasks with the reviewed contract, plus these three rendering tasks.
Other multi-stage contracts, PSD VLM judging, GPU, MCP and arbitrary task Compose
services are not supported. Compose files are not
executed: review their requirements before opting into `local-v1`. This is a local
trusted-host runner, not a multi-tenant service. Its feedback listener uses an
ephemeral port and a per-trial token. Preflight cannot reserve resources against
unrelated host processes or guarantee model-server availability.

## Relay request size

The trusted model relay accepts request bodies up to 256 MiB by default. This
byte limit is independent of the model context window: retained image data can
make a multimodal request large even when its token count is within context.
Oversized requests return JSON HTTP 413 (`request_body_too_large`) and are not
forwarded. The trusted relay process can override the byte limit through
`GAMEBENCH_RELAY_MAX_REQUEST_BYTES`; this is a relay-container setting, not a
model parameter. Request content is never truncated to fit the limit.
