from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import ip_address
import json
import os
from time import perf_counter, sleep
from typing import Any, Protocol
from urllib.parse import urlparse


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str


@dataclass(frozen=True)
class ModelRequest:
    provider: str
    model: str
    system_prompt: str
    messages: list[ModelMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    tools_allowed: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReasoningEffortTransport:
    configured_reasoning_effort: str | None
    sent_reasoning_effort: str | None = None
    reasoning_effort_sent: bool = False
    reasoning_effort_ignored: bool = False
    reasoning_effort_mapping: str | None = None
    reasoning_effort_ignore_reason: str | None = None


@dataclass(frozen=True)
class ModelResponse:
    text: str
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0
    finish_reason: str = "stop"
    raw_provider: str = "mock"
    adapter: str = "mock"
    mocked: bool = True


@dataclass(frozen=True)
class ModelProviderInfo:
    name: str
    adapter: str
    enabled: bool
    real_calls: bool
    real_calls_configured: bool
    requires_credentials: bool
    description: str


class ModelAdapter(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse:
        ...


class MockModelAdapter:
    def complete(self, request: ModelRequest) -> ModelResponse:
        started = perf_counter()
        step_name = str(request.metadata.get("step_name", "step"))
        agent_role = str(request.metadata.get("agent_role", "Agent"))
        task_title = str(request.metadata.get("task_title", "Task"))
        text = (
            f"{agent_role} completed {step_name}.\n\n"
            f"Task: {task_title}\n"
            f"Provider: {request.provider}\n"
            f"Model: {request.model}\n"
        )
        elapsed_ms = max(1, int((perf_counter() - started) * 1000))
        return ModelResponse(
            text=text,
            usage={
                "input_tokens": _estimate_tokens(request.system_prompt)
                + sum(_estimate_tokens(message.content) for message in request.messages),
                "output_tokens": _estimate_tokens(text),
            },
            latency_ms=elapsed_ms,
            finish_reason="stop",
            raw_provider=request.provider,
            adapter="mock",
            mocked=True,
        )


class ProviderStubAdapter:
    def __init__(self, provider: str) -> None:
        self.provider = provider

    def complete(self, request: ModelRequest) -> ModelResponse:
        raise ModelRuntimeError(
            f"Model provider skeleton is not enabled: {self.provider}. "
            "Real provider calls are not implemented in this phase."
        )


class OpenAICompatibleModelAdapter:
    def __init__(
        self,
        *,
        provider: str,
        api_key_env: str,
        base_url: str | Callable[[], str] | None = None,
        client: Any | None = None,
        allow_real_calls_env: str = "TEAM_AGENT_ALLOW_REAL_MODEL_CALLS",
        endpoint: str = "responses",
        max_attempts: int = 3,
        retry_delay_seconds: float = 0.75,
        timeout_seconds: float | None = None,
    ) -> None:
        self.provider = provider
        self.api_key_env = api_key_env
        self.base_url = base_url
        self._client = client
        self.allow_real_calls_env = allow_real_calls_env
        self.endpoint = endpoint
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self.timeout_seconds = timeout_seconds

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not _real_model_calls_allowed(self.allow_real_calls_env):
            raise ModelRuntimeError(
                f"Real model calls are disabled for provider: {self.provider}. "
                f"Set {self.allow_real_calls_env}=1 to enable explicit real calls."
            )
        started = perf_counter()
        client = self._client or self._build_client()
        response = self._complete_with_retries(client, request, started)

        elapsed_ms = max(1, int((perf_counter() - started) * 1000))
        adapter = "openai_compatible_chat" if self.endpoint == "chat_completions" else "openai_compatible"
        try:
            _validate_openai_response_shape(response)
            finish_reason = _response_finish_reason(response)
            text = _response_text(response)
            usage = _response_usage(response)
        except ModelRuntimeError as exc:
            raise ModelRuntimeError(
                str(exc),
                provider=self.provider,
                model=request.model,
                adapter=adapter,
                error_class=exc.error_class or "InvalidModelResponse",
                error_summary=exc.error_summary,
                elapsed_ms=elapsed_ms,
            ) from exc
        return ModelResponse(
            text=text,
            usage=usage,
            latency_ms=elapsed_ms,
            finish_reason=finish_reason,
            raw_provider=self.provider,
            adapter=adapter,
            mocked=False,
        )

    def _build_client(self) -> Any:
        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise ModelRuntimeError(
                f"Model provider is not enabled: {self.provider}. "
                f"Set {self.api_key_env} to enable real calls."
            )
        base_url = self._resolved_base_url()
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ModelRuntimeError("OpenAI Python SDK is not installed.") from exc

        # Keep a single retry owner so the configured timeout is not multiplied by SDK retries.
        kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": 0}
        if base_url is not None:
            kwargs["base_url"] = base_url
        timeout_seconds = self._resolved_timeout_seconds()
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        return OpenAI(**kwargs)

    def _resolved_base_url(self) -> str | None:
        if callable(self.base_url):
            return self.base_url()
        return self.base_url

    def _complete_with_retries(self, client: Any, request: ModelRequest, started: float) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._complete_once(client, request)
            except Exception as exc:
                last_exc = exc
                if attempt >= self.max_attempts or not _is_transient_model_error(exc):
                    break
                if self.retry_delay_seconds:
                    sleep(self.retry_delay_seconds)
        elapsed_ms = max(1, int((perf_counter() - started) * 1000))
        raise ModelRuntimeError(
            f"{self.provider} model call failed. See server logs for provider details.",
            provider=self.provider,
            model=request.model,
            adapter="openai_compatible_chat" if self.endpoint == "chat_completions" else "openai_compatible",
            error_class=last_exc.__class__.__name__ if last_exc is not None else None,
            error_summary=_safe_provider_error_summary(last_exc),
            elapsed_ms=elapsed_ms,
        ) from last_exc

    def _complete_once(self, client: Any, request: ModelRequest) -> Any:
        request_options = self._request_options(request)
        if self.endpoint == "chat_completions":
            return client.chat.completions.create(
                model=request.model,
                messages=[
                    {"role": "system", "content": request.system_prompt},
                    *[
                        {"role": message.role, "content": message.content}
                        for message in request.messages
                    ],
                ],
                **request_options,
            )
        return client.responses.create(
            model=request.model,
            input=[
                {"role": "system", "content": request.system_prompt},
                *[
                    {"role": message.role, "content": message.content}
                        for message in request.messages
                    ],
                ],
            **request_options,
        )

    def _request_options(self, request: ModelRequest) -> dict[str, Any]:
        options = _chat_request_options(request) if self.endpoint == "chat_completions" else _request_options(request)
        timeout_seconds = self._resolved_timeout_seconds()
        if timeout_seconds is not None:
            options["timeout"] = timeout_seconds
            if request.provider == "litellm_proxy":
                headers = dict(options.get("extra_headers") or {})
                headers["x-litellm-timeout"] = _format_timeout_header(timeout_seconds)
                options["extra_headers"] = headers
        return options

    def _resolved_timeout_seconds(self) -> float | None:
        if self.timeout_seconds is not None:
            if self.timeout_seconds <= 0:
                raise ModelRuntimeError("TEAM_AGENT_MODEL_TIMEOUT_SECONDS must be a positive number.")
            return self.timeout_seconds
        raw = os.environ.get("TEAM_AGENT_MODEL_TIMEOUT_SECONDS")
        if raw is None or not raw.strip():
            return 180.0
        try:
            timeout = float(raw)
        except ValueError as exc:
            raise ModelRuntimeError("TEAM_AGENT_MODEL_TIMEOUT_SECONDS must be a positive number.") from exc
        if timeout <= 0:
            raise ModelRuntimeError("TEAM_AGENT_MODEL_TIMEOUT_SECONDS must be a positive number.")
        return timeout


class ModelGateway:
    def __init__(self, adapters: dict[str, ModelAdapter] | None = None) -> None:
        self.adapters = adapters or default_model_adapters()

    def complete(self, request: ModelRequest) -> ModelResponse:
        adapter = self.adapters.get(request.provider)
        if adapter is None:
            raise ModelRuntimeError(f"Model provider not configured: {request.provider}")
        response = adapter.complete(request)
        return _validate_model_response(response, provider=request.provider, model=request.model)


class ModelRuntimeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        adapter: str | None = None,
        error_class: str | None = None,
        error_summary: str | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.adapter = adapter
        self.error_class = error_class
        self.error_summary = _normalize_model_error_summary(error_summary)
        self.elapsed_ms = elapsed_ms


def model_runtime_error_payload(exc: Exception) -> dict[str, Any]:
    payload = {
        "provider": getattr(exc, "provider", None),
        "model": getattr(exc, "model", None),
        "adapter": getattr(exc, "adapter", None),
        "error_class": getattr(exc, "error_class", None),
        "error_summary": getattr(exc, "error_summary", None),
        "elapsed_ms": getattr(exc, "elapsed_ms", None),
    }
    if payload["error_summary"] is not None:
        payload["error_summary"] = _normalize_model_error_summary(str(payload["error_summary"]))
    return {key: value for key, value in payload.items() if value is not None}


REAL_MODEL_PROVIDER_API_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "litellm_proxy": "LITELLM_API_KEY",
}


REAL_MODEL_PROVIDERS = set(REAL_MODEL_PROVIDER_API_KEY_ENVS)


ROUTABLE_MODEL_PROVIDERS = {"mock", *REAL_MODEL_PROVIDERS}


DEFAULT_REAL_MODEL_REASONING_EFFORT = "xhigh"


def default_reasoning_effort_for_model(provider: str, model: str) -> str | None:
    if provider in REAL_MODEL_PROVIDERS:
        return DEFAULT_REAL_MODEL_REASONING_EFFORT
    return None


def default_model_adapters() -> dict[str, ModelAdapter]:
    return {
        "mock": MockModelAdapter(),
        "openai": OpenAICompatibleModelAdapter(provider="openai", api_key_env="OPENAI_API_KEY"),
        "anthropic": ProviderStubAdapter("anthropic"),
        "deepseek": OpenAICompatibleModelAdapter(
            provider="deepseek",
            api_key_env="DEEPSEEK_API_KEY",
            base_url="https://api.deepseek.com",
            endpoint="chat_completions",
        ),
        "litellm_proxy": OpenAICompatibleModelAdapter(
            provider="litellm_proxy",
            api_key_env="LITELLM_API_KEY",
            base_url=_litellm_base_url,
            endpoint="chat_completions",
            max_attempts=_positive_int_env("TEAM_AGENT_LITELLM_PROXY_MAX_ATTEMPTS", 1),
        ),
        "local": ProviderStubAdapter("local"),
    }


def model_provider_catalog() -> list[ModelProviderInfo]:
    return [
        ModelProviderInfo(
            name="mock",
            adapter="mock",
            enabled=True,
            real_calls=False,
            real_calls_configured=False,
            requires_credentials=False,
            description="Deterministic mocked adapter used for local development and tests.",
        ),
        ModelProviderInfo(
            name="openai",
            adapter="openai_compatible",
            enabled=_real_model_calls_allowed() and bool(os.environ.get("OPENAI_API_KEY")),
            real_calls=True,
            real_calls_configured=bool(os.environ.get("OPENAI_API_KEY")),
            requires_credentials=True,
            description=(
                "OpenAI adapter is available only when OPENAI_API_KEY is set, "
                "an agent explicitly opts in, and real calls are explicitly enabled."
            ),
        ),
        ModelProviderInfo(
            name="anthropic",
            adapter="provider_stub",
            enabled=False,
            real_calls=False,
            real_calls_configured=False,
            requires_credentials=True,
            description="Anthropic adapter skeleton only; API keys and real calls are not wired.",
        ),
        ModelProviderInfo(
            name="deepseek",
            adapter="openai_compatible_chat",
            enabled=_real_model_calls_allowed() and bool(os.environ.get("DEEPSEEK_API_KEY")),
            real_calls=True,
            real_calls_configured=bool(os.environ.get("DEEPSEEK_API_KEY")),
            requires_credentials=True,
            description="DeepSeek adapter uses the OpenAI-compatible API only when DEEPSEEK_API_KEY is set and an agent explicitly opts in.",
        ),
        ModelProviderInfo(
            name="litellm_proxy",
            adapter="openai_compatible_chat",
            enabled=_real_model_calls_allowed() and bool(os.environ.get("LITELLM_API_KEY")),
            real_calls=True,
            real_calls_configured=bool(os.environ.get("LITELLM_API_KEY")),
            requires_credentials=True,
            description=(
                "LiteLLM Proxy adapter uses an OpenAI-compatible gateway at LITELLM_BASE_URL "
                "and requires LITELLM_API_KEY plus explicit real-call opt-in."
            ),
        ),
        ModelProviderInfo(
            name="local",
            adapter="provider_stub",
            enabled=False,
            real_calls=False,
            real_calls_configured=False,
            requires_credentials=False,
            description="Local model adapter skeleton only; local endpoints are not wired.",
        ),
    ]


def model_request_from_agent(
    *,
    task_title: str,
    task_goal: str,
    step_name: str,
    agent_id: str,
    agent_role: str,
    system_prompt: str,
    model_config: dict[str, Any],
    allowed_tools: list[str],
    context: dict[str, Any],
) -> ModelRequest:
    provider = str(model_config.get("provider", "mock"))
    model = str(model_config.get("model", "mock-model"))
    reasoning_effort = _optional_str(model_config.get("reasoning_effort")) or default_reasoning_effort_for_model(
        provider,
        model,
    )
    return ModelRequest(
        provider=provider,
        model=model,
        system_prompt=system_prompt,
        messages=[
            ModelMessage(role="user", content=f"Task: {task_title}\nGoal: {task_goal}"),
            ModelMessage(role="user", content=f"Step: {step_name}\nAgent: {agent_role}"),
            ModelMessage(role="user", content=context_message_from_envelope(context)),
        ],
        temperature=_optional_float(model_config.get("temperature")),
        max_tokens=_optional_int(model_config.get("max_tokens")),
        reasoning_effort=reasoning_effort,
        tools_allowed=allowed_tools,
        metadata={
            "task_title": task_title,
            "step_name": step_name,
            "agent_id": agent_id,
            "agent_run_id": context.get("agent_run_id"),
            "agent_role": agent_role,
            "context_keys": sorted(context.keys()),
        },
    )


def context_message_from_envelope(context: dict[str, Any]) -> str:
    return "Context envelope:\n" + json.dumps(
        _model_context_payload(context),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _model_context_payload(context: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "task_objective",
        "step_objective",
        "allowed_tools",
        "required_inputs",
        "required_artifacts",
        "previous_agent_run_id",
        "previous_handoff",
        "depends_on",
        "phase",
        "coordination_context",
        "handoff_summary",
        "state_breadcrumb",
        "gate_context",
        "artifact_refs",
        "artifact_excerpts",
        "research_tool_evidence",
        "context_manifest",
    ]
    return {key: context.get(key) for key in keys if key in context}


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ModelRuntimeError(f"{name} must be a positive integer.") from exc
    if value <= 0:
        raise ModelRuntimeError(f"{name} must be a positive integer.")
    return value


def _estimate_tokens(value: str) -> int:
    return max(1, len(value.split()))


def _format_timeout_header(value: float) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)


def _request_options(request: ModelRequest) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if request.temperature is not None and _supports_temperature(request):
        options["temperature"] = request.temperature
    if request.max_tokens is not None:
        options["max_output_tokens"] = request.max_tokens
    transport = reasoning_effort_transport(request)
    if transport.reasoning_effort_sent and transport.sent_reasoning_effort is not None:
        options["reasoning"] = {"effort": transport.sent_reasoning_effort}
    return options


def _chat_request_options(request: ModelRequest) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if request.temperature is not None and _supports_temperature(request):
        options["temperature"] = request.temperature
    if request.max_tokens is not None:
        options["max_tokens"] = request.max_tokens
    transport = reasoning_effort_transport(request)
    if transport.reasoning_effort_sent and transport.sent_reasoning_effort is not None:
        options["reasoning_effort"] = transport.sent_reasoning_effort
    return options


def reasoning_effort_transport(request: ModelRequest) -> ReasoningEffortTransport:
    configured = request.reasoning_effort or default_reasoning_effort_for_model(request.provider, request.model)
    if configured is None:
        return ReasoningEffortTransport(configured_reasoning_effort=None)

    if _supports_xhigh_reasoning_effort(request):
        return ReasoningEffortTransport(
            configured_reasoning_effort=configured,
            sent_reasoning_effort=configured,
            reasoning_effort_sent=True,
        )
    if _supports_openai_reasoning_effort(request):
        sent = "high" if configured == "xhigh" else configured
        return ReasoningEffortTransport(
            configured_reasoning_effort=configured,
            sent_reasoning_effort=sent,
            reasoning_effort_sent=True,
            reasoning_effort_mapping=f"{configured}->{sent}" if sent != configured else None,
        )
    return ReasoningEffortTransport(
        configured_reasoning_effort=configured,
        reasoning_effort_sent=False,
        reasoning_effort_ignored=True,
        reasoning_effort_ignore_reason="provider_or_model_not_known_to_support_reasoning_effort",
    )


def _supports_temperature(request: ModelRequest) -> bool:
    if request.provider == "litellm_proxy" and request.model.lower() == "gpt5.5":
        return False
    return True


def reasoning_effort_trace_payload(request: ModelRequest) -> dict[str, Any]:
    transport = reasoning_effort_transport(request)
    if transport.configured_reasoning_effort is None:
        return {}
    payload = {
        "reasoning_effort": transport.configured_reasoning_effort,
        "configured_reasoning_effort": transport.configured_reasoning_effort,
        "sent_reasoning_effort": transport.sent_reasoning_effort,
        "reasoning_effort_sent": transport.reasoning_effort_sent,
        "reasoning_effort_ignored": transport.reasoning_effort_ignored,
        "reasoning_effort_mapping": transport.reasoning_effort_mapping,
        "reasoning_effort_ignore_reason": transport.reasoning_effort_ignore_reason,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _supports_xhigh_reasoning_effort(request: ModelRequest) -> bool:
    if request.provider != "litellm_proxy":
        return False
    if request.model.lower() != "gpt5.5":
        return False
    return os.environ.get("TEAM_AGENT_LITELLM_XHIGH_REASONING_PASSTHROUGH") == "1"


def _supports_openai_reasoning_effort(request: ModelRequest) -> bool:
    if request.provider == "openai":
        return _is_known_openai_reasoning_model(request.model)
    if request.provider == "litellm_proxy":
        return request.model.lower() == "gpt5.5"
    return False


def _is_known_openai_reasoning_model(model: str) -> bool:
    normalized = model.lower().strip()
    return (
        normalized == "gpt5.5"
        or normalized.startswith("gpt-5")
        or normalized.startswith("o1")
        or normalized.startswith("o3")
        or normalized.startswith("o4")
    )


_MISSING_RESPONSE_FIELD = object()
_COMPLETE_RESPONSE_STATES = frozenset({"stop", "completed"})
_KNOWN_INCOMPLETE_RESPONSE_STATES = frozenset(
    {"length", "content_filter", "failed", "incomplete", "tool_calls", "function_call", "cancelled", "canceled"}
)
_MODEL_RESPONSE_USAGE_KEYS = ("input_tokens", "output_tokens", "total_tokens")
_MODEL_RESPONSE_IDENTIFIER_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)
_MAX_MODEL_RESPONSE_IDENTIFIER_LENGTH = 128
_MAX_MODEL_RESPONSE_COUNTER = (1 << 63) - 1


def _validate_model_response(response: Any, *, provider: str, model: str) -> ModelResponse:
    if not isinstance(response, ModelResponse):
        raise ModelRuntimeError(
            "Model adapter returned an invalid response.",
            provider=provider,
            model=model,
            error_class="InvalidModelResponse",
            error_summary="response_type=invalid",
        )
    try:
        usage = _validated_model_response_usage(response.usage)
        latency_ms = _validated_model_response_counter(response.latency_ms)
        raw_provider = _validated_model_response_identifier(response.raw_provider)
        adapter = _validated_model_response_identifier(response.adapter)
        if raw_provider != provider or type(response.mocked) is not bool:
            _raise_invalid_model_response_metadata()
    except Exception:
        raise ModelRuntimeError(
            "Model adapter returned invalid response metadata.",
            provider=provider,
            model=model,
            error_class="InvalidModelResponse",
            error_summary="response_type=invalid",
        ) from None
    try:
        finish_reason = _validated_finish_reason(response.finish_reason)
        text = _validated_response_text(response.text)
    except ModelRuntimeError as exc:
        raise ModelRuntimeError(
            str(exc),
            provider=provider,
            model=model,
            error_class=exc.error_class or "InvalidModelResponse",
            error_summary=exc.error_summary,
            elapsed_ms=latency_ms,
        ) from exc
    return ModelResponse(
        text=text,
        usage=usage,
        latency_ms=latency_ms,
        finish_reason=finish_reason,
        raw_provider=raw_provider,
        adapter=adapter,
        mocked=response.mocked,
    )


def _validated_model_response_usage(value: Any) -> dict[str, int]:
    if type(value) is not dict:
        _raise_invalid_model_response_metadata()
    if any(type(key) is not str or key not in _MODEL_RESPONSE_USAGE_KEYS for key in value):
        _raise_invalid_model_response_metadata()
    return {
        key: _validated_model_response_counter(value[key])
        for key in _MODEL_RESPONSE_USAGE_KEYS
        if key in value
    }


def _validated_model_response_counter(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_MODEL_RESPONSE_COUNTER:
        _raise_invalid_model_response_metadata()
    return value


def _validated_model_response_identifier(value: Any) -> str:
    if type(value) is not str:
        _raise_invalid_model_response_metadata()
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_MODEL_RESPONSE_IDENTIFIER_LENGTH
        or any(character not in _MODEL_RESPONSE_IDENTIFIER_CHARS for character in normalized)
    ):
        _raise_invalid_model_response_metadata()
    return normalized


def _raise_invalid_model_response_metadata() -> None:
    raise ModelRuntimeError(
        "Model adapter returned invalid response metadata.",
        error_class="InvalidModelResponse",
        error_summary="response_type=invalid",
    ) from None


def _validate_openai_response_shape(response: Any) -> None:
    choices = getattr(response, "choices", None) or []
    for choice in choices:
        message = getattr(choice, "message", None)
        if getattr(message, "tool_calls", None) or getattr(message, "function_call", None):
            _raise_unsupported_response_shape("tool_call")
        if getattr(message, "refusal", None) is not None:
            _raise_unsupported_response_shape("refusal")

    output = getattr(response, "output", None) or []
    for item in output:
        item_type = _normalized_response_item_type(item)
        if _is_tool_call_item_type(item_type):
            _raise_unsupported_response_shape("tool_call")
        if item_type == "refusal" or getattr(item, "refusal", None) is not None:
            _raise_unsupported_response_shape("refusal")
        for content in getattr(item, "content", None) or []:
            content_type = _normalized_response_item_type(content)
            if _is_tool_call_item_type(content_type):
                _raise_unsupported_response_shape("tool_call")
            if content_type == "refusal" or getattr(content, "refusal", None) is not None:
                _raise_unsupported_response_shape("refusal")


def _normalized_response_item_type(item: Any) -> str | None:
    item_type = getattr(item, "type", None)
    if not isinstance(item_type, str):
        return None
    return item_type.strip().lower()


def _is_tool_call_item_type(item_type: str | None) -> bool:
    return item_type is not None and (item_type == "function_call" or item_type.endswith("_call"))


def _raise_unsupported_response_shape(shape: str) -> None:
    raise ModelRuntimeError(
        "Model response included unsupported tool or refusal content.",
        error_class="InvalidModelResponse",
        error_summary=f"response_shape={shape}",
    )


def _response_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    for choice in choices:
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        return _validated_response_text(content)
    output_text = getattr(response, "output_text", _MISSING_RESPONSE_FIELD)
    if output_text is not _MISSING_RESPONSE_FIELD:
        return _validated_response_text(output_text)
    output = getattr(response, "output", None) or []
    parts: list[str] = []
    for item in output:
        for content in getattr(item, "content", []) or []:
            text = getattr(content, "text", _MISSING_RESPONSE_FIELD)
            if text is _MISSING_RESPONSE_FIELD:
                continue
            if not isinstance(text, str):
                raise ModelRuntimeError(
                    "Model response did not contain non-empty text.",
                    error_class="InvalidModelResponse",
                    error_summary="response_text_type=non_string",
                )
            parts.append(text)
    if parts:
        return _validated_response_text("\n".join(parts))
    raise ModelRuntimeError(
        "Model response did not contain non-empty text.",
        error_class="InvalidModelResponse",
        error_summary="response_text=missing",
    )


def _validated_response_text(value: Any) -> str:
    if not isinstance(value, str):
        raise ModelRuntimeError(
            "Model response did not contain non-empty text.",
            error_class="InvalidModelResponse",
            error_summary="response_text_type=non_string",
        )
    if not value.strip():
        raise ModelRuntimeError(
            "Model response did not contain non-empty text.",
            error_class="InvalidModelResponse",
            error_summary="response_text=empty",
        )
    return value


def _response_usage(response: Any) -> dict[str, int]:
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        prompt_tokens = _response_usage_field(usage, "prompt_tokens")
        completion_tokens = _response_usage_field(usage, "completion_tokens")
        total_tokens = _response_usage_field(usage, "total_tokens")
        if prompt_tokens is not _MISSING_RESPONSE_FIELD or completion_tokens is not _MISSING_RESPONSE_FIELD:
            raw_usage = {
                "input_tokens": _response_usage_counter(prompt_tokens),
                "output_tokens": _response_usage_counter(completion_tokens),
                "total_tokens": _response_usage_counter(total_tokens),
            }
        else:
            input_tokens = _response_usage_field(usage, "input_tokens")
            output_tokens = _response_usage_field(usage, "output_tokens")
            if (
                input_tokens is _MISSING_RESPONSE_FIELD
                and output_tokens is _MISSING_RESPONSE_FIELD
                and total_tokens is _MISSING_RESPONSE_FIELD
            ):
                _raise_invalid_model_response_metadata()
            raw_usage = {
                "input_tokens": _response_usage_counter(input_tokens),
                "output_tokens": _response_usage_counter(output_tokens),
                "total_tokens": _response_usage_counter(total_tokens),
            }
        return _validated_model_response_usage(raw_usage)
    except Exception:
        _raise_invalid_model_response_metadata()


def _response_usage_field(usage: Any, name: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(name, _MISSING_RESPONSE_FIELD)
    return getattr(usage, name, _MISSING_RESPONSE_FIELD)


def _response_usage_counter(value: Any) -> int:
    if value is _MISSING_RESPONSE_FIELD or value is None:
        return 0
    return _validated_model_response_counter(value)


def _response_finish_reason(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    for choice in choices:
        return _validated_finish_reason(getattr(choice, "finish_reason", None))
    return _validated_finish_reason(getattr(response, "status", None))


def _validated_finish_reason(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        summary = (
            "finish_reason=missing"
            if value is None or (isinstance(value, str) and not value.strip())
            else "finish_reason_type=non_string"
        )
        raise ModelRuntimeError(
            "Model response did not include a valid completion status.",
            error_class="InvalidModelResponse",
            error_summary=summary,
        )
    finish_reason = value.strip().lower()
    if finish_reason not in _COMPLETE_RESPONSE_STATES:
        summary = (
            f"finish_reason={finish_reason}"
            if finish_reason in _KNOWN_INCOMPLETE_RESPONSE_STATES
            else "finish_reason=unsupported"
        )
        raise ModelRuntimeError(
            "Model response did not complete successfully.",
            error_class="IncompleteModelResponse",
            error_summary=summary,
        )
    return finish_reason


def _is_transient_model_error(exc: Exception) -> bool:
    text = str(exc).lower()
    transient_markers = (
        "connection error",
        "connection reset",
        "connection aborted",
        "timeout",
        "timed out",
        "temporarily unavailable",
        "service unavailable",
        "internalservererror",
        "internal server error",
        "bad gateway",
        "gateway timeout",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
        "status code: 500",
        "status code: 502",
        "status code: 503",
        "status code: 504",
    )
    non_transient_markers = (
        "401",
        "403",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "invalid_api_key",
        "authentication",
        "permission denied",
        "insufficient quota",
        "billing",
    )
    return any(marker in text for marker in transient_markers) and not any(
        marker in text for marker in non_transient_markers
    )


def _safe_provider_error_summary(exc: Exception | None) -> str | None:
    if exc is None:
        return None
    status_code = _provider_error_status_code(exc)
    classification = _provider_error_classification(exc, status_code=status_code)
    parts = [f"classification={classification}"]
    if status_code is not None:
        parts.append(f"status_code={status_code}")
    parts.append(f"retryable={'true' if _is_transient_model_error(exc) else 'false'}")
    return ";".join(parts)


def _provider_error_status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    for value in (getattr(exc, "status_code", None), getattr(response, "status_code", None)):
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    return None


def _provider_error_classification(exc: Exception, *, status_code: int | None) -> str:
    class_name = exc.__class__.__name__.lower()
    if status_code in {401, 403}:
        return "authentication_or_permission_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code in {408, 504} or "timeout" in class_name:
        return "timeout_error"
    if status_code is not None and status_code >= 500:
        return "upstream_service_error"
    if "connection" in class_name:
        return "connection_error"
    if _is_transient_model_error(exc):
        return "transient_provider_error"
    return "provider_error"


_MODEL_ERROR_SUMMARY_ALLOWED_VALUES = {
    "classification": frozenset(
        {
            "authentication_or_permission_error",
            "rate_limit_error",
            "timeout_error",
            "upstream_service_error",
            "connection_error",
            "transient_provider_error",
            "provider_error",
            "unclassified_model_runtime_error",
        }
    ),
    "retryable": frozenset({"true", "false"}),
    "response_type": frozenset({"invalid"}),
    "response_shape": frozenset({"tool_call", "refusal"}),
    "response_text_type": frozenset({"non_string"}),
    "response_text": frozenset({"missing", "empty"}),
    "finish_reason": _KNOWN_INCOMPLETE_RESPONSE_STATES | frozenset({"missing", "unsupported"}),
    "finish_reason_type": frozenset({"non_string"}),
}
_MODEL_ERROR_SUMMARY_RESPONSE_KEYS = frozenset(
    {
        "response_type",
        "response_shape",
        "response_text_type",
        "response_text",
        "finish_reason",
        "finish_reason_type",
    }
)
_UNCLASSIFIED_MODEL_ERROR_SUMMARY = "classification=unclassified_model_runtime_error"


def _normalize_model_error_summary(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or len(value) > 500:
        return _UNCLASSIFIED_MODEL_ERROR_SUMMARY
    parsed: dict[str, str] = {}
    for part in value.split(";"):
        key, separator, item = part.partition("=")
        if separator != "=" or key in parsed:
            return _UNCLASSIFIED_MODEL_ERROR_SUMMARY
        if key == "status_code":
            if not item.isdigit() or not 100 <= int(item) <= 599:
                return _UNCLASSIFIED_MODEL_ERROR_SUMMARY
        elif item not in _MODEL_ERROR_SUMMARY_ALLOWED_VALUES.get(key, frozenset()):
            return _UNCLASSIFIED_MODEL_ERROR_SUMMARY
        parsed[key] = item
    keys = frozenset(parsed)
    if "classification" in keys:
        if not keys <= {"classification", "status_code", "retryable"}:
            return _UNCLASSIFIED_MODEL_ERROR_SUMMARY
    elif len(keys) != 1 or not keys <= _MODEL_ERROR_SUMMARY_RESPONSE_KEYS:
        return _UNCLASSIFIED_MODEL_ERROR_SUMMARY
    return value


def _real_model_calls_allowed(env_name: str = "TEAM_AGENT_ALLOW_REAL_MODEL_CALLS") -> bool:
    return os.environ.get(env_name) == "1"


def _litellm_base_url() -> str:
    value = os.environ.get("LITELLM_BASE_URL", "http://127.0.0.1:4000/v1").strip()
    parsed = urlparse(value)
    if not value or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelRuntimeError("LITELLM_BASE_URL must be an http(s) URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelRuntimeError("LITELLM_BASE_URL must not include credentials, query, or fragment.")
    if not _is_loopback_host(parsed.hostname or ""):
        if os.environ.get("TEAM_AGENT_ALLOW_REMOTE_LITELLM_PROXY") != "1":
            raise ModelRuntimeError(
                "Remote LiteLLM Proxy URLs are disabled. "
                "Use a loopback URL or set TEAM_AGENT_ALLOW_REMOTE_LITELLM_PROXY=1 for a trusted HTTPS proxy."
            )
        if parsed.scheme != "https":
            raise ModelRuntimeError("Remote LiteLLM Proxy URLs must use https.")
    return value


def _is_loopback_host(hostname: str) -> bool:
    if hostname.lower() in {"localhost"}:
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False
