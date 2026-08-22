from __future__ import annotations

import importlib.util
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "codex_harness_mcp.py"
VALID_PLAN_HASH = "a" * 64
VALID_PLAN_HASH_BYTES = VALID_PLAN_HASH.encode("ascii")
VALID_TIMESTAMP = "2026-08-14T12:00:00Z"


def load_script_module():
    spec = importlib.util.spec_from_file_location("codex_harness_mcp", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, responses: dict[tuple[str, str], object] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, str, object | None]] = []

    def get(self, path: str):
        self.calls.append(("GET", path, None))
        response = self.responses[("GET", path)]
        if isinstance(response, Exception):
            raise response
        return response

    def post(self, path: str, payload: object):
        self.calls.append(("POST", path, payload))
        response = self.responses[("POST", path)]
        if isinstance(response, Exception):
            raise response
        return response


class LocalApiClient:
    def __init__(self, client: TestClient, error_type: type[Exception]) -> None:
        self.client = client
        self.error_type = error_type

    def get(self, path: str):
        response = self.client.get(path)
        if response.status_code >= 400:
            raise self.error_type(f"Harness API returned HTTP {response.status_code}.")
        return response.json()

    def post(self, path: str, payload: object):
        response = self.client.post(path, json=payload)
        if response.status_code >= 400:
            raise self.error_type(f"Harness API returned HTTP {response.status_code}.")
        return response.json()


class HttpErrorOpener:
    def __init__(self, module, status_code: int, body: bytes) -> None:
        self.module = module
        self.status_code = status_code
        self.body = io.BytesIO(body)

    def open(self, request, timeout: float):
        raise self.module.HTTPError(
            request.full_url,
            self.status_code,
            "unsafe upstream text",
            hdrs=None,
            fp=self.body,
        )


class StubHttpResponse:
    def __init__(self, status: int, body: bytes, content_type: str | None) -> None:
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = {} if content_type is None else {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.body.close()

    def read(self, limit: int) -> bytes:
        return self.body.read(limit)


class StubResponseOpener:
    def __init__(self, status: int, body: bytes, content_type: str | None = "application/json") -> None:
        self.response = StubHttpResponse(status, body, content_type)

    def open(self, request, timeout: float):
        return self.response


class FakeMonotonic:
    def __init__(self) -> None:
        self.current = 0.0

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


class RecordingSocket:
    def __init__(self) -> None:
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)


class TimedDripResponse:
    def __init__(self, clock: FakeMonotonic, body: bytes, delay: float) -> None:
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self.clock = clock
        self.body = body
        self.delay = delay
        self.offset = 0
        self.socket = RecordingSocket()
        self.fp = type("FakeFp", (), {})()
        self.fp.raw = type("FakeRaw", (), {"_sock": self.socket})()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read1(self, limit: int) -> bytes:
        assert limit > 0
        self.clock.advance(self.delay)
        if self.offset >= len(self.body):
            return b""
        chunk = self.body[self.offset : self.offset + 1]
        self.offset += len(chunk)
        return chunk


def initialize_server(module, client: FakeClient, *, environ: dict[str, str] | None = None):
    server = module.McpServer(client, environ={} if environ is None else environ)
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "codex", "version": "0.146.1"},
            },
        }
    )
    assert response is not None
    assert response["result"]["protocolVersion"] == "2025-11-25"
    return server


def call_tool(server, request_id: int, name: str, arguments: dict[str, object]):
    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None
    return response["result"]


def tool_payload(result: dict[str, object]):
    content = result["content"]
    assert isinstance(content, list)
    return json.loads(content[0]["text"])


def sample_team_selection() -> dict[str, object]:
    return {
        "version": "team-selection-v1",
        "pack_name": "research",
        "assignments": [
            {
                "slot": "planner",
                "role_card_id": "research-planner",
                "route": {
                    "family": "gpt",
                    "provider": "litellm_proxy",
                    "model": "gpt5.5",
                    "reasoning_effort": "xhigh",
                    "fallbacks": [
                        {
                            "family": "deepseek",
                            "provider": "deepseek",
                            "model": "deepseek-v4-pro",
                        }
                    ],
                },
            }
        ],
    }


def sample_team_receipt() -> dict[str, object]:
    return {
        "version": "team-selection-v1",
        "pack_name": "research",
        "assignments": [
            {
                "slot": "Reader",
                "agent_id": "reader",
                "model_family": "gpt",
                "provider": "openai",
                "model": "gpt5.5",
                "reasoning_effort": "xhigh",
                "fallbacks": [],
            }
        ],
    }


def sample_team_template_response() -> dict[str, object]:
    return {
        "team_selection": {
            "version": "team-selection-v1",
            "pack_name": "research",
            "assignments": [
                {
                    "slot": "Reader",
                    "role_card_id": "research-reader",
                    "route": {
                        "family": "gpt",
                        "provider": "openai",
                        "model": "gpt5.5",
                        "reasoning_effort": "xhigh",
                        "fallbacks": [],
                    },
                }
            ],
        },
        "slots": [
            {
                "slot": "Reader",
                "agent_id": "reader",
                "tool_permissions": ["web.search"],
                "runtime_limits": {"max_steps": 4, "timeout_seconds": 30.0},
            }
        ],
        "role_cards": [{"id": "research-reader"}],
        "configuration_warnings": [],
    }


def sample_team_validation_response() -> dict[str, object]:
    return {
        "valid": True,
        "team_selection": {
            "version": "team-selection-v1",
            "pack_name": "research",
            "assignments": [
                {
                    "slot": "planner",
                    "agent_id": "research-planner",
                    "role_card_id": "research-planner",
                    "model_family": "gpt",
                    "provider": "litellm_proxy",
                    "model": "gpt5.5",
                    "reasoning_effort": "xhigh",
                    "fallbacks": [
                        {
                            "model_family": "deepseek",
                            "provider": "deepseek",
                            "model": "deepseek-v4-pro",
                        }
                    ],
                }
            ],
        },
        "public_execution_plan_hash": VALID_PLAN_HASH,
        "immutable_after_run_creation": True,
    }


def sample_run_response(
    *,
    run_id: str = "run-1",
    task_id: str = "task-1",
    status: str = "queued",
    confirm_real_models: bool = False,
    confirm_real_web: bool = False,
    plan_hash: str | None = VALID_PLAN_HASH,
    final_artifact_id: str | None = None,
    confirmed_real_web_tools: tuple[str, ...] | None = (),
    confirmed_real_web_tool_routes: tuple[tuple[str, str], ...] | None = (),
) -> dict[str, object]:
    return {
        "id": run_id,
        "task_id": task_id,
        "status": status,
        "real_model_access_confirmed": confirm_real_models,
        "real_web_access_confirmed": confirm_real_web,
        "confirmed_real_web_tools": (
            list(confirmed_real_web_tools)
            if confirmed_real_web_tools is not None
            else None
        ),
        "confirmed_real_web_tool_routes": (
            [
                {"name": name, "provider": provider}
                for name, provider in confirmed_real_web_tool_routes
            ]
            if confirmed_real_web_tool_routes is not None
            else None
        ),
        "execution_plan_hash": plan_hash,
        "final_artifact_id": final_artifact_id,
    }


def sample_recent_task_response(
    *,
    task_id: str = "task-1",
    title: str = "Recent task",
) -> dict[str, object]:
    return {
        "id": task_id,
        "title": title,
        "goal": "DO_NOT_RETURN_GOAL",
        "workflow_pack": "research",
        "inputs": {"private": "DO_NOT_RETURN_INPUTS"},
        "constraints": ["DO_NOT_RETURN_CONSTRAINTS"],
        "acceptance_criteria": [],
        "created_by": "system",
        "created_at": VALID_TIMESTAMP,
    }


def sample_recent_run_response(
    *,
    run_id: str = "run-1",
    task_id: str = "task-1",
    status: str = "queued",
    final_artifact_id: str | None = None,
) -> dict[str, object]:
    return {
        **sample_run_response(
            run_id=run_id,
            task_id=task_id,
            status=status,
            final_artifact_id=final_artifact_id,
        ),
        "current_step": None,
        "started_at": None,
        "finished_at": None,
        "internal_state": "DO_NOT_RETURN_INTERNAL_STATE",
    }


def sample_artifact_response(
    *,
    artifact_id: str = "artifact-1",
    run_id: str = "run-1",
    content: str = "Final artifact content.",
    validation_status: str = "pass",
) -> dict[str, object]:
    return {
        "artifact": {
            "id": artifact_id,
            "run_id": run_id,
            "agent_run_id": "agent-run-1",
            "type": "final_report",
            "path": "artifacts/final-report.md",
            "content_hash": sha256(content.encode("utf-8")).hexdigest(),
            "source_refs": ["https://example.com/source"],
            "validation_status": validation_status,
            "created_at": VALID_TIMESTAMP,
        },
        "content": content,
    }


def sample_run_team_response(
    *,
    run_id: str = "run-1",
    team_selection: dict[str, object] | None = None,
    plan_hash: str | None = VALID_PLAN_HASH,
    immutable: bool = True,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "team_selection": team_selection,
        "execution_plan_hash": plan_hash,
        "immutable": immutable,
    }


def sample_quality_criteria() -> dict[str, object]:
    return {
        "required_artifact_types": ["final_report"],
        "required_step_artifacts": {"solve": "final_report"},
        "required_eval_checks": ["solve:acceptance:nonempty-final"],
        "final_artifact_type": "final_report",
        "pack_eval_step_name": "solve",
        "min_final_artifact_chars": 1,
        "require_completed_run": True,
        "verify_artifact_hashes": True,
    }


def sample_quality_metrics() -> dict[str, object]:
    return {
        "model_calls": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "usage_complete": True,
        "unmetered_model_calls": 0,
        "duration_seconds": None,
    }


def sample_quality_check_names() -> list[str]:
    return [
        "run_completed",
        "artifact:final_report",
        "artifact:solve:final_report",
        "eval:solve:acceptance:nonempty-final",
        "artifact_hashes",
        "final_artifact_run",
        "final_artifact_latest_completed_attempt",
        "final_artifact_type",
        "final_artifact_content",
    ]


def sample_passing_quality_response(
    *,
    run_id: str = "run-1",
    plan_hash: str | None = VALID_PLAN_HASH,
) -> dict[str, object]:
    return {
        "run_id": run_id,
        "passed": True,
        "checks": [
            {"name": name, "status": "pass", "message": "passed"}
            for name in sample_quality_check_names()
        ],
        "metrics": sample_quality_metrics(),
        "criteria": sample_quality_criteria(),
        "execution_plan_hash": plan_hash,
    }


def sample_failing_quality_response(
    *,
    run_id: str = "run-1",
    failed_check: str = "run_completed",
    plan_hash: str | None = VALID_PLAN_HASH,
) -> dict[str, object]:
    check_names = sample_quality_check_names()
    assert failed_check in check_names
    return {
        "run_id": run_id,
        "passed": False,
        "checks": [
            {
                "name": name,
                "status": "fail" if name == failed_check else "pass",
                "message": "failed" if name == failed_check else "passed",
            }
            for name in check_names
        ],
        "metrics": sample_quality_metrics(),
        "criteria": sample_quality_criteria(),
        "execution_plan_hash": plan_hash,
    }


def sample_run_detail_response(
    *,
    run: dict[str, object] | None = None,
) -> dict[str, object]:
    run_response = sample_run_response() if run is None else run
    return {
        "run": run_response,
        "task": {
            "id": run_response["task_id"],
            "title": "Task",
            "goal": "Inspect the run.",
            "workflow_pack": "research",
        },
        "agent_runs": [],
        "handoffs": [],
        "trace": [],
        "artifacts": [],
        "eval_results": [],
    }


def bound_default_run_server(module):
    client = FakeClient(
        {
            ("POST", "/runs"): sample_run_response(),
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
        }
    )
    server = initialize_server(module, client)
    started = call_tool(server, 2, "harness_start_run", {"task_id": "task-1"})
    assert started["isError"] is False
    client.calls.clear()
    return server, client


def bound_real_web_run_server(module):
    run_response = sample_run_response(
        confirm_real_web=True,
        confirmed_real_web_tools=("web_search",),
        confirmed_real_web_tool_routes=(("web_search", "tavily"),),
    )
    client = FakeClient(
        {
            ("POST", "/runs"): run_response,
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
        }
    )
    server = initialize_server(
        module,
        client,
        environ={"TEAM_AGENT_CODEX_ALLOW_REAL_WEB": "1"},
    )
    started = call_tool(
        server,
        2,
        "harness_start_run",
        {"task_id": "task-1", "confirm_real_web": True},
    )
    assert started["isError"] is False
    client.calls.clear()
    return server, client


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8014",
        "http://127.0.0.2:8014/",
        "http://localhost:8014",
        "http://[::1]:8014/",
    ],
)
def test_base_url_accepts_only_loopback_http_origins(url: str) -> None:
    module = load_script_module()

    assert module.validate_base_url(url).endswith("/")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8014",
        "http://example.com:8014",
        "http://127.0.0.1",
        "http://user:pass@127.0.0.1:8014",
        "http://127.0.0.1:8014/api",
        "http://127.0.0.1:8014?token=x",
        "http://127.0.0.1:8014/#fragment",
        "http://127.0.0.1:not-a-port",
    ],
)
def test_base_url_rejects_non_loopback_or_ambiguous_values(url: str) -> None:
    module = load_script_module()

    with pytest.raises(module.HarnessMcpError):
        module.validate_base_url(url)


