from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.execution_plan import (
    ExecutionPlan,
    ExecutionPlanStep,
    execution_plan_from_pack,
    execution_plan_hash,
    freeze_execution_plan,
    validate_execution_plan_against_pack,
    workflow_pack_from_execution_plan,
)
from app.main import create_app
from app.core.models import ArtifactType, Task
from app.core.model_runtime import ModelGateway, ModelResponse, ModelRuntimeError
from app.core.plan_generation import generate_execution_plan
from app.packs.base import (
    AgentLoopPolicy,
    EvalCheck,
    StepAcceptanceCriterion,
    WorkflowPack,
)
from app.packs.code_rd import get_code_rd_pack
from app.packs.code_rd_institutional import get_code_rd_institutional_pack


def _single_agent_plan() -> ExecutionPlan:
    return ExecutionPlan(
        workflow_pack="code_rd",
        source="operator",
        final_artifact_type=ArtifactType.FINAL_REPORT,
        max_parallel_steps=1,
        steps=[
            ExecutionPlanStep(
                step_id="solve",
                objective="Solve the task and produce the accepted final delivery.",
                agent_role="Finalizer",
                expected_artifacts=[ArtifactType.FINAL_REPORT],
                acceptance_criteria=[
                    StepAcceptanceCriterion(
                        name="nonempty-final",
                        kind="artifact_nonempty",
                        artifact_type=ArtifactType.FINAL_REPORT,
                    )
                ],
            )
        ],
    )


def test_workflow_pack_plan_round_trip_is_stable() -> None:
    pack = get_code_rd_pack()

    plan = execution_plan_from_pack(pack)
    rebuilt = workflow_pack_from_execution_plan(plan, pack)

    assert plan.source == "workflow_pack"
    assert execution_plan_hash(plan) == execution_plan_hash(ExecutionPlan.model_validate(plan.model_dump()))
    assert [step.name for step in rebuilt.steps] == [step.name for step in pack.steps]
    assert rebuilt.final_artifact_type == pack.final_artifact_type
    assert [step.depends_on for step in rebuilt.steps] == [step.depends_on for step in pack.steps]
    assert [step.allowed_tools for step in rebuilt.steps] == [step.allowed_tools for step in pack.steps]
    assert [step.runtime for step in rebuilt.steps] == [step.runtime for step in pack.steps]
    assert all(step.acceptance_criteria for step in plan.steps)
    assert {snapshot.role for snapshot in plan.agent_snapshots} == {
        agent.role for agent in pack.agents
    }


def test_frozen_plan_rebuilds_agents_from_snapshot_after_pack_changes() -> None:
    pack = get_code_rd_pack()
    frozen = execution_plan_from_pack(pack)
    original = next(snapshot for snapshot in frozen.agent_snapshots if snapshot.role == "Finalizer")
    changed_agents = [
        agent.model_copy(
            update={
                "system_prompt": "Changed after submission.",
                "model_settings": {"provider": "mock", "model": "changed-model"},
                "runtime_limits": {"max_steps": 2},
                "effective_skill_ids": ["changed-skill"],
            }
        )
        if agent.role == "Finalizer"
        else agent
        for agent in pack.agents
    ]
    changed_pack = pack.model_copy(update={"agents": changed_agents})

    recovered = workflow_pack_from_execution_plan(
        frozen,
        changed_pack,
        allow_frozen_workflow_pack_snapshot=True,
    )
    finalizer = next(agent for agent in recovered.agents if agent.role == "Finalizer")

    assert finalizer.id == original.agent_id
    assert finalizer.system_prompt == original.system_prompt
    assert finalizer.model_settings == original.model_settings
    assert finalizer.runtime_limits == original.runtime_limits
    assert finalizer.effective_skill_ids == original.effective_skill_ids


