from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.core.models import AgentDefinition, ArtifactType, HarnessModel


class EvalCheck(HarnessModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    severity: Literal["blocker", "warning"] = "blocker"
    required_artifact_types: list[str] = Field(default_factory=list)


class ReturnContract(HarnessModel):
    required_artifact_types: list[str] = Field(default_factory=list)
    require_summary: bool = True
    require_open_questions: bool = False
    require_risk_notes: bool = False


class SessionPolicy(HarnessModel):
    persistent: bool = False
    resume_strategy: Literal["none", "latest_artifact_and_trace", "full_trace", "manual"] = "none"
    requires_approval: bool = False


class ContextPolicy(HarnessModel):
    artifact_excerpt_chars: int = Field(default=0, ge=0, le=100_000)
    max_artifacts: int = Field(default=8, ge=0, le=32)
    max_upstream_handoffs: int = Field(default=8, ge=0, le=32)
    max_context_chars: int = Field(default=100_000, ge=10_000, le=1_000_000)
    max_context_bytes: int = Field(default=300_000, ge=10_000, le=3_000_000)


class WorkflowStep(HarnessModel):
    name: str = Field(min_length=1)
    agent_role: str = Field(min_length=1)
    required_inputs: list[str] = Field(default_factory=list)
    required_artifacts: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    phase: str | None = Field(default=None, min_length=1)
    produces_artifact_type: str | None = Field(default=None, min_length=1)
    coordination_role: Literal["controller", "subagent", "gate", "synthesizer"] | None = None
    controller_step: str | None = Field(default=None, min_length=1)
    return_contract: ReturnContract | None = None
    runtime: Literal["model", "session", "acp"] = "model"
    session_policy: SessionPolicy = Field(default_factory=SessionPolicy)
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)
    requires_eval_pass: bool = False
    requires_artifact: list[str] = Field(default_factory=list)
    ownership: dict[str, list[str]] = Field(default_factory=dict)


