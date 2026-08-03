import pytest
from pydantic import ValidationError

from app.core.models import AgentDefinition
from app.packs.base import ContextPolicy, EvalCheck, ReturnContract, SessionPolicy, WorkflowPack, WorkflowStep


def _agent(role: str) -> AgentDefinition:
    return AgentDefinition(
        id=f"agent-{role.lower()}",
        pack_name="demo",
        role=role,
        system_prompt=f"Act as {role}.",
        tool_permissions=["read_file"],
    )


def test_workflow_pack_declares_agents_steps_tools_and_eval_checks() -> None:
    pack = WorkflowPack(
        name="demo",
        description="Demo workflow pack.",
        agents=[_agent("Clarifier"), _agent("Reviewer")],
        steps=[
            WorkflowStep(
                name="clarify",
                agent_role="Clarifier",
                required_inputs=["goal"],
                allowed_tools=["read_file"],
                phase="planning",
                produces_artifact_type="source_summary",
                coordination_role="controller",
            ),
            WorkflowStep(
                name="review",
                agent_role="Reviewer",
                required_artifacts=["source_summary"],
                allowed_tools=["read_file"],
                depends_on=["clarify"],
                phase="review",
                produces_artifact_type="final_report",
                coordination_role="subagent",
                controller_step="clarify",
                return_contract=ReturnContract(
                    required_artifact_types=["final_report"],
                    require_risk_notes=True,
                ),
                runtime="session",
                session_policy=SessionPolicy(
                    persistent=True,
                    resume_strategy="latest_artifact_and_trace",
                ),
            ),
        ],
        eval_checks=[
            EvalCheck(
                name="requirements_explicit",
                description="Requirements must be explicit.",
                severity="blocker",
                required_artifact_types=["source_summary"],
            )
        ],
        final_artifact_type="final_report",
    )

    dumped = pack.model_dump(mode="json", by_alias=True)

    assert dumped["name"] == "demo"
    assert dumped["steps"][0]["agent_role"] == "Clarifier"
    assert dumped["steps"][0]["phase"] == "planning"
    assert dumped["steps"][0]["produces_artifact_type"] == "source_summary"
    assert dumped["steps"][0]["coordination_role"] == "controller"
    assert dumped["steps"][1]["allowed_tools"] == ["read_file"]
    assert dumped["steps"][1]["depends_on"] == ["clarify"]
    assert dumped["steps"][1]["coordination_role"] == "subagent"
    assert dumped["steps"][1]["controller_step"] == "clarify"
    assert dumped["steps"][1]["return_contract"]["required_artifact_types"] == ["final_report"]
    assert dumped["steps"][1]["return_contract"]["require_risk_notes"] is True
    assert dumped["steps"][1]["runtime"] == "session"
    assert dumped["steps"][1]["session_policy"]["persistent"] is True
    assert dumped["steps"][1]["session_policy"]["resume_strategy"] == "latest_artifact_and_trace"
    assert dumped["eval_checks"][0]["severity"] == "blocker"


def test_workflow_step_context_policy_serializes_and_rejects_negative_budgets() -> None:
    step = WorkflowStep(
        name="review",
        agent_role="Reviewer",
        context_policy=ContextPolicy(
            artifact_excerpt_chars=16000,
            max_artifacts=6,
            max_upstream_handoffs=4,
        ),
    )

    assert step.model_dump(mode="json")["context_policy"] == {
        "artifact_excerpt_chars": 16000,
        "max_artifacts": 6,
        "max_upstream_handoffs": 4,
        "max_context_chars": 100000,
        "max_context_bytes": 300000,
    }
    with pytest.raises(ValidationError):
        ContextPolicy(artifact_excerpt_chars=-1)
    with pytest.raises(ValidationError):
        ContextPolicy(artifact_excerpt_chars=100001)


def test_workflow_pack_requires_context_capacity_for_every_declared_dependency() -> None:
    with pytest.raises(ValidationError, match="max_upstream_handoffs"):
        WorkflowPack(
            name="demo",
            description="Dependency handoffs must not be silently dropped.",
            agents=[_agent("Planner"), _agent("Reviewer")],
            steps=[
                WorkflowStep(name="first", agent_role="Planner", produces_artifact_type="research_note"),
                WorkflowStep(name="second", agent_role="Planner", produces_artifact_type="source_summary"),
                WorkflowStep(
                    name="review",
                    agent_role="Reviewer",
                    depends_on=["first", "second"],
                    context_policy=ContextPolicy(max_upstream_handoffs=1),
                    produces_artifact_type="final_report",
                ),
            ],
            final_artifact_type="final_report",
        )


def test_workflow_pack_rejects_empty_agents_or_steps() -> None:
    with pytest.raises(ValidationError):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[],
            steps=[WorkflowStep(name="clarify", agent_role="Clarifier")],
            final_artifact_type="final_report",
        )

    with pytest.raises(ValidationError):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[],
            final_artifact_type="final_report",
        )


