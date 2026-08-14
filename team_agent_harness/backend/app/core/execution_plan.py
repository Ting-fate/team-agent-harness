from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from typing import Any, Literal

from pydantic import ConfigDict, Field, model_validator

from app.core.models import AgentDefinition, ArtifactType, HarnessModel
from app.packs.base import (
    AgentLoopPolicy,
    ContextPolicy,
    EvalCheck,
    ReturnContract,
    SessionPolicy,
    StepAcceptanceCriterion,
    WorkflowPack,
    WorkflowStep,
)


EXECUTION_PLAN_SCHEMA_VERSION = "execution-plan-v1"
_TEAM_SELECTION_VERSION_KEY = "team_selection_version"
_TEAM_SELECTION_MANIFEST_KEY = "team_selection_manifest_hash"
_TEAM_SELECTION_VERSION = "team-selection-v1"


class ExecutionPlanAgent(HarnessModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    agent_id: str = Field(min_length=1)
    role: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field(min_length=1)
    model_settings: dict[str, Any] = Field(default_factory=dict)
    tool_permissions: list[str] = Field(default_factory=list, max_length=32)
    runtime_limits: dict[str, Any] = Field(default_factory=dict)
    effective_skill_ids: list[str] = Field(default_factory=list)


class ExecutionPlanStep(HarnessModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    step_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    objective: str = Field(min_length=1, max_length=10_000)
    agent_role: str = Field(min_length=1, max_length=200)
    dependencies: list[str] = Field(default_factory=list, max_length=32)
    inputs: list[str] = Field(default_factory=list, max_length=64)
    required_artifacts: list[ArtifactType] = Field(default_factory=list, max_length=16)
    expected_artifacts: list[ArtifactType] = Field(min_length=1, max_length=1)
    acceptance_criteria: list[StepAcceptanceCriterion] = Field(default_factory=list, max_length=16)
    tool_permissions: list[str] = Field(default_factory=list, max_length=32)
    ownership: dict[str, list[str]] = Field(default_factory=dict, max_length=16)
    phase: str | None = Field(default=None, min_length=1, max_length=100)
    coordination_role: Literal["controller", "subagent", "gate", "synthesizer"] | None = None
    controller_step: str | None = Field(default=None, min_length=1, max_length=100)
    return_contract: ReturnContract | None = None
    runtime: Literal["model", "session", "acp"] = "model"
    session_policy: SessionPolicy = Field(default_factory=SessionPolicy)
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)
    agent_loop: AgentLoopPolicy = Field(default_factory=AgentLoopPolicy)
    requires_eval_pass: bool = False
    required_eval_checks: list[str] = Field(default_factory=list, max_length=16)
    requires_artifact: list[ArtifactType] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_step(self) -> "ExecutionPlanStep":
        for field_name, values in {
            "dependencies": self.dependencies,
            "inputs": self.inputs,
            "tool_permissions": self.tool_permissions,
            "required_eval_checks": self.required_eval_checks,
        }.items():
            if len(values) != len(set(values)):
                raise ValueError(f"Execution plan step {self.step_id} has duplicate {field_name}.")
        criterion_names = [criterion.name for criterion in self.acceptance_criteria]
        if len(criterion_names) != len(set(criterion_names)):
            raise ValueError(f"Execution plan step {self.step_id} has duplicate acceptance criteria.")
        return self


class ExecutionPlan(HarnessModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    schema_version: Literal["execution-plan-v1"] = EXECUTION_PLAN_SCHEMA_VERSION
    workflow_pack: str = Field(min_length=1, max_length=100)
    source: Literal["workflow_pack", "planner", "operator"] = "operator"
    final_artifact_type: ArtifactType
    max_parallel_steps: int = Field(default=4, ge=1, le=16)
    steps: list[ExecutionPlanStep] = Field(min_length=1, max_length=32)
    eval_checks: list[EvalCheck] = Field(default_factory=list, max_length=32)
    agent_snapshots: list[ExecutionPlanAgent] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_plan(self) -> "ExecutionPlan":
        step_ids = [step.step_id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Execution plan step ids must be unique.")
        snapshot_roles = [snapshot.role for snapshot in self.agent_snapshots]
        snapshot_ids = [snapshot.agent_id for snapshot in self.agent_snapshots]
        if len(snapshot_roles) != len(set(snapshot_roles)):
            raise ValueError("Execution plan agent snapshot roles must be unique.")
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise ValueError("Execution plan agent snapshot ids must be unique.")
        eval_check_names = [check.name for check in self.eval_checks]
        if len(eval_check_names) != len(set(eval_check_names)):
            raise ValueError("Execution plan eval check names must be unique.")
        if self.agent_snapshots:
            missing_snapshot_roles = sorted(set(step.agent_role for step in self.steps) - set(snapshot_roles))
            if missing_snapshot_roles:
                raise ValueError(
                    "Execution plan is missing agent snapshots for roles: "
                    f"{', '.join(missing_snapshot_roles)}"
                )
        known_steps = set(step_ids)
        for step in self.steps:
            unknown = sorted(set(step.dependencies) - known_steps)
            if unknown:
                raise ValueError(
                    f"Execution plan step {step.step_id} has unknown dependencies: {', '.join(unknown)}"
                )
            if step.step_id in step.dependencies:
                raise ValueError(f"Execution plan step {step.step_id} cannot depend on itself.")
            if step.controller_step is not None and step.controller_step not in known_steps:
                raise ValueError(f"Execution plan step {step.step_id} has an unknown controller_step.")
            if self.source != "workflow_pack" and step.runtime != "model":
                raise ValueError("Planner/operator execution plans may create model runtime steps only.")
            if self.source != "workflow_pack" and not step.acceptance_criteria:
                raise ValueError(
                    f"Planner/operator execution plan step {step.step_id} requires acceptance_criteria."
                )
        cycle = _dependency_cycle(self.steps)
        if cycle:
            raise ValueError(f"Execution plan contains a dependency cycle: {' -> '.join(cycle)}")
        depended_on = {dependency for step in self.steps for dependency in step.dependencies}
        sinks = [step for step in self.steps if step.step_id not in depended_on]
        if not any(self.final_artifact_type in step.expected_artifacts for step in sinks):
            raise ValueError("A terminal execution plan step must produce final_artifact_type.")
        return self


def execution_plan_from_pack(pack: WorkflowPack) -> ExecutionPlan:
    steps = []
    for step in pack.steps:
        if step.produces_artifact_type is None:
            raise ValueError(f"Workflow pack step {step.name} must declare produces_artifact_type to freeze a plan.")
        artifact_type = ArtifactType(step.produces_artifact_type)
        acceptance_criteria = step.acceptance_criteria or [
            StepAcceptanceCriterion(
                name="artifact-nonempty",
                kind="artifact_nonempty",
                artifact_type=artifact_type,
            )
        ]
        steps.append(
            ExecutionPlanStep(
                step_id=step.name,
                objective=step.objective or step.name,
                agent_role=step.agent_role,
                dependencies=step.depends_on,
                inputs=step.required_inputs,
                required_artifacts=[ArtifactType(value) for value in step.required_artifacts],
                expected_artifacts=[artifact_type],
                acceptance_criteria=acceptance_criteria,
                tool_permissions=step.allowed_tools,
                ownership=step.ownership,
                phase=step.phase,
                coordination_role=step.coordination_role,
                controller_step=step.controller_step,
                return_contract=step.return_contract,
                runtime=step.runtime,
                session_policy=step.session_policy,
                context_policy=step.context_policy,
                agent_loop=step.agent_loop,
                requires_eval_pass=step.requires_eval_pass,
                required_eval_checks=step.required_eval_checks,
                requires_artifact=[ArtifactType(value) for value in step.requires_artifact],
            )
        )
    plan = ExecutionPlan(
        workflow_pack=pack.name,
        source="workflow_pack",
        final_artifact_type=ArtifactType(pack.final_artifact_type),
        max_parallel_steps=pack.max_parallel_steps,
        steps=steps,
        eval_checks=pack.eval_checks,
    )
    return plan.model_copy(update={"agent_snapshots": _agent_snapshots_for_plan(plan, pack)})


def validate_execution_plan_against_pack(
    plan: ExecutionPlan,
    pack: WorkflowPack,
    *,
    allow_frozen_workflow_pack_snapshot: bool = False,
) -> ExecutionPlan:
    if plan.workflow_pack != pack.name:
        raise ValueError(
            f"Execution plan expects workflow pack {plan.workflow_pack}, got {pack.name}."
        )
    if allow_frozen_workflow_pack_snapshot and not plan.agent_snapshots:
        raise ValueError("Persisted execution plan is missing agent snapshots.")
    expected_snapshots = _agent_snapshots_for_plan(plan, pack)
    if (
        plan.agent_snapshots
        and not allow_frozen_workflow_pack_snapshot
        and plan.agent_snapshots != expected_snapshots
    ):
        raise ValueError("Execution plan agent snapshots do not match the current Pack agents.")
    if (
        plan.source == "workflow_pack"
        and not allow_frozen_workflow_pack_snapshot
        and _without_agent_snapshots(plan) != _without_agent_snapshots(execution_plan_from_pack(pack))
    ):
        raise ValueError("A workflow_pack execution plan must exactly match the current Pack snapshot.")
    agents_by_role = {
        agent.role: agent
        for agent in (
            [_agent_definition_from_snapshot(snapshot, pack.name) for snapshot in plan.agent_snapshots]
            if allow_frozen_workflow_pack_snapshot and plan.agent_snapshots
            else pack.agents
        )
    }
    for step in plan.steps:
        agent = agents_by_role.get(step.agent_role)
        if agent is None:
            raise ValueError(
                f"Execution plan step {step.step_id} references unknown agent role {step.agent_role}."
            )
        extra_tools = sorted(set(step.tool_permissions) - set(agent.tool_permissions))
        if extra_tools:
            raise ValueError(
                f"Execution plan step {step.step_id} expands agent tool permissions: {', '.join(extra_tools)}"
            )
        if step.agent_loop.enabled and not step.tool_permissions:
            raise ValueError(f"Execution plan step {step.step_id} enables an agent loop without tools.")
        if step.agent_loop.enabled:
            _validate_agent_loop_runtime_limits(step, agent)
        criterion_artifact_types = {
            criterion.artifact_type
            for criterion in step.acceptance_criteria
            if criterion.artifact_type is not None
        }
        undeclared_criterion_types = sorted(
            artifact_type.value
            for artifact_type in criterion_artifact_types - set(step.expected_artifacts)
        )
        if undeclared_criterion_types:
            raise ValueError(
                f"Execution plan step {step.step_id} has acceptance criteria for undeclared artifacts: "
                f"{', '.join(undeclared_criterion_types)}"
            )
    if plan.source != "workflow_pack":
        if not allow_frozen_workflow_pack_snapshot:
            if not pack.allow_dynamic_execution_plans:
                raise ValueError(f"Workflow pack {pack.name} does not allow dynamic execution plans.")
            plan = _preserve_dynamic_pack_blocker_evals(plan, pack)
            _validate_dynamic_role_usage(plan, pack)
        _validate_dynamic_plan_dataflow(plan)
    if (
        not allow_frozen_workflow_pack_snapshot
        and plan.max_parallel_steps > pack.max_parallel_steps
    ):
        raise ValueError("Execution plan max_parallel_steps exceeds the workflow Pack limit.")
    return plan


def freeze_execution_plan(plan: ExecutionPlan, pack: WorkflowPack) -> ExecutionPlan:
    candidate = plan.model_copy(update={"agent_snapshots": []})
    validated = validate_execution_plan_against_pack(candidate, pack)
    return validated.model_copy(
        update={"agent_snapshots": _agent_snapshots_for_plan(validated, pack)}
    )


def _agent_snapshots_for_plan(
    plan: ExecutionPlan,
    pack: WorkflowPack,
) -> list[ExecutionPlanAgent]:
    required_roles = (
        {agent.role for agent in pack.agents}
        if _pack_has_complete_team_selection(pack)
        else {step.agent_role for step in plan.steps}
    )
    return [
        ExecutionPlanAgent(
            agent_id=agent.id,
            role=agent.role,
            system_prompt=agent.system_prompt,
            model_settings=agent.model_settings,
            tool_permissions=agent.tool_permissions,
            runtime_limits=agent.runtime_limits,
            effective_skill_ids=agent.effective_skill_ids,
        )
        for agent in pack.agents
        if agent.role in required_roles
    ]


def _pack_has_complete_team_selection(pack: WorkflowPack) -> bool:
    marker_states: list[bool] = []
    manifest_hashes: set[str] = set()
    for agent in pack.agents:
        version = agent.model_settings.get(_TEAM_SELECTION_VERSION_KEY)
        manifest_hash = agent.model_settings.get(_TEAM_SELECTION_MANIFEST_KEY)
        if version is None and manifest_hash is None:
            marker_states.append(False)
            continue
        if (
            version != _TEAM_SELECTION_VERSION
            or not isinstance(manifest_hash, str)
            or len(manifest_hash) != 64
            or any(character not in "0123456789abcdef" for character in manifest_hash)
        ):
            raise ValueError("Workflow Pack contains invalid frozen team selection metadata.")
        marker_states.append(True)
        manifest_hashes.add(manifest_hash)

    if any(marker_states) and not all(marker_states):
        raise ValueError("Workflow Pack contains partial frozen team selection metadata.")
    if len(manifest_hashes) > 1:
        raise ValueError("Workflow Pack contains inconsistent frozen team selection metadata.")
    return bool(marker_states) and all(marker_states)


def _without_agent_snapshots(plan: ExecutionPlan) -> ExecutionPlan:
    return plan.model_copy(update={"agent_snapshots": []})


def _agent_definition_from_snapshot(
    snapshot: ExecutionPlanAgent,
    pack_name: str,
) -> AgentDefinition:
    return AgentDefinition(
        id=snapshot.agent_id,
        pack_name=pack_name,
        role=snapshot.role,
        system_prompt=snapshot.system_prompt,
        model_config=snapshot.model_settings,
        tool_permissions=snapshot.tool_permissions,
        runtime_limits=snapshot.runtime_limits,
        effective_skill_ids=snapshot.effective_skill_ids,
    )


def _validate_agent_loop_runtime_limits(
    step: ExecutionPlanStep,
    agent: AgentDefinition,
) -> None:
    policy = step.agent_loop
    comparable_limits: tuple[tuple[str, int | float | None], ...] = (
        ("max_steps", policy.max_steps),
        ("max_tool_calls", policy.max_tool_calls),
        ("max_total_tokens", policy.max_total_tokens),
        ("timeout_seconds", policy.timeout_seconds),
        ("max_repeated_tool_calls", policy.max_repeated_tool_calls),
        ("max_observation_chars", policy.max_observation_chars),
        ("max_cost_usd", policy.max_cost_usd),
    )
    for limit_name, requested in comparable_limits:
        allowed = agent.runtime_limits.get(limit_name)
        if allowed is None:
            continue
        if isinstance(allowed, bool) or not isinstance(allowed, (int, float)):
            raise ValueError(
                f"Agent role {agent.role} has an invalid {limit_name} runtime limit."
            )
        if requested is None:
            if limit_name == "max_cost_usd":
                raise ValueError(
                    f"Execution plan step {step.step_id} requires a max_cost_usd ceiling."
                )
            continue
        if requested > allowed:
            raise ValueError(
                f"Execution plan step {step.step_id} exceeds agent {limit_name} runtime limit."
            )


def _validate_dynamic_plan_dataflow(plan: ExecutionPlan) -> None:
    steps_by_id = {step.step_id: step for step in plan.steps}
    upstream_cache: dict[str, set[str]] = {}

    def upstream_steps(step_id: str) -> set[str]:
        if step_id in upstream_cache:
            return upstream_cache[step_id]
        upstream: set[str] = set()
        for dependency in steps_by_id[step_id].dependencies:
            upstream.add(dependency)
            upstream.update(upstream_steps(dependency))
        upstream_cache[step_id] = upstream
        return upstream

    for step in plan.steps:
        available_artifacts = {
            artifact_type
            for dependency in upstream_steps(step.step_id)
            for artifact_type in steps_by_id[dependency].expected_artifacts
        }
        required_artifacts = set(step.required_artifacts) | set(step.requires_artifact)
        missing = sorted(
            artifact_type.value
            for artifact_type in required_artifacts - available_artifacts
        )
        if missing:
            raise ValueError(
                f"Execution plan step {step.step_id} requires artifacts not produced by its dependencies: "
                f"{', '.join(missing)}"
            )


def _preserve_dynamic_pack_blocker_evals(
    plan: ExecutionPlan,
    pack: WorkflowPack,
) -> ExecutionPlan:
    produced_artifact_types = {
        artifact_type.value
        for step in plan.steps
        for artifact_type in step.expected_artifacts
    }
    required_checks = [
        check
        for check in pack.eval_checks
        if check.severity == "blocker"
        and check.required_artifact_types
        and set(check.required_artifact_types).issubset(produced_artifact_types)
    ]
    checks_by_name = {check.name: check for check in plan.eval_checks}
    missing_checks: list[EvalCheck] = []
    for required in required_checks:
        existing = checks_by_name.get(required.name)
        if existing is not None and existing != required:
            raise ValueError(
                f"Execution plan eval {required.name} conflicts with workflow Pack blocker eval."
            )
        if existing is None:
            missing_checks.append(required)
    if not missing_checks:
        return plan
    if len(plan.eval_checks) + len(missing_checks) > 32:
        raise ValueError(
            "Required workflow Pack blocker evals exceed the 32-check execution plan limit."
        )
    updated = plan.model_copy(
        update={"eval_checks": [*plan.eval_checks, *missing_checks]}
    )
    return ExecutionPlan.model_validate(updated.model_dump(mode="json"))


def _validate_dynamic_role_usage(plan: ExecutionPlan, pack: WorkflowPack) -> None:
    planned_role_counts = Counter(step.agent_role for step in plan.steps)
    static_role_counts = Counter(step.agent_role for step in pack.steps)
    for role, planned_count in sorted(planned_role_counts.items()):
        allowed_count = static_role_counts.get(role, 0)
        if planned_count > allowed_count:
            raise ValueError(
                f"Execution plan repeats agent role {role} {planned_count} times; "
                f"workflow Pack allows {allowed_count}."
            )


def workflow_pack_from_execution_plan(
    plan: ExecutionPlan,
    base_pack: WorkflowPack,
    *,
    allow_frozen_workflow_pack_snapshot: bool = False,
) -> WorkflowPack:
    plan = validate_execution_plan_against_pack(
        plan,
        base_pack,
        allow_frozen_workflow_pack_snapshot=allow_frozen_workflow_pack_snapshot,
    )
    steps = [
        WorkflowStep(
            name=step.step_id,
            objective=step.objective,
            agent_role=step.agent_role,
            required_inputs=step.inputs,
            required_artifacts=[artifact.value for artifact in step.required_artifacts],
            allowed_tools=step.tool_permissions,
            depends_on=step.dependencies,
            phase=step.phase,
            produces_artifact_type=step.expected_artifacts[0].value,
            coordination_role=step.coordination_role,
            controller_step=step.controller_step,
            return_contract=step.return_contract
            or ReturnContract(required_artifact_types=[artifact.value for artifact in step.expected_artifacts]),
            runtime=step.runtime,
            session_policy=step.session_policy,
            context_policy=step.context_policy,
            agent_loop=step.agent_loop,
            execution_source=plan.source,
            acceptance_criteria=step.acceptance_criteria,
            requires_eval_pass=step.requires_eval_pass,
            required_eval_checks=step.required_eval_checks,
            requires_artifact=[artifact.value for artifact in step.requires_artifact],
            ownership=step.ownership,
        )
        for step in plan.steps
    ]
    return WorkflowPack(
        name=base_pack.name,
        description=f"Frozen execution plan for {base_pack.description}",
        agents=(
            [_agent_definition_from_snapshot(snapshot, base_pack.name) for snapshot in plan.agent_snapshots]
            if plan.agent_snapshots
            else base_pack.agents
        ),
        steps=steps,
        eval_checks=plan.eval_checks,
        final_artifact_type=plan.final_artifact_type.value,
        max_parallel_steps=plan.max_parallel_steps,
        allow_dynamic_execution_plans=base_pack.allow_dynamic_execution_plans,
    )


def execution_plan_hash(plan: ExecutionPlan) -> str:
    payload = json.dumps(
        plan.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def parse_execution_plan_json(value: str, pack: WorkflowPack) -> ExecutionPlan:
    try:
        raw: Any = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Planner output is not valid execution plan JSON.") from exc
    plan = ExecutionPlan.model_validate(raw)
    return validate_execution_plan_against_pack(plan, pack)


def _dependency_cycle(steps: list[ExecutionPlanStep]) -> list[str]:
    dependencies = {step.step_id: step.dependencies for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()
    path: list[str] = []

    def visit(step_id: str) -> list[str]:
        if step_id in visiting:
            start = path.index(step_id)
            return [*path[start:], step_id]
        if step_id in visited:
            return []
        visiting.add(step_id)
        path.append(step_id)
        for dependency in dependencies[step_id]:
            cycle = visit(dependency)
            if cycle:
                return cycle
        path.pop()
        visiting.remove(step_id)
        visited.add(step_id)
        return []

    for step_id in dependencies:
        cycle = visit(step_id)
        if cycle:
            return cycle
    return []