@pytest.mark.parametrize("status_code", [404, 409])
def test_http_error_status_is_stable_and_body_is_never_reflected(status_code: int) -> None:
    module = load_script_module()
    opener = HttpErrorOpener(module, status_code, b'{"detail":"api_key=topsecret"}')
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = opener

    with pytest.raises(module.HarnessApiError) as error:
        client.get("/runs/run-1/team")

    assert str(error.value) == f"Harness API returned HTTP {status_code}."
    assert "topsecret" not in str(error.value)
    assert opener.body.closed is True


def test_http_client_enforces_wall_clock_deadline_against_slow_drip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script_module()
    clock = FakeMonotonic()
    response = TimedDripResponse(
        clock,
        b'{"status":"ok","worker":"running"}',
        delay=0.26,
    )

    class Opener:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def open(self, request, timeout: float):
            self.timeouts.append(timeout)
            return response

    opener = Opener()
    monkeypatch.setattr(module, "monotonic", clock)
    client = module.HarnessApiClient("http://127.0.0.1:8014", timeout_seconds=1)
    client._opener = opener

    with pytest.raises(module.HarnessApiError, match="Cannot reach"):
        client.get("/health")

    assert opener.timeouts == [pytest.approx(1.0)]
    assert len(response.socket.timeouts) == 4
    assert response.socket.timeouts == pytest.approx([1.0, 0.74, 0.48, 0.22])
    assert response.offset == 4
    assert clock.current == pytest.approx(1.04)


def test_composite_tool_http_calls_share_one_wall_clock_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = load_script_module()
    clock = FakeMonotonic()

    class AdvancingOpener:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def open(self, request, timeout: float):
            self.timeouts.append(timeout)
            clock.advance(0.34)
            return StubHttpResponse(200, b"[]", "application/json")

    opener = AdvancingOpener()
    monkeypatch.setattr(module, "monotonic", clock)
    client = module.HarnessApiClient("http://127.0.0.1:8014", timeout_seconds=1)
    client._opener = opener
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_list_catalog", {})

    assert result["isError"] is True
    assert opener.timeouts == pytest.approx([1.0, 0.66, 0.32])
    assert clock.current == pytest.approx(1.02)


@pytest.mark.parametrize(
    ("method", "path", "status", "body", "expected_type"),
    [
        ("GET", "/health", 200, b'{"status":"ok","worker":"running"}', dict),
        ("GET", "/workflow-packs", 200, b"[]", list),
        ("GET", "/agents", 200, b"[]", list),
        ("GET", "/model-providers", 200, b"[]", list),
        ("GET", "/tool-providers", 200, b"[]", list),
        (
            "GET",
            "/tasks?limit=50&offset=0",
            200,
            json.dumps([sample_recent_task_response()]).encode("utf-8"),
            list,
        ),
        (
            "GET",
            "/runs?limit=50&offset=0",
            200,
            json.dumps([sample_recent_run_response()]).encode("utf-8"),
            list,
        ),
        (
            "GET",
            "/workflow-packs/research/team-template",
            200,
            b'{"team_selection":{"version":"team-selection-v1","pack_name":"research",'
            b'"assignments":[{"slot":"Reader","route":{"family":"gpt","provider":"openai",'
            b'"model":"gpt5.5","reasoning_effort":"xhigh","fallbacks":[]}}]},'
            b'"slots":[{"slot":"Reader","agent_id":"reader","tool_permissions":[],'
            b'"runtime_limits":{}}],"role_cards":[],"configuration_warnings":[]}',
            dict,
        ),
        ("POST", "/tasks", 201, b'{"id":"task-1","title":"t","goal":"g","workflow_pack":"research"}', dict),
        (
            "POST",
            "/team-selections/validate",
            200,
            json.dumps(sample_team_validation_response()).encode("utf-8"),
            dict,
        ),
        (
            "POST",
            "/runs",
            201,
            json.dumps(sample_run_response()).encode("utf-8"),
            dict,
        ),
        (
            "GET",
            "/runs/run-1",
            200,
            json.dumps(sample_run_response()).encode("utf-8"),
            dict,
        ),
        (
            "GET",
            "/runs/run-1/detail",
            200,
            b'{"run":{"id":"run-1","task_id":"task-1","status":"queued"},'
            b'"task":{"id":"task-1","title":"t","goal":"g","workflow_pack":"research"},'
            b'"agent_runs":[],"handoffs":[],"trace":[],"artifacts":[],"eval_results":[]}',
            dict,
        ),
        (
            "GET",
            "/runs/run-1/team",
            200,
            b'{"run_id":"run-1","team_selection":null,"execution_plan_hash":"'
            + VALID_PLAN_HASH_BYTES
            + b'","immutable":true}',
            dict,
        ),
        (
            "GET",
            "/runs/run-1/quality",
            200,
            json.dumps(sample_passing_quality_response()).encode("utf-8"),
            dict,
        ),
        (
            "GET",
            "/artifacts/artifact-1",
            200,
            json.dumps(sample_artifact_response()).encode("utf-8"),
            dict,
        ),
    ],
)
def test_http_contracts_cover_every_whitelisted_operation(
    method: str,
    path: str,
    status: int,
    body: bytes,
    expected_type: type,
) -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(status, body, "application/json; charset=utf-8")

    request_payload = {
        "/tasks": {"title": "t", "goal": "g", "workflow_pack": "research"},
        "/team-selections/validate": sample_team_selection(),
        "/runs": {"task_id": "task-1"},
    }.get(path)
    result = client._request(method, path, request_payload)

    assert type(result) is expected_type


@pytest.mark.parametrize(
    ("method", "path", "status", "request_payload", "response_payload"),
    [
        (
            "POST",
            "/tasks",
            201,
            {"title": "t", "goal": "g", "workflow_pack": "research"},
            {"id": "task-1", "title": "other", "goal": "g", "workflow_pack": "research"},
        ),
        (
            "POST",
            "/tasks",
            201,
            {"title": "t", "goal": "g", "workflow_pack": "research"},
            {"id": "task-1", "title": "t", "goal": "other", "workflow_pack": "research"},
        ),
        (
            "POST",
            "/tasks",
            201,
            {"title": "t", "goal": "g", "workflow_pack": "research"},
            {"id": "task-1", "title": "t", "goal": "g", "workflow_pack": "code_rd"},
        ),
        (
            "GET",
            "/runs/run-1",
            200,
            None,
            sample_run_response(run_id="run-2"),
        ),
        (
            "POST",
            "/runs",
            201,
            {"task_id": "task-1"},
            sample_run_response(task_id="task-2"),
        ),
        (
            "GET",
            "/workflow-packs/research/team-template",
            200,
            None,
            {
                **sample_team_template_response(),
                "team_selection": {
                    **sample_team_template_response()["team_selection"],
                    "pack_name": "code_rd",
                },
            },
        ),
        (
            "GET",
            "/runs/run-1/team",
            200,
            None,
            {
                "run_id": "run-2",
                "team_selection": None,
                "execution_plan_hash": None,
                "immutable": False,
            },
        ),
        (
            "GET",
            "/runs/run-1/quality",
            200,
            None,
            sample_failing_quality_response(run_id="run-2"),
        ),
        (
            "GET",
            "/artifacts/artifact-1",
            200,
            None,
            sample_artifact_response(artifact_id="artifact-2"),
        ),
    ],
)
def test_http_contract_rejects_response_bound_to_different_request_object(
    method: str,
    path: str,
    status: int,
    request_payload: dict[str, object] | None,
    response_payload: dict[str, object],
) -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        status,
        json.dumps(response_payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="does not match the request"):
        client._request(method, path, request_payload)


def test_http_recent_projections_drop_task_and_run_private_fields() -> None:
    module = load_script_module()
    task = sample_recent_task_response()
    run = sample_recent_run_response()

    task_client = module.HarnessApiClient("http://127.0.0.1:8014")
    task_client._opener = StubResponseOpener(
        200,
        json.dumps([task]).encode("utf-8"),
        "application/json",
    )
    run_client = module.HarnessApiClient("http://127.0.0.1:8014")
    run_client._opener = StubResponseOpener(
        200,
        json.dumps([run]).encode("utf-8"),
        "application/json",
    )

    tasks = task_client.get("/tasks?limit=50&offset=0")
    runs = run_client.get("/runs?limit=50&offset=0")

    assert tasks == [
        {
            "id": "task-1",
            "title": "Recent task",
            "workflow_pack": "research",
            "created_at": VALID_TIMESTAMP,
        }
    ]
    assert set(runs[0]) == {
        "id",
        "task_id",
        "status",
        "current_step",
        "final_artifact_id",
        "started_at",
        "finished_at",
        "execution_plan_hash",
    }
    serialized = json.dumps({"tasks": tasks, "runs": runs})
    assert "DO_NOT_RETURN" not in serialized


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/tasks?limit=50&offset=0",
            [sample_recent_task_response(task_id=f"task-{index}") for index in range(51)],
        ),
        (
            "/runs?limit=50&offset=0",
            [sample_recent_run_response(run_id=f"run-{index}") for index in range(51)],
        ),
    ],
)
def test_http_recent_lists_reject_more_than_fixed_limit(
    path: str,
    payload: list[dict[str, object]],
) -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="recent-(task|run) limit"):
        client.get(path)


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/tasks?limit=50&offset=0",
            [{**sample_recent_task_response(), "created_at": "2026-08-14T12:00:00"}],
        ),
        (
            "/runs?limit=50&offset=0",
            [{**sample_recent_run_response(), "started_at": "not-a-datetime"}],
        ),
    ],
)
def test_http_recent_lists_require_timezone_aware_iso_datetimes(
    path: str,
    payload: list[dict[str, object]],
) -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="invalid field values"):
        client.get(path)


def test_http_artifact_projection_drops_storage_metadata() -> None:
    module = load_script_module()
    payload = sample_artifact_response()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    result = client.get("/artifacts/artifact-1")

    assert result == {
        "artifact": {
            "id": "artifact-1",
            "run_id": "run-1",
            "type": "final_report",
            "content_hash": payload["artifact"]["content_hash"],
            "validation_status": "pass",
        },
        "content": "Final artifact content.",
    }
    assert "path" not in json.dumps(result)
    assert "source_refs" not in json.dumps(result)
    assert "agent_run_id" not in json.dumps(result)


@pytest.mark.parametrize("case", ["missing_hash", "invalid_hash", "invalid_run_id"])
def test_http_artifact_rejects_incomplete_or_invalid_identity_fields(case: str) -> None:
    module = load_script_module()
    payload = sample_artifact_response()
    artifact = payload["artifact"]
    if case == "missing_hash":
        artifact.pop("content_hash")
        message = "missing required typed fields"
    elif case == "invalid_hash":
        artifact["content_hash"] = "A" * 64
        message = "invalid field values"
    else:
        artifact["run_id"] = "../run-1"
        message = "invalid field values"
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match=message):
        client.get("/artifacts/artifact-1")


def test_http_artifact_rejects_response_above_adapter_limit() -> None:
    module = load_script_module()
    payload = sample_artifact_response(content="x" * (module.MAX_RESPONSE_BYTES + 1))
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="exceeded the adapter limit"):
        client.get("/artifacts/artifact-1")


@pytest.mark.parametrize(
    "case",
    ["run_id", "task_id", "agent_runs", "handoffs", "trace", "artifacts", "eval_results"],
)
def test_http_run_detail_rejects_inconsistent_request_and_object_relations(case: str) -> None:
    module = load_script_module()
    payload = {
        "run": {"id": "run-1", "task_id": "task-1", "status": "running"},
        "task": {
            "id": "task-1",
            "title": "t",
            "goal": "g",
            "workflow_pack": "research",
        },
        "agent_runs": [
            {
                "id": "agent-run-1",
                "run_id": "run-1",
                "agent_id": "reader",
                "step_name": "read_sources",
                "status": "running",
            }
        ],
        "handoffs": [
            {
                "id": "handoff-1",
                "run_id": "run-1",
                "from_agent_run_id": "agent-run-1",
                "to_agent_id": "writer",
            }
        ],
        "trace": [
            {
                "id": "trace-1",
                "run_id": "run-1",
                "event_type": "workflow_event",
                "payload": {},
            }
        ],
        "artifacts": [
            {
                "id": "artifact-1",
                "run_id": "run-1",
                "agent_run_id": "agent-run-1",
                "type": "research_note",
                "path": "artifacts/research-note.md",
                "validation_status": "pass",
            }
        ],
        "eval_results": [
            {
                "id": "eval-1",
                "run_id": "run-1",
                "check_name": "source_quality",
                "status": "pass",
            }
        ],
    }
    if case == "run_id":
        payload["run"]["id"] = "run-2"
    elif case == "task_id":
        payload["task"]["id"] = "task-2"
    else:
        payload[case][0]["run_id"] = "run-2"
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="does not match the request"):
        client.get("/runs/run-1/detail")


