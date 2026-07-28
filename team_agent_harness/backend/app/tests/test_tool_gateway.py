import pytest

from app.core.models import AgentDefinition, AgentRun, Run, Task, TraceEventType
from app.core.storage import SQLiteStorage
from app.core.tool_gateway import (
    ToolContext,
    ToolDefinition,
    ToolGatewayError,
    ToolPermissionError,
    ToolValidationError,
    create_mock_gateway,
)
from app.core.trace import TraceLogger


@pytest.fixture
def tool_env(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("hello harness\n", encoding="utf-8")
    (workspace / "notes.txt").write_text("research harness\n", encoding="utf-8")
    with SQLiteStorage(tmp_path / "harness.sqlite3") as db:
        db.init_schema()
        task = db.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
        run = db.create_run(Run(id="run-1", task_id=task.id))
        agent = db.create_agent_definition(
            AgentDefinition(
                id="agent-1",
                pack_name="code_rd",
                role="Reviewer",
                system_prompt="Review work.",
                tool_permissions=[
                    "read_file",
                    "list_files",
                    "search_files",
                    "run_test_command",
                    "write_artifact",
                ],
            )
        )
        agent_run = db.create_agent_run(
            AgentRun(id="agent-run-1", run_id=run.id, agent_id=agent.id, step_name="review")
        )
        logger = TraceLogger(db)
        artifact_store = ArtifactStore(tmp_path / "artifacts", db, logger)
        gateway = create_mock_gateway(logger, workspace, artifact_store=artifact_store)
        context = ToolContext(
            run_id=run.id,
            agent_run_id=agent_run.id,
            agent=agent,
            allowed_tools=frozenset({"read_file", "list_files", "search_files", "write_artifact"}),
        )
        yield db, logger, gateway, context, workspace


def test_gateway_executes_allowed_tool_and_records_trace(tool_env) -> None:
    db, logger, gateway, context, _ = tool_env

    result = gateway.call_tool(context, "read_file", {"path": "README.md"})

    assert result == {"content": "hello harness\n"}
    events = logger.list_for_run(context.run_id)
    assert [event.event_type for event in events] == [TraceEventType.TOOL_CALL, TraceEventType.TOOL_RESULT]
    assert events[0].payload["tool"] == "read_file"
    assert events[1].payload["output"] == {"content_length": len(result["content"])}


def test_gateway_rejects_tool_not_allowed_by_step_and_records_error(tool_env) -> None:
    _, logger, gateway, context, _ = tool_env

    with pytest.raises(ToolPermissionError):
        gateway.call_tool(context, "run_test_command", {"command": "pytest"})

    events = logger.list_for_run(context.run_id)
    assert len(events) == 1
    assert events[0].event_type == TraceEventType.ERROR
    assert events[0].payload["error_type"] == "ToolPermissionError"


def test_gateway_rejects_tool_not_allowed_for_agent(tool_env) -> None:
    _, logger, gateway, context, _ = tool_env
    context = ToolContext(
        run_id=context.run_id,
        agent_run_id=context.agent_run_id,
        agent=context.agent,
        allowed_tools=frozenset({"web_search_mock"}),
    )

    with pytest.raises(ToolPermissionError):
        gateway.call_tool(context, "web_search_mock", {"query": "agent harness"})

    assert logger.list_for_run(context.run_id)[0].event_type == TraceEventType.ERROR


def test_gateway_rejects_missing_required_fields(tool_env) -> None:
    _, logger, gateway, context, _ = tool_env

    with pytest.raises(ToolValidationError):
        gateway.call_tool(context, "read_file", {})

    assert logger.list_for_run(context.run_id)[0].payload["error_type"] == "ToolValidationError"


def test_gateway_rejects_unknown_tool(tool_env) -> None:
    _, logger, gateway, context, _ = tool_env

    with pytest.raises(ToolGatewayError):
        gateway.call_tool(context, "unknown", {})

    assert logger.list_for_run(context.run_id)[0].payload["message"] == "Unknown tool: unknown"


def test_file_tools_stay_inside_workspace(tool_env) -> None:
    _, logger, gateway, context, workspace = tool_env
    outside = workspace.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(ToolPermissionError):
        gateway.call_tool(context, "read_file", {"path": "../secret.txt"})

    events = logger.list_for_run(context.run_id)
    assert [event.event_type for event in events] == [
        TraceEventType.TOOL_CALL,
        TraceEventType.ERROR,
    ]
    assert events[-1].payload["error_type"] == "ToolPermissionError"


def test_file_tools_reject_absolute_paths(tool_env) -> None:
    _, logger, gateway, context, workspace = tool_env

    with pytest.raises(ToolPermissionError):
        gateway.call_tool(context, "read_file", {"path": str((workspace / "README.md").resolve())})

    assert logger.list_for_run(context.run_id)[-1].payload["error_type"] == "ToolPermissionError"


def test_file_tools_reject_symlink_escape(tool_env) -> None:
    _, logger, gateway, context, workspace = tool_env
    outside = workspace.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "secret-link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is not available in this environment")

    with pytest.raises(ToolPermissionError):
        gateway.call_tool(context, "read_file", {"path": "secret-link.txt"})

    assert logger.list_for_run(context.run_id)[-1].payload["error_type"] == "ToolPermissionError"


