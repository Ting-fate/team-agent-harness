# Durable Run Worker and Context Budgets

## Status

Implemented on 2026-07-10, reliability-hardened on 2026-07-27, security/long-run verified on 2026-07-28, patched-test/recovery provenance hardened on 2026-08-03, and extended with immutable execution plans, bounded Agent Loops, quality gates, and controlled parallelism on 2026-08-04. This design extends the original single-process MVP without introducing a distributed queue or a data-format migration. The follow-ups add idempotent SQLite indexes for bounded history and run-scoped lookups, additive checkpoint lineage inside existing `AgentRun.input_context` JSON, and plan snapshots inside the existing Run JSON payload.

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
- `POST /execution-plans/validate` validates an `execution-plan-v1` object against the selected Pack and returns the redacted plan plus its `public_plan_hash`. It reports `run_execution_plan_hash=null` because the actual immutable hash is created only after task-time Skill snapshots are frozen by `POST /runs`.
- `POST /execution-plans/generate` uses a selected Planner-capable Pack role. Mock routing returns a conservative validated plan; real routing requires `confirm_real_models=true`, accepts plain JSON only, and cannot introduce roles, tools, side effects, `session`, or `acp` authority absent from the Pack.
- `POST /runs` accepts an optional validated execution plan. Every new API run persists either that plan or a frozen Pack-derived plan plus its SHA-256 hash.
- Task-time Skills are applied before freezing. Each persisted plan snapshots the participating Agent ids, roles, effective prompts, model settings, tools, runtime limits, and Skill ids. Run-facing API responses remove these trusted snapshots and mark the plan as redacted.
- `GET /runs/{run_id}/quality` derives required artifacts and eval checks from the frozen plan and verifies latest-completed-attempt ownership, final-artifact Run ownership and type, final content, and artifact hashes.
- `GET /providers/doctor`, `POST /routes/explain`, and `GET /models/{provider}/{model:path}/capabilities` expose configuration/readiness, deterministic route selection, and capability metadata without calling a provider. `POST /providers/{provider}/smoke` is the explicit exception: real-provider smoke requires server-side enablement, configured credentials, and `confirm_real_models=true`, and may incur provider cost.
- Model capability metadata comes from the built-in registry or an explicitly selected `TEAM_AGENT_MODEL_CAPABILITIES_CONFIG` JSON file. Route-level price pairs override registry prices; a cost-bounded Agent Loop fails closed when any candidate lacks complete price metadata. Registry prices are interpreted as USD per million tokens.
- `GET /tasks` and `GET /runs` return at most 500 newest records by default and accept validated `limit`/`offset` parameters with a hard limit of 1000.
- `GET /health` reports the worker state and returns HTTP 503 if the worker thread is stopped.
- `POST /runs/{run_id}/runtime-jobs/{job_id}/approve?background=true` persists the approval and durable queue item, then returns HTTP 202 before resumed model execution completes. The browser UI uses this mode; omitting the query preserves the synchronous compatibility path.
- A repeated background approval reuses the active queue segment. A completed old job returns its persisted state and cannot advance the current approval. Startup also requeues a persisted `WAITING` + `APPROVED` combination.

## Multimodal Input And Model Consent

The additive multimodal task contract is opt-in. `inputs.allow_external_model_inputs=true` is required together with bounded `content_blocks`. Text blocks are validated for NUL/secret-like content. `image_ref` and `file_ref` blocks require an `inputs/...` relative path, allowlisted MIME, SHA-256, and exact size. The path boundary rejects observed traversal, links/junctions/reparse points, device/alternate-stream syntax, sensitive names, and hard-linked files; bytes are read through one file descriptor with size/identity checks. Windows component checks remain defense in depth rather than a race-free sandbox against a concurrently hostile filesystem.