def test_frozen_dynamic_plan_recovery_ignores_current_pack_policy_drift() -> None:
    pack = get_code_rd_pack()
    frozen = freeze_execution_plan(_single_agent_plan(), pack)
    changed_steps = [
        step.model_copy(update={"produces_artifact_type": ArtifactType.FINAL_REPORT.value})
        if step.name == "review_delivery"
        else step
        for step in pack.steps
        if step.agent_role != "Finalizer"
    ]
    changed_pack = WorkflowPack.model_validate(
        pack.model_copy(
            update={
                "steps": changed_steps,
                "eval_checks": [
                    EvalCheck(
                        name="replacement_final_gate",
                        description="A later Pack version changed its final gate.",
                        required_artifact_types=[ArtifactType.FINAL_REPORT.value],
                    )
                ],
                "max_parallel_steps": 1,
            }
        ).model_dump(mode="json", by_alias=True)
    )

    recovered = workflow_pack_from_execution_plan(
        frozen,
        changed_pack,
        allow_frozen_workflow_pack_snapshot=True,
    )

    assert [step.name for step in recovered.steps] == ["solve"]
    assert [agent.role for agent in recovered.agents] == ["Finalizer"]
    assert [check.name for check in recovered.eval_checks] == [
        "final_delivery_summary_exists"
    ]
    assert recovered.max_parallel_steps == frozen.max_parallel_steps


def test_workflow_pack_source_cannot_be_forged() -> None:
    pack = get_code_rd_pack()
    forged = execution_plan_from_pack(pack).model_copy(update={"max_parallel_steps": 1})

    with pytest.raises(ValueError, match="must exactly match"):
        validate_execution_plan_against_pack(forged, pack)

    recovered = workflow_pack_from_execution_plan(
        forged,
        pack,
        allow_frozen_workflow_pack_snapshot=True,
    )
    assert recovered.max_parallel_steps == 1


def test_operator_plan_cannot_expand_agent_tool_permissions() -> None:
    plan = _single_agent_plan()
    step = plan.steps[0].model_copy(update={"tool_permissions": ["run_test_command"]})
    plan = plan.model_copy(update={"steps": [step]})

    with pytest.raises(ValueError, match="expands agent tool permissions"):
        validate_execution_plan_against_pack(plan, get_code_rd_pack())


def test_operator_plan_requires_acceptance_and_rejects_cycles() -> None:
    with pytest.raises(ValueError, match="requires acceptance_criteria"):
        ExecutionPlan(
            workflow_pack="code_rd",
            source="operator",
            final_artifact_type=ArtifactType.FINAL_REPORT,
            steps=[
                ExecutionPlanStep(
                    step_id="solve",
                    objective="Solve.",
                    agent_role="Finalizer",
                    expected_artifacts=[ArtifactType.FINAL_REPORT],
                )
            ],
        )

    criterion = StepAcceptanceCriterion(
        name="nonempty",
        kind="artifact_nonempty",
        artifact_type=ArtifactType.FINAL_REPORT,
    )
    with pytest.raises(ValueError, match="dependency cycle"):
        ExecutionPlan(
            workflow_pack="code_rd",
            source="operator",
            final_artifact_type=ArtifactType.FINAL_REPORT,
            steps=[
                ExecutionPlanStep(
                    step_id="a",
                    objective="A.",
                    agent_role="Finalizer",
                    dependencies=["b"],
                    expected_artifacts=[ArtifactType.FINAL_REPORT],
                    acceptance_criteria=[criterion],
                ),
                ExecutionPlanStep(
                    step_id="b",
                    objective="B.",
                    agent_role="Finalizer",
                    dependencies=["a"],
                    expected_artifacts=[ArtifactType.FINAL_REPORT],
                    acceptance_criteria=[criterion],
                ),
            ],
        )


