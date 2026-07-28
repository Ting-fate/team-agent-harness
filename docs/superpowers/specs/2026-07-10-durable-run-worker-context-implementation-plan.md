# Implementation Plan: Durable Run Worker and Context Budgets

## Status

Completed and verified on 2026-07-10. The current behavior and remaining boundaries are recorded in `2026-07-10-durable-run-worker-context-design.md` and the backend README.

## Increment 1: Durable background submission

- Add a local `RunWorker` and queue submission APIs.
- Start/stop the worker with FastAPI lifespan.
- Add `RunCreateRequest.background` while preserving synchronous behavior.
- Update UI and operator CLI to submit background runs.
- Verify queued submission returns immediately and completes asynchronously.

## Increment 2: Restart recovery and attempt safety

- Requeue orphaned `RUNNING` runs on worker startup.
- Release orphaned locks and terminalize only non-completed runtime state.
- Allow a new attempt after cancelled/failed incomplete agent runs.
- Ignore artifacts/handoffs from incomplete attempts and suffix retry filenames.
- Verify recovery resumes after completed checkpoints without overwriting files.

## Increment 3: Per-step context policy

- Add typed `ContextPolicy` to `WorkflowStep`.
- Read bounded excerpts from completed artifacts.
- Add excerpt data and budget telemetry to the context envelope.
- Configure conservative budgets in all three workflow packs.
- Raise only Research Planner output budget from 700 to 1000.
- Verify deterministic trimming, incomplete-attempt exclusion, and model payload inclusion.

## Final Verification

- Run targeted worker, API, runner, context, CLI, and UI tests.
- Run the full pytest suite.
- Restart local services and verify background submission in a real browser.
- Confirm no credential/config values were written to source, trace, or artifacts.
