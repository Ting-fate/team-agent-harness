# Team Agent Harness Backend

Local single-process backend for the team-oriented multi-agent harness.

## Current Scope

This directory includes the original MVP phases, the durable local worker and bounded-context increment completed on 2026-07-10, and the frozen-plan/agent-loop increment completed on 2026-08-04:

- FastAPI application skeleton with worker-aware `GET /health`; it returns HTTP 503 when the background worker thread is not running.
- Core Pydantic domain models and enums, including local runtime session, runtime job, run queue item, and run lock records.
- Rule-based task intake analysis that can classify task type, complexity, risk, domain, recommended workflow pack, confidence, reasons, and suggested constraints without creating tasks or starting runs.
- SQLite storage layer for core models plus local runtime session/job observability records and local run queue/lock records.
- Workflow pack schema declarations, including lightweight DAG metadata (`depends_on`, `phase`, and `produces_artifact_type`), explicit Main/Subagent coordination metadata (`coordination_role`, `controller_step`, and `return_contract`), step gate metadata (`requires_eval_pass`, `requires_artifact`, and `ownership`), long-running runtime metadata (`runtime` and `session_policy`), and bounded per-position context policy (`context_policy`).
- In-memory agent registry for pack/role lookup.
- Trace logging and local file-backed artifact storage.
- Tool gateway with permission checks, mocked tools, optional Tavily-backed web search/fetch tools, and optional local browser bridge search/fetch tools for Research.
- Model runtime contract with a deterministic mocked adapter and `model_request` / `model_response` trace observability.
- Typed model tool definitions and tool-call observations for both Chat Completions and Responses-compatible providers. Unknown tools, malformed arguments, duplicate call ids, undeclared tool calls, and oversized arguments fail closed.
- Real-model responses fail closed: only `stop`/`completed` responses with non-empty string text are accepted; truncated, filtered, tool-call-only, failed, incomplete, missing, or non-text responses raise a sanitized runtime error without persisting the raw provider response.
- Backward-compatible mocked model profiles inside each workflow Pack plus an optional run-scoped `team-selection-v1` contract. The operator may replace each fixed position's role card and choose only GPT or DeepSeek routes through `openai`, `deepseek`, or `litellm_proxy`; the selection cannot add positions, change the DAG, widen tools, raise runtime limits, or alter Skill bindings.
- Provider catalog and explicit opt-in OpenAI-compatible adapters for OpenAI, DeepSeek, and an optional LiteLLM Proxy gateway; mock remains the default, while Anthropic and local providers are still skeleton-only.
- Server-side model routing config that can assign selected agents to `mock`, `openai`, `deepseek`, or `litellm_proxy` without storing API keys in the app.
- Single-process workflow runner with deterministic mocked agent execution, dependency-aware ready-set step scheduling, dependency-edge handoffs for DAG packs, explicit dependency lineage on completed DAG attempts, bounded context envelope construction, workflow-level intake/context/skill-route trace events, scoped structural/executor/pack evaluation, named step eval and artifact gate enforcement, ready-batch/ownership conflict checks, conservative opt-in parallel executor dispatch for safe DAG batches, failure trace recording, run/agent-run status updates, local runtime session/job metadata recording for `session` and `acp` steps, and local approval gates.
- Versioned immutable execution plans that freeze the effective Agent prompt, model route, tools, runtime limits, and Skill ids for an existing Pack or accept a validated Planner/operator DAG. Planner/operator plans may reuse existing roles and permission subsets only, may create `runtime=model` steps only, and require deterministic acceptance criteria for every step.
- Opt-in bounded Agent Loops with model/tool iteration, step/tool/token/time/conservative-estimated-cost/repetition budgets, untrusted-observation labeling, side-effect approval, and action/observation trace. Existing Pack steps retain one-call behavior unless `agent_loop.enabled=true`.
- Durable local `RunWorker` plus run coordinator. UI and operator CLI submissions return after persistence, execute outside the initiating HTTP request, maintain a per-run lock heartbeat, and recover interrupted runs from completed step checkpoints on service restart.
- Mocked Code R&D workflow pack with Clarifier, Architect, Coder, Tester, Reviewer, and Finalizer steps.
- Mocked Institutional Code R&D workflow pack with GPT as the trusted main thread, DeepSeek V4 Pro long-context reading/review roles, GPT implementation/test executor branches that require local approval before their mocked/model step execution proceeds, DeepSeek final risk review, and GPT final approval.
- Research workflow pack with Planner, Searcher, Reader, Verifier, Writer, and Reviewer steps. It defaults to mock web search/fetch and can use Tavily or local browser bridge search/fetch only after explicit server-side opt-in.
- FastAPI endpoints for creating/listing tasks, validating run-scoped teams, reading a Pack team template, starting workflows synchronously or in the local background worker, reading the immutable selected-team receipt, run metadata, trace events, artifacts, workflow pack catalogs, single workflow pack details, model provider skeletons, tool provider status, local Skill Library metadata, skill bindings, and pack agent catalogs; default routes remain mock unless server-side routing or an explicitly confirmed run-scoped team enables real providers. `GET /tasks` and `GET /runs` are bounded to 500 newest records by default and accept validated `limit`/`offset` query parameters up to 1000 records.
- Conservative Skill Auto-Router that can automatically select relevant read-only local skills for agents from explicit workflow, role, tool-permission, document-file task signals, and high-confidence domain task signals such as security, performance, database, testing, architecture, UI/web, and AI/model work while preserving manual bindings as an override/extension path.
- FastAPI endpoints for reading full run observability data: aggregated run detail, agent runs, handoffs, eval results, trace events, artifacts, runtime sessions, runtime jobs, and safe queue/lock summaries.
- FastAPI execution-plan validation/generation and run-quality endpoints. Quality reports verify frozen-plan artifacts, every declared step acceptance result, blocker evals, final content, and artifact hashes instead of treating artifact presence alone as success.
- Reproducible paired-replicate benchmark evaluation for Single-Agent, current Multi-Agent, role ablations, and model combinations, including quality, token usage, latency, estimated cost, manual rework, contradictions, and indispensable contributions.
- FastAPI endpoints for local runtime job approval actions: approve, reject, and cancel. These mutate local job/session/run state only; they do not launch external ACP processes.
- Safe Codex/operator CLI plus a restricted stdio MCP adapter for creating tasks, validating GPT/DeepSeek teams, starting background runs, polling run state, and reading redacted run/team/quality data. Neither surface exposes approval, writeback, arbitrary shell, Git, configuration, dependency installation, or credential tools.
- Same-origin Chinese thin UI for creating tasks, filling Code R&D / Institutional Code R&D / Research examples, selecting each fixed position's role card, GPT/DeepSeek family, provider, model, and reasoning effort for the next run, confirming the actual selected real-provider/fallback routes and real web-search routes before execution, inspecting selected pack details including step phase, dependencies, Main/Subagent coordination role, runtime, session policy, return contract, provider/tool status, local Skill Library bindings, and reading execution chains, local session/job/approval/queue/lock status, eval results, trace events, failure summaries, artifacts, and artifact content through the aggregated run detail contract.
- Unit tests for health check, OpenAPI availability, model validation, model runtime contract, model routing, JSON serialization, storage round trips, pack schema validation, registry behavior, trace logging, artifact writes, tool gateway behavior, runner success/failure paths, Code R&D pack happy/blocker/gating paths, Institutional Code R&D API path, Research pack happy/blocker/gating paths, API happy/error/isolation paths, and static UI serving.

The compatibility default remains mocked: current workflow Packs use `provider="mock"` when a caller omits `team_selection`. The browser team editor intentionally constructs real GPT/DeepSeek routes and therefore requires the chosen provider to be configured, the server gate `TEAM_AGENT_ALLOW_REAL_MODEL_CALLS=1`, and explicit confirmation for that run. OpenAI, DeepSeek, and LiteLLM Proxy adapters cannot make real calls merely because a model name was selected. Tavily web search/fetch can make real network calls only when `TEAM_AGENT_ALLOW_REAL_WEB_SEARCH=1`, `TEAM_AGENT_WEB_SEARCH_PROVIDER=tavily`, and `TAVILY_API_KEY` are set. Browser search/fetch can make real browser calls only when `TEAM_AGENT_ALLOW_BROWSER_ACCESS=1`, `TEAM_AGENT_BROWSER_PROVIDER=edge|chrome|browser_cdp`, and a compatible local CDP proxy is available. API keys are read from server-side environment variables only; they are not entered in the browser task UI, obvious secret-like task/team content is rejected before SQLite storage, and keys are not written to trace/artifacts. The separate local desktop launcher can save keys to `.env.local`, which is ignored by Git. Anthropic and local model providers are outside the run-scoped team contract.

The runner can dispatch safe explicit-DAG batches in parallel only when the executor explicitly opts in and every ready step has non-conflicting ownership. Parallel work is submitted up to the frozen plan's `max_parallel_steps`; after a branch failure, queued branches are not started, running calls are allowed to settle, and commit/error handling stays in deterministic plan order. `ModelGateway` separately limits concurrent calls per provider through `TEAM_AGENT_PROVIDER_MAX_CONCURRENCY` (default `4`). The in-process `RunWorker` is a durable local execution mechanism, not a distributed queue or external engineering executor. Built-in runtime jobs still do not launch external ACP processes or maintain live background child sessions. Code R&D does not receive general web search by default; only Research uses the web tools.

## Durable Background Runs And Context Budgets

`POST /runs` accepts an additive `background` field:

- `background=false` preserves synchronous completion for existing API callers.
- `background=true` persists the run and queue item, wakes the local worker, and returns HTTP 201 with the queued run before model execution completes.
- The Chinese UI and `scripts/harness_control.py start-run` send `background=true` and use existing GET endpoints for polling.
- `confirm_real_web` is persisted on the run as `real_web_access_confirmed`. The tool gateway checks that persisted value immediately before any configured real web or browser handler; a provider becoming available after submission cannot grant an unconfirmed background run real network access. Existing run records without the field default to unconfirmed.
- `POST /runs/{run_id}/runtime-jobs/{job_id}/approve?background=true` persists the approval, requeues the waiting run through the same durable worker, and returns HTTP 202 before the resumed model chain completes. The UI uses this mode; the query-less endpoint remains synchronous for compatibility.

The worker is intentionally local and single-process:

- SQLite run/queue state is authoritative; the in-memory queue is only a wake-up signal.
- Graceful shutdown stops accepting work, waits up to 30 seconds for only the active worker segment, and leaves the queued backlog persisted for the next start. If the bound is reached, SQLite stays open until process exit so the still-finishing daemon segment is not handed a closed connection.
- Startup schedules persisted `queued` runs, releases orphaned locks for every run state, repairs the `queued` run / `running` queue-item crash window, and requeues orphaned `running` runs.
- Persisted Run payload reads verify that the embedded id matches the SQLite row id. Startup validates every persisted Run and converts an otherwise-valid one-sided `execution_plan` / `execution_plan_hash` pair into the existing loadable failed sentinel; all other corrupt records are quarantined without blocking healthy Runs by atomically failing active queue state, releasing locks, cancelling open runtime state, and marking only the SQLite Run index failed. The original corrupt payload remains unchanged as evidence and typed Run endpoints return HTTP 409 for it.
- A step becomes a completed `AgentRun` checkpoint only after its eval gate and every declared outgoing edge has exactly one valid handoff. For explicit DAG dependencies, the consuming `AgentRun.input_context` records the exact dependency attempt and handoff ids. Recovery rejects missing, duplicate, extra, mis-targeted, or cross-attempt handoffs; lineage keys must exactly equal `depends_on`, while dependency-free steps must not claim lineage. Every artifact file owned by the checkpoint is streamed through its durable content hash before reuse. An invalid latest checkpoint and its older completed fallbacks are cancelled so the step is rerun without deleting artifacts or records.
- Only validated handoffs from the latest completed attempt of each declared dependency enter downstream context. Every referenced artifact must belong to that attempt and the handoff must include its declared produced artifact. Structural artifact checks, executor-supplied checks, and pack evals use separate trace scopes; a required named executor check must have exactly one `PASS` result.
- A persisted `waiting` run with an `approved` runtime job is requeued automatically on startup. Retrying a background approval reuses its active queue segment, and retrying an already-completed approval returns the persisted result without advancing a newer job.
- Retry artifact filenames gain an `attempt-N` suffix, so existing artifact files and records are never overwritten or deleted.
- A transient storage failure after a run becomes `running` is recovered through the normal interrupted-run checkpoint path; the run and active queue segment return to `queued` together before retry. Missing task/pack configuration atomically terminalizes open runtime state, run state, and queue state.
- A hard crash during an external model request can repeat that interrupted request after restart. Exactly-once external execution is not guaranteed.

Run locks store a heartbeat every five seconds. Stale-lock recovery prefers the latest valid timezone-aware heartbeat over the original acquisition time, so long-running work is not falsely reclaimed. Transient queue reads, queue-status writes, and terminal lock-release writes are retried; a retried terminal wake-up also releases any orphaned acquired lock before it returns. Unresolved wake-ups stay persisted and terminal run state repairs a stale non-terminal queue item on restart. Background trace failures remain isolated from worker execution.

Each `WorkflowStep` has a typed `context_policy`:

- `artifact_excerpt_chars`: total artifact excerpt character budget.
- `max_artifacts`: maximum completed-attempt artifact refs/texts retained.
- `max_upstream_handoffs`: maximum structured upstream handoffs retained; schema validation requires it to be at least the number of declared dependencies.

Artifact excerpt budgets are configured by position from 2K to 24K characters. The global schema caps a step at 100K excerpt characters, 300K encoded bytes, 32 artifacts, and 32 handoffs. The final structured context is checked again before any model call. Artifact files are streamed through a full hash verification while retaining only the bounded excerpt; tampered artifacts and artifacts or handoffs from incomplete attempts are excluded. Context trace events record retained/dropped counts and character totals, never artifact bodies. Task intake is independently bounded by character, byte, container-size, and nesting-depth limits before SQLite persistence. The active local `research-planner` route uses `max_tokens=1000`; every `deepseek-v4-pro` route uses at least `max_tokens=4096` because a real Research reader still reached `finish_reason=length` at 2048 tokens. Incomplete model responses fail closed instead of becoming checkpoints. Other GPT budgets are unchanged.

## Multimodal Input Contract

Tasks may opt into bounded external content with `inputs.allow_external_model_inputs=true` and a `content_blocks` list. A block is either `{ "type": "text", "text": "..." }` or a validated `{ "type": "image_ref" | "file_ref", "path": "inputs/...", "mime_type": "...", "sha256": "...", "size_bytes": n }` reference. Paths are relative to the configured `inputs/` directory; observed links, junctions, reparse points, alternate data streams, device syntax, hard links, and sensitive names/content are rejected. `file_ref` is intentionally converted to bounded, marked-untrusted UTF-8 text; serializers never emit a bare `input_file` payload. These host-path checks are defense in depth, not a race-free Windows filesystem sandbox: do not let an untrusted process mutate the input tree concurrently.

Run creation verifies every reference and stages the exact bytes under the run's artifact root. The persisted `Run.content_block_snapshot_files` map is used for retries and restart recovery, so a later source-file edit, deletion, or configuration drift cannot silently change bytes sent to a model. If the frozen snapshot contains any image/file reference, every execution requires the corresponding staged blob; an empty or partial staged-file map fails closed and never reopens the mutable source path. Staged blobs are hash-checked on every read and are not ordinary `Artifact` records or a replacement for artifact retention policy.

Image blocks require a vision-capable route. The Gateway derives the `vision` requirement from actual message blocks, not only caller metadata; non-vision, unconfigured, unhealthy, unready, unconfirmed, or unapproved routes are skipped before adapter execution. A capability override cannot make the mock provider vision-capable. Direct image delivery includes an explicit untrusted-external-data instruction, and `test_changes` receives the same validated multimodal context as other model-backed steps. An optional `vision_preprocess` sidecar must pass the same readiness and approval checks before egress, use a 2,048-token output cap, fit the final envelope plus text-block budgets, and have durable artifact storage plus an AgentRun attempt before any call. The artifact filename includes the aggregate image digest, full attempt digest, and full content hash. Identical retries reuse one artifact; a matching file orphaned between filesystem and SQLite persistence is safely adopted, while a repeated external call returning different valid text creates a separate artifact. Mixed image/file input preserves file context; sidecar provenance names only images actually sent, and downstream artifacts retain original image/file refs plus the sidecar artifact id.

The runtime validator, not generated OpenAPI, is currently authoritative for the discriminated `content_blocks` shapes and the separate 4 MiB `file_ref` limit. Image validation bounds encoded/raw bytes and verifies allowlisted magic, but does not decode pixel dimensions or animation frames and does not predict every provider-specific context-window rule; a provider may still reject a valid bounded payload. Strong containment for concurrently hostile local files or decompression/resource attacks requires a separate trusted staging or sandbox boundary.

Every run-bound real-model request carries the persisted `real_model_access_confirmed` value. Primary and fallback real routes are rejected by the Gateway when that value is absent; route receipts record the rejection. This remains true if credentials or provider availability change after the run was created. Real route confirmation is also required when generating a dynamic execution plan with a real fallback.

## Frozen Plans, Agent Loops, And Quality

`POST /runs` always stores an `execution-plan-v1` snapshot and its canonical SHA-256 hash. Before the snapshot is frozen, task-time Skill routing is applied to every participating Agent. The stored Agent snapshots include the effective system prompt, model settings, tool permissions, runtime limits, and Skill ids; restart recovery rebuilds Agents from those snapshots rather than the current Pack definitions. A persisted plan without Agent snapshots fails recovery instead of falling back to current Pack agents. Recovery validates permissions, runtime limits, and dataflow against the frozen plan and Agent snapshots; later changes to the current Pack's dynamic-plan switch, blocker evals, role counts, or parallelism do not reinterpret an in-flight run. Run-facing APIs remove `agent_snapshots` and set `execution_plan_redacted=true`, so injected Skill bodies and role prompts are not exposed through run responses.

With no supplied plan, the server freezes the selected Pack. An operator can first call `POST /execution-plans/validate`, or request a Planner result through `POST /execution-plans/generate`. These endpoints return the redacted plan, `public_plan_hash`, `run_execution_plan_hash=null`, and `immutable_after_run_creation=true`; only `POST /runs` freezes task-specific Skill snapshots and returns the actual `execution_plan_hash`. Real-model generation requires `confirm_real_models=true`; model output must be one plain JSON object without Markdown fences and is still validated against the Pack's roles and tool permissions. Planner schemas omit trusted Agent snapshots; the Harness adds them only after validation. Dynamic plans cannot exceed Pack parallelism, Agent Loop limits, or the static Pack's use count for any role. Blocker Pack evals whose required artifacts are produced by the dynamic plan are restored by the Harness, and a same-name weakened gate is rejected. If restoring those gates would exceed the 32-check plan schema limit, validation fails before persistence. `code_rd_institutional` rejects Planner/operator plans entirely because its patch/test/writeback sequence depends on reserved static step identities. Restart recovery recalculates the persisted hash and rebuilds execution from the immutable snapshot, so later Pack edits cannot silently change an in-flight run.