def test_dynamic_plan_rejects_agent_loop_without_shared_tool_permission() -> None:
    plan = _single_agent_plan()
    step = plan.steps[0].model_copy(
        update={
            "tool_permissions": ["read_file"],
            "agent_loop": AgentLoopPolicy(enabled=True, max_steps=2, max_tool_calls=1),
        }
    )
    plan = plan.model_copy(update={"steps": [step]})

    with pytest.raises(ValueError, match="expands agent tool permissions"):
        validate_execution_plan_against_pack(plan, get_code_rd_pack())


def test_dynamic_plan_cannot_exceed_pack_or_agent_runtime_limits() -> None:
    plan = _single_agent_plan().model_copy(update={"max_parallel_steps": 9})
    with pytest.raises(ValueError, match="exceeds the workflow Pack limit"):
        validate_execution_plan_against_pack(plan, get_code_rd_pack())

    step = _single_agent_plan().steps[0].model_copy(
        update={
            "agent_role": "Coder",
            "tool_permissions": ["read_file"],
            "agent_loop": AgentLoopPolicy(enabled=True, max_steps=9, max_tool_calls=1),
        }
    )
    with pytest.raises(ValueError, match="exceeds agent max_steps runtime limit"):
        validate_execution_plan_against_pack(
            _single_agent_plan().model_copy(update={"steps": [step]}),
            get_code_rd_pack(),
        )


def test_dynamic_plan_cannot_omit_a_trusted_agent_cost_ceiling() -> None:
    step = _single_agent_plan().steps[0].model_copy(
        update={
            "agent_role": "Coder",
            "tool_permissions": ["write_artifact"],
            "agent_loop": AgentLoopPolicy(
                enabled=True,
                max_steps=2,
                max_tool_calls=1,
                max_cost_usd=None,
            ),
        }
    )

    with pytest.raises(ValueError, match="requires a max_cost_usd ceiling"):
        validate_execution_plan_against_pack(
            _single_agent_plan().model_copy(update={"steps": [step]}),
            get_code_rd_pack(),
        )


def test_dynamic_plan_can_omit_cost_ceiling_when_trusted_agent_has_none() -> None:
    pack = get_code_rd_pack()
    agents = [
        agent.model_copy(
            update={
                "runtime_limits": {
                    name: value
                    for name, value in agent.runtime_limits.items()
                    if name != "max_cost_usd"
                }
            }
        )
        if agent.role == "Coder"
        else agent
        for agent in pack.agents
    ]
    pack = pack.model_copy(update={"agents": agents})
    step = _single_agent_plan().steps[0].model_copy(
        update={
            "agent_role": "Coder",
            "tool_permissions": ["write_artifact"],
            "agent_loop": AgentLoopPolicy(
                enabled=True,
                max_steps=2,
                max_tool_calls=1,
                max_cost_usd=None,
            ),
        }
    )

    validated = validate_execution_plan_against_pack(
        _single_agent_plan().model_copy(update={"steps": [step]}),
        pack,
    )

    assert validated.steps[0].agent_loop.max_cost_usd is None


def test_dynamic_plan_restores_relevant_pack_blocker_evals() -> None:
    validated = validate_execution_plan_against_pack(
        _single_agent_plan(),
        get_code_rd_pack(),
    )

    assert [check.name for check in validated.eval_checks] == [
        "final_delivery_summary_exists"
    ]


def test_dynamic_plan_rejects_conflicting_pack_blocker_eval() -> None:
    plan = _single_agent_plan().model_copy(
        update={
            "eval_checks": [
                EvalCheck(
                    name="final_delivery_summary_exists",
                    description="Weaken the final gate.",
                    severity="warning",
                    required_artifact_types=[],
                )
            ]
        }
    )

    with pytest.raises(ValueError, match="conflicts with workflow Pack blocker eval"):
        validate_execution_plan_against_pack(plan, get_code_rd_pack())


