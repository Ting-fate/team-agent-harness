# Team Agent Harness

Local Windows application for durable, observable multi-agent workflows. It
supports mocked execution by default and optional GPT-family plus DeepSeek
routing through an explicitly configured LiteLLM proxy.

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
- Provides reproducible Single-Agent versus Multi-Agent benchmark reporting
  without treating missing usage or possibly billed interrupted calls as free.
- Keeps mock execution as the default. Real model, web, browser, host-test, and
  source-write actions remain separate explicit opt-ins.

The supported runtime is intentionally local and single-process. It does not
start external Codex/ACP child processes, provide distributed workers, or
guarantee exactly-once execution for a model request interrupted by a hard
process crash.

## Windows Download And Start

No server deployment is required. After you have access to this repository:

1. On GitHub, choose **Code > Download ZIP**.
2. Extract the entire ZIP to a normal folder. Do not run files inside the ZIP preview.
3. Double-click `Start-Team-Agent-Harness.cmd` in the extracted folder.
4. The first run prepares project-local Python environments and creates
   `Team Agent Harness Launcher` on your desktop. Later runs reuse them.
5. In the launcher, enter a local LiteLLM key beginning with `sk-`, your GPT
   relay key and `/v1` base URL, and your DeepSeek API key. Choose **Save**, then
   **Start Services**, then **Open UI**.

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
