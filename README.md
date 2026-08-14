# Team Agent Harness

Local Windows control plane for durable, observable multi-agent workflows. It
keeps mocked execution as the compatibility default and supports run-scoped,
operator-selected GPT plus DeepSeek teams through direct providers or an
explicitly configured LiteLLM proxy.

The runnable project lives in [`team_agent_harness/backend`](team_agent_harness/backend/README.md).
Shared design records live in [`docs/superpowers/specs`](docs/superpowers/specs/).

## Current Capabilities

- Persists tasks, immutable execution plans, run checkpoints, handoffs,
  artifacts, eval results, trace events, queue state, and locks in a local
  FastAPI + SQLite service.
- Supports opt-in bounded Agent Loops with typed tool calls, explicit
  side-effect approval, provider/model route receipts, fallback control, and
  time, token, tool-call, repetition, and conservative estimated-cost budgets.
- Validates dynamic plans against the selected Workflow Pack, executes only
  ownership-safe DAG branches in parallel, and evaluates real artifact hashes,
  acceptance criteria, blocker checks, and final-artifact lineage.
- Lets the operator select a role card, GPT/DeepSeek model family, provider,
  model, reasoning effort, and bounded fallbacks for every fixed Pack position.
  The Pack DAG, tools, runtime limits, and position ownership cannot be expanded
  by that selection, and the effective team is frozen into the run plan.
- Provides reproducible Single-Agent versus Multi-Agent benchmark reporting
  without treating missing usage or possibly billed interrupted calls as free.
- Exposes a restricted local stdio MCP adapter so Codex can create tasks,
  validate teams, start background runs, and inspect redacted results without
  receiving approval, writeback, shell, Git, configuration, or credential tools.
- Keeps mock execution as the default. Real model, web, browser, host-test, and
  source-write actions remain separate explicit opt-ins.

The supported runtime is intentionally local and single-process. It does not
start external Codex/ACP child processes, provide distributed workers, or
guarantee exactly-once execution for a model request interrupted by a hard
process crash.

## Repository And License Boundary

This repository is maintained as Ting's private personal project. No
open-source license is granted by the absence of a `LICENSE` file; access does
not by itself grant permission to copy, redistribute, or sublicense the code.
Collaborators can still be invited through GitHub's private-repository access
controls.

The project does not copy or integrate the AGPL-licensed RyensX/OpenCodex
remote UI. Remote public hosting and a browser/PWA bridge remain outside the
supported scope, preserving the option to choose a separate license or
commercial model later.

## Windows Download And Start

No server deployment is required. After you have access to this repository:

1. On GitHub, choose **Code > Download ZIP**.
2. Extract the entire ZIP to a normal folder. Do not run files inside the ZIP preview.
3. Double-click `Start-Team-Agent-Harness.cmd` in the extracted folder.
4. The first run prepares project-local Python environments and creates
   `Team Agent Harness Launcher` on your desktop. Later runs reuse them.
5. In the launcher, enter a local LiteLLM key beginning with `sk-`, your GPT
   relay key and `/v1` base URL, and your DeepSeek API key. Choose **Save**, then
   **Start Services**. The launcher opens the UI only after both worker health
   and the actual HTML workspace are ready; **Open UI** remains disabled before
   that point.

The first run needs an internet connection to download Python packages. Python
3.12 or 3.13 is required for LiteLLM. If neither version is installed, setup
offers to install Python 3.13 for the current Windows user through `winget`; it
does not silently install global software. Provider credentials stay in the
ignored local file `team_agent_harness/backend/.env.local` and are never added
to the repository.

Normal local endpoints are:

- Harness: `http://127.0.0.1:8014/`
- LiteLLM: `http://127.0.0.1:4000/`

If a project-local environment becomes incomplete, rebuild only those ignored
environments from PowerShell:

```powershell
.\team_agent_harness\backend\scripts\setup-desktop.ps1 -Repair
```

## Codex MCP Control

The backend includes `scripts/codex_harness_mcp.py`, a loopback-only stdio MCP
adapter. Registering it is an explicit Codex configuration change, so setup
does not do that automatically. From `team_agent_harness/backend`, inspect the
installed command contract with `codex mcp add --help`, then register the local
Python and adapter paths. Real-model and real-web confirmations remain disabled
unless the MCP process is separately granted
`TEAM_AGENT_CODEX_ALLOW_REAL_MODELS=1` or
`TEAM_AGENT_CODEX_ALLOW_REAL_WEB=1`. See the backend README for the exact
command and tool boundary.

## Verification

The 2026-08-14 release candidate passed the full local backend suite with
`1314 passed, 5 skipped, 1 warning`, plus compile, dependency, JavaScript,
PowerShell, YAML/JSON, whitespace, and high-confidence credential checks. A
fresh temporary-data Uvicorn service also completed a six-step mock Research
run through the real stdio MCP process: all 12 tools were present, the quality
report passed 28 checks, the final artifact was hash-bound and readable, and no
error trace remained. The MCP adapter now enforces one monotonic wall-clock
deadline across every HTTP call in a tool invocation and bounds cached Run
bindings to a 128-entry LRU. Launcher rollback restores a replaceable prior
Harness, credential files use same-directory atomic replacement, work-state
inspection fails closed after 20 Runs, and both Run listboxes implement roving
keyboard navigation. No paid model was called. The normal Harness and LiteLLM
services then passed live health checks on ports `8014` and `4000`. A fresh
post-fix isolated Chrome pass covered `1440`, `390`, and `320` pixel layouts,
found no page-level horizontal overflow, application request failure, console
warning, or console error, and exercised task selection with Arrow/Enter plus
Run selection with End/Space. Narrow navigation and detail-tab rows use bounded
horizontal scrolling. These are local worktree results; remote GitHub Actions
status is reported separately after publication.

## Developer Setup

The desktop bootstrap handles this automatically. For a manual development
environment, run:

```powershell
Set-Location .\team_agent_harness\backend
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
.\scripts\start-litellm-harness.ps1
```

See the [backend README](team_agent_harness/backend/README.md) for architecture,
provider opt-in, safety boundaries, operator commands, and recovery behavior.
