# Team Agent Harness Backend

Local single-process backend for the team-oriented multi-agent harness.

## Current Scope

This directory includes the original MVP phases plus the durable local worker and bounded-context increment completed on 2026-07-10:

- FastAPI application skeleton with worker-aware `GET /health`; it returns HTTP 503 when the background worker thread is not running.
- Core Pydantic domain models and enums, including local runtime session, runtime job, run queue item, and run lock records.
- Rule-based task intake analysis that can classify task type, complexity, risk, domain, recommended workflow pack, confidence, reasons, and suggested constraints without creating tasks or starting runs.
- SQLite storage layer for core models plus local runtime session/job observability records and local run queue/lock records.
- Workflow pack schema declarations, including lightweight DAG metadata (`depends_on`, `phase`, and `produces_artifact_type`), explicit Main/Subagent coordination metadata (`coordination_role`, `controller_step`, and `return_contract`), step gate metadata (`requires_eval_pass`, `requires_artifact`, and `ownership`), long-running runtime metadata (`runtime` and `session_policy`), and bounded per-position context policy (`context_policy`).
- In-memory agent registry for pack/role lookup.
- Trace logging and local file-backed artifact storage.
- Tool gateway with permission checks, mocked tools, optional Tavily-backed web search/fetch tools, and optional local browser bridge search/fetch tools for Research.
- Model runtime contract with a deterministic mocked adapter and `model_request` / `model_response` trace observability.
- Real-model responses fail closed: only `stop`/`completed` responses with non-empty string text are accepted; truncated, filtered, tool-call-only, failed, incomplete, missing, or non-text responses raise a sanitized runtime error without persisting the raw provider response.
- Mocked multi-model profile assignment inside each workflow pack, so one run can show two or more model profiles cooperating through agent-level `model_config`.
- Provider catalog and explicit opt-in OpenAI-compatible adapters for OpenAI, DeepSeek, and an optional LiteLLM Proxy gateway; mock remains the default, while Anthropic and local providers are still skeleton-only.
- Server-side model routing config that can assign selected agents to `mock`, `openai`, `deepseek`, or `litellm_proxy` without storing API keys in the app.
- Single-process workflow runner with deterministic mocked agent execution, dependency-aware ready-set step scheduling, dependency-edge handoffs for DAG packs, bounded context envelope construction, workflow-level intake/context/skill-route trace events, structural checkpoint evaluation, step eval/artifact gate enforcement, ready-batch/ownership conflict checks, conservative opt-in parallel executor dispatch for safe DAG batches, failure trace recording, run/agent-run status updates, local runtime session/job metadata recording for `session` and `acp` steps, and local approval gates.
- Durable local `RunWorker` plus run coordinator. UI and operator CLI submissions return after persistence, execute outside the initiating HTTP request, maintain a per-run lock heartbeat, and recover interrupted runs from completed step checkpoints on service restart.
- Mocked Code R&D workflow pack with Clarifier, Architect, Coder, Tester, Reviewer, and Finalizer steps.
- Mocked Institutional Code R&D workflow pack with GPT as the trusted main thread, DeepSeek V4 Pro long-context reading/review roles, GPT implementation/test executor branches that require local approval before their mocked/model step execution proceeds, DeepSeek final risk review, and GPT final approval.
- Research workflow pack with Planner, Searcher, Reader, Verifier, Writer, and Reviewer steps. It defaults to mock web search/fetch and can use Tavily or local browser bridge search/fetch only after explicit server-side opt-in.
- FastAPI endpoints for creating/listing tasks, starting workflows synchronously or in the local background worker, reading run metadata, trace events, artifacts, workflow pack catalogs, single workflow pack details, model provider skeletons, tool provider status, local Skill Library metadata, skill bindings, and pack agent catalogs; default routes remain mock unless server-side routing explicitly enables real providers for selected agents. `GET /tasks` and `GET /runs` are bounded to 500 newest records by default and accept validated `limit`/`offset` query parameters up to 1000 records.
- Conservative Skill Auto-Router that can automatically select relevant read-only local skills for agents from explicit workflow, role, tool-permission, document-file task signals, and high-confidence domain task signals such as security, performance, database, testing, architecture, UI/web, and AI/model work while preserving manual bindings as an override/extension path.
- FastAPI endpoints for reading full run observability data: aggregated run detail, agent runs, handoffs, eval results, trace events, artifacts, runtime sessions, runtime jobs, and safe queue/lock summaries.
- FastAPI endpoints for local runtime job approval actions: approve, reject, and cancel. These mutate local job/session/run state only; they do not launch external ACP processes.
- Safe Codex/operator CLI for creating tasks, starting runs, polling run state, reading run details, listing approval-required runtime jobs, and inspecting provider/pack catalogs without exposing approval, writeback, or arbitrary shell commands.
- Same-origin Chinese thin UI for creating tasks, filling Code R&D / Institutional Code R&D / Research examples, running workflows, confirming enabled real-provider and real web-search routes before execution, inspecting selected pack details including step phase, dependencies, Main/Subagent coordination role, runtime, session policy, return contract, agent model assignment, provider/tool status, local Skill Library bindings, and reading execution chains, local session/job/approval/queue/lock status, eval results, trace events, failure summaries, artifacts, and artifact content through the aggregated run detail contract.
- Unit tests for health check, OpenAPI availability, model validation, model runtime contract, model routing, JSON serialization, storage round trips, pack schema validation, registry behavior, trace logging, artifact writes, tool gateway behavior, runner success/failure paths, Code R&D pack happy/blocker/gating paths, Institutional Code R&D API path, Research pack happy/blocker/gating paths, API happy/error/isolation paths, and static UI serving.