An Agent Loop is enabled per step. It follows `reason -> tool call -> observation -> revise -> finish` and stops at the first configured step, tool-call, token, wall-clock, estimated-cost, or repeated-call limit. Every model request caps `max_tokens` to the remaining token budget. When `max_cost_usd` is set, every primary/fallback candidate must have an input/output price from the capability registry or an explicit frozen route override. Before each request, the Loop serializes the current system prompt, messages, tool calls/observations, and tool schemas, uses its UTF-8 byte size plus framing allowance as a conservative input-token estimate, and applies the highest candidate input/output rates to reduce `max_tokens` or stop before dispatch. The response records the actual selected provider/model and is charged at that selected route's price. Missing or incomplete provider usage never counts as zero: when total consumption cannot be proven, the response consumes the entire remaining token allowance; when only aggregate tokens are available, cost is estimated at the higher selected input/output rate. This is a conservative estimated-call control, not a provider billing hard cap: provider chat templates, special vision/input pricing, price drift, and an already-completed single request can still make the final bill exceed the estimate. Once any budget is exhausted, that outcome is irreversible: later text cannot relabel the loop as `finished`, a batch stops executing additional tool calls, and exhaustion without a non-empty usable result fails the step instead of inventing placeholder success. If usable text exists, the loop returns that best result with `budget_exhausted=true` and the exact stop reason. Tool permissions are the intersection of the frozen step and agent definition. `local_write`, `local_execute`, and external-write tools require explicit persisted approval in loop mode. Tool output is treated as untrusted data, redacted, and bounded before it returns to the model; artifact-write observations omit local filesystem paths. Agent Loops with `read_file`, `list_files`, or `search_files` require an explicit existing `repository_path` and never fall back to the Harness process directory. File tools use descriptor-level identity/size checks and exclude symlinks, junctions/reparse points, hard-linked files, `.env`, credentials/secrets/token/password names, private-key formats, VCS/dependency/cache directories, non-UTF-8 files, and files over 1 MiB; list/search result counts are bounded. These checks reduce accidental disclosure but do not make the host workspace a race-free sandbox against a concurrently hostile local process.

`GET /runs/{run_id}/quality` derives its checks from the frozen plan. It requires artifacts produced by the latest completed attempt for each step, each step's named acceptance eval, blocker Pack evals, valid content hashes, and a verified non-empty final artifact that belongs to the Run, comes from a latest completed attempt, and matches the plan's final artifact type. `scripts/benchmark_control.py` applies explicit benchmark case criteria to existing runs and compares variants without rerunning paid models:

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_control.py `
  --suite config\benchmarks\code-repair-v1.json `
  --trials path\to\trials.json `
  --output output\benchmark-report.json
```

The benchmark does not claim Multi-Agent value by construction. Every case must explicitly declare its expected `final_artifact_type`; benchmark loading fails if that contract is absent. Every trial declares a positive `replicate`; each `(case_id, variant)` must use the same continuous replicate grid, and a `run_id` cannot be reused elsewhere in the suite. Variant comparisons are paired by `(case_id, replicate)` before quality, cost, duration, and rework deltas are aggregated. Normal model responses and `vision_preprocess` sidecar responses are both counted under the actual selected provider/model. Quality metrics expose `usage_complete` and `unmetered_model_calls`. A persisted confirmed real-model request with no terminal response is conservatively treated as potentially dispatched after a hard crash, and a failed real-provider route attempt before a successful fallback is also unmetered; confirmed local preflight rejection and mock failure do not create external-cost debt. If any call lacks input/output usage or any potentially billed attempt is unmetered, estimated cost and the variant's average token count are `null`, so the value gate fails rather than treating unknown consumption as free. A variant passes its value gate only when measured quality or rework improves within the configured cost and duration ratios.

## Main/Subagent Mental Model

`team_agent_harness` is the orchestration layer. It is not based on Codex, Claude, GPT, or DeepSeek as the framework. A workflow pack describes the Main/Subagent collaboration contract:

- Main/controller steps plan, dispatch, synthesize, or approve work.
- Subagent steps execute bounded work packages and return a structured result: summary, artifacts, open questions, risks, and evals.
- Handoffs connect controller steps to subagent steps and then back into synthesizer/review steps.
- Each agent has an independent `model_config`. In the recommended GPT + DeepSeek setup, GPT is the trusted main thread for planning, code work, testing, synthesis, and final approval; DeepSeek V4 Pro is used for long-context reading, plan challenge, alignment review, and final risk review.
- Each step also declares a `runtime`: `model` for ordinary model calls, `session` for long-running resumable agent sessions, and `acp` for future external engineering executors. `session_policy` records whether the step is persistent, how it should resume, and whether approval is required.

Direct Anthropic/Claude calls are still not implemented. To use Claude roles now, configure a LiteLLM Proxy route such as `provider="litellm_proxy"` with a Claude model name. Direct OpenAI and DeepSeek routes are available through their OpenAI-compatible adapters.

## Real Model Opt-In

Mock remains the safe default. To enable real provider calls for selected agents, set the explicit real-call gate, set provider credentials in the server process environment, then configure only those agents with the matching provider:

```powershell
$env:TEAM_AGENT_ALLOW_REAL_MODEL_CALLS="1"
$env:OPENAI_API_KEY="..."
$env:DEEPSEEK_API_KEY="..."
```

Provider route example:

```json
{
  "agents": {
    "code_rd-coder": {
      "provider": "deepseek",
      "model": "deepseek-v4-pro",
      "reasoning_effort": "xhigh",
      "allow_real_calls": true
    },
    "code_rd-reviewer": {
      "provider": "litellm_proxy",
      "model": "gpt5.5",
      "reasoning_effort": "xhigh",
      "allow_real_calls": true
    }
  }
}
```

## Model Control Plane

The Harness keeps workflow orchestration separate from provider selection. A
model request is evaluated against configured primary/fallback routes, required
capabilities, provider readiness, the process-local health circuit, and the
run's durable real-model confirmation before any adapter is called.

The built-in capability registry is the default source for protocol, tool,
vision, reasoning, context, output, and price metadata. To replace it with a
reviewed local registry, set `TEAM_AGENT_MODEL_CAPABILITIES_CONFIG` to a JSON
file following `config/model-capabilities.example.json`. `input_price` and
`output_price` are USD per million tokens. Explicit per-route
`input_usd_per_million` / `output_usd_per_million` values take precedence. A
cost-bounded Agent Loop fails before dispatch when any candidate lacks a
complete price pair; unknown price or usage is never treated as zero.

The local diagnostic surface is:

- `GET /providers/doctor`: reports configuration, readiness, health, and
  capability-registry source without making a network call.
- `POST /routes/explain`: evaluates a proposed primary/fallback chain without
  calling a model.
- `GET /models/{provider}/{model}/capabilities`: resolves the effective
  capability entry for one provider/model pair.
- `POST /providers/{provider}/smoke`: performs a fixed health prompt. A real
  provider requires both server-side real-call enablement and
  `confirm_real_models=true`, so this endpoint can incur provider cost.

Retryable failures feed a process-local circuit breaker. Three consecutive
retryable failures open the provider circuit for 30 seconds; restarting the
service resets that health state. Fallback candidates remain ordered and must
individually pass capability, readiness, health, mock-fallback, and real-call
approval checks. Trace and benchmark accounting use the actual selected
provider/model and preserve route receipts for rejected or failed candidates.
For persisted runs, every physical HTTP attempt made by the exact built-in
OpenAI-compatible adapter first writes a durable `model_provider_attempt_started`
trace. If that write fails, dispatch is aborted; custom adapters cannot attest
trusted provider attempts. A crash after dispatch can still repeat the request
on recovery, so this does not provide exactly-once external execution.

LiteLLM Proxy route example:

```powershell
$env:TEAM_AGENT_ALLOW_REAL_MODEL_CALLS="1"
$env:LITELLM_API_KEY="..."
$env:LITELLM_BASE_URL="http://127.0.0.1:4000/v1"
$env:TEAM_AGENT_MODEL_ROUTING_CONFIG="config/model-routing.json"
```

`LITELLM_BASE_URL` defaults to the local proxy URL. Remote LiteLLM Proxy URLs are disabled by default so `LITELLM_API_KEY` is not sent to an arbitrary host by accident. If you intentionally use a trusted remote LiteLLM Proxy, it must use HTTPS and you must set:

```powershell
$env:TEAM_AGENT_ALLOW_REMOTE_LITELLM_PROXY="1"
```

