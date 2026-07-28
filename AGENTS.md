# Team Agent Harness Project Rules

## Scope And Sources Of Truth

- The workspace root contains shared design records in `docs/superpowers/specs/` and the runnable backend in `team_agent_harness/backend/`.
- For current runtime behavior, read `team_agent_harness/backend/README.md` and `docs/superpowers/specs/2026-07-10-durable-run-worker-context-design.md` before older phase plans.
- The 2026-06-15 design and implementation plan are historical baselines. Do not treat their synchronous-run and no-worker statements as current behavior.

## Runtime Boundaries

- The backend is a local, single-Uvicorn-process FastAPI application backed by SQLite and local artifact files.
- UI and operator CLI runs use the in-process `RunWorker`; `POST /runs` remains backward compatible with `background=false` for synchronous callers.
- SQLite run and queue records are authoritative. The in-memory queue only wakes the worker.
- Completed agent runs are recovery checkpoints. A hard restart may repeat the interrupted external model step; exactly-once external execution is not guaranteed.
- Context injection reads only completed-attempt artifacts and applies each `WorkflowStep.context_policy` budget.
- External ACP processes, distributed workers, deployment, and public hosting are out of scope.

## Protected Local State

- Do not edit `.env.local`, credentials, SQLite schema/data, `data/artifacts/`, or existing run output unless the operator explicitly asks.
- Do not delete old artifacts or attempt records during recovery or cleanup.
- Do not initialize or repair Git automatically. The workspace contains a `.git` directory, but Git currently reports that this is not a repository.
- Generated browser verification files belong under `team_agent_harness/backend/output/playwright/`.

## Working Directory And Verification

Run backend commands from `team_agent_harness/backend/`.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app scripts
.\scripts\start-litellm-harness.ps1
```

- Prefer targeted tests first, then the full suite for worker, runner, API contract, or context changes.
- The normal local endpoints are Harness `http://127.0.0.1:8014/` and LiteLLM `http://127.0.0.1:4000/`.
- Browser smoke tests must not call paid models unless the operator explicitly confirms real-model execution. Use request interception for UI-only polling checks.

## Documentation Discipline

- Update the current README and the 2026-07-10 durable-run design when runtime, recovery, queue, lock, or context behavior changes.
- Keep old dated specs as historical evidence; mark supersession explicitly instead of silently rewriting history.
- Keep local filesystem, live service, Git, and remote state separate in reports.
