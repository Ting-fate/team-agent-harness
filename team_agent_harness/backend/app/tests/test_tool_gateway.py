import os

import pytest

from app.core.models import AgentDefinition, AgentRun, Run, Task, TraceEventType
from app.core.storage import SQLiteStorage
from app.core.tool_gateway import (
    ToolContext,
    ToolDefinition,
    ToolGateway,
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


def test_gateway_exposes_and_enforces_the_same_typed_schema(tool_env) -> None:
    _, _, gateway, context, _ = tool_env

    specs = gateway.model_tool_specs(frozenset({"read_file"}))

    assert specs == [
        {
            "name": "read_file",
            "description": "Read a UTF-8 text file within the workspace.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string", "maxLength": 4096}},
                "required": ["path"],
                "additionalProperties": False,
            },
        }
    ]
    with pytest.raises(ToolValidationError, match="Unexpected fields"):
        gateway.call_tool(context, "read_file", {"path": "README.md", "extra": True})
    with pytest.raises(ToolValidationError, match="Invalid field type"):
        gateway.call_tool(context, "read_file", {"path": 7})


def test_gateway_requires_explicit_side_effect_approval_in_agent_loop_mode(tool_env) -> None:
    _, _, gateway, context, _ = tool_env
    guarded_context = ToolContext(
        run_id=context.run_id,
        agent_run_id=context.agent_run_id,
        agent=context.agent,
        allowed_tools=frozenset({"run_test_command"}),
        enforce_side_effect_approval=True,
    )

    with pytest.raises(ToolPermissionError, match="Explicit side effect approval"):
        gateway.call_tool(guarded_context, "run_test_command", {"command": "pytest -q"})

    approved_context = ToolContext(
        **{
            **guarded_context.__dict__,
            "approved_side_effect_tools": frozenset({"run_test_command"}),
        }
    )
    assert gateway.call_tool(approved_context, "run_test_command", {"command": "pytest -q"})[
        "status"
    ] == "mocked"


def test_gateway_binds_real_web_calls_to_run_tool_snapshot_and_keeps_legacy_behavior(tool_env) -> None:
    _, logger, _, context, _ = tool_env
    gateway = ToolGateway(logger)
    tool_names = ["web_search", "browser_search", "browser_fetch"]
    active_providers = {
        "web_search": "tavily",
        "browser_search": "edge",
        "browser_fetch": "edge",
    }
    for tool_name in tool_names:
        gateway.register_tool(
            ToolDefinition(
                name=tool_name,
                description=f"Test {tool_name}.",
                required_fields=frozenset(),
                handler=lambda _payload, name=tool_name: {"tool": name},
                real_web_call_enabled=lambda: True,
                real_web_provider=lambda name=tool_name: active_providers[name],
            )
        )
    agent = AgentDefinition(
        id="real-web-agent",
        pack_name="research",
        role="Searcher",
        system_prompt="Use only confirmed web tools.",
        tool_permissions=tool_names,
    )
    snapshotted = ToolContext(
        run_id=context.run_id,
        agent_run_id=context.agent_run_id,
        agent=agent,
        allowed_tools=frozenset(tool_names),
        real_web_access_confirmed=True,
        confirmed_real_web_tools=frozenset({"web_search"}),
        confirmed_real_web_tool_routes=frozenset({("web_search", "tavily")}),
    )

    assert gateway.call_tool(snapshotted, "web_search", {}) == {"tool": "web_search"}
    with pytest.raises(ToolPermissionError, match="run tool snapshot"):
        gateway.call_tool(snapshotted, "browser_search", {})
    with pytest.raises(ToolPermissionError, match="run tool snapshot"):
        gateway.call_tool(snapshotted, "browser_fetch", {})

    active_providers["web_search"] = "other-provider"
    with pytest.raises(ToolPermissionError, match="run tool snapshot"):
        gateway.call_tool(snapshotted, "web_search", {})

    partial = ToolContext(
        **{
            **snapshotted.__dict__,
            "confirmed_real_web_tool_routes": None,
        }
    )
    with pytest.raises(ToolPermissionError, match="incomplete"):
        gateway.call_tool(partial, "web_search", {})

    legacy = ToolContext(
        **{
            **snapshotted.__dict__,
            "confirmed_real_web_tools": None,
            "confirmed_real_web_tool_routes": None,
        }
    )
    assert gateway.call_tool(legacy, "browser_search", {}) == {"tool": "browser_search"}