```json
{
  "agents": {
    "code_rd_institutional-planner": {
      "provider": "litellm_proxy",
      "model": "gpt5.5",
      "reasoning_effort": "xhigh",
      "allow_real_calls": true
    },
    "code_rd_institutional-context_reader": {
      "provider": "litellm_proxy",
      "model": "deepseek-v4-pro",
      "reasoning_effort": "xhigh",
      "allow_real_calls": true
    },
    "code_rd_institutional-implementation_executor": {
      "provider": "litellm_proxy",
      "model": "gpt5.5",
      "reasoning_effort": "xhigh",
      "allow_real_calls": true
    },
    "code_rd_institutional-context_reviewer": {
      "provider": "litellm_proxy",
      "model": "deepseek-v4-pro",
      "reasoning_effort": "xhigh",
      "role_file": "roles/code-reviewer.md",
      "allow_real_calls": true
    },
    "code_rd_institutional-synthesizer": {
      "provider": "litellm_proxy",
      "model": "gpt5.5",
      "reasoning_effort": "xhigh",
      "allow_real_calls": true
    },
    "code_rd_institutional-final_reviewer": {
      "provider": "litellm_proxy",
      "model": "deepseek-v4-pro",
      "reasoning_effort": "xhigh",
      "role_file": "roles/code-reviewer.md",
      "allow_real_calls": true
    },
    "code_rd_institutional-final_approver": {
      "provider": "litellm_proxy",
      "model": "gpt5.5",
      "reasoning_effort": "xhigh",
      "allow_real_calls": true
    }
  }
}
```

`team_agent_harness` still performs orchestration, trace, artifacts, and evals. LiteLLM Proxy is only an optional OpenAI-compatible model gateway for unified provider access, virtual keys, cost tracking, fallback, or load balancing outside this harness.

Do not put API keys in the browser, task title/goal/inputs/constraints, SQLite data, source code, routing config, trace, queue/lock metadata, or artifacts. `POST /tasks` rejects obvious secret-like task content before storage, but that is a safety net, not a secret manager. Run detail and queue/lock endpoints expose safe summaries only; they do not expose raw queue/lock metadata, lock owners, external refs, provider headers, environment values, or tokens.

## Real Web Search Opt-In

Mock web search remains the default. Research Pack can use real Tavily search/fetch only after explicit server-side opt-in:

```powershell
$env:TEAM_AGENT_ALLOW_REAL_WEB_SEARCH="1"
$env:TEAM_AGENT_WEB_SEARCH_PROVIDER="tavily"
$env:TAVILY_API_KEY="..."
```

When enabled, `Searcher` can call `web_search`, while `Reader` and `Verifier` can call `fetch_page`. `Writer` and `Reviewer` do not directly access the web. Code R&D packs do not receive general web search.

Trace events record only safe summaries such as provider, query hash/length, a validated result limit or fixed invalid marker, public URL origins, result counts, latency, and content length. They do not record raw queries, input URL components, Tavily keys, request headers, full search snippets, or full fetched page bodies. `fetch_page` accepts public `http(s)` URLs only, canonicalizes IDNA hostnames before validation, and rejects localhost, loopback, private, link-local, CGNAT, reserved, transition/embedded non-public addresses, credentialed URLs, and fragments.

The UI shows `联网工具` provider status and asks for confirmation before running a workflow that has real web tools enabled. That confirmation is durable run authorization, not a one-time availability check: every real `web_search`, `fetch_page`, `browser_search`, and `browser_fetch` call is denied unless the run persisted the confirmation when it was created.

### Local Browser Web Access Opt-In

If you do not want a Tavily key, Research Pack can also use a local browser bridge. This is still explicit opt-in and only affects Research `Searcher`, `Reader`, and `Verifier`.

Chrome mode is the recommended local browser path on Windows. `scripts/start-litellm-harness.ps1` starts the bundled local Chrome CDP proxy when `TEAM_AGENT_BROWSER_PROVIDER=chrome`:

```powershell
$env:TEAM_AGENT_ALLOW_BROWSER_ACCESS="1"
$env:TEAM_AGENT_BROWSER_PROVIDER="chrome"
$env:TEAM_AGENT_BROWSER_SEARCH_ENGINE="google"
$env:TEAM_AGENT_BROWSER_CDP_URL="http://127.0.0.1:3456"
```

Optional Chrome overrides:

```powershell
$env:TEAM_AGENT_CHROME_PATH="C:\Program Files\Google\Chrome\Application\chrome.exe"
$env:TEAM_AGENT_CHROME_PROFILE_DIR="output\chrome-cdp-profile"
```

The proxy exposes its harness-facing HTTP bridge on port `3456` and launches Chrome DevTools on port `9223` by default. It launches a managed Chrome with an isolated user-data directory, incognito browser contexts, extensions disabled, and a pinned loopback egress proxy, so it does not reuse your daily Chrome profile, cookies, extensions, DNS path, or direct network path. If an unmanaged DevTools endpoint is already listening on the selected debug port, startup fails closed instead of reusing it.

Chrome support is intentionally narrow: the harness exposes `browser_search` and `browser_fetch` to the Research workflow through the local CDP bridge. It does not expose a general Chrome automation API, does not grant browser access to Code R&D packs by default, and does not reuse the user's daily Chrome profile unless explicitly configured outside the default isolated profile.

Generic CDP bridge mode is also available if you provide your own compatible local proxy implementing the same header-gated atomic-navigation, pinned-egress, and isolated-context capability contract:

```powershell
$env:TEAM_AGENT_ALLOW_BROWSER_ACCESS="1"
$env:TEAM_AGENT_BROWSER_PROVIDER="browser_cdp"
$env:TEAM_AGENT_BROWSER_SEARCH_ENGINE="bing"
$env:TEAM_AGENT_BROWSER_CDP_URL="http://127.0.0.1:3456"
```

With this mode enabled, `Searcher` can call `browser_search`, while `Reader` and `Verifier` can call `browser_fetch`. `Writer`, `Reviewer`, and Code R&D packs do not receive browser access.

The browser bridge expects a compatible local proxy at `TEAM_AGENT_BROWSER_CDP_URL`. Health and operations require the harness bridge header, the exact loopback authority, no browser `Origin`, and the `atomic_navigate_eval_v2`, `pinned_public_egress_v1`, and `isolated_browser_context_v1` capabilities. Each operation creates a fresh BrowserContext and target, denies downloads before target creation, keeps request interception active through evaluation, and disposes the context afterward. Cleanup confirmation requires well-formed context and target inventories; an unconfirmed cleanup resets the managed browser.

Chrome HTTP(S), redirects, WebSocket tunnels, and subresources are forced through a loopback egress proxy that connects only to the validated public numeric address on ports 80/443. CONNECT accepts TLS ClientHello traffic only, so plaintext `ws://` tunneling on port 443 is rejected. Any local/private DNS answer blocks the request; fake-IP fallback uses pinned secure DNS with a 60-second, 256-entry LRU cache. QUIC, WebTransport, non-proxied WebRTC UDP, and direct Chrome DNS resolution are disabled. The harness does not store browser cookies, headers, API keys, or full page HTML in trace. Browser-read page text is truncated on a complete UTF-8 byte boundary before it becomes an artifact; bridge responses are capped at 2 MiB and oversized responses fail closed. Trace stores only the URL origin plus a hash, never userinfo, query strings, fragments, or path tokens. Search and fetch availability are reported and routed independently, so a partial client or bridge failure cannot advertise the other tool as usable.

## Skill Library

Skill Library reads local `SKILL.md` files as prompt guidance. It does not execute skill scripts, does not load references/assets automatically, and does not grant tool permissions.

Default scan roots are fixed to the local allowlist:

- `%USERPROFILE%\.codex\skills`
- `%USERPROFILE%\.agents\skills`
- `%USERPROFILE%\.codex\skills\.system`

Bindings are stored in `config/skill-bindings.local.json`, separate from model routing. Saved bindings return `restart_required=true`: restart the harness service before expecting `/agents`, `/workflow-packs`, and model prompts to include the bound skills.

Skill content is appended after the agent role prompt with an explicit read-only boundary. It does not change `model_config`, `tool_permissions`, runtime policy, or approval policy. If a local skill contains secret-like text, the scanner marks it with `secret_like_content_omitted` and hides its content and frontmatter metadata from the API. Each agent can bind up to five skills, with a hard total prompt-content byte limit.

Skill Auto-Router is enabled in two layers:

- Startup auto-routing conservatively selects up to three relevant skills per agent from explicit workflow structure, agent role, and tool permissions. High-risk or broad skills such as UI design, browser automation, image generation, workflow meta-skills, and document tools require clear structural signals; role-card prompt text alone does not trigger them. Startup routes are injected as read-only guidance under `Auto-Selected Local Skills`, reported through `GET /skill-auto-routes`, and shown in the UI.
- Task-time auto-routing selects additional read-only skills for the current model request from the task title, goal, inputs, constraints, and acceptance criteria. It supports document handling (`pdf`, `docx`/Word, `pptx`/PPT, and `xlsx`/Excel/CSV) plus high-confidence domain families for security, performance, database, testing, architecture, web/UI, and AI/model work.

Auto-routing does not write `config/skill-bindings.local.json`, does not execute skill scripts, does not automatically read `references/`, does not grant any tool permission, and does not change model routing. Task-time routes are transient: they affect only that run's model request prompt, not `/agents` or `/workflow-packs`. Manual bindings remain available and are merged before auto-selected skills.

## Role Files

You can give an individual agent a dedicated system prompt by adding a markdown role file and referencing it from the server-side model routing config.

Role files should live under `config/roles/*.md`. Frontmatter is allowed for human metadata; the harness strips the frontmatter and sends the markdown body as the agent system prompt.

The thin UI also includes a `角色卡` page for local role-card management. It can create, edit, and delete `config/roles/*.md`, and bind a role card to a known agent by writing `config/model-routing.local.json`. Saved bindings return `restart_required=true`: restart the harness service before expecting `/agents`, `/workflow-packs`, and model calls to use the new role card.

Example:

```json
{
  "agents": {
    "code_rd_institutional-context_reviewer": {
      "provider": "litellm_proxy",
      "model": "deepseek-v4-pro",
      "role_file": "roles/code-reviewer.md",
      "allow_real_calls": true
    }
  }
}
```

`role_file` is resolved relative to the routing config file first, then the project root. It must point to a local `.md` file inside the routing config directory or project root, and the file must stay under 64KB.

Role files are not secret storage. Their content is used as model prompt text and is visible through local workflow/agent catalog APIs. Do not put API keys, relay secrets, tokens, private credentials, or unrelated sensitive data in role files.

`config/model-routing.local.json` is local machine state and is ignored by Git. The desktop/start scripts prefer it when present, then fall back to `config/model-routing.litellm.example.json`.

## Windows Download And Desktop Setup

GitHub users do not need to deploy FastAPI or LiteLLM manually:

1. Download the repository ZIP and extract the complete folder.
2. Double-click `Start-Team-Agent-Harness.cmd` at the repository root.
3. Wait for the first-run installer to create `.venv` and `.venv-litellm` and
   install their dependencies. Both environments remain inside the project.
4. Use the desktop launcher to enter the GPT relay key and `/v1` base URL plus
   the DeepSeek API key. The local LiteLLM key is a private value chosen by the
   user and must begin with `sk-`.
5. Choose **Save Config**, then **Start Services**. The launcher opens the UI
   automatically only after the worker, HTML workspace marker, and required API
   surface are ready. Use **Open UI** later when you only need to reopen it.

Setup selects Python 3.13 first and Python 3.12 second because LiteLLM is not
supported by this project on Python 3.14. When neither compatible version is
available, the user can explicitly approve a current-user Python 3.13 install
through `winget`. No provider key is placed in setup arguments, console output,
the shortcut, or tracked files.

The setup is idempotent. Dependency fingerprints are stored only inside the
ignored virtual environments, so later launches skip installation while a
changed `pyproject.toml` triggers dependency reconciliation. The desktop
shortcut also runs the setup check before opening `scripts/harness-launcher.ps1`.
The downloaded LiteLLM Proxy dependency is pinned to the release covered by the
current project verification record instead of following an untested latest release.
To rebuild incomplete project-local environments explicitly:

```powershell
.\scripts\setup-desktop.ps1 -Repair
```

## LiteLLM Manual Start

This repo includes safe, no-key templates:

- `config/litellm.config.example.yaml`
- `config/model-routing.litellm.example.json`
- `config/roles/code-reviewer.md`
- `scripts/start-litellm-harness.ps1`
- `scripts/harness-launcher.ps1`
- `scripts/create-desktop-shortcut.ps1`
- `.env.local.example`

For persistent local configuration, copy `.env.local.example` to `.env.local` and fill the values on your machine:

```powershell
Copy-Item .env.local.example .env.local
notepad .env.local
```

`.env.local` is ignored by Git and is loaded automatically by `scripts/start-litellm-harness.ps1`. Keep real keys only in `.env.local` or process environment variables.

You can also set keys in the current PowerShell session only:

```powershell
$env:LITELLM_API_KEY="replace-with-local-proxy-key"
$env:OPENAI_API_KEY="..."
$env:OPENAI_API_BASE="https://your-openai-relay.example.com/v1"
$env:DEEPSEEK_API_KEY="..."
```

Then start LiteLLM Proxy and the harness:

```powershell
.\scripts\start-litellm-harness.ps1
```

The start script automatically prefers `.venv-litellm\Scripts\python.exe` for LiteLLM and falls back to the main `.venv` only when that dedicated environment is absent. Harness, LiteLLM, and Chrome proxy interpreter selection remain independent. Before creating logs or probing support services, startup verifies any existing Harness listener by process identity; it later requires the same PID and creation time before sending Harness HTTP requests. A new or idle-replacement Harness also requires its selected interpreter to be a file. It validates service-specific health and OpenAPI identity, watches newly started child processes, and fails closed when a port belongs to an unrelated service. This keeps the documented Quick Start working when the main application environment uses Python 3.14, which LiteLLM does not currently support in this project. To create the dedicated environment when needed:

```powershell
py -3.13 -m venv .venv-litellm
```

To create or refresh only the desktop shortcut manually:

```powershell
.\scripts\create-desktop-shortcut.ps1
```

This creates `Team Agent Harness Launcher.lnk` on the desktop. The shortcut runs
the idempotent setup check and then opens the launcher. The launcher can edit
`.env.local`, save model settings, start/stop local services, open the Harness
UI, open the project folder, open `.env.local`, and open the log folder. Stop
actions revalidate the project venv command line, base Python executable,
service entry point, and port before terminating a process; unrelated port
owners are displayed but never stopped.

The script starts LiteLLM on `http://127.0.0.1:4000` and the harness UI on `http://127.0.0.1:8014/`. The harness routes GPT-family agents through the single LiteLLM alias `gpt5.5`; do not use role-specific GPT aliases such as `gpt-planner`, `gpt-coder`, or `gpt-reviewer` in project routing unless you intentionally reintroduce them. GPT routes use `OPENAI_API_BASE`, so they can point at an OpenAI-compatible relay instead of the official OpenAI endpoint. DeepSeek routes stay on `deepseek-v4-pro` and are used for long-context reading and review, not code writing or final approval.

The launcher also sets a bounded real-model failure budget unless you already provided one in the environment: `TEAM_AGENT_MODEL_TIMEOUT_SECONDS=180`, `REQUEST_TIMEOUT=180`, `TEAM_AGENT_LITELLM_PROXY_MAX_ATTEMPTS=1`, and `DEFAULT_MAX_RETRIES=0`. The previous 75-second boundary cut off a healthy GPT step; 180 seconds accommodates observed long responses while still preventing nested retries or an upstream outage from leaving a run indefinitely active. Each LiteLLM request also carries `x-litellm-timeout` so the proxy applies the same timeout per call. Override these values only when you intentionally want a different upstream wait.

The YAML/JSON files intentionally do not contain real keys. Edit model names in the YAML if your LiteLLM provider names differ, but keep credentials in environment variables or `.env.local`.

### LiteLLM Alias Rules

When `provider` is `litellm_proxy`, each harness agent should use the LiteLLM model alias, not the upstream model id. This project standardizes GPT-family routing on `gpt5.5`. For example, `config/model-routing.local.json` should contain:

```json
{
  "agents": {
    "code_rd_institutional-planner": {
      "provider": "litellm_proxy",
      "model": "gpt5.5",
      "reasoning_effort": "xhigh"
    },
    "code_rd_institutional-context_reader": {
      "provider": "litellm_proxy",
      "model": "deepseek-v4-pro",
      "reasoning_effort": "xhigh"
    }
  }
}
```

The alias is resolved by LiteLLM in `config/litellm.config.example.yaml`:

```yaml
model_list:
  - model_name: gpt5.5
    litellm_params:
      model: openai/gpt-5.5
      api_key: os.environ/OPENAI_API_KEY
      api_base: os.environ/OPENAI_API_BASE

  - model_name: deepseek-v4-pro
    litellm_params:
      model: deepseek/deepseek-v4-pro
      api_key: os.environ/DEEPSEEK_API_KEY
```

So the harness asks LiteLLM for `gpt5.5`, and LiteLLM calls the real upstream `openai/gpt-5.5`. If your OpenAI key is from a relay, keep `OPENAI_API_BASE` in `.env.local`; LiteLLM sends GPT alias traffic to that OpenAI-compatible relay instead of the official OpenAI endpoint. Do not put the relay key or any provider key in routing JSON or the browser UI.

Direct OpenAI-compatible provider routes are still supported for simple setups, but this project standardizes GPT-family routing on the LiteLLM alias `gpt5.5`. The recommended GPT + DeepSeek setup uses `litellm_proxy` with `model="gpt5.5"` so one harness config can switch or rename upstream providers centrally.

### Reasoning / Thinking Level

Codex's own thinking level is separate from model calls made by `team_agent_harness`.

- Codex thread reasoning, such as `reasoning_effort` or `thinking`, controls this Codex session while it edits and debugs the project.
- Harness model calls are external API calls made by the backend. Their `model_config` supports `provider`, `model`, `temperature`, `max_tokens`, and `reasoning_effort`.
- Project default: all real model routes store `reasoning_effort="xhigh"`. GPT-family routes use the `gpt5.5` LiteLLM alias; DeepSeek routes also keep `xhigh` as the configuration intent.
- The backend sends or maps this option per provider capability. OpenAI-compatible standard routes map configured `xhigh` to the highest standard sent value (`high`) unless a provider-specific passthrough is enabled; providers or aliases not known to support the option are not sent the parameter.
- Model trace events record `configured_reasoning_effort`, `sent_reasoning_effort`, `reasoning_effort_sent`, and, when applicable, `reasoning_effort_mapping` or `reasoning_effort_ignore_reason`.

Recommended implementation shape:

```json
{
  "agents": {
    "code_rd_institutional-planner": {
      "provider": "litellm_proxy",
      "model": "gpt5.5",
      "reasoning_effort": "xhigh"
    }
  }
}
```

The backend validates accepted values (`minimal`, `low`, `medium`, `high`, and `xhigh`), passes them only to providers or aliases known to support them, and records configured versus sent or ignored values in model trace events. Choose `gpt5.5` for GPT planner/coder/approver roles, and use DeepSeek aliases for long-context reading, review, and research control.

### Real-Model Timeout Handling

LiteLLM itself defaults to very long upstream waits and provider retries. The harness keeps the failure boundary local and observable:

- `TEAM_AGENT_MODEL_TIMEOUT_SECONDS` controls the harness OpenAI-compatible client timeout and defaults to 180 seconds in the launcher.
- Provider semaphore waiting, provider retries, and retry backoff consume one monotonic request deadline. Each retry receives only the remaining time instead of restarting the timeout budget.
- LiteLLM proxy calls receive `x-litellm-timeout` with that same value.
- `TEAM_AGENT_LITELLM_PROXY_MAX_ATTEMPTS` defaults to `1` so harness retries do not stack on top of LiteLLM retries.
- `REQUEST_TIMEOUT` and `DEFAULT_MAX_RETRIES` are set by `scripts/start-litellm-harness.ps1` for the local LiteLLM process.

If a relay returns `504`, the run should fail with a model error trace instead of waiting through several nested retry loops. The remote relay or upstream provider may still be unhealthy; the local fix is to fail clearly and quickly, not to hide the outage.

Provider failures in `code_rd_institutional` are also fail-closed by default. Set `TEAM_AGENT_ALLOW_MODEL_FALLBACK_TO_MOCK=1` only when an operator explicitly wants non-approved session steps in that pack to fall back to mock output; without that exact opt-in, a real-provider error fails the run and cannot be mistaken for a real-model result.

## Local Code Executor MVP

`code_rd_institutional` can now use a local code executor for the `prepare_patch` and `test_changes` steps when a task explicitly provides `repository_path`.

Example task inputs:

```json
{
  "repository_path": "E:\\your-project",
  "focus_paths": ["app", "tests"],
  "test_command": "python -m pytest -q",
  "allow_host_test_execution": true
}
```

Current safety behavior:

- The Harness itself never writes the source repository during `prepare_patch` or `test_changes`. The test process still runs with the current host user's permissions, so the working copy is not an OS security sandbox. Use only trusted repositories/providers or add an external VM/container boundary.
- Host pytest is denied unless the task explicitly sets `allow_host_test_execution=true`. The executor copies the repository into `output/local_code_workspaces/<run_id>/<step_name>/repo`; source and workspace paths must be disjoint, and neither may contain the other.
- Copying is rejected before `copytree` when the filtered repository exceeds 20,000 files or 500 MB.
- `.env`, secret-like files, symlinks, hard-linked files, Windows junctions and other reparse points, `.git`, `.venv`, `node_modules`, cache folders, generated output folders, binary files, and large files are skipped.
- `prepare_patch` produces a patch proposal artifact only; it does not apply the patch.
- `test_changes` accepts exactly one `PATCH` artifact through the canonical handoff from the current completed `prepare_patch` attempt. It verifies the artifact's stored byte hash, reparses the fenced unified diff without stripping significant trailing whitespace, copies the source repository into the test workspace, checks the copied base hashes, and applies that exact patch before taking the test snapshot or starting pytest. Immediately before pytest, every source patch target must still match the patch-declared base hash; the same targets are checked again after pytest.
- The test report records the patch artifact id, patch hash, changed files, and working-copy application state. Patch bytes are verified again after pytest and every later artifact excerpt read, closing the test-to-review tamper window. A missing or tampered artifact, invalid diff, base mismatch, missing/non-executing test command, timeout, or non-zero test exit fails the step and prevents downstream review.
- `test_changes` runs a conservative pytest-core option allowlist through the Harness interpreter. Collect/setup/help/version modes (including `-VV`), `--pyargs`, response files, unknown plugin options, absolute/parent paths, and path-changing options are rejected. Host `PYTEST_ADDOPTS` and project `addopts` are overridden. The Harness also parses a separate JUnit report and requires at least one non-skipped test case with zero failures/errors, so an all-skipped run or a simple `conftest.py` exit-code override cannot produce a passing checkpoint.
- Test commands receive an environment-variable allowlist with private HOME/TEMP directories. Source/workspace absolute paths are removed from task/context model payloads, pytest output, model review text, artifacts, summaries, and persisted executor errors. Secret-like assignments, credentialed connection URLs, common provider tokens, JWTs, and private-key blocks are redacted from repository context, test output, model review text, and runner error summaries. Embedded credentials can still exist in the copied repository and remain readable by host pytest; do not use untrusted repositories without a VM/container boundary. Proxy variables provide best-effort egress reduction, but they do not block raw sockets and are not a network sandbox.
- Institutional mock execution does not synthesize a successful test result when `repository_path` or `test_command` is absent. It fails the required `patched_local_test_command` gate instead.

## Explicit Writeback

Generated code is still not written back by normal runtime-job approval. `POST /runs/{run_id}/runtime-jobs/{job_id}/approve` only lets the waiting local executor step continue.

To apply a generated patch to the original repository, use the separate writeback API:

1. Run `code_rd_institutional` with `repository_path`, `focus_paths`, `test_command`, and `allow_host_test_execution=true`.
2. Approve the `prepare_patch` runtime job so the patch artifact is created.
3. Preview the writeback:

```http
POST /runs/{run_id}/writeback/preview
```

```json
{
  "patch_artifact_id": "patch-artifact-id"
}
```

4. Inspect the returned `writeback_id`, `patch_hash`, `files_changed`, and `base_hashes`.
5. Explicitly approve writeback:

```http
POST /runs/{run_id}/writeback/approve
```

```json
{
  "patch_artifact_id": "patch-artifact-id",
  "writeback_id": "preview-writeback-id",
  "confirm_repository_path": "E:\\your-project",
  "confirm_patch_hash": "preview-patch-hash",
  "expected_base_hashes": {
    "app/example.py": "preview-base-hash"
  }
}
```

Writeback safety limits:

- Only fenced unified diffs are accepted.
- The MVP only supports modifying existing text files.
- Create, delete, rename, binary, absolute-path, parent-directory, symlink, sensitive-file, and excluded-directory patches are rejected.
- `focus_paths` are enforced again during writeback.
- Preview and approval accept only a PATCH owned by the latest completed `prepare_patch` attempt for that run; artifacts from failed, cancelled, or superseded attempts are rejected. Approval revalidates the same ownership and artifact hash after the potentially long test command, before it creates a source-write transaction.
- Before writing, the executor applies the exact fenced diff bytes, including significant final-line whitespace and EOF newline semantics, to a disjoint working copy and runs the allowlisted `test_command` under the same explicit host-execution opt-in.
- The original file hash must still match the preview `base_hashes`; otherwise writeback returns a conflict and does not overwrite local edits.
- Approval is serialized in the supported single-process runtime. A repeated concurrent approval returns the persisted completed result instead of applying the patch twice.
- Multi-file source updates use atomic replacements and roll back already-written files in reverse order if a later write fails.
- The final `writeback_applied` audit trace is part of the success boundary. If that trace cannot be persisted, all just-written files are restored to their verified base contents; any rollback failure is reported with the affected file names.
- Before source replacement, version 2 journals persist verified base backups and transaction-owned temporary paths. Startup recovery runs before the worker starts, revalidates the journal against SQLite and the patch artifact, and hashes every target as one set: an exact success trace plus all-new hashes is retained, base/new mixed state is rolled back as a whole, and any unknown target bytes stop startup without deleting the journal or overwriting the external edit.
- Trace records file names and hashes, not API keys or full patch contents.

