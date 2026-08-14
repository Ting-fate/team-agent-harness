from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime
from hashlib import sha256
import ipaddress
import json
import math
import os
import re
import sys
from collections.abc import Mapping
from time import monotonic
from typing import Any, NamedTuple, TextIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


DEFAULT_BASE_URL = "http://127.0.0.1:8014"
DEFAULT_TIMEOUT_SECONDS = 30.0
SERVER_NAME = "team-agent-harness-codex"
SERVER_VERSION = "1.0.0"
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    LATEST_PROTOCOL_VERSION,
}

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
HTTP_READ_CHUNK_BYTES = 64 * 1024
MAX_TOOL_RESULT_BYTES = 2 * 1024 * 1024
MAX_ERROR_CHARS = 500
MAX_JSON_DEPTH = 12
MAX_JSON_ITEMS = 1024
RECENT_LIST_LIMIT = 50
MAX_RUN_BINDINGS = 128
MAX_FINAL_ARTIFACT_CHARS = 100_000
STDIO_ENCODING_ERROR_EXIT_CODE = 3

REAL_MODELS_CAPABILITY_ENV = "TEAM_AGENT_CODEX_ALLOW_REAL_MODELS"
REAL_WEB_CAPABILITY_ENV = "TEAM_AGENT_CODEX_ALLOW_REAL_WEB"

JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_OPERATIONAL_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_ROLE_CARD_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
_ROLE_CARD_ID_RE = re.compile(_ROLE_CARD_ID_PATTERN)
_LOWER_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_PACK_PATH = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_SAFE_RUN_PATH = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_SAFE_ARTIFACT_PATH = _SAFE_RUN_PATH
_RUN_STATUSES = frozenset({"queued", "running", "waiting", "failed", "completed", "cancelled"})
_TRACE_EVENT_TYPES = frozenset(
    {
        "model_action",
        "workflow_event",
        "runtime_event",
        "tool_call",
        "tool_result",
        "handoff",
        "artifact_created",
        "eval_result",
        "error",
    }
)
_ARTIFACT_TYPES = frozenset(
    {
        "design_doc",
        "patch",
        "test_report",
        "source_summary",
        "research_note",
        "image_description",
        "final_report",
    }
)
_ARTIFACT_VALIDATION_STATUSES = frozenset({"unvalidated", "pass", "warn", "fail"})
_EVAL_STATUSES = frozenset({"pass", "warn", "fail"})
_TEAM_MODEL_FAMILIES = frozenset({"gpt", "deepseek"})
_TEAM_MODEL_PROVIDERS = frozenset({"openai", "deepseek", "litellm_proxy"})
_REAL_WEB_TOOL_NAMES = frozenset(
    {"web_search", "fetch_page", "browser_search", "browser_fetch"}
)
_TEAM_RUNTIME_LIMIT_FIELDS = frozenset(
    {
        "max_steps",
        "max_tool_calls",
        "max_total_tokens",
        "timeout_seconds",
        "max_repeated_tool_calls",
        "max_observation_chars",
        "max_cost_usd",
    }
)
_TRACE_PAYLOAD_STRING_FIELDS = frozenset(
    {
        "action",
        "agent_id",
        "artifact_id",
        "artifact_type",
        "check_name",
        "error_class",
        "model",
        "outcome",
        "provider",
        "status",
        "step_name",
        "tool_name",
    }
)
_TRACE_PAYLOAD_BOOLEAN_FIELDS = frozenset({"real_model_access_confirmed", "run_bound"})
_TRACE_PAYLOAD_INTEGER_FIELDS = frozenset({"attempt", "duration_ms"})
_USAGE_COUNTER_FIELDS = frozenset({"input_tokens", "output_tokens", "total_tokens"})
_QUALITY_COUNTER_FIELDS = frozenset(
    {
        "model_calls",
        "tool_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "unmetered_model_calls",
    }
)
_QUALITY_CRITERIA_FIELDS = (
    "required_artifact_types",
    "required_step_artifacts",
    "required_eval_checks",
    "final_artifact_type",
    "pack_eval_step_name",
    "min_final_artifact_chars",
    "require_completed_run",
    "verify_artifact_hashes",
)
_SENSITIVE_FIELD_MARKERS = (
    "apikey",
    "authorization",
    "clientsecret",
    "connectionstring",
    "credential",
    "databaseurl",
    "dburl",
    "dsn",
    "password",
    "passwd",
    "privatekey",
    "pwd",
    "secret",
    "token",
)
_SAFE_COUNTER_FIELD_NAMES = frozenset(
    {
        "completiontokens",
        "inputtokens",
        "maxoutputtokens",
        "maxtokens",
        "maxtotaltokens",
        "outputtokens",
        "prompttokens",
        "totaltokens",
    }
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(Bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;&]+"),
    re.compile(r"(?i)https?://[^\s/@:]+:[^\s/@]+@"),
    re.compile(r"\b(?:sk|ghp|github_pat)-[A-Za-z0-9_-]+\b", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", re.IGNORECASE),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)
_SECRET_ASSIGNMENT_KEY = (
    r"[A-Za-z0-9_.-]*(?:api[_-]?key|apikey|access[_-]?key|client[_-]?secret|credential|"
    r"database[_-]?url|db[_-]?url|dsn|connection[_-]?string|password|passwd|private[_-]?key|"
    r"pwd|secret|token)"
)
_SECRET_VALUE_PATTERNS = (
    re.compile(rf"(?i)\b{_SECRET_ASSIGNMENT_KEY}\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bauthorization\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@/\s]+@"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", re.IGNORECASE),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
)


class _HttpResponseContract(NamedTuple):
    operation: str
    method: str
    path_pattern: re.Pattern[str]
    expected_status: int
    top_level_type: type
    required_keys: frozenset[str] = frozenset()
    list_item_type: type | None = None


class _JsonFieldRule(NamedTuple):
    name: str
    expected_types: tuple[type, ...]
    allowed_values: frozenset[Any] | None = None


class _RunBinding(NamedTuple):
    task_id: str
    team_selection: dict[str, Any] | None
    execution_plan_hash: str | None
    real_model_access_confirmed: bool
    real_web_access_confirmed: bool
    confirmed_real_web_tools: tuple[str, ...] | None
    confirmed_real_web_tool_routes: tuple[tuple[str, str], ...] | None


_HTTP_RESPONSE_CONTRACTS = (
    _HttpResponseContract(
        "health",
        "GET",
        re.compile(r"^/health$"),
        200,
        dict,
        frozenset({"status", "worker"}),
    ),
    _HttpResponseContract("list_workflow_packs", "GET", re.compile(r"^/workflow-packs$"), 200, list, list_item_type=dict),
    _HttpResponseContract("list_agents", "GET", re.compile(r"^/agents$"), 200, list, list_item_type=dict),
    _HttpResponseContract("list_model_providers", "GET", re.compile(r"^/model-providers$"), 200, list, list_item_type=dict),
    _HttpResponseContract("list_tool_providers", "GET", re.compile(r"^/tool-providers$"), 200, list, list_item_type=dict),
    _HttpResponseContract(
        "list_recent_tasks",
        "GET",
        re.compile(r"^/tasks\?limit=50&offset=0$"),
        200,
        list,
        list_item_type=dict,
    ),
    _HttpResponseContract(
        "list_recent_runs",
        "GET",
        re.compile(r"^/runs\?limit=50&offset=0$"),
        200,
        list,
        list_item_type=dict,
    ),
    _HttpResponseContract(
        "get_team_template",
        "GET",
        re.compile(rf"^/workflow-packs/{_SAFE_PACK_PATH}/team-template$"),
        200,
        dict,
        frozenset({"team_selection", "slots", "role_cards", "configuration_warnings"}),
    ),
    _HttpResponseContract(
        "create_task",
        "POST",
        re.compile(r"^/tasks$"),
        201,
        dict,
        frozenset({"id", "title", "goal", "workflow_pack"}),
    ),
    _HttpResponseContract(
        "validate_team",
        "POST",
        re.compile(r"^/team-selections/validate$"),
        200,
        dict,
        frozenset({"valid", "team_selection", "public_execution_plan_hash", "immutable_after_run_creation"}),
    ),
    _HttpResponseContract(
        "start_run",
        "POST",
        re.compile(r"^/runs$"),
        201,
        dict,
        frozenset(
            {
                "id",
                "task_id",
                "status",
                "real_model_access_confirmed",
                "real_web_access_confirmed",
                "confirmed_real_web_tools",
                "confirmed_real_web_tool_routes",
                "execution_plan_hash",
            }
        ),
    ),
    _HttpResponseContract(
        "get_run",
        "GET",
        re.compile(rf"^/runs/{_SAFE_RUN_PATH}$"),
        200,
        dict,
        frozenset(
            {
                "id",
                "task_id",
                "status",
                "real_model_access_confirmed",
                "real_web_access_confirmed",
                "confirmed_real_web_tools",
                "confirmed_real_web_tool_routes",
                "execution_plan_hash",
                "final_artifact_id",
            }
        ),
    ),
    _HttpResponseContract(
        "get_run_detail",
        "GET",
        re.compile(rf"^/runs/{_SAFE_RUN_PATH}/detail$"),
        200,
        dict,
        frozenset({"run", "task", "agent_runs", "handoffs", "trace", "artifacts", "eval_results"}),
    ),
    _HttpResponseContract(
        "get_run_team",
        "GET",
        re.compile(rf"^/runs/{_SAFE_RUN_PATH}/team$"),
        200,
        dict,
        frozenset({"run_id", "team_selection", "execution_plan_hash", "immutable"}),
    ),
    _HttpResponseContract(
        "get_quality",
        "GET",
        re.compile(rf"^/runs/{_SAFE_RUN_PATH}/quality$"),
        200,
        dict,
        frozenset(
            {
                "run_id",
                "passed",
                "checks",
                "metrics",
                "criteria",
                "execution_plan_hash",
            }
        ),
    ),
    _HttpResponseContract(
        "get_artifact",
        "GET",
        re.compile(rf"^/artifacts/{_SAFE_ARTIFACT_PATH}$"),
        200,
        dict,
        frozenset({"artifact", "content"}),
    ),
)

_HTTP_RESPONSE_FIELD_RULES = {
    "health": (
        _JsonFieldRule("status", (str,), frozenset({"ok"})),
        _JsonFieldRule("worker", (str,), frozenset({"running"})),
    ),
    "get_team_template": (
        _JsonFieldRule("team_selection", (dict,)),
        _JsonFieldRule("slots", (list,)),
        _JsonFieldRule("role_cards", (list,)),
        _JsonFieldRule("configuration_warnings", (list,)),
    ),
    "create_task": tuple(
        _JsonFieldRule(field, (str,)) for field in ("id", "title", "goal", "workflow_pack")
    ),
    "validate_team": (
        _JsonFieldRule("valid", (bool,), frozenset({True})),
        _JsonFieldRule("team_selection", (dict,)),
        _JsonFieldRule("public_execution_plan_hash", (str,)),
        _JsonFieldRule("immutable_after_run_creation", (bool,), frozenset({True})),
    ),
    "start_run": (
        _JsonFieldRule("id", (str,)),
        _JsonFieldRule("task_id", (str,)),
        _JsonFieldRule("status", (str,), _RUN_STATUSES),
        _JsonFieldRule("real_model_access_confirmed", (bool,)),
        _JsonFieldRule("real_web_access_confirmed", (bool,)),
        _JsonFieldRule("confirmed_real_web_tools", (list, type(None))),
        _JsonFieldRule("confirmed_real_web_tool_routes", (list, type(None))),
        _JsonFieldRule("execution_plan_hash", (str,)),
    ),
    "get_run": (
        _JsonFieldRule("id", (str,)),
        _JsonFieldRule("task_id", (str,)),
        _JsonFieldRule("status", (str,), _RUN_STATUSES),
        _JsonFieldRule("real_model_access_confirmed", (bool,)),
        _JsonFieldRule("real_web_access_confirmed", (bool,)),
        _JsonFieldRule("confirmed_real_web_tools", (list, type(None))),
        _JsonFieldRule("confirmed_real_web_tool_routes", (list, type(None))),
        _JsonFieldRule("execution_plan_hash", (str, type(None))),
        _JsonFieldRule("final_artifact_id", (str, type(None))),
    ),
    "get_run_detail": (
        _JsonFieldRule("run", (dict,)),
        _JsonFieldRule("task", (dict,)),
        *(
            _JsonFieldRule(field, (list,))
            for field in ("agent_runs", "handoffs", "trace", "artifacts", "eval_results")
        ),
    ),
    "get_run_team": (
        _JsonFieldRule("run_id", (str,)),
        _JsonFieldRule("team_selection", (dict, type(None))),
        _JsonFieldRule("execution_plan_hash", (str, type(None))),
        _JsonFieldRule("immutable", (bool,)),
    ),
    "get_quality": (
        _JsonFieldRule("run_id", (str,)),
        _JsonFieldRule("passed", (bool,)),
        _JsonFieldRule("checks", (list,)),
        _JsonFieldRule("metrics", (dict,)),
        _JsonFieldRule("criteria", (dict,)),
        _JsonFieldRule("execution_plan_hash", (str, type(None))),
    ),
    "get_artifact": (
        _JsonFieldRule("artifact", (dict,)),
        _JsonFieldRule("content", (str,)),
    ),
}

_HTTP_LIST_ITEM_FIELD_RULES = {
    "list_workflow_packs": (
        _JsonFieldRule("name", (str,)),
        _JsonFieldRule("agents", (list,)),
        _JsonFieldRule("steps", (list,)),
    ),
    "list_agents": (
        _JsonFieldRule("id", (str,)),
        _JsonFieldRule("role", (str,)),
        _JsonFieldRule("model_config", (dict,)),
    ),
    "list_model_providers": (
        _JsonFieldRule("name", (str,)),
        _JsonFieldRule("enabled", (bool,)),
        _JsonFieldRule("real_calls", (bool,)),
    ),
    "list_tool_providers": (
        _JsonFieldRule("name", (str,)),
        _JsonFieldRule("enabled", (bool,)),
        _JsonFieldRule("real_calls", (bool,)),
    ),
    "list_recent_tasks": (
        _JsonFieldRule("id", (str,)),
        _JsonFieldRule("title", (str,)),
        _JsonFieldRule("workflow_pack", (str,)),
        _JsonFieldRule("created_at", (str,)),
    ),
    "list_recent_runs": (
        _JsonFieldRule("id", (str,)),
        _JsonFieldRule("task_id", (str,)),
        _JsonFieldRule("status", (str,), _RUN_STATUSES),
        _JsonFieldRule("current_step", (str, type(None))),
        _JsonFieldRule("final_artifact_id", (str, type(None))),
        _JsonFieldRule("started_at", (str, type(None))),
        _JsonFieldRule("finished_at", (str, type(None))),
        _JsonFieldRule("execution_plan_hash", (str, type(None))),
    ),
}

_RUN_DETAIL_LIST_ITEM_FIELD_RULES = {
    "agent_runs": (
        _JsonFieldRule("id", (str,)),
        _JsonFieldRule("run_id", (str,)),
        _JsonFieldRule("agent_id", (str,)),
        _JsonFieldRule("step_name", (str,)),
        _JsonFieldRule("status", (str,), _RUN_STATUSES),
    ),
    "handoffs": (
        _JsonFieldRule("id", (str,)),
        _JsonFieldRule("run_id", (str,)),
        _JsonFieldRule("from_agent_run_id", (str,)),
        _JsonFieldRule("to_agent_id", (str,)),
    ),
    "trace": (
        _JsonFieldRule("id", (str,)),
        _JsonFieldRule("run_id", (str,)),
        _JsonFieldRule("event_type", (str,), _TRACE_EVENT_TYPES),
        _JsonFieldRule("payload", (dict,)),
    ),
    "artifacts": (
        _JsonFieldRule("id", (str,)),
        _JsonFieldRule("run_id", (str,)),
        _JsonFieldRule("agent_run_id", (str,)),
        _JsonFieldRule("type", (str,), _ARTIFACT_TYPES),
        _JsonFieldRule("path", (str,)),
        _JsonFieldRule("validation_status", (str,), _ARTIFACT_VALIDATION_STATUSES),
    ),
    "eval_results": (
        _JsonFieldRule("id", (str,)),
        _JsonFieldRule("run_id", (str,)),
        _JsonFieldRule("check_name", (str,)),
        _JsonFieldRule("status", (str,), _EVAL_STATUSES),
    ),
}

_AGENT_RESPONSE_FIELD_RULES = (
    _JsonFieldRule("id", (str,)),
    _JsonFieldRule("role", (str,)),
    _JsonFieldRule("model_config", (dict,)),
)
_AGENT_MODEL_CONFIG_FIELD_RULES = (
    _JsonFieldRule("provider", (str,)),
    _JsonFieldRule("model", (str,)),
)
_WORKFLOW_STEP_FIELD_RULES = (
    _JsonFieldRule("name", (str,)),
    _JsonFieldRule("agent_role", (str,)),
)
_QUALITY_CHECK_FIELD_RULES = (
    _JsonFieldRule("name", (str,)),
    _JsonFieldRule("status", (str,), frozenset({"pass", "fail"})),
)
_ARTIFACT_RESPONSE_FIELD_RULES = (
    _JsonFieldRule("id", (str,)),
    _JsonFieldRule("run_id", (str,)),
    _JsonFieldRule("agent_run_id", (str,)),
    _JsonFieldRule("type", (str,), _ARTIFACT_TYPES),
    _JsonFieldRule("path", (str,)),
    _JsonFieldRule("content_hash", (str,)),
    _JsonFieldRule("source_refs", (list,)),
    _JsonFieldRule("validation_status", (str,), _ARTIFACT_VALIDATION_STATUSES),
    _JsonFieldRule("created_at", (str,)),
)


class HarnessMcpError(RuntimeError):
    pass


class ToolInputError(HarnessMcpError):
    pass


class HarnessApiError(HarnessMcpError):
    pass


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class HarnessApiClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.base_url = validate_base_url(base_url)
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
            raise HarnessMcpError("HTTP timeout must be a finite number from 1 to 120 seconds.")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout < 1 or timeout > 120:
            raise HarnessMcpError("HTTP timeout must be a finite number from 1 to 120 seconds.")
        self.timeout_seconds = timeout
        self._opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
        self._active_deadline: float | None = None

    @contextmanager
    def tool_deadline(self):
        previous_deadline = self._active_deadline
        deadline = monotonic() + self.timeout_seconds
        if previous_deadline is not None:
            deadline = min(deadline, previous_deadline)
        self._active_deadline = deadline
        try:
            yield
        finally:
            self._active_deadline = previous_deadline

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: Any) -> Any:
        return self._request("POST", path, payload)

    def _request(self, method: str, path: str, payload: Any | None = None) -> Any:
        deadline = self._active_deadline
        if deadline is None:
            deadline = monotonic() + self.timeout_seconds
        contract = _http_response_contract(method, path)
        url = urljoin(self.base_url, path.lstrip("/"))
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with self._opener.open(
                request,
                timeout=_remaining_timeout(deadline),
            ) as response:
                _remaining_timeout(deadline)
                status_code = getattr(response, "status", None)
                if type(status_code) is not int:
                    status_code = response.getcode()
                if status_code != contract.expected_status:
                    raise HarnessApiError(
                        f"Harness API returned HTTP {status_code}; expected {contract.expected_status}."
                    )
                content_type = str(response.headers.get("Content-Type", ""))
                media_type = content_type.split(";", 1)[0].strip().lower()
                if media_type != "application/json":
                    raise HarnessApiError("Harness API returned an unexpected Content-Type.")
                raw = _read_bounded_response(response, deadline)
        except HTTPError as exc:
            status_code = exc.code
            exc.close()
            raise HarnessApiError(f"Harness API returned HTTP {status_code}.") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise HarnessApiError("Cannot reach the local Harness API.") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise HarnessApiError("Harness API response exceeded the adapter limit.")
        if not raw or not raw.strip():
            raise HarnessApiError("Harness API returned an empty JSON body.")
        try:
            result = _strict_json_loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
            raise HarnessApiError("Harness API returned invalid JSON.") from exc
        projected = _validate_and_project_http_response(contract, path, payload, result)
        try:
            _remaining_timeout(deadline)
        except TimeoutError as exc:
            raise HarnessApiError("Cannot reach the local Harness API.") from exc
        return projected


def _remaining_timeout(deadline: float) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("Harness API request deadline exceeded.")
    return remaining


def _read_bounded_response(response: Any, deadline: float) -> bytes:
    chunks: list[bytes] = []
    total = 0
    read_chunk = getattr(response, "read1", None)
    if not callable(read_chunk):
        read_chunk = response.read
    while total <= MAX_RESPONSE_BYTES:
        remaining = _remaining_timeout(deadline)
        _set_response_socket_timeout(response, remaining)
        chunk = read_chunk(
            min(HTTP_READ_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - total)
        )
        _remaining_timeout(deadline)
        if not isinstance(chunk, bytes):
            raise HarnessApiError("Harness API returned an invalid response body.")
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _set_response_socket_timeout(response: Any, timeout: float) -> None:
    fp = getattr(response, "fp", None)
    raw = getattr(fp, "raw", None)
    candidates = (response, fp, raw, getattr(raw, "_sock", None))
    for candidate in candidates:
        settimeout = getattr(candidate, "settimeout", None)
        if callable(settimeout):
            settimeout(timeout)
            return


def _validate_and_project_http_response(
    contract: _HttpResponseContract,
    path: str,
    request_payload: Any | None,
    result: Any,
) -> Any:
    if type(result) is not contract.top_level_type:
        raise HarnessApiError("Harness API returned an unexpected top-level JSON structure.")
    if isinstance(result, dict) and not contract.required_keys.issubset(result):
        raise HarnessApiError("Harness API response is missing required fields.")
    if contract.list_item_type is not None and any(
        type(item) is not contract.list_item_type for item in result
    ):
        raise HarnessApiError("Harness API response contains invalid list items.")
    _validate_http_response_fields(contract.operation, result)
    _validate_http_response_context(contract.operation, path, request_payload, result)
    return _project_http_response(contract.operation, result)


def _http_response_contract(method: str, path: str) -> _HttpResponseContract:
    if not isinstance(method, str) or not isinstance(path, str):
        raise HarnessApiError("Harness API operation is not allowed.")
    matches = [
        contract
        for contract in _HTTP_RESPONSE_CONTRACTS
        if contract.method == method and contract.path_pattern.fullmatch(path)
    ]
    if len(matches) != 1:
        raise HarnessApiError("Harness API operation is not allowed.")
    return matches[0]


def _validate_http_response_context(
    operation: str,
    path: str,
    request_payload: Any | None,
    result: Any,
) -> None:
    if operation == "create_task":
        if type(request_payload) is not dict or any(
            result[field] != request_payload.get(field)
            for field in ("title", "goal", "workflow_pack")
        ):
            _raise_response_binding_error()
        return
    if operation == "validate_team":
        _validate_team_receipt_binding(request_payload, result["team_selection"])
        return
    if operation == "start_run":
        if (
            type(request_payload) is not dict
            or result["task_id"] != request_payload.get("task_id")
            or result["real_model_access_confirmed"]
            is not request_payload.get("confirm_real_models", False)
            or result["real_web_access_confirmed"]
            is not request_payload.get("confirm_real_web", False)
        ):
            _raise_response_binding_error()
        return
    if operation == "get_team_template":
        requested_pack = path.split("/", 3)[2]
        if result["team_selection"]["pack_name"] != requested_pack:
            _raise_response_binding_error()
        return
    if operation == "get_artifact":
        requested_artifact_id = path.split("/", 3)[2]
        if result["artifact"]["id"] != requested_artifact_id:
            _raise_response_binding_error()
        return
    if operation not in {"get_run", "get_run_detail", "get_run_team", "get_quality"}:
        return

    requested_run_id = path.split("/", 3)[2]
    if operation == "get_run" and result["id"] != requested_run_id:
        _raise_response_binding_error()
    elif operation == "get_run_detail":
        if (
            result["run"]["id"] != requested_run_id
            or result["run"]["task_id"] != result["task"]["id"]
            or any(
                item["run_id"] != requested_run_id
                for field in _RUN_DETAIL_LIST_ITEM_FIELD_RULES
                for item in result[field]
            )
        ):
            _raise_response_binding_error()
    elif operation == "get_run_team" and result["run_id"] != requested_run_id:
        _raise_response_binding_error()
    elif operation == "get_quality" and result["run_id"] != requested_run_id:
        _raise_response_binding_error()


def _validate_team_receipt_binding(request_selection: Any, receipt: dict[str, Any]) -> None:
    if type(request_selection) is not dict:
        _raise_response_binding_error()
    request_assignments = request_selection.get("assignments")
    receipt_assignments = receipt["assignments"]
    if (
        request_selection.get("version") != receipt["version"]
        or request_selection.get("pack_name") != receipt["pack_name"]
        or type(request_assignments) is not list
        or len(request_assignments) != len(receipt_assignments)
    ):
        _raise_response_binding_error()

    receipts_by_slot = {item["slot"]: item for item in receipt_assignments}
    if len(receipts_by_slot) != len(receipt_assignments):
        _raise_response_binding_error()
    for assignment in request_assignments:
        if type(assignment) is not dict or type(assignment.get("route")) is not dict:
            _raise_response_binding_error()
        route = assignment["route"]
        request_fallbacks = route.get("fallbacks", [])
        if type(request_fallbacks) is not list:
            _raise_response_binding_error()
        response_assignment = receipts_by_slot.get(assignment.get("slot"))
        if response_assignment is None:
            _raise_response_binding_error()
        expected_reasoning = route.get("reasoning_effort") or "xhigh"
        expected_primary = (route.get("family"), route.get("provider"), route.get("model"))
        response_primary = (
            response_assignment["model_family"],
            response_assignment["provider"],
            response_assignment["model"],
        )
        expected_fallbacks = [
            (item.get("family"), item.get("provider"), item.get("model"))
            for item in request_fallbacks
            if type(item) is dict
        ]
        response_fallbacks = [
            (item["model_family"], item["provider"], item["model"])
            for item in response_assignment["fallbacks"]
        ]
        if (
            response_assignment.get("role_card_id") != assignment.get("role_card_id")
            or response_assignment["reasoning_effort"] != expected_reasoning
            or response_primary != expected_primary
            or response_fallbacks != expected_fallbacks
            or len(expected_fallbacks) != len(request_fallbacks)
        ):
            _raise_response_binding_error()


def _raise_response_binding_error() -> None:
    raise HarnessApiError("Harness API response does not match the request.")


def _validate_http_response_fields(operation: str, result: Any) -> None:
    if type(result) is dict:
        _validate_response_object(result, _HTTP_RESPONSE_FIELD_RULES.get(operation, ()))
    item_rules = _HTTP_LIST_ITEM_FIELD_RULES.get(operation, ())
    if item_rules:
        for item in result:
            _validate_response_object(item, item_rules)

    if operation == "list_workflow_packs":
        for pack in result:
            _validate_response_identifier(pack["name"], 100)
            _validate_response_object_list(pack["agents"], _AGENT_RESPONSE_FIELD_RULES)
            for agent in pack["agents"]:
                _validate_agent_response(agent)
            _validate_response_object_list(pack["steps"], _WORKFLOW_STEP_FIELD_RULES)
            for step in pack["steps"]:
                _validate_response_identifier(step["name"])
                _validate_bounded_response_string(step["agent_role"], 200)

    if operation == "list_agents":
        for item in result:
            _validate_agent_response(item)

    if operation in {"list_model_providers", "list_tool_providers"}:
        for item in result:
            _validate_response_identifier(item["name"], 100)

    if operation == "list_recent_tasks":
        if len(result) > RECENT_LIST_LIMIT:
            raise HarnessApiError("Harness API response exceeded the recent-task limit.")
        for item in result:
            _validate_response_identifier(item["id"], 128)
            _validate_bounded_response_string(item["title"], 500)
            _validate_response_identifier(item["workflow_pack"], 100)
            _validate_iso_datetime(item["created_at"])

    if operation == "list_recent_runs":
        if len(result) > RECENT_LIST_LIMIT:
            raise HarnessApiError("Harness API response exceeded the recent-run limit.")
        for item in result:
            _validate_response_identifier(item["id"], 128)
            _validate_response_identifier(item["task_id"], 128)
            if item["current_step"] is not None:
                _validate_response_identifier(item["current_step"], 100)
            if item["final_artifact_id"] is not None:
                _validate_response_identifier(item["final_artifact_id"], 128)
            if item["started_at"] is not None:
                _validate_iso_datetime(item["started_at"])
            if item["finished_at"] is not None:
                _validate_iso_datetime(item["finished_at"])
            if item["execution_plan_hash"] is not None:
                _validate_lower_hex_hash(item["execution_plan_hash"])

    if operation == "get_team_template":
        _validate_team_template_response(result)
        _validate_response_object_list(
            result["role_cards"],
            (_JsonFieldRule("id", (str,)),),
        )
        for role_card in result["role_cards"]:
            _validate_role_card_id(role_card["id"])
        if any(type(item) is not str for item in result["configuration_warnings"]):
            raise HarnessApiError("Harness API response contains invalid field values.")
    elif operation == "create_task":
        _validate_response_identifier(result["id"], 128)
        _validate_response_identifier(result["workflow_pack"], 100)
    elif operation == "validate_team":
        _validate_team_selection_response(result["team_selection"], receipt=True)
        _validate_lower_hex_hash(result["public_execution_plan_hash"])
    elif operation == "start_run":
        _validate_response_identifier(result["id"], 128)
        _validate_response_identifier(result["task_id"], 128)
        _validate_lower_hex_hash(result["execution_plan_hash"])
        names, _routes = _normalized_real_web_snapshot(result)
        if not result["real_web_access_confirmed"] and names:
            raise HarnessApiError("Harness API response contains invalid field values.")
    elif operation == "get_run":
        _validate_response_identifier(result["id"], 128)
        _validate_response_identifier(result["task_id"], 128)
        if result["execution_plan_hash"] is not None:
            _validate_lower_hex_hash(result["execution_plan_hash"])
        if result["final_artifact_id"] is not None:
            _validate_response_identifier(result["final_artifact_id"], 128)
        names, _routes = _normalized_real_web_snapshot(result)
        if not result["real_web_access_confirmed"] and names:
            raise HarnessApiError("Harness API response contains invalid field values.")
    elif operation == "get_run_detail":
        detail_run_rules = (
            _JsonFieldRule("id", (str,)),
            _JsonFieldRule("task_id", (str,)),
            _JsonFieldRule("status", (str,), _RUN_STATUSES),
        )
        _validate_response_object(
            result["run"],
            detail_run_rules,
        )
        optional_run_fields = {
            rule.name for rule in _HTTP_RESPONSE_FIELD_RULES["get_run"]
        } - {rule.name for rule in detail_run_rules}
        present_optional_run_fields = optional_run_fields & result["run"].keys()
        if present_optional_run_fields and present_optional_run_fields != optional_run_fields:
            raise HarnessApiError("Harness API response is missing required typed fields.")
        if present_optional_run_fields:
            _validate_response_object(result["run"], _HTTP_RESPONSE_FIELD_RULES["get_run"])
        _validate_response_object(
            result["task"],
            (
                _JsonFieldRule("id", (str,)),
                _JsonFieldRule("title", (str,)),
                _JsonFieldRule("goal", (str,)),
                _JsonFieldRule("workflow_pack", (str,)),
            ),
        )
        for field, rules in _RUN_DETAIL_LIST_ITEM_FIELD_RULES.items():
            _validate_response_object_list(result[field], rules)
        _validate_response_identifier(result["run"]["id"], 128)
        _validate_response_identifier(result["run"]["task_id"], 128)
        if present_optional_run_fields:
            if result["run"]["execution_plan_hash"] is not None:
                _validate_lower_hex_hash(result["run"]["execution_plan_hash"])
            if result["run"]["final_artifact_id"] is not None:
                _validate_response_identifier(result["run"]["final_artifact_id"], 128)
            names, _routes = _normalized_real_web_snapshot(result["run"])
            if not result["run"]["real_web_access_confirmed"] and names:
                raise HarnessApiError("Harness API response contains invalid field values.")
        _validate_response_identifier(result["task"]["id"], 128)
        _validate_response_identifier(result["task"]["workflow_pack"], 100)
        identifier_fields = {
            "agent_runs": ("id", "run_id", "agent_id", "step_name"),
            "handoffs": ("id", "run_id", "from_agent_run_id", "to_agent_id"),
            "trace": ("id", "run_id"),
            "artifacts": ("id", "run_id", "agent_run_id"),
            "eval_results": ("id", "run_id", "check_name"),
        }
        for field, names in identifier_fields.items():
            for item in result[field]:
                for name in names:
                    _validate_response_identifier(item[name])
    elif operation == "get_run_team":
        _validate_response_identifier(result["run_id"], 128)
        _validate_run_team_state(result)
    elif operation == "get_quality":
        _validate_response_object_list(result["checks"], _QUALITY_CHECK_FIELD_RULES)
        _validate_response_identifier(result["run_id"], 128)
        if result["execution_plan_hash"] is not None:
            _validate_lower_hex_hash(result["execution_plan_hash"])
        criteria = _validate_quality_criteria(result["criteria"])
        if not result["checks"] or result["passed"] != all(
            item["status"] == "pass" for item in result["checks"]
        ):
            raise HarnessApiError("Harness API response contains invalid field values.")
        check_names = [item["name"] for item in result["checks"]]
        expected_check_names = _quality_check_names_from_criteria(criteria)
        if check_names != expected_check_names:
            raise HarnessApiError("Harness API response does not match its quality criteria.")
        for item in result["checks"]:
            _validate_quality_check_name(item["name"])
        _validate_quality_metrics(result["metrics"])
    elif operation == "get_artifact":
        artifact = result["artifact"]
        _validate_response_object(artifact, _ARTIFACT_RESPONSE_FIELD_RULES)
        for field in ("id", "run_id", "agent_run_id"):
            _validate_response_identifier(artifact[field], 128)
        _validate_bounded_response_string(artifact["path"], 4096)
        _validate_lower_hex_hash(artifact["content_hash"])
        if len(artifact["source_refs"]) > 128:
            raise HarnessApiError("Harness API response contains invalid field values.")
        for source_ref in artifact["source_refs"]:
            _validate_bounded_response_string(source_ref, 4096)
        _validate_iso_datetime(artifact["created_at"])


def _validate_response_object(value: Any, rules: tuple[_JsonFieldRule, ...]) -> None:
    if type(value) is not dict or any(rule.name not in value for rule in rules):
        raise HarnessApiError("Harness API response is missing required typed fields.")
    for rule in rules:
        field_value = value[rule.name]
        if type(field_value) not in rule.expected_types:
            raise HarnessApiError("Harness API response contains invalid field types.")
        if rule.allowed_values is not None and field_value not in rule.allowed_values:
            raise HarnessApiError("Harness API response contains invalid field values.")


def _validate_response_object_list(value: list[Any], rules: tuple[_JsonFieldRule, ...]) -> None:
    for item in value:
        _validate_response_object(item, rules)


def _validate_agent_response(value: dict[str, Any]) -> None:
    _validate_response_object(value["model_config"], _AGENT_MODEL_CONFIG_FIELD_RULES)
    _validate_response_identifier(value["id"])
    _validate_bounded_response_string(value["role"], 200)
    _validate_response_identifier(value["model_config"]["provider"], 100)
    _validate_response_identifier(value["model_config"]["model"])


def _validate_team_template_response(value: dict[str, Any]) -> None:
    selection = value["team_selection"]
    _validate_team_selection_response(selection, receipt=False)
    slots = value["slots"]
    if not 1 <= len(slots) <= 32:
        raise HarnessApiError("Harness API response contains invalid field values.")
    _validate_response_object_list(
        slots,
        (
            _JsonFieldRule("slot", (str,)),
            _JsonFieldRule("agent_id", (str,)),
            _JsonFieldRule("tool_permissions", (list,)),
            _JsonFieldRule("runtime_limits", (dict,)),
        ),
    )
    slot_names: list[str] = []
    agent_ids: list[str] = []
    for item in slots:
        _validate_bounded_response_string(item["slot"], 200)
        _validate_response_identifier(item["agent_id"])
        slot_names.append(item["slot"])
        agent_ids.append(item["agent_id"])
        if len(item["tool_permissions"]) > 64:
            raise HarnessApiError("Harness API response contains invalid field values.")
        for permission in item["tool_permissions"]:
            _validate_response_identifier(permission)
        _validate_runtime_limits(item["runtime_limits"])
    if len(set(slot_names)) != len(slot_names) or len(set(agent_ids)) != len(agent_ids):
        raise HarnessApiError("Harness API response contains invalid field values.")
    assignment_slots = [item["slot"] for item in selection["assignments"]]
    if set(assignment_slots) != set(slot_names):
        raise HarnessApiError("Harness API response contains invalid field values.")


def _validate_runtime_limits(value: dict[str, Any]) -> None:
    if set(value) - _TEAM_RUNTIME_LIMIT_FIELDS:
        raise HarnessApiError("Harness API response contains unsupported runtime-limit fields.")
    for item in value.values():
        if (
            type(item) not in {int, float}
            or item < 0
            or (type(item) is float and not math.isfinite(item))
        ):
            raise HarnessApiError("Harness API response contains invalid field values.")


def _validate_role_card_id(value: Any) -> None:
    if value is None:
        return
    if (
        type(value) is not str
        or len(value) > 200
        or not _ROLE_CARD_ID_RE.fullmatch(value)
    ):
        raise HarnessApiError("Harness API response contains invalid field values.")


def _validate_bounded_response_string(value: Any, max_length: int) -> None:
    if (
        type(value) is not str
        or not value.strip()
        or value != value.strip()
        or len(value) > max_length
        or "\x00" in value
    ):
        raise HarnessApiError("Harness API response contains invalid field values.")


def _validate_iso_datetime(value: Any) -> None:
    if type(value) is not str or not value or len(value) > 64:
        raise HarnessApiError("Harness API response contains invalid field values.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HarnessApiError("Harness API response contains invalid field values.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HarnessApiError("Harness API response contains invalid field values.")


def _validate_response_identifier(value: Any, max_length: int = 200) -> None:
    if (
        type(value) is not str
        or len(value) > max_length
        or not _OPERATIONAL_IDENTIFIER_RE.fullmatch(value)
    ):
        raise HarnessApiError("Harness API response contains invalid field values.")


def _validate_lower_hex_hash(value: Any) -> None:
    if type(value) is not str or not _LOWER_HEX_64_RE.fullmatch(value):
        raise HarnessApiError("Harness API response contains invalid field values.")


def _validate_run_team_state(value: dict[str, Any]) -> None:
    selection = value["team_selection"]
    plan_hash = value["execution_plan_hash"]
    immutable = value["immutable"]
    if selection is None and plan_hash is None and immutable is False:
        return
    if selection is None and plan_hash is not None and immutable is True:
        _validate_lower_hex_hash(plan_hash)
        return
    if type(selection) is dict and immutable is True:
        _validate_lower_hex_hash(plan_hash)
        _validate_team_selection_response(selection, receipt=True)
        return
    raise HarnessApiError("Harness API response contains invalid team receipt state.")


def _normalized_real_web_snapshot(
    value: dict[str, Any],
) -> tuple[tuple[str, ...] | None, tuple[tuple[str, str], ...] | None]:
    names = value["confirmed_real_web_tools"]
    routes = value["confirmed_real_web_tool_routes"]
    if names is None or routes is None:
        if names is None and routes is None:
            return None, None
        raise HarnessApiError("Harness API response contains an incomplete real-web snapshot.")
    if len(names) > 4 or len(routes) > 4:
        raise HarnessApiError("Harness API response contains invalid field values.")

    normalized_names: list[str] = []
    for name in names:
        if type(name) is not str or name not in _REAL_WEB_TOOL_NAMES:
            raise HarnessApiError("Harness API response contains invalid field values.")
        normalized_names.append(name)
    if len(normalized_names) != len(set(normalized_names)):
        raise HarnessApiError("Harness API response contains duplicate real-web tools.")

    normalized_routes: list[tuple[str, str]] = []
    for route in routes:
        _validate_response_object(
            route,
            (
                _JsonFieldRule("name", (str,), _REAL_WEB_TOOL_NAMES),
                _JsonFieldRule("provider", (str,)),
            ),
        )
        provider = route["provider"]
        _validate_response_identifier(provider, 100)
        if provider != provider.lower():
            raise HarnessApiError("Harness API response contains invalid field values.")
        normalized_routes.append((route["name"], provider))
    if len(normalized_routes) != len(set(normalized_routes)):
        raise HarnessApiError("Harness API response contains duplicate real-web routes.")

    normalized_names.sort()
    normalized_routes.sort()
    if tuple(normalized_names) != tuple(name for name, _provider in normalized_routes):
        raise HarnessApiError("Harness API response contains mismatched real-web snapshots.")
    return tuple(normalized_names), tuple(normalized_routes)


def _validate_quality_metrics(value: dict[str, Any]) -> None:
    _validate_response_object(
        value,
        (
            *(
                _JsonFieldRule(field, (int,))
                for field in sorted(_QUALITY_COUNTER_FIELDS)
            ),
            _JsonFieldRule("usage_complete", (bool,)),
            _JsonFieldRule("duration_seconds", (int, float, type(None))),
        ),
    )
    if any(value[field] < 0 for field in _QUALITY_COUNTER_FIELDS):
        raise HarnessApiError("Harness API response contains invalid field values.")
    duration = value["duration_seconds"]
    if duration is not None and (
        duration < 0
        or (type(duration) is float and not math.isfinite(duration))
    ):
        raise HarnessApiError("Harness API response contains invalid field values.")


def _validate_quality_criteria(value: dict[str, Any]) -> dict[str, Any]:
    _validate_response_object(
        value,
        (
            _JsonFieldRule("required_artifact_types", (list,)),
            _JsonFieldRule("required_step_artifacts", (dict,)),
            _JsonFieldRule("required_eval_checks", (list,)),
            _JsonFieldRule("final_artifact_type", (str,), _ARTIFACT_TYPES),
            _JsonFieldRule("pack_eval_step_name", (str,)),
            _JsonFieldRule("min_final_artifact_chars", (int,)),
            _JsonFieldRule("require_completed_run", (bool,)),
            _JsonFieldRule("verify_artifact_hashes", (bool,)),
        ),
    )
    artifact_types = value["required_artifact_types"]
    step_artifacts = value["required_step_artifacts"]
    eval_checks = value["required_eval_checks"]
    if (
        not 1 <= len(artifact_types) <= len(_ARTIFACT_TYPES)
        or any(type(item) is not str or item not in _ARTIFACT_TYPES for item in artifact_types)
        or len(artifact_types) != len(set(artifact_types))
        or value["final_artifact_type"] not in artifact_types
        or not 1 <= len(step_artifacts) <= 32
        or not 1 <= len(eval_checks) <= 544
        or len(eval_checks) != len(set(eval_checks))
        or not 0 <= value["min_final_artifact_chars"] <= 1_000_000
    ):
        raise HarnessApiError("Harness API response contains invalid quality criteria.")
    for step_name, artifact_type in step_artifacts.items():
        _validate_response_identifier(step_name, 100)
        if type(artifact_type) is not str or artifact_type not in artifact_types:
            raise HarnessApiError("Harness API response contains invalid quality criteria.")
    for check_name in eval_checks:
        _validate_quality_check_name(check_name)
    if any(
        not any(check_name.startswith(f"{step_name}:acceptance:") for check_name in eval_checks)
        for step_name in step_artifacts
    ):
        raise HarnessApiError("Harness API response contains incomplete quality criteria.")
    if value["pack_eval_step_name"] not in step_artifacts:
        raise HarnessApiError("Harness API response contains invalid quality criteria.")
    return _project_quality_criteria(value)


def _quality_check_names_from_criteria(criteria: dict[str, Any]) -> list[str]:
    names: list[str] = []
    if criteria["require_completed_run"]:
        names.append("run_completed")
    names.extend(f"artifact:{artifact_type}" for artifact_type in criteria["required_artifact_types"])
    names.extend(
        f"artifact:{step_name}:{artifact_type}"
        for step_name, artifact_type in criteria["required_step_artifacts"].items()
    )
    names.extend(f"eval:{check_name}" for check_name in criteria["required_eval_checks"])
    if criteria["verify_artifact_hashes"]:
        names.append("artifact_hashes")
    names.extend(
        (
            "final_artifact_run",
            "final_artifact_latest_completed_attempt",
            "final_artifact_type",
        )
    )
    if criteria["min_final_artifact_chars"]:
        names.append("final_artifact_content")
    return names


def _validate_quality_check_name(value: Any) -> None:
    if (
        type(value) is not str
        or len(value) > 320
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*", value)
    ):
        raise HarnessApiError("Harness API response contains invalid field values.")


def _validate_team_selection_response(value: Any, *, receipt: bool) -> None:
    _validate_response_object(
        value,
        (
            _JsonFieldRule("version", (str,), frozenset({"team-selection-v1"})),
            _JsonFieldRule("pack_name", (str,)),
            _JsonFieldRule("assignments", (list,)),
        ),
    )
    _validate_response_identifier(value["pack_name"], 100)
    assignments = value["assignments"]
    if not 1 <= len(assignments) <= 32:
        raise HarnessApiError("Harness API response contains invalid field values.")
    slots: list[str] = []
    agent_ids: list[str] = []
    for assignment in assignments:
        if receipt:
            _validate_response_object(
                assignment,
                (
                    _JsonFieldRule("slot", (str,)),
                    _JsonFieldRule("agent_id", (str,)),
                    _JsonFieldRule("model_family", (str,), _TEAM_MODEL_FAMILIES),
                    _JsonFieldRule("provider", (str,), _TEAM_MODEL_PROVIDERS),
                    _JsonFieldRule("model", (str,)),
                    _JsonFieldRule("reasoning_effort", (str,)),
                    _JsonFieldRule("fallbacks", (list,)),
                ),
            )
            _validate_bounded_response_string(assignment["slot"], 200)
            _validate_response_identifier(assignment["agent_id"])
            _validate_response_identifier(assignment["model"])
            _validate_bounded_response_string(assignment["reasoning_effort"], 200)
            _validate_role_card_id(assignment.get("role_card_id"))
            if len(assignment["fallbacks"]) > 4:
                raise HarnessApiError("Harness API response contains invalid field values.")
            _validate_response_object_list(
                assignment["fallbacks"],
                (
                    _JsonFieldRule("model_family", (str,), _TEAM_MODEL_FAMILIES),
                    _JsonFieldRule("provider", (str,), _TEAM_MODEL_PROVIDERS),
                    _JsonFieldRule("model", (str,)),
                ),
            )
            slots.append(assignment["slot"])
            agent_ids.append(assignment["agent_id"])
            _validate_response_model_target(
                assignment["model_family"],
                assignment["provider"],
                assignment["model"],
            )
            for fallback in assignment["fallbacks"]:
                _validate_response_model_target(
                    fallback["model_family"],
                    fallback["provider"],
                    fallback["model"],
                )
                _validate_response_identifier(fallback["model"])
            continue
        _validate_response_object(
            assignment,
            (
                _JsonFieldRule("slot", (str,)),
                _JsonFieldRule("route", (dict,)),
            ),
        )
        _validate_bounded_response_string(assignment["slot"], 200)
        _validate_role_card_id(assignment.get("role_card_id"))
        _validate_response_object(
            assignment["route"],
            (
                _JsonFieldRule("family", (str,), _TEAM_MODEL_FAMILIES),
                _JsonFieldRule("provider", (str,), _TEAM_MODEL_PROVIDERS),
                _JsonFieldRule("model", (str,)),
                _JsonFieldRule("fallbacks", (list,)),
            ),
        )
        route = assignment["route"]
        _validate_response_identifier(route["model"])
        if len(route["fallbacks"]) > 4:
            raise HarnessApiError("Harness API response contains invalid field values.")
        if "reasoning_effort" in route and route["reasoning_effort"] is not None:
            _validate_bounded_response_string(route["reasoning_effort"], 200)
        _validate_response_object_list(
            route["fallbacks"],
            (
                _JsonFieldRule("family", (str,), _TEAM_MODEL_FAMILIES),
                _JsonFieldRule("provider", (str,), _TEAM_MODEL_PROVIDERS),
                _JsonFieldRule("model", (str,)),
            ),
        )
        slots.append(assignment["slot"])
        _validate_response_model_target(route["family"], route["provider"], route["model"])
        for fallback in route["fallbacks"]:
            _validate_response_identifier(fallback["model"])
            _validate_response_model_target(
                fallback["family"],
                fallback["provider"],
                fallback["model"],
            )
    if len(set(slots)) != len(slots):
        raise HarnessApiError("Harness API response contains invalid field values.")
    if receipt and len(set(agent_ids)) != len(agent_ids):
        raise HarnessApiError("Harness API response contains invalid field values.")


def _validate_response_model_target(family: str, provider: str, model: str) -> None:
    try:
        _validate_family_provider_model(family, provider, model, "response route")
    except ToolInputError as exc:
        raise HarnessApiError("Harness API response contains invalid field values.") from exc


def _project_http_response(operation: str, result: Any) -> Any:
    if operation == "health":
        return _project_response_fields(result, _HTTP_RESPONSE_FIELD_RULES[operation])
    if operation == "list_workflow_packs":
        return [
            {
                "name": pack["name"],
                "agents": [_project_agent_response(agent) for agent in pack["agents"]],
                "steps": [
                    _project_response_fields(step, _WORKFLOW_STEP_FIELD_RULES)
                    for step in pack["steps"]
                ],
            }
            for pack in result
        ]
    if operation == "list_agents":
        return [_project_agent_response(agent) for agent in result]
    if operation in {"list_model_providers", "list_tool_providers"}:
        rules = _HTTP_LIST_ITEM_FIELD_RULES[operation]
        return [_project_response_fields(item, rules) for item in result]
    if operation in {"list_recent_tasks", "list_recent_runs"}:
        rules = _HTTP_LIST_ITEM_FIELD_RULES[operation]
        return [_project_response_fields(item, rules) for item in result]
    if operation == "get_team_template":
        return {
            "team_selection": _project_team_selection(result["team_selection"], receipt=False),
            "slots": [
                {
                    "slot": item["slot"],
                    "agent_id": item["agent_id"],
                    "tool_permissions": list(item["tool_permissions"]),
                    "runtime_limits": {
                        key: item["runtime_limits"][key]
                        for key in sorted(_TEAM_RUNTIME_LIMIT_FIELDS)
                        if key in item["runtime_limits"]
                    },
                }
                for item in result["slots"]
            ],
            "role_cards": [{"id": item["id"]} for item in result["role_cards"]],
            "configuration_warnings": list(result["configuration_warnings"]),
        }
    if operation == "validate_team":
        return {
            "valid": result["valid"],
            "team_selection": _project_team_selection(result["team_selection"], receipt=True),
            "public_execution_plan_hash": result["public_execution_plan_hash"],
            "immutable_after_run_creation": result["immutable_after_run_creation"],
        }
    if operation == "create_task":
        return _project_response_fields(result, _HTTP_RESPONSE_FIELD_RULES[operation])
    if operation == "start_run":
        return _project_run_response(operation, result)
    if operation == "get_run":
        return _project_run_response(operation, result)
    if operation == "get_run_detail":
        projected_lists: dict[str, list[dict[str, Any]]] = {}
        for field, rules in _RUN_DETAIL_LIST_ITEM_FIELD_RULES.items():
            projected_lists[field] = [
                _project_response_fields(item, rules) for item in result[field]
            ]
        for index, item in enumerate(result["trace"]):
            projected_lists["trace"][index]["payload"] = _project_trace_payload(item["payload"])
        projected_run = _project_response_fields(
            result["run"],
            (
                _JsonFieldRule("id", (str,)),
                _JsonFieldRule("task_id", (str,)),
                _JsonFieldRule("status", (str,)),
            ),
        )
        if "execution_plan_hash" in result["run"]:
            projected_run = _project_run_response("get_run", result["run"])
        return {
            "run": projected_run,
            "task": _project_response_fields(
                result["task"],
                tuple(
                    _JsonFieldRule(field, (str,))
                    for field in ("id", "workflow_pack")
                ),
            ),
            **projected_lists,
        }
    if operation == "get_run_team":
        return {
            "run_id": result["run_id"],
            "team_selection": (
                _project_team_selection(result["team_selection"], receipt=True)
                if result["team_selection"] is not None
                else None
            ),
            "execution_plan_hash": result["execution_plan_hash"],
            "immutable": result["immutable"],
        }
    if operation == "get_quality":
        return {
            "run_id": result["run_id"],
            "passed": result["passed"],
            "checks": [
                _project_response_fields(item, _QUALITY_CHECK_FIELD_RULES)
                for item in result["checks"]
            ],
            "metrics": {
                field: result["metrics"][field]
                for field in (
                    *sorted(_QUALITY_COUNTER_FIELDS),
                    "usage_complete",
                    "duration_seconds",
                )
                if field in result["metrics"]
            },
            "criteria": _project_quality_criteria(result["criteria"]),
            "execution_plan_hash": result["execution_plan_hash"],
        }
    if operation == "get_artifact":
        artifact = result["artifact"]
        return {
            "artifact": {
                field: artifact[field]
                for field in (
                    "id",
                    "run_id",
                    "type",
                    "content_hash",
                    "validation_status",
                )
            },
            "content": result["content"],
        }
    raise HarnessApiError("Harness API response projection is not defined.")


def _project_response_fields(
    value: dict[str, Any],
    rules: tuple[_JsonFieldRule, ...],
) -> dict[str, Any]:
    return {rule.name: value[rule.name] for rule in rules}


def _project_quality_criteria(value: dict[str, Any]) -> dict[str, Any]:
    return {
        field: (
            list(value[field])
            if field in {"required_artifact_types", "required_eval_checks"}
            else dict(value[field])
            if field == "required_step_artifacts"
            else value[field]
        )
        for field in _QUALITY_CRITERIA_FIELDS
    }


def _project_agent_response(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": value["id"],
        "role": value["role"],
        "model_config": {
            "provider": value["model_config"]["provider"],
            "model": value["model_config"]["model"],
        },
    }


def _project_run_response(operation: str, value: dict[str, Any]) -> dict[str, Any]:
    projected = _project_response_fields(value, _HTTP_RESPONSE_FIELD_RULES[operation])
    names, routes = _normalized_real_web_snapshot(value)
    projected["confirmed_real_web_tools"] = list(names) if names is not None else None
    projected["confirmed_real_web_tool_routes"] = (
        [{"name": name, "provider": provider} for name, provider in routes]
        if routes is not None
        else None
    )
    return projected


def _project_team_selection(value: dict[str, Any], *, receipt: bool) -> dict[str, Any]:
    assignments: list[dict[str, Any]] = []
    for item in value["assignments"]:
        if receipt:
            projected = {
                "slot": item["slot"],
                "agent_id": item["agent_id"],
                "model_family": item["model_family"],
                "provider": item["provider"],
                "model": item["model"],
                "reasoning_effort": item["reasoning_effort"],
                "fallbacks": [
                    {
                        "model_family": fallback["model_family"],
                        "provider": fallback["provider"],
                        "model": fallback["model"],
                    }
                    for fallback in item["fallbacks"]
                ],
            }
        else:
            route = item["route"]
            projected = {
                "slot": item["slot"],
                "route": {
                    "family": route["family"],
                    "provider": route["provider"],
                    "model": route["model"],
                    "fallbacks": [
                        {
                            "family": fallback["family"],
                            "provider": fallback["provider"],
                            "model": fallback["model"],
                        }
                        for fallback in route["fallbacks"]
                    ],
                },
            }
            if "reasoning_effort" in route:
                projected["route"]["reasoning_effort"] = route["reasoning_effort"]
        if "role_card_id" in item:
            projected["role_card_id"] = item["role_card_id"]
        assignments.append(projected)
    return {
        "version": value["version"],
        "pack_name": value["pack_name"],
        "assignments": assignments,
    }


def _project_trace_payload(value: dict[str, Any]) -> dict[str, Any]:
    projected: dict[str, Any] = {}
    for field in sorted(_TRACE_PAYLOAD_STRING_FIELDS):
        if field not in value:
            continue
        if type(value[field]) is not str:
            raise HarnessApiError("Harness API response contains invalid field types.")
        _validate_response_identifier(value[field])
        projected[field] = value[field]
    for field in sorted(_TRACE_PAYLOAD_BOOLEAN_FIELDS):
        if field not in value:
            continue
        if type(value[field]) is not bool:
            raise HarnessApiError("Harness API response contains invalid field types.")
        projected[field] = value[field]
    for field in sorted(_TRACE_PAYLOAD_INTEGER_FIELDS):
        if field not in value:
            continue
        if type(value[field]) is not int or value[field] < 0:
            raise HarnessApiError("Harness API response contains invalid field values.")
        projected[field] = value[field]
    if "usage" in value:
        usage = value["usage"]
        if type(usage) is not dict:
            raise HarnessApiError("Harness API response contains invalid field types.")
        projected["usage"] = {}
        for field in sorted(_USAGE_COUNTER_FIELDS):
            if field not in usage:
                continue
            if type(usage[field]) is not int or usage[field] < 0:
                raise HarnessApiError("Harness API response contains invalid field values.")
            projected["usage"][field] = usage[field]
    return projected


def validate_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise HarnessMcpError("Harness base URL must be a loopback http origin.")
    candidate = value.strip()
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError as exc:
        raise HarnessMcpError("Harness base URL must be a loopback http origin.") from exc
    if (
        parsed.scheme != "http"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.params
        or parsed.path not in {"", "/"}
        or port is None
    ):
        raise HarnessMcpError(
            "Harness base URL must be a loopback http origin with an explicit port "
            "and no credentials, path, query, or fragment."
        )
    hostname = parsed.hostname or ""
    if not _is_loopback_host(hostname):
        raise HarnessMcpError("Harness base URL must use a loopback host.")
    return f"http://{parsed.netloc}/"


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _route_schema() -> dict[str, Any]:
    fallback_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["family", "provider", "model"],
        "properties": {
            "family": {"type": "string", "enum": ["gpt", "deepseek"]},
            "provider": {"type": "string", "enum": ["openai", "deepseek", "litellm_proxy"]},
            "model": {"type": "string", "minLength": 1, "maxLength": 200},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["family", "provider", "model"],
        "properties": {
            "family": {"type": "string", "enum": ["gpt", "deepseek"]},
            "provider": {"type": "string", "enum": ["openai", "deepseek", "litellm_proxy"]},
            "model": {"type": "string", "minLength": 1, "maxLength": 200},
            "reasoning_effort": {
                "type": ["string", "null"],
                "enum": ["minimal", "low", "medium", "high", "xhigh", None],
            },
            "fallbacks": {"type": "array", "maxItems": 4, "items": fallback_schema},
        },
    }


def _team_selection_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["version", "pack_name", "assignments"],
        "properties": {
            "version": {"type": "string", "const": "team-selection-v1"},
            "pack_name": {"type": "string", "minLength": 1, "maxLength": 100},
            "assignments": {
                "type": "array",
                "minItems": 1,
                "maxItems": 32,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["slot", "route"],
                    "properties": {
                        "slot": {"type": "string", "minLength": 1, "maxLength": 200},
                        "role_card_id": {
                            "type": ["string", "null"],
                            "minLength": 1,
                            "maxLength": 200,
                            "pattern": _ROLE_CARD_ID_PATTERN,
                        },
                        "route": _route_schema(),
                    },
                },
            },
        },
    }


def _tool_definitions() -> list[dict[str, Any]]:
    empty_schema = {"type": "object", "additionalProperties": False, "properties": {}}
    run_id_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["run_id"],
        "properties": {"run_id": {"type": "string", "minLength": 1, "maxLength": 128}},
    }
    return [
        {
            "name": "harness_health",
            "description": "Read the local Team Agent Harness health status.",
            "inputSchema": empty_schema,
        },
        {
            "name": "harness_list_catalog",
            "description": "Read workflow, agent, model-provider, and tool-provider catalogs.",
            "inputSchema": empty_schema,
        },
        {
            "name": "harness_list_recent",
            "description": "Read up to 50 recent Harness tasks and runs with bounded metadata only.",
            "inputSchema": empty_schema,
        },
        {
            "name": "harness_get_team_template",
            "description": "Read the selectable Agent slots for one workflow pack.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["pack_name"],
                "properties": {"pack_name": {"type": "string", "minLength": 1, "maxLength": 100}},
            },
        },
        {
            "name": "harness_create_task",
            "description": "Create a Harness task without starting it.",
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "goal", "workflow_pack"],
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 500},
                    "goal": {"type": "string", "minLength": 1, "maxLength": 50000},
                    "workflow_pack": {"type": "string", "minLength": 1, "maxLength": 100},
                    "inputs": {"type": "object", "maxProperties": 128},
                    "constraints": {
                        "type": "array",
                        "maxItems": 64,
                        "items": {"type": "string", "minLength": 1, "maxLength": 5000},
                    },
                    "acceptance_criteria": {
                        "type": "array",
                        "maxItems": 64,
                        "items": {"type": "string", "minLength": 1, "maxLength": 5000},
                    },
                },
            },
        },
        {
            "name": "harness_validate_team",
            "description": "Validate a bounded GPT/DeepSeek Agent selection without starting a run.",
            "inputSchema": _team_selection_schema(),
        },
        {
            "name": "harness_start_run",
            "description": (
                "Start a persisted background run. Paid models and real web access require both "
                "an explicit argument and the matching MCP process capability."
            ),
            "inputSchema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["task_id"],
                "properties": {
                    "task_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "confirm_real_models": {"type": "boolean", "default": False},
                    "confirm_real_web": {"type": "boolean", "default": False},
                    "team_selection": _team_selection_schema(),
                },
            },
        },
        {
            "name": "harness_get_run",
            "description": "Read lightweight state for one Harness run.",
            "inputSchema": run_id_schema,
        },
        {
            "name": "harness_get_run_detail",
            "description": "Read the aggregated, server-redacted detail for one Harness run.",
            "inputSchema": run_id_schema,
        },
        {
            "name": "harness_get_run_team",
            "description": "Read the immutable selected-team receipt for one Harness run.",
            "inputSchema": run_id_schema,
        },
        {
            "name": "harness_get_quality",
            "description": "Read the verified quality report for one Harness run.",
            "inputSchema": run_id_schema,
        },
        {
            "name": "harness_get_final_artifact",
            "description": "Read bounded untrusted text from a completed run's final artifact.",
            "inputSchema": run_id_schema,
        },
    ]