def test_list_and_search_skip_symlink_escape(tool_env) -> None:
    _, _, gateway, context, workspace = tool_env
    outside = workspace.parent / "secret.txt"
    outside.write_text("outside secret", encoding="utf-8")
    link = workspace / "secret-link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlink creation is not available in this environment")

    listed = gateway.call_tool(context, "list_files", {})
    searched = gateway.call_tool(context, "search_files", {"query": "outside secret"})

    assert "secret-link.txt" not in listed["files"]
    assert searched == {"matches": []}


def test_list_and_search_file_tools(tool_env) -> None:
    _, _, gateway, context, _ = tool_env

    listed = gateway.call_tool(context, "list_files", {})
    searched = gateway.call_tool(context, "search_files", {"query": "research"})

    assert listed == {"files": ["README.md", "notes.txt"]}
    assert searched == {"matches": [{"path": "notes.txt"}]}


def test_write_artifact_tool_uses_artifact_store_and_redacts_content_trace(tool_env) -> None:
    db, logger, gateway, context, _ = tool_env

    result = gateway.call_tool(
        context,
        "write_artifact",
        {"filename": "result.md", "content": "# Result", "artifact_type": "final_report"},
    )

    artifacts = db.list_artifacts_for_run(context.run_id)
    assert len(artifacts) == 1
    assert result["artifact_id"] == artifacts[0].id
    events = logger.list_for_run(context.run_id)
    tool_call = [event for event in events if event.event_type == TraceEventType.TOOL_CALL][0]
    assert tool_call.payload["input"]["content_length"] == len("# Result")
    assert "content" not in tool_call.payload["input"]


def test_run_test_command_mock_uses_allowlist(tool_env) -> None:
    _, logger, gateway, context, _ = tool_env
    context = ToolContext(
        run_id=context.run_id,
        agent_run_id=context.agent_run_id,
        agent=context.agent,
        allowed_tools=frozenset({"run_test_command"}),
    )

    assert gateway.call_tool(context, "run_test_command", {"command": "pytest -q"}) == {
        "status": "mocked",
        "command": "pytest -q",
    }

    with pytest.raises(ToolPermissionError):
        gateway.call_tool(context, "run_test_command", {"command": "rm -rf ."})

    assert logger.list_for_run(context.run_id)[-1].payload["error_type"] == "ToolPermissionError"


def test_gateway_records_unexpected_handler_errors(tool_env) -> None:
    _, logger, gateway, context, _ = tool_env
    gateway.register_tool(
        ToolDefinition(
            name="broken_tool",
            description="Raises a non gateway exception.",
            required_fields=frozenset(),
            handler=lambda payload: (_ for _ in ()).throw(ValueError("broken")),
        )
    )
    context = ToolContext(
        run_id=context.run_id,
        agent_run_id=context.agent_run_id,
        agent=AgentDefinition(
            id="agent-broken",
            pack_name="code_rd",
            role="Broken",
            system_prompt="Break.",
            tool_permissions=["broken_tool"],
        ),
        allowed_tools=frozenset({"broken_tool"}),
    )

    with pytest.raises(ToolValidationError, match="Tool handler failed"):
        gateway.call_tool(context, "broken_tool", {})

    assert logger.list_for_run(context.run_id)[-1].payload["error_type"] == "ValueError"
from app.core.artifacts import ArtifactStore
