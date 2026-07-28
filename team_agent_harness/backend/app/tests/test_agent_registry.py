import pytest

from app.core.models import AgentDefinition
from app.core.registry import AgentRegistry, AgentRegistryError
from app.packs.base import WorkflowPack, WorkflowStep


def _agent(agent_id: str, pack_name: str, role: str, tools: list[str] | None = None) -> AgentDefinition:
    return AgentDefinition(
        id=agent_id,
        pack_name=pack_name,
        role=role,
        system_prompt=f"Act as {role}.",
        tool_permissions=tools or [],
    )


def test_register_and_query_agent_by_id_and_role() -> None:
    registry = AgentRegistry()
    agent = _agent("agent-1", "code_rd", "Reviewer", ["read_file"])

    registry.register_agent(agent)

    assert registry.get_agent("agent-1") == agent
    assert registry.get_agent_for_role("code_rd", "Reviewer") == agent
    assert registry.get_agent_for_role("code_rd", "Missing") is None


def test_register_pack_registers_default_agents() -> None:
    registry = AgentRegistry()
    pack = WorkflowPack(
        name="code_rd",
        description="Code workflow.",
        agents=[
            _agent("agent-clarifier", "code_rd", "Clarifier"),
            _agent("agent-reviewer", "code_rd", "Reviewer", ["read_file"]),
        ],
        steps=[
            WorkflowStep(name="clarify", agent_role="Clarifier"),
            WorkflowStep(
                name="review",
                agent_role="Reviewer",
                allowed_tools=["read_file"],
                produces_artifact_type="final_report",
            ),
        ],
        final_artifact_type="final_report",
    )

    registered = registry.register_pack(pack)

    assert registered == pack.agents
    assert registry.list_agents("code_rd") == pack.agents


def test_duplicate_agent_id_is_rejected() -> None:
    registry = AgentRegistry()
    registry.register_agent(_agent("agent-1", "code_rd", "Reviewer"))

    with pytest.raises(AgentRegistryError, match="id already registered"):
        registry.register_agent(_agent("agent-1", "research", "Reviewer"))


def test_duplicate_pack_role_is_rejected() -> None:
    registry = AgentRegistry()
    registry.register_agent(_agent("agent-1", "code_rd", "Reviewer"))

    with pytest.raises(AgentRegistryError, match="role already registered"):
        registry.register_agent(_agent("agent-2", "code_rd", "Reviewer"))


def test_same_role_can_exist_in_different_packs() -> None:
    registry = AgentRegistry()
    code_reviewer = registry.register_agent(_agent("agent-code-reviewer", "code_rd", "Reviewer"))
    research_reviewer = registry.register_agent(_agent("agent-research-reviewer", "research", "Reviewer"))

    assert registry.get_agent_for_role("code_rd", "Reviewer") == code_reviewer
    assert registry.get_agent_for_role("research", "Reviewer") == research_reviewer


def test_register_pack_is_atomic_when_later_agent_conflicts() -> None:
    registry = AgentRegistry()
    registry.register_agent(_agent("existing-reviewer", "code_rd", "Reviewer"))
    pack = WorkflowPack(
        name="code_rd",
        description="Code workflow.",
        agents=[
            _agent("new-clarifier", "code_rd", "Clarifier"),
            _agent("new-reviewer", "code_rd", "Reviewer"),
        ],
        steps=[
            WorkflowStep(name="clarify", agent_role="Clarifier"),
            WorkflowStep(name="review", agent_role="Reviewer", produces_artifact_type="final_report"),
        ],
        final_artifact_type="final_report",
    )

    with pytest.raises(AgentRegistryError):
        registry.register_pack(pack)

    assert registry.get_agent("new-clarifier") is None
    assert registry.get_agent_for_role("code_rd", "Clarifier") is None