@pytest.mark.parametrize(
    "case",
    [
        "pack_name",
        "slot",
        "role_card_id",
        "family",
        "provider",
        "model",
        "reasoning_effort",
        "fallback",
    ],
)
def test_http_team_validation_receipt_must_match_normalized_selection(case: str) -> None:
    module = load_script_module()
    selection = sample_team_selection()
    response = sample_team_validation_response()
    receipt = response["team_selection"]
    assignment = receipt["assignments"][0]
    if case == "pack_name":
        receipt["pack_name"] = "code_rd"
    elif case == "slot":
        assignment["slot"] = "writer"
    elif case == "role_card_id":
        assignment["role_card_id"] = "other-card"
    elif case == "family":
        assignment["model_family"] = "deepseek"
    elif case == "provider":
        assignment["provider"] = "openai"
    elif case == "model":
        assignment["model"] = "gpt-other"
    elif case == "reasoning_effort":
        assignment["reasoning_effort"] = "high"
    else:
        assignment["fallbacks"][0]["model"] = "deepseek-chat"
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(response).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="does not match the request"):
        client.post("/team-selections/validate", selection)


@pytest.mark.parametrize("reasoning_value", ["missing", None])
def test_http_team_validation_receipt_applies_default_reasoning_effort(
    reasoning_value: str | None,
) -> None:
    module = load_script_module()
    selection = sample_team_selection()
    route = selection["assignments"][0]["route"]
    if reasoning_value == "missing":
        route.pop("reasoning_effort")
    else:
        route["reasoning_effort"] = reasoning_value
    response = sample_team_validation_response()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(response).encode("utf-8"),
        "application/json",
    )

    result = client.post("/team-selections/validate", selection)

    assert result["team_selection"]["assignments"][0]["reasoning_effort"] == "xhigh"


@pytest.mark.parametrize(
    ("passed", "checks"),
    [
        (True, []),
        (True, [{"name": "gate", "status": "fail", "message": "failed"}]),
        (False, [{"name": "gate", "status": "pass", "message": "passed"}]),
    ],
)
def test_http_quality_rejects_empty_or_contradictory_checks(
    passed: bool,
    checks: list[dict[str, object]],
) -> None:
    module = load_script_module()
    payload = sample_passing_quality_response()
    payload["passed"] = passed
    payload["checks"] = checks
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="invalid field values"):
        client.get("/runs/run-1/quality")


@pytest.mark.parametrize(
    "missing_field",
    [
        "model_calls",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "usage_complete",
        "unmetered_model_calls",
        "duration_seconds",
    ],
)
def test_http_quality_requires_every_metric_field(missing_field: str) -> None:
    module = load_script_module()
    payload = sample_failing_quality_response()
    payload["metrics"].pop(missing_field)
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="missing required typed fields"):
        client.get("/runs/run-1/quality")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model_calls", "0"),
        ("tool_calls", "0"),
        ("input_tokens", "0"),
        ("output_tokens", "0"),
        ("total_tokens", "0"),
        ("unmetered_model_calls", "0"),
        ("usage_complete", 1),
        ("duration_seconds", "0"),
        ("model_calls", -1),
        ("tool_calls", -1),
        ("input_tokens", -1),
        ("output_tokens", -1),
        ("total_tokens", -1),
        ("unmetered_model_calls", -1),
        ("duration_seconds", -0.1),
    ],
)
def test_http_quality_rejects_invalid_metric_types_and_ranges(
    field: str,
    value: object,
) -> None:
    module = load_script_module()
    payload = sample_failing_quality_response()
    payload["metrics"][field] = value
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="invalid field (types|values)"):
        client.get("/runs/run-1/quality")


@pytest.mark.parametrize(
    "case",
    ["pack_name", "step_name", "model", "provider", "trace_action", "quality_name"],
)
def test_http_contract_rejects_prose_in_operational_identifier_fields(case: str) -> None:
    module = load_script_module()
    if case in {"pack_name", "step_name"}:
        path = "/workflow-packs"
        payload = [
            {
                "name": "hidden prompt body" if case == "pack_name" else "research",
                "agents": [],
                "steps": [
                    {
                        "name": "hidden prompt body" if case == "step_name" else "read_sources",
                        "agent_role": "Reader",
                    }
                ],
            }
        ]
    elif case in {"model", "provider"}:
        path = "/agents"
        payload = [
            {
                "id": "research-reader",
                "role": "Reader",
                "model_config": {
                    "provider": "hidden prompt body" if case == "provider" else "openai",
                    "model": "hidden prompt body" if case == "model" else "gpt5.5",
                },
            }
        ]
    elif case == "trace_action":
        path = "/runs/run-1/detail"
        payload = {
            "run": {"id": "run-1", "task_id": "task-1", "status": "running"},
            "task": {
                "id": "task-1",
                "title": "t",
                "goal": "g",
                "workflow_pack": "research",
            },
            "agent_runs": [],
            "handoffs": [],
            "trace": [
                {
                    "id": "trace-1",
                    "run_id": "run-1",
                    "event_type": "workflow_event",
                    "payload": {"action": "hidden prompt body"},
                }
            ],
            "artifacts": [],
            "eval_results": [],
        }
    else:
        path = "/runs/run-1/quality"
        payload = sample_failing_quality_response()
        payload["criteria"]["required_eval_checks"] = ["hidden prompt body"]
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="invalid field values"):
        client.get(path)


@pytest.mark.parametrize(
    ("method", "path", "status", "expected_status"),
    [
        ("GET", "/health", 204, 200),
        ("POST", "/tasks", 200, 201),
        ("POST", "/runs", 202, 201),
    ],
)
def test_http_contract_rejects_empty_204_and_wrong_success_statuses(
    method: str,
    path: str,
    status: int,
    expected_status: int,
) -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(status, b"", "application/json")

    with pytest.raises(module.HarnessApiError) as error:
        client._request(method, path, {} if method == "POST" else None)

    assert str(error.value) == f"Harness API returned HTTP {status}; expected {expected_status}."


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/problem+json"])
def test_http_contract_rejects_wrong_content_type(content_type: str | None) -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(200, b'{"status":"ok"}', content_type)

    with pytest.raises(module.HarnessApiError, match="unexpected Content-Type"):
        client.get("/health")


def test_http_contract_rejects_empty_body_and_non_utf8_json() -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(200, b" \r\n\t", "application/json")

    with pytest.raises(module.HarnessApiError, match="empty JSON body"):
        client.get("/health")

    gbk_body = json.dumps({"message": "中文"}, ensure_ascii=False).encode("gbk")
    client._opener = StubResponseOpener(200, gbk_body, "application/json; charset=gbk")
    with pytest.raises(module.HarnessApiError, match="invalid JSON"):
        client.get("/health")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/health", b"[]"),
        ("/workflow-packs", b"{}"),
    ],
)
def test_http_contract_rejects_wrong_top_level_structure(path: str, body: bytes) -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(200, body, "application/json")

    with pytest.raises(module.HarnessApiError, match="unexpected top-level JSON structure"):
        client.get(path)


@pytest.mark.parametrize(
    ("path", "body", "message"),
    [
        ("/health", b'{"status":"ok"}', "missing required fields"),
        ("/runs/run-1", b'{"id":"run-1"}', "missing required fields"),
        ("/workflow-packs", b'["research"]', "invalid list items"),
    ],
)
def test_http_contract_rejects_incomplete_objects_and_invalid_list_items(
    path: str,
    body: bytes,
    message: str,
) -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(200, body, "application/json")

    with pytest.raises(module.HarnessApiError, match=message):
        client.get(path)


@pytest.mark.parametrize(
    ("method", "path", "status", "body"),
    [
        ("GET", "/health", 200, b'{"status":true,"worker":"running"}'),
        ("GET", "/health", 200, b'{"status":"ok","worker":"stopped"}'),
        ("GET", "/health", 200, b'{"status":"ok","worker":true}'),
        ("GET", "/workflow-packs", 200, b'[{"name":1,"agents":[],"steps":[]}]'),
        ("GET", "/agents", 200, b'[{"id":"agent-1","role":1,"model_config":{}}]'),
        ("GET", "/model-providers", 200, b'[{"name":"mock","enabled":"yes","real_calls":false}]'),
        ("GET", "/tool-providers", 200, b'[{"name":"mock","enabled":true,"real_calls":0}]'),
        (
            "GET",
            "/tasks?limit=50&offset=0",
            200,
            json.dumps(
                [{**sample_recent_task_response(), "created_at": 1}]
            ).encode("utf-8"),
        ),
        (
            "GET",
            "/runs?limit=50&offset=0",
            200,
            json.dumps(
                [{**sample_recent_run_response(), "current_step": 1}]
            ).encode("utf-8"),
        ),
        (
            "GET",
            "/workflow-packs/research/team-template",
            200,
            b'{"team_selection":{},"slots":{},"role_cards":[],"configuration_warnings":[]}',
        ),
        ("POST", "/tasks", 201, b'{"id":"task-1","title":false,"goal":"g","workflow_pack":"research"}'),
        (
            "POST",
            "/team-selections/validate",
            200,
            b'{"valid":"yes","team_selection":{},"public_execution_plan_hash":"'
            + VALID_PLAN_HASH_BYTES
            + b'","immutable_after_run_creation":true}',
        ),
        (
            "POST",
            "/runs",
            201,
            json.dumps({**sample_run_response(), "status": "unknown"}).encode("utf-8"),
        ),
        (
            "GET",
            "/runs/run-1",
            200,
            json.dumps({**sample_run_response(), "status": 1}).encode("utf-8"),
        ),
        (
            "GET",
            "/runs/run-1/detail",
            200,
            b'{"run":{},"task":{},"agent_runs":[],"handoffs":[],"trace":{},"artifacts":[],"eval_results":[]}',
        ),
        (
            "GET",
            "/runs/run-1/team",
            200,
            b'{"run_id":"run-1","team_selection":null,"execution_plan_hash":"'
            + VALID_PLAN_HASH_BYTES
            + b'","immutable":"yes"}',
        ),
        (
            "GET",
            "/runs/run-1/quality",
            200,
            json.dumps(
                {**sample_passing_quality_response(), "passed": "yes"}
            ).encode("utf-8"),
        ),
        (
            "GET",
            "/artifacts/artifact-1",
            200,
            json.dumps(
                {**sample_artifact_response(), "content": 1}
            ).encode("utf-8"),
        ),
    ],
)
def test_http_contract_rejects_wrong_field_types_or_enums_for_every_operation(
    method: str,
    path: str,
    status: int,
    body: bytes,
) -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(status, body, "application/json")

    with pytest.raises(module.HarnessApiError, match="invalid field (types|values)"):
        client._request(method, path, {} if method == "POST" else None)


def test_http_agent_catalog_rejects_invalid_nested_model_config() -> None:
    module = load_script_module()
    body = b'[{"id":"agent-1","role":"Reader","model_config":{"provider":[],"model":"mock"}}]'
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(200, body, "application/json")

    with pytest.raises(module.HarnessApiError, match="invalid field types"):
        client.get("/agents")


