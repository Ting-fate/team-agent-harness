# Durable Run Worker and Context Budgets

## Status

Implemented on 2026-07-10, reliability-hardened on 2026-07-27, security/long-run verified on 2026-07-28, and patched-test/recovery provenance hardened on 2026-08-03. This design extends the original single-process MVP without introducing a distributed queue or a data-format migration. The follow-ups add idempotent SQLite indexes for bounded history and run-scoped lookups, plus additive checkpoint lineage inside existing `AgentRun.input_context` JSON.

## Objective

Make UI and operator-CLI task runs independent from the initiating HTTP request, recover interrupted local runs from the last completed workflow step, and give each workflow position a bounded amount of relevant artifact text without materially degrading response time.

## Current Constraints

- The application is a local, single-Uvicorn-process FastAPI service backed by SQLite.
- Existing `POST /runs` callers may depend on synchronous completion.
- Existing run, queue, lock, trace, artifact, agent-run, session, and runtime-job records remain authoritative.
- Credentials and `.env.local` are out of scope.
- Recovery must not delete existing artifacts or records.
- LiteLLM retries remain conservative; worker recovery must not hide provider quota, validation, or timeout failures.

## API Contract

`RunCreateRequest` gains an additive `background: bool = false` field.

- `background=false`: preserve the existing synchronous behavior and response shape.
- `background=true`: persist the run and queue item, wake the local worker, and immediately return the queued run with HTTP 201.
- The browser UI and safe operator CLI send `background=true`.
- `confirm_real_web` is copied into the run record as `real_web_access_confirmed`. Real web and browser handlers re-check this durable authorization at the tool execution boundary; later provider or bridge availability cannot upgrade an unconfirmed queued/recovered run. Pre-existing run JSON without the additive field defaults to unconfirmed, so no SQLite schema or data migration is required.
- Existing GET endpoints remain the polling and observability contract.
- `GET /tasks` and `GET /runs` return at most 500 newest records by default and accept validated `limit`/`offset` parameters with a hard limit of 1000.
- `GET /health` reports the worker state and returns HTTP 503 if the worker thread is stopped.
- `POST /runs/{run_id}/runtime-jobs/{job_id}/approve?background=true` persists the approval and durable queue item, then returns HTTP 202 before resumed model execution completes. The browser UI uses this mode; omitting the query preserves the synchronous compatibility path.
- A repeated background approval reuses the active queue segment. A completed old job returns its persisted state and cannot advance the current approval. Startup also requeues a persisted `WAITING` + `APPROVED` combination.

## Worker Design

- One in-process worker thread per `HarnessAppState`.
- SQLite run and queue records are authoritative; the in-memory queue is only a wake-up mechanism.
- Worker startup scans persisted runs:
  - `QUEUED` runs are submitted for execution; an orphaned `RUNNING` queue item and lock from the pre-run activation window are cancelled/released first.
  - orphaned `RUNNING` runs have non-terminal step state cancelled, stale locks released, and the run requeued.
  - `WAITING` runs with a persisted `APPROVED` runtime job are requeued to finish the approved step.
  - stale non-terminal queue items for waiting or terminal runs are reconciled to the authoritative run state.
