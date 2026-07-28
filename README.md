# Team Agent Harness

Local FastAPI harness for durable, observable multi-agent workflows. It supports
mocked execution by default and optional GPT-family plus DeepSeek routing through
an explicitly configured LiteLLM proxy.

The runnable project lives in [`team_agent_harness/backend`](team_agent_harness/backend/README.md).
Shared design records live in [`docs/superpowers/specs`](docs/superpowers/specs/).

## Quick Start

Run these commands from PowerShell:

```powershell
Set-Location .\team_agent_harness\backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
.\scripts\start-litellm-harness.ps1
```

The normal local endpoints are:

- Harness: `http://127.0.0.1:8014/`
- LiteLLM: `http://127.0.0.1:4000/`

Copy `.env.local.example` to `.env.local` only when real provider routing is
needed. Credentials, machine-local routing, SQLite state, run artifacts,
browser profiles, logs, and virtual environments are intentionally excluded
from version control.

See the [backend README](team_agent_harness/backend/README.md) for architecture,
provider opt-in, safety boundaries, operator commands, and recovery behavior.