@pytest.mark.parametrize(
    "field",
    ["agent_runs", "handoffs", "trace", "artifacts", "eval_results"],
)
def test_http_run_detail_rejects_incomplete_nested_items(field: str) -> None:
    module = load_script_module()
    payload = {
        "run": {"id": "run-1", "task_id": "task-1", "status": "queued"},
        "task": {
            "id": "task-1",
            "title": "t",
            "goal": "g",
            "workflow_pack": "research",
        },
        "agent_runs": [],
        "handoffs": [],
        "trace": [],
        "artifacts": [],
        "eval_results": [],
    }
    payload[field] = [{}]
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="missing required typed fields"):
        client.get("/runs/run-1/detail")


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/workflow-packs/research/team-template",
            b'{"team_selection":{"version":"other","pack_name":"research","assignments":[]},'
            b'"slots":[],"role_cards":[],"configuration_warnings":[]}',
        ),
        (
            "/workflow-packs/research/team-template",
            b'{"team_selection":{"version":"team-selection-v1","pack_name":"research",'
            b'"assignments":[{"slot":"Reader","route":{"family":"gpt","provider":"openai",'
            b'"model":"gpt5.5","fallbacks":[{"family":"deepseek","provider":"other",'
            b'"model":"deepseek-v4-pro"}]}}]},"slots":[],"role_cards":[],"configuration_warnings":[]}',
        ),
        (
            "/workflow-packs/research/team-template",
            b'{"team_selection":{"version":"team-selection-v1","pack_name":"research",'
            b'"assignments":[{"slot":"Reader","route":{"family":"deepseek","provider":"openai",'
            b'"model":"deepseek-chat","fallbacks":[]}}]},"slots":[],"role_cards":[],'
            b'"configuration_warnings":[]}',
        ),
        (
            "/runs/run-1/team",
            b'{"run_id":"run-1","team_selection":{"version":"team-selection-v1","pack_name":"research",'
            b'"assignments":[{"slot":"Reader","agent_id":"reader","model_family":"other",'
            b'"provider":"openai","model":"gpt5.5","reasoning_effort":"high","fallbacks":[]}]},'
            b'"execution_plan_hash":"'
            + VALID_PLAN_HASH_BYTES
            + b'","immutable":true}',
        ),
        (
            "/runs/run-1/team",
            b'{"run_id":"run-1","team_selection":{"version":"team-selection-v1","pack_name":"research",'
            b'"assignments":[{"slot":"Reader","agent_id":"reader","model_family":"deepseek",'
            b'"provider":"openai","model":"deepseek-chat","reasoning_effort":"high","fallbacks":[]}]},'
            b'"execution_plan_hash":"'
            + VALID_PLAN_HASH_BYTES
            + b'","immutable":true}',
        ),
        (
            "/runs/run-1/team",
            b'{"run_id":"run-1","team_selection":{"version":"team-selection-v1","pack_name":"research",'
            b'"assignments":[{"slot":"Reader","agent_id":"reader","model_family":"gpt",'
            b'"provider":"openai","model":"gpt5.5","reasoning_effort":"high",'
            b'"fallbacks":[{"model_family":"deepseek",'
            b'"provider":"openai","model":"deepseek-chat"}]}]},'
            b'"execution_plan_hash":"'
            + VALID_PLAN_HASH_BYTES
            + b'","immutable":true}',
        ),
        (
            "/runs/run-1/team",
            b'{"run_id":"run-1","team_selection":{"version":"team-selection-v1","pack_name":"research",'
            b'"assignments":[{"slot":"Reader","agent_id":"reader","model_family":"gpt",'
            b'"provider":"openai","model":"gpt5.5","reasoning_effort":"high",'
            b'"fallbacks":[{"model_family":"other",'
            b'"provider":"deepseek","model":"deepseek-v4-pro"}]}]},'
            b'"execution_plan_hash":"'
            + VALID_PLAN_HASH_BYTES
            + b'","immutable":true}',
        ),
        (
            "/runs/run-1/quality",
            json.dumps(
                {
                    **sample_passing_quality_response(),
                    "checks": [
                        {
                            **check,
                            "status": "warn" if index == 0 else check["status"],
                        }
                        for index, check in enumerate(
                            sample_passing_quality_response()["checks"]
                        )
                    ],
                }
            ).encode("utf-8"),
        ),
        (
            "/runs?limit=50&offset=0",
            json.dumps(
                [{**sample_recent_run_response(), "status": "unknown"}]
            ).encode("utf-8"),
        ),
        (
            "/artifacts/artifact-1",
            json.dumps(
                sample_artifact_response(validation_status="unknown")
            ).encode("utf-8"),
        ),
    ],
)
def test_http_contract_rejects_invalid_nested_enums(path: str, body: bytes) -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(200, body, "application/json")

    with pytest.raises(module.HarnessApiError, match="invalid field values"):
        client.get(path)


@pytest.mark.parametrize(
    ("include_field", "reasoning_effort", "message"),
    [
        (False, None, "missing required typed fields"),
        (True, [], "invalid field types"),
        (True, "   ", "invalid field values"),
    ],
)
def test_http_run_team_receipt_requires_nonempty_reasoning_effort(
    include_field: bool,
    reasoning_effort: object,
    message: str,
) -> None:
    module = load_script_module()
    assignment: dict[str, object] = {
        "slot": "Reader",
        "agent_id": "reader",
        "model_family": "gpt",
        "provider": "openai",
        "model": "gpt5.5",
        "fallbacks": [],
    }
    if include_field:
        assignment["reasoning_effort"] = reasoning_effort
    payload = {
        "run_id": "run-1",
        "team_selection": {
            "version": "team-selection-v1",
            "pack_name": "research",
            "assignments": [assignment],
        },
        "execution_plan_hash": VALID_PLAN_HASH,
        "immutable": True,
    }
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match=message):
        client.get("/runs/run-1/team")


def test_http_run_team_receipt_accepts_nonempty_reasoning_effort() -> None:
    module = load_script_module()
    body = (
        b'{"run_id":"run-1","team_selection":{"version":"team-selection-v1","pack_name":"research",'
        b'"assignments":[{"slot":"Reader","agent_id":"reader","model_family":"gpt",'
        b'"provider":"openai","model":"gpt5.5","reasoning_effort":"xhigh","fallbacks":[]}]},'
        b'"execution_plan_hash":"'
        + VALID_PLAN_HASH_BYTES
        + b'","immutable":true}'
    )
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(200, body, "application/json")

    result = client.get("/runs/run-1/team")

    assert result["team_selection"]["assignments"][0]["reasoning_effort"] == "xhigh"


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("empty", "invalid field values"),
        ("duplicate_slot", "invalid field values"),
        ("duplicate_agent_id", "invalid field values"),
        ("partial", "missing required typed fields"),
    ],
)
def test_http_run_team_rejects_empty_duplicate_and_partial_receipts(
    case: str,
    message: str,
) -> None:
    module = load_script_module()
    receipt = sample_team_receipt()
    assignments = receipt["assignments"]
    assert isinstance(assignments, list)
    first = assignments[0]
    assert isinstance(first, dict)
    if case == "empty":
        assignments.clear()
    elif case == "duplicate_slot":
        assignments.append({**first, "agent_id": "reader-two"})
    elif case == "duplicate_agent_id":
        assignments.append({**first, "slot": "Writer"})
    else:
        first.pop("agent_id")
    payload = {
        "run_id": "run-1",
        "team_selection": receipt,
        "execution_plan_hash": VALID_PLAN_HASH,
        "immutable": True,
    }
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match=message):
        client.get("/runs/run-1/team")


@pytest.mark.parametrize(
    "plan_hash",
    ["hash", "A" * 64, "a" * 63, "g" * 64],
)
def test_http_run_team_rejects_noncanonical_execution_plan_hash(plan_hash: str) -> None:
    module = load_script_module()
    payload = {
        "run_id": "run-1",
        "team_selection": None,
        "execution_plan_hash": plan_hash,
        "immutable": True,
    }
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="invalid field values"):
        client.get("/runs/run-1/team")


@pytest.mark.parametrize(
    ("selection_kind", "plan_hash", "immutable"),
    [
        ("none", VALID_PLAN_HASH, False),
        ("none", None, True),
        ("receipt", None, True),
        ("receipt", VALID_PLAN_HASH, False),
    ],
)
def test_http_run_team_rejects_incoherent_receipt_state_combinations(
    selection_kind: str,
    plan_hash: str | None,
    immutable: bool,
) -> None:
    module = load_script_module()
    payload = {
        "run_id": "run-1",
        "team_selection": sample_team_receipt() if selection_kind == "receipt" else None,
        "execution_plan_hash": plan_hash,
        "immutable": immutable,
    }
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    with pytest.raises(module.HarnessApiError, match="invalid (team receipt state|field values)"):
        client.get("/runs/run-1/team")


@pytest.mark.parametrize(
    ("selection_kind", "plan_hash", "immutable"),
    [
        ("none", None, False),
        ("none", VALID_PLAN_HASH, True),
        ("receipt", VALID_PLAN_HASH, True),
    ],
)
def test_http_run_team_accepts_only_supported_receipt_states(
    selection_kind: str,
    plan_hash: str | None,
    immutable: bool,
) -> None:
    module = load_script_module()
    selection = sample_team_receipt() if selection_kind == "receipt" else None
    payload = {
        "run_id": "run-1",
        "team_selection": selection,
        "execution_plan_hash": plan_hash,
        "immutable": immutable,
    }
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    assert client.get("/runs/run-1/team") == payload


@pytest.mark.parametrize(
    "case",
    [
        "empty_assignments",
        "empty_slots",
        "slot_mismatch",
        "duplicate_assignment",
        "duplicate_slot",
        "duplicate_agent_id",
    ],
)
def test_http_team_template_rejects_empty_mismatched_and_duplicate_slots(case: str) -> None:
    module = load_script_module()
    payload = sample_team_template_response()
    selection = payload["team_selection"]
    slots = payload["slots"]
    assert isinstance(selection, dict)
    assignments = selection["assignments"]
    assert isinstance(assignments, list)
    assert isinstance(slots, list)
    if case == "empty_assignments":
        assignments.clear()
    elif case == "empty_slots":
        slots.clear()
    elif case == "slot_mismatch":
        slots[0]["slot"] = "Writer"
    elif case == "duplicate_assignment":
        assignments.append(dict(assignments[0]))
    elif case == "duplicate_slot":
        slots.append({**slots[0], "agent_id": "reader-two"})
    else:
        assignments.append({**assignments[0], "slot": "Writer"})
        slots.append({**slots[0], "slot": "Writer"})

    with pytest.raises(module.HarnessApiError, match="invalid field values"):
        module._validate_http_response_fields("get_team_template", payload)


@pytest.mark.parametrize(
    ("runtime_limits", "message"),
    [
        ({"unknown_limit": 1}, "unsupported runtime-limit fields"),
        ({"max_steps": True}, "invalid field values"),
        ({"max_steps": "4"}, "invalid field values"),
        ({"max_steps": -1}, "invalid field values"),
        ({"max_cost_usd": float("nan")}, "invalid field values"),
        ({"timeout_seconds": float("inf")}, "invalid field values"),
    ],
)
def test_http_team_template_rejects_unsupported_or_invalid_runtime_limits(
    runtime_limits: dict[str, object],
    message: str,
) -> None:
    module = load_script_module()
    payload = sample_team_template_response()
    slots = payload["slots"]
    assert isinstance(slots, list)
    slots[0]["runtime_limits"] = runtime_limits

    with pytest.raises(module.HarnessApiError, match=message):
        module._validate_http_response_fields("get_team_template", payload)


def test_http_response_projection_drops_unknown_top_level_fields() -> None:
    module = load_script_module()
    body = json.dumps(
        {
            "status": "ok",
            "worker": "running",
            "system_prompt": "DO_NOT_LEAK",
            "body": {"arbitrary": "DO_NOT_LEAK"},
        }
    ).encode("utf-8")
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(200, body, "application/json")

    assert client.get("/health") == {"status": "ok", "worker": "running"}


def test_http_response_projection_drops_unknown_nested_agent_and_workflow_fields() -> None:
    module = load_script_module()
    payload = [
        {
            "name": "research",
            "system_prompt": "DO_NOT_LEAK",
            "agents": [
                {
                    "id": "reader",
                    "role": "Reader",
                    "system_prompt": "DO_NOT_LEAK",
                    "model_config": {
                        "provider": "openai",
                        "model": "gpt5.5",
                        "body": "DO_NOT_LEAK",
                    },
                }
            ],
            "steps": [
                {
                    "name": "read",
                    "agent_role": "Reader",
                    "arbitrary": {"body": "DO_NOT_LEAK"},
                }
            ],
        }
    ]
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    assert client.get("/workflow-packs") == [
        {
            "name": "research",
            "agents": [
                {
                    "id": "reader",
                    "role": "Reader",
                    "model_config": {"provider": "openai", "model": "gpt5.5"},
                }
            ],
            "steps": [{"name": "read", "agent_role": "Reader"}],
        }
    ]


def test_http_team_template_projection_preserves_role_card_id_and_drops_bodies() -> None:
    module = load_script_module()
    payload = sample_team_template_response()
    selection = payload["team_selection"]
    slots = payload["slots"]
    role_cards = payload["role_cards"]
    assert isinstance(selection, dict)
    assignments = selection["assignments"]
    assert isinstance(assignments, list)
    assert isinstance(slots, list)
    assert isinstance(role_cards, list)
    payload["system_prompt"] = "DO_NOT_LEAK"
    assignments[0]["body"] = "DO_NOT_LEAK"
    assignments[0]["route"]["arbitrary"] = {"body": "DO_NOT_LEAK"}
    slots[0]["body"] = "DO_NOT_LEAK"
    role_cards[0]["body"] = "DO_NOT_LEAK"
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    result = client.get("/workflow-packs/research/team-template")
    serialized = json.dumps(result)

    assert result["team_selection"]["assignments"][0]["role_card_id"] == "research-reader"
    assert result["role_cards"] == [{"id": "research-reader"}]
    assert "DO_NOT_LEAK" not in serialized
    assert '"system_prompt"' not in serialized
    assert '"body"' not in serialized
    assert '"arbitrary"' not in serialized