At run creation, each validated reference is copied to the artifact root as a hash-addressed staged input. `Run.content_block_snapshot_files` persists the hash-to-file map, and recovery reads and verifies those staged bytes instead of reopening the mutable source path. A frozen image/file reference always requires its staged blob even when the persisted map is empty or partial; missing staged state fails closed. `content_block_snapshot`, its hash, `allow_external_model_inputs_snapshot`, and `vision_preprocess_snapshot` are checked before any reference bytes are read and reject task/config drift after submission. Staged inputs are retained with the run and are not deleted during checkpoint invalidation.

`file_ref` has one semantic path: it is decoded as bounded UTF-8 untrusted text before model serialization. Chat and Responses serializers reject a bare `file_ref`; they accept only validated image data URIs with exact MIME/base64/magic/size checks. The Gateway derives the `vision` capability from actual image blocks, so a caller cannot route an image to a non-vision model by omitting metadata. Mock providers reject image requests rather than claiming visual understanding.

Vision preprocessing is a bounded, durable sidecar. Direct image delivery is selected only when a configured, ready, healthy, capability-matching candidate also satisfies run confirmation and fallback real-call approval; mock vision claims are forbidden even in an external capability registry. Direct image messages carry the untrusted-external-data safety notice. Otherwise the sidecar remains eligible. Before egress it requires a configured and ready non-mock vision provider, explicit real-call opt-in, durable artifact storage, and a valid AgentRun attempt. It uses a 2,048-token output cap and the step timeout/final-envelope/text-block budgets. Only image blocks are sent. Mixed file blocks remain in the main context and are not falsely attributed as sidecar inputs. The result must have a complete finish reason, contain no secret-like text, and is stored as an idempotent `IMAGE_DESCRIPTION` artifact named by the aggregate image digest, full attempt digest, and full content hash. Identical retries reuse one artifact; a matching orphan from the file-before-SQLite crash window is adopted, while a repeated external call with different valid output creates another durable artifact. Downstream provenance retains the original image/file refs and sidecar artifact id.

Runtime validation remains authoritative because the generic task `inputs` OpenAPI schema does not yet expose `content_blocks` as a discriminated union or publish the narrower 4 MiB `file_ref` limit. Raw and encoded byte limits plus image magic checks do not decode pixel/frame counts or guarantee provider-specific context-window fit. Those checks, race-free hostile-filesystem containment, and provider-side decompression limits remain external boundaries rather than claimed guarantees.

Every model request generated inside a persisted run carries `run_bound=true` and the frozen `real_model_access_confirmed` value. The Gateway rejects primary and fallback real providers when that value is false, even if provider credentials or availability change after submission. Dynamic plan generation applies the same confirmation rule to real fallback candidates.

## Worker Design

- One in-process worker thread per `HarnessAppState`.
- SQLite run and queue records are authoritative; the in-memory queue is only a wake-up mechanism.
- Worker startup scans persisted runs:
  - `QUEUED` runs are submitted for execution; an orphaned `RUNNING` queue item and lock from the pre-run activation window are cancelled/released first.
  - orphaned `RUNNING` runs have non-terminal step state cancelled, stale locks released, and the run requeued.
  - `WAITING` runs with a persisted `APPROVED` runtime job are requeued to finish the approved step.
  - stale non-terminal queue items for waiting or terminal runs are reconciled to the authoritative run state.
