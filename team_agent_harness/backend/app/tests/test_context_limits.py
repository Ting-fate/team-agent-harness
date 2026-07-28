import pytest
from fastapi.testclient import TestClient

from app.core.context_injection import ContextBudgetExceeded, ContextInjector
from app.core.models import AgentDefinition, AgentRun, Run, Task
from app.main import create_app
from app.packs.base import ContextPolicy, WorkflowStep


def test_task_api_rejects_oversized_and_deeply_nested_payloads(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(app) as client:
        oversized = client.post(
            "/tasks",
            json={
                "title": "Oversized task",
                "goal": "bounded",
                "workflow_pack": "code_rd",
                "inputs": {"brief": "x" * 100_001},
            },
        )
        nested_value: object = "value"
        for _ in range(10):
            nested_value = {"next": nested_value}
        nested = client.post(
            "/tasks",
            json={
                "title": "Deep task",
                "goal": "bounded",
                "workflow_pack": "code_rd",
                "inputs": {"root": nested_value},
            },
        )

    assert oversized.status_code == 400
    assert "exceeds 100000 characters" in oversized.json()["detail"]
    assert nested.status_code == 400
    assert "nesting exceeds 8 levels" in nested.json()["detail"]


def test_context_injector_fails_closed_before_oversized_context_reaches_model() -> None:
    task = Task(id="task-1", title="Large task", goal="x" * 20_000, workflow_pack="demo")
    run = Run(id="run-1", task_id=task.id)
    agent = AgentDefinition(id="agent-1", pack_name="demo", role="Reader", system_prompt="Read.")
    agent_run = AgentRun(id="agent-run-1", run_id=run.id, agent_id=agent.id, step_name="read")
    step = WorkflowStep(
        name="read",
        agent_role=agent.role,
        context_policy=ContextPolicy(max_context_chars=10_000, max_context_bytes=30_000),
    )

    with pytest.raises(ContextBudgetExceeded, match="exceeds character budget"):
        ContextInjector().build(
            task=task,
            run=run,
            step=step,
            agent_run=agent_run,
            previous_agent_run=None,
            previous_handoff=None,
            upstream_handoffs=[],
            agent_session=None,
            runtime_job=None,
            artifacts=[],
            total_artifacts=[],
            artifact_texts={},
            truncated_artifact_text_ids=set(),
        )