TOOLS = _tool_definitions()
TOOL_NAMES = frozenset(tool["name"] for tool in TOOLS)


class McpServer:
    def __init__(self, client: Any, *, environ: Mapping[str, str] | None = None) -> None:
        self.client = client
        self.environ = os.environ if environ is None else environ
        self.initialized = False
        self._run_bindings: OrderedDict[str, _RunBinding] = OrderedDict()

    def handle_message(self, message: Any) -> dict[str, Any] | None:
        request_id: str | int | None = None
        has_id = False
        try:
            if not isinstance(message, dict):
                raise _JsonRpcFailure(JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC request.")
            has_id = "id" in message
            request_id = _validated_request_id(message.get("id")) if has_id else None
            _require_exact_keys(message, {"jsonrpc", "id", "method", "params"}, "request")
            if message.get("jsonrpc") != "2.0":
                raise _JsonRpcFailure(JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC version.")
            method = message.get("method")
            if not isinstance(method, str) or not method or len(method) > 128:
                raise _JsonRpcFailure(JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC method.")
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise _JsonRpcFailure(JSONRPC_INVALID_PARAMS, "Method params must be an object.")

            if not has_id:
                self._handle_notification(method, params)
                return None
            result = self._handle_request(method, params)
            return _jsonrpc_result(request_id, result)
        except _JsonRpcFailure as exc:
            if not has_id:
                return None
            return _jsonrpc_error(request_id, exc.code, exc.message)
        except ToolInputError as exc:
            if not has_id:
                return None
            return _jsonrpc_error(request_id, JSONRPC_INVALID_PARAMS, _redact_error_text(str(exc)))
        except Exception:
            if not has_id:
                return None
            return _jsonrpc_error(request_id, JSONRPC_INTERNAL_ERROR, "Internal adapter error.")

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "notifications/initialized":
            _require_exact_keys(params, {"_meta"}, "notifications/initialized params")
            return
        # Never execute tools from a fire-and-forget notification.

    def _handle_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            _require_exact_keys(params, {"_meta"}, "ping params")
            return {}
        if not self.initialized:
            raise _JsonRpcFailure(JSONRPC_INVALID_REQUEST, "MCP server is not initialized.")
        if method == "tools/list":
            _require_exact_keys(params, {"cursor", "_meta"}, "tools/list params")
            cursor = params.get("cursor")
            if cursor not in {None, ""}:
                raise _JsonRpcFailure(JSONRPC_INVALID_PARAMS, "Tool catalog does not support pagination.")
            return {"tools": TOOLS}
        if method == "tools/call":
            return self._call_tool(params)
        raise _JsonRpcFailure(JSONRPC_METHOD_NOT_FOUND, "Method not found.")

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.initialized:
            raise _JsonRpcFailure(JSONRPC_INVALID_REQUEST, "MCP server is already initialized.")
        _require_exact_keys(
            params,
            {"protocolVersion", "capabilities", "clientInfo", "_meta"},
            "initialize params",
        )
        requested = params.get("protocolVersion")
        if not isinstance(requested, str) or not requested or len(requested) > 32:
            raise _JsonRpcFailure(JSONRPC_INVALID_PARAMS, "Invalid MCP protocol version.")
        if not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
            raise _JsonRpcFailure(JSONRPC_INVALID_PARAMS, "Invalid MCP client metadata.")
        self.initialized = True
        negotiated = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        return {
            "protocolVersion": negotiated,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            _require_exact_keys(params, {"name", "arguments", "_meta"}, "tools/call params")
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not isinstance(name, str) or not name or len(name) > 128:
                raise ToolInputError("Tool name is invalid.")
            if not isinstance(arguments, dict):
                raise ToolInputError("Tool arguments must be an object.")
            if name not in TOOL_NAMES:
                raise ToolInputError("Unknown Harness tool.")
            if type(self.client) is HarnessApiClient:
                with self.client.tool_deadline():
                    result = self._dispatch_tool(name, arguments)
            else:
                result = self._dispatch_tool(name, arguments)
            return _tool_result(result)
        except (HarnessMcpError, ValueError) as exc:
            return _tool_error(_redact_error_text(str(exc)))
        except Exception:
            return _tool_error("Harness tool failed safely.")

    def _dispatch_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "harness_health":
            _require_exact_keys(arguments, set(), "harness_health arguments")
            return self.client.get("/health")
        if name == "harness_list_catalog":
            _require_exact_keys(arguments, set(), "harness_list_catalog arguments")
            return {
                "workflow_packs": self.client.get("/workflow-packs"),
                "agents": self.client.get("/agents"),
                "model_providers": self.client.get("/model-providers"),
                "tool_providers": self.client.get("/tool-providers"),
            }
        if name == "harness_list_recent":
            _require_exact_keys(arguments, set(), "harness_list_recent arguments")
            return {
                "tasks": self._validated_client_get("/tasks?limit=50&offset=0"),
                "runs": self._validated_client_get("/runs?limit=50&offset=0"),
            }
        if name == "harness_get_team_template":
            _require_exact_keys(arguments, {"pack_name"}, "harness_get_team_template arguments")
            pack_name = _validated_identifier(arguments.get("pack_name"), "pack_name", 100)
            return self.client.get(f"/workflow-packs/{quote(pack_name, safe='')}/team-template")
        if name == "harness_create_task":
            return self.client.post("/tasks", _validated_task_payload(arguments))
        if name == "harness_validate_team":
            return self.client.post("/team-selections/validate", _validated_team_selection(arguments))
        if name == "harness_start_run":
            return self._start_run(arguments)
        if name == "harness_get_final_artifact":
            return self._get_final_artifact(arguments)
        if name in {
            "harness_get_run",
            "harness_get_run_detail",
            "harness_get_run_team",
            "harness_get_quality",
        }:
            _require_exact_keys(arguments, {"run_id"}, f"{name} arguments")
            run_id = _validated_identifier(arguments.get("run_id"), "run_id", 128)
            suffix = {
                "harness_get_run": "",
                "harness_get_run_detail": "/detail",
                "harness_get_run_team": "/team",
                "harness_get_quality": "/quality",
            }[name]
            path = f"/runs/{quote(run_id, safe='')}{suffix}"
            binding = self._get_run_binding(run_id)
            canonical_run: dict[str, Any] | None = None
            canonical_team: dict[str, Any] | None = None
            if binding is None:
                binding, canonical_run, canonical_team = self._rebuild_run_binding(run_id)
            if name == "harness_get_run":
                if canonical_run is not None:
                    return canonical_run
                result = self.client.get(path)
                return self._validate_known_run_response(path, result, binding)
            if name == "harness_get_run_team":
                if canonical_team is not None:
                    return canonical_team
                result = self.client.get(path)
                team = self._validate_known_team_response(path, result, binding)
                if canonical_run is None:
                    run_path = f"/runs/{quote(run_id, safe='')}"
                    self._validate_known_run_response(
                        run_path,
                        self.client.get(run_path),
                        binding,
                    )
                return team
            result = self.client.get(path)
            if name == "harness_get_run_detail":
                detail = self._validated_client_response("GET", path, None, result)
                run_path = f"/runs/{quote(run_id, safe='')}"
                current_run = canonical_run or self._validate_known_run_response(
                    run_path,
                    self.client.get(run_path),
                    binding,
                )
                if detail["run"] != current_run or detail["task"]["id"] != binding.task_id:
                    _raise_response_binding_error()
                return detail

            quality = _validate_and_project_http_response(
                _http_response_contract("GET", path),
                path,
                None,
                result,
            )
            if quality["execution_plan_hash"] != binding.execution_plan_hash:
                _raise_response_binding_error()
            run_path = f"/runs/{quote(run_id, safe='')}"
            run = canonical_run or self._validate_known_run_response(
                run_path,
                self.client.get(run_path),
                binding,
            )
            if quality["passed"] and (
                run["status"] != "completed" or run["final_artifact_id"] is None
            ):
                _raise_response_binding_error()
            return quality
        raise ToolInputError("Unknown Harness tool.")

    def _validated_client_get(self, path: str) -> Any:
        return self._validated_client_response("GET", path, None, self.client.get(path))

    def _validated_client_response(
        self,
        method: str,
        path: str,
        payload: Any | None,
        result: Any,
    ) -> Any:
        if type(self.client) is HarnessApiClient:
            return result
        return _validate_and_project_http_response(
            _http_response_contract(method, path),
            path,
            payload,
            result,
        )

    def _get_final_artifact(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_exact_keys(arguments, {"run_id"}, "harness_get_final_artifact arguments")
        run_id = _validated_identifier(arguments.get("run_id"), "run_id", 128)
        binding = self._get_run_binding(run_id)
        if binding is None:
            binding, run, _team = self._rebuild_run_binding(run_id)
        else:
            run_path = f"/runs/{quote(run_id, safe='')}"
            run = self._validate_known_run_response(
                run_path,
                self.client.get(run_path),
                binding,
            )
        final_artifact_id = run["final_artifact_id"]
        if run["status"] != "completed" or final_artifact_id is None:
            raise HarnessApiError("The run does not have an available completed final artifact.")

        artifact_path = f"/artifacts/{quote(final_artifact_id, safe='')}"
        response = self._validated_client_get(artifact_path)
        artifact = response["artifact"]
        if artifact["id"] != final_artifact_id or artifact["run_id"] != run_id:
            _raise_response_binding_error()
        content = response["content"]
        if sha256(content.encode("utf-8")).hexdigest() != artifact["content_hash"]:
            _raise_response_binding_error()
        content_length = len(content)
        truncated = content_length > MAX_FINAL_ARTIFACT_CHARS
        return {
            "trust": "untrusted_artifact_content",
            "type": artifact["type"],
            "content_hash": artifact["content_hash"],
            "content_length": content_length,
            "truncated": truncated,
            "content": content[:MAX_FINAL_ARTIFACT_CHARS],
        }

    def _rebuild_run_binding(
        self,
        run_id: str,
    ) -> tuple[_RunBinding, dict[str, Any], dict[str, Any]]:
        encoded_run_id = quote(run_id, safe="")
        run_path = f"/runs/{encoded_run_id}"
        run = _validate_and_project_http_response(
            _http_response_contract("GET", run_path),
            run_path,
            None,
            self.client.get(run_path),
        )
        team_path = f"{run_path}/team"
        team = _validate_and_project_http_response(
            _http_response_contract("GET", team_path),
            team_path,
            None,
            self.client.get(team_path),
        )
        expected_immutable = run["execution_plan_hash"] is not None
        if (
            team["immutable"] is not expected_immutable
            or team["execution_plan_hash"] != run["execution_plan_hash"]
        ):
            _raise_response_binding_error()
        names, routes = _normalized_real_web_snapshot(run)
        binding = _RunBinding(
            task_id=run["task_id"],
            team_selection=team["team_selection"],
            execution_plan_hash=run["execution_plan_hash"],
            real_model_access_confirmed=run["real_model_access_confirmed"],
            real_web_access_confirmed=run["real_web_access_confirmed"],
            confirmed_real_web_tools=names,
            confirmed_real_web_tool_routes=routes,
        )
        self._store_run_binding(run_id, binding)
        return binding, run, team

    def _get_run_binding(self, run_id: str) -> _RunBinding | None:
        binding = self._run_bindings.get(run_id)
        if binding is not None:
            self._run_bindings.move_to_end(run_id)
        return binding

    def _store_run_binding(self, run_id: str, binding: _RunBinding) -> None:
        self._run_bindings[run_id] = binding
        self._run_bindings.move_to_end(run_id)
        while len(self._run_bindings) > MAX_RUN_BINDINGS:
            self._run_bindings.popitem(last=False)

    def _start_run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = self._validated_run_payload(arguments)
        path = "/runs"
        run = _validate_and_project_http_response(
            _http_response_contract("POST", path),
            path,
            payload,
            self.client.post(path, payload),
        )
        run_id = run["id"]
        if run_id in self._run_bindings:
            _raise_response_binding_error()

        team_path = f"/runs/{quote(run_id, safe='')}/team"
        team = _validate_and_project_http_response(
            _http_response_contract("GET", team_path),
            team_path,
            None,
            self.client.get(team_path),
        )
        request_selection = payload.get("team_selection")
        receipt = team["team_selection"]
        if (
            team["immutable"] is not True
            or team["execution_plan_hash"] != run["execution_plan_hash"]
        ):
            _raise_response_binding_error()
        if request_selection is None:
            if receipt is not None:
                _raise_response_binding_error()
        else:
            if type(receipt) is not dict:
                _raise_response_binding_error()
            _validate_team_receipt_binding(request_selection, receipt)

        confirmed_web_tools, confirmed_web_routes = _normalized_real_web_snapshot(run)
        self._store_run_binding(
            run_id,
            _RunBinding(
                task_id=run["task_id"],
                team_selection=(
                    _project_team_selection(receipt, receipt=True)
                    if receipt is not None
                    else None
                ),
                execution_plan_hash=run["execution_plan_hash"],
                real_model_access_confirmed=run["real_model_access_confirmed"],
                real_web_access_confirmed=run["real_web_access_confirmed"],
                confirmed_real_web_tools=confirmed_web_tools,
                confirmed_real_web_tool_routes=confirmed_web_routes,
            ),
        )
        return run

    def _validate_known_run_response(
        self,
        path: str,
        result: Any,
        binding: _RunBinding,
    ) -> dict[str, Any]:
        run = _validate_and_project_http_response(
            _http_response_contract("GET", path),
            path,
            None,
            result,
        )
        names, routes = _normalized_real_web_snapshot(run)
        if (
            run["task_id"] != binding.task_id
            or run["real_model_access_confirmed"]
            is not binding.real_model_access_confirmed
            or run["real_web_access_confirmed"]
            is not binding.real_web_access_confirmed
            or names != binding.confirmed_real_web_tools
            or routes != binding.confirmed_real_web_tool_routes
            or run["execution_plan_hash"] != binding.execution_plan_hash
        ):
            _raise_response_binding_error()
        return run

    def _validate_known_team_response(
        self,
        path: str,
        result: Any,
        binding: _RunBinding,
    ) -> dict[str, Any]:
        team = _validate_and_project_http_response(
            _http_response_contract("GET", path),
            path,
            None,
            result,
        )
        expected_immutable = binding.execution_plan_hash is not None
        if (
            team["immutable"] is not expected_immutable
            or team["execution_plan_hash"] != binding.execution_plan_hash
            or team["team_selection"] != binding.team_selection
        ):
            _raise_response_binding_error()
        return team

    def _validated_run_payload(self, arguments: dict[str, Any]) -> dict[str, Any]:
        _require_exact_keys(
            arguments,
            {"task_id", "confirm_real_models", "confirm_real_web", "team_selection"},
            "harness_start_run arguments",
        )
        task_id = _validated_identifier(arguments.get("task_id"), "task_id", 128)
        confirm_models = _optional_boolean(arguments, "confirm_real_models", False)
        confirm_web = _optional_boolean(arguments, "confirm_real_web", False)
        if confirm_models and self.environ.get(REAL_MODELS_CAPABILITY_ENV) != "1":
            raise ToolInputError(
                "Real-model confirmation is disabled for this MCP server; enable "
                f"{REAL_MODELS_CAPABILITY_ENV}=1 when registering it."
            )
        if confirm_web and self.environ.get(REAL_WEB_CAPABILITY_ENV) != "1":
            raise ToolInputError(
                "Real-web confirmation is disabled for this MCP server; enable "
                f"{REAL_WEB_CAPABILITY_ENV}=1 when registering it."
            )
        payload: dict[str, Any] = {
            "task_id": task_id,
            "confirm_real_models": confirm_models,
            "confirm_real_web": confirm_web,
            "background": True,
        }
        if "team_selection" in arguments:
            payload["team_selection"] = _validated_team_selection(arguments["team_selection"])
        return payload


class _JsonRpcFailure(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validated_task_payload(arguments: dict[str, Any]) -> dict[str, Any]:
    allowed = {"title", "goal", "workflow_pack", "inputs", "constraints", "acceptance_criteria"}
    _require_exact_keys(arguments, allowed, "harness_create_task arguments")
    for required in ("title", "goal", "workflow_pack"):
        if required not in arguments:
            raise ToolInputError(f"Missing required field: {required}.")
    inputs = arguments.get("inputs", {})
    if not isinstance(inputs, dict):
        raise ToolInputError("inputs must be an object.")
    _validate_json_shape(inputs)
    return {
        "title": _bounded_string(arguments["title"], "title", 500),
        "goal": _bounded_string(arguments["goal"], "goal", 50_000),
        "workflow_pack": _validated_identifier(arguments["workflow_pack"], "workflow_pack", 100),
        "inputs": inputs,
        "constraints": _string_list(arguments.get("constraints", []), "constraints", 64, 5000),
        "acceptance_criteria": _string_list(
            arguments.get("acceptance_criteria", []), "acceptance_criteria", 64, 5000
        ),
        "created_by": "codex_mcp",
    }


def _validated_team_selection(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolInputError("team selection must be an object.")
    _require_exact_keys(value, {"version", "pack_name", "assignments"}, "team selection")
    if value.get("version") != "team-selection-v1":
        raise ToolInputError("team selection version must be team-selection-v1.")
    pack_name = _validated_identifier(value.get("pack_name"), "pack_name", 100)
    assignments = value.get("assignments")
    if not isinstance(assignments, list) or not 1 <= len(assignments) <= 32:
        raise ToolInputError("assignments must contain from 1 to 32 items.")
    validated_assignments = []
    slots: set[str] = set()
    for index, assignment in enumerate(assignments):
        if not isinstance(assignment, dict):
            raise ToolInputError(f"assignments[{index}] must be an object.")
        _require_exact_keys(assignment, {"slot", "role_card_id", "route"}, f"assignments[{index}]")
        if "slot" not in assignment or "route" not in assignment:
            raise ToolInputError(f"assignments[{index}] requires slot and route.")
        slot = _validated_team_assignment_string(
            assignment["slot"], f"assignments[{index}].slot"
        )
        if slot in slots:
            raise ToolInputError("assignment slots must be unique.")
        slots.add(slot)
        item: dict[str, Any] = {"slot": slot, "route": _validated_route(assignment["route"], index)}
        if "role_card_id" in assignment:
            role_card_id = assignment["role_card_id"]
            if role_card_id is not None:
                item["role_card_id"] = _validated_role_card_id(
                    role_card_id, f"assignments[{index}].role_card_id"
                )
        validated_assignments.append(item)
    return {"version": "team-selection-v1", "pack_name": pack_name, "assignments": validated_assignments}


def _validated_route(value: Any, assignment_index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ToolInputError(f"assignments[{assignment_index}].route must be an object.")
    allowed = {
        "family",
        "provider",
        "model",
        "reasoning_effort",
        "fallbacks",
    }
    _require_exact_keys(value, allowed, f"assignments[{assignment_index}].route")
    for required in ("family", "provider", "model"):
        if required not in value:
            raise ToolInputError(f"assignments[{assignment_index}].route requires {required}.")
    family = value["family"]
    provider = value["provider"]
    if family not in {"gpt", "deepseek"}:
        raise ToolInputError("route family must be gpt or deepseek.")
    if provider not in {"openai", "deepseek", "litellm_proxy"}:
        raise ToolInputError("route provider is not allowed.")
    _validate_family_provider_model(family, provider, value["model"], "route")
    route: dict[str, Any] = {
        "family": family,
        "provider": provider,
        "model": _bounded_string(value["model"], "route.model", 200),
    }
    if value.get("reasoning_effort") is not None:
        effort = value["reasoning_effort"]
        if effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ToolInputError("route.reasoning_effort is invalid.")
        route["reasoning_effort"] = effort
    if "fallbacks" in value:
        route["fallbacks"] = _validated_fallbacks(value["fallbacks"])
        targets = {(provider, route["model"])}
        for fallback in route["fallbacks"]:
            target = (fallback["provider"], fallback["model"])
            if target in targets:
                raise ToolInputError("route candidates must use unique provider/model targets.")
            targets.add(target)
    return route


def _validated_fallbacks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 4:
        raise ToolInputError("route.fallbacks must contain at most 4 items.")
    result = []
    targets: set[tuple[str, str]] = set()
    for index, fallback in enumerate(value):
        if not isinstance(fallback, dict):
            raise ToolInputError(f"route.fallbacks[{index}] must be an object.")
        _require_exact_keys(
            fallback,
            {"family", "provider", "model"},
            f"route.fallbacks[{index}]",
        )
        if any(field not in fallback for field in ("family", "provider", "model")):
            raise ToolInputError(f"route.fallbacks[{index}] requires family, provider, and model.")
        family = fallback["family"]
        provider = fallback["provider"]
        if family not in {"gpt", "deepseek"}:
            raise ToolInputError("fallback family must be gpt or deepseek.")
        if provider not in {"openai", "deepseek", "litellm_proxy"}:
            raise ToolInputError("fallback provider is not allowed.")
        model = _bounded_string(fallback["model"], f"route.fallbacks[{index}].model", 200)
        _validate_family_provider_model(family, provider, model, f"route.fallbacks[{index}]")
        target = (provider, model)
        if target in targets:
            raise ToolInputError("fallback provider/model targets must be unique.")
        targets.add(target)
        item: dict[str, Any] = {"family": family, "provider": provider, "model": model}
        result.append(item)
    return result


def _validate_family_provider_model(family: str, provider: str, model: Any, field: str) -> None:
    model_name = _bounded_string(model, f"{field}.model", 200)
    if provider == "openai" and (family != "gpt" or not model_name.startswith("gpt")):
        raise ToolInputError(f"{field} direct openai routes require a gpt family/model.")
    if provider == "deepseek" and (family != "deepseek" or not model_name.startswith("deepseek-")):
        raise ToolInputError(f"{field} direct deepseek routes require a deepseek family/model.")


def _validate_json_shape(value: Any) -> None:
    remaining = [MAX_JSON_ITEMS]

    def visit(item: Any, depth: int) -> None:
        if depth > MAX_JSON_DEPTH:
            raise ToolInputError("inputs nesting is too deep.")
        remaining[0] -= 1
        if remaining[0] < 0:
            raise ToolInputError("inputs contain too many values.")
        if item is None or isinstance(item, (str, bool, int)):
            if isinstance(item, str) and len(item) > 50_000:
                raise ToolInputError("inputs contain an oversized string.")
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ToolInputError("inputs contain a non-finite number.")
            return
        if isinstance(item, dict):
            if len(item) > 128:
                raise ToolInputError("inputs object is too large.")
            for key, child in item.items():
                if not isinstance(key, str) or not key or len(key) > 256:
                    raise ToolInputError("inputs contain an invalid object key.")
                visit(child, depth + 1)
            return
        if isinstance(item, list):
            if len(item) > 128:
                raise ToolInputError("inputs array is too large.")
            for child in item:
                visit(child, depth + 1)
            return
        raise ToolInputError("inputs contain an unsupported JSON value.")

    visit(value, 0)


def _bounded_string(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length or "\x00" in value:
        raise ToolInputError(f"{field} must be a non-empty string of at most {max_length} characters.")
    return value.strip()


def _validated_identifier(value: Any, field: str, max_length: int) -> str:
    text = _bounded_string(value, field, max_length)
    if not _SAFE_ID_RE.fullmatch(text):
        raise ToolInputError(f"{field} contains unsupported characters.")
    return text


def _validated_team_assignment_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ToolInputError(f"{field} must be a non-empty string of at most 200 characters.")
    text = value.strip()
    if not text or len(text) > 200:
        raise ToolInputError(f"{field} must be a non-empty string of at most 200 characters.")
    return text


def _validated_role_card_id(value: Any, field: str) -> str:
    text = _validated_team_assignment_string(value, field)
    if not _ROLE_CARD_ID_RE.fullmatch(text):
        raise ToolInputError(f"{field} contains unsupported characters.")
    return text


def _string_list(value: Any, field: str, max_items: int, max_length: int) -> list[str]:
    if not isinstance(value, list) or len(value) > max_items:
        raise ToolInputError(f"{field} must be an array with at most {max_items} items.")
    return [_bounded_string(item, f"{field}[{index}]", max_length) for index, item in enumerate(value)]


def _bounded_integer(value: Any, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ToolInputError(f"{field} must be an integer in the allowed range.")
    return value


def _required_boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ToolInputError(f"{field} must be boolean.")
    return value


def _optional_boolean(arguments: dict[str, Any], field: str, default: bool) -> bool:
    if field not in arguments:
        return default
    return _required_boolean(arguments[field], field)


def _require_exact_keys(value: dict[str, Any], allowed: set[str], field: str) -> None:
    extras = set(value) - allowed
    if extras:
        raise ToolInputError(f"{field} contains unsupported fields.")


def _validated_request_id(value: Any) -> str | int | None:
    if value is None:
        return None
    if isinstance(value, str) and len(value) <= 128:
        return value
    if type(value) is int and -(2**63) <= value <= 2**63 - 1:
        return value
    raise _JsonRpcFailure(JSONRPC_INVALID_REQUEST, "Invalid JSON-RPC id.")


def _tool_result(payload: Any) -> dict[str, Any]:
    _reject_sensitive_tool_result(payload)
    try:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise HarnessMcpError("Harness API returned an unsupported result.") from exc
    if len(text.encode("utf-8")) > MAX_TOOL_RESULT_BYTES:
        raise HarnessMcpError("Harness tool result exceeded the adapter limit.")
    return {"content": [{"type": "text", "text": text}], "isError": False}


def _reject_sensitive_tool_result(value: Any) -> None:
    stack = [value]
    visited: set[int] = set()
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if _contains_secret_like_text(item):
                raise HarnessMcpError("Harness API returned sensitive-looking data; result was blocked.")
            continue
        if type(item) not in {dict, list}:
            continue
        identity = id(item)
        if identity in visited:
            continue
        visited.add(identity)
        if type(item) is list:
            stack.extend(item)
            continue
        for key, child in item.items():
            if isinstance(key, str) and (
                (
                    _is_sensitive_field_name(key)
                    and not _is_safe_operational_field(key, child)
                )
                or _contains_secret_like_text(key)
            ):
                raise HarnessMcpError("Harness API returned sensitive-looking data; result was blocked.")
            stack.append(child)


def _is_sensitive_field_name(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())
    return any(marker in normalized for marker in _SENSITIVE_FIELD_MARKERS)


def _is_safe_operational_field(name: str, value: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    if normalized in _SAFE_COUNTER_FIELD_NAMES:
        return type(value) is int and value >= 0
    if normalized == "requirescredentials":
        return type(value) is bool
    return False


def _contains_secret_like_text(value: str) -> bool:
    for env_name in ("LITELLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        secret = os.environ.get(env_name)
        if secret and len(secret) >= 4 and secret in value:
            return True
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message[:MAX_ERROR_CHARS]}], "isError": True}


def _jsonrpc_result(request_id: str | int | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: str | int | None, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _redact_error_text(text: str) -> str:
    redacted = str(text)
    for env_name in ("LITELLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        secret = os.environ.get(env_name)
        if secret and len(secret) >= 4:
            redacted = redacted.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        if "PRIVATE KEY" in pattern.pattern:
            redacted = pattern.sub("[REDACTED PRIVATE KEY]", redacted)
        elif "https?" in pattern.pattern:
            redacted = pattern.sub("https://[REDACTED]@", redacted)
        else:
            redacted = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", redacted)
    return redacted[:MAX_ERROR_CHARS]


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _strict_json_loads(text: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object key.")
            result[key] = value
        return result

    return json.loads(
        text,
        object_pairs_hook=object_pairs,
        parse_constant=_reject_json_constant,
    )


def serve(server: McpServer, stdin: TextIO, stdout: TextIO) -> int:
    while True:
        line = stdin.readline(MAX_REQUEST_BYTES + 1)
        if line == "":
            return 0
        if len(line.encode("utf-8", errors="replace")) > MAX_REQUEST_BYTES or not line.endswith("\n"):
            if not line.endswith("\n"):
                while True:
                    remainder = stdin.readline(MAX_REQUEST_BYTES + 1)
                    if remainder == "" or remainder.endswith("\n"):
                        break
            response = _jsonrpc_error(None, JSONRPC_PARSE_ERROR, "JSON-RPC request exceeded the adapter limit.")
        elif not line.strip():
            continue
        else:
            try:
                message = _strict_json_loads(line)
            except (json.JSONDecodeError, ValueError, RecursionError):
                response = _jsonrpc_error(None, JSONRPC_PARSE_ERROR, "Invalid JSON-RPC payload.")
            else:
                response = server.handle_message(message)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
            stdout.flush()


def _configure_real_stdio_utf8(
    stdin: TextIO | None,
    stdout: TextIO | None,
) -> tuple[TextIO, TextIO]:
    input_stream = stdin if stdin is not None else sys.stdin
    output_stream = stdout if stdout is not None else sys.stdout
    for name, stream, write_through in (
        ("stdin", input_stream, False),
        ("stdout", output_stream, True),
        ("stderr", sys.stderr, True),
    ):
        if (name == "stdin" and stdin is not None) or (name == "stdout" and stdout is not None):
            continue
        if name == "stderr" and stdin is not None and stdout is not None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            raise HarnessMcpError(f"Cannot configure MCP {name} as strict UTF-8.")
        options: dict[str, Any] = {
            "encoding": "utf-8",
            "errors": "strict",
            "newline": "\n",
        }
        if write_through:
            options["write_through"] = True
        reconfigure(**options)
    return input_stream, output_stream


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restricted stdio MCP adapter for Team Agent Harness.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("TEAM_AGENT_CODEX_HARNESS_BASE_URL", DEFAULT_BASE_URL),
        help="Loopback Harness API origin (default: http://127.0.0.1:8014).",
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="HTTP timeout (1..120 seconds).")
    return parser


def main(argv: list[str] | None = None, *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        input_stream, output_stream = _configure_real_stdio_utf8(stdin, stdout)
        client = HarnessApiClient(args.base_url, args.timeout)
    except HarnessMcpError as exc:
        print(_redact_error_text(str(exc)), file=sys.stderr)
        return 2
    try:
        return serve(McpServer(client), input_stream, output_stream)
    except UnicodeError:
        print("MCP stdio must contain valid UTF-8.", file=sys.stderr)
        return STDIO_ENCODING_ERROR_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