- Completed agent runs are checkpoints only after structural/required eval evidence and exactly one valid handoff for every declared outgoing edge are persisted. Recovery rejects duplicate, extra, missing, mis-targeted, cross-run, or cross-attempt handoffs and reruns the invalid step instead of falling back to an older completed attempt. Before reuse, every artifact file owned by the checkpoint is streamed through its durable content hash; missing, invalid UTF-8, or tampered files invalidate the checkpoint without deleting artifacts or records.
- Explicit DAG steps persist `dependency_lineage` in the consuming `AgentRun.input_context`, mapping every declared dependency to the exact completed dependency attempt and handoff consumed. Recovery requires lineage keys to equal `depends_on`, the handoff artifacts to belong to that attempt, and the declared produced artifact to be present. Legacy implicit serial steps declare no `depends_on` edges and must not claim lineage.
- Retry artifact filenames include an attempt suffix after the first attempt, so old files are preserved and new writes remain collision-free.
- Handoffs and artifacts from non-completed attempts are excluded from subsequent context. When a step has multiple historical completed attempts, only handoffs from its latest completed attempt enter downstream context.
- Eval trace records distinguish `step_structural`, `step_executor`, and `pack` scopes. Executor output cannot reuse the reserved `<step>:artifacts_created` structural check name. A named required executor eval is valid only when exactly one scoped result exists and it is `PASS`; missing, duplicate, `WARN`, or `FAIL` evidence invalidates the checkpoint.
- The Institutional Code R&D execution chain is `dispatch_work -> prepare_patch -> test_changes`. `test_changes` consumes exactly one canonical `PATCH` handoff from the current completed `prepare_patch` attempt, verifies the artifact bytes against durable metadata, applies the exact fenced diff bytes (including significant trailing whitespace and EOF newline semantics) to a disjoint working copy, checks copied base hashes, and only then runs the allowlisted pytest command. Immediately before pytest, source patch targets must still equal the patch-declared base hashes; the same targets and patch bytes are rechecked after pytest and on later excerpt reads. A separate JUnit report must prove at least one non-skipped case and zero failures/errors, so all-skipped runs and simple exit-code hook overrides fail before model review even when the process returns zero.
- A disjoint working copy is not an OS sandbox. Host pytest requires `allow_host_test_execution=true`, receives an environment allowlist/private HOME and TEMP, excludes source/workspace absolute paths from task/context model payloads, pytest output, model review text, artifacts, summaries, and persisted executor errors, overrides pytest `addopts`, and verifies source patch-target hashes before and after execution. Repository context, pytest output, model review text, and runner errors redact secret-like assignments, credentialed URLs, common provider tokens, JWTs, and private-key blocks. Embedded credentials remain readable inside the copied repository, and raw host filesystem/network authority still requires a VM/container boundary for untrusted code.
- Institutional execution without `repository_path` and `test_command` cannot synthesize a successful mock test checkpoint; it fails the named patched-test gate.
- Graceful application shutdown stops new submissions, waits at most 30 seconds for only the active worker segment, and does not drain the queued backlog. A timeout leaves SQLite open for the daemon segment; forced process termination is reconciled on the next startup.
- Active run locks receive a heartbeat every five seconds. Transient heartbeat and terminal lock-release storage errors are retried; a terminal wake-up also releases any orphaned acquired lock before returning.
- Background trace failures are isolated from queue execution; an observability write cannot terminate the worker loop.
- Transient queue-item reads and status writes are retried. A persistent terminal queue-write failure is repaired from authoritative run state on the next startup rather than being converted into a permanent `completed` run / `failed` queue contradiction.
- A storage error after run activation may temporarily leave `run=running` with a running/failed queue segment. The live worker detects that contradiction, invokes the same checkpoint invalidation/runtime terminalization path used for interrupted processes, and atomically requeues the run plus current segment before retry. Missing task/pack configuration atomically terminalizes open AgentRun/session/job state with run and queue failure.
- All unhandled worker terminalization, including open runtime state, queue item, and run status, is written in one SQLite transaction. A failure rolls the full transition back and the live worker loop retries, so readers cannot observe `run=FAILED` with `queue=QUEUED`.
- Run-scoped tables have idempotent `run_id` indexes; queue/lock status and runtime-session lookups also have targeted indexes. Startup creates missing indexes without rewriting run payloads or deleting records.
- Explicit source writeback accepts only a PATCH owned by the latest completed `prepare_patch` attempt for the run and revalidates ownership plus artifact hash after its test command, before creating the source-write transaction. It treats its final success audit as part of the transaction boundary: failure to persist `writeback_applied` restores every just-written file to its verified base content, and reports any file whose rollback also fails.
- Pending version 2 writeback journals are recovered before the run worker starts. Recovery first validates SQLite, artifact, repository, backup, temporary-path, and full target-hash identity; only an exact `writeback_applied` trace with every target at its new hash is retained. A base/new mixed set is rolled back as one transaction, while unknown target bytes fail startup closed and preserve both the external edit and journal for diagnosis.

## Observability

Trace events use stable actions:

- `background_run_queued`
- `background_run_started`
- `background_run_completed`
- `background_run_failed`
- `interrupted_run_requeued`

Queue items retain bounded status/action fields. Trace and queue payloads never include credentials, full prompts, or artifact bodies.

## Context Policy

`WorkflowStep` gains a typed `context_policy` with:

- `artifact_excerpt_chars`: total character budget for artifact excerpts.
- `max_artifacts`: maximum number of completed artifacts included.
- `max_upstream_handoffs`: maximum structured handoffs retained; it must cover every declared dependency.

The runner reads only completed-attempt artifacts through `ArtifactStore`. Each excerpt read streams the full artifact hash while retaining only the configured character prefix, so post-test tampering fails closed without unbounded memory use. The context injector records retained/dropped counts and excerpt characters in the context manifest.

The schema enforces global ceilings of 100,000 excerpt characters, 32 artifacts, and 32 upstream handoffs per step, and rejects `max_upstream_handoffs < len(depends_on)`.
The final structured context is additionally capped at 100,000 characters and 300,000 encoded bytes before a model call. Task intake has separate total-character, encoded-byte, container-length, and nesting-depth limits before persistence.

Recommended budgets:

| Position group | Artifact excerpt budget |
| --- | ---: |
| Intake and control | 2K-4K chars |
| Planning and context reading | 8K-12K chars |
| Implementation and testing | 12K-16K chars |
| Review and gates | 16K-24K chars |
| Synthesis and final delivery | 12K-20K chars |
| Search and verification | 4K-8K chars |

The active Research Planner output budget increases from 700 to 1000 tokens. Every active `deepseek-v4-pro` route has at least 4096 output tokens because a real Research reader still reached `finish_reason=length` at 2048 tokens; such incomplete responses fail closed instead of becoming checkpoints. Other GPT output budgets remain unchanged.

