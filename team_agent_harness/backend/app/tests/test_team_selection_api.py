from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from app.core.execution_plan import ExecutionPlan
from app.core.model_capabilities import CapabilityRegistry, ModelCapability, default_model_capability_registry
from app.core.models import Run
from app.core.model_runtime import MockModelAdapter, ModelGateway
from app.main import create_app


def _app(tmp_path: Path):
    return create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
        skill_roots_override=[],
    )


def _task(client: TestClient, *, pack_name: str = "code_rd") -> dict[str, object]:
    response = client.post(
        "/tasks",
        json={
            "title": "Configurable team",
            "goal": "Run the fixed workflow with a run-scoped GPT and DeepSeek team.",
            "workflow_pack": pack_name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _template(client: TestClient, pack_name: str = "code_rd") -> dict[str, object]:
    response = client.get(f"/workflow-packs/{pack_name}/team-template")
    assert response.status_code == 200, response.text
    return response.json()


def test_team_template_covers_fixed_slots_without_exposing_prompts(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        pack = client.get("/workflow-packs/code_rd").json()
        template = _template(client)

    selection = template["team_selection"]
    assignments = selection["assignments"]
    assert selection["version"] == "team-selection-v1"
    assert selection["pack_name"] == "code_rd"
    assert {item["slot"] for item in assignments} == {agent["role"] for agent in pack["agents"]}
    assert {item["route"]["family"] for item in assignments} == {"gpt", "deepseek"}
    assert {item["route"]["provider"] for item in assignments} == {"litellm_proxy", "deepseek"}
    assert all(item["route"]["model"] in {"gpt5.6-sol", "deepseek-v4-flash"} for item in assignments)
    for item in assignments:
        if item["route"]["family"] == "gpt":
            assert item["route"]["fallbacks"] == [
                {
                    "family": "deepseek",
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                }
            ]
        else:
            assert item["route"]["fallbacks"] == []
    assert {item["slot"] for item in template["slots"]} == {
        item["slot"] for item in assignments
    }
    dumped = json.dumps(template).lower()
    assert "system_prompt" not in dumped
    assert "api_key" not in dumped
    assert "authorization" not in dumped
    assert "bearer" not in dumped


def test_validate_and_run_freeze_the_selected_team_and_role_card(tmp_path: Path) -> None:
    role_dir = tmp_path / "config" / "roles"
    role_dir.mkdir(parents=True)
    role_dir.joinpath("strict-reviewer.md").write_text(
        "---\nname: Strict Reviewer\n---\n\nChallenge correctness and report evidence only.\n",
        encoding="utf-8",
    )
    app = _app(tmp_path)
    app.state.harness.model_gateway = ModelGateway(
        {"litellm_proxy": MockModelAdapter(), "deepseek": MockModelAdapter()}
    )

    with TestClient(app) as client:
        task = _task(client)
        selection = _template(client)["team_selection"]
        reviewer = next(item for item in selection["assignments"] if item["slot"] == "Reviewer")
        reviewer["role_card_id"] = "strict-reviewer"

        validation = client.post("/team-selections/validate", json=selection)
        assert validation.status_code == 200, validation.text
        assert validation.json()["valid"] is True
        assert validation.json()["requires_real_model_confirmation"] is True

        response = client.post(
            "/runs",
            json={
                "task_id": task["id"],
                "team_selection": selection,
                "confirm_real_models": True,
            },
        )
        assert response.status_code == 201, response.text
        run = response.json()
        assert run["status"] == "completed"

        frozen = client.get(f"/runs/{run['id']}/team")
        assert frozen.status_code == 200, frozen.text
        frozen_payload = frozen.json()
        assert frozen_payload["run_id"] == run["id"]
        assert frozen_payload["immutable"] is True
        assert frozen_payload["execution_plan_hash"] == run["execution_plan_hash"]
        assert frozen_payload["team_selection"] == validation.json()["team_selection"]
        assert "Challenge correctness" not in json.dumps(frozen_payload)

        persisted = app.state.harness.storage.get_run(run["id"])
        assert persisted is not None and persisted.execution_plan is not None
        plan = ExecutionPlan.model_validate(persisted.execution_plan)
        reviewer_snapshot = next(item for item in plan.agent_snapshots if item.role == "Reviewer")
        assert reviewer_snapshot.system_prompt == "Challenge correctness and report evidence only."
        assert reviewer_snapshot.model_settings["team_selection_version"] == "team-selection-v1"
        assert reviewer_snapshot.model_settings["model_family"] == "deepseek"
        assert reviewer_snapshot.model_settings["role_card_id"] == "strict-reviewer"


def test_run_team_receipt_uses_frozen_manifest_after_pack_changes(tmp_path: Path) -> None:
    app = _app(tmp_path)
    app.state.harness.model_gateway = ModelGateway(
        {"litellm_proxy": MockModelAdapter(), "deepseek": MockModelAdapter()}
    )

    with TestClient(app) as client:
        task = _task(client)
        selection = _template(client)["team_selection"]
        run_response = client.post(
            "/runs",
            json={
                "task_id": task["id"],
                "team_selection": selection,
                "confirm_real_models": True,
            },
        )
        assert run_response.status_code == 201, run_response.text
        run = run_response.json()

        pack = app.state.harness.packs["code_rd"]
        revised_agents = list(pack.agents)
        revised_agents[0] = revised_agents[0].model_copy(
            update={"id": f"{revised_agents[0].id}-revised"}
        )
        app.state.harness.packs["code_rd"] = pack.model_copy(
            update={"agents": revised_agents},
            deep=True,
        )

        receipt_response = client.get(f"/runs/{run['id']}/team")

    assert receipt_response.status_code == 200, receipt_response.text
    receipt = receipt_response.json()["team_selection"]
    assert receipt is not None
    assert receipt["assignments"][0]["agent_id"] != revised_agents[0].id


@pytest.mark.parametrize("endpoint", ["team", "quality", "detail", "trace"])
def test_run_observability_rejects_storage_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    app = _app(tmp_path)
    mismatched_run = Run(id="different-run", task_id="task-1")

    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.harness.storage,
            "get_run",
            lambda _run_id: mismatched_run,
        )
        response = client.get(f"/runs/requested-run/{endpoint}")

    assert response.status_code == 409
    assert "identity" in response.json()["detail"].lower()


@pytest.mark.parametrize("endpoint", ["team", "quality"])
@pytest.mark.parametrize(
    ("execution_plan", "plan_hash"),
    [
        ({}, None),
        (None, "a" * 64),
    ],
)
def test_run_observability_rejects_incomplete_execution_plan_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    execution_plan: dict[str, object] | None,
    plan_hash: str | None,
) -> None:
    app = _app(tmp_path)
    corrupt_run = Run(id="run-incomplete-plan", task_id="task-1").model_copy(
        update={
            "execution_plan": execution_plan,
            "execution_plan_hash": plan_hash,
        }
    )

    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.harness.storage,
            "get_run",
            lambda _run_id: corrupt_run,
        )
        response = client.get(f"/runs/{corrupt_run.id}/{endpoint}")

    assert response.status_code == 409
    assert "incomplete" in response.json()["detail"].lower()