def test_restarted_run_preserves_web_route_snapshot_and_rejects_provider_drift(tmp_path) -> None:
    database_path = tmp_path / "harness.sqlite3"
    active_provider = {"name": "tavily"}
    agent = AgentDefinition(
        id="restart-web-agent",
        pack_name="research",
        role="Searcher",
        system_prompt="Use only the persisted web route.",
        tool_permissions=["web_search"],
    )
    with SQLiteStorage(database_path) as storage:
        storage.init_schema()
        task = storage.create_task(
            Task(id="restart-web-task", title="Task", goal="Goal", workflow_pack="research")
        )
        run = storage.create_run(
            Run(
                id="restart-web-run",
                task_id=task.id,
                real_web_access_confirmed=True,
                confirmed_real_web_tools=["web_search"],
                confirmed_real_web_tool_routes=[
                    {"name": "web_search", "provider": "tavily"},
                ],
            )
        )
        storage.create_agent_definition(agent)
        storage.create_agent_run(
            AgentRun(
                id="restart-web-agent-run",
                run_id=run.id,
                agent_id=agent.id,
                step_name="search",
            )
        )

    with SQLiteStorage(database_path) as storage:
        storage.init_schema()
        restored = storage.get_run("restart-web-run")
        assert restored is not None
        assert restored.confirmed_real_web_tools == ["web_search"]
        assert [
            route.model_dump(mode="json")
            for route in restored.confirmed_real_web_tool_routes or []
        ] == [{"name": "web_search", "provider": "tavily"}]

        gateway = ToolGateway(TraceLogger(storage))
        gateway.register_tool(
            ToolDefinition(
                name="web_search",
                description="Test restarted web authorization.",
                required_fields=frozenset(),
                handler=lambda _payload: {"status": "called"},
                real_web_call_enabled=lambda: True,
                real_web_provider=lambda: active_provider["name"],
            )
        )
        context = ToolContext(
            run_id=restored.id,
            agent_run_id="restart-web-agent-run",
            agent=agent,
            allowed_tools=frozenset({"web_search"}),
            real_web_access_confirmed=restored.real_web_access_confirmed,
            confirmed_real_web_tools=frozenset(restored.confirmed_real_web_tools or []),
            confirmed_real_web_tool_routes=frozenset(
                (route.name, route.provider)
                for route in restored.confirmed_real_web_tool_routes or []
            ),
        )

        assert gateway.call_tool(context, "web_search", {}) == {"status": "called"}
        active_provider["name"] = "other-provider"
        with pytest.raises(ToolPermissionError, match="run tool snapshot"):
            gateway.call_tool(context, "web_search", {})


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
    assert searched == {"matches": [], "truncated": False}


def test_file_tools_reject_hard_link_to_file_outside_workspace(tool_env) -> None:
    _, logger, gateway, context, workspace = tool_env
    outside = workspace.parent / "outside-source.txt"
    outside.write_text("outside hard-link marker", encoding="utf-8")
    link = workspace / "innocent.txt"
    try:
        os.link(outside, link)
    except OSError:
        pytest.skip("Hard-link creation is not available in this environment")

    assert link.stat().st_nlink > 1
    with pytest.raises(ToolPermissionError, match="Hard-linked"):
        gateway.call_tool(context, "read_file", {"path": "innocent.txt"})

    listed = gateway.call_tool(context, "list_files", {})
    searched = gateway.call_tool(context, "search_files", {"query": "outside hard-link marker"})
    assert "innocent.txt" not in listed["files"]
    assert searched == {"matches": [], "truncated": False}
    errors = [
        event for event in logger.list_for_run(context.run_id) if event.event_type == TraceEventType.ERROR
    ]
    assert errors[-1].payload["error_type"] == "ToolPermissionError"


def test_list_and_search_file_tools(tool_env) -> None:
    _, _, gateway, context, _ = tool_env

    listed = gateway.call_tool(context, "list_files", {})
    searched = gateway.call_tool(context, "search_files", {"query": "research"})

    assert listed == {"files": ["README.md", "notes.txt"], "truncated": False}
    assert searched == {"matches": [{"path": "notes.txt"}], "truncated": False}


@pytest.mark.parametrize(
    "relative_path",
    [
        ".env",
        ".env.local",
        "credentials.json",
        "private.pem",
        "secrets/provider.json",
        ".secrets/provider.json",
        "credentials/provider.json",
        ".token",
        "token.txt",
        "passwords.csv",
        "api_key.json",
        ".htpasswd",
        ".ssh/id_ed25519",
        ".git/config",
        ".venv/site.py",
    ],
)
def test_file_tools_hide_sensitive_workspace_paths(tool_env, relative_path: str) -> None:
    _, _, gateway, context, workspace = tool_env
    sensitive = workspace / relative_path
    sensitive.parent.mkdir(parents=True, exist_ok=True)
    sensitive.write_text("sensitive-marker", encoding="utf-8")

    with pytest.raises(ToolPermissionError, match="Sensitive workspace paths"):
        gateway.call_tool(context, "read_file", {"path": relative_path})

    listed = gateway.call_tool(context, "list_files", {})
    searched = gateway.call_tool(context, "search_files", {"query": "sensitive-marker"})
    assert relative_path not in listed["files"]
    assert searched["matches"] == []


def test_sensitive_name_filter_does_not_hide_source_names_with_embedded_markers(tool_env) -> None:
    _, _, gateway, context, workspace = tool_env
    expected = ["password_policy.py", "secret_scanner.py", "tokenizer.py"]
    for filename in expected:
        (workspace / filename).write_text("bounded-filter-marker", encoding="utf-8")

    listed = gateway.call_tool(context, "list_files", {})
    searched = gateway.call_tool(context, "search_files", {"query": "bounded-filter-marker"})

    assert all(filename in listed["files"] for filename in expected)
    assert searched["matches"] == [{"path": filename} for filename in expected]
    assert gateway.call_tool(context, "read_file", {"path": "tokenizer.py"}) == {
        "content": "bounded-filter-marker"
    }


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
    assert "path" not in result
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