class WorkflowPack(HarnessModel):
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    agents: list[AgentDefinition] = Field(min_length=1)
    steps: list[WorkflowStep] = Field(min_length=1)
    eval_checks: list[EvalCheck] = Field(default_factory=list)
    final_artifact_type: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_pack(self) -> "WorkflowPack":
        wrong_pack_agents = sorted(agent.id for agent in self.agents if agent.pack_name != self.name)
        if wrong_pack_agents:
            raise ValueError(f"Agents declare a different pack_name: {', '.join(wrong_pack_agents)}")

        agent_roles = [agent.role for agent in self.agents]
        duplicate_roles = _duplicates(agent_roles)
        if duplicate_roles:
            raise ValueError(f"Duplicate agent roles: {', '.join(duplicate_roles)}")

        step_names = [step.name for step in self.steps]
        duplicate_steps = _duplicates(step_names)
        if duplicate_steps:
            raise ValueError(f"Duplicate workflow steps: {', '.join(duplicate_steps)}")

        missing_roles = sorted({step.agent_role for step in self.steps} - set(agent_roles))
        if missing_roles:
            raise ValueError(f"Steps reference undefined agent roles: {', '.join(missing_roles)}")

        missing_dependencies = sorted(
            dependency
            for step in self.steps
            for dependency in step.depends_on
            if dependency not in set(step_names)
        )
        if missing_dependencies:
            raise ValueError(f"Steps reference undefined dependencies: {', '.join(missing_dependencies)}")

        self_dependencies = sorted(step.name for step in self.steps if step.name in step.depends_on)
        if self_dependencies:
            raise ValueError(f"Steps cannot depend on themselves: {', '.join(self_dependencies)}")

        missing_controller_steps = sorted(
            step.name
            for step in self.steps
            if step.coordination_role == "subagent" and step.controller_step is None
        )
        if missing_controller_steps:
            raise ValueError(
                "Subagent steps must declare controller_step: "
                f"{', '.join(missing_controller_steps)}"
            )

        unknown_controller_steps = sorted(
            step.controller_step
            for step in self.steps
            if step.controller_step is not None and step.controller_step not in set(step_names)
        )
        if unknown_controller_steps:
            raise ValueError(f"Steps reference undefined controller_step: {', '.join(unknown_controller_steps)}")

        invalid_controller_links = sorted(
            step.name
            for step in self.steps
            if step.controller_step is not None
            and step.controller_step not in _upstream_steps(step.name, self.steps)
        )
        if invalid_controller_links:
            raise ValueError(
                "Step controller_step must be an upstream dependency: "
                f"{', '.join(invalid_controller_links)}"
            )

        non_persistent_resume_steps = sorted(
            step.name
            for step in self.steps
            if step.session_policy.resume_strategy != "none" and not step.session_policy.persistent
        )
        if non_persistent_resume_steps:
            raise ValueError(
                "Session resume_strategy requires persistent=true: "
                f"{', '.join(non_persistent_resume_steps)}"
            )

        acp_without_approval_steps = sorted(
            step.name
            for step in self.steps
            if step.runtime == "acp" and not step.session_policy.requires_approval
        )
        if acp_without_approval_steps:
            raise ValueError(
                "ACP runtime steps must require approval: "
                f"{', '.join(acp_without_approval_steps)}"
            )

        cycle = _dependency_cycle(self.steps)
        if cycle:
            raise ValueError(f"Workflow step dependencies contain a cycle: {' -> '.join(cycle)}")

        artifact_values = {artifact_type.value for artifact_type in ArtifactType}
        invalid_required_artifact_types = sorted(
            required
            for step in self.steps
            for required in [*step.required_artifacts, *step.requires_artifact]
            if required not in artifact_values
        )
        if invalid_required_artifact_types:
            raise ValueError(
                "Steps require unsupported artifact types: "
                f"{', '.join(invalid_required_artifact_types)}"
            )

        invalid_eval_artifact_types = sorted(
            required
            for check in self.eval_checks
            for required in check.required_artifact_types
            if required not in artifact_values
        )
        if invalid_eval_artifact_types:
            raise ValueError(
                "Eval checks require unsupported artifact types: "
                f"{', '.join(invalid_eval_artifact_types)}"
            )

        invalid_return_contract_artifact_types = sorted(
            required
            for step in self.steps
            if step.return_contract is not None
            for required in step.return_contract.required_artifact_types
            if required not in artifact_values
        )
        if invalid_return_contract_artifact_types:
            raise ValueError(
                "Return contracts require unsupported artifact types: "
                f"{', '.join(invalid_return_contract_artifact_types)}"
            )

        invalid_produced_artifact_types = sorted(
            produced
            for produced in (step.produces_artifact_type for step in self.steps)
            if produced is not None and produced not in artifact_values
        )
        if invalid_produced_artifact_types:
            raise ValueError(
                f"Steps declare unsupported produced artifact types: {', '.join(invalid_produced_artifact_types)}"
            )

        if self.final_artifact_type not in artifact_values:
            raise ValueError(f"Unsupported final_artifact_type: {self.final_artifact_type}")

        produced_artifact_types = {step.produces_artifact_type for step in self.steps if step.produces_artifact_type}
        if produced_artifact_types and self.final_artifact_type not in produced_artifact_types:
            raise ValueError(f"No step declares final_artifact_type: {self.final_artifact_type}")

        return self


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for value in values:
        if value in seen:
            dupes.add(value)
        seen.add(value)
    return sorted(dupes)


def _dependency_cycle(steps: list[WorkflowStep]) -> list[str]:
    dependencies = {step.name: step.depends_on for step in steps}
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(step_name: str) -> list[str]:
        if step_name in visited:
            return []
        if step_name in visiting:
            try:
                start = stack.index(step_name)
            except ValueError:
                start = 0
            return [*stack[start:], step_name]

        visiting.add(step_name)
        stack.append(step_name)
        for dependency in dependencies.get(step_name, []):
            cycle = visit(dependency)
            if cycle:
                return cycle
        stack.pop()
        visiting.remove(step_name)
        visited.add(step_name)
        return []

    for step in steps:
        cycle = visit(step.name)
        if cycle:
            return cycle
    return []


def _upstream_steps(step_name: str, steps: list[WorkflowStep]) -> set[str]:
    dependencies = {step.name: step.depends_on for step in steps}
    upstream: set[str] = set()
    pending = list(dependencies.get(step_name, []))
    while pending:
        dependency = pending.pop()
        if dependency in upstream:
            continue
        upstream.add(dependency)
        pending.extend(dependencies.get(dependency, []))
    return upstream