Real-provider failure is fail-closed by default. Institutional session-step fallback to mock is available only with the explicit operator opt-in `TEAM_AGENT_ALLOW_MODEL_FALLBACK_TO_MOCK=1`, so a provider outage cannot silently produce a mock checkpoint.

## Success Criteria

- A background submission returns before a deliberately blocked executor completes.
- The worker completes a queued run and records queue/trace state.
- On startup, an orphaned running run resumes after its last completed step without overwriting prior artifacts.
- A recovered `test_changes` checkpoint is reusable only when its stored dependency lineage still identifies the current completed `prepare_patch` attempt and canonical patch handoff; a newer patch invalidates the older test checkpoint.
- `test_changes` passes only after the referenced patch is applied in the disjoint working copy, explicit host execution is enabled, JUnit evidence proves at least one non-skipped test with no failures/errors, pytest returns zero, the patch hash still matches, and source patch-target hashes are unchanged. The Harness does not write source files before the separate writeback flow; untrusted-code containment remains an external sandbox responsibility.
- A queued or recovered run submitted without `confirm_real_web` cannot call real web/browser handlers even if those providers become available before the tool step executes.
- UI auto-refresh observes queued, running, waiting, failed, and completed states.
- Artifact excerpts never exceed the step policy and never include artifacts from incomplete attempts.
- Existing synchronous callers continue to pass unchanged.
- Targeted worker/context tests and the full pytest suite pass.

## Non-Goals

- Distributed workers or multi-process coordination.
- Exactly-once external model execution across a hard crash.
- Automatic retry of provider quota, authentication, validation, or timeout failures.
- Data-format migrations, artifact deletion, deployment, or public hosting. Additive idempotent indexes are in scope for local query stability.

## Verification Record

- Patched-test, dependency-provenance, credential-redaction, and storage-recovery follow-up on 2026-08-03: `643 passed, 5 skipped, 1 warning`. The warning is the existing Starlette `TestClient` compatibility deprecation for the installed `httpx`; it does not affect the live Uvicorn path. Regression coverage proves exact handoff artifact ownership in live/recovery/writeback paths, exact lineage keys, full checkpoint file hash validation, one canonical PATCH, no dependency-handoff budget truncation, post-test artifact hash enforcement, pytest option and JUnit execution-evidence checks, EOF newline fidelity, explicit host-execution opt-in, activated-run storage retry, missing-pack runtime terminalization, and the prior 20/20 real-worker-loop atomic terminalization retry result.
- A fresh temporary-database Uvicorn smoke queued and completed all six mock steps, persisted six artifacts and a final artifact, completed its queue item, released its lock, recorded no error trace, and released the service port after shutdown.
- A fresh two-process forced-restart smoke killed the first Uvicorn process with the run, first agent attempt, queue item, and lock all persisted as active. Startup recovery cancelled only the interrupted attempt, completed its retry plus the remaining steps, preserved `cancelled/completed` queue history, released both locks, produced six completed checkpoints and six artifacts, recorded `interrupted_run_requeued` with no error trace, and released both service ports. A second interruption in the narrow terminal window was also repaired on the next startup from `completed + running queue + acquired lock` to a completed queue and released locks.
- Full backend suite: `290 passed, 3 skipped` on 2026-07-10.
- Python compile check: `python -m compileall -q app scripts` passed.
- Playwright verified `background=true`, UI polling from `running` to `completed`, desktop and 390px layouts, and zero browser console errors.
- The browser smoke used request interception; it did not call real models or persist smoke runs/tasks.
- Reliability follow-up verification on 2026-07-27: `330 passed, 4 skipped`, `python -m compileall -q app scripts`, `pip check`, and JavaScript/PowerShell syntax checks passed.
- Security and long-run verification on 2026-07-28: `563 passed, 5 skipped`, `python -m compileall -q app scripts`, `pip check`, JavaScript syntax, and all project PowerShell parse checks passed.
- A temporary-database 100-run deterministic background stress completed 100/100 runs, 600/600 agent checkpoints, and 600 artifacts in 65.677 seconds. All queue items completed, all locks released, one production-interval heartbeat advanced, no worker/heartbeat/queue state leaked, and SQLite reported `integrity_check=ok` with zero foreign-key violations.
- A real forced-process restart preserved the completed first checkpoint, cancelled and retried only the interrupted second step, produced an `attempt-2` artifact, completed the recovered run in 2.141 seconds, and left cancelled/completed queue history with both locks released. Exactly-once execution remains intentionally out of scope for the interrupted external step.
- Paid route smoke verified both `gpt5.5` and `deepseek-v4-pro` after the latest restart. Real six-step Research run `edfbd0e2-443e-43c4-b9a2-75cd52a39430` completed through the background worker in 172.3 seconds; all six model responses were real with `finish_reason=stop`, the queue completed, the lock released, no error trace was recorded, and final artifact `d7bce709-abd5-4ad7-8e78-9d5a7a9999d4` contained 2573 characters.
- Playwright verified the live UI at 1440x1000 and 390x844, opened the completed run and its final artifact, observed no horizontal overflow, received HTTP 200 for every application request, and reported zero console warnings or errors.
