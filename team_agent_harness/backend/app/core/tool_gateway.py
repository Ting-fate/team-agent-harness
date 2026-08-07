from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from typing import Any, Literal

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


_MAX_WORKSPACE_FILE_BYTES = 1_000_000
_MAX_WORKSPACE_LIST_RESULTS = 2_000
_MAX_WORKSPACE_SEARCH_RESULTS = 200
_IGNORED_WORKSPACE_DIRECTORIES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}
_SENSITIVE_WORKSPACE_DIRECTORIES = {
    ".aws",
    ".azure",
    ".credentials",
    ".gnupg",
    ".secrets",
    ".ssh",
    "credentials",
    "secrets",
}
_SENSITIVE_WORKSPACE_FILENAMES = {
    ".git-credentials",
    ".htpasswd",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "service-account.json",
    "service_account.json",
}
_SENSITIVE_WORKSPACE_STEMS = {
    "api_key",
    "api_keys",
    "apikey",
    "apikeys",
    "credential",
    "credentials",
    "passwd",
    "password",
    "passwords",
    "private_key",
    "private_keys",
    "secret",
    "secrets",
    "token",
    "tokens",
}
_SENSITIVE_WORKSPACE_SUFFIXES = {
    ".jks",
    ".kdbx",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
    ".ppk",
}


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    required_fields: frozenset[str]
    handler: ToolHandler
    input_schema: dict[str, Any] | None = None
    side_effect: Literal["none", "local_write", "local_execute", "external_write"] = "none"
    real_web_call_enabled: Callable[[], bool] | None = None


