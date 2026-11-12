# Deliverables and output files

Each task’s `instruction.md` gives the exact paths and formats to edit. Multi-stage tasks provide an instruction for each stage. For example, a UV task and a PSD task have different deliverables; follow the selected task’s paths. The harness collects the declared files from the agent workspace.

During a run, the agent can invoke `submit_score` through Bash to score its current work. The response includes feedback and the remaining submission budget. Submission is optional. Leave the best candidate in the required paths for final evaluation.

A run stores one directory per task and attempt. Use `result.json` for the terminal status, score and ranking validity, `online/` for submission receipts, `final/` for independent grading, and `agent/sessions/` for the conversation and tool calls. The run-level `manifest.json` records identities needed to compare runs. Preserve the attempt alongside its original manifest when reviewing results.