def test_dynamic_plan_rejects_eval_overflow_when_restoring_pack_gate() -> None:
    plan = _single_agent_plan().model_copy(
        update={
            "eval_checks": [
                EvalCheck(
                    name=f"custom-warning-{index}",
                    description="Custom non-blocking evaluation.",
                    severity="warning",
                )
                for index in range(32)
            ]
        }
    )

    with pytest.raises(ValueError, match="exceed the 32-check execution plan limit"):
        validate_execution_plan_against_pack(plan, get_code_rd_pack())


def test_dynamic_plan_cannot_repeat_a_role_beyond_static_pack_usage() -> None:
    criterion = StepAcceptanceCriterion(
        name="nonempty",
        kind="artifact_nonempty",
        artifact_type=ArtifactType.FINAL_REPORT,
    )
    plan = ExecutionPlan(
        workflow_pack="code_rd",
        source="operator",
        final_artifact_type=ArtifactType.FINAL_REPORT,
        steps=[
            ExecutionPlanStep(
                step_id="draft",
                objective="Draft a delivery.",
                agent_role="Finalizer",
                expected_artifacts=[ArtifactType.FINAL_REPORT],
                acceptance_criteria=[criterion],
            ),
            ExecutionPlanStep(
                step_id="redraft",
                objective="Repeat the same role to multiply its budget.",
                agent_role="Finalizer",
                dependencies=["draft"],
                expected_artifacts=[ArtifactType.FINAL_REPORT],
                acceptance_criteria=[criterion],
            ),
        ],
    )

    with pytest.raises(ValueError, match="repeats agent role Finalizer"):
        validate_execution_plan_against_pack(plan, get_code_rd_pack())


