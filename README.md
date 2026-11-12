# GameAtlas

GameAtlas evaluates whether an agent can turn a game development brief into an editable deliverable. Depending on the task, the agent works from images, video, scene files or layered assets. It can inspect its work, request feedback with `submit_score`, and revise before an independent verifier scores the final files.

[Project website](https://kylin1112.github.io/GameAtlas-site/) · [Task packages](https://huggingface.co/datasets/GameDevBenchProTeam/GameAtlas) · [Evaluation protocol](docs/evaluation.md)

<!-- Project overview figure reserved for the release. -->

## Task collection

The candidate collection contains **91 tasks**. Task selection and score calibration are under review.

| Task family | Tasks | What the agent delivers |
| --- | ---: | --- |
| Image-to-mesh modeling | 26 | Geometry reconstructed from visual references |
| Animation reconstruction | 20 | Character motion reconstructed from a reference clip |
| Camera reconstruction | 10 | Camera pose and field of view for a fixed scene |
| Scene layout | 10 | Object placements in a given scene |
| UV unwrapping | 10 | A UV layout on the supplied mesh |
| Retopology | 10 | A lower-complexity mesh that preserves the source shape |
| Rendering | 3 | Renderer changes evaluated from prescribed camera views |
| PSD composition | 2 | An editable layered composition and its exported image |

## Run your first task

Use Python 3.11 or later, Docker with Linux AMD64 support, and a tool-capable model API. Image-based tasks require vision support. The harness runs directly from the clone.

```bash
git clone https://github.com/kylin1112/GameAtlas.git
cd GameAtlas
```

Follow [Download the task packages](docs/dataset.md) to fetch the pinned candidate. While the repositories are private, both the clone and the dataset download require read access.

```bash
cp gameatlasharness/model.example.json model.local.json
```

Edit `model.local.json` with your model ID, API base URL, supported thinking level and context limits. Supply `OPENAI_API_KEY` in your shell. The example selects one PSD task at concurrency 1.

```bash
python3 -m gameatlasharness.run --config model.local.json --prepare
python3 -m gameatlasharness.run --config model.local.json --output runs/first-task
```

Preparation builds the runtime and task images. The second command starts the agent. Open `runs/first-task/psd-banner-bridge-v1/attempt-001/result.json` to check the final score and `valid_for_ranking`. See the [Quick Start](gameatlasharness/QUICKSTART.md) for trajectory and submission receipts, and the [harness reference](gameatlasharness/README.md) for model protocols, batch runs and recovery.

## Documentation

- [Dataset layout and download](docs/dataset.md)
- [Deliverables and output files](docs/submissions.md)
- [Evaluation, timeouts and aggregation](docs/evaluation.md)
