# Download task packages

The private frozen dataset is [GameDevBenchProTeam/GameAtlas](https://huggingface.co/datasets/GameDevBenchProTeam/GameAtlas). It contains benchmark inputs and verifiers. The separate model-results dataset holds evaluation runs.

From the GameAtlas clone, install the download client and authenticate with an account that can read the dataset:

```bash
python3 -m pip install huggingface_hub
hf auth login
python3 -m gameatlasharness.download --task psd-banner-bridge-v1
```

For the full collection, run `python3 -m gameatlasharness.download`. Both commands use the dataset revision specified in [dataset-lock.json](dataset-lock.json), check the SHA-256 of every selected file and restore the executable permissions used by the task packages.

## Layout

Each directory under `tasks/` is one task. `task.toml` defines its environment, limits and scoring configuration. The agent receives `instruction.md` and the public workspace prepared by `environment/`. The `tests/` directory contains the trusted apply and scoring logic, including private references where required. Multi-stage tasks declare their stages in `task.toml` and keep stage instructions under `steps/`.

The candidate includes 89 single-stage tasks and two four-stage Sponza tasks. Each multi-stage task counts once in the 91-task benchmark. Task files remain on the host; use the harness to expose the prescribed workspace to the agent.

To run all 91 tasks, download the entire collection and remove the example’s single-task `tasks` list from your run configuration. Automatic discovery only sees task packages present locally.

## Release alignment

The lock pins the frozen 91-task collection plus the UV-010 padding candidate-memory fix used for rescoring. It records the source commit, immutable Hugging Face revision and manifest SHA-256. The harness matches the standalone `opensource` harness, with only the `gameatlasharness` package name and HF download entry point adapted. Repositories remain private during release preparation.

The task resource profile is taken from the actual frozen evaluation snapshots: every task uses 2 CPUs / 4096 MiB for work and 4 CPUs / 8192 MiB for scoring. The source commit alone is insufficient to reproduce this profile because its historical resource defaults differ. `runtime_profile` in the lock records this correction; internal deployment image addresses are not required.