def test_institutional_pack_rejects_dynamic_plan_that_bypasses_patch_test_chain() -> None:
    plan = ExecutionPlan(
        workflow_pack="code_rd_institutional",
        source="operator",
        final_artifact_type=ArtifactType.FINAL_REPORT,
        max_parallel_steps=1,
        steps=[
            ExecutionPlanStep(
                step_id="approve_without_patch_test",
                objective="Approve a final report without running the institutional chain.",
                agent_role="FinalApprover",
                expected_artifacts=[ArtifactType.FINAL_REPORT],
                acceptance_criteria=[
                    StepAcceptanceCriterion(
                        name="nonempty-final",
                        kind="artifact_nonempty",
                        artifact_type=ArtifactType.FINAL_REPORT,
                    )
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="does not allow dynamic execution plans"):
        validate_execution_plan_against_pack(plan, get_code_rd_institutional_pack())


def test_dynamic_plan_rejects_impossible_artifact_dataflow() -> None:
    final_criterion = StepAcceptanceCriterion(
        name="final",
        kind="artifact_nonempty",
        artifact_type=ArtifactType.FINAL_REPORT,
    )
    plan = ExecutionPlan(
        workflow_pack="code_rd",
        source="operator",
        final_artifact_type=ArtifactType.FINAL_REPORT,
        steps=[
            ExecutionPlanStep(
                step_id="patch",
                objective="Prepare patch.",
                agent_role="Coder",
                expected_artifacts=[ArtifactType.PATCH],
                acceptance_criteria=[
                    StepAcceptanceCriterion(
                        name="patch",
                        kind="artifact_nonempty",
                        artifact_type=ArtifactType.PATCH,
                    )
                ],
            ),
            ExecutionPlanStep(
                step_id="finish",
                objective="Finish without declaring the patch dependency.",
                agent_role="Finalizer",
                required_artifacts=[ArtifactType.PATCH],
                expected_artifacts=[ArtifactType.FINAL_REPORT],
                acceptance_criteria=[final_criterion],
            ),
        ],
    )

    with pytest.raises(ValueError, match="not produced by its dependencies"):
        validate_execution_plan_against_pack(plan, get_code_rd_pack())


def test_dynamic_plan_rejects_acceptance_for_undeclared_artifact() -> None:
    plan = _single_agent_plan()
    step = plan.steps[0].model_copy(
        update={
            "acceptance_criteria": [
                StepAcceptanceCriterion(
                    name="wrong-artifact",
                    kind="artifact_nonempty",
                    artifact_type=ArtifactType.PATCH,
                )
            ]
        }
    )

    with pytest.raises(ValueError, match="acceptance criteria for undeclared artifacts"):
        validate_execution_plan_against_pack(
            plan.model_copy(update={"steps": [step]}),
            get_code_rd_pack(),
        )


def test_api_validates_freezes_and_executes_dynamic_single_agent_plan(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )
    plan = _single_agent_plan()
    with TestClient(app) as client:
        validation = client.post("/execution-plans/validate", json=plan.model_dump(mode="json"))
        assert validation.status_code == 200
        validated_plan = ExecutionPlan.model_validate(validation.json()["execution_plan"])
        assert not validated_plan.agent_snapshots
        assert validation.json()["public_plan_hash"] == execution_plan_hash(validated_plan)
        assert validation.json()["run_execution_plan_hash"] is None
        assert validation.json()["immutable_after_run_creation"] is True
        assert "execution_plan_hash" not in validation.json()

        task = client.post(
            "/tasks",
            json={
                "title": "Dynamic plan",
                "goal": "Complete through one frozen step.",
                "workflow_pack": "code_rd",
            },
        ).json()
        response = client.post(
            "/runs",
            json={"task_id": task["id"], "execution_plan": plan.model_dump(mode="json")},
        )

        assert response.status_code == 201
        run = response.json()
        assert run["status"] == "completed"
        assert run["execution_plan_redacted"] is True
        assert run["execution_plan"]["agent_snapshots"] == []
        persisted_run = app.state.harness.storage.get_run(run["id"])
        assert persisted_run is not None and persisted_run.execution_plan is not None
        persisted_plan = ExecutionPlan.model_validate(persisted_run.execution_plan)
        assert persisted_plan.agent_snapshots
        assert run["execution_plan_hash"] == execution_plan_hash(persisted_plan)
        detail = client.get(f"/runs/{run['id']}/detail").json()
        quality = client.get(f"/runs/{run['id']}/quality")
        assert [agent_run["step_name"] for agent_run in detail["agent_runs"]] == ["solve"]
        assert any(
            event["payload"].get("action") == "execution_plan_loaded"
            for event in detail["trace"]
        )
        assert any(
            result["check_name"] == "solve:acceptance:nonempty-final"
            and result["status"] == "pass"
            for result in detail["eval_results"]
        )
        assert quality.status_code == 200
        quality_payload = quality.json()
        assert quality_payload["passed"] is True
        assert any(
            check["name"] == "eval:solve:acceptance:nonempty-final"
            and check["status"] == "pass"
            for check in quality_payload["checks"]
        )
        stored_run = app.state.harness.storage.get_run(run["id"])
        assert stored_run is not None and stored_run.execution_plan is not None
        app.state.harness.storage.update_run(
            stored_run.model_copy(
                update={
                    "execution_plan": {
                        **stored_run.execution_plan,
                        "max_parallel_steps": 2,
                    }
                }
            )
        )
        tampered_quality = client.get(f"/runs/{run['id']}/quality")
        assert tampered_quality.status_code == 409
        assert "hash does not match" in tampered_quality.json()["detail"]


def test_api_rejects_side_effect_approval_outside_frozen_plan(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )
    with TestClient(app) as client:
        assert app.state.harness.executor_factory().model_gateway is app.state.harness.model_gateway
        task = client.post(
            "/tasks",
            json={"title": "Approval", "goal": "Reject broad approval.", "workflow_pack": "code_rd"},
        ).json()
        response = client.post(
            "/runs",
            json={
                "task_id": task["id"],
                "execution_plan": _single_agent_plan().model_dump(mode="json"),
                "approved_side_effect_tools": ["run_test_command"],
            },
        )

        assert response.status_code == 400
    assert "outside the frozen execution plan" in response.json()["detail"]


def test_agent_loop_workspace_tools_require_explicit_repository_path(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )
    plan = ExecutionPlan(
        workflow_pack="code_rd",
        source="operator",
        final_artifact_type=ArtifactType.FINAL_REPORT,
        max_parallel_steps=1,
        steps=[
            ExecutionPlanStep(
                step_id="inspect",
                objective="Inspect only the explicitly selected repository.",
                agent_role="Clarifier",
                expected_artifacts=[ArtifactType.FINAL_REPORT],
                acceptance_criteria=[
                    StepAcceptanceCriterion(
                        name="nonempty-final",
                        kind="artifact_nonempty",
                        artifact_type=ArtifactType.FINAL_REPORT,
                    )
                ],
                tool_permissions=["read_file"],
                agent_loop=AgentLoopPolicy(
                    enabled=True,
                    max_steps=2,
                    max_tool_calls=1,
                    max_cost_usd=10,
                ),
            )
        ],
    )

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "No implicit workspace",
                "goal": "Do not expose the service working directory.",
                "workflow_pack": "code_rd",
            },
        ).json()
        response = client.post(
            "/runs",
            json={"task_id": task["id"], "execution_plan": plan.model_dump(mode="json")},
        )
        detail = client.get(f"/runs/{response.json()['id']}/detail").json()

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert "repository_path" in str(detail["trace"])


class _PlanAdapter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[object] = []

    def complete(self, request: object) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            text=self.text,
            raw_provider="litellm_proxy",
            adapter="scripted",
            mocked=False,
        )


