# Model Routing

This document describes the current model-selection contract for Team Agent
Harness. It is intentionally separate from the historical Workflow Pack design
records.

## Defaults

The defaults apply when a new run uses the generated team template or the
local routing file has not provided an explicit route:

| Position | Primary | Fallback |
| --- | --- | --- |
| Planner | LiteLLM `gpt5.6-sol` | official DeepSeek `deepseek-v4-flash` |
| Searcher | official DeepSeek `deepseek-v4-flash` | none |
| Reader | official DeepSeek `deepseek-v4-flash` | none |
| Verifier | official DeepSeek `deepseek-v4-flash` | none |
| Writer | LiteLLM `gpt5.6-sol` | official DeepSeek `deepseek-v4-flash` |
| Final Reviewer | LiteLLM `gpt5.6-sol` | official DeepSeek `deepseek-v4-flash` |

The Research Pack still calls its final review slot `Reviewer`; its default
route follows the Final Reviewer row above. The Institutional Code R&D Pack
uses the explicit `FinalReviewer` slot.

Every real-provider route defaults to `reasoning_effort="xhigh"`. The UI and
API accept `minimal`, `low`, `medium`, `high`, or `xhigh` when the selected
model supports the requested capability. GPT relay routes map `xhigh` to the
provider's highest standard transport value unless the explicit LiteLLM
passthrough flag is enabled; the configured value remains visible in trace
metadata.

## User Selection

The browser team editor supports a run-scoped selection for every fixed Pack
position. A user may choose:

- model family: `gpt` or `deepseek`;
- provider: `openai`, `deepseek`, or `litellm_proxy`;
- provider-specific model name;
- reasoning effort;
- up to four ordered fallbacks.

The selection cannot add positions, alter the DAG, widen tool permissions, raise
runtime limits, or change Skill bindings. On submission, the backend validates
the route against the active capability registry and freezes the effective
selection into the immutable execution plan.

LiteLLM aliases must have an exact capability attestation. Direct OpenAI model
names must begin with `gpt`; direct DeepSeek model names must begin with
`deepseek-`. Custom aliases can be registered through
`TEAM_AGENT_MODEL_CAPABILITIES_CONFIG` using the schema in
`team_agent_harness/backend/config/model-capabilities.example.json`.

## Configuration Boundary

The checked-in examples are safe templates:

- `team_agent_harness/backend/config/model-routing.litellm.example.json`
  contains the recommended role routes and fallback chain.
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
LiteLLM: http://127.0.0.1:4000/
```

Use these read-only checks before a real run:

```powershell
Invoke-WebRequest http://127.0.0.1:8014/providers/doctor
Invoke-WebRequest http://127.0.0.1:8014/models/litellm_proxy/gpt5.6-sol/capabilities
Invoke-WebRequest http://127.0.0.1:8014/models/deepseek/deepseek-v4-flash/capabilities
```

`/providers/doctor`, `/routes/explain`, and model capability endpoints do not
call a provider. Real model calls still require server-side enablement and
explicit run confirmation.