def test_workflow_pack_rejects_duplicate_agent_roles() -> None:
    with pytest.raises(ValidationError, match="Duplicate agent roles"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier"), _agent("Clarifier")],
            steps=[WorkflowStep(name="clarify", agent_role="Clarifier")],
            final_artifact_type="final_report",
        )


def test_workflow_pack_rejects_agents_from_different_pack() -> None:
    with pytest.raises(ValidationError, match="different pack_name"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[
                AgentDefinition(
                    id="agent-clarifier",
                    pack_name="other",
                    role="Clarifier",
                    system_prompt="Act as Clarifier.",
                )
            ],
            steps=[WorkflowStep(name="clarify", agent_role="Clarifier")],
            final_artifact_type="final_report",
        )


def test_workflow_pack_rejects_duplicate_step_names() -> None:
    with pytest.raises(ValidationError, match="Duplicate workflow steps"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[
                WorkflowStep(name="clarify", agent_role="Clarifier"),
                WorkflowStep(name="clarify", agent_role="Clarifier"),
            ],
            final_artifact_type="final_report",
        )


def test_workflow_pack_rejects_steps_with_undefined_agent_roles() -> None:
    with pytest.raises(ValidationError, match="undefined agent roles"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[WorkflowStep(name="review", agent_role="Reviewer")],
            final_artifact_type="final_report",
        )


def test_eval_check_rejects_unknown_severity() -> None:
    with pytest.raises(ValidationError):
        EvalCheck(name="check", description="Check something.", severity="info")


def test_workflow_pack_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[WorkflowStep(name="clarify", agent_role="Clarifier")],
            final_artifact_type="final_report",
            runner_config={"max_steps": 10},
        )


def test_workflow_pack_rejects_invalid_dependencies_and_artifact_types() -> None:
    with pytest.raises(ValidationError, match="undefined dependencies"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[WorkflowStep(name="clarify", agent_role="Clarifier", depends_on=["missing"])],
            final_artifact_type="final_report",
        )

    with pytest.raises(ValidationError, match="depend on themselves"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[WorkflowStep(name="clarify", agent_role="Clarifier", depends_on=["clarify"])],
            final_artifact_type="final_report",
        )

    with pytest.raises(ValidationError, match="contain a cycle"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier"), _agent("Reviewer")],
            steps=[
                WorkflowStep(name="clarify", agent_role="Clarifier", depends_on=["review"]),
                WorkflowStep(name="review", agent_role="Reviewer", depends_on=["clarify"]),
            ],
            final_artifact_type="final_report",
        )

    with pytest.raises(ValidationError, match="unsupported produced artifact"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[WorkflowStep(name="clarify", agent_role="Clarifier", produces_artifact_type="unknown_artifact")],
            final_artifact_type="final_report",
        )

    with pytest.raises(ValidationError, match="unsupported artifact types"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[
                WorkflowStep(
                    name="clarify",
                    agent_role="Clarifier",
                    required_artifacts=["unknown_artifact"],
                    produces_artifact_type="final_report",
                )
            ],
            final_artifact_type="final_report",
        )

    with pytest.raises(ValidationError, match="unsupported artifact types"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[WorkflowStep(name="clarify", agent_role="Clarifier", produces_artifact_type="final_report")],
            eval_checks=[
                EvalCheck(
                    name="bad_eval_artifact",
                    description="Bad eval artifact.",
                    required_artifact_types=["unknown_artifact"],
                )
            ],
            final_artifact_type="final_report",
        )

    with pytest.raises(ValidationError, match="No step declares final_artifact_type"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[WorkflowStep(name="clarify", agent_role="Clarifier", produces_artifact_type="source_summary")],
            final_artifact_type="final_report",
        )