class _SelectedPlanAdapter:
    def __init__(
        self,
        text: str,
        *,
        provider: str,
        mocked: bool,
        usage: dict[str, int],
    ) -> None:
        self.text = text
        self.provider = provider
        self.mocked = mocked
        self.usage = usage

    def complete(self, request: object) -> ModelResponse:
        return ModelResponse(
            text=self.text,
            usage=self.usage,
            raw_provider=self.provider,
            adapter="scripted",
            mocked=self.mocked,
        )


class _RetryablePlanFailureAdapter:
    def complete(self, request: object) -> ModelResponse:
        raise ModelRuntimeError(
            "Timed out.",
            provider="litellm_proxy",
            model="gpt5.5",
            error_class="TimeoutError",
            error_summary="classification=timeout_error;retryable=true",
        )


def _planner_response_plan(*, tools: list[str] | None = None) -> ExecutionPlan:
    plan = _single_agent_plan()
    step = plan.steps[0].model_copy(update={"tool_permissions": tools or []})
    return plan.model_copy(update={"source": "planner", "steps": [step]})


def test_api_generates_and_validates_conservative_mock_plan(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={"title": "Plan", "goal": "Create a safe plan.", "workflow_pack": "code_rd"},
        ).json()
        response = client.post("/execution-plans/generate", json={"task_id": task["id"]})

    assert response.status_code == 200
    payload = response.json()
    assert payload["execution_plan"]["source"] == "planner"
    assert payload["public_plan_hash"]
    assert payload["run_execution_plan_hash"] is None
    assert payload["immutable_after_run_creation"] is True
    assert "execution_plan_hash" not in payload
    assert payload["mocked"] is True
    assert payload["provider"] == "mock"
    assert payload["model"]
    assert payload["selected_provider"] == payload["provider"]
    assert payload["selected_model"] == payload["model"]
    assert payload["route_receipt"] == []
    assert payload["usage"] == {}
    assert payload["usage_complete"] is True
    assert payload["estimated_cost_usd"] == 0.0
    assert payload["included_in_run_benchmark"] is False