def test_http_run_detail_projection_drops_unknown_nested_payload_fields() -> None:
    module = load_script_module()
    payload = {
        "run": {
            "id": "run-1",
            "task_id": "task-1",
            "status": "running",
            "system_prompt": "DO_NOT_LEAK",
        },
        "task": {
            "id": "task-1",
            "title": "DO_NOT_LEAK_TITLE",
            "goal": "DO_NOT_LEAK_GOAL",
            "workflow_pack": "research",
            "body": "DO_NOT_LEAK",
        },
        "agent_runs": [
            {
                "id": "agent-run-1",
                "run_id": "run-1",
                "agent_id": "reader",
                "step_name": "read",
                "status": "running",
                "body": "DO_NOT_LEAK",
            }
        ],
        "handoffs": [],
        "trace": [
            {
                "id": "trace-1",
                "run_id": "run-1",
                "event_type": "runtime_event",
                "payload": {
                    "message": "DO_NOT_LEAK_MESSAGE",
                    "error_summary": "DO_NOT_LEAK_ERROR_SUMMARY",
                    "reason": "DO_NOT_LEAK_REASON",
                    "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
                    "system_prompt": "DO_NOT_LEAK",
                    "body": {"arbitrary": "DO_NOT_LEAK"},
                },
            }
        ],
        "artifacts": [],
        "eval_results": [],
        "arbitrary": "DO_NOT_LEAK",
    }
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    result = client.get("/runs/run-1/detail")
    serialized = json.dumps(result)

    assert result["task"] == {"id": "task-1", "workflow_pack": "research"}
    assert result["trace"][0]["payload"] == {
        "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
    }
    assert "DO_NOT_LEAK" not in serialized
    assert '"system_prompt"' not in serialized
    assert '"body"' not in serialized
    assert '"arbitrary"' not in serialized


def test_http_quality_projection_drops_check_message_body() -> None:
    module = load_script_module()
    payload = sample_failing_quality_response(failed_check="final_artifact_content")
    payload["checks"][-1]["message"] = "DO_NOT_LEAK_ARTIFACT_BODY"
    payload["metrics"]["internal_detail"] = "DO_NOT_LEAK_METRIC_DETAIL"
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    client._opener = StubResponseOpener(
        200,
        json.dumps(payload).encode("utf-8"),
        "application/json",
    )

    result = client.get("/runs/run-1/quality")

    assert result["checks"][-1] == {
        "name": "final_artifact_content",
        "status": "fail",
    }
    assert result["metrics"] == sample_quality_metrics()
    assert "DO_NOT_LEAK" not in json.dumps(result)


def test_http_field_and_sensitive_output_contracts_accept_live_local_api_responses(
    tmp_path: Path,
) -> None:
    module = load_script_module()
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts", config_root=tmp_path)

    with TestClient(app) as api:
        template = api.get("/workflow-packs/research/team-template").json()
        task = api.post(
            "/tasks",
            json={
                "title": "MCP live contract",
                "goal": "Verify local response shapes without external calls.",
                "workflow_pack": "research",
            },
        ).json()
        validation = api.post("/team-selections/validate", json=template["team_selection"]).json()
        run = api.post("/runs", json={"task_id": task["id"]}).json()
        quality = api.get(f"/runs/{run['id']}/quality").json()
        assert set(quality) == {
            "run_id",
            "passed",
            "checks",
            "metrics",
            "criteria",
            "execution_plan_hash",
        }
        assert set(quality["metrics"]) == set(sample_quality_metrics())
        assert set(quality["criteria"]) == set(sample_quality_criteria())
        assert quality["execution_plan_hash"] == run["execution_plan_hash"]
        responses = [
            ("health", api.get("/health").json()),
            ("list_workflow_packs", api.get("/workflow-packs").json()),
            ("list_agents", api.get("/agents").json()),
            ("list_model_providers", api.get("/model-providers").json()),
            ("list_tool_providers", api.get("/tool-providers").json()),
            ("get_team_template", template),
            ("create_task", task),
            ("validate_team", validation),
            ("start_run", run),
            ("get_run", api.get(f"/runs/{run['id']}").json()),
            ("get_run_detail", api.get(f"/runs/{run['id']}/detail").json()),
            ("get_run_team", api.get(f"/runs/{run['id']}/team").json()),
            ("get_quality", quality),
        ]

    projected_responses = []
    for operation, payload in responses:
        module._validate_http_response_fields(operation, payload)
        projected = module._project_http_response(operation, payload)
        projected_responses.append(projected)
        serialized = json.dumps(projected)
        assert module._tool_result(projected)["isError"] is False
        assert '"system_prompt"' not in serialized
        assert '"body"' not in serialized

    assert any(projected != payload for projected, (_, payload) in zip(projected_responses, responses))
    assert any('"system_prompt"' in json.dumps(payload) for _, payload in responses)


def test_http_client_rejects_operations_outside_the_whitelist_without_opening() -> None:
    module = load_script_module()
    client = module.HarnessApiClient("http://127.0.0.1:8014")
    opener = StubResponseOpener(200, b"{}", "application/json")
    client._opener = opener

    with pytest.raises(module.HarnessApiError, match="operation is not allowed"):
        client.get("/docs")

    assert opener.response.body.tell() == 0


def test_initialize_ping_and_tool_catalog_match_stdio_mcp_contract() -> None:
    module = load_script_module()
    server = initialize_server(module, FakeClient())

    ping = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}})
    listing = server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})

    assert ping == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert listing is not None
    tools = listing["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert names == {
        "harness_health",
        "harness_list_catalog",
        "harness_list_recent",
        "harness_get_team_template",
        "harness_create_task",
        "harness_delegate_plan",
        "harness_validate_team",
        "harness_start_run",
        "harness_get_run",
        "harness_get_run_detail",
        "harness_get_run_team",
        "harness_get_quality",
        "harness_get_final_artifact",
    }
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)
    assert not any(
        marker in name
        for name in names
        for marker in ("approve", "writeback", "shell", "git", "config", "secret", "credential")
    )


def test_delegate_plan_creates_codex_plan_snapshot_and_deepseek_only_selection() -> None:
    module = load_script_module()
    task_response = {
        "id": "task-delegated",
        "title": "Delegated plan",
        "goal": "Run the delegated plan.",
        "workflow_pack": "research",
        "created_at": VALID_TIMESTAMP,
    }
    client = FakeClient(
        {
            ("POST", "/tasks"): task_response,
            ("GET", "/workflow-packs/research/team-template"): sample_team_template_response(),
            ("POST", "/team-selections/validate"): {
                "valid": True,
                "team_selection": {
                    "version": "team-selection-v1",
                    "pack_name": "research",
                    "assignments": [
                        {
                            "slot": "Reader",
                            "agent_id": "research-reader",
                            "role_card_id": None,
                            "model_family": "deepseek",
                            "provider": "deepseek",
                            "model": "deepseek-v4-flash",
                            "reasoning_effort": "xhigh",
                            "fallbacks": [],
                        }
                    ],
                },
                "public_execution_plan_hash": VALID_PLAN_HASH,
                "immutable_after_run_creation": True,
            },
        }
    )
    server = initialize_server(module, client)

    result = call_tool(
        server,
        2,
        "harness_delegate_plan",
        {
            "title": "Delegated plan",
            "goal": "Run the delegated plan.",
            "workflow_pack": "research",
            "plan": "1. Read the sources.\n2. Verify the claims.\n3. Return a concise report.",
        },
    )

    assert result["isError"] is False
    payload = tool_payload(result)
    assert payload["delegation"] == "codex_to_deepseek"
    assert payload["started"] is False
    assert payload["route_policy"] == "deepseek_only_no_gpt_fallback"
    selection_request = next(
        payload for method, path, payload in client.calls if method == "POST" and path == "/team-selections/validate"
    )
    route = selection_request["assignments"][0]["route"]
    assert route == {
        "family": "deepseek",
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "reasoning_effort": "xhigh",
        "fallbacks": [],
    }
    task_request = next(payload for method, path, payload in client.calls if method == "POST" and path == "/tasks")
    assert task_request["inputs"]["codex_plan_source"] == "codex_mcp"
    assert task_request["inputs"]["codex_plan_hash"] == sha256(task_request["inputs"]["codex_plan"].encode()).hexdigest()


def test_unknown_protocol_version_negotiates_latest_supported_version() -> None:
    module = load_script_module()
    server = module.McpServer(FakeClient(), environ={})

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2099-01-01",
                "capabilities": {},
                "clientInfo": {"name": "future-client"},
            },
        }
    )

    assert response["result"]["protocolVersion"] == module.LATEST_PROTOCOL_VERSION


def test_tools_are_unavailable_before_initialize_and_unknown_methods_fail_closed() -> None:
    module = load_script_module()
    server = module.McpServer(FakeClient(), environ={})

    before_init = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    initialized = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "codex", "version": "0.146.1"},
            },
        }
    )
    unknown = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "unsafe/method", "params": {}})

    assert before_init["error"]["code"] == module.JSONRPC_INVALID_REQUEST
    assert initialized["result"]["protocolVersion"] == "2025-11-25"
    assert unknown["error"]["code"] == module.JSONRPC_METHOD_NOT_FOUND


def test_tool_call_notification_never_executes_mutating_operation() -> None:
    module = load_script_module()
    client = FakeClient()
    server = initialize_server(module, client)

    response = server.handle_message(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "harness_create_task",
                "arguments": {"title": "x", "goal": "y", "workflow_pack": "research"},
            },
        }
    )

    assert response is None
    assert client.calls == []


def test_health_and_catalog_use_fixed_read_only_paths() -> None:
    module = load_script_module()
    client = FakeClient(
        {
            ("GET", "/health"): {"status": "ok", "worker": "running"},
            ("GET", "/workflow-packs"): [{"name": "research"}],
            ("GET", "/agents"): [{"id": "research-planner"}],
            ("GET", "/model-providers"): [{"name": "mock"}],
            ("GET", "/tool-providers"): [{"name": "mock_web"}],
        }
    )
    server = initialize_server(module, client)

    health = call_tool(server, 2, "harness_health", {})
    catalog = call_tool(server, 3, "harness_list_catalog", {})

    assert tool_payload(health) == {"status": "ok", "worker": "running"}
    assert tool_payload(catalog)["workflow_packs"] == [{"name": "research"}]
    assert client.calls == [
        ("GET", "/health", None),
        ("GET", "/workflow-packs", None),
        ("GET", "/agents", None),
        ("GET", "/model-providers", None),
        ("GET", "/tool-providers", None),
    ]


