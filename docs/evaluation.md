# Evaluation protocol

GameAtlas uses task-specific programmatic verifiers. Each task package defines its inputs, deliverables, build environments, scoring code and resource limits. Exact metrics and thresholds belong to the pinned dataset revision.

## Online feedback and final score

The agent starts with the public instructions and workspace. No baseline score is submitted automatically. Each accepted `submit_score` evaluates the current candidate and consumes one submission, including the first. The agent can use the feedback to revise. The final verifier evaluates the final working files independently of the online budget. The highest online score is not substituted for the final score.

The two Sponza tasks each have four stages. Each stage has its own instructions, time limits and gate. Workspace, agent session and submission budget continue across stages. Failure to pass a gate stops progression. Final reward comes from the last attempted stage’s final verifier.

## Reporting

The local runner records reward on a 0–1 scale. Multiply by 100 for display. Use the receipt’s `valid_for_ranking` field. A valid `timeout_scored` result counts when the agent times out and its final files are successfully evaluated. Agent-error diagnostics, infrastructure failures and interrupted attempts do not count as valid results. A valid zero counts as a measured score.

For a category mean, average valid rounds within each task, then average task scores with equal weight. Report coverage as valid tasks divided by all tasks in the declared category. Report overall coverage alongside the overall task mean. Document retry selection; infrastructure retries are attempts at the same round.

## Reproduction

Record the Git commit, HF dataset revision, model ID and protocol, reasoning and sampling settings, limits, image identities, concurrency and any resource overrides. Default task resources are declared in the package. For a scored run, retain the final deliverable and receipts to distinguish evaluator changes from variation in a fresh model attempt.