- Completed agent runs are checkpoints only after structural/required eval evidence and exactly one valid handoff for every declared outgoing edge are persisted. Recovery rejects duplicate, extra, missing, mis-targeted, cross-run, or cross-attempt handoffs and reruns the invalid step instead of falling back to an older completed attempt. Before reuse, every artifact file owned by the checkpoint is streamed through its durable content hash; missing, invalid UTF-8, or tampered files invalidate the checkpoint without deleting artifacts or records.
- Recovery validates the persisted execution-plan schema and canonical hash before reconstructing Agents and steps from the stored snapshots. A new-style persisted plan without Agent snapshots fails recovery rather than using current Pack agents. Permissions, runtime limits, and dataflow are checked against the frozen plan and snapshots; current Pack changes to dynamic-plan enablement, blocker evals, role counts, or parallelism are not applied retroactively. In-flight behavior therefore does not drift when Pack prompts, routes, tools, limits, Skill bindings, or planning policy change after submission. Pre-plan legacy runs continue through their original Pack for compatibility.
- Explicit DAG steps persist `dependency_lineage` in the consuming `AgentRun.input_context`, mapping every declared dependency to the exact completed dependency attempt and handoff consumed. Recovery requires lineage keys to equal `depends_on`, the handoff artifacts to belong to that attempt, and the declared produced artifact to be present. Legacy implicit serial steps declare no `depends_on` edges and must not claim lineage.
- Retry artifact filenames include an attempt suffix after the first attempt, so old files are preserved and new writes remain collision-free.
- Handoffs and artifacts from non-completed attempts are excluded from subsequent context. When a step has multiple historical completed attempts, only handoffs from its latest completed attempt enter downstream context.
- Eval trace records distinguish `step_structural`, `step_executor`, and `pack` scopes. Executor output cannot reuse the reserved `<step>:artifacts_created` structural check name. A named required executor eval is valid only when exactly one scoped result exists and it is `PASS`; missing, duplicate, `WARN`, or `FAIL` evidence invalidates the checkpoint.
- The Institutional Code R&D execution chain is `dispatch_work -> prepare_patch -> test_changes`. `test_changes` consumes exactly one canonical `PATCH` handoff from the current completed `prepare_patch` attempt, verifies the artifact bytes against durable metadata, applies the exact fenced diff bytes (including significant trailing whitespace and EOF newline semantics) to a disjoint working copy, checks copied base hashes, and only then runs the allowlisted pytest command. Immediately before pytest, source patch targets must still equal the patch-declared base hashes; the same targets and patch bytes are rechecked after pytest and on later excerpt reads. A separate JUnit report must prove at least one non-skipped case and zero failures/errors, so all-skipped runs and simple exit-code hook overrides fail before model review even when the process returns zero.
- A disjoint working copy is not an OS sandbox. Host pytest requires `allow_host_test_execution=true`, receives an environment allowlist/private HOME and TEMP, excludes source/workspace absolute paths from task/context model payloads, pytest output, model review text, artifacts, summaries, and persisted executor errors, overrides pytest `addopts`, and verifies source patch-target hashes before and after execution. Repository context, pytest output, model review text, and runner errors redact secret-like assignments, credentialed URLs, common provider tokens, JWTs, and private-key blocks. Embedded credentials remain readable inside the copied repository, and raw host filesystem/network authority still requires a VM/container boundary for untrusted code.
- Institutional execution without `repository_path` and `test_command` cannot synthesize a successful mock test checkpoint; it fails the named patched-test gate.
- Graceful application shutdown stops new submissions, waits at most 30 seconds for only the active worker segment, and does not drain the queued backlog. A timeout leaves SQLite open for the daemon segment; forced process termination is reconciled on the next startup.
- Active run locks receive a heartbeat every five seconds. Transient heartbeat and terminal lock-release storage errors are retried; a terminal wake-up also releases any orphaned acquired lock before returning.
- Background trace failures are isolated from the worker loop. The safety-critical exception is a run-bound physical provider attempt: the exact built-in OpenAI-compatible adapter must persist `model_provider_attempt_started` immediately before each HTTP dispatch, and a persistence failure aborts that dispatch. This applies to normal Pack steps, Agent Loop iterations, vision sidecars, and Institutional patch/test model calls. Custom adapters cannot attest trusted provider attempts. A hard crash after dispatch may still repeat the request during recovery, so exactly-once external execution remains out of scope.
- Explicit DAG ready steps execute concurrently only when the executor opts in and all ownership claims are present and non-conflicting. Submission is bounded by the frozen `max_parallel_steps`. A branch failure stops new submissions and cancels futures that have not started; already-running calls settle because Python threads cannot be killed safely. Results are committed or failed in immutable plan order.
- Model calls pass through a per-provider bounded semaphore (`TEAM_AGENT_PROVIDER_MAX_CONCURRENCY`, default `4`) in addition to step-level time/token/conservative-estimated-cost limits. Semaphore wait, provider retries, and retry backoff share one monotonic deadline; each attempt receives only the remaining request time. Capacity waits fail with a sanitized provider error when the deadline is exhausted.
- Provider health is intentionally process-local: three consecutive retryable failures open a 30-second circuit, and a service restart clears that transient history. Every fallback candidate must independently satisfy capability, readiness, health, mock-fallback, and durable real-call approval checks before dispatch.
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