def test_list_recent_uses_fixed_limits_and_projects_raw_fake_responses() -> None:
    module = load_script_module()
    task = sample_recent_task_response()
    run = sample_recent_run_response()
    client = FakeClient(
        {
            ("GET", "/tasks?limit=50&offset=0"): [task],
            ("GET", "/runs?limit=50&offset=0"): [run],
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_list_recent", {})

    payload = tool_payload(result)
    assert payload["tasks"] == [
        {
            "id": "task-1",
            "title": "Recent task",
            "workflow_pack": "research",
            "created_at": VALID_TIMESTAMP,
        }
    ]
    assert set(payload["runs"][0]) == {
        "id",
        "task_id",
        "status",
        "current_step",
        "final_artifact_id",
        "started_at",
        "finished_at",
        "execution_plan_hash",
    }
    assert "DO_NOT_RETURN" not in json.dumps(payload)
    assert client.calls == [
        ("GET", "/tasks?limit=50&offset=0", None),
        ("GET", "/runs?limit=50&offset=0", None),
    ]


def test_list_recent_rejects_arguments_without_calling_api() -> None:
    module = load_script_module()
    client = FakeClient()
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_list_recent", {"limit": 1000})

    assert result["isError"] is True
    assert client.calls == []


def test_injected_client_cannot_bypass_recent_raw_schema_validation() -> None:
    module = load_script_module()
    invalid_task = {**sample_recent_task_response(), "created_at": 1}
    client = FakeClient(
        {
            ("GET", "/tasks?limit=50&offset=0"): [invalid_task],
            ("GET", "/runs?limit=50&offset=0"): [],
        }
    )
    client.returns_validated_projection = True
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_list_recent", {})

    assert result["isError"] is True
    assert client.calls == [("GET", "/tasks?limit=50&offset=0", None)]


def test_get_team_template_uses_bounded_path_segment() -> None:
    module = load_script_module()
    path = "/workflow-packs/code_rd/team-template"
    client = FakeClient({("GET", path): {"version": "team-template-v1", "pack_name": "code_rd"}})
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_get_team_template", {"pack_name": "code_rd"})

    assert tool_payload(result)["pack_name"] == "code_rd"
    assert client.calls == [("GET", path, None)]


def test_team_template_round_trips_through_live_local_api(tmp_path: Path) -> None:
    module = load_script_module()
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts", config_root=tmp_path)

    with TestClient(app) as api:
        server = initialize_server(module, LocalApiClient(api, module.HarnessApiError))
        template_result = call_tool(
            server,
            2,
            "harness_get_team_template",
            {"pack_name": "research"},
        )
        template = tool_payload(template_result)
        validation_result = call_tool(
            server,
            3,
            "harness_validate_team",
            template["team_selection"],
        )

    validation = tool_payload(validation_result)
    assert validation["valid"] is True
    assert validation["team_selection"]["pack_name"] == "research"
    assert len(validation["team_selection"]["assignments"]) == len(template["slots"])


def test_create_task_posts_strict_bounded_payload_with_fixed_creator() -> None:
    module = load_script_module()
    expected = {
        "title": "Research",
        "goal": "Compare evidence.",
        "workflow_pack": "research",
        "inputs": {"topic": "MCP", "limits": [1, 2]},
        "constraints": ["Use local state."],
        "acceptance_criteria": ["Return citations."],
        "created_by": "codex_mcp",
    }
    client = FakeClient({("POST", "/tasks"): {"id": "task-1"}})
    server = initialize_server(module, client)

    result = call_tool(
        server,
        2,
        "harness_create_task",
        {
            "title": expected["title"],
            "goal": expected["goal"],
            "workflow_pack": expected["workflow_pack"],
            "inputs": expected["inputs"],
            "constraints": expected["constraints"],
            "acceptance_criteria": expected["acceptance_criteria"],
        },
    )

    assert tool_payload(result) == {"id": "task-1"}
    assert client.calls == [("POST", "/tasks", expected)]


def test_create_task_rejects_unknown_fields_and_deep_inputs_without_calling_api() -> None:
    module = load_script_module()
    client = FakeClient()
    server = initialize_server(module, client)

    unknown = call_tool(
        server,
        2,
        "harness_create_task",
        {"title": "x", "goal": "y", "workflow_pack": "research", "api_key": "secret"},
    )
    nested: dict[str, object] = {}
    cursor = nested
    for _ in range(module.MAX_JSON_DEPTH + 2):
        child: dict[str, object] = {}
        cursor["next"] = child
        cursor = child
    too_deep = call_tool(
        server,
        3,
        "harness_create_task",
        {"title": "x", "goal": "y", "workflow_pack": "research", "inputs": nested},
    )

    assert unknown["isError"] is True
    assert too_deep["isError"] is True
    assert "secret" not in unknown["content"][0]["text"].lower()
    assert client.calls == []


def test_validate_team_posts_complete_team_selection_contract() -> None:
    module = load_script_module()
    selection = sample_team_selection()
    client = FakeClient({("POST", "/team-selections/validate"): {"valid": True}})
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_validate_team", selection)

    assert tool_payload(result) == {"valid": True}
    assert client.calls == [("POST", "/team-selections/validate", selection)]


def test_team_selection_schema_matches_backend_slot_and_role_card_id_contract() -> None:
    module = load_script_module()

    assignment_properties = module._team_selection_schema()["properties"]["assignments"]["items"][
        "properties"
    ]

    assert assignment_properties["slot"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 200,
    }
    assert assignment_properties["role_card_id"] == {
        "type": ["string", "null"],
        "minLength": 1,
        "maxLength": 200,
        "pattern": r"^[A-Za-z0-9_-]+$",
    }


def test_validate_team_accepts_backend_slot_and_role_card_id_boundaries() -> None:
    module = load_script_module()
    selection = sample_team_selection()
    assignment = selection["assignments"][0]
    slot_prefix = "设计 / review.v2: "
    expected_slot = slot_prefix + "x" * (200 - len(slot_prefix))
    assignment["slot"] = f"  {expected_slot}  "
    assignment["role_card_id"] = "r" * 200
    client = FakeClient({("POST", "/team-selections/validate"): {"valid": True}})
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_validate_team", selection)

    assert result["isError"] is False
    forwarded_assignment = client.calls[0][2]["assignments"][0]
    assert forwarded_assignment["slot"] == expected_slot
    assert len(forwarded_assignment["slot"]) == 200
    assert forwarded_assignment["role_card_id"] == "r" * 200


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("slot", "   "),
        ("slot", "x" * 201),
        ("role_card_id", "review.card"),
        ("role_card_id", "r" * 201),
    ],
)
def test_validate_team_rejects_values_outside_backend_assignment_contract(
    field: str,
    value: str,
) -> None:
    module = load_script_module()
    selection = sample_team_selection()
    selection["assignments"][0][field] = value
    client = FakeClient()
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_validate_team", selection)

    assert result["isError"] is True
    assert client.calls == []


def test_team_route_schema_excludes_pack_owned_limits_and_prices() -> None:
    module = load_script_module()
    schema = module._route_schema()
    forbidden = {
        "temperature",
        "max_tokens",
        "input_usd_per_million",
        "output_usd_per_million",
    }

    assert forbidden.isdisjoint(schema["properties"])
    assert forbidden.isdisjoint(schema["properties"]["fallbacks"]["items"]["properties"])


def test_validate_team_accepts_and_normalizes_nullable_template_fields() -> None:
    module = load_script_module()
    selection = sample_team_selection()
    assignment = selection["assignments"][0]
    assignment["role_card_id"] = None
    route = assignment["route"]
    route["reasoning_effort"] = None
    client = FakeClient({("POST", "/team-selections/validate"): {"valid": True}})
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_validate_team", selection)

    assert result["isError"] is False
    forwarded = client.calls[0][2]
    forwarded_assignment = forwarded["assignments"][0]
    assert "role_card_id" not in forwarded_assignment
    assert "reasoning_effort" not in forwarded_assignment["route"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda selection: selection.update({"unknown": True}),
        lambda selection: selection["assignments"][0]["route"].update({"provider": "anthropic"}),
        lambda selection: selection["assignments"][0]["route"].update(
            {"family": "gpt", "provider": "deepseek"}
        ),
        lambda selection: selection["assignments"][0]["route"].update(
            {"provider": "openai", "model": "deepseek-chat"}
        ),
        lambda selection: selection["assignments"][0]["route"].update({"temperature": 0.2}),
        lambda selection: selection["assignments"][0]["route"].update({"max_tokens": 200_000}),
        lambda selection: selection["assignments"][0]["route"].update(
            {"input_usd_per_million": 1.0}
        ),
        lambda selection: selection["assignments"][0]["route"].update(
            {"output_usd_per_million": 1.0}
        ),
        lambda selection: selection["assignments"][0]["route"]["fallbacks"][0].update(
            {"input_usd_per_million": 1.0}
        ),
        lambda selection: selection["assignments"][0]["route"]["fallbacks"][0].update(
            {"output_usd_per_million": 1.0}
        ),
        lambda selection: selection["assignments"][0]["route"]["fallbacks"][0].pop("family"),
        lambda selection: selection["assignments"].append(selection["assignments"][0].copy()),
    ],
)
def test_validate_team_rejects_expansion_and_invalid_routes_without_api_call(mutate) -> None:
    module = load_script_module()
    selection = sample_team_selection()
    mutate(selection)
    client = FakeClient()
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_validate_team", selection)

    assert result["isError"] is True
    assert client.calls == []


