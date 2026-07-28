from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.models import AgentDefinition, TraceEventType
from app.core.artifacts import ArtifactStore
from app.core.browser_tools import BrowserToolProvider, safe_browser_tool_input_summary, safe_browser_tool_output_summary
from app.core.trace import TraceLogger
from app.core.web_tools import WebToolProvider, redact_tool_message, safe_tool_input_summary, safe_tool_output_summary


class ToolGatewayError(RuntimeError):
    pass


class ToolPermissionError(ToolGatewayError):
    pass


class ToolValidationError(ToolGatewayError):
    pass


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    required_fields: frozenset[str]
    handler: ToolHandler
    real_web_call_enabled: Callable[[], bool] | None = None


@dataclass(frozen=True)
class ToolContext:
    run_id: str
    agent_run_id: str
    agent: AgentDefinition
    allowed_tools: frozenset[str]
    real_web_access_confirmed: bool = False


class ToolGateway:
    def __init__(self, trace_logger: TraceLogger) -> None:
        self.trace_logger = trace_logger
        self._tools: dict[str, ToolDefinition] = {}

    def register_tool(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ToolGatewayError(f"Tool already registered: {definition.name}")
        self._tools[definition.name] = definition

    def list_tools(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def call_tool(self, context: ToolContext, tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            definition = self._get_tool(tool_name)
            self._check_permission(context, definition)
            self._validate_payload(definition, payload)
        except ToolGatewayError as exc:
            self.trace_logger.record(
                run_id=context.run_id,
                agent_run_id=context.agent_run_id,
                event_type=TraceEventType.ERROR,
                payload={
                    "tool": tool_name,
                    "error_type": exc.__class__.__name__,
                    "message": redact_tool_message(str(exc)),
                },
            )
            raise

        self.trace_logger.record(
            run_id=context.run_id,
            agent_run_id=context.agent_run_id,
            event_type=TraceEventType.TOOL_CALL,
            payload={"tool": tool_name, "input": _summarize_tool_input(tool_name, payload)},
        )
        handler_payload = _with_context_payload(tool_name, payload, context)
        try:
            result = definition.handler(handler_payload)
        except ToolGatewayError as exc:
            self.trace_logger.record(
                run_id=context.run_id,
                agent_run_id=context.agent_run_id,
                event_type=TraceEventType.ERROR,
                payload={
                    "tool": tool_name,
                    "error_type": exc.__class__.__name__,
                    "message": redact_tool_message(str(exc)),
                },
            )
            raise
        except Exception as exc:
            self.trace_logger.record(
                run_id=context.run_id,
                agent_run_id=context.agent_run_id,
                event_type=TraceEventType.ERROR,
                payload={
                    "tool": tool_name,
                    "error_type": exc.__class__.__name__,
                    "message": redact_tool_message(str(exc)),
                },
            )
            raise ToolValidationError(f"Tool handler failed: {tool_name}") from exc
        self.trace_logger.record(
            run_id=context.run_id,
            agent_run_id=context.agent_run_id,
            event_type=TraceEventType.TOOL_RESULT,
            payload={"tool": tool_name, "output": _summarize_tool_output(tool_name, result)},
        )
        return result

    def _get_tool(self, tool_name: str) -> ToolDefinition:
        try:
            return self._tools[tool_name]
        except KeyError as exc:
            raise ToolValidationError(f"Unknown tool: {tool_name}") from exc

    def _check_permission(self, context: ToolContext, definition: ToolDefinition) -> None:
        tool_name = definition.name
        if tool_name not in context.allowed_tools:
            raise ToolPermissionError(f"Tool not allowed by workflow step: {tool_name}")
        if tool_name not in context.agent.tool_permissions:
            raise ToolPermissionError(f"Tool not allowed for agent {context.agent.role}: {tool_name}")
        if (
            definition.real_web_call_enabled is not None
            and definition.real_web_call_enabled()
            and not context.real_web_access_confirmed
        ):
            raise ToolPermissionError(
                f"Persisted real web access confirmation is required for tool: {tool_name}"
            )

    def _validate_payload(self, definition: ToolDefinition, payload: dict[str, Any]) -> None:
        missing = sorted(definition.required_fields - set(payload))
        if missing:
            raise ToolValidationError(f"Missing required fields for {definition.name}: {', '.join(missing)}")


def create_mock_gateway(
    trace_logger: TraceLogger,
    workspace_root: str | Path,
    artifact_store: ArtifactStore | None = None,
    web_tool_provider: WebToolProvider | None = None,
    browser_tool_provider: BrowserToolProvider | None = None,
) -> ToolGateway:
    workspace_root = Path(workspace_root).resolve()
    web_tool_provider = web_tool_provider or WebToolProvider()
    browser_tool_provider = browser_tool_provider or BrowserToolProvider()
    gateway = ToolGateway(trace_logger)
    gateway.register_tool(
        ToolDefinition(
            name="read_file",
            description="Read a UTF-8 text file within the workspace.",
            required_fields=frozenset({"path"}),
            handler=lambda payload: {"content": _read_workspace_file(workspace_root, payload["path"])},
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="list_files",
            description="List files under the workspace root.",
            required_fields=frozenset(),
            handler=lambda payload: {"files": _list_workspace_files(workspace_root)},
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="search_files",
            description="Search workspace text files by substring.",
            required_fields=frozenset({"query"}),
            handler=lambda payload: {"matches": _search_workspace_files(workspace_root, payload["query"])},
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="run_test_command",
            description="Mock test command execution.",
            required_fields=frozenset({"command"}),
            handler=_run_test_command_mock,
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="web_search_mock",
            description="Mock web search.",
            required_fields=frozenset({"query"}),
            handler=lambda payload: {"results": [{"title": "Mock result", "query": payload["query"]}]},
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="web_search",
            description="Search the public web through the configured provider.",
            required_fields=frozenset({"query"}),
            handler=web_tool_provider.search,
            real_web_call_enabled=lambda: (
                web_tool_provider.provider_name != "mock" and web_tool_provider.real_calls_enabled
            ),
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="fetch_page_mock",
            description="Mock page fetch.",
            required_fields=frozenset({"url"}),
            handler=lambda payload: {"url": payload["url"], "content": "mock page content"},
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="fetch_page",
            description="Fetch a public http(s) page through the configured provider.",
            required_fields=frozenset({"url"}),
            handler=web_tool_provider.fetch_page,
            real_web_call_enabled=lambda: (
                web_tool_provider.provider_name != "mock" and web_tool_provider.real_calls_enabled
            ),
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="browser_search",
            description="Search the public web through the configured local browser bridge.",
            required_fields=frozenset({"query"}),
            handler=browser_tool_provider.search,
            real_web_call_enabled=lambda: (
                browser_tool_provider.provider_name != "mock"
                and browser_tool_provider.real_calls_enabled
            ),
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="browser_fetch",
            description="Fetch a public http(s) page through the configured local browser bridge.",
            required_fields=frozenset({"url"}),
            handler=browser_tool_provider.fetch_page,
            real_web_call_enabled=lambda: (
                browser_tool_provider.provider_name != "mock"
                and browser_tool_provider.real_calls_enabled
            ),
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="write_artifact",
            description="Write an artifact through ArtifactStore.",
            required_fields=frozenset({"filename", "content"}),
            handler=lambda payload: _write_artifact(artifact_store, payload),
        )
    )
    return gateway


def _workspace_path(root: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise ToolPermissionError("Path must be relative to workspace root")
    path = (root / candidate).resolve()
    if not path.is_relative_to(root):
        raise ToolPermissionError("Path must stay within workspace root")
    return path


def _read_workspace_file(root: Path, raw_path: str) -> str:
    path = _workspace_path(root, raw_path)
    if not path.is_file():
        raise ToolValidationError(f"Not a file: {raw_path}")
    return path.read_text(encoding="utf-8")


def _list_workspace_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if _is_workspace_file(root, path)
    )


def _search_workspace_files(root: Path, query: str) -> list[dict[str, str]]:
    matches: list[dict[str, str]] = []
    for path in root.rglob("*"):
        if not _is_workspace_file(root, path):
            continue
        relative = path.relative_to(root).as_posix()
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if query in content:
            matches.append({"path": relative})
    return matches


def _is_workspace_file(root: Path, path: Path) -> bool:
    resolved = path.resolve()
    return resolved.is_relative_to(root) and resolved.is_file()


def _write_artifact(artifact_store: ArtifactStore | None, payload: dict[str, Any]) -> dict[str, Any]:
    if artifact_store is None:
        raise ToolValidationError("write_artifact requires an ArtifactStore")
    artifact = artifact_store.write_text(
        run_id=payload["run_id"],
        agent_run_id=payload["agent_run_id"],
        artifact_type=payload.get("artifact_type", "final_report"),
        filename=payload["filename"],
        content=payload["content"],
        source_refs=payload.get("source_refs", []),
    )
    return {"artifact_id": artifact.id, "path": artifact.path, "content_hash": artifact.content_hash}


def _run_test_command_mock(payload: dict[str, Any]) -> dict[str, Any]:
    command = payload["command"]
    allowed = {"pytest", "pytest -q", "python -m pytest", "python -m pytest -q"}
    if command not in allowed:
        raise ToolPermissionError(f"Test command is not in allowlist: {command}")
    return {"status": "mocked", "command": command}


def _with_context_payload(tool_name: str, payload: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    if tool_name != "write_artifact":
        return payload
    return {**payload, "run_id": context.run_id, "agent_run_id": context.agent_run_id}


def _summarize_tool_input(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    if tool_name in {"browser_search", "browser_fetch"}:
        return safe_browser_tool_input_summary(tool_name, payload)
    return safe_tool_input_summary(tool_name, payload)


def _summarize_tool_output(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    if tool_name in {"browser_search", "browser_fetch"}:
        return safe_browser_tool_output_summary(tool_name, result)
    return safe_tool_output_summary(tool_name, result)