## Agent Loop And Tool Boundary

`WorkflowStep.agent_loop` is opt-in; existing steps preserve the one-model-call behavior. Enabled steps follow a bounded model/tool loop and declare `max_steps`, `max_tool_calls`, `max_total_tokens`, `timeout_seconds`, `max_repeated_tool_calls`, `max_observation_chars`, and optional `max_cost_usd`. Built-in Agents declare explicit ceilings for those Loop fields, and a dynamic plan cannot exceed them. Every request, response, action, observation, and stop reason is traced without raw credentials. Each request caps `max_tokens` to the remaining token budget. A cost-bounded Loop requires every primary/fallback candidate to have input/output prices from the capability registry or an explicit frozen route override. Before dispatch it serializes the current system prompt, messages, tool calls/observations, and tool schemas, uses UTF-8 byte size plus framing allowance as a conservative input-token estimate, and applies the highest candidate rates to reduce `max_tokens` or stop. Responses carry the actual selected provider/model and selected price, so fallback accounting never combines a fallback provider with the primary model or price. Missing/incomplete provider usage consumes the entire remaining token allowance; aggregate-only usage is priced at the higher selected rate. This is estimated-call control, not a provider billing hard cap: provider templates, special input/vision pricing, price drift, and an already-completed single request remain outside the guarantee. Budget exhaustion is irreversible, stops the rest of a returned tool-call batch, and cannot later be relabeled `finished`; it returns the best non-empty result with an exhausted marker, or fails if no usable result exists.

The model sees the same typed JSON schema that `ToolGateway` enforces. A call must be declared by both the Agent and frozen step. Side-effect tools require persisted per-run approval in loop mode. Observations are marked untrusted, redacted, and truncated before the next model request; artifact-write observations omit local filesystem paths. Workspace read/list/search tools require an explicit existing `repository_path` and never inherit the Harness process directory. They use descriptor-level identity, size, and link-count checks; reject sensitive credential/secret/token/password/private-key paths, symlinks, junctions/reparse points, hard-linked files, non-UTF-8 and oversized files; and bound generated dependency/cache traversal plus list/search result counts. This is defense in depth for a trusted local workspace, not a race-free sandbox against a concurrently hostile process.

Every Planner/operator step must declare deterministic acceptance criteria. The runner writes `<step>:acceptance:<criterion>` eval results before checkpoint creation. The run-quality report requires those results plus blocker Pack evals and verified artifacts; artifact-type presence alone is not a quality claim. Planner output cannot provide trusted Agent snapshots; the Harness supplies them only after validation. Planner/operator plans cannot exceed Pack parallelism, Agent Loop ceilings, or the static Pack's per-role step count. The Harness restores blocker Pack evals whose required artifact types are present in the dynamic plan, rejects conflicting same-name definitions, and fails validation if the merged set would exceed the 32-check schema limit. The Institutional Pack rejects dynamic plans because its reserved patch/test identities and writeback provenance require the static Pack chain.

Reproducible benchmark suites compare Single-Agent, Multi-Agent, ablation, and model-combination Run ids across quality, rework, contradictions, indispensable contributions, tokens, estimated price, and elapsed time. Every case must explicitly declare `final_artifact_type`; missing final-output contracts fail schema validation. Trials require a positive replicate, continuous identical replicate grids for every case/variant, and globally unique Run ids. Comparisons pair each variant with the baseline by `(case_id, replicate)` before aggregation. Both normal responses and vision-sidecar responses are metered under the actual selected provider/model. Run quality exposes usage completeness. A persisted confirmed real request with no terminal response is conservatively treated as potentially dispatched after a hard crash; a failed real-provider route attempt before fallback success is also unmetered, while confirmed local preflight rejection and mock failure are not. Any missing input/output usage or potentially billed unknown attempt makes cost and aggregate token comparison unknown, and the value gate cannot pass by treating unknown consumption as zero.

