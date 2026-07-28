from __future__ import annotations

from app.core.models import AgentDefinition
from app.packs.base import WorkflowPack


class AgentRegistryError(RuntimeError):
    pass


class AgentRegistry:
    def __init__(self) -> None:
        self._agents_by_id: dict[str, AgentDefinition] = {}
        self._agent_ids_by_pack_role: dict[tuple[str, str], str] = {}

    def register_agent(self, agent: AgentDefinition) -> AgentDefinition:
        self._ensure_can_register(agent)
        key = (agent.pack_name, agent.role)
        self._agents_by_id[agent.id] = agent
        self._agent_ids_by_pack_role[key] = agent.id
        return agent

    def register_pack(self, pack: WorkflowPack) -> list[AgentDefinition]:
        for agent in pack.agents:
            self._ensure_can_register(agent)

        for agent in pack.agents:
            self._agents_by_id[agent.id] = agent
            self._agent_ids_by_pack_role[(agent.pack_name, agent.role)] = agent.id

        return pack.agents

    def get_agent(self, agent_id: str) -> AgentDefinition | None:
        return self._agents_by_id.get(agent_id)

    def get_agent_for_role(self, pack_name: str, role: str) -> AgentDefinition | None:
        agent_id = self._agent_ids_by_pack_role.get((pack_name, role))
        if agent_id is None:
            return None
        return self._agents_by_id[agent_id]

    def list_agents(self, pack_name: str | None = None) -> list[AgentDefinition]:
        agents = list(self._agents_by_id.values())
        if pack_name is None:
            return agents
        return [agent for agent in agents if agent.pack_name == pack_name]

    def _ensure_can_register(self, agent: AgentDefinition) -> None:
        key = (agent.pack_name, agent.role)
        existing_id = self._agent_ids_by_pack_role.get(key)
        if existing_id is not None and existing_id != agent.id:
            raise AgentRegistryError(f"Agent role already registered for pack {agent.pack_name}: {agent.role}")
        if agent.id in self._agents_by_id:
            raise AgentRegistryError(f"Agent id already registered: {agent.id}")