def test_start_run_posts_background_run_and_optional_team_selection() -> None:
    module = load_script_module()
    selection = sample_team_selection()
    receipt = sample_team_validation_response()["team_selection"]
    assert isinstance(receipt, dict)
    expected = {
        "task_id": "task-1",
        "confirm_real_models": False,
        "confirm_real_web": False,
        "background": True,
        "team_selection": selection,
    }
    client = FakeClient(
        {
            ("POST", "/runs"): sample_run_response(),
            ("GET", "/runs/run-1/team"): sample_run_team_response(
                team_selection=receipt,
            ),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_start_run", {"task_id": "task-1", "team_selection": selection})

    assert tool_payload(result)["id"] == "run-1"
    assert client.calls == [
        ("POST", "/runs", expected),
        ("GET", "/runs/run-1/team", None),
    ]


def test_start_run_default_mock_path_binds_an_immutable_empty_team_receipt() -> None:
    module = load_script_module()
    client = FakeClient(
        {
            ("POST", "/runs"): sample_run_response(),
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_start_run", {"task_id": "task-1"})

    assert result["isError"] is False
    assert tool_payload(result)["execution_plan_hash"] == VALID_PLAN_HASH
    assert server._run_bindings["run-1"].team_selection is None


def test_run_binding_lru_refreshes_hits_and_revalidates_evicted_lineage() -> None:
    module = load_script_module()
    responses: dict[tuple[str, str], object] = {}
    for index in range(module.MAX_RUN_BINDINGS + 1):
        run_id = f"run-{index}"
        responses[("GET", f"/runs/{run_id}")] = sample_run_response(
            run_id=run_id,
            task_id=f"task-{index}",
        )
        responses[("GET", f"/runs/{run_id}/team")] = sample_run_team_response(
            run_id=run_id,
        )
    client = FakeClient(responses)
    server = initialize_server(module, client)

    for index in range(module.MAX_RUN_BINDINGS):
        result = call_tool(
            server,
            index + 2,
            "harness_get_run",
            {"run_id": f"run-{index}"},
        )
        assert result["isError"] is False

    call_tool(
        server,
        module.MAX_RUN_BINDINGS + 2,
        "harness_get_run",
        {"run_id": "run-0"},
    )
    call_tool(
        server,
        module.MAX_RUN_BINDINGS + 3,
        "harness_get_run",
        {"run_id": f"run-{module.MAX_RUN_BINDINGS}"},
    )

    assert len(server._run_bindings) == module.MAX_RUN_BINDINGS
    assert "run-0" in server._run_bindings
    assert "run-1" not in server._run_bindings

    responses[("GET", "/runs/run-1/team")] = sample_run_team_response(
        run_id="run-1",
        plan_hash="b" * 64,
    )
    client.calls.clear()
    evicted = call_tool(
        server,
        module.MAX_RUN_BINDINGS + 4,
        "harness_get_run",
        {"run_id": "run-1"},
    )

    assert evicted["isError"] is True
    assert client.calls == [
        ("GET", "/runs/run-1", None),
        ("GET", "/runs/run-1/team", None),
    ]
    assert "run-1" not in server._run_bindings
    assert len(server._run_bindings) == module.MAX_RUN_BINDINGS


def test_start_run_binds_normalized_real_web_snapshot_from_response() -> None:
    module = load_script_module()
    client = FakeClient(
        {
            ("POST", "/runs"): sample_run_response(
                confirm_real_web=True,
                confirmed_real_web_tools=("web_search", "browser_fetch"),
                confirmed_real_web_tool_routes=(
                    ("web_search", "tavily"),
                    ("browser_fetch", "edge"),
                ),
            ),
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
        }
    )
    server = initialize_server(
        module,
        client,
        environ={"TEAM_AGENT_CODEX_ALLOW_REAL_WEB": "1"},
    )

    result = call_tool(
        server,
        2,
        "harness_start_run",
        {"task_id": "task-1", "confirm_real_web": True},
    )

    assert result["isError"] is False
    binding = server._run_bindings["run-1"]
    assert binding.confirmed_real_web_tools == ("browser_fetch", "web_search")
    assert binding.confirmed_real_web_tool_routes == (
        ("browser_fetch", "edge"),
        ("web_search", "tavily"),
    )
    payload = tool_payload(result)
    assert payload["confirmed_real_web_tools"] == ["browser_fetch", "web_search"]


def test_start_run_rejects_same_task_old_run_bound_to_another_team() -> None:
    module = load_script_module()
    selection = sample_team_selection()
    other_receipt = sample_team_validation_response()["team_selection"]
    assert isinstance(other_receipt, dict)
    other_receipt["pack_name"] = "code_rd"
    client = FakeClient(
        {
            ("POST", "/runs"): sample_run_response(),
            ("GET", "/runs/run-1/team"): sample_run_team_response(
                team_selection=other_receipt,
            ),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(
        server,
        2,
        "harness_start_run",
        {"task_id": "task-1", "team_selection": selection},
    )

    assert result["isError"] is True
    assert server._run_bindings == {}


def test_start_run_without_team_selection_rejects_nonempty_team_receipt() -> None:
    module = load_script_module()
    client = FakeClient(
        {
            ("POST", "/runs"): sample_run_response(),
            ("GET", "/runs/run-1/team"): sample_run_team_response(
                team_selection=sample_team_receipt(),
            ),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_start_run", {"task_id": "task-1"})

    assert result["isError"] is True
    assert server._run_bindings == {}


@pytest.mark.parametrize(
    "response_update",
    [
        {"real_model_access_confirmed": True},
        {"real_web_access_confirmed": True},
        {"execution_plan_hash": ""},
        {"execution_plan_hash": "A" * 64},
    ],
)
def test_start_run_rejects_confirmation_or_plan_hash_mismatch(
    response_update: dict[str, object],
) -> None:
    module = load_script_module()
    client = FakeClient(
        {
            ("POST", "/runs"): {**sample_run_response(), **response_update},
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_start_run", {"task_id": "task-1"})

    assert result["isError"] is True
    assert client.calls == [
        (
            "POST",
            "/runs",
            {
                "task_id": "task-1",
                "confirm_real_models": False,
                "confirm_real_web": False,
                "background": True,
            },
        )
    ]


def test_start_run_rejects_execution_plan_hash_drift_in_team_receipt() -> None:
    module = load_script_module()
    client = FakeClient(
        {
            ("POST", "/runs"): sample_run_response(),
            ("GET", "/runs/run-1/team"): sample_run_team_response(
                plan_hash="b" * 64,
            ),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_start_run", {"task_id": "task-1"})

    assert result["isError"] is True
    assert server._run_bindings == {}


@pytest.mark.parametrize(
    ("argument", "capability_env"),
    [
        ("confirm_real_models", "TEAM_AGENT_CODEX_ALLOW_REAL_MODELS"),
        ("confirm_real_web", "TEAM_AGENT_CODEX_ALLOW_REAL_WEB"),
    ],
)
def test_start_run_requires_process_capability_for_real_access(argument: str, capability_env: str) -> None:
    module = load_script_module()
    denied_client = FakeClient()
    denied_server = initialize_server(module, denied_client, environ={})

    denied = call_tool(denied_server, 2, "harness_start_run", {"task_id": "task-1", argument: True})

    assert denied["isError"] is True
    assert denied_client.calls == []

    allowed_client = FakeClient(
        {
            ("POST", "/runs"): sample_run_response(
                confirm_real_models=argument == "confirm_real_models",
                confirm_real_web=argument == "confirm_real_web",
            ),
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
        }
    )
    allowed_server = initialize_server(module, allowed_client, environ={capability_env: "1"})
    allowed = call_tool(allowed_server, 3, "harness_start_run", {"task_id": "task-1", argument: True})

    assert allowed["isError"] is False
    assert allowed_client.calls[0][2][argument] is True


def test_get_final_artifact_reads_completed_run_with_verified_binding() -> None:
    module = load_script_module()
    content = "Final artifact content."
    run = sample_run_response(status="completed", final_artifact_id="artifact-1")
    client = FakeClient(
        {
            ("GET", "/runs/run-1"): run,
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
            ("GET", "/artifacts/artifact-1"): sample_artifact_response(content=content),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_get_final_artifact", {"run_id": "run-1"})

    assert tool_payload(result) == {
        "trust": "untrusted_artifact_content",
        "type": "final_report",
        "content_hash": sha256(content.encode("utf-8")).hexdigest(),
        "content_length": len(content),
        "truncated": False,
        "content": content,
    }
    assert client.calls == [
        ("GET", "/runs/run-1", None),
        ("GET", "/runs/run-1/team", None),
        ("GET", "/artifacts/artifact-1", None),
    ]


def test_get_final_artifact_truncates_by_unicode_characters_after_hash_verification() -> None:
    module = load_script_module()
    content = "界" * module.MAX_FINAL_ARTIFACT_CHARS + "🙂"
    run = sample_run_response(status="completed", final_artifact_id="artifact-1")
    client = FakeClient(
        {
            ("GET", "/runs/run-1"): run,
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
            ("GET", "/artifacts/artifact-1"): sample_artifact_response(content=content),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_get_final_artifact", {"run_id": "run-1"})

    payload = tool_payload(result)
    assert payload["content_length"] == module.MAX_FINAL_ARTIFACT_CHARS + 1
    assert payload["truncated"] is True
    assert payload["content"] == "界" * module.MAX_FINAL_ARTIFACT_CHARS
    assert payload["content_hash"] == sha256(content.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "run",
    [
        sample_run_response(status="running", final_artifact_id="artifact-1"),
        sample_run_response(status="completed", final_artifact_id=None),
    ],
)
def test_get_final_artifact_rejects_unfinished_or_missing_final_artifact_id(
    run: dict[str, object],
) -> None:
    module = load_script_module()
    client = FakeClient(
        {
            ("GET", "/runs/run-1"): run,
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_get_final_artifact", {"run_id": "run-1"})

    assert result["isError"] is True
    assert client.calls == [
        ("GET", "/runs/run-1", None),
        ("GET", "/runs/run-1/team", None),
    ]


@pytest.mark.parametrize(
    "artifact",
    [
        sample_artifact_response(artifact_id="artifact-2"),
        sample_artifact_response(run_id="run-2"),
    ],
)
def test_get_final_artifact_rejects_forged_artifact_or_cross_run_binding(
    artifact: dict[str, object],
) -> None:
    module = load_script_module()
    run = sample_run_response(status="completed", final_artifact_id="artifact-1")
    client = FakeClient(
        {
            ("GET", "/runs/run-1"): run,
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
            ("GET", "/artifacts/artifact-1"): artifact,
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_get_final_artifact", {"run_id": "run-1"})

    assert result["isError"] is True


def test_get_final_artifact_rejects_invalid_validation_status() -> None:
    module = load_script_module()
    run = sample_run_response(status="completed", final_artifact_id="artifact-1")
    client = FakeClient(
        {
            ("GET", "/runs/run-1"): run,
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
            ("GET", "/artifacts/artifact-1"): sample_artifact_response(
                validation_status="unknown"
            ),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_get_final_artifact", {"run_id": "run-1"})

    assert result["isError"] is True


def test_get_final_artifact_rejects_invalid_or_mismatched_hash() -> None:
    module = load_script_module()
    run = sample_run_response(status="completed", final_artifact_id="artifact-1")
    invalid_hash = sample_artifact_response()
    invalid_hash["artifact"]["content_hash"] = "A" * 64
    mismatched_hash = sample_artifact_response(content="Expected content")
    mismatched_hash["content"] = "Different content"

    for artifact in (invalid_hash, mismatched_hash):
        client = FakeClient(
            {
                ("GET", "/runs/run-1"): run,
                ("GET", "/runs/run-1/team"): sample_run_team_response(),
                ("GET", "/artifacts/artifact-1"): artifact,
            }
        )
        server = initialize_server(module, client)

        result = call_tool(server, 2, "harness_get_final_artifact", {"run_id": "run-1"})

        assert result["isError"] is True


def test_get_final_artifact_rejects_missing_content() -> None:
    module = load_script_module()
    run = sample_run_response(status="completed", final_artifact_id="artifact-1")
    artifact = sample_artifact_response()
    del artifact["content"]
    client = FakeClient(
        {
            ("GET", "/runs/run-1"): run,
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
            ("GET", "/artifacts/artifact-1"): artifact,
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_get_final_artifact", {"run_id": "run-1"})

    assert result["isError"] is True


def test_get_final_artifact_blocks_secret_like_content() -> None:
    module = load_script_module()
    content = "api_key=topsecret"
    run = sample_run_response(status="completed", final_artifact_id="artifact-1")
    client = FakeClient(
        {
            ("GET", "/runs/run-1"): run,
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
            ("GET", "/artifacts/artifact-1"): sample_artifact_response(content=content),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_get_final_artifact", {"run_id": "run-1"})

    assert result["isError"] is True
    assert "topsecret" not in result["content"][0]["text"]


def test_get_final_artifact_rejects_unsafe_run_id_without_calling_api() -> None:
    module = load_script_module()
    client = FakeClient()
    server = initialize_server(module, client)

    result = call_tool(
        server,
        2,
        "harness_get_final_artifact",
        {"run_id": "../run-1/secret"},
    )

    assert result["isError"] is True
    assert client.calls == []


def test_recent_and_final_artifact_round_trip_through_live_local_api(tmp_path: Path) -> None:
    module = load_script_module()
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts", config_root=tmp_path)

    with TestClient(app) as api:
        task = api.post(
            "/tasks",
            json={
                "title": "MCP recent and final artifact",
                "goal": "Verify the local read-only MCP paths.",
                "workflow_pack": "research",
            },
        ).json()
        run = api.post(
            "/runs",
            json={"task_id": task["id"], "background": False},
        ).json()
        assert run["status"] == "completed"
        server = initialize_server(module, LocalApiClient(api, module.HarnessApiError))

        recent_result = call_tool(server, 2, "harness_list_recent", {})
        artifact_result = call_tool(
            server,
            3,
            "harness_get_final_artifact",
            {"run_id": run["id"]},
        )

    recent = tool_payload(recent_result)
    artifact = tool_payload(artifact_result)
    assert recent["tasks"][0] == {
        "id": task["id"],
        "title": task["title"],
        "workflow_pack": "research",
        "created_at": task["created_at"],
    }
    assert recent["runs"][0]["id"] == run["id"]
    assert recent["runs"][0]["status"] == "completed"
    assert artifact["trust"] == "untrusted_artifact_content"
    assert artifact["type"] == "final_report"
    assert artifact["content_length"] == len(artifact["content"])
    assert artifact["content_hash"] == sha256(artifact["content"].encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    ("tool_name", "suffix"),
    [
        ("harness_get_run", ""),
        ("harness_get_run_detail", "/detail"),
        ("harness_get_run_team", "/team"),
        ("harness_get_quality", "/quality"),
    ],
)
def test_run_read_tools_use_fixed_bounded_paths(tool_name: str, suffix: str) -> None:
    module = load_script_module()
    path = f"/runs/run-1{suffix}"
    run = sample_run_response()
    team = sample_run_team_response()
    response = {
        "harness_get_run": run,
        "harness_get_run_detail": sample_run_detail_response(run=run),
        "harness_get_run_team": team,
        "harness_get_quality": sample_failing_quality_response(),
    }[tool_name]
    client = FakeClient(
        {
            ("GET", "/runs/run-1"): run,
            ("GET", "/runs/run-1/team"): team,
            ("GET", path): response,
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, tool_name, {"run_id": "run-1"})

    assert result["isError"] is False
    assert server._run_bindings["run-1"].execution_plan_hash == VALID_PLAN_HASH


@pytest.mark.parametrize(
    "tool_name",
    [
        "harness_get_run",
        "harness_get_run_detail",
        "harness_get_run_team",
        "harness_get_quality",
    ],
)
def test_fresh_run_read_rejects_canonical_run_team_hash_drift(tool_name: str) -> None:
    module = load_script_module()
    client = FakeClient(
        {
            ("GET", "/runs/run-1"): sample_run_response(),
            ("GET", "/runs/run-1/team"): sample_run_team_response(plan_hash="b" * 64),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, tool_name, {"run_id": "run-1"})

    assert result["isError"] is True
    assert server._run_bindings == {}


def test_fresh_legacy_run_read_rebuilds_null_hash_binding() -> None:
    module = load_script_module()
    client = FakeClient(
        {
            ("GET", "/runs/run-1"): sample_run_response(plan_hash=None),
            ("GET", "/runs/run-1/team"): sample_run_team_response(
                plan_hash=None,
                immutable=False,
            ),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_get_run", {"run_id": "run-1"})

    assert result["isError"] is False
    assert server._run_bindings["run-1"].execution_plan_hash is None


@pytest.mark.parametrize(
    "detail_run_update",
    [
        {"task_id": "task-2"},
        {"status": "running"},
        {"final_artifact_id": "artifact-2"},
    ],
)
def test_fresh_run_detail_rejects_canonical_run_drift(
    detail_run_update: dict[str, object],
) -> None:
    module = load_script_module()
    canonical_run = sample_run_response(
        status="completed",
        final_artifact_id="artifact-1",
    )
    detail_run = {**canonical_run, **detail_run_update}
    client = FakeClient(
        {
            ("GET", "/runs/run-1"): canonical_run,
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
            ("GET", "/runs/run-1/detail"): sample_run_detail_response(run=detail_run),
        }
    )
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_get_run_detail", {"run_id": "run-1"})

    assert result["isError"] is True


def test_fake_client_cannot_spoof_prevalidated_http_projection() -> None:
    module = load_script_module()
    run = sample_run_response(status="completed", final_artifact_id="artifact-1")
    detail = sample_run_detail_response(run=run)
    detail["task"] = {"id": "task-1", "workflow_pack": "research"}
    client = FakeClient(
        {
            ("POST", "/runs"): run,
            ("GET", "/runs/run-1/team"): sample_run_team_response(),
            ("GET", "/runs/run-1/detail"): detail,
            ("GET", "/runs/run-1"): run,
        }
    )
    client.returns_validated_projection = True
    server = initialize_server(module, client)
    started = call_tool(server, 2, "harness_start_run", {"task_id": "task-1"})
    assert started["isError"] is False

    result = call_tool(server, 3, "harness_get_run_detail", {"run_id": "run-1"})

    assert result["isError"] is True


@pytest.mark.parametrize(
    "response_update",
    [
        {"task_id": "task-2"},
        {"real_model_access_confirmed": True},
        {"real_web_access_confirmed": True},
        {"execution_plan_hash": "b" * 64},
    ],
)
def test_known_run_rejects_task_confirmation_or_hash_drift(
    response_update: dict[str, object],
) -> None:
    module = load_script_module()
    server, client = bound_default_run_server(module)
    client.responses[("GET", "/runs/run-1")] = {
        **sample_run_response(),
        **response_update,
    }

    result = call_tool(server, 3, "harness_get_run", {"run_id": "run-1"})

    assert result["isError"] is True


@pytest.mark.parametrize(
    "team_response",
    [
        sample_run_team_response(plan_hash="b" * 64),
        sample_run_team_response(team_selection=sample_team_receipt()),
    ],
)
def test_known_run_team_rejects_hash_or_team_drift(
    team_response: dict[str, object],
) -> None:
    module = load_script_module()
    server, client = bound_default_run_server(module)
    client.responses[("GET", "/runs/run-1/team")] = team_response

    result = call_tool(server, 3, "harness_get_run_team", {"run_id": "run-1"})

    assert result["isError"] is True


@pytest.mark.parametrize(
    ("tool_name", "suffix"),
    [
        ("harness_get_run", ""),
        ("harness_get_run_team", "/team"),
        ("harness_get_quality", "/quality"),
    ],
)
@pytest.mark.parametrize(
    "case",
    [
        "tool_drift",
        "provider_drift",
        "missing_tools",
        "partial_null",
        "legacy_null",
        "duplicate",
        "name_mismatch",
    ],
)
def test_known_run_reads_reject_real_web_snapshot_drift_or_malformed_state(
    tool_name: str,
    suffix: str,
    case: str,
) -> None:
    module = load_script_module()
    server, client = bound_real_web_run_server(module)
    run_response = sample_run_response(
        confirm_real_web=True,
        confirmed_real_web_tools=("web_search",),
        confirmed_real_web_tool_routes=(("web_search", "tavily"),),
    )
    if case == "tool_drift":
        run_response["confirmed_real_web_tools"] = ["browser_search"]
        run_response["confirmed_real_web_tool_routes"] = [
            {"name": "browser_search", "provider": "edge"}
        ]
    elif case == "provider_drift":
        run_response["confirmed_real_web_tool_routes"] = [
            {"name": "web_search", "provider": "other"}
        ]
    elif case == "missing_tools":
        run_response.pop("confirmed_real_web_tools")
    elif case == "partial_null":
        run_response["confirmed_real_web_tools"] = None
    elif case == "legacy_null":
        run_response["confirmed_real_web_tools"] = None
        run_response["confirmed_real_web_tool_routes"] = None
    elif case == "duplicate":
        run_response["confirmed_real_web_tools"] = ["web_search", "web_search"]
        run_response["confirmed_real_web_tool_routes"] = [
            {"name": "web_search", "provider": "tavily"},
            {"name": "web_search", "provider": "tavily"},
        ]
    else:
        run_response["confirmed_real_web_tool_routes"] = [
            {"name": "fetch_page", "provider": "tavily"}
        ]

    client.responses[("GET", "/runs/run-1")] = run_response
    client.responses[("GET", "/runs/run-1/team")] = sample_run_team_response()
    client.responses[("GET", "/runs/run-1/quality")] = sample_failing_quality_response()

    result = call_tool(server, 3, tool_name, {"run_id": "run-1"})

    assert result["isError"] is True
    assert client.calls[0] == ("GET", f"/runs/run-1{suffix}", None)


def test_quality_rejects_single_arbitrary_passing_check_for_known_run() -> None:
    module = load_script_module()
    server, client = bound_default_run_server(module)
    quality = sample_passing_quality_response()
    quality["checks"] = [
        {"name": "anything", "status": "pass", "message": "passed"}
    ]
    client.responses[("GET", "/runs/run-1/quality")] = quality

    result = call_tool(server, 3, "harness_get_quality", {"run_id": "run-1"})

    assert result["isError"] is True
    assert client.calls == [("GET", "/runs/run-1/quality", None)]


def test_quality_rejects_duplicate_check_names() -> None:
    module = load_script_module()
    server, client = bound_default_run_server(module)
    quality = sample_failing_quality_response()
    quality["checks"].insert(1, dict(quality["checks"][0]))
    client.responses[("GET", "/runs/run-1/quality")] = quality

    result = call_tool(server, 3, "harness_get_quality", {"run_id": "run-1"})

    assert result["isError"] is True
    assert client.calls == [("GET", "/runs/run-1/quality", None)]


@pytest.mark.parametrize(
    "run_response",
    [
        sample_run_response(status="running", final_artifact_id="artifact-1"),
        sample_run_response(status="completed", final_artifact_id=None),
        sample_run_response(
            status="completed",
            final_artifact_id="artifact-1",
            plan_hash="b" * 64,
        ),
    ],
)
def test_passing_quality_requires_completed_bound_run_with_final_artifact(
    run_response: dict[str, object],
) -> None:
    module = load_script_module()
    server, client = bound_default_run_server(module)
    client.responses[("GET", "/runs/run-1/quality")] = sample_passing_quality_response()
    client.responses[("GET", "/runs/run-1")] = run_response

    result = call_tool(server, 3, "harness_get_quality", {"run_id": "run-1"})

    assert result["isError"] is True
    assert client.calls == [
        ("GET", "/runs/run-1/quality", None),
        ("GET", "/runs/run-1", None),
    ]


def test_failed_quality_still_checks_the_bound_execution_plan_hash() -> None:
    module = load_script_module()
    server, client = bound_default_run_server(module)
    client.responses[("GET", "/runs/run-1/quality")] = sample_failing_quality_response(
        plan_hash="b" * 64,
    )

    result = call_tool(server, 3, "harness_get_quality", {"run_id": "run-1"})

    assert result["isError"] is True
    assert client.calls == [("GET", "/runs/run-1/quality", None)]


def test_passing_quality_accepts_completed_bound_run_with_final_artifact() -> None:
    module = load_script_module()
    server, client = bound_default_run_server(module)
    client.responses[("GET", "/runs/run-1/quality")] = sample_passing_quality_response()
    client.responses[("GET", "/runs/run-1")] = sample_run_response(
        status="completed",
        final_artifact_id="artifact-1",
    )

    result = call_tool(server, 3, "harness_get_quality", {"run_id": "run-1"})

    assert result["isError"] is False
    assert tool_payload(result)["passed"] is True


def test_unknown_tool_and_unsafe_path_input_fail_without_api_call() -> None:
    module = load_script_module()
    client = FakeClient()
    server = initialize_server(module, client)

    unknown = call_tool(server, 2, "harness_approve", {})
    unsafe = call_tool(server, 3, "harness_get_run", {"run_id": "../secret"})

    assert unknown["isError"] is True
    assert unsafe["isError"] is True
    assert client.calls == []


def test_tool_errors_are_redacted_and_api_bodies_are_not_reflected() -> None:
    module = load_script_module()
    client = FakeClient(
        {
            ("GET", "/health"): module.HarnessApiError(
                "Authorization: Bearer sk-real-secret api_key=topsecret token=abc123"
            )
        }
    )
    server = initialize_server(module, client, environ={"OPENAI_API_KEY": "sk-real-secret"})

    result = call_tool(server, 2, "harness_health", {})
    text = result["content"][0]["text"]

    assert result["isError"] is True
    assert "sk-real-secret" not in text
    assert "topsecret" not in text
    assert "abc123" not in text
    assert "[REDACTED]" in text


@pytest.mark.parametrize("env_name", ["OPENAI_OFFICIAL_API_KEY", "TAVILY_API_KEY"])
def test_tool_errors_redact_exact_configured_server_secrets(
    env_name: str,
) -> None:
    module = load_script_module()
    secret = f"{env_name.lower()}-unpatterned-987654"
    client = FakeClient({("GET", "/health"): module.HarnessApiError(f"upstream: {secret}")})
    server = initialize_server(module, client, environ={env_name: secret})

    result = call_tool(server, 2, "harness_health", {})
    text = result["content"][0]["text"]

    assert result["isError"] is True
    assert secret not in text
    assert "[REDACTED]" in text


@pytest.mark.parametrize("env_name", ["OPENAI_OFFICIAL_API_KEY", "TAVILY_API_KEY"])
def test_successful_tool_result_blocks_exact_configured_server_secrets(
    env_name: str,
) -> None:
    module = load_script_module()
    secret = f"{env_name.lower()}-unpatterned-987654"
    client = FakeClient({("GET", "/health"): {"message": f"upstream: {secret}"}})
    server = initialize_server(module, client, environ={env_name: secret})

    result = call_tool(server, 2, "harness_health", {})

    assert result["isError"] is True
    assert secret not in result["content"][0]["text"]
    assert "blocked" in result["content"][0]["text"]


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "placeholder"},
        {"safe": [{"client_secret": "placeholder"}]},
        {"safe": {"nested": [{"authorization": "placeholder"}]}},
        {"safe": {"my_token_value": "placeholder"}},
        {"safe": {"access_tokens": ["placeholder"]}},
        {"safe": {"api_key_value": "placeholder"}},
        {"safe": {"secret_value": "placeholder"}},
        {"safe": {"tokens": ["placeholder"]}},
        {"safe": {"max_tokens": "placeholder"}},
        {"safe": {"input_tokens": ["placeholder"]}},
        {"safe": {"input_tokens": True}},
        {"safe": {"requires_credentials": "placeholder"}},
    ],
)
def test_successful_tool_result_rejects_sensitive_field_names_recursively(payload: object) -> None:
    module = load_script_module()
    client = FakeClient({("GET", "/health"): payload})
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_health", {})

    assert result["isError"] is True
    assert "blocked" in result["content"][0]["text"]
    assert "placeholder" not in result["content"][0]["text"]


@pytest.mark.parametrize(
    "secret_value",
    [
        "Bearer abcdefghijklmnop",
        "sk-abcdefghijklmno",
        "AKIAABCDEFGHIJKLMNOP",
        "github_pat_abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_successful_tool_result_rejects_secret_like_values_recursively(secret_value: str) -> None:
    module = load_script_module()
    client = FakeClient({("GET", "/health"): {"safe": [{"nested": secret_value}]}})
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_health", {})

    assert result["isError"] is True
    assert "blocked" in result["content"][0]["text"]
    assert secret_value not in result["content"][0]["text"]


def test_successful_tool_result_allows_normal_nested_operational_data() -> None:
    module = load_script_module()
    payload = {
        "status": "ok",
        "worker": "running",
        "metrics": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        "provider": {"requires_credentials": True, "real_calls_configured": False},
        "items": [{"message": "No private data was returned."}],
    }
    client = FakeClient({("GET", "/health"): payload})
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_health", {})

    assert result["isError"] is False
    assert tool_payload(result) == payload


def test_oversized_tool_result_fails_closed() -> None:
    module = load_script_module()
    client = FakeClient({("GET", "/health"): {"value": "x" * (module.MAX_TOOL_RESULT_BYTES + 1)}})
    server = initialize_server(module, client)

    result = call_tool(server, 2, "harness_health", {})

    assert result["isError"] is True
    assert "exceeded" in result["content"][0]["text"]


def test_stdio_loop_handles_handshake_notification_invalid_json_and_tool_list() -> None:
    module = load_script_module()
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "codex", "version": "0.146.1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    wire = "\n".join(json.dumps(message) for message in messages[:2]) + "\n{bad json}\n"
    wire += json.dumps(messages[2]) + "\n"
    stdout = io.StringIO()

    assert module.serve(module.McpServer(FakeClient(), environ={}), io.StringIO(wire), stdout) == 0

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [response["id"] for response in responses] == [1, None, 2]
    assert responses[1]["error"]["code"] == module.JSONRPC_PARSE_ERROR
    assert len(responses[2]["result"]["tools"]) == 13


def test_stdio_loop_rejects_oversized_line_and_recovers_at_next_message() -> None:
    module = load_script_module()
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "codex", "version": "0.146.1"},
        },
    }
    wire = "x" * (module.MAX_REQUEST_BYTES + 10) + "\n" + json.dumps(initialize) + "\n"
    stdout = io.StringIO()

    assert module.serve(module.McpServer(FakeClient(), environ={}), io.StringIO(wire), stdout) == 0

    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert responses[0]["error"]["code"] == module.JSONRPC_PARSE_ERROR
    assert responses[1]["result"]["serverInfo"]["name"] == module.SERVER_NAME


def test_strict_json_rejects_duplicate_keys_and_non_finite_values() -> None:
    module = load_script_module()

    with pytest.raises(ValueError):
        module._strict_json_loads('{"jsonrpc":"2.0","jsonrpc":"2.0"}')
    with pytest.raises(ValueError):
        module._strict_json_loads('{"value":NaN}')


def test_request_and_method_params_reject_unknown_fields() -> None:
    module = load_script_module()
    server = module.McpServer(FakeClient(), environ={})

    root_extra = server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}, "unsafe": True}
    )
    ping_extra = server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {"unsafe": True}}
    )

    assert root_extra["error"]["code"] == module.JSONRPC_INVALID_PARAMS
    assert ping_extra["error"]["code"] == module.JSONRPC_INVALID_PARAMS


def _run_mcp_subprocess(wire: bytes) -> subprocess.CompletedProcess[bytes]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "gbk:strict"
    env["PYTHONUTF8"] = "0"
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        input=wire,
        capture_output=True,
        check=False,
        env=env,
        timeout=15,
    )


def test_raw_stdio_subprocess_forces_utf8_despite_pythonioencoding() -> None:
    request_id = "初始化-中文-emoji-😀"
    initialize = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "Codex 中文 😀", "version": "test"},
        },
    }
    wire = (json.dumps(initialize, ensure_ascii=False) + "\n").encode("utf-8")

    completed = _run_mcp_subprocess(wire)

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    assert completed.stderr == b""
    assert "中文".encode("utf-8") in completed.stdout
    assert "😀".encode("utf-8") in completed.stdout
    response = json.loads(completed.stdout.decode("utf-8"))
    assert response["id"] == request_id


def test_raw_stdio_subprocess_rejects_gbk_valid_non_utf8_bytes() -> None:
    completed = _run_mcp_subprocess(b"\x81\x40\n")

    assert completed.returncode == 3
    assert completed.stdout == b""
    assert completed.stderr.decode("utf-8").strip() == "MCP stdio must contain valid UTF-8."
