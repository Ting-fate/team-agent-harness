from __future__ import annotations

from dataclasses import dataclass
import json

from app.core.execution_plan import (
    ExecutionPlan,
    execution_plan_from_pack,
    freeze_execution_plan,
    parse_execution_plan_json,
)
from app.core.model_runtime import (
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    default_reasoning_effort_for_model,
    model_allow_mock_fallback_from_config,
    model_fallbacks_from_config,
)
from app.core.models import AgentDefinition, Task
from app.packs.base import SessionPolicy, StepAcceptanceCriterion, WorkflowPack


@dataclass(frozen=True)
class ExecutionPlanGenerationResult:
    plan: ExecutionPlan
    planner: AgentDefinition
    request: ModelRequest | None
    response: ModelResponse | None


def generate_execution_plan(
    *,
    task: Task,
    pack: WorkflowPack,
    model_gateway: ModelGateway,
    planner_role: str | None = None,
) -> ExecutionPlanGenerationResult:
    if not pack.allow_dynamic_execution_plans:
        raise ValueError(f"Workflow pack {pack.name} does not allow dynamic execution plans.")
    planner = select_planner_agent(pack, planner_role)
    provider = str(planner.model_settings.get("provider", "mock"))
    if provider == "mock":
        return ExecutionPlanGenerationResult(
            plan=freeze_execution_plan(_conservative_mock_plan(pack), pack),
            planner=planner,
            request=None,
            response=None,
        )

    model = str(planner.model_settings.get("model", ""))
    request = ModelRequest(
        provider=provider,
        model=model,
        system_prompt=(
            f"{planner.system_prompt}\n\n"
            "Return one execution-plan-v1 JSON object only. Do not use markdown fences. "
            "Use only the supplied agent roles and each role's supplied tools. "
            "Every step must use runtime=model and have deterministic acceptance_criteria. "
            "Omit agent_snapshots; the Harness adds trusted snapshots after validation."
        ),
        messages=[
            ModelMessage(
                role="user",
                content=json.dumps(
                    {
                        "task": {
                            "title": task.title,
                            "goal": task.goal,
                            "constraints": task.constraints,
                            "acceptance_criteria": task.acceptance_criteria,
                            "input_keys": sorted(task.inputs),
                        },
                        "workflow_pack": pack.name,
                        "available_agents": [
                            {
                                "role": agent.role,
                                "tool_permissions": agent.tool_permissions,
                            }
                            for agent in pack.agents
                        ],
                        "execution_plan_schema": _planner_execution_plan_schema(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
        ],
        temperature=0.0,
        max_tokens=max(4096, int(planner.model_settings.get("max_tokens", 4096))),
        reasoning_effort=(
            str(planner.model_settings["reasoning_effort"])
            if planner.model_settings.get("reasoning_effort") is not None
            else default_reasoning_effort_for_model(provider, model)
        ),
        fallbacks=model_fallbacks_from_config(planner.model_settings),
        metadata={
            "task_title": task.title,
            "step_name": "generate_execution_plan",
            "agent_id": planner.id,
            "agent_role": planner.role,
            "context_keys": ["task", "available_agents", "execution_plan_schema"],
            "allow_mock_fallback": model_allow_mock_fallback_from_config(planner.model_settings),
        },
    )
    response = model_gateway.complete(request)
    plan = parse_execution_plan_json(response.text, pack)
    if plan.source != "planner":
        raise ValueError("Generated execution plan must declare source=planner.")
    return ExecutionPlanGenerationResult(
        plan=freeze_execution_plan(plan, pack),
        planner=planner,
        request=request,
        response=response,
    )


def select_planner_agent(pack: WorkflowPack, planner_role: str | None) -> AgentDefinition:
    if planner_role is not None:
        for agent in pack.agents:
            if agent.role == planner_role:
                return agent
        raise ValueError(f"Planner role is not available in workflow pack: {planner_role}")
    preferred = {"Planner": 0, "Architect": 1, "Clarifier": 2}
    return min(
        pack.agents,
        key=lambda agent: (preferred.get(agent.role, 3), pack.agents.index(agent)),
    )


def _conservative_mock_plan(pack: WorkflowPack) -> ExecutionPlan:
    static_plan = execution_plan_from_pack(pack)
    steps = []
    previous_step_id: str | None = None
    for step in static_plan.steps:
        criteria = step.acceptance_criteria or [
            StepAcceptanceCriterion(
                name="artifact-nonempty",
                kind="artifact_nonempty",
                artifact_type=step.expected_artifacts[0],
            )
        ]
        steps.append(
            step.model_copy(
                update={
                    "runtime": "model",
                    "session_policy": SessionPolicy(),
                    "acceptance_criteria": criteria,
                    "dependencies": step.dependencies or ([previous_step_id] if previous_step_id else []),
                }
            )
        )
        previous_step_id = step.step_id
    return ExecutionPlan(
        workflow_pack=pack.name,
        source="planner",
        final_artifact_type=static_plan.final_artifact_type,
        max_parallel_steps=static_plan.max_parallel_steps,
        steps=steps,
        eval_checks=static_plan.eval_checks,
    )


def _planner_execution_plan_schema() -> dict[str, object]:
    schema = ExecutionPlan.model_json_schema()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        properties.pop("agent_snapshots", None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [name for name in required if name != "agent_snapshots"]
    return schema