@dataclass(frozen=True)
class ToolContext:
    run_id: str
    agent_run_id: str
    agent: AgentDefinition
    allowed_tools: frozenset[str]
    real_web_access_confirmed: bool = False
    enforce_side_effect_approval: bool = False
    approved_side_effect_tools: frozenset[str] = frozenset()


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

    def model_tool_specs(self, allowed_tools: frozenset[str]) -> list[dict[str, Any]]:
        specs = []
        for tool_name in sorted(allowed_tools):
            definition = self._get_tool(tool_name)
            specs.append(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "input_schema": _tool_input_schema(definition),
                }
            )
        return specs

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
            context.enforce_side_effect_approval
            and definition.side_effect != "none"
            and tool_name not in context.approved_side_effect_tools
        ):
            raise ToolPermissionError(f"Explicit side effect approval is required for tool: {tool_name}")
        if (
            definition.real_web_call_enabled is not None
            and definition.real_web_call_enabled()
            and not context.real_web_access_confirmed
        ):
            raise ToolPermissionError(
                f"Persisted real web access confirmation is required for tool: {tool_name}"
            )

    def _validate_payload(self, definition: ToolDefinition, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ToolValidationError(f"Tool payload must be an object: {definition.name}")
        missing = sorted(definition.required_fields - set(payload))
        if missing:
            raise ToolValidationError(f"Missing required fields for {definition.name}: {', '.join(missing)}")
        _validate_input_schema(definition.name, payload, _tool_input_schema(definition))


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
            input_schema=_object_schema({"path": _string_schema(max_length=4096)}, required={"path"}),
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="list_files",
            description="List source files under the workspace root, excluding sensitive and generated paths.",
            required_fields=frozenset(),
            handler=lambda payload: _list_workspace_files(workspace_root),
            input_schema=_object_schema({}),
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="search_files",
            description="Search bounded workspace source files by substring, excluding sensitive paths.",
            required_fields=frozenset({"query"}),
            handler=lambda payload: _search_workspace_files(workspace_root, payload["query"]),
            input_schema=_object_schema({"query": _string_schema(max_length=10_000)}, required={"query"}),
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="run_test_command",
            description="Mock test command execution.",
            required_fields=frozenset({"command"}),
            handler=_run_test_command_mock,
            input_schema=_object_schema({"command": _string_schema(max_length=200)}, required={"command"}),
            side_effect="local_execute",
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="web_search_mock",
            description="Mock web search.",
            required_fields=frozenset({"query"}),
            handler=lambda payload: {"results": [{"title": "Mock result", "query": payload["query"]}]},
            input_schema=_object_schema({"query": _string_schema(max_length=10_000)}, required={"query"}),
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="web_search",
            description="Search the public web through the configured provider.",
            required_fields=frozenset({"query"}),
            handler=web_tool_provider.search,
            input_schema=_object_schema(
                {
                    "query": _string_schema(max_length=10_000),
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                required={"query"},
            ),
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
            input_schema=_object_schema({"url": _string_schema(max_length=4096)}, required={"url"}),
        )
    )
    gateway.register_tool(
        ToolDefinition(
            name="fetch_page",
            description="Fetch a public http(s) page through the configured provider.",
            required_fields=frozenset({"url"}),
            handler=web_tool_provider.fetch_page,
            input_schema=_fetch_schema(),
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
            input_schema=_object_schema(
                {
                    "query": _string_schema(max_length=10_000),
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                required={"query"},
            ),
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
            input_schema=_fetch_schema(),
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
            input_schema=_object_schema(
                {
                    "filename": _string_schema(max_length=255),
                    "content": _string_schema(max_length=1_000_000),
                    "artifact_type": _string_schema(max_length=100),
                    "source_refs": {
                        "type": "array",
                        "items": _string_schema(max_length=4096),
                        "maxItems": 64,
                    },
                },
                required={"filename", "content"},
            ),
            side_effect="local_write",
        )
    )
    return gateway


def _workspace_path(root: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        raise ToolPermissionError("Path must be relative to workspace root")
    path = root / candidate
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ToolPermissionError("Path must stay within workspace root")
    if _is_sensitive_workspace_path(root, path) or _is_sensitive_workspace_path(root, resolved):
        raise ToolPermissionError("Sensitive workspace paths are not available to agents")
    return path


def _read_workspace_file(root: Path, raw_path: str) -> str:
    path = _workspace_path(root, raw_path)
    content = _read_verified_workspace_bytes(root, path, raw_path)
    try:
        return content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise ToolValidationError(f"File is not UTF-8 text: {raw_path}") from exc


def _list_workspace_files(root: Path) -> dict[str, Any]:
    files: list[str] = []
    truncated = False
    for path in _iter_workspace_files(root):
        if len(files) >= _MAX_WORKSPACE_LIST_RESULTS:
            truncated = True
            break
        files.append(path.relative_to(root).as_posix())
    return {"files": files, "truncated": truncated}


def _search_workspace_files(root: Path, query: str) -> dict[str, Any]:
    matches: list[dict[str, str]] = []
    truncated = False
    for path in _iter_workspace_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            content = (
                _read_verified_workspace_bytes(root, path, relative)
                .decode("utf-8")
                .replace("\r\n", "\n")
                .replace("\r", "\n")
            )
        except (ToolGatewayError, UnicodeDecodeError, OSError):
            continue
        if query in content:
            if len(matches) >= _MAX_WORKSPACE_SEARCH_RESULTS:
                truncated = True
                break
            matches.append({"path": relative})
    return {"matches": matches, "truncated": truncated}


def _iter_workspace_files(root: Path):
    for directory, directory_names, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        directory_names[:] = sorted(
            name
            for name in directory_names
            if name.lower() not in _IGNORED_WORKSPACE_DIRECTORIES
            and name.lower() not in _SENSITIVE_WORKSPACE_DIRECTORIES
            and _is_workspace_directory(root, directory_path / name)
        )
        for filename in sorted(filenames):
            path = directory_path / filename
            if _is_workspace_file(root, path):
                yield path


def _is_workspace_file(root: Path, path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        file_stat = os.stat(path, follow_symlinks=False)
        _reject_workspace_link_components(root, path)
    except (OSError, RuntimeError, ToolPermissionError, ValueError):
        return False
    return (
        resolved.is_relative_to(root)
        and stat.S_ISREG(file_stat.st_mode)
        and not _is_reparse_point(file_stat)
        and getattr(file_stat, "st_nlink", 0) == 1
        and not _is_sensitive_workspace_path(root, path)
        and not _is_sensitive_workspace_path(root, resolved)
    )


def _is_workspace_directory(root: Path, path: Path) -> bool:
    try:
        resolved = path.resolve(strict=True)
        directory_stat = os.stat(path, follow_symlinks=False)
        _reject_workspace_link_components(root, path)
    except (OSError, RuntimeError, ToolPermissionError, ValueError):
        return False
    return (
        resolved.is_relative_to(root)
        and stat.S_ISDIR(directory_stat.st_mode)
        and not _is_reparse_point(directory_stat)
        and not _is_sensitive_workspace_path(root, path)
        and not _is_sensitive_workspace_path(root, resolved)
    )


def _is_sensitive_workspace_path(root: Path, path: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return True
    parts = [part.lower() for part in relative.parts]
    if any(part in _SENSITIVE_WORKSPACE_DIRECTORIES for part in parts[:-1]):
        return True
    if any(part in _IGNORED_WORKSPACE_DIRECTORIES for part in parts[:-1]):
        return True
    filename = parts[-1] if parts else ""
    filename_stem = filename.lstrip(".").split(".", 1)[0]
    return (
        filename == ".env"
        or filename.startswith(".env.")
        or filename in _SENSITIVE_WORKSPACE_FILENAMES
        or filename_stem in _SENSITIVE_WORKSPACE_STEMS
        or Path(filename).suffix.lower() in _SENSITIVE_WORKSPACE_SUFFIXES
    )


def _read_verified_workspace_bytes(root: Path, path: Path, display_path: str) -> bytes:
    _reject_workspace_link_components(root, path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ToolValidationError(f"File could not be opened safely: {display_path}") from exc
    try:
        before = os.fstat(descriptor)
        _validate_opened_workspace_file(root, path, before, display_path)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(_MAX_WORKSPACE_FILE_BYTES + 1)
        after = os.fstat(descriptor)
        _validate_opened_workspace_file(root, path, after, display_path)
        if (
            not os.path.samestat(before, after)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ToolValidationError(f"File changed while being read: {display_path}")
        if len(content) > _MAX_WORKSPACE_FILE_BYTES:
            raise ToolValidationError(f"File exceeds read limit: {display_path}")
        return content
    finally:
        os.close(descriptor)


def _validate_opened_workspace_file(
    root: Path,
    path: Path,
    opened_stat: os.stat_result,
    display_path: str,
) -> None:
    if not stat.S_ISREG(opened_stat.st_mode):
        raise ToolValidationError(f"Not a file: {display_path}")
    if _is_reparse_point(opened_stat):
        raise ToolPermissionError("Linked workspace files are not available to agents")
    if getattr(opened_stat, "st_nlink", 0) != 1:
        raise ToolPermissionError("Hard-linked workspace files are not available to agents")
    if opened_stat.st_size > _MAX_WORKSPACE_FILE_BYTES:
        raise ToolValidationError(f"File exceeds read limit: {display_path}")
    try:
        current_stat = os.stat(path, follow_symlinks=False)
        resolved = path.resolve(strict=True)
        _reject_workspace_link_components(root, path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolPermissionError("Workspace file path changed during validation") from exc
    if (
        not resolved.is_relative_to(root)
        or _is_sensitive_workspace_path(root, path)
        or _is_sensitive_workspace_path(root, resolved)
        or _is_reparse_point(current_stat)
        or not os.path.samestat(opened_stat, current_stat)
    ):
        raise ToolPermissionError("Workspace file path changed during validation")


def _reject_workspace_link_components(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ToolPermissionError("Path must stay within workspace root") from exc
    current = root
    for part in relative.parts:
        current /= part
        try:
            component_stat = os.stat(current, follow_symlinks=False)
        except OSError as exc:
            raise ToolPermissionError("Workspace path could not be validated safely") from exc
        if stat.S_ISLNK(component_stat.st_mode) or _is_reparse_point(component_stat):
            raise ToolPermissionError("Linked workspace paths are not available to agents")


def _is_reparse_point(file_stat: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(
        reparse_attribute
        and getattr(file_stat, "st_file_attributes", 0) & reparse_attribute
    )


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
    return {"artifact_id": artifact.id, "content_hash": artifact.content_hash}


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


def _tool_input_schema(definition: ToolDefinition) -> dict[str, Any]:
    if definition.input_schema is not None:
        return definition.input_schema
    return _object_schema(
        {field: {} for field in sorted(definition.required_fields)},
        required=set(definition.required_fields),
    )


def _object_schema(properties: dict[str, Any], *, required: set[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(required or set()),
        "additionalProperties": False,
    }


def _string_schema(*, max_length: int) -> dict[str, Any]:
    return {"type": "string", "maxLength": max_length}


def _fetch_schema() -> dict[str, Any]:
    return _object_schema(
        {
            "url": _string_schema(max_length=4096),
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1_000_000},
        },
        required={"url"},
    )


def _validate_input_schema(tool_name: str, payload: dict[str, Any], schema: dict[str, Any]) -> None:
    properties = schema.get("properties")
    if schema.get("type") != "object" or not isinstance(properties, dict):
        raise ToolValidationError(f"Invalid input schema for tool: {tool_name}")
    if schema.get("additionalProperties") is False:
        unexpected = sorted(set(payload) - set(properties))
        if unexpected:
            raise ToolValidationError(f"Unexpected fields for {tool_name}: {', '.join(unexpected)}")
    for field_name, value in payload.items():
        field_schema = properties.get(field_name)
        if not isinstance(field_schema, dict):
            continue
        _validate_schema_value(tool_name, field_name, value, field_schema)


def _validate_schema_value(tool_name: str, field_name: str, value: Any, schema: dict[str, Any]) -> None:
    value_type = schema.get("type")
    valid = (
        value_type is None
        or (value_type == "string" and isinstance(value, str))
        or (value_type == "integer" and type(value) is int)
        or (value_type == "boolean" and type(value) is bool)
        or (value_type == "array" and isinstance(value, list))
    )
    if not valid:
        raise ToolValidationError(f"Invalid field type for {tool_name}.{field_name}")
    if isinstance(value, str) and isinstance(schema.get("maxLength"), int):
        if len(value) > schema["maxLength"]:
            raise ToolValidationError(f"Field is too long for {tool_name}.{field_name}")
    if type(value) is int:
        if isinstance(schema.get("minimum"), int) and value < schema["minimum"]:
            raise ToolValidationError(f"Field is below minimum for {tool_name}.{field_name}")
        if isinstance(schema.get("maximum"), int) and value > schema["maximum"]:
            raise ToolValidationError(f"Field exceeds maximum for {tool_name}.{field_name}")
    if isinstance(value, list):
        if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
            raise ToolValidationError(f"Too many items for {tool_name}.{field_name}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(tool_name, f"{field_name}[{index}]", item, item_schema)