## Success Criteria

- A background submission returns before a deliberately blocked executor completes.
- A frozen dynamic plan cannot expand Pack roles/tool permissions, repeat a role beyond its static Pack count, or remove an applicable blocker Pack eval. Changes cannot alter the plan after submission, and recovery rejects a plan hash mismatch or missing Agent snapshots.
- An Agent Loop cannot exceed its step/tool/token/time/repetition budgets, cannot dispatch when the conservative estimated-cost calculation has no remaining room, and cannot execute an unapproved side effect. Provider billing remains outside the estimated-cost guarantee described above.
- Parallel failure prevents queued sibling branches from starting and leaves every prepared sibling in a terminal state.
- Run quality fails when a required acceptance eval, blocker eval, artifact, final content, artifact hash, or final-artifact Run/type/latest-attempt lineage is missing or invalid.
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
- Unbounded autonomous replanning, self-created roles, or permission expansion.
- Killing an already-running provider call in another Python thread; timeout enforcement still depends on the provider transport returning control.
- Automatic retry of provider quota, authentication, validation, or timeout failures.
- Data-format migrations, artifact deletion, deployment, or public hosting. Additive idempotent indexes are in scope for local query stability.

## Verification Record

- Final release verification on 2026-08-07: the current worktree passed `823 passed, 5 skipped, 1 warning`. Fourteen focused adversarial regressions proved pre-dispatch durable provider-attempt evidence, fail-closed trace persistence, partial-usage accounting, local serialization rejection, sensitive-identifier sanitization, the exact built-in adapter trust boundary, and recorder coverage for normal Pack, Agent Loop, vision-sidecar, and Institutional local-code paths. `python -m compileall -q app scripts`, `pip check`, all four tracked PowerShell parse checks, CI/config YAML parsing, and `git diff --check` passed. The warning remains the existing Starlette `TestClient` deprecation. A fresh temporary-data Uvicorn smoke completed all six mock steps with `quality_passed=true`, six AgentRun checkpoints, five handoffs, six artifacts, 18 eval results, and no error trace; the service stopped cleanly and the temporary data was removed. This record describes local worktree evidence, not remote CI state.
- Agent-Loop/multimodal/quality follow-up on 2026-08-06: the current worktree passed `751 passed, 5 skipped, 1 warning`; the final focused cross-module regression set passed `203 passed, 1 skipped`, and the multimodal-only regression set passed `22 passed`. `python -m compileall -q app scripts`, `pip check`, `git diff --check`, and a high-confidence source/config secret scan passed. The warning remains the existing Starlette `TestClient` deprecation. A fresh temporary-data Uvicorn smoke on port `8015` completed all six mock steps with `quality_passed=true`, six metered model calls, complete usage, six AgentRun checkpoints, five handoffs, six artifacts, 17 eval results, and no error trace; the service was stopped and its temporary data removed. This record describes local worktree evidence, not remote CI state.
- Frozen-plan/Agent-Loop follow-up on 2026-08-04: `705 passed, 5 skipped, 1 warning`. `python -m compileall -q app scripts`, `pip check`, all project PowerShell parse checks, CI YAML parsing, `git diff --check`, and a source/config secret scan passed. The warning remains the existing Starlette `TestClient` compatibility deprecation. The CI workflow is configured for Windows Python 3.12/3.13/3.14 with immutable Action SHAs; this local record does not claim the remote matrix ran. `requirements.lock` pins versions but does not contain distribution hashes, so package-index trust remains outside the lock.
- A temporary-data forced-restart Uvicorn smoke submitted 20 mock runs and killed the first process while all 20 were non-terminal. After restart, all 20 runs completed, all 20 quality reports passed, no error trace remained, and every queue item and lock was terminal.
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
