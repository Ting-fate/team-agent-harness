# Model Routing

This document describes the current model-selection contract for Team Agent
Harness. It is intentionally separate from the historical Workflow Pack design
records.

## Defaults

The defaults apply when a new run uses the generated team template or the
local routing file has not provided an explicit route:

| Position | Primary | Fallback |
| --- | --- | --- |
| Planner | direct relay `gpt-5.6-sol` | official DeepSeek `deepseek-v4-flash` |
| Searcher | official DeepSeek `deepseek-v4-flash` | none |
| Reader | official DeepSeek `deepseek-v4-flash` | none |
| Verifier | official DeepSeek `deepseek-v4-flash` | none |
| Writer | direct relay `gpt-5.6-sol` | official DeepSeek `deepseek-v4-flash` |
| Final Reviewer | direct relay `gpt-5.6-sol` | official DeepSeek `deepseek-v4-flash` |

The Research Pack still calls its final review slot `Reviewer`; its default
route follows the Final Reviewer row above. The Institutional Code R&D Pack
uses the explicit `FinalReviewer` slot.

Every real-provider route defaults to `reasoning_effort="xhigh"`. The direct
relay maps it to `high` unless `TEAM_AGENT_GPT_RELAY_XHIGH_PASSTHROUGH=1`.
The UI and API accept `minimal`, `low`, `medium`, `high`, or `xhigh` when the
selected model supports the requested capability. LiteLLM keeps its separate
passthrough flag; the configured and transmitted values remain visible in
trace metadata.

## User Selection

The browser team editor supports a run-scoped selection for every fixed Pack
position. A user may choose:

- model family: `gpt` or `deepseek`;
- provider: `openai`, `gpt_relay`, `deepseek`, or `litellm_proxy`;
- provider-specific model name;
- reasoning effort;
- up to four ordered fallbacks.

The selection cannot add positions, alter the DAG, widen tool permissions, raise
runtime limits, or change Skill bindings. On submission, the backend validates
the route against the active capability registry and freezes the effective
selection into the immutable execution plan.

GPT relay and LiteLLM aliases must have an exact capability attestation. Direct OpenAI model
names must begin with `gpt`; direct DeepSeek model names must begin with
`deepseek-`. Custom aliases can be registered through
`TEAM_AGENT_MODEL_CAPABILITIES_CONFIG` using the schema in
`team_agent_harness/backend/config/model-capabilities.example.json`.

## Configuration Boundary

The checked-in examples are safe templates:

- `team_agent_harness/backend/config/model-routing.direct-relay.example.json`
  contains the default direct relay routes and DeepSeek fallback chain.
- `team_agent_harness/backend/config/model-routing.litellm.example.json`
  remains the advanced LiteLLM route example.
- `team_agent_harness/backend/config/litellm.config.example.yaml` contains the
  `gpt5.6-sol` and `deepseek-v4-flash` aliases while retaining old aliases for
  historical execution plans.
- `team_agent_harness/backend/config/model-routing.local.json` is ignored by
  Git and is the machine-local override. It may contain model choices, token
  budgets, temperatures, and role-file bindings, but never credentials.
- Provider credentials remain in `.env.local`, which is ignored and is never
  placed in SQLite, traces, artifacts, documentation, or commits.

The normal local endpoints are:

```text
Harness: http://127.0.0.1:8014/
LiteLLM advanced mode: http://127.0.0.1:4000/
```

Use these read-only checks before a real run:

```powershell
Invoke-WebRequest http://127.0.0.1:8014/providers/doctor
Invoke-WebRequest http://127.0.0.1:8014/models/gpt_relay/gpt-5.6-sol/capabilities
Invoke-WebRequest http://127.0.0.1:8014/models/deepseek/deepseek-v4-flash/capabilities
```

`/providers/doctor`, `/routes/explain`, and model capability endpoints do not
call a provider. Real model calls still require server-side enablement and
explicit run confirmation.

## Why A Relay Can Fail While Direct DeepSeek Works

The two paths are not equivalent:

- official DeepSeek uses the direct `deepseek` OpenAI-compatible adapter and the
  provider's own endpoint;
- GPT now uses `gpt_relay`, the relay's real model id `gpt-5.6-sol`, and the
  configured `OPENAI_API_BASE` without a local LiteLLM hop;
- direct mode sends the selected Chat Completions or Responses envelope with
  bounded output, reasoning, tools, and SDK timeout. Only advanced LiteLLM mode
  adds `x-litellm-timeout`. Both paths reject incomplete or non-standard
  response shapes instead of treating them as successful output. The shared
  model HTTP client disables redirects and environment proxy inheritance, so an
  upstream cannot replay a model request body to a second origin.

The legacy `gpt5.6-sol` name is a LiteLLM alias, not the relay's real model id.
In direct mode it is mapped in memory to `gpt-5.6-sol` for new plans only.
Therefore a relay can fail even when another multi-model client works if it
uses a different model alias, endpoint, streaming mode, retry policy, timeout,
or parameter set. The most common failure classes are alias/upstream mapping,
unsupported `reasoning_effort` or tool parameters, relay timeout/rate limiting,
and a response that is not standard Chat Completions JSON. The provider doctor
only proves local readiness; use the tiny provider smoke with explicit consent
to distinguish readiness from a real network failure. The run trace records the
provider attempt, error class, timeout boundary, and route receipt without
storing credentials.

Fallback does not hide local configuration failures: a missing/unconfigured
primary provider, invalid local request, authentication error, or permission
error stops immediately. Explicit `408`, `429`, any `5xx`, transport failure,
and invalid/incomplete remote responses may use the frozen approved fallback.
Agent Loop requests retain their own remaining token/cost cap instead of being
raised to the normal DeepSeek minimum output allowance.

## Codex Plan Delegation

The restricted stdio MCP adapter exposes `harness_delegate_plan`. Codex sends a
title, goal, Pack name, and plan text; the adapter stores a redacted plan hash
and source marker in task inputs, validates every Pack slot, and selects only:

```text
provider: deepseek
model: deepseek-v4-flash
reasoning_effort: xhigh
fallbacks: []
```

With `start_run=false`, Codex receives a task and immutable plan hash before
execution. With `start_run=true`, the MCP process must be registered with
`TEAM_AGENT_CODEX_ALLOW_REAL_MODELS=1` and the call must set
`confirm_real_models=true`; the run is submitted to the durable background
worker. Codex then polls `harness_get_run`, `harness_get_quality`, and
`harness_get_final_artifact`. No GPT fallback, shell access, writeback, Git
operation, or credential operation is exposed by this delegation path.

Non-smoke DeepSeek requests receive at least 8,000 output tokens because the
provider may spend a large share of the output budget on reasoning; this stays
inside the frozen per-step 32,000 total-token ceiling. Provider smoke requests
keep their explicit small `max_tokens` value.