def test_legacy_run_without_execution_plan_pair_remains_observable(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        task = _task(client)
        legacy_run = app.state.harness.storage.create_run(
            Run(id="legacy-run", task_id=str(task["id"]))
        )
        team_response = client.get(f"/runs/{legacy_run.id}/team")
        quality_response = client.get(f"/runs/{legacy_run.id}/quality")

    assert team_response.status_code == 200, team_response.text
    assert team_response.json() == {
        "run_id": legacy_run.id,
        "team_selection": None,
        "execution_plan_hash": None,
        "immutable": False,
    }
    assert quality_response.status_code == 200, quality_response.text
    assert quality_response.json()["run_id"] == legacy_run.id


def test_run_confirmation_checks_the_resolved_team_not_the_base_pack(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        task = _task(client)
        selection = _template(client)["team_selection"]
        monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
        monkeypatch.setenv("LITELLM_API_KEY", "configured-for-test")

        response = client.post(
            "/runs",
            json={"task_id": task["id"], "team_selection": selection},
        )
        persisted_runs = app.state.harness.storage.list_runs()

    assert response.status_code == 400
    assert "confirm_real_models=true" in response.json()["detail"]
    assert persisted_runs == []


def test_run_confirmation_rejects_selected_team_when_provider_is_disabled(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    app = _app(tmp_path)
    with TestClient(app) as client:
        task = _task(client)
        selection = _template(client)["team_selection"]

        response = client.post(
            "/runs",
            json={"task_id": task["id"], "team_selection": selection},
        )
        persisted_runs = app.state.harness.storage.list_runs()

    assert response.status_code == 400
    assert "confirm_real_models=true" in response.json()["detail"]
    assert persisted_runs == []


def test_team_template_rejects_secret_like_nested_metadata(tmp_path: Path) -> None:
    app = _app(tmp_path)
    pack = app.state.harness.packs["code_rd"]
    agents = list(pack.agents)
    agents[0] = agents[0].model_copy(
        update={
            "model_settings": {
                "provider": "litellm_proxy",
                "model": "sk-abcdefghijklmno",
                "model_family": "gpt",
            }
        },
        deep=True,
    )
    app.state.harness.packs["code_rd"] = pack.model_copy(
        update={"agents": agents},
        deep=True,
    )

    with TestClient(app) as client:
        response = client.get("/workflow-packs/code_rd/team-template")

    assert response.status_code == 400
    assert "sensitive" in response.json()["detail"].lower()
    assert "sk-abcdefghijklmno" not in response.text


def test_team_template_rejects_sensitive_runtime_limit_field(tmp_path: Path) -> None:
    app = _app(tmp_path)
    pack = app.state.harness.packs["code_rd"]
    agents = list(pack.agents)
    agents[0] = agents[0].model_copy(
        update={
            "runtime_limits": {
                **agents[0].runtime_limits,
                "api_key": "opaque-secret-value",
            }
        },
        deep=True,
    )
    app.state.harness.packs["code_rd"] = pack.model_copy(
        update={"agents": agents},
        deep=True,
    )

    with TestClient(app) as client:
        response = client.get("/workflow-packs/code_rd/team-template")

    assert response.status_code == 400
    assert "sensitive" in response.json()["detail"].lower()
    assert "opaque-secret-value" not in response.text


def test_dynamic_subset_plan_preserves_complete_selected_team_receipt(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    app.state.harness.model_gateway = ModelGateway(
        {"litellm_proxy": MockModelAdapter(), "deepseek": MockModelAdapter()}
    )
    execution_plan = {
        "schema_version": "execution-plan-v1",
        "workflow_pack": "code_rd",
        "source": "operator",
        "final_artifact_type": "final_report",
        "max_parallel_steps": 1,
        "steps": [
            {
                "step_id": "final-only",
                "objective": "Produce the final report directly.",
                "agent_role": "Finalizer",
                "expected_artifacts": ["final_report"],
                "acceptance_criteria": [
                    {
                        "name": "final-nonempty",
                        "kind": "artifact_nonempty",
                        "artifact_type": "final_report",
                    }
                ],
            }
        ],
    }

    with TestClient(app) as client:
        task = _task(client)
        selection = _template(client)["team_selection"]
        validation = client.post("/team-selections/validate", json=selection)
        assert validation.status_code == 200, validation.text
        response = client.post(
            "/runs",
            json={
                "task_id": task["id"],
                "execution_plan": execution_plan,
                "team_selection": selection,
                "confirm_real_models": True,
            },
        )
        assert response.status_code == 201, response.text
        run = response.json()
        assert run["status"] == "completed"
        receipt_response = client.get(f"/runs/{run['id']}/team")

    assert receipt_response.status_code == 200, receipt_response.text
    assert receipt_response.json()["team_selection"] == validation.json()["team_selection"]


def test_plan_generation_confirmation_checks_the_selected_planner_route(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        task = _task(client)
        selection = _template(client)["team_selection"]
        response = client.post(
            "/execution-plans/generate",
            json={"task_id": task["id"], "team_selection": selection},
        )

    assert response.status_code == 400
    assert "confirm_real_models=true" in response.json()["detail"]


def test_team_selection_api_uses_the_active_model_capability_registry(tmp_path: Path) -> None:
    app = _app(tmp_path)
    default_registry = default_model_capability_registry()
    custom_registry = CapabilityRegistry(
        [
            *default_registry.capabilities,
            ModelCapability(
                provider="litellm_proxy",
                model_pattern="custom-gpt",
                protocol="chat_completions",
                model_family="gpt",
            ),
        ],
        source="test",
    )
    app.state.harness.model_gateway = ModelGateway(capability_registry=custom_registry)

    with TestClient(app) as client:
        selection = _template(client)["team_selection"]
        selection["assignments"][0]["route"] = {
            "family": "gpt",
            "provider": "litellm_proxy",
            "model": "custom-gpt",
        }
        response = client.post("/team-selections/validate", json=selection)

    assert response.status_code == 200, response.text


def test_team_template_uses_only_exact_models_from_the_active_capability_registry(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    custom_registry = CapabilityRegistry(
        [
            ModelCapability(
                provider="litellm_proxy",
                model_pattern="custom-gpt",
                protocol="chat_completions",
                model_family="gpt",
                supports_reasoning=True,
            ),
            ModelCapability(
                provider="litellm_proxy",
                model_pattern="custom-deepseek",
                protocol="chat_completions",
                model_family="deepseek",
                supports_reasoning=True,
            ),
        ],
        source="external",
    )
    app.state.harness.model_gateway = ModelGateway(capability_registry=custom_registry)

    with TestClient(app) as client:
        template_response = client.get("/workflow-packs/code_rd/team-template")
        assert template_response.status_code == 200, template_response.text
        selection = template_response.json()["team_selection"]
        validation = client.post("/team-selections/validate", json=selection)

    assert {
        (assignment["route"]["family"], assignment["route"]["model"])
        for assignment in selection["assignments"]
    } == {
        ("gpt", "custom-gpt"),
        ("deepseek", "custom-deepseek"),
    }
    assert validation.status_code == 200, validation.text
    receipts_by_slot = {
        receipt["slot"]: receipt
        for receipt in validation.json()["team_selection"]["assignments"]
    }
    for assignment in selection["assignments"]:
        receipt = receipts_by_slot[assignment["slot"]]
        assert (
            receipt["model_family"],
            receipt["provider"],
            receipt["model"],
        ) == (
            assignment["route"]["family"],
            assignment["route"]["provider"],
            assignment["route"]["model"],
        )


def test_team_template_falls_back_to_the_only_available_registered_family(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    custom_registry = CapabilityRegistry(
        [
            ModelCapability(
                provider="litellm_proxy",
                model_pattern="custom-gpt",
                protocol="chat_completions",
                model_family="gpt",
                supports_reasoning=True,
            )
        ],
        source="external",
    )
    app.state.harness.model_gateway = ModelGateway(capability_registry=custom_registry)

    with TestClient(app) as client:
        template_response = client.get("/workflow-packs/code_rd/team-template")
        assert template_response.status_code == 200, template_response.text
        template = template_response.json()
        validation = client.post(
            "/team-selections/validate",
            json=template["team_selection"],
        )

    assert validation.status_code == 200, validation.text
    assert {
        (assignment["route"]["family"], assignment["route"]["model"])
        for assignment in template["team_selection"]["assignments"]
    } == {("gpt", "custom-gpt")}
    assert any(
        "deepseek" in warning.lower() and "gpt" in warning.lower()
        for warning in template["configuration_warnings"]
    )


def test_team_template_fails_closed_without_exact_gpt_and_deepseek_capabilities(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    unclassified_registry = CapabilityRegistry(
        [
            ModelCapability(
                provider="litellm_proxy",
                model_pattern="*",
                protocol="chat_completions",
            )
        ],
        source="external",
    )
    app.state.harness.model_gateway = ModelGateway(capability_registry=unclassified_registry)

    with TestClient(app) as client:
        response = client.get("/workflow-packs/code_rd/team-template")

    assert response.status_code == 400
    assert "exact gpt or deepseek capability" in response.json()["detail"].lower()


def test_team_selection_rejects_missing_or_wrong_pack_slots(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        selection = _template(client)["team_selection"]
        missing_slot = {
            **selection,
            "assignments": selection["assignments"][:-1],
        }
        missing_response = client.post("/team-selections/validate", json=missing_slot)

        wrong_pack = {**selection, "pack_name": "research"}
        wrong_pack_response = client.post("/team-selections/validate", json=wrong_pack)

        missing_run_team = client.get("/runs/missing/team")

    assert missing_response.status_code == 400
    assert "cover every workflow Pack agent slot exactly once" in missing_response.json()["detail"]
    assert wrong_pack_response.status_code == 400
    assert "cover every workflow Pack agent slot exactly once" in wrong_pack_response.json()["detail"]
    assert missing_run_team.status_code == 404


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", 2),
        ("max_tokens", 200_000),
        ("input_usd_per_million", 0),
        ("output_usd_per_million", 0),
    ],
)
def test_team_selection_api_rejects_client_runtime_and_price_overrides(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        selection = _template(client)["team_selection"]
        selection["assignments"][0]["route"][field] = value
        response = client.post("/team-selections/validate", json=selection)

    assert response.status_code == 422
    assert any(item["type"] == "extra_forbidden" for item in response.json()["detail"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", 2),
        ("max_tokens", 200_000),
        ("input_usd_per_million", 0),
        ("output_usd_per_million", 0),
    ],
)
def test_team_selection_api_rejects_fallback_runtime_and_price_overrides(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        selection = _template(client)["team_selection"]
        selection["assignments"][0]["route"]["fallbacks"] = [
            {
                "family": "deepseek",
                "provider": "deepseek",
                "model": "deepseek-chat",
                field: value,
            }
        ]
        response = client.post("/team-selections/validate", json=selection)

    assert response.status_code == 422
    assert any(item["type"] == "extra_forbidden" for item in response.json()["detail"])