def test_api_plan_generation_reports_actual_fallback_identity_and_unknown_cost(
    tmp_path: Path,
) -> None:
    app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )
    pack = _pack_with_real_planner()
    planner = next(agent for agent in pack.agents if agent.role == "Architect")
    planner.model_settings = {
        "provider": "litellm_proxy",
        "model": "gpt5.5",
        "allow_mock_fallback": True,
        "fallbacks": [{"provider": "mock", "model": "mock-plan"}],
    }
    plan = _planner_response_plan()
    with TestClient(app) as client:
        state = app.state.harness
        state.packs["code_rd"] = pack
        state.model_gateway = ModelGateway(
            {
                "litellm_proxy": _RetryablePlanFailureAdapter(),
                "mock": _SelectedPlanAdapter(
                    plan.model_dump_json(),
                    provider="mock",
                    mocked=True,
                    usage={"input_tokens": 10, "output_tokens": 5},
                ),
            }
        )
        task = client.post(
            "/tasks",
            json={"title": "Plan", "goal": "Create a fallback plan.", "workflow_pack": "code_rd"},
        ).json()
        response = client.post(
            "/execution-plans/generate",
            json={
                "task_id": task["id"],
                "planner_role": "Architect",
                "confirm_real_models": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "mock"
    assert payload["model"] == "mock-plan"
    assert payload["selected_provider"] == "mock"
    assert payload["selected_model"] == "mock-plan"
    assert [attempt["outcome"] for attempt in payload["route_receipt"]] == [
        "failed",
        "succeeded",
    ]
    assert payload["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert payload["usage_complete"] is False
    assert payload["estimated_cost_usd"] is None
    assert payload["included_in_run_benchmark"] is False


def test_api_plan_generation_reports_canonical_selected_route_cost(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )
    pack = _pack_with_real_planner()
    planner = next(agent for agent in pack.agents if agent.role == "Architect")
    planner.model_settings = {"provider": "deepseek", "model": "deepseek-chat"}
    plan = _planner_response_plan()
    with TestClient(app) as client:
        state = app.state.harness
        state.packs["code_rd"] = pack
        state.model_gateway = ModelGateway(
            {
                "deepseek": _SelectedPlanAdapter(
                    plan.model_dump_json(),
                    provider="deepseek",
                    mocked=False,
                    usage={"input_tokens": 100, "output_tokens": 20},
                )
            }
        )
        task = client.post(
            "/tasks",
            json={"title": "Plan", "goal": "Create a metered plan.", "workflow_pack": "code_rd"},
        ).json()
        response = client.post(
            "/execution-plans/generate",
            json={
                "task_id": task["id"],
                "planner_role": "Architect",
                "confirm_real_models": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_provider"] == "deepseek"
    assert payload["selected_model"] == "deepseek-chat"
    assert payload["usage_complete"] is True
    assert payload["estimated_cost_usd"] == pytest.approx(0.0000196)
    assert payload["estimated_cost_usd"] == payload["route_receipt"][-1]["cost_usd"]
    assert payload["included_in_run_benchmark"] is False


def test_plan_generation_rejects_disabled_dynamic_pack_before_model_call() -> None:
    pack = get_code_rd_institutional_pack()
    planner = next(agent for agent in pack.agents if agent.role == "Planner")
    planner.model_settings = {"provider": "litellm_proxy", "model": "gpt5.5"}
    adapter = _PlanAdapter("{}")

    with pytest.raises(ValueError, match="does not allow dynamic execution plans"):
        generate_execution_plan(
            task=Task(
                title="Institutional plan",
                goal="Reject dynamic generation before provider execution.",
                workflow_pack="code_rd_institutional",
            ),
            pack=pack,
            model_gateway=ModelGateway({"litellm_proxy": adapter}),
            planner_role="Planner",
        )

    assert adapter.requests == []


def test_api_rejects_unknown_planner_role(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )
    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={"title": "Plan", "goal": "Create a plan.", "workflow_pack": "code_rd"},
        ).json()
        response = client.post(
            "/execution-plans/generate",
            json={"task_id": task["id"], "planner_role": "Unknown"},
        )

    assert response.status_code == 400
    assert "Planner role is not available" in response.json()["detail"]


def test_api_requires_confirmation_before_real_model_plan_generation(tmp_path: Path) -> None:
    app = create_app(
        db_path=tmp_path / "harness.sqlite3",
        artifact_root=tmp_path / "artifacts",
        config_root=tmp_path,
    )
    with TestClient(app) as client:
        state = app.state.harness
        state.packs["code_rd"] = _pack_with_real_planner()
        task = client.post(
            "/tasks",
            json={"title": "Plan", "goal": "Create a real plan.", "workflow_pack": "code_rd"},
        ).json()
        response = client.post(
            "/execution-plans/generate",
            json={"task_id": task["id"], "planner_role": "Architect"},
        )

    assert response.status_code == 400
    assert "confirm_real_models=true" in response.json()["detail"]


def test_plan_generation_accepts_plain_json_from_real_model() -> None:
    pack = _pack_with_real_planner()
    plan = _planner_response_plan()

    result = generate_execution_plan(
        task=_task_for_plan_generation(),
        pack=pack,
        model_gateway=ModelGateway(
            {"litellm_proxy": _PlanAdapter(plan.model_dump_json())}
        ),
        planner_role="Architect",
    )

    expected = validate_execution_plan_against_pack(plan, pack)
    assert result.plan.model_copy(update={"agent_snapshots": []}) == expected
    assert result.plan.agent_snapshots
    assert result.response is not None and result.response.mocked is False


def test_plan_generation_inherits_trusted_planner_request_limits() -> None:
    pack = _pack_with_real_planner()
    planner = next(agent for agent in pack.agents if agent.role == "Architect")
    planner.model_settings = {
        "provider": "litellm_proxy",
        "model": "gpt5.5",
        "temperature": 0.75,
        "max_tokens": 1234,
        "reasoning_effort": "high",
    }
    adapter = _PlanAdapter(_planner_response_plan().model_dump_json())

    generate_execution_plan(
        task=_task_for_plan_generation(),
        pack=pack,
        model_gateway=ModelGateway({"litellm_proxy": adapter}),
        planner_role="Architect",
    )

    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.temperature == 0.75
    assert request.max_tokens == 1234
    assert request.reasoning_effort == "high"


@pytest.mark.parametrize(
    ("response_text", "message"),
    [
        ("```json\n{}\n```", "not valid execution plan JSON"),
        ("not-json", "not valid execution plan JSON"),
    ],
)
def test_plan_generation_rejects_non_plain_json(response_text: str, message: str) -> None:
    pack = _pack_with_real_planner()

    with pytest.raises(ValueError, match=message):
        generate_execution_plan(
            task=_task_for_plan_generation(),
            pack=pack,
            model_gateway=ModelGateway(
                {"litellm_proxy": _PlanAdapter(response_text)}
            ),
            planner_role="Architect",
        )


def test_plan_generation_rejects_tool_permission_expansion() -> None:
    pack = _pack_with_real_planner()
    plan = _planner_response_plan(tools=["run_test_command"])

    with pytest.raises(ValueError, match="expands agent tool permissions"):
        generate_execution_plan(
            task=_task_for_plan_generation(),
            pack=pack,
            model_gateway=ModelGateway(
                {"litellm_proxy": _PlanAdapter(plan.model_dump_json())}
            ),
            planner_role="Architect",
        )


def _task_for_plan_generation() -> Task:
    return Task(
        title="Generate plan",
        goal="Produce a frozen execution plan.",
        workflow_pack="code_rd",
    )


def _pack_with_real_planner():
    pack = get_code_rd_pack()
    planner = next(agent for agent in pack.agents if agent.role == "Architect")
    planner.model_settings = {"provider": "litellm_proxy", "model": "gpt5.5"}
    return pack