The default multi-model assignment is still mocked: current workflow packs use `provider="mock"` unless an agent is explicitly reconfigured through server-side routing. OpenAI, DeepSeek, and LiteLLM Proxy adapters can make real calls only when their provider key is set, `TEAM_AGENT_ALLOW_REAL_MODEL_CALLS=1` is set, and an agent explicitly opts into that provider. Tavily web search/fetch can make real network calls only when `TEAM_AGENT_ALLOW_REAL_WEB_SEARCH=1`, `TEAM_AGENT_WEB_SEARCH_PROVIDER=tavily`, and `TAVILY_API_KEY` are set. Browser search/fetch can make real browser calls only when `TEAM_AGENT_ALLOW_BROWSER_ACCESS=1`, `TEAM_AGENT_BROWSER_PROVIDER=edge|chrome|browser_cdp`, and a compatible local CDP proxy is available. API keys are read from server-side environment variables only; they are not entered in the browser task UI, obvious secret-like task content is rejected before SQLite storage, and keys are not written to trace/artifacts. The separate local desktop launcher can save keys to `.env.local`, which is ignored by Git. Anthropic and local model providers are still skeleton entries.

The runner can dispatch safe explicit-DAG batches in parallel only when the executor explicitly opts in and every ready step has non-conflicting ownership. The in-process `RunWorker` is a durable local execution mechanism, not a distributed queue or external engineering executor. Built-in runtime jobs still do not launch external ACP processes or maintain live background child sessions. Code R&D does not receive general web search by default; only Research uses the web tools.

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
- A step becomes a completed `AgentRun` checkpoint only after its eval gate and all outgoing handoffs are persisted. Recovery invalidates pre-fix completion markers missing structural eval or required handoff evidence, then creates a new attempt without deleting old artifacts or records.
- A persisted `waiting` run with an `approved` runtime job is requeued automatically on startup. Retrying a background approval reuses its active queue segment, and retrying an already-completed approval returns the persisted result without advancing a newer job.
- Retry artifact filenames gain an `attempt-N` suffix, so existing artifact files and records are never overwritten or deleted.
- A hard crash during an external model request can repeat that interrupted request after restart. Exactly-once external execution is not guaranteed.

Run locks store a heartbeat every five seconds. Stale-lock recovery prefers the latest valid timezone-aware heartbeat over the original acquisition time, so long-running work is not falsely reclaimed. Transient queue reads and queue-status writes are retried; unresolved wake-ups stay persisted and terminal run state repairs a stale non-terminal queue item on restart. Background trace failures remain isolated from worker execution.

Each `WorkflowStep` has a typed `context_policy`:

- `artifact_excerpt_chars`: total artifact excerpt character budget.
- `max_artifacts`: maximum completed-attempt artifact refs/texts retained.
- `max_upstream_handoffs`: maximum structured upstream handoffs retained.

Artifact excerpt budgets are configured by position from 2K to 24K characters. The global schema caps a step at 100K excerpt characters, 300K encoded bytes, 32 artifacts, and 32 handoffs. The final structured context is checked again before any model call. Artifact files are read through a bounded prefix API; artifacts and handoffs from incomplete attempts are excluded. Context trace events record retained/dropped counts and character totals, never artifact bodies. Task intake is independently bounded by character, byte, container-size, and nesting-depth limits before SQLite persistence. The active local `research-planner` route uses `max_tokens=1000`; every `deepseek-v4-pro` route uses at least `max_tokens=4096` because a real Research reader still reached `finish_reason=length` at 2048 tokens. Incomplete model responses fail closed instead of becoming checkpoints. Other GPT budgets are unchanged.

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

## LiteLLM Quick Start

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

The start script automatically prefers `.venv-litellm\Scripts\python.exe` for LiteLLM and falls back to the main `.venv` only when that dedicated environment is absent. It validates service-specific health and OpenAPI identity, watches newly started child processes, and fails closed when a port belongs to an unrelated service. This keeps the documented Quick Start working when the main application environment uses Python 3.14, which LiteLLM does not currently support in this project. To create the dedicated environment when needed:

```powershell
py -3.13 -m venv .venv-litellm
```

On Windows, you can also use the desktop launcher:

```powershell
.\scripts\create-desktop-shortcut.ps1
```

This creates `Team Agent Harness Launcher.lnk` on the desktop. The launcher can edit `.env.local`, save model settings, start/stop local services, open the Harness UI, open the project folder, open `.env.local`, and open the log folder. Stop actions revalidate the project venv command line, base Python executable, service entry point, and port before terminating a process; unrelated port owners are displayed but never stopped.

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
  "test_command": "python -m pytest -q"
}
```

Current safety behavior:

- The original repository is not modified.
- The executor copies the repository into `output/local_code_workspaces/<run_id>/<step_name>/repo`, so `prepare_patch` and `test_changes` use isolated workspace copies.
- Copying is rejected before `copytree` when the filtered repository exceeds 20,000 files or 500 MB.
- `.env`, secret-like files, symlinks, hard-linked files, Windows junctions and other reparse points, `.git`, `.venv`, `node_modules`, cache folders, generated output folders, binary files, and large files are skipped.
- `prepare_patch` produces a patch proposal artifact only; it does not apply the patch.
- `test_changes` runs only allowlisted test commands: `pytest ...` or `python -m pytest ...`. Both forms are forced through the Harness interpreter, and absolute, parent-directory, or pytest response-file targets outside the isolated workspace are rejected.
- Test commands run in the isolated workspace copy with secret-like environment variables removed.

## Explicit Writeback

Generated code is still not written back by normal runtime-job approval. `POST /runs/{run_id}/runtime-jobs/{job_id}/approve` only lets the waiting local executor step continue.

To apply a generated patch to the original repository, use the separate writeback API:

1. Run `code_rd_institutional` with `repository_path`, `focus_paths`, and `test_command`.
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
- Before writing, the executor applies the patch to an isolated workspace and runs the allowlisted `test_command`.
- The original file hash must still match the preview `base_hashes`; otherwise writeback returns a conflict and does not overwrite local edits.
- Approval is serialized in the supported single-process runtime. A repeated concurrent approval returns the persisted completed result instead of applying the patch twice.
- Multi-file source updates use atomic replacements and roll back already-written files in reverse order if a later write fails.
- The final `writeback_applied` audit trace is part of the success boundary. If that trace cannot be persisted, all just-written files are restored to their verified base contents; any rollback failure is reported with the affected file names.
- Before source replacement, version 2 journals persist verified base backups and transaction-owned temporary paths. Startup recovery runs before the worker starts, revalidates the journal against SQLite and the patch artifact, and hashes every target as one set: an exact success trace plus all-new hashes is retained, base/new mixed state is rolled back as a whole, and any unknown target bytes stop startup without deleting the journal or overwriting the external edit.
- Trace records file names and hashes, not API keys or full patch contents.

## Local Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install -e .[test]
```

If editable install dependency resolution is slow, install the minimal current test/runtime dependencies:

```powershell
.\.venv\Scripts\python -m pip install fastapi httpx pytest uvicorn
```

## Run Tests

```powershell
.\.venv\Scripts\python -m pytest -v
```

### Current Verification Record

- On 2026-07-28 the full backend suite passed with `563 passed, 5 skipped`; `python -m compileall -q app scripts`, `pip check`, JavaScript syntax, and all project PowerShell parse checks also passed.
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
  --input-json '{"repository_path":"E:\\your-project","focus_paths":["app","tests"],"test_command":"python -m pytest -q"}'
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

## API Surface

- `POST /tasks`
- `POST /task-intake/analyze`
- `GET /tasks`
- `GET /tasks/{task_id}`
- `POST /runs`
- `GET /runs`
- `GET /runs/{run_id}`
- `GET /runs/{run_id}/detail`
- `GET /runs/{run_id}/trace`
- `GET /runs/{run_id}/artifacts`
- `GET /runs/{run_id}/agent-runs`
- `GET /runs/{run_id}/handoffs`
- `GET /runs/{run_id}/eval-results`
- `GET /runs/{run_id}/runtime-sessions`
- `GET /runs/{run_id}/runtime-jobs`
- `GET /runs/{run_id}/queue-state`
- `GET /runs/{run_id}/lock-state`
- `POST /runs/{run_id}/runtime-jobs/{job_id}/approve`
- `POST /runs/{run_id}/runtime-jobs/{job_id}/reject`
- `POST /runs/{run_id}/runtime-jobs/{job_id}/cancel`
- `GET /artifacts/{artifact_id}`
- `GET /workflow-packs`
- `GET /workflow-packs/{pack_name}`
- `GET /model-providers`
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