## Manual Developer Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -r requirements.lock
.\.venv\Scripts\python -m pip install --no-deps -e .
```

`requirements.lock` pins the complete tested runtime/test dependency graph by version. It intentionally does not include distribution hashes, so installation still trusts the configured package index. The Windows desktop bootstrap uses the same lock, and `.github/workflows/ci.yml` is configured to verify it on Windows with Python 3.12, 3.13, and 3.14. Local verification does not prove that the remote GitHub Actions matrix has executed.

## Run Tests

```powershell
.\.venv\Scripts\python -m pytest -v
```

### Current Verification Record

- On 2026-08-14 the CI-stabilized code snapshot at commit `5643b69` passed the full local backend suite with `1321 passed, 5 skipped, 1 warning` in 443.24 seconds. The runtime body-read abort target passed 100/100 repetitions, the worker shutdown/backlog target passed 20/20 repetitions, the strict setup ready-state target passed 100/100 repetitions, and the complete startup-script file passed 59 tests. GitHub Actions run `31795016194` then passed Windows Python 3.12, 3.13, and 3.14 with `1325 passed, 1 skipped, 1 warning` per job. These fixes change tests only: they use explicit body-read and worker-completion barriers, retain independent real hang watchdogs, and replace repeated environment-wide `pip check` calls in the setup state-machine test with a fail-closed exact-argv shim. Production worker shutdown, launcher readiness, model routing, and request timeout behavior are unchanged. Compile, both dependency environments, both JavaScript files, all four PowerShell scripts, YAML/JSON, whitespace, and intended-diff credential checks passed. Harness health and UI returned HTTP 200 with the worker running, and LiteLLM liveliness returned HTTP 200. GitHub's Node.js 20 action deprecation annotation remains a separate CI-maintenance item. No real or paid model call was made.
- On 2026-08-14 the configurable-team, launcher, quality-lineage, and restricted Codex MCP release candidate passed the full local backend suite with `1321 passed, 5 skipped, 1 warning` in 445.21 seconds. The warning remains the existing Starlette `TestClient` deprecation. `python -m compileall -q app scripts`, `pip check` in both project environments, JavaScript syntax for both static scripts, all four tracked PowerShell AST parses, YAML/JSON parsing, `git diff --check`, and high-confidence credential and private-path scans passed. Quality evaluation binds each required artifact and acceptance result to the latest completed attempt, and Pack acceptance evidence must have one matching trace in the correct attempt/scope. The MCP adapter applies one monotonic wall-clock deadline across every HTTP request in a tool call, reads response bodies against the remaining budget, and bounds remembered Run receipts to a 128-entry LRU whose evictions are revalidated from Run/Team/hash lineage. Launcher startup now rejects an unrelated Harness listener before HTTP or support-service effects, fails before those effects when a free port has no interpreter file, and binds the later readiness probe to the initially verified PID and creation time. Harness interpreter overrides do not alter LiteLLM fallback or Chrome proxy runtimes. Launcher saves preserve unrelated `.env.local` fields/comments and protected ACL semantics through same-directory atomic replacement; post-replace ACL failure restores the original content and ACL, while incomplete rollback fails explicitly and retains the recoverable original backup. Service rollback stops the new process before restoring a reconstructable prior Harness, unreconstructable enabled-provider state is reused fail-closed, and active-work inspection returns `unknown` at its 20-Run bound. Task and Run listboxes implement Arrow/Home/End/Enter/Space behavior with roving `tabindex`. A fresh temporary-data Uvicorn process completed a six-step mock Research run through the real stdio MCP process: the fixed 12-tool catalog was present, `harness_list_recent`, `harness_get_quality`, and `harness_get_final_artifact` all passed their Run/hash bindings, quality passed 28 checks, six AgentRun checkpoints and six artifacts were persisted, and no error trace remained. The normal Harness and LiteLLM processes then passed live health/readiness checks on ports `8014` and `4000`. A fresh post-fix isolated Chrome pass covered `1440`, `390`, and `320` pixel layouts, found no page-level horizontal overflow or failed application request, reported zero console warnings/errors, and exercised task selection with Arrow/Enter plus Run selection with End/Space. Narrow navigation and detail-tab rows remained reachable through explicit horizontal scrolling. No real or paid model call was made. This is local worktree evidence only and does not claim that CI or the remote repository contains these changes.
- On 2026-08-10 the configurable-team, desktop-launcher, and restricted Codex MCP increment passed the focused execution-plan, Team Selection, API, and MCP regression set with `220 passed, 1 warning` and the full local backend suite with `1027 passed, 5 skipped, 1 warning`. `python -m compileall -q app scripts`, `pip check`, JavaScript syntax, every tracked PowerShell AST parse, `git diff --check`, and high-confidence credential-exposure scans passed. Browser verification completed default-team and six-position custom GPT/DeepSeek mock runs, confirmed immutable team receipts and passing quality reports, exercised widths `320`, `390`, `768`, `1024`, and `1440` without horizontal overflow or control overlap, preserved focus across provider/model redraws, enforced disabled custom-team controls, and reported no console warning or error. No real or paid model call was made. This is local worktree evidence only and does not claim that CI or the remote repository contains these changes.
- On 2026-08-10 the async-worker CI stabilization at commit `d8ce98b` passed the affected-file suite with `69 passed, 1 warning` and the full local backend suite with `823 passed, 5 skipped, 1 warning`. Worker-facing tests now use one shared 60-second test-only observation bound for scheduling, recovery, and terminal-state paths; `background_run_completed` is observed after authoritative queue terminalization and lock release, while deterministic release, join, heartbeat, ordering, and cleanup probes retain narrow bounds. `python -m compileall -q app scripts`, `pip check`, all four tracked PowerShell parse checks, two YAML and three JSON configuration parses, a high-confidence intended-diff credential scan, and `git diff --check` passed. GitHub Actions run `31323576572` passed the Windows Python 3.12, 3.13, and 3.14 matrix with `827 passed, 1 skipped, 1 warning` per job. The test warning remains the existing Starlette `TestClient` deprecation. GitHub also emitted its existing Node.js 20 action deprecation annotation; no CI workflow change was included in this test-only fix. No real or paid model call was made.
- On 2026-08-07 the final release review passed the full backend suite with `823 passed, 5 skipped, 1 warning`. Fourteen focused adversarial regressions proved pre-dispatch durable provider-attempt evidence, fail-closed trace persistence, partial-usage accounting, local serialization rejection, sensitive-identifier sanitization, the exact built-in adapter trust boundary, and recorder coverage for normal Pack, Agent Loop, vision-sidecar, and Institutional local-code paths. `python -m compileall -q app scripts`, `pip check`, all four tracked PowerShell parse checks, CI/config YAML parsing, and `git diff --check` passed. The warning remains the existing Starlette `TestClient` deprecation. A fresh temporary-data Uvicorn smoke completed all six mock steps with `quality_passed=true`, six AgentRun checkpoints, five handoffs, six artifacts, 18 eval results, and no error trace; the service stopped cleanly and the temporary data was removed. This is local worktree evidence only and does not claim that CI or the remote repository contains these changes.
- On 2026-08-06 the current worktree passed the full backend suite with `751 passed, 5 skipped, 1 warning`; the final focused cross-module regression set passed `203 passed, 1 skipped`, and the multimodal-only regression set passed `22 passed`. `python -m compileall -q app scripts`, `pip check`, `git diff --check`, and a high-confidence source/config secret scan also passed. The warning remains the existing Starlette `TestClient` deprecation. A fresh temporary-data Uvicorn smoke on port `8015` completed all six mock steps with `quality_passed=true`, six metered model calls, complete usage, six AgentRun checkpoints, five handoffs, six artifacts, 17 eval results, and no error trace; the service was then stopped and its temporary data removed. This is local worktree evidence only and does not claim that CI or the remote repository contains these changes.
- On 2026-08-04 the immutable-plan, Agent-Loop, shared-deadline, quality/benchmark, dynamic-plan gate, frozen-recovery, final-artifact-lineage, and static-executor-boundary increment passed the full backend suite with `705 passed, 5 skipped, 1 warning`. `python -m compileall -q app scripts`, `pip check`, all project PowerShell parse checks, CI YAML parsing, `git diff --check`, and a source/config secret scan also passed. The warning remains the existing Starlette `TestClient` deprecation. A forced-restart Uvicorn smoke submitted 20 temporary-data mock runs, killed the service while all 20 were non-terminal, then recovered all 20 to `completed`; all 20 quality reports passed, no error trace remained, and every queue item and lock was terminal.
- On 2026-08-03 the patched-test, dependency-provenance, credential-redaction, and worker-recovery hardening passed the full backend suite with `643 passed, 5 skipped, 1 warning`. The warning is the existing Starlette `TestClient` compatibility deprecation for the installed `httpx`; it does not affect the live Uvicorn path. Live and recovery paths reject mixed-attempt/duplicate/extra handoffs, multiple canonical PATCH artifacts, inexact lineage, and missing/tampered checkpoint files. Pytest requires independent test-case evidence instead of trusting exit code alone; writeback rejects stale attempts; storage retry and missing-pack paths preserve atomic runtime/run/queue terminal state. EOF diff semantics and the 20 consecutive real-loop terminalization retry repetitions remain covered.
- A fresh temporary-database live smoke queued and completed all six mock steps, persisted six artifacts and a final artifact, completed its queue item, released its lock, recorded no error trace, and released the service port after shutdown.
- A fresh two-process forced-restart smoke killed the first service with the run, first agent attempt, queue item, and lock all persisted as active. The second service cancelled only the interrupted attempt, completed its retry plus the remaining steps, preserved `cancelled/completed` queue history, released both locks, produced six completed checkpoints and six artifacts, recorded `interrupted_run_requeued` with no error trace, and released both service ports. A separate terminal-window interruption was also recovered on the next startup from `completed + running queue + acquired lock` to a completed queue and released locks.
- On 2026-07-28 the full backend suite passed with `569 passed, 5 skipped`; `python -m compileall -q app scripts`, `pip check` in both project environments, JavaScript syntax, and all project PowerShell parse checks also passed. The desktop bootstrap also completed a real dependency reconciliation and then reused both environments without reinstalling them on its second run.
- A temporary-database 100-run stress completed 100/100 runs, 600/600 agent checkpoints, and 600 artifacts with no error trace, active lock, queue, session, job, or SQLite integrity failure. A run lasting beyond the production five-second heartbeat interval advanced its heartbeat.
- A separate forced-process restart preserved the completed first checkpoint, cancelled and retried only the interrupted second step, wrote its retry artifact with `attempt-2`, completed the recovered run, and released both old and new locks. The recovery phase took 2.141 seconds in the deterministic test.
- Paid route smoke passed for `gpt5.5` and `deepseek-v4-pro`. Stored run `edfbd0e2-443e-43c4-b9a2-75cd52a39430` completed all six real Research steps in 172.3 seconds with `mocked=false`, six `finish_reason=stop` responses, no error trace, a completed queue item, a released lock, and a non-empty final artifact.
- Playwright loaded the live UI at 1440x1000 and 390x844, opened the completed run and final artifact, observed no horizontal overflow, received HTTP 200 for every application request, and reported zero console warnings or errors.

## Run Server

```powershell
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