def test_workflow_pack_validates_subagent_controller_and_return_contract() -> None:
    with pytest.raises(ValidationError, match="must declare controller_step"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier"), _agent("Reviewer")],
            steps=[
                WorkflowStep(name="clarify", agent_role="Clarifier", produces_artifact_type="source_summary"),
                WorkflowStep(
                    name="review",
                    agent_role="Reviewer",
                    depends_on=["clarify"],
                    coordination_role="subagent",
                    produces_artifact_type="final_report",
                ),
            ],
            final_artifact_type="final_report",
        )

    with pytest.raises(ValidationError, match="undefined controller_step"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier"), _agent("Reviewer")],
            steps=[
                WorkflowStep(name="clarify", agent_role="Clarifier", produces_artifact_type="source_summary"),
                WorkflowStep(
                    name="review",
                    agent_role="Reviewer",
                    depends_on=["clarify"],
                    coordination_role="subagent",
                    controller_step="missing",
                    produces_artifact_type="final_report",
                ),
            ],
            final_artifact_type="final_report",
        )

    with pytest.raises(ValidationError, match="upstream dependency"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier"), _agent("Reviewer"), _agent("Writer")],
            steps=[
                WorkflowStep(name="clarify", agent_role="Clarifier", produces_artifact_type="source_summary"),
                WorkflowStep(name="write", agent_role="Writer", produces_artifact_type="patch"),
                WorkflowStep(
                    name="review",
                    agent_role="Reviewer",
                    depends_on=["clarify"],
                    coordination_role="subagent",
                    controller_step="write",
                    produces_artifact_type="final_report",
                ),
            ],
            final_artifact_type="final_report",
        )

    with pytest.raises(ValidationError, match="Return contracts require unsupported artifact types"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier"), _agent("Reviewer")],
            steps=[
                WorkflowStep(name="clarify", agent_role="Clarifier", produces_artifact_type="source_summary"),
                WorkflowStep(
                    name="review",
                    agent_role="Reviewer",
                    depends_on=["clarify"],
                    coordination_role="subagent",
                    controller_step="clarify",
                    produces_artifact_type="final_report",
                    return_contract=ReturnContract(required_artifact_types=["unknown_artifact"]),
                ),
            ],
            final_artifact_type="final_report",
        )


def test_workflow_pack_validates_runtime_and_session_policy() -> None:
    with pytest.raises(ValidationError, match="resume_strategy requires persistent=true"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[
                WorkflowStep(
                    name="clarify",
                    agent_role="Clarifier",
                    produces_artifact_type="final_report",
                    runtime="session",
                    session_policy=SessionPolicy(
                        persistent=False,
                        resume_strategy="latest_artifact_and_trace",
                    ),
                )
            ],
            final_artifact_type="final_report",
        )


def test_workflow_step_gate_fields_are_serialized_and_validate_artifact_types() -> None:
    pack = WorkflowPack(
        name="demo",
        description="Demo pack.",
        agents=[_agent("Planner"), _agent("Writer")],
        steps=[
            WorkflowStep(
                name="plan",
                agent_role="Planner",
                produces_artifact_type="research_note",
            ),
            WorkflowStep(
                name="write",
                agent_role="Writer",
                depends_on=["plan"],
                requires_eval_pass=True,
                required_eval_checks=["patched_local_test_command"],
                requires_artifact=["research_note"],
                ownership={"files": ["app/api.py"], "artifacts": ["final_report"]},
                produces_artifact_type="final_report",
            ),
        ],
        final_artifact_type="final_report",
    )

    dumped = pack.model_dump(mode="json")
    write_step = dumped["steps"][1]
    assert write_step["requires_eval_pass"] is True
    assert write_step["required_eval_checks"] == ["patched_local_test_command"]
    assert write_step["requires_artifact"] == ["research_note"]
    assert write_step["ownership"]["files"] == ["app/api.py"]

    with pytest.raises(ValidationError, match="unsupported artifact types"):
        WorkflowPack(
            name="demo",
            description="Demo pack.",
            agents=[_agent("Planner")],
            steps=[
                WorkflowStep(
                    name="plan",
                    agent_role="Planner",
                    requires_artifact=["unsupported"],
                    produces_artifact_type="final_report",
                )
            ],
            final_artifact_type="final_report",
        )


@pytest.mark.parametrize(
    ("required_eval_checks", "requires_eval_pass", "message"),
    [
        (["patched_local_test_command"], False, "requires requires_eval_pass=true"),
        (["patched_local_test_command", "patched_local_test_command"], True, "duplicate"),
        (["   "], True, "blank"),
        (["write:artifacts_created"], True, "structural artifact check"),
    ],
)
def test_workflow_pack_rejects_invalid_required_eval_checks(
    required_eval_checks: list[str],
    requires_eval_pass: bool,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        WorkflowPack(
            name="demo",
            description="Demo pack.",
            agents=[_agent("Writer")],
            steps=[
                WorkflowStep(
                    name="write",
                    agent_role="Writer",
                    requires_eval_pass=requires_eval_pass,
                    required_eval_checks=required_eval_checks,
                    produces_artifact_type="final_report",
                )
            ],
            final_artifact_type="final_report",
        )


def test_workflow_pack_rejects_acp_runtime_without_approval() -> None:
    with pytest.raises(ValidationError, match="ACP runtime steps must require approval"):
        WorkflowPack(
            name="demo",
            description="Demo workflow pack.",
            agents=[_agent("Clarifier")],
            steps=[
                WorkflowStep(
                    name="clarify",
                    agent_role="Clarifier",
                    produces_artifact_type="final_report",
                    runtime="acp",
                    session_policy=SessionPolicy(persistent=True),
                )
            ],
            final_artifact_type="final_report",
        )
