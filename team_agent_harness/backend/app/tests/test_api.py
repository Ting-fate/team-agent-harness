from fastapi.testclient import TestClient
from datetime import UTC, datetime, timedelta
import json
import pytest

from app.core.models import (
    AgentDefinition,
    AgentSession,
    ArtifactType,
    Run,
    RunLock,
    RunQueueItemStatus,
    RuntimeJob,
    RuntimeJobStatus,
    Task,
)
from app.core.model_runtime import (
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ModelRuntimeError,
    OpenAICompatibleModelAdapter,
)
from app.core.runner import AgentArtifactOutput, AgentStepOutput
from app.core.storage import StorageError
from app.api import PackMappedExecutor
from app.core.model_routing import apply_model_routing_config, load_model_routing_config
from app.packs.code_rd import get_code_rd_pack
from app.packs.base import WorkflowStep
from app.main import create_app


def test_openapi_declares_typed_run_response_contracts(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts", config_root=tmp_path)
    with TestClient(app) as client:
        response = client.get("/openapi.json")

    assert response.status_code == 200
    document = response.json()
    schemas = document["components"]["schemas"]
    assert schemas["Run"]["properties"]["real_web_access_confirmed"] == {
        "type": "boolean",
        "title": "Real Web Access Confirmed",
        "default": False,
    }
    assert document["paths"]["/runs"]["post"]["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Run"
    }
    assert document["paths"]["/runs"]["get"]["responses"]["200"]["content"]["application/json"]["schema"][
        "items"
    ] == {"$ref": "#/components/schemas/Run"}
    assert document["paths"]["/runs/{run_id}"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/Run"}
    assert document["paths"]["/runs/{run_id}/detail"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/RunDetailResponse"}
    assert schemas["RunDetailResponse"]["properties"]["run"] == {"$ref": "#/components/schemas/Run"}


def test_run_endpoints_preserve_the_same_serialized_run_contract(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Run response contract",
                "goal": "Verify every run endpoint returns the same serialized run.",
                "workflow_pack": "code_rd",
            },
        ).json()

        created = client.post("/runs", json={"task_id": task["id"]}).json()
        listed = client.get("/runs", params={"limit": 1}).json()[0]
        fetched = client.get(f"/runs/{created['id']}").json()
        detailed = client.get(f"/runs/{created['id']}/detail").json()["run"]

    assert created["real_web_access_confirmed"] is False
    assert listed == created
    assert fetched == created
    assert detailed == created


def test_task_creation_rejects_invalid_unicode_with_client_error(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/tasks",
            content=(
                '{"title":"Invalid Unicode","goal":"bad \\ud800 value",'
                '"workflow_pack":"research"}'
            ),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail == [
        {
            "type": "string_unicode",
            "loc": ["body", "goal"],
            "msg": "Invalid request.",
        }
    ]
    assert "bad" not in response.text


def test_request_validation_response_bounds_errors_and_hides_dynamic_field_names(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    marker = "RAW_PROMPT_SECRET_FIELD"
    payload = {
        "title": "Bound validation errors",
        "goal": "Reject extra fields safely.",
        "workflow_pack": "code_rd",
        **{f"{marker}_{index}": index for index in range(1_000)},
    }

    with TestClient(app) as client:
        response = client.post("/tasks", json=payload)

    assert response.status_code == 422
    assert len(response.content) < 8_192
    assert marker not in response.text
    detail = response.json()["detail"]
    assert len(detail) == 33
    assert all(item["msg"] == "Invalid request." for item in detail[:-1])
    assert all(item["loc"] == ["body", "[field]"] for item in detail[:-1])
    assert detail[-1] == {
        "type": "too_many_errors",
        "loc": ["request"],
        "msg": "Additional validation errors omitted.",
    }


def test_task_run_trace_artifact_api_round_trip(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        task_response = client.post(
            "/tasks",
            json={
                "title": "Add project health check",
                "goal": "Implement a small code change with tests and review.",
                "workflow_pack": "code_rd",
                "inputs": {"repository_path": "workspace/app"},
                "constraints": ["Do not call real shell commands."],
                "acceptance_criteria": ["Final summary includes test and review status."],
            },
        )

        assert task_response.status_code == 201
        task = task_response.json()
        assert task["workflow_pack"] == "code_rd"

        assert client.get("/tasks").json()[0]["id"] == task["id"]
        assert client.get(f"/tasks/{task['id']}").json()["goal"] == task["goal"]

        run_response = client.post("/runs", json={"task_id": task["id"]})

        assert run_response.status_code == 201
        run = run_response.json()
        assert run["task_id"] == task["id"]
        assert run["status"] == "completed"
        assert run["final_artifact_id"]

        assert client.get("/runs").json()[0]["id"] == run["id"]
        assert client.get(f"/runs/{run['id']}").json()["status"] == "completed"

        agent_runs = client.get(f"/runs/{run['id']}/agent-runs").json()
        assert [agent_run["step_name"] for agent_run in agent_runs] == [
            "clarify_requirements",
            "design_implementation",
            "prepare_patch",
            "test_changes",
            "review_delivery",
            "finalize_delivery",
        ]
        assert {agent_run["status"] for agent_run in agent_runs} == {"completed"}
        assert {agent_run["run_id"] for agent_run in agent_runs} == {run["id"]}
        handoffs = client.get(f"/runs/{run['id']}/handoffs").json()
        assert len(handoffs) == 5
        for index, handoff in enumerate(handoffs):
            assert handoff["from_agent_run_id"] == agent_runs[index]["id"]
            assert handoff["to_agent_id"] == agent_runs[index + 1]["agent_id"]
            assert handoff["next_objective"] == agent_runs[index + 1]["step_name"]

        trace = client.get(f"/runs/{run['id']}/trace").json()
        assert {event["event_type"] for event in trace} >= {"model_action", "artifact_created", "handoff", "eval_result"}
        assert "runtime_event" not in {event["event_type"] for event in trace}
        model_requests = [
            event for event in trace if event["event_type"] == "model_action" and event["payload"].get("action") == "model_request"
        ]
        model_responses = [
            event for event in trace if event["event_type"] == "model_action" and event["payload"].get("action") == "model_response"
        ]
        model_request_starts = [
            event
            for event in trace
            if event["event_type"] == "model_action" and event["payload"].get("action") == "model_request_started"
        ]
        assert {
            event["payload"].get("action")
            for event in trace
            if event["event_type"] == "model_action"
        } == {"model_request_started", "model_request", "model_response"}
        assert len(model_request_starts) == 6
        assert len(model_requests) == 6
        assert len(model_responses) == 6
        assert all("configured_reasoning_effort" not in event["payload"] for event in model_request_starts)
        assert {event["payload"]["provider"] for event in model_requests} == {"mock"}
        assert {event["payload"]["model"] for event in model_requests} == {
            "mock-code-planner",
            "mock-code-builder",
            "mock-code-reviewer",
        }
        assert all(event["payload"]["adapter"] == "mock" for event in model_requests)
        assert all(event["payload"]["mocked"] is True for event in model_requests)
        assert all(event["payload"]["agent_id"] for event in model_requests)
        assert all(event["payload"]["step_name"] for event in model_requests)
        assert {event["payload"]["model"] for event in model_responses} == {
            "mock-code-planner",
            "mock-code-builder",
            "mock-code-reviewer",
        }
        assert all(event["payload"]["adapter"] == "mock" for event in model_responses)
        assert all(event["payload"]["mocked"] is True for event in model_responses)
        assert all(event["payload"]["agent_id"] for event in model_responses)
        assert all(event["payload"]["step_name"] for event in model_responses)
        assert all(event["payload"]["usage"]["output_tokens"] > 0 for event in model_responses)
        assert all(event["payload"]["latency_ms"] >= 1 for event in model_responses)
        eval_results = client.get(f"/runs/{run['id']}/eval-results").json()
        assert {result["status"] for result in eval_results} == {"pass"}
        assert "final_delivery_summary_exists" in {result["check_name"] for result in eval_results}

        artifacts = client.get(f"/runs/{run['id']}/artifacts").json()
        assert [artifact["type"] for artifact in artifacts] == [
            "source_summary",
            "design_doc",
            "patch",
            "test_report",
            "research_note",
            "final_report",
        ]
        artifact_response = client.get(f"/artifacts/{run['final_artifact_id']}")
        assert artifact_response.status_code == 200
        payload = artifact_response.json()
        assert payload["artifact"]["id"] == run["final_artifact_id"]
        assert "finalize_delivery" in payload["content"]

        detail = client.get(f"/runs/{run['id']}/detail").json()
        assert detail["run"] == run
        assert detail["task"] == task
        assert detail["agent_runs"] == agent_runs
        assert detail["handoffs"] == handoffs
        assert detail["trace"] == trace
        assert detail["artifacts"] == artifacts
        assert detail["eval_results"] == eval_results
        assert detail["runtime_sessions"] == []
        assert detail["runtime_jobs"] == []
        assert len(detail["queue_state"]) == 1
        assert detail["queue_state"][0]["action"] == "start_run"
        assert detail["queue_state"][0]["status"] == "completed"
        assert detail["queue_state"][0]["local_only"] is True
        assert detail["queue_state"][0]["background_worker_started"] is False
        assert len(detail["lock_state"]) == 1
        assert detail["lock_state"][0]["status"] == "released"
        assert detail["lock_state"][0]["resource_type"] == "run"
        assert detail["lock_state"][0]["local_only"] is True

        assert client.get(f"/runs/{run['id']}/queue-state").json() == detail["queue_state"]
        assert client.get(f"/runs/{run['id']}/lock-state").json() == detail["lock_state"]


def test_task_intake_analyze_recommends_pack_without_creating_task_or_run(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        response = client.post(
            "/task-intake/analyze",
            json={
                "title": "给 API 加 JWT 认证",
                "goal": "为 FastAPI 接口增加 JWT authentication 和权限检查，并补测试。",
                "inputs": {"focus_paths": ["app/api.py", "app/tests"]},
                "constraints": ["不要泄漏 token"],
                "acceptance_criteria": ["认证失败返回 401", "pytest 通过"],
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["task_type"] in {"feature", "test"}
        assert payload["complexity"] == "L"
        assert payload["risk"] == "high"
        assert payload["domain"] == "security"
        assert payload["recommended_pack"] == "code_rd_institutional"
        assert 0 < payload["confidence"] <= 1
        assert any("高风险" in constraint for constraint in payload["constraints"])
        assert client.get("/tasks").json() == []
        assert client.get("/runs").json() == []


def test_create_task_auto_pack_routes_complex_work_to_institutional_pack(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            json={
                "title": "给 API 加 JWT 认证",
                "goal": "为 FastAPI 接口增加 JWT authentication 和权限检查，并补测试。",
                "inputs": {"focus_paths": ["app/api.py", "app/tests"]},
                "constraints": ["不要泄漏 token"],
                "acceptance_criteria": ["认证失败返回 401", "pytest 通过"],
            },
        )

        assert response.status_code == 201
        task = response.json()
    assert task["workflow_pack"] == "code_rd_institutional"


def test_create_task_rejects_secret_like_inputs_before_storage(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            json={
                "title": "Secret input",
                "goal": "Exercise task input boundary.",
                "workflow_pack": "research",
                "inputs": {
                    "nested": {
                        "OPENAI_API_KEY": "sk-secret-value",
                    }
                },
            },
        )

        assert response.status_code == 400
        dumped = json.dumps(response.json())
        assert "sensitive task content" in dumped.lower()
        assert "sk-secret-value" not in dumped
        assert client.get("/tasks").json() == []


def test_create_task_rejects_secret_like_goal_before_storage(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            json={
                "title": "Secret goal",
                "goal": "Use Authorization: Bearer sk-secret-value for the request.",
                "workflow_pack": "research",
            },
        )

        assert response.status_code == 400
        dumped = json.dumps(response.json())
        assert "sensitive task content" in dumped.lower()
        assert "sk-secret-value" not in dumped
        assert client.get("/tasks").json() == []


@pytest.mark.parametrize(
    "secret_value",
    [
        "ghp_" + ("a" * 32),
        "xoxb-" + ("1" * 12) + "-" + ("2" * 12) + "-" + ("a" * 22),
        "AK" + "IA" + ("A" * 16),
        ".".join(("eyJ" + ("a" * 33), "eyJ" + ("b" * 33), "c" * 32)),
    ],
)
def test_create_task_rejects_common_token_shapes_before_storage(tmp_path, secret_value: str) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            json={
                "title": "Secret token",
                "goal": "Exercise common token detection.",
                "workflow_pack": "research",
                "inputs": {"note": f"use this credential {secret_value}"},
            },
        )

        assert response.status_code == 400
        dumped = json.dumps(response.json())
        assert "sensitive task content" in dumped.lower()
        assert secret_value not in dumped
        assert client.get("/tasks").json() == []


def test_create_task_allows_security_topic_without_secret_value(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        response = client.post(
            "/tasks",
            json={
                "title": "Review credential handling",
                "goal": "Review API key handling, token storage, credentials, and auth boundary.",
                "workflow_pack": "code_rd",
                "constraints": ["Do not leak tokens."],
            },
        )

        assert response.status_code == 201


def test_auto_complex_task_run_uses_institutional_subagent_branches(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "重构跨模块认证流程",
                "goal": "跨模块实现 auth token 校验、权限检查和测试，保留人工确认点。",
                "workflow_pack": "auto",
                "inputs": {"focus_paths": ["app/api.py", "app/core"]},
                "constraints": ["不得泄漏 secret 或 token"],
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()

        assert task["workflow_pack"] == "code_rd_institutional"
        assert run["status"] == "waiting"
        assert run["current_step"] == "prepare_patch"
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        assert {job["step_name"] for job in jobs if job["status"] == "approval_required"} == {
            "prepare_patch"
        }
        ready_event = next(
            event
            for event in client.get(f"/runs/{run['id']}/trace").json()
            if event["event_type"] == "workflow_event"
            and event["payload"].get("action") == "ready_batches_planned"
        )
        execution_batches = [
            batch
            for batch in ready_event["payload"]["batches"]
            if set(batch["steps"]) & {"prepare_patch", "test_changes"}
        ]
        assert [batch["steps"] for batch in execution_batches] == [["prepare_patch"], ["test_changes"]]
        assert all(batch["parallel_candidate"] is False for batch in execution_batches)
        assert ready_event["payload"]["true_parallel_execution"] is False


def test_pack_and_agent_catalog_endpoints(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        packs = client.get("/workflow-packs").json()
        providers = client.get("/model-providers").json()

        assert {pack["name"] for pack in packs} == {"code_rd", "code_rd_institutional", "research"}
        assert {provider["name"] for provider in providers} == {
            "mock",
            "openai",
            "anthropic",
            "deepseek",
            "litellm_proxy",
            "local",
        }
        assert [provider for provider in providers if provider["enabled"]] == [
            {
                "name": "mock",
                "adapter": "mock",
                "enabled": True,
                "real_calls": False,
                "real_calls_configured": False,
                "requires_credentials": False,
                "description": "Deterministic mocked adapter used for local development and tests.",
            }
        ]
        assert {provider["name"] for provider in providers if provider["real_calls"]} == {
            "openai",
            "deepseek",
            "litellm_proxy",
        }
        assert all(provider["real_calls_configured"] is False for provider in providers)
        assert "model_config" in packs[0]["agents"][0]
        assert all(len({agent["model_config"]["model"] for agent in pack["agents"]}) >= 2 for pack in packs)
        assert all(pack["steps"] for pack in packs)
        assert all(pack["eval_checks"] for pack in packs)
        assert any(step["allowed_tools"] for pack in packs for step in pack["steps"])
        assert any(check["description"] for pack in packs for check in pack["eval_checks"])

        packs_by_name = {pack["name"]: pack for pack in packs}
        code_pack = client.get("/workflow-packs/code_rd").json()
        research_pack = client.get("/workflow-packs/research").json()
        assert code_pack == packs_by_name["code_rd"]
        assert research_pack == packs_by_name["research"]
        assert [step["name"] for step in code_pack["steps"]] == [
            "clarify_requirements",
            "design_implementation",
            "prepare_patch",
            "test_changes",
            "review_delivery",
            "finalize_delivery",
        ]
        assert code_pack["steps"][0]["phase"] == "intake"
        assert code_pack["steps"][0]["produces_artifact_type"] == "source_summary"
        assert "model_config" in code_pack["agents"][0]
        assert {agent["model_config"]["model"] for agent in code_pack["agents"]} == {
            "mock-code-planner",
            "mock-code-builder",
            "mock-code-reviewer",
        }

        institutional_pack = client.get("/workflow-packs/code_rd_institutional").json()
        assert [step["name"] for step in institutional_pack["steps"]] == [
            "read_context",
            "plan_delivery",
            "review_plan",
            "dispatch_work",
            "prepare_patch",
            "test_changes",
            "review_context_alignment",
            "synthesize_delivery",
            "final_review",
            "final_approval",
        ]
        steps_by_name = {step["name"]: step for step in institutional_pack["steps"]}
        assert steps_by_name["read_context"]["agent_role"] == "ContextReader"
        assert steps_by_name["plan_delivery"]["depends_on"] == ["read_context"]
        assert steps_by_name["dispatch_work"]["coordination_role"] == "controller"
        assert steps_by_name["prepare_patch"]["depends_on"] == ["dispatch_work"]
        assert steps_by_name["prepare_patch"]["coordination_role"] == "subagent"
        assert steps_by_name["prepare_patch"]["controller_step"] == "dispatch_work"
        assert steps_by_name["prepare_patch"]["return_contract"]["required_artifact_types"] == ["patch"]
        assert steps_by_name["prepare_patch"]["return_contract"]["require_risk_notes"] is True
        assert steps_by_name["prepare_patch"]["runtime"] == "acp"
        assert steps_by_name["prepare_patch"]["session_policy"]["persistent"] is True
        assert steps_by_name["prepare_patch"]["session_policy"]["requires_approval"] is True
        assert steps_by_name["test_changes"]["coordination_role"] == "subagent"
        assert steps_by_name["test_changes"]["controller_step"] == "dispatch_work"
        assert steps_by_name["test_changes"]["depends_on"] == ["prepare_patch"]
        assert "patch" in steps_by_name["test_changes"]["required_artifacts"]
        assert steps_by_name["test_changes"]["requires_eval_pass"] is True
        assert steps_by_name["test_changes"]["required_eval_checks"] == ["patched_local_test_command"]
        assert steps_by_name["review_context_alignment"]["agent_role"] == "ContextReviewer"
        assert steps_by_name["review_context_alignment"]["depends_on"] == ["prepare_patch", "test_changes"]
        assert steps_by_name["synthesize_delivery"]["coordination_role"] == "synthesizer"
        assert steps_by_name["synthesize_delivery"]["runtime"] == "session"
        assert steps_by_name["synthesize_delivery"]["depends_on"] == ["review_context_alignment"]
        assert steps_by_name["final_review"]["agent_role"] == "FinalReviewer"
        assert steps_by_name["final_approval"]["agent_role"] == "FinalApprover"
        assert steps_by_name["final_approval"]["depends_on"] == ["final_review"]

        assert len(client.get("/agents").json()) == 22
        code_agents = client.get("/agents", params={"pack_name": "code_rd"}).json()
        assert [agent["role"] for agent in code_agents] == [
            "Clarifier",
            "Architect",
            "Coder",
            "Tester",
            "Reviewer",
            "Finalizer",
        ]
        assert "model_config" in code_agents[0]
        assert {agent["model_config"]["model"] for agent in code_agents} == {
            "mock-code-planner",
            "mock-code-builder",
            "mock-code-reviewer",
        }


def test_model_routing_config_is_reflected_in_agent_catalog_and_trace(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    routing_path = tmp_path / "model-routing.json"
    routing_path.write_text(
        json.dumps(
            {
                "agents": {
                    "code_rd-coder": {
                        "provider": "mock",
                        "model": "mock-routed-coder",
                        "temperature": 0.1,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TEAM_AGENT_MODEL_ROUTING_CONFIG", str(routing_path))

    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        code_agents = client.get("/agents", params={"pack_name": "code_rd"}).json()
        coder = next(agent for agent in code_agents if agent["id"] == "code_rd-coder")
        assert coder["model_config"]["provider"] == "mock"
        assert coder["model_config"]["model"] == "mock-routed-coder"

        task = client.post(
            "/tasks",
            json={
                "title": "Routed model run",
                "goal": "Exercise model routing.",
                "workflow_pack": "code_rd",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        trace = client.get(f"/runs/{run['id']}/trace").json()
        requests = [
            event
            for event in trace
            if event["event_type"] == "model_action" and event["payload"].get("action") == "model_request"
        ]

        assert any(
            event["payload"]["agent_id"] == "code_rd-coder"
            and event["payload"]["provider"] == "mock"
            and event["payload"]["model"] == "mock-routed-coder"
            for event in requests
        )
        assert "secret" not in json.dumps(trace).lower()
        assert "api_key" not in json.dumps(trace).lower()


def test_role_card_api_can_create_update_delete_and_bind_agents(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAM_AGENT_MODEL_ROUTING_CONFIG", raising=False)
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    monkeypatch.chdir(tmp_path)
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")

    with TestClient(app) as client:
        assert client.get("/role-cards").json() == []

        create_response = client.put(
            "/role-cards/code-reviewer",
            json={
                "name": "Code Reviewer",
                "description": "Review correctness and security.",
                "color": "purple",
                "emoji": "eye",
                "vibe": "Reviews like a mentor.",
                "content": "# Code Reviewer\n\nReview correctness and security.",
            },
        )

        assert create_response.status_code == 200, create_response.text
        role_card = create_response.json()
        assert role_card["id"] == "code-reviewer"
        assert role_card["path"] == "config/roles/code-reviewer.md"
        assert role_card["frontmatter"]["name"] == "Code Reviewer"
        assert role_card["content"].startswith("# Code Reviewer")
        assert "api_key" not in json.dumps(role_card).lower()

        cards = client.get("/role-cards").json()
        assert [card["id"] for card in cards] == ["code-reviewer"]
        assert cards[0]["content"] == ""
        assert client.get("/role-cards/code-reviewer").json()["content"].startswith("# Code Reviewer")

        bind_response = client.put(
            "/agent-bindings/code_rd-reviewer",
            json={
                "provider": "mock",
                "model": "mock-role-reviewer",
                "temperature": 0.2,
                "max_tokens": 2048,
                "reasoning_effort": "xhigh",
                "role_card_id": "code-reviewer",
            },
        )

        assert bind_response.status_code == 200, bind_response.text
        binding = bind_response.json()
        assert binding == {
            "agent_id": "code_rd-reviewer",
            "provider": "mock",
            "model": "mock-role-reviewer",
            "temperature": 0.2,
            "max_tokens": 2048,
            "reasoning_effort": "xhigh",
            "role_card_id": "code-reviewer",
            "role_file": "roles/code-reviewer.md",
            "allow_real_calls": False,
            "restart_required": True,
        }

        assert client.get("/agent-bindings").json() == [binding]
        routing_path = tmp_path / "config" / "model-routing.local.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        assert routing == {
            "agents": {
                "code_rd-reviewer": {
                    "provider": "mock",
                    "model": "mock-role-reviewer",
                    "temperature": 0.2,
                    "max_tokens": 2048,
                    "reasoning_effort": "xhigh",
                    "role_file": "roles/code-reviewer.md",
                }
            }
        }
        assert "allow_real_calls" not in json.dumps(routing)

        loaded_routing = load_model_routing_config(routing_path)
        routed_packs = apply_model_routing_config({"code_rd": get_code_rd_pack()}, loaded_routing)
        reviewer = next(agent for agent in routed_packs["code_rd"].agents if agent.id == "code_rd-reviewer")
        assert reviewer.model_settings == {
            "provider": "mock",
            "model": "mock-role-reviewer",
            "temperature": 0.2,
            "max_tokens": 2048,
            "reasoning_effort": "xhigh",
        }
        assert reviewer.system_prompt.startswith("# Code Reviewer")
        assert "name: Code Reviewer" not in reviewer.system_prompt

        delete_response = client.delete("/role-cards/code-reviewer")

        assert delete_response.status_code == 200
        assert client.get("/role-cards").json() == []
        assert json.loads(routing_path.read_text(encoding="utf-8")) == {
            "agents": {
                "code_rd-reviewer": {
                    "provider": "mock",
                    "model": "mock-role-reviewer",
                    "temperature": 0.2,
                    "max_tokens": 2048,
                    "reasoning_effort": "xhigh",
                }
            }
        }


def test_agent_binding_defaults_real_provider_reasoning_effort_to_xhigh(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_AGENT_MODEL_ROUTING_CONFIG", raising=False)
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.chdir(tmp_path)
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")

    with TestClient(app) as client:
        bind_response = client.put(
            "/agent-bindings/code_rd-reviewer",
            json={
                "provider": "litellm_proxy",
                "model": "gpt5.5",
                "allow_real_calls": True,
            },
        )

        assert bind_response.status_code == 200, bind_response.text
        binding = bind_response.json()
        assert binding["reasoning_effort"] == "xhigh"
        routing_path = tmp_path / "config" / "model-routing.local.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        assert routing["agents"]["code_rd-reviewer"]["reasoning_effort"] == "xhigh"


def test_role_card_api_rejects_secret_content_bad_paths_unknown_agents_and_real_routes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_AGENT_MODEL_ROUTING_CONFIG", raising=False)
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    monkeypatch.chdir(tmp_path)
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")

    with TestClient(app) as client:
        assert (
            client.put(
                "/role-cards/bad%20id",
                json={"name": "Bad", "description": "", "content": "Bad"},
            ).status_code
            == 422
        )
        allowed_security_terms_response = client.put(
            "/role-cards/security-reviewer",
            json={
                "name": "Security Reviewer",
                "description": "Reviews auth checks and credential handling.",
                "content": (
                    "# Security Reviewer\n\n"
                    "Review API key handling, token storage, credentials, "
                    "Authorization headers, and secret leakage risks."
                ),
            },
        )
        assert allowed_security_terms_response.status_code == 200, allowed_security_terms_response.text
        assert (
            client.put(
                "/role-cards/secret-reviewer",
                json={
                    "name": "Secret Reviewer",
                    "description": "",
                    "content": "OPENAI_API_KEY=sk-secret",
                },
            ).status_code
            == 400
        )
        assert (
            client.put(
                "/role-cards/huge-reviewer",
                json={
                    "name": "Huge Reviewer",
                    "description": "",
                    "content": "x" * (64 * 1024 + 1),
                },
            ).status_code
            == 422
        )
        assert (
            client.put(
                "/agent-bindings/missing-agent",
                json={"provider": "mock", "model": "mock-model"},
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/agent-bindings/code_rd-reviewer",
                json={"provider": "openai", "model": "gpt-reviewer", "allow_real_calls": True},
            ).status_code
            == 400
        )
        assert (
            client.put(
                "/agent-bindings/code_rd-reviewer",
                json={"provider": "unknown", "model": "x"},
            ).status_code
            == 400
        )
        assert not (tmp_path / "config" / "model-routing.local.json").exists()


def test_api_validates_request_bodies(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        assert (
            client.post(
                "/tasks",
                json={"title": "", "goal": "Bad", "workflow_pack": "code_rd"},
            ).status_code
            == 422
        )
        assert client.post("/runs", json={"task_id": ""}).status_code == 422


def test_research_run_via_api(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Research multi-agent harness patterns",
                "goal": "Produce a sourced research report about multi-agent harness architecture.",
                "workflow_pack": "research",
                "inputs": {"recency": "not required"},
            },
        ).json()

        run_response = client.post("/runs", json={"task_id": task["id"]})

        assert run_response.status_code == 201
        run = run_response.json()
        assert run["status"] == "completed"
        artifacts = client.get(f"/runs/{run['id']}/artifacts").json()
        assert [artifact["type"] for artifact in artifacts] == [
            "design_doc",
            "source_summary",
            "research_note",
            "test_report",
            "final_report",
            "research_note",
        ]
        assert run["final_artifact_id"] == artifacts[-2]["id"]
        trace = client.get(f"/runs/{run['id']}/trace").json()
        model_requests = [
            event for event in trace if event["event_type"] == "model_action" and event["payload"].get("action") == "model_request"
        ]
        assert {event["payload"]["model"] for event in model_requests} == {
            "mock-research-planner",
            "mock-research-reader",
            "mock-research-verifier",
            "mock-research-writer",
        }


def test_code_rd_institutional_run_via_api_records_dependency_handoffs(tmp_path) -> None:
    repo = tmp_path / "institutional_repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import answer\n\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        state = app.state.harness
        state.executor_factory = lambda: PackMappedExecutor(
            model_gateway=ModelGateway({"mock": PatchProducingAdapter()}),
            artifact_store=state.artifact_store,
            trace_logger=state.trace_logger,
            web_tool_provider=state.web_tool_provider,
            browser_tool_provider=state.browser_tool_provider,
            skill_library=state.skill_library,
        )
        task = client.post(
            "/tasks",
            json={
                "title": "Institutional code delivery",
                "goal": "Exercise planning, review, dispatch, execution branches, synthesis, and final review.",
                "workflow_pack": "code_rd_institutional",
                "inputs": {
                    "repository_path": str(repo),
                    "focus_paths": ["app.py", "test_app.py"],
                    "test_command": "python -m pytest -q",
                    "allow_host_test_execution": True,
                },
            },
        ).json()

        run_response = client.post("/runs", json={"task_id": task["id"]})

        assert run_response.status_code == 201
        run = run_response.json()
        assert run["status"] == "waiting"
        assert run["current_step"] == "prepare_patch"
        agent_runs = client.get(f"/runs/{run['id']}/agent-runs").json()
        assert [agent_run["step_name"] for agent_run in agent_runs] == [
            "read_context",
            "plan_delivery",
            "review_plan",
            "dispatch_work",
            "prepare_patch",
        ]
        assert {agent_run["step_name"] for agent_run in agent_runs if agent_run["status"] == "waiting"} == {
            "prepare_patch",
        }

        artifacts = client.get(f"/runs/{run['id']}/artifacts").json()
        assert [artifact["type"] for artifact in artifacts] == [
            "source_summary",
            "design_doc",
            "research_note",
            "research_note",
        ]
        assert run["final_artifact_id"] is None
        runtime_sessions = client.get(f"/runs/{run['id']}/runtime-sessions").json()
        runtime_jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        assert len(runtime_sessions) == 5
        assert len(runtime_jobs) == 5
        assert {session["runtime"] for session in runtime_sessions} == {"session", "acp"}
        assert {job["runtime"] for job in runtime_jobs} == {"session", "acp"}
        acp_jobs = [job for job in runtime_jobs if job["runtime"] == "acp"]
        assert len(acp_jobs) == 1
        assert {job["status"] for job in acp_jobs} == {"approval_required"}
        assert all(job["approval_required"] is True for job in acp_jobs)
        assert all(job["metadata"]["external_runtime_started"] is False for job in runtime_jobs)
        assert {job["status"] for job in runtime_jobs if job["runtime"] == "session"} == {"completed"}

        handoffs = client.get(f"/runs/{run['id']}/handoffs").json()
        assert len(handoffs) == 4
        handoffs_by_from = {}
        for handoff in handoffs:
            handoffs_by_from.setdefault(handoff["from_agent_run_id"], []).append(handoff)

        dispatch_run = next(agent_run for agent_run in agent_runs if agent_run["step_name"] == "dispatch_work")
        dispatch_handoffs = handoffs_by_from[dispatch_run["id"]]
        assert {handoff["next_objective"] for handoff in dispatch_handoffs} == {"prepare_patch"}

        patch_run = next(agent_run for agent_run in agent_runs if agent_run["step_name"] == "prepare_patch")
        assert patch_run["input_context"]["coordination_role"] == "subagent"
        assert patch_run["input_context"]["controller_step"] == "dispatch_work"
        assert patch_run["input_context"]["return_contract"]["require_risk_notes"] is True
        assert patch_run["input_context"]["runtime"] == "acp"
        assert patch_run["input_context"]["session_policy"]["persistent"] is True
        assert patch_run["input_context"]["session_policy"]["requires_approval"] is True

        jobs_by_step = {job["step_name"]: job for job in runtime_jobs}
        approve_patch = client.post(f"/runs/{run['id']}/runtime-jobs/{jobs_by_step['prepare_patch']['id']}/approve")
        assert approve_patch.status_code == 200
        patch_payload = approve_patch.json()
        assert patch_payload["run"]["status"] == "waiting"
        assert patch_payload["run"]["current_step"] == "test_changes"
        assert patch_payload["runtime_job"]["status"] == "completed"
        assert patch_payload["runtime_job"]["approved_at"]
        assert patch_payload["runtime_job"]["metadata"]["external_runtime_started"] is False

        runtime_jobs_after_patch = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        assert {job["step_name"]: job["status"] for job in runtime_jobs_after_patch if job["runtime"] == "acp"} == {
            "prepare_patch": "completed",
            "test_changes": "approval_required",
        }
        agent_runs_after_patch = client.get(f"/runs/{run['id']}/agent-runs").json()
        test_run = next(agent_run for agent_run in agent_runs_after_patch if agent_run["step_name"] == "test_changes")
        assert test_run["input_context"]["coordination_role"] == "subagent"
        assert test_run["input_context"]["controller_step"] == "dispatch_work"
        assert test_run["input_context"]["return_contract"]["require_risk_notes"] is True
        assert test_run["input_context"]["runtime"] == "acp"
        assert test_run["input_context"]["session_policy"]["persistent"] is True
        assert test_run["input_context"]["session_policy"]["requires_approval"] is True
        handoffs_after_patch = client.get(f"/runs/{run['id']}/handoffs").json()
        patch_to_test = next(
            handoff
            for handoff in handoffs_after_patch
            if handoff["from_agent_run_id"] == patch_run["id"]
            and handoff["next_objective"] == "test_changes"
        )
        assert len(patch_to_test["artifact_refs"]) == 1

        jobs_by_step = {job["step_name"]: job for job in runtime_jobs_after_patch}
        approve_test = client.post(f"/runs/{run['id']}/runtime-jobs/{jobs_by_step['test_changes']['id']}/approve")
        assert approve_test.status_code == 200
        final_payload = approve_test.json()
        assert final_payload["run"]["status"] == "completed"
        assert final_payload["run"]["final_artifact_id"]

        run = final_payload["run"]
        agent_runs = client.get(f"/runs/{run['id']}/agent-runs").json()
        assert [agent_run["step_name"] for agent_run in agent_runs] == [
            "read_context",
            "plan_delivery",
            "review_plan",
            "dispatch_work",
            "prepare_patch",
            "test_changes",
            "review_context_alignment",
            "synthesize_delivery",
            "final_review",
            "final_approval",
        ]
        completed_test_run = next(
            agent_run for agent_run in agent_runs if agent_run["step_name"] == "test_changes"
        )
        assert completed_test_run["input_context"]["dependency_lineage"] == {
            "prepare_patch": {
                "handoff_id": patch_to_test["id"],
                "from_agent_run_id": patch_run["id"],
            }
        }
        artifacts = client.get(f"/runs/{run['id']}/artifacts").json()
        assert [artifact["type"] for artifact in artifacts] == [
            "source_summary",
            "design_doc",
            "research_note",
            "research_note",
            "patch",
            "test_report",
            "research_note",
            "final_report",
            "research_note",
            "final_report",
        ]
        assert run["final_artifact_id"] == artifacts[-1]["id"]
        runtime_sessions = client.get(f"/runs/{run['id']}/runtime-sessions").json()
        runtime_jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        assert len(runtime_sessions) == 10
        assert len(runtime_jobs) == 10
        assert all(job["metadata"]["external_runtime_started"] is False for job in runtime_jobs)
        assert {job["status"] for job in runtime_jobs} == {"completed"}

        handoffs = client.get(f"/runs/{run['id']}/handoffs").json()
        assert len(handoffs) == 10
        synthesize_run = next(agent_run for agent_run in agent_runs if agent_run["step_name"] == "synthesize_delivery")
        assert synthesize_run["input_context"]["coordination_role"] == "synthesizer"
        assert synthesize_run["input_context"]["runtime"] == "session"
        assert set(synthesize_run["input_context"]["required_artifacts"]) == {
            "design_doc",
            "research_note",
            "source_summary",
            "patch",
            "test_report",
        }
        final_approval_run = next(agent_run for agent_run in agent_runs if agent_run["step_name"] == "final_approval")
        assert final_approval_run["input_context"]["coordination_role"] == "controller"
        assert final_approval_run["input_context"]["runtime"] == "session"

        detail = client.get(f"/runs/{run['id']}/detail").json()
        synthesize_detail = next(
            agent_run for agent_run in detail["agent_runs"] if agent_run["step_name"] == "synthesize_delivery"
        )
        assert synthesize_detail["input_context"]["required_artifacts"] == synthesize_run["input_context"]["required_artifacts"]
        assert len(detail["runtime_sessions"]) == len(runtime_sessions)
        assert len(detail["runtime_jobs"]) == len(runtime_jobs)
        assert all("external_ref" not in session for session in detail["runtime_sessions"])
        assert all("external_ref" not in job for job in detail["runtime_jobs"])
        assert all(set(job["metadata"]) == {"external_runtime_started"} for job in detail["runtime_jobs"])
        runtime_events = [
            event for event in detail["trace"] if event["event_type"] == "runtime_event"
        ]
        assert len(runtime_events) >= 11
        assert all(event["payload"]["external_runtime_started"] is False for event in runtime_events)
        assert {"runtime_job_approved", "run_waiting_for_local_approval"}.issubset(
            {event["payload"]["action"] for event in runtime_events}
        )
        assert {"approval_required", "approved", "completed"}.issubset(
            {event["payload"]["job_status"] for event in runtime_events if event["payload"].get("runtime") == "acp"}
        )
        detail_dump = json.dumps(detail).lower()
        assert "api_key" not in detail_dump
        assert "bearer" not in detail_dump
        assert "sk-" not in detail_dump
        assert "lease_token" not in detail_dump
        assert "owner_token" not in detail_dump
        assert "external_ref" not in json.dumps(detail["queue_state"]).lower()
        assert "external_ref" not in json.dumps(detail["lock_state"]).lower()


def test_runtime_job_approval_is_retryable_after_approved_intent_was_persisted(
    tmp_path,
    monkeypatch,
) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Retry approval",
                "goal": "Resume after approval persistence outlives the request.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")
        original_record = state.trace_logger.record
        failures = 0

        def fail_first_approval_trace(*, run_id, event_type, payload, agent_run_id=None, duration_ms=None):
            nonlocal failures
            if payload.get("action") == "runtime_job_approved" and failures == 0:
                failures += 1
                raise StorageError("transient approval trace failure")
            return original_record(
                run_id=run_id,
                event_type=event_type,
                payload=payload,
                agent_run_id=agent_run_id,
                duration_ms=duration_ms,
            )

        monkeypatch.setattr(state.trace_logger, "record", fail_first_approval_trace)

        first = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve")

        assert first.status_code == 400
        persisted_job = state.storage.get_runtime_job(patch_job["id"])
        assert persisted_job is not None
        assert persisted_job.status == RuntimeJobStatus.APPROVED
        assert state.storage.get_run(run["id"]).status.value == "waiting"  # type: ignore[union-attr]

        retry = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve")

        assert retry.status_code == 200, retry.text
        assert retry.json()["run"]["status"] == "waiting"
        assert retry.json()["runtime_job"]["status"] == "completed"
        assert failures == 1
        assert RunQueueItemStatus.FAILED.value not in {
            item.status.value for item in state.storage.list_run_queue_items_for_run(run["id"])
        }


def test_synchronous_approval_rejects_stale_approved_job(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Reject stale synchronous approval",
                "goal": "Only the current runtime job may resume a waiting run.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        first_wait = client.post("/runs", json={"task_id": task["id"]}).json()
        first_job = next(
            job
            for job in client.get(f"/runs/{first_wait['id']}/runtime-jobs").json()
            if job["step_name"] == "prepare_patch"
        )
        second_wait = client.post(
            f"/runs/{first_wait['id']}/runtime-jobs/{first_job['id']}/approve"
        ).json()["run"]
        assert second_wait["status"] == "waiting"
        assert second_wait["current_step"] == "test_changes"

        stale_job = app.state.harness.storage.get_runtime_job(first_job["id"])
        assert stale_job is not None and stale_job.status == RuntimeJobStatus.COMPLETED
        app.state.harness.storage.update_runtime_job(
            stale_job.model_copy(update={"status": RuntimeJobStatus.APPROVED})
        )

        stale_retry = client.post(
            f"/runs/{first_wait['id']}/runtime-jobs/{first_job['id']}/approve"
        )

        assert stale_retry.status_code == 409
        persisted_run = client.get(f"/runs/{first_wait['id']}").json()
        assert persisted_run["status"] == "waiting"
        assert persisted_run["current_step"] == "test_changes"


def test_run_detail_redacts_runtime_session_and_job_sensitive_fields(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Runtime redaction",
                "goal": "Exercise safe run detail runtime summaries.",
                "workflow_pack": "code_rd",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        agent_run = client.get(f"/runs/{run['id']}/agent-runs").json()[0]
        storage = app.state.harness.storage
        storage.create_agent_session(
            AgentSession(
                run_id=run["id"],
                agent_run_id=agent_run["id"],
                agent_id=agent_run["agent_id"],
                step_name=agent_run["step_name"],
                runtime="session",
                external_ref="external-session-secret",
                metadata={"owner_token": "sk-owner-token", "external_runtime_started": True},
            )
        )
        storage.create_runtime_job(
            RuntimeJob(
                run_id=run["id"],
                agent_run_id=agent_run["id"],
                step_name=agent_run["step_name"],
                runtime="acp",
                external_ref="external-job-secret",
                metadata={"lease_token": "sk-lease-token", "external_runtime_started": True},
            )
        )

        detail = client.get(f"/runs/{run['id']}/detail").json()
        runtime_sessions = client.get(f"/runs/{run['id']}/runtime-sessions").json()
        runtime_jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()

        dumped = json.dumps(
            {
                "detail": detail,
                "runtime_sessions": runtime_sessions,
                "runtime_jobs": runtime_jobs,
            }
        )
        assert "external-session-secret" not in dumped
        assert "external-job-secret" not in dumped
        assert "owner_token" not in dumped
        assert "lease_token" not in dumped
        assert "sk-owner-token" not in dumped
        assert "sk-lease-token" not in dumped
        assert any(job["metadata"] == {"external_runtime_started": True} for job in detail["runtime_jobs"])
        assert all("external_ref" not in session for session in runtime_sessions)
        assert all("metadata" not in session for session in runtime_sessions)
        assert all("external_ref" not in job for job in runtime_jobs)
        assert any(job["metadata"] == {"external_runtime_started": True} for job in runtime_jobs)


def test_code_rd_institutional_session_step_falls_back_to_mock_on_provider_failure(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_MODEL_FALLBACK_TO_MOCK", "1")
    adapter = DispatchFailingAdapter()
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        state = app.state.harness
        state.executor_factory = lambda: PackMappedExecutor(
            model_gateway=ModelGateway(
                {
                    "litellm_proxy": adapter,
                    "mock": PatchProducingAdapter(),
                }
            ),
            artifact_store=state.artifact_store,
            trace_logger=state.trace_logger,
            web_tool_provider=state.web_tool_provider,
            browser_tool_provider=state.browser_tool_provider,
            skill_library=state.skill_library,
        )
        pack = state.packs["code_rd_institutional"]
        state.packs["code_rd_institutional"] = pack.model_copy(
            update={
                "agents": [
                    agent.model_copy(
                        update={
                            "model_settings": {
                                "provider": "litellm_proxy",
                                "model": "gpt5.5",
                            }
                        }
                    )
                    for agent in pack.agents
                ]
            }
        )

        task = client.post(
            "/tasks",
            json={
                "title": "Institutional fallback",
                "goal": "Fallback the dispatch step when the provider fails.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()

        run = client.post("/runs", json={"task_id": task["id"]}).json()

        assert run["status"] == "waiting"
        assert run["current_step"] == "prepare_patch"
        assert adapter.calls == ["read_context", "plan_delivery", "review_plan", "dispatch_work"]
        detail = client.get(f"/runs/{run['id']}/detail").json()
        fallback_events = [
            event
            for event in detail["trace"]
            if event["event_type"] == "workflow_event"
            and event["payload"].get("action") == "model_provider_fallback"
        ]
        assert len(fallback_events) == 1
        fallback_payload = fallback_events[0]["payload"]
        assert fallback_payload["step_name"] == "dispatch_work"
        assert fallback_payload["failed_provider"] == "litellm_proxy"
        assert fallback_payload["failed_model"] == "gpt5.5"
        assert fallback_payload["fallback_provider"] == "mock"
        assert fallback_payload["fallback_model"] == "mock-model"
        assert fallback_payload["elapsed_ms"] == 29000
        assert fallback_payload["error_summary"] == "classification=provider_error;retryable=false"
        dumped_detail = json.dumps(detail)
        assert "Fallback: real provider failed; mock response used." in dumped_detail
        assert "sk-secret" not in dumped_detail
        assert "payload=secret" not in dumped_detail
        model_requests = [
            event
            for event in detail["trace"]
            if event["event_type"] == "model_action"
            and event["payload"].get("action") == "model_request"
        ]
        dispatch_request = next(
            event for event in model_requests if event["payload"]["step_name"] == "dispatch_work"
        )
        assert dispatch_request["payload"]["provider"] == "mock"
        assert dispatch_request["payload"]["mocked"] is True


def test_code_rd_institutional_provider_failure_is_fail_closed_by_default(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("TEAM_AGENT_ALLOW_MODEL_FALLBACK_TO_MOCK", raising=False)
    adapter = DispatchFailingAdapter()
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        state = app.state.harness
        state.executor_factory = lambda: PackMappedExecutor(
            model_gateway=ModelGateway(
                {
                    "litellm_proxy": adapter,
                    "mock": PatchProducingAdapter(),
                }
            ),
            artifact_store=state.artifact_store,
            trace_logger=state.trace_logger,
            web_tool_provider=state.web_tool_provider,
            browser_tool_provider=state.browser_tool_provider,
            skill_library=state.skill_library,
        )
        pack = state.packs["code_rd_institutional"]
        state.packs["code_rd_institutional"] = pack.model_copy(
            update={
                "agents": [
                    agent.model_copy(
                        update={
                            "model_settings": {
                                "provider": "litellm_proxy",
                                "model": "gpt5.5",
                            }
                        }
                    )
                    for agent in pack.agents
                ]
            }
        )
        task = client.post(
            "/tasks",
            json={
                "title": "Institutional fail closed",
                "goal": "Do not hide a real provider failure behind mock output.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()

        run = client.post("/runs", json={"task_id": task["id"]}).json()

        assert run["status"] == "failed"
        assert run["current_step"] == "dispatch_work"
        detail = client.get(f"/runs/{run['id']}/detail").json()
        assert not any(
            event["payload"].get("action") == "model_provider_fallback"
            for event in detail["trace"]
        )
        dispatch_run = next(
            agent_run
            for agent_run in detail["agent_runs"]
            if agent_run["step_name"] == "dispatch_work"
        )
        assert dispatch_run["status"] == "failed"


def test_run_api_requires_server_side_confirmation_for_real_model_routes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    adapter = PatchProducingAdapter()
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        state = app.state.harness
        state.executor_factory = lambda: PackMappedExecutor(
            model_gateway=ModelGateway({"litellm_proxy": adapter}),
            artifact_store=state.artifact_store,
            trace_logger=state.trace_logger,
            web_tool_provider=state.web_tool_provider,
            browser_tool_provider=state.browser_tool_provider,
            skill_library=state.skill_library,
        )
        pack = state.packs["code_rd"]
        state.packs["code_rd"] = pack.model_copy(
            update={
                "agents": [
                    agent.model_copy(
                        update={"model_settings": {"provider": "litellm_proxy", "model": "gpt5.5"}}
                    )
                    for agent in pack.agents
                ]
            }
        )
        task = client.post(
            "/tasks",
            json={"title": "Real model confirmation", "goal": "Require explicit API confirmation.", "workflow_pack": "code_rd"},
        ).json()

        rejected = client.post("/runs", json={"task_id": task["id"]})
        accepted = client.post("/runs", json={"task_id": task["id"], "confirm_real_models": True})

        assert rejected.status_code == 400
        assert "confirm_real_models" in rejected.text
        assert accepted.status_code == 201


def test_code_rd_institutional_local_code_executor_via_api(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_AGENT_MODEL_ROUTING_CONFIG", raising=False)
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import answer\n\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    secret = repo / ".env"
    secret.write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    original_source = source.read_text(encoding="utf-8")

    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        state = app.state.harness
        state.executor_factory = lambda: PackMappedExecutor(
            model_gateway=ModelGateway({"mock": PatchProducingAdapter()}),
            artifact_store=state.artifact_store,
            trace_logger=state.trace_logger,
            web_tool_provider=state.web_tool_provider,
            browser_tool_provider=state.browser_tool_provider,
            skill_library=state.skill_library,
        )
        task = client.post(
            "/tasks",
            json={
                "title": "Local code executor smoke",
                "goal": "Inspect the repository and produce a patch proposal plus test report.",
                "workflow_pack": "code_rd_institutional",
                "inputs": {
                    "repository_path": str(repo),
                    "focus_paths": ["app.py", "test_app.py"],
                    "test_command": "python -m pytest -q",
                    "allow_host_test_execution": True,
                },
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        assert run["status"] == "waiting"

        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")
        patch_response = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve")
        assert patch_response.status_code == 200, patch_response.text
        patch_payload = patch_response.json()
        assert patch_payload["run"]["status"] == "waiting"

        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        test_job = next(job for job in jobs if job["step_name"] == "test_changes")
        test_response = client.post(f"/runs/{run['id']}/runtime-jobs/{test_job['id']}/approve")
        assert test_response.status_code == 200, test_response.text
        final_payload = test_response.json()
        assert final_payload["run"]["status"] == "completed"

        artifacts = client.get(f"/runs/{run['id']}/artifacts").json()
        patch_artifact = next(artifact for artifact in artifacts if artifact["type"] == "patch")
        test_artifact = next(artifact for artifact in artifacts if artifact["type"] == "test_report")
        patch_content = client.get(f"/artifacts/{patch_artifact['id']}").json()["content"]
        test_content = client.get(f"/artifacts/{test_artifact['id']}").json()["content"]

        assert source.read_text(encoding="utf-8") == original_source
        assert "Source repository write requested: `false`" in patch_content
        assert str(repo.resolve()) not in patch_content
        assert "app.py" in patch_content
        assert ".env" not in patch_content
        assert "sk-secret" not in patch_content
        assert f"Tested patch artifact: `{patch_artifact['id']}`" in test_content
        assert "Patch applied to isolated workspace: `true`" in test_content
        assert "Test command passed" in test_content
        assert "1 passed" in test_content

        detail = client.get(f"/runs/{run['id']}/detail").json()
        trace_dump = json.dumps(detail["trace"]).lower()
        assert "sk-secret" not in trace_dump
        assert "api_key" not in trace_dump


def test_code_rd_institutional_breaking_patch_fails_before_review(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_AGENT_MODEL_ROUTING_CONFIG", raising=False)
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import answer\n\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    adapter = PatchProducingAdapter(old_value=42, new_value=43)
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        state = app.state.harness
        state.executor_factory = lambda: PackMappedExecutor(
            model_gateway=ModelGateway({"mock": adapter}),
            artifact_store=state.artifact_store,
            trace_logger=state.trace_logger,
            web_tool_provider=state.web_tool_provider,
            browser_tool_provider=state.browser_tool_provider,
            skill_library=state.skill_library,
        )
        task = client.post(
            "/tasks",
            json={
                "title": "Breaking patch gate",
                "goal": "Reject a patch that breaks a previously passing test.",
                "workflow_pack": "code_rd_institutional",
                "inputs": {
                    "repository_path": str(repo),
                    "focus_paths": ["app.py", "test_app.py"],
                    "test_command": "python -m pytest -q",
                    "allow_host_test_execution": True,
                },
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        patch_job = next(
            job
            for job in client.get(f"/runs/{run['id']}/runtime-jobs").json()
            if job["step_name"] == "prepare_patch"
        )
        assert client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve").status_code == 200
        test_job = next(
            job
            for job in client.get(f"/runs/{run['id']}/runtime-jobs").json()
            if job["step_name"] == "test_changes"
        )

        test_response = client.post(f"/runs/{run['id']}/runtime-jobs/{test_job['id']}/approve")

        assert test_response.status_code == 400
        failed_run = client.get(f"/runs/{run['id']}").json()
        assert failed_run["status"] == "failed"
        assert failed_run["current_step"] == "test_changes"
        test_agent_run = next(
            agent_run
            for agent_run in client.get(f"/runs/{run['id']}/agent-runs").json()
            if agent_run["step_name"] == "test_changes"
        )
        assert test_agent_run["status"] == "failed"
        assert not any(
            handoff["from_agent_run_id"] == test_agent_run["id"]
            and handoff["next_objective"] == "review_context_alignment"
            for handoff in client.get(f"/runs/{run['id']}/handoffs").json()
        )
        assert source.read_text(encoding="utf-8") == "def answer():\n    return 42\n"
        assert adapter.calls.count("test_changes") == 0


def test_code_rd_institutional_writeback_requires_explicit_writeback_api(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_AGENT_MODEL_ROUTING_CONFIG", raising=False)
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import answer\n\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )

    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=lambda: PackMappedExecutor(
            ModelGateway({"mock": PatchProducingAdapter()})
        ),
    )
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Explicit writeback smoke",
                "goal": "Change answer from 41 to 42.",
                "workflow_pack": "code_rd_institutional",
                "inputs": {
                    "repository_path": str(repo),
                    "focus_paths": ["app.py", "test_app.py"],
                    "test_command": "python -m pytest -q",
                    "allow_host_test_execution": True,
                },
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")

        approve_patch = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve")
        assert approve_patch.status_code == 200, approve_patch.text
        assert source.read_text(encoding="utf-8") == "def answer():\n    return 41\n"

        artifacts = client.get(f"/runs/{run['id']}/artifacts").json()
        patch_artifact = next(artifact for artifact in artifacts if artifact["type"] == "patch")
        preview_response = client.post(
            f"/runs/{run['id']}/writeback/preview",
            json={"patch_artifact_id": patch_artifact["id"]},
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["files_changed"][0]["path"] == "app.py"
        assert source.read_text(encoding="utf-8") == "def answer():\n    return 41\n"

        approve_writeback = client.post(
            f"/runs/{run['id']}/writeback/approve",
            json={
                "patch_artifact_id": patch_artifact["id"],
                "writeback_id": preview["writeback_id"],
                "confirm_repository_path": str(repo),
                "confirm_patch_hash": preview["patch_hash"],
                "expected_base_hashes": preview["base_hashes"],
            },
        )
        assert approve_writeback.status_code == 200, approve_writeback.text
        assert source.read_text(encoding="utf-8") == "def answer():\n    return 42\n"
        payload = approve_writeback.json()
        assert payload["applied_files"] == ["app.py"]
        assert payload["test"]["exit_code"] == 0

        detail = client.get(f"/runs/{run['id']}/detail").json()
        trace_dump = json.dumps(detail["trace"])
        assert "writeback_previewed" in trace_dump
        assert "writeback_applied" in trace_dump
        assert "return 42" not in trace_dump


def test_writeback_approve_conflicts_on_bad_patch_hash(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_AGENT_MODEL_ROUTING_CONFIG", raising=False)
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    (repo / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=lambda: PackMappedExecutor(
            ModelGateway({"mock": PatchProducingAdapter()})
        ),
    )
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Writeback hash conflict",
                "goal": "Change answer from 41 to 42.",
                "workflow_pack": "code_rd_institutional",
                "inputs": {
                    "repository_path": str(repo),
                    "focus_paths": ["app.py", "test_app.py"],
                    "test_command": "python -m pytest -q",
                    "allow_host_test_execution": True,
                },
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")
        assert client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve").status_code == 200
        artifacts = client.get(f"/runs/{run['id']}/artifacts").json()
        patch_artifact = next(artifact for artifact in artifacts if artifact["type"] == "patch")
        preview = client.post(
            f"/runs/{run['id']}/writeback/preview",
            json={"patch_artifact_id": patch_artifact["id"]},
        ).json()

        response = client.post(
            f"/runs/{run['id']}/writeback/approve",
            json={
                "patch_artifact_id": patch_artifact["id"],
                "writeback_id": preview["writeback_id"],
                "confirm_repository_path": str(repo),
                "confirm_patch_hash": "bad-hash",
                "expected_base_hashes": preview["base_hashes"],
            },
        )

        assert response.status_code == 409
        assert source.read_text(encoding="utf-8") == "def answer():\n    return 41\n"


def test_writeback_api_rejects_allow_without_tests_bypass(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_AGENT_MODEL_ROUTING_CONFIG", raising=False)
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")

    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=lambda: PackMappedExecutor(
            ModelGateway({"mock": PatchProducingAdapter()})
        ),
    )
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Writeback test bypass",
                "goal": "Change answer from 41 to 42.",
                "workflow_pack": "code_rd_institutional",
                "inputs": {
                    "repository_path": str(repo),
                    "focus_paths": ["app.py"],
                },
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")
        assert client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve").status_code == 200
        artifacts = client.get(f"/runs/{run['id']}/artifacts").json()
        patch_artifact = next(artifact for artifact in artifacts if artifact["type"] == "patch")
        preview = client.post(
            f"/runs/{run['id']}/writeback/preview",
            json={"patch_artifact_id": patch_artifact["id"]},
        ).json()

        response = client.post(
            f"/runs/{run['id']}/writeback/approve",
            json={
                "patch_artifact_id": patch_artifact["id"],
                "writeback_id": preview["writeback_id"],
                "confirm_repository_path": str(repo),
                "confirm_patch_hash": preview["patch_hash"],
                "expected_base_hashes": preview["base_hashes"],
                "allow_without_tests": True,
            },
        )

        assert response.status_code == 422
        assert source.read_text(encoding="utf-8") == "def answer():\n    return 41\n"


def test_runtime_job_reject_and_cancel_are_local_only(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Reject local runtime intent",
                "goal": "Exercise local approval rejection.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")
        raw_patch_job = app.state.harness.storage.get_runtime_job(patch_job["id"])
        assert raw_patch_job is not None
        app.state.harness.storage.update_runtime_job(
            raw_patch_job.model_copy(
                update={
                    "external_ref": "external-job-secret",
                    "metadata": {"lease_token": "sk-lease-token", "external_runtime_started": True},
                }
            )
        )

        reject_response = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/reject")

        assert reject_response.status_code == 200
        payload = reject_response.json()
        dumped_payload = json.dumps(payload)
        assert payload["run"]["status"] == "cancelled"
        assert payload["runtime_job"]["status"] == "rejected"
        assert payload["runtime_job"]["metadata"]["external_runtime_started"] is False
        assert payload["runtime_session"]["status"] == "rejected"
        assert "external_ref" not in dumped_payload
        assert "lease_token" not in dumped_payload
        assert "sk-lease-token" not in dumped_payload

        approve_after_reject = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve")
        assert approve_after_reject.status_code == 409

        task_2 = client.post(
            "/tasks",
            json={
                "title": "Cancel local runtime intent",
                "goal": "Exercise local runtime cancellation.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        run_2 = client.post("/runs", json={"task_id": task_2["id"]}).json()
        jobs_2 = client.get(f"/runs/{run_2['id']}/runtime-jobs").json()
        patch_job_2 = next(job for job in jobs_2 if job["step_name"] == "prepare_patch")
        approve_patch_2 = client.post(
            f"/runs/{run_2['id']}/runtime-jobs/{patch_job_2['id']}/approve"
        )
        assert approve_patch_2.status_code == 200
        jobs_2 = client.get(f"/runs/{run_2['id']}/runtime-jobs").json()
        test_job = next(job for job in jobs_2 if job["step_name"] == "test_changes")
        raw_test_job = app.state.harness.storage.get_runtime_job(test_job["id"])
        assert raw_test_job is not None
        app.state.harness.storage.update_runtime_job(
            raw_test_job.model_copy(
                update={
                    "external_ref": "external-job-secret",
                    "metadata": {"lease_token": "sk-lease-token", "external_runtime_started": True},
                }
            )
        )

        cancel_response = client.post(f"/runs/{run_2['id']}/runtime-jobs/{test_job['id']}/cancel")

        assert cancel_response.status_code == 200
        cancel_payload = cancel_response.json()
        dumped_cancel_payload = json.dumps(cancel_payload)
        assert cancel_payload["run"]["status"] == "cancelled"
        assert cancel_payload["runtime_job"]["status"] == "cancelled"
        assert cancel_payload["runtime_job"]["metadata"]["external_runtime_started"] is False
        assert "external_ref" not in dumped_cancel_payload
        assert "lease_token" not in dumped_cancel_payload
        assert "sk-lease-token" not in dumped_cancel_payload


def test_run_lock_conflict_returns_409_without_approving_runtime_job(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Lock conflict",
                "goal": "Exercise local run lock conflict.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")
        app.state.harness.storage.create_run_lock(
            RunLock(
                id="manual-lock",
                run_id=run["id"],
                owner="test",
                metadata={"lease_token": "sk-secret"},
            )
        )

        approve_response = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve")

        assert approve_response.status_code == 409
        updated_job = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        assert next(job for job in updated_job if job["id"] == patch_job["id"])["status"] == "approval_required"
        detail_dump = json.dumps(client.get(f"/runs/{run['id']}/detail").json()).lower()
        assert "lease_token" not in detail_dump
        assert "sk-secret" not in detail_dump


@pytest.mark.parametrize("action", ["reject", "cancel"])
def test_run_lock_conflict_returns_409_without_terminal_runtime_action(tmp_path, action: str) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": f"Lock conflict {action}",
                "goal": "Exercise local run lock conflict.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")
        app.state.harness.storage.create_run_lock(
            RunLock(
                id="manual-lock",
                run_id=run["id"],
                owner="test",
                metadata={"lease_token": "sk-secret"},
            )
        )

        response = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/{action}")

        assert response.status_code == 409
        current_run = client.get(f"/runs/{run['id']}").json()
        updated_jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        assert current_run["status"] == "waiting"
        assert current_run["current_step"] == "prepare_patch"
        assert next(job for job in updated_jobs if job["id"] == patch_job["id"])["status"] == "approval_required"
        detail_dump = json.dumps(client.get(f"/runs/{run['id']}/detail").json()).lower()
        assert "lease_token" not in detail_dump
        assert "sk-secret" not in detail_dump


def test_runtime_job_approve_response_redacts_sensitive_runtime_fields(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Approve redaction",
                "goal": "Exercise local approval response redaction.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")
        storage = app.state.harness.storage
        raw_job = storage.get_runtime_job(patch_job["id"])
        assert raw_job is not None
        raw_session = storage.get_agent_session(raw_job.agent_session_id)
        assert raw_session is not None
        storage.update_runtime_job(
            raw_job.model_copy(
                update={
                    "external_ref": "external-job-secret",
                    "metadata": {"lease_token": "sk-lease-token", "external_runtime_started": True},
                }
            )
        )
        storage.update_agent_session(
            raw_session.model_copy(
                update={
                    "external_ref": "external-session-secret",
                    "metadata": {"owner_token": "sk-owner-token", "external_runtime_started": True},
                }
            )
        )

        approve_response = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve")

        assert approve_response.status_code == 200
        payload = approve_response.json()
        dumped = json.dumps(payload)
        assert payload["runtime_job"]["metadata"] == {"external_runtime_started": False}
        assert "external_ref" not in dumped
        assert "owner_token" not in dumped
        assert "lease_token" not in dumped
        assert "sk-owner-token" not in dumped
        assert "sk-lease-token" not in dumped
        assert "external-session-secret" not in dumped
        assert "external-job-secret" not in dumped


def test_stale_run_lock_is_recovered_before_runtime_job_approval(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Stale lock recovery",
                "goal": "Recover from an interrupted local approval.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")
        app.state.harness.storage.create_run_lock(
            RunLock(
                id="stale-lock",
                run_id=run["id"],
                owner="test",
                acquired_at=datetime.now(UTC) - timedelta(minutes=20),
            )
        )

        approve_response = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve")

        assert approve_response.status_code == 200, approve_response.text
        locks = client.get(f"/runs/{run['id']}/lock-state").json()
        stale = next(lock for lock in locks if lock["id"] == "stale-lock")
        assert stale["status"] == "released"


def test_api_returns_404_for_missing_resources(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        assert client.get("/tasks/missing").status_code == 404
        assert client.get("/runs/missing").status_code == 404
        assert client.get("/runs/missing/detail").status_code == 404
        assert client.get("/runs/missing/trace").status_code == 404
        assert client.get("/runs/missing/artifacts").status_code == 404
        assert client.get("/runs/missing/agent-runs").status_code == 404
        assert client.get("/runs/missing/handoffs").status_code == 404
        assert client.get("/runs/missing/eval-results").status_code == 404
        assert client.get("/runs/missing/runtime-sessions").status_code == 404
        assert client.get("/runs/missing/runtime-jobs").status_code == 404
        assert client.get("/runs/missing/queue-state").status_code == 404
        assert client.get("/runs/missing/lock-state").status_code == 404
        assert client.post("/runs/missing/runtime-jobs/missing/approve").status_code == 404
        assert client.post("/runs/missing/runtime-jobs/missing/reject").status_code == 404
        assert client.post("/runs/missing/runtime-jobs/missing/cancel").status_code == 404
        assert client.get("/artifacts/missing").status_code == 404
        assert client.get("/workflow-packs/missing").status_code == 404
        assert client.get("/model-providers").status_code == 200
        assert client.get("/agents", params={"pack_name": "missing"}).status_code == 404
        assert (
            client.post(
                "/tasks",
                json={"title": "Bad", "goal": "Bad", "workflow_pack": "missing"},
            ).status_code
            == 404
        )
        assert client.post("/runs", json={"task_id": "missing"}).status_code == 404


def test_api_instances_are_isolated_by_storage_path(tmp_path) -> None:
    first_app = create_app(tmp_path / "first.sqlite3", tmp_path / "first-artifacts")
    second_app = create_app(tmp_path / "second.sqlite3", tmp_path / "second-artifacts")

    with TestClient(first_app) as first_client:
        task_response = first_client.post(
            "/tasks",
            json={"title": "First", "goal": "Only first app can see this.", "workflow_pack": "research"},
        )
        assert task_response.status_code == 201
        assert len(first_client.get("/tasks").json()) == 1

    with TestClient(second_app) as second_client:
        assert second_client.get("/tasks").json() == []


def test_runner_business_failure_returns_failed_run_with_error_trace(tmp_path) -> None:
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=lambda: FailingExecutor(),
    )
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={"title": "Failing run", "goal": "Exercise failed run API semantics.", "workflow_pack": "code_rd"},
        ).json()

        run_response = client.post("/runs", json={"task_id": task["id"]})

        assert run_response.status_code == 201
        run = run_response.json()
        assert run["status"] == "failed"
        assert run["final_artifact_id"] is None

        agent_runs = client.get(f"/runs/{run['id']}/agent-runs").json()
        assert [(agent_run["step_name"], agent_run["status"]) for agent_run in agent_runs] == [
            ("clarify_requirements", "completed"),
            ("design_implementation", "failed"),
        ]
        handoffs = client.get(f"/runs/{run['id']}/handoffs").json()
        assert len(handoffs) == 1
        assert handoffs[0]["from_agent_run_id"] == agent_runs[0]["id"]
        assert handoffs[0]["to_agent_id"] == agent_runs[1]["agent_id"]

        errors = [
            event
            for event in client.get(f"/runs/{run['id']}/trace").json()
            if event["event_type"] == "error"
        ]
        assert len(errors) == 1
        assert errors[0]["payload"]["step_name"] == "design_implementation"
        assert errors[0]["payload"]["agent_id"] == "code_rd-architect"
        assert errors[0]["payload"]["error_type"] == "RuntimeError"
        assert "forced API test failure" in errors[0]["payload"]["message"]

        detail = client.get(f"/runs/{run['id']}/detail").json()
        assert detail["run"]["status"] == "failed"
        assert [(agent_run["step_name"], agent_run["status"]) for agent_run in detail["agent_runs"]] == [
            ("clarify_requirements", "completed"),
            ("design_implementation", "failed"),
        ]
        assert detail["handoffs"] == handoffs
        assert [event for event in detail["trace"] if event["event_type"] == "error"] == errors


def test_provider_failure_trace_does_not_expose_prompt_or_secret_details(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    provider_adapter = OpenAICompatibleModelAdapter(
        provider="litellm_proxy",
        api_key_env="LITELLM_API_KEY",
        client=PromptEchoFailingClient(),
        max_attempts=1,
    )
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=lambda: FailingModelExecutor(provider_adapter),
    )
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={"title": "Secret failure", "goal": "Exercise provider failure redaction.", "workflow_pack": "code_rd"},
        ).json()

        run_response = client.post("/runs", json={"task_id": task["id"]})

        assert run_response.status_code == 201
        run = run_response.json()
        assert run["status"] == "failed"
        trace = client.get(f"/runs/{run['id']}/trace").json()
        detail = client.get(f"/runs/{run['id']}/detail").json()
        dumped = json.dumps({"trace": trace, "detail": detail})
        assert "Model runtime call failed. See structured error metadata." in dumped
        assert "sk-secret" not in dumped
        assert "Bearer" not in dumped
        assert "payload=secret" not in dumped
        assert "EXTERNAL_EVIDENCE_BODY" not in dumped
        assert "classification=provider_error" in dumped


def test_task_time_skill_injection_reaches_model_request_without_leaking_to_run_detail(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    (skill_root / "pdf").mkdir(parents=True)
    (skill_root / "pdf" / "SKILL.md").write_text(
        "---\nname: PDF Skill\n---\n\n# PDF Skill\n\nDO_NOT_LEAK_TASK_SKILL_BODY_PDF",
        encoding="utf-8",
    )
    (skill_root / "docx").mkdir(parents=True)
    (skill_root / "docx" / "SKILL.md").write_text(
        "---\nname: Word Skill\n---\n\n# Word Skill\n\nDO_NOT_LEAK_TASK_SKILL_BODY_DOCX",
        encoding="utf-8",
    )
    from app.core.skill_library import SkillLibrary

    skill_library = SkillLibrary.from_roots([skill_root])
    adapter = CapturingAdapter()
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        skill_roots_override=[skill_root],
    )
    state = app.state.harness
    state.executor_factory = lambda: PackMappedExecutor(
            model_gateway=ModelGateway({"mock": adapter}),
            artifact_store=state.artifact_store,
            trace_logger=state.trace_logger,
            web_tool_provider=state.web_tool_provider,
            browser_tool_provider=state.browser_tool_provider,
            skill_library=skill_library,
        )
    with TestClient(app) as client:
        agents = client.get("/agents", params={"pack_name": "research"}).json()
        assert all("DO_NOT_LEAK_TASK_SKILL_BODY" not in agent["system_prompt"] for agent in agents)

        task = client.post(
            "/tasks",
            json={
                "title": "读取 PDF 并写 Word 文档报告",
                "goal": "分析本地 PDF 资料，然后生成 docx 文档。",
                "workflow_pack": "research",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()

        assert run["status"] == "completed"
        assert adapter.requests
        assert any("Task-Selected Local Skills" in request.system_prompt for request in adapter.requests)
        assert any("DO_NOT_LEAK_TASK_SKILL_BODY_PDF" in request.system_prompt for request in adapter.requests)
        assert any("DO_NOT_LEAK_TASK_SKILL_BODY_DOCX" in request.system_prompt for request in adapter.requests)
        detail = client.get(f"/runs/{run['id']}/detail").json()
        dumped_detail = json.dumps(detail)
        assert "DO_NOT_LEAK_TASK_SKILL_BODY" not in dumped_detail
        assert "sk-" not in dumped_detail


def test_task_time_skill_route_trace_records_reasons_without_skill_body(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    (skill_root / "security").mkdir(parents=True)
    (skill_root / "security" / "SKILL.md").write_text(
        "---\nname: Security Skill\ndescription: Security auth jwt token review.\n---\n\n# Security\n\nTRACE_DO_NOT_LEAK_SKILL_BODY",
        encoding="utf-8",
    )
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        skill_roots_override=[skill_root],
    )
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "给 API 加 JWT 认证",
                "goal": "实现 auth token 校验并补测试。",
                "workflow_pack": "code_rd",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()

        assert run["status"] == "completed"
        detail = client.get(f"/runs/{run['id']}/detail").json()
        route_events = [
            event
            for event in detail["trace"]
            if event["event_type"] == "workflow_event"
            and event["payload"].get("action") == "task_skill_routes_applied"
        ]
        assert route_events
        assert route_events[0]["payload"]["skill_ids"] == ["skills-security"]
        assert route_events[0]["payload"]["injected_bytes"] > 0
        dumped_detail = json.dumps(detail)
        assert "TRACE_DO_NOT_LEAK_SKILL_BODY" not in dumped_detail


def test_skill_refresh_rebuilds_routes_and_default_executor_uses_current_skill_library(tmp_path) -> None:
    skill_root = tmp_path / "skills"
    (skill_root / "security").mkdir(parents=True)
    skill_path = skill_root / "security" / "SKILL.md"
    skill_path.write_text(
        "---\nname: Security Skill\ndescription: Security auth token guidance.\n---\n\nOLD_BODY",
        encoding="utf-8",
    )
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        skill_roots_override=[skill_root],
    )
    with TestClient(app) as client:
        state = app.state.harness
        first_executor = state.executor_factory()
        first_executor.trace_logger = None
        first_agent = first_executor._task_routed_agent(
            task=Task(title="JWT auth token", goal="Review security token handling.", workflow_pack="code_rd"),
            run=Run(task_id="task-1"),
            step=WorkflowStep(name="review", agent_role="Reviewer"),
            agent=AgentDefinition(id="agent-reviewer", pack_name="code_rd", role="Reviewer", system_prompt="Review."),
            context={},
        )
        assert "OLD_BODY" in first_agent.system_prompt

        skill_path.write_text(
            "---\nname: Security Skill\ndescription: Security auth token guidance.\n---\n\nNEW_BODY",
            encoding="utf-8",
        )
        refresh = client.post("/skills/refresh")
        assert refresh.status_code == 200, refresh.text
        assert refresh.json()["restart_required"] is False

        refreshed_executor = state.executor_factory()
        refreshed_executor.trace_logger = None
        refreshed_agent = refreshed_executor._task_routed_agent(
            task=Task(title="JWT auth token", goal="Review security token handling.", workflow_pack="code_rd"),
            run=Run(task_id="task-2"),
            step=WorkflowStep(name="review", agent_role="Reviewer"),
            agent=AgentDefinition(id="agent-reviewer", pack_name="code_rd", role="Reviewer", system_prompt="Review."),
            context={},
        )
        assert "NEW_BODY" in refreshed_agent.system_prompt
        assert "OLD_BODY" not in refreshed_agent.system_prompt


def test_institutional_test_gate_without_local_repository_fails_before_model_call() -> None:
    adapter = PatchProducingAdapter()
    output = PackMappedExecutor(
        model_gateway=ModelGateway({"mock": adapter})
    ).execute(
        task=Task(
            id="task-1",
            title="Institutional test gate",
            goal="Do not simulate a patched test run.",
            workflow_pack="code_rd_institutional",
        ),
        run=Run(id="run-1", task_id="task-1"),
        step=WorkflowStep(
            name="test_changes",
            agent_role="TestExecutor",
            produces_artifact_type=ArtifactType.TEST_REPORT.value,
            requires_eval_pass=True,
            required_eval_checks=["patched_local_test_command"],
        ),
        agent=AgentDefinition(
            id="agent-tester",
            pack_name="code_rd_institutional",
            role="TestExecutor",
            system_prompt="Test the patch.",
        ),
        context={},
    )

    assert adapter.calls == []
    assert output.eval_results[0].check_name == "patched_local_test_command"
    assert output.eval_results[0].status.value == "fail"
    assert "were not run" in output.artifacts[0].content


def test_task_time_skill_injection_reaches_local_code_executor_path(tmp_path) -> None:
    repo = tmp_path / "sample_repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    skill_root = tmp_path / "skills"
    (skill_root / "docx").mkdir(parents=True)
    (skill_root / "docx" / "SKILL.md").write_text(
        "---\nname: Word Skill\n---\n\n# Word Skill\n\nLOCAL_CODE_DOCX_SKILL_BODY",
        encoding="utf-8",
    )
    from app.core.skill_library import SkillLibrary

    skill_library = SkillLibrary.from_roots([skill_root])
    adapter = CapturingAdapter()
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        skill_roots_override=[skill_root],
        executor_factory=lambda: PackMappedExecutor(
            ModelGateway({"mock": adapter}),
            skill_library=skill_library,
        ),
    )
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "根据代码生成 Word 文档说明",
                "goal": "读取代码并生成 docx 文档风格的说明。",
                "workflow_pack": "code_rd_institutional",
                "inputs": {
                    "repository_path": str(repo),
                    "focus_paths": ["app.py"],
                },
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")

        approve_response = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve")

        assert approve_response.status_code == 200, approve_response.text
        patch_requests = [request for request in adapter.requests if request.metadata.get("step_name") == "prepare_patch"]
        assert patch_requests
        assert "Task-Selected Local Skills" in patch_requests[0].system_prompt
        assert "LOCAL_CODE_DOCX_SKILL_BODY" in patch_requests[0].system_prompt


class FailingExecutor:
    def execute(
        self,
        *,
        task: Task,
        run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context,
    ) -> AgentStepOutput:
        if step.name == "design_implementation":
            raise RuntimeError("forced API test failure")
        return AgentStepOutput(
            summary=f"{agent.role} completed {step.name}.",
            artifacts=[
                AgentArtifactOutput(
                    type=ArtifactType.SOURCE_SUMMARY,
                    filename=f"{step.name}.md",
                    content=f"# {step.name}\n",
                )
            ],
        )


class FailingModelExecutor:
    def __init__(self, adapter=None) -> None:
        self.model_gateway = ModelGateway(
            adapters={"litellm_proxy": adapter or SecretFailingAdapter()}
        )

    def execute(
        self,
        *,
        task: Task,
        run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context,
    ) -> AgentStepOutput:
        request = ModelRequest(
            provider="litellm_proxy",
            model="gpt-reviewer",
            system_prompt="System",
            messages=[],
            metadata={"agent_id": agent.id, "step_name": step.name},
        )
        self.model_gateway.complete(request)
        raise AssertionError("unreachable")


class SecretFailingAdapter:
    def complete(self, request: ModelRequest):
        raise ModelRuntimeError(
            "litellm_proxy model call failed. See server logs for provider details."
        ) from RuntimeError("Authorization: Bearer sk-secret payload=secret")


class PromptEchoFailingClient:
    class Responses:
        @staticmethod
        def create(**kwargs):
            raise RuntimeError(
                "Provider rejected request and echoed EXTERNAL_EVIDENCE_BODY "
                f"Authorization: Bearer sk-secret payload=secret input={kwargs.get('input')}"
            )

    def __init__(self) -> None:
        self.responses = self.Responses()


class DispatchFailingAdapter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        step_name = str(request.metadata.get("step_name", "step"))
        self.calls.append(step_name)
        if step_name == "dispatch_work":
            raise ModelRuntimeError(
                "litellm_proxy model call failed. See server logs for provider details.",
                provider=request.provider,
                model=request.model,
                adapter="openai_compatible_chat",
                error_class="RuntimeError",
                error_summary="classification=provider_error;retryable=false",
                elapsed_ms=29000,
            )
        text = f"Real provider completed {step_name}."
        return ModelResponse(
            text=text,
            usage={"input_tokens": 1, "output_tokens": len(text.split())},
            latency_ms=1,
            raw_provider=request.provider,
            adapter="openai_compatible_chat",
            mocked=False,
        )


class PatchProducingAdapter:
    def __init__(self, *, old_value: int = 41, new_value: int = 42) -> None:
        self.old_value = old_value
        self.new_value = new_value
        self.calls: list[str] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        step_name = str(request.metadata.get("step_name", "step"))
        self.calls.append(step_name)
        if step_name == "prepare_patch":
            text = (
                "Prepared patch.\n\n"
                "```diff\n"
                "--- a/app.py\n"
                "+++ b/app.py\n"
                "@@ -1,2 +1,2 @@\n"
                " def answer():\n"
                f"-    return {self.old_value}\n"
                f"+    return {self.new_value}\n"
                "```\n"
            )
        else:
            text = f"Mock completed {step_name}."
        return ModelResponse(
            text=text,
            usage={"input_tokens": 1, "output_tokens": len(text.split())},
            latency_ms=1,
            raw_provider=request.provider,
            adapter="mock",
            mocked=True,
        )


class CapturingAdapter:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        step_name = str(request.metadata.get("step_name", "step"))
        text = f"Captured {step_name}."
        return ModelResponse(
            text=text,
            usage={"input_tokens": 1, "output_tokens": len(text.split())},
            latency_ms=1,
            raw_provider=request.provider,
            adapter="mock",
            mocked=True,
        )