Then open:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/docs`

The root URL serves the Chinese thin UI from the same FastAPI origin as the API.

## Codex Operator Mode

Codex can act as an operator for a running local harness service. In this mode Codex is not the framework underneath the harness; it is a control console that calls the harness API to create tasks, choose a workflow pack, start runs, poll status, inspect trace/artifacts, and report approval points back to you.

Use the safe CLI wrapper when operating from a terminal or from Codex:

```powershell
.\.venv\Scripts\python.exe scripts\harness_control.py --base-url http://127.0.0.1:8014 health
.\.venv\Scripts\python.exe scripts\harness_control.py workflow-packs
.\.venv\Scripts\python.exe scripts\harness_control.py model-providers
```

Before running a full real-model workflow, you can smoke test LiteLLM aliases with tiny requests. This still may call paid external model APIs through LiteLLM, so it requires the same explicit real-model confirmation:

```powershell
.\.venv\Scripts\python.exe scripts\harness_control.py model-smoke `
  --confirm-real-models `
  --model gpt5.5 `
  --model deepseek-v4-pro `
  --max-tokens 8
```

This checks the LiteLLM-to-provider path only with a fixed tiny prompt. It does not accept custom prompt text. `--max-tokens` is bounded to `1..128`, defaults to `8`, and should stay tiny. The command returns provider/model observability, latency, a short content preview, and usage when available; it does not print API keys. Remote LiteLLM Proxy URLs are disabled for smoke tests unless `TEAM_AGENT_ALLOW_REMOTE_LITELLM_PROXY=1` is set and the URL uses HTTPS.

Create a task without starting it:

```powershell
.\.venv\Scripts\python.exe scripts\harness_control.py create-task `
  --title "代码审查任务" `
  --goal "审查目标项目的核心变更，输出问题、风险和建议。" `
  --workflow-pack code_rd_institutional `
  --input-json '{"repository_path":"E:\\your-project","focus_paths":["app","tests"],"test_command":"python -m pytest -q","allow_host_test_execution":true}'
```

Start a run:

```powershell
.\.venv\Scripts\python.exe scripts\harness_control.py start-run --task-id <task-id>
```

`start-run` submits to the local background worker and returns the queued run. Use `run-status` or `run-detail` to poll; closing the CLI does not stop the persisted run.

If `/model-providers` shows any enabled real provider and the selected workflow uses it, `start-run` refuses to proceed unless you add explicit confirmation:

```powershell
.\.venv\Scripts\python.exe scripts\harness_control.py start-run --task-id <task-id> --confirm-real-models
```

If real Tavily search or local browser access is enabled and the selected workflow can call those tools, the server also requires:

```powershell
.\.venv\Scripts\python.exe scripts\harness_control.py start-run --task-id <task-id> --confirm-real-web
```

When both are enabled for the selected workflow, pass both flags.

Inspect a run:

```powershell
.\.venv\Scripts\python.exe scripts\harness_control.py run-status --run-id <run-id>
.\.venv\Scripts\python.exe scripts\harness_control.py run-detail --run-id <run-id>
.\.venv\Scripts\python.exe scripts\harness_control.py list-approvals --run-id <run-id>
```

Safety boundary:

- The CLI can observe, create tasks, start runs, and list approval-required runtime jobs.
- It intentionally has no command for `approve`, `reject`, `cancel`, writeback preview/approve, arbitrary shell execution, Git operations, dependency installation, deletion, deployment, or key editing.
- Codex may automate observation and task creation. Real model calls, runtime-job approval, external executor approval, writeback to the original repository, key changes, and global environment changes still require explicit user confirmation.
- API keys stay in server-side environment variables or `.env.local`; never put keys in CLI arguments, task inputs, routing JSON, trace, artifacts, or UI fields.

### Restricted Codex MCP Adapter

`scripts/codex_harness_mcp.py` exposes the same narrow operator boundary over
stdio MCP. It is intentionally not registered by setup because `codex mcp add`
changes the user's Codex configuration. From this backend directory, register
the current checkout explicitly:

```powershell
$backend = (Get-Location).Path
codex mcp add team-agent-harness -- `
  "$backend\.venv\Scripts\python.exe" `
  "$backend\scripts\codex_harness_mcp.py"
codex mcp get team-agent-harness
```

The adapter binds only to an explicit loopback `http` Harness origin, disables
HTTP proxies and redirects, and communicates with Codex through strict UTF-8
newline-delimited JSON-RPC. Its fixed allowlist contains exactly 12 tools:
`harness_health`, `harness_list_catalog`, `harness_list_recent`,
`harness_get_team_template`, `harness_create_task`, `harness_validate_team`,
`harness_start_run`, `harness_get_run`, `harness_get_run_detail`,
`harness_get_run_team`, `harness_get_quality`, and
`harness_get_final_artifact`. Recent-list reads use fixed server-side bounds;
final-artifact reads require a completed Run, matching Run/artifact/hash
lineage, and return at most 100,000 characters marked as untrusted content. The
adapter has no approval, writeback, shell, Git, configuration,
dependency-installation, deletion, deployment, or secret tool.

By default an MCP-launched run cannot assert real-model or real-web consent.
Grant either capability only when registering the MCP process intentionally:

```powershell
codex mcp add `
  --env TEAM_AGENT_CODEX_ALLOW_REAL_MODELS=1 `
  --env TEAM_AGENT_CODEX_ALLOW_REAL_WEB=1 `
  team-agent-harness -- `
  "$backend\.venv\Scripts\python.exe" `
  "$backend\scripts\codex_harness_mcp.py"
```

These flags do not contain provider keys and do not bypass the Harness's own
provider gates. API keys remain only in the Harness process environment or its
ignored `.env.local`.

## API Surface

- `POST /tasks`
- `POST /task-intake/analyze`
- `GET /tasks`
- `GET /tasks/{task_id}`
- `POST /team-selections/validate`
- `GET /workflow-packs/{pack_name}/team-template`
- `POST /runs`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/team`
- `GET /runs/{run_id}/detail`
- `GET /runs/{run_id}/quality`
- `GET /runs/{run_id}/trace`
- `GET /runs/{run_id}/artifacts`
- `GET /runs/{run_id}/agent-runs`
- `GET /runs/{run_id}/handoffs`
- `GET /runs/{run_id}/eval-results`
- `GET /runs/{run_id}/runtime-sessions`
- `GET /runs/{run_id}/runtime-jobs`
- `GET /runs/{run_id}/queue-state`
- `GET /runs/{run_id}/lock-state`
- `POST /execution-plans/validate`
- `POST /execution-plans/generate`
- `POST /runs/{run_id}/runtime-jobs/{job_id}/approve`
- `POST /runs/{run_id}/runtime-jobs/{job_id}/reject`
- `POST /runs/{run_id}/runtime-jobs/{job_id}/cancel`
- `POST /runs/{run_id}/writeback/preview`
- `POST /runs/{run_id}/writeback/approve`
- `GET /artifacts/{artifact_id}`
- `GET /workflow-packs`
- `GET /workflow-packs/{pack_name}`
- `GET /model-providers`
- `GET /providers/doctor`
- `POST /routes/explain`
- `POST /providers/{provider}/smoke`
- `GET /models/{provider}/{model:path}/capabilities`
- `GET /tool-providers`
- `GET /skill-auto-routes`
- `GET /agents`
- `GET /role-cards`
- `GET /role-cards/{role_card_id}`
- `PUT /role-cards/{role_card_id}`
- `DELETE /role-cards/{role_card_id}`
- `GET /agent-bindings`
- `PUT /agent-bindings/{agent_id}`
- `DELETE /agent-bindings/{agent_id}`
- `GET /skills`
- `GET /skills/{skill_id}`
- `POST /skills/refresh`
- `GET /skill-bindings`
- `PUT /skill-bindings/{agent_id}`
- `DELETE /skill-bindings/{agent_id}`

`GET /skills/{skill_id}` is a local preview endpoint and returns the local `SKILL.md` body unless the skill scanner omitted it for secret-like content. Run detail, agent catalog, workflow pack catalog, and task-time skill route trace events expose skill ids, reasons, and injected byte counts only; they do not expose task-time skill bodies.

## Thin UI Surface

- Load backend health, workflow pack catalog, model provider skeleton catalog, agent catalog, tasks, and runs.
- Inspect selected pack steps, required inputs/artifacts, allowed tools, agent tool permissions, and eval checks through `GET /workflow-packs/{pack_name}` while keeping the workflow pack catalog as the selector source.
- Inspect manually bound and auto-selected Skill Library routes. Auto-selected skills are shown as read-only prompt guidance and are not saved as manual bindings.
- Inspect Main/Subagent coordination roles, controller steps, dependency branches, and return contracts for workflow steps.
- Inspect runtime and session policy metadata for long-running sessions and future ACP-backed execution steps.
- Inspect local runtime session, job, approval-intent, queue, and lock records while clearly distinguishing the local background worker from external ACP/process execution.
- Inspect ready-set batches, true parallel execution trace events, gate fields, ownership declarations, and task-time skill route summaries without exposing skill bodies.
- Fill Code R&D, Institutional Code R&D, or Research example tasks.
- Create a task for `code_rd`, `code_rd_institutional`, or `research`.
- Submit a selected task to the local background worker and poll queued/running/waiting/failed/completed state; default pack routes are mock, enabled real-provider and real web/browser routes require server-side opt-in plus UI confirmation, and ACP-marked steps pause for local approval before continuing.
- Approve, reject, or cancel local runtime jobs from the UI. These actions only update local records and trace events; they do not start external ACP.
- Inspect run status, current step, final artifact id, execution chain, per-step model route, mock/real marker, latency, usage, agent run status, local session/job status, approval-required jobs, queue/lock safe summaries, handoffs, eval results, failed run error summaries, trace timeline, artifact metadata, and artifact text content.
- Load selected run details through `GET /runs/{run_id}/detail` while keeping individual observability endpoints available for debugging and direct API clients.
