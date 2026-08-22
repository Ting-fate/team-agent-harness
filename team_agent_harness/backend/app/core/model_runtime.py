from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
import base64
import binascii
import html
from ipaddress import ip_address
import json
from math import isfinite
import os
import re
from time import perf_counter, sleep
from threading import BoundedSemaphore, Lock
from typing import Any, Protocol
from urllib.parse import urlparse

from app.core.model_capabilities import (
    CapabilityError,
    CapabilityRegistry,
    load_capability_registry,
)
from app.core.provider_health import ProviderHealthRegistry
from app.core.route_policy import (
    RoutePolicyError,
    RouteRequirements,
    explain_route,
    route_candidates_from_request,
)
from app.core.sensitive_text import contains_secret_like_text


_MAX_MODEL_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_MODEL_DATA_URI_BYTES = 16 * 1024 * 1024
_DEEPSEEK_MIN_RUN_OUTPUT_TOKENS = 8000
_INVALID_RESPONSE_RETRY_LIMIT = 1
_DSML_MARKER = "\uff5c\uff5cDSML\uff5c\uff5c"
_DSML_CONTENT_PARAMETER_RE = re.compile(
    rf"<{re.escape(_DSML_MARKER)}parameter name=\"content\"[^>]*>"
    rf"(?P<content>.*?)</{re.escape(_DSML_MARKER)}parameter>",
    re.DOTALL,
)
_DATA_URI_RE = re.compile(
    r"^data:(image/(?:jpeg|png|gif|webp));base64,([A-Za-z0-9+/]+={0,2})$"
)
_LOCAL_ROUTE_REJECTION_REASONS = {
    "capability_mismatch",
    "invalid_request",
    "provider_capacity_timeout",
    "provider_not_configured",
    "provider_not_ready",
    "provider_attempt_record_failed",
    "route_timeout_budget_exhausted",
}
_LOCAL_ROUTE_REJECTION_TOKEN = object()


@dataclass(frozen=True)
class ModelToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelMessage:
    role: str
    content: str | list[dict[str, Any]] = ""
    tool_call_id: str | None = None
    tool_calls: list[ModelToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class ModelRequest:
    provider: str
    model: str
    system_prompt: str
    messages: list[ModelMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    max_continuations: int = 0
    reasoning_effort: str | None = None
    timeout_seconds: float | None = None
    tools_allowed: list[str] = field(default_factory=list)
    tools: list[ModelToolDefinition] = field(default_factory=list)
    fallbacks: list[dict[str, Any]] = field(default_factory=list)
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    provider_attempt_recorder: Callable[[dict[str, Any]], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )


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
    tool_calls: list[ModelToolCall] = field(default_factory=list)
    route_receipt: list[dict[str, Any]] = field(default_factory=list)
    provider_attempts: list[dict[str, Any]] = field(default_factory=list)
    partial: bool = False
    continuation_count: int = 0
    selected_provider: str | None = None
    selected_model: str | None = None
    selected_input_usd_per_million: float | None = None
    selected_output_usd_per_million: float | None = None


@dataclass(frozen=True)
class ModelRoutePrice:
    provider: str
    model: str
    input_usd_per_million: float
    output_usd_per_million: float
    source: str


@dataclass(frozen=True)
class ModelInteraction:
    request: ModelRequest
    response: ModelResponse


@dataclass(frozen=True)
class ModelProviderInfo:
    name: str
    adapter: str
    enabled: bool
    real_calls: bool
    real_calls_configured: bool
    requires_credentials: bool
    description: str
    protocol: str = "unknown"


class ModelAdapter(Protocol):
    def complete(self, request: ModelRequest) -> ModelResponse:
        ...


class MockModelAdapter:
    def complete(self, request: ModelRequest) -> ModelResponse:
        if "vision" in _required_model_capabilities(request) or _request_contains_image_block(request):
            raise ModelRuntimeError(
                "The mock provider cannot inspect image input; use a confirmed vision-capable provider.",
                provider=request.provider,
                model=request.model,
                error_class="MockVisionUnsupported",
                error_summary="classification=capability_error;retryable=false",
            )
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
            selected_provider=request.provider,
            selected_model=request.model,
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
        api_key_resolver: Callable[[], str | None] | None = None,
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
        self.api_key_resolver = api_key_resolver
        self.base_url = base_url
        self._client = client
        self._client_lock = Lock()
        self._owns_client = False
        self.allow_real_calls_env = allow_real_calls_env
        self.endpoint = endpoint
        self.max_attempts = max(1, max_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self.timeout_seconds = timeout_seconds

    def complete(self, request: ModelRequest) -> ModelResponse:
        if not _real_model_calls_allowed(self.allow_real_calls_env):
            raise _LocalModelRouteRejection(
                f"Real model calls are disabled for provider: {self.provider}. "
                f"Set {self.allow_real_calls_env}=1 to enable explicit real calls.",
                reason="provider_not_ready",
                token=_LOCAL_ROUTE_REJECTION_TOKEN,
                provider=self.provider,
                model=request.model,
                error_class="ProviderNotReady",
                error_summary="classification=provider_error;retryable=false",
            )
        started = perf_counter()
        try:
            client = self._client_for_call()
            timeout_budget = self._request_timeout_seconds(request)
        except _LocalModelRouteRejection:
            raise
        except ModelRuntimeError as exc:
            raise _LocalModelRouteRejection(
                str(exc),
                reason="provider_not_ready",
                token=_LOCAL_ROUTE_REJECTION_TOKEN,
                provider=self.provider,
                model=request.model,
                error_class=exc.error_class or "ProviderNotReady",
                error_summary=exc.error_summary or "classification=provider_error;retryable=false",
            ) from exc
        except Exception as exc:
            raise _LocalModelRouteRejection(
                "Model provider failed local transport setup.",
                reason="provider_not_ready",
                token=_LOCAL_ROUTE_REJECTION_TOKEN,
                provider=self.provider,
                model=request.model,
                error_class="ProviderNotReady",
                error_summary="classification=provider_error;retryable=false",
            ) from exc
        deadline = started + timeout_budget if timeout_budget is not None else None
        response, provider_attempts = self._complete_with_retries(client, request, started, deadline)

        elapsed_ms = max(1, int((perf_counter() - started) * 1000))
        adapter = "openai_compatible_chat" if self.endpoint == "chat_completions" else "openai_compatible"
        try:
            _validate_openai_response_shape(response)
            if not request.tools and _response_has_tool_calls(response):
                _raise_unsupported_response_shape("tool_call")
            tool_calls = _response_tool_calls(response)
            _validate_requested_tool_calls(request, tool_calls)
            text = _normalize_provider_text(_response_text(response, allow_empty=bool(tool_calls)))
            raw_finish_reason = _raw_response_finish_reason(response)
            partial = False
            try:
                finish_reason = _validated_finish_reason(
                    raw_finish_reason,
                    allow_tool_calls=bool(tool_calls),
                )
            except ModelRuntimeError as exc:
                if (
                    request.max_continuations > 0
                    and raw_finish_reason in {"length", "incomplete"}
                    and text.strip()
                ):
                    finish_reason = str(raw_finish_reason)
                    partial = True
                else:
                    raise exc
            usage = _response_usage(response)
        except ModelRuntimeError as exc:
            failed_attempts = [
                *provider_attempts,
                _provider_attempt_evidence(
                    len(provider_attempts) + 1,
                    error_class=exc.error_class or "InvalidModelResponse",
                    error_summary=exc.error_summary,
                    retryable=False,
                ),
            ]
            raise ModelRuntimeError(
                str(exc),
                provider=self.provider,
                model=request.model,
                adapter=adapter,
                error_class=exc.error_class or "InvalidModelResponse",
                error_summary=exc.error_summary,
                elapsed_ms=elapsed_ms,
                provider_attempts=failed_attempts,
            ) from exc
        return ModelResponse(
            text=text,
            usage=usage,
            latency_ms=elapsed_ms,
            finish_reason=finish_reason,
            raw_provider=self.provider,
            selected_provider=self.provider,
            selected_model=request.model,
            adapter=adapter,
            mocked=False,
            tool_calls=tool_calls,
            provider_attempts=provider_attempts,
            partial=partial,
        )

    def _client_for_call(self) -> Any:
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                self._client = self._build_client()
                self._owns_client = True
            return self._client

    def close(self) -> None:
        with self._client_lock:
            client = self._client if self._owns_client else None
            self._client = None
            self._owns_client = False
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def _build_client(self) -> Any:
        api_key = (
            self.api_key_resolver()
            if self.api_key_resolver is not None
            else os.environ.get(self.api_key_env)
        )
        if not api_key:
            raise ModelRuntimeError(
                f"Model provider is not enabled: {self.provider}. "
                f"Set {self.api_key_env} to enable real calls."
            )
        base_url = self._resolved_base_url()
        try:
            from openai import DefaultHttpxClient, OpenAI
        except ImportError as exc:
            raise ModelRuntimeError("OpenAI Python SDK is not installed.") from exc

        # Keep a single retry owner so the configured timeout is not multiplied by SDK retries.
        http_client = DefaultHttpxClient(follow_redirects=False, trust_env=False)
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "max_retries": 0,
            "http_client": http_client,
        }
        if base_url is not None:
            kwargs["base_url"] = base_url
        timeout_seconds = self._resolved_timeout_seconds()
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds
        try:
            return OpenAI(**kwargs)
        except Exception:
            http_client.close()
            raise

    def _resolved_base_url(self) -> str | None:
        if callable(self.base_url):
            return self.base_url()
        return self.base_url

    def _complete_with_retries(
        self,
        client: Any,
        request: ModelRequest,
        started: float,
        deadline: float | None,
    ) -> tuple[Any, list[dict[str, Any]]]:
        last_exc: Exception | None = None
        provider_attempts: list[dict[str, Any]] = []
        for attempt in range(1, self.max_attempts + 1):
            attempt_request = request
            if deadline is not None:
                remaining_seconds = deadline - perf_counter()
                if remaining_seconds <= 0:
                    if not provider_attempts:
                        raise _LocalModelRouteRejection(
                            "Model request timeout was exhausted before provider dispatch.",
                            reason="route_timeout_budget_exhausted",
                            token=_LOCAL_ROUTE_REJECTION_TOKEN,
                            provider=self.provider,
                            model=request.model,
                            error_class="ModelRequestTimeout",
                            error_summary="classification=timeout_error;retryable=true",
                        )
                    break
                attempt_request = replace(request, timeout_seconds=remaining_seconds)
            try:
                return self._complete_once(
                    client,
                    attempt_request,
                    provider_attempt=attempt,
                ), provider_attempts
            except Exception as exc:
                if isinstance(exc, _LocalModelRouteRejection):
                    raise
                last_exc = exc
                retryable = _is_transient_model_error(exc)
                provider_attempts.append(
                    _provider_attempt_evidence(
                        attempt,
                        error_class=exc.__class__.__name__,
                        error_summary=_safe_provider_error_summary(exc),
                        retryable=retryable,
                    )
                )
                if attempt >= self.max_attempts or not retryable:
                    break
                if self.retry_delay_seconds:
                    delay = self.retry_delay_seconds
                    if deadline is not None:
                        remaining_seconds = deadline - perf_counter()
                        if remaining_seconds <= 0:
                            break
                        delay = min(delay, remaining_seconds)
                    sleep(delay)
        elapsed_ms = max(1, int((perf_counter() - started) * 1000))
        raise ModelRuntimeError(
            f"{self.provider} model call failed. See server logs for provider details.",
            provider=self.provider,
            model=request.model,
            adapter="openai_compatible_chat" if self.endpoint == "chat_completions" else "openai_compatible",
            error_class=last_exc.__class__.__name__ if last_exc is not None else None,
            error_summary=_safe_provider_error_summary(last_exc),
            elapsed_ms=elapsed_ms,
            provider_attempts=provider_attempts,
        ) from last_exc

    def _complete_once(
        self,
        client: Any,
        request: ModelRequest,
        *,
        provider_attempt: int,
    ) -> Any:
        try:
            request_options = self._request_options(request)
            if self.endpoint == "chat_completions":
                messages = [
                    {"role": "system", "content": request.system_prompt},
                    *[_chat_message(message) for message in request.messages],
                ]
            else:
                input_items = [
                    {"role": "system", "content": request.system_prompt},
                    *_responses_input_items(request.messages),
                ]
        except ModelRuntimeError as exc:
            raise _LocalModelRouteRejection(
                "Model request failed local transport validation.",
                reason="invalid_request",
                token=_LOCAL_ROUTE_REJECTION_TOKEN,
                provider=self.provider,
                model=request.model,
                error_class="InvalidModelRequest",
                error_summary="classification=provider_error;retryable=false",
            ) from exc
        except (TypeError, ValueError) as exc:
            raise _LocalModelRouteRejection(
                "Model request failed local transport validation.",
                reason="invalid_request",
                token=_LOCAL_ROUTE_REJECTION_TOKEN,
                provider=self.provider,
                model=request.model,
                error_class="InvalidModelRequest",
                error_summary="classification=provider_error;retryable=false",
            ) from exc
        _record_provider_attempt_started(request, provider_attempt)
        if self.endpoint == "chat_completions":
            return client.chat.completions.create(
                model=request.model,
                messages=messages,
                **request_options,
            )
        return client.responses.create(
            model=request.model,
            input=input_items,
            **request_options,
        )

    def _request_options(self, request: ModelRequest) -> dict[str, Any]:
        options = _chat_request_options(request) if self.endpoint == "chat_completions" else _request_options(request)
        timeout_seconds = self._request_timeout_seconds(request)
        if timeout_seconds is not None:
            options["timeout"] = timeout_seconds
            if request.provider == "litellm_proxy":
                headers = dict(options.get("extra_headers") or {})
                headers["x-litellm-timeout"] = _format_timeout_header(timeout_seconds)
                options["extra_headers"] = headers
        return options

    def _request_timeout_seconds(self, request: ModelRequest) -> float | None:
        timeout_seconds = self._resolved_timeout_seconds()
        if request.timeout_seconds is not None:
            request_timeout = _validated_positive_timeout(
                request.timeout_seconds,
                "Model request timeout_seconds must be a positive finite number.",
            )
            timeout_seconds = (
                min(timeout_seconds, request_timeout)
                if timeout_seconds is not None
                else request_timeout
            )
        return timeout_seconds

    def _resolved_timeout_seconds(self) -> float | None:
        if self.timeout_seconds is not None:
            return _validated_positive_timeout(
                self.timeout_seconds,
                "Model adapter timeout_seconds must be a positive finite number.",
            )
        raw = os.environ.get("TEAM_AGENT_MODEL_TIMEOUT_SECONDS")
        if raw is None or not raw.strip():
            return 180.0
        try:
            timeout = float(raw)
        except ValueError as exc:
            raise ModelRuntimeError("TEAM_AGENT_MODEL_TIMEOUT_SECONDS must be a positive number.") from exc
        if not isfinite(timeout) or timeout <= 0:
            raise ModelRuntimeError(
                "TEAM_AGENT_MODEL_TIMEOUT_SECONDS must be a positive finite number."
            )
        return timeout


class ModelGateway:
    def __init__(
        self,
        adapters: dict[str, ModelAdapter] | None = None,
        *,
        provider_concurrency_limits: dict[str, int] | None = None,
        capability_registry: CapabilityRegistry | None = None,
        health_registry: ProviderHealthRegistry | None = None,
    ) -> None:
        self.adapters = adapters or default_model_adapters()
        self.capability_registry = capability_registry or load_capability_registry()
        self.health_registry = health_registry or ProviderHealthRegistry()
        default_limit = _positive_int_env("TEAM_AGENT_PROVIDER_MAX_CONCURRENCY", 4)
        configured_limits = provider_concurrency_limits or {}
        unknown_providers = sorted(set(configured_limits) - set(self.adapters))
        if unknown_providers:
            raise ModelRuntimeError(
                f"Concurrency limits reference unconfigured providers: {', '.join(unknown_providers)}"
            )
        invalid_providers = sorted(
            provider for provider, limit in configured_limits.items() if type(limit) is not int or limit <= 0
        )
        if invalid_providers:
            raise ModelRuntimeError(
                f"Provider concurrency limits must be positive integers: {', '.join(invalid_providers)}"
            )
        self._provider_gates = {
            provider: BoundedSemaphore(configured_limits.get(provider, default_limit))
            for provider in self.adapters
        }

    def close(self) -> None:
        for adapter in self.adapters.values():
            close = getattr(adapter, "close", None)
            if callable(close):
                close()

    def complete(self, request: ModelRequest) -> ModelResponse:
        try:
            candidates = route_candidates_from_request(
                request.provider,
                request.model,
                request.fallbacks,
                input_usd_per_million=request.input_usd_per_million,
                output_usd_per_million=request.output_usd_per_million,
            )
        except (RoutePolicyError, ValueError) as exc:
            raise ModelRuntimeError(
                "Model request contains an invalid route candidate.",
                provider=request.provider,
                model=request.model,
                error_class="RoutePolicyError",
                error_summary="classification=provider_error;retryable=false",
            ) from exc
        requirements = RouteRequirements(
            tools=bool(request.tools),
            vision="vision" in _required_model_capabilities(request),
            reasoning="reasoning" in _required_model_capabilities(request),
            web_sidecar="web_sidecar" in _required_model_capabilities(request),
        )
        route_deadline: float | None = None
        if len(candidates) > 1:
            total_timeout = (
                _validated_positive_timeout(
                    request.timeout_seconds,
                    "Model request timeout_seconds must be a positive finite number.",
                )
                if request.timeout_seconds is not None
                else _positive_float_env("TEAM_AGENT_MODEL_TIMEOUT_SECONDS", 180.0)
            )
            route_deadline = perf_counter() + total_timeout
        receipt: list[dict[str, Any]] = []
        last_error: ModelRuntimeError | None = None
        for attempt, candidate in enumerate(candidates, start=1):
            if (
                candidate.provider in REAL_MODEL_PROVIDERS
                and request.metadata.get("run_bound") is True
                and request.metadata.get("real_model_access_confirmed") is not True
            ):
                receipt.append(
                    _route_receipt_entry(
                        attempt=attempt,
                        provider=candidate.provider,
                        model=candidate.model,
                        outcome="rejected",
                        reason="real_model_access_not_confirmed",
                    )
                )
                continue
            if (
                candidate.provider == "mock"
                and attempt > 1
                and request.metadata.get("allow_mock_fallback") is not True
            ):
                receipt.append(
                    _route_receipt_entry(
                        attempt=attempt,
                        provider=candidate.provider,
                        model=candidate.model,
                        outcome="rejected",
                        reason="mock_fallback_disabled",
                    )
                )
                continue
            if (
                attempt > 1
                and candidate.provider in REAL_MODEL_PROVIDERS
                and not candidate.allow_real_calls
            ):
                receipt.append(
                    _route_receipt_entry(
                        attempt=attempt,
                        provider=candidate.provider,
                        model=candidate.model,
                        outcome="rejected",
                        reason="real_fallback_not_approved",
                    )
                )
                continue
            decision = explain_route(
                [candidate],
                requirements=requirements,
                capabilities=self.capability_registry,
                configured_providers=set(self.adapters),
                health=self.health_registry,
                allow_mock_fallback=request.metadata.get("allow_mock_fallback") is True,
                provider_ready=lambda route_candidate: self.provider_ready(route_candidate.provider),
            )
            if not decision.usable:
                rejection = decision.rejected[0].reason if decision.rejected else "route_rejected"
                receipt.append(
                    _route_receipt_entry(
                        attempt=attempt,
                        provider=candidate.provider,
                        model=candidate.model,
                        outcome="rejected",
                        reason=rejection,
                    )
                )
                if len(candidates) > 1 and attempt == 1 and rejection in {
                    "provider_not_configured",
                    "provider_not_ready",
                }:
                    error_class = (
                        "ProviderNotConfigured"
                        if rejection == "provider_not_configured"
                        else "ProviderNotReady"
                    )
                    raise ModelRuntimeError(
                        "The primary model route is not ready; fallback was not attempted.",
                        provider=candidate.provider,
                        model=candidate.model,
                        error_class=error_class,
                        error_summary="classification=provider_error;retryable=false",
                        route_receipt=receipt,
                    )
                continue
            candidate_price = self._route_price(candidate)
            candidate_request = replace(
                request,
                provider=candidate.provider,
                model=candidate.model,
                max_tokens=(
                    max(request.max_tokens or 0, _DEEPSEEK_MIN_RUN_OUTPUT_TOKENS)
                    if candidate.provider == "deepseek"
                    and request.metadata.get("smoke_test") is not True
                    and request.metadata.get("agent_loop_step") is None
                    else request.max_tokens
                ),
                fallbacks=[],
                input_usd_per_million=(
                    candidate_price.input_usd_per_million if candidate_price is not None else None
                ),
                output_usd_per_million=(
                    candidate_price.output_usd_per_million if candidate_price is not None else None
                ),
                metadata={
                    **request.metadata,
                    "route_attempt": attempt,
                    "route_reason": candidate.reason,
                },
            )
            if route_deadline is not None:
                remaining_seconds = route_deadline - perf_counter()
                if remaining_seconds <= 0:
                    receipt.append(
                        _route_receipt_entry(
                            attempt=attempt,
                            provider=candidate.provider,
                            model=candidate.model,
                            outcome="rejected",
                            reason="route_timeout_budget_exhausted",
                        )
                    )
                    break
                candidate_request = replace(candidate_request, timeout_seconds=remaining_seconds)
            try:
                trusted_transport = _is_trusted_attempt_adapter(
                    self.adapters.get(candidate.provider)
                )
                response_retry_attempt = 0
                while True:
                    try:
                        validated = self._complete_with_continuations(
                            candidate_request,
                            provider=candidate.provider,
                            model=candidate.model,
                            allowed_tools={tool.name for tool in request.tools},
                            allow_provider_attempts=trusted_transport,
                            deadline=route_deadline,
                        )
                        break
                    except ModelRuntimeError as exc:
                        if (
                            exc.error_class != "InvalidModelResponse"
                            or response_retry_attempt >= _INVALID_RESPONSE_RETRY_LIMIT
                        ):
                            raise
                        response_retry_attempt += 1
                        remaining_seconds = (
                            route_deadline - perf_counter()
                            if route_deadline is not None
                            else candidate_request.timeout_seconds
                        )
                        if remaining_seconds is not None and remaining_seconds <= 0:
                            raise
                        candidate_request = replace(
                            candidate_request,
                            timeout_seconds=remaining_seconds,
                            metadata={
                                **candidate_request.metadata,
                                "response_retry_attempt": response_retry_attempt,
                            },
                        )
            except ModelRuntimeError as exc:
                retryable = model_error_is_retryable(exc)
                if (
                    exc.error_class in {"IncompleteModelResponse", "InvalidModelResponse"}
                    and attempt < len(candidates)
                ):
                    retryable = True
                local_rejection_reason = (
                    exc.reason if isinstance(exc, _LocalModelRouteRejection) else None
                )
                if local_rejection_reason is None:
                    self.health_registry.record_failure(
                        candidate.provider,
                        exc.error_class,
                        retryable=retryable,
                    )
                trusted_transport = _is_trusted_attempt_adapter(
                    self.adapters.get(candidate.provider)
                )
                provider_attempts = (
                    getattr(exc, "provider_attempts", []) if trusted_transport else []
                )
                if local_rejection_reason is not None:
                    receipt.append(
                        _route_receipt_entry(
                            attempt=attempt,
                            provider=candidate.provider,
                            model=candidate.model,
                            outcome="rejected",
                            reason=local_rejection_reason,
                            error_class=exc.error_class,
                            error_summary=exc.error_summary,
                            latency_ms=exc.elapsed_ms,
                        )
                    )
                elif provider_attempts:
                    receipt.extend(
                        _route_receipt_entry(
                            attempt=attempt,
                            provider=candidate.provider,
                            model=candidate.model,
                            outcome="failed",
                            reason=str(provider_attempt["reason"]),
                            error_class=str(provider_attempt["error_class"]),
                            error_summary=str(provider_attempt["error_summary"]),
                            provider_attempt=int(provider_attempt["provider_attempt"]),
                            usage_known=False,
                        )
                        for provider_attempt in provider_attempts
                    )
                else:
                    receipt.append(
                        _route_receipt_entry(
                            attempt=attempt,
                            provider=candidate.provider,
                            model=candidate.model,
                            outcome="failed",
                            reason="retryable_error" if retryable else "non_retryable_error",
                            error_class=exc.error_class,
                            error_summary=exc.error_summary,
                            latency_ms=exc.elapsed_ms,
                        )
                    )
                exc.route_receipt = receipt
                last_error = exc
                if not retryable:
                    raise
                continue
            self.health_registry.record_success(candidate.provider, validated.latency_ms)
            selected_price = candidate_price
            receipt.extend(
                _route_receipt_entry(
                    attempt=attempt,
                    provider=candidate.provider,
                    model=candidate.model,
                    outcome="failed",
                    reason=str(provider_attempt["reason"]),
                    error_class=str(provider_attempt["error_class"]),
                    error_summary=str(provider_attempt["error_summary"]),
                    provider_attempt=int(provider_attempt["provider_attempt"]),
                    usage_known=False,
                )
                for provider_attempt in validated.provider_attempts
            )
            selected_provider_attempt = (
                len(validated.provider_attempts) + 1
                if trusted_transport
                else None
            )
            receipt.append(
                _route_receipt_entry(
                    attempt=attempt,
                    provider=candidate.provider,
                    model=candidate.model,
                    outcome="succeeded",
                    reason="selected",
                    mocked=validated.mocked,
                    latency_ms=validated.latency_ms,
                    usage=validated.usage,
                    cost_usd=self._estimate_route_cost(selected_price, validated.usage),
                    provider_attempt=selected_provider_attempt,
                    usage_known=(
                        (
                            type(validated.usage.get("input_tokens")) is int
                            and type(validated.usage.get("output_tokens")) is int
                        )
                        if selected_provider_attempt is not None
                        else None
                    ),
                )
            )
            return replace(
                validated,
                route_receipt=receipt,
                selected_provider=candidate.provider,
                selected_model=candidate.model,
                selected_input_usd_per_million=(
                    selected_price.input_usd_per_million if selected_price is not None else None
                ),
                selected_output_usd_per_million=(
                    selected_price.output_usd_per_million if selected_price is not None else None
                ),
            )
        if last_error is not None:
            last_error.route_receipt = receipt
            raise last_error
        if len(candidates) == 1 and receipt:
            reason = receipt[0].get("reason")
            if reason == "provider_not_configured":
                raise ModelRuntimeError(
                    f"Model provider not configured: {request.provider}",
                    provider=request.provider,
                    model=request.model,
                    error_class="ProviderNotConfigured",
                    error_summary="classification=provider_error;retryable=false",
                    route_receipt=receipt,
                )
            if reason == "capability_mismatch":
                raise ModelRuntimeError(
                    "Model route does not satisfy the request capability contract.",
                    provider=request.provider,
                    model=request.model,
                    error_class="ModelCapabilityError",
                    error_summary="classification=capability_error;retryable=false",
                    route_receipt=receipt,
                )
        raise ModelRuntimeError(
            "No usable model route remained after capability and health checks.",
            provider=request.provider,
            model=request.model,
            error_class="RoutePolicyError",
            error_summary="classification=provider_error;retryable=false",
            route_receipt=receipt,
        )

    def provider_ready(self, provider: str) -> bool:
        adapter = self.adapters.get(provider)
        if adapter is None or isinstance(adapter, ProviderStubAdapter):
            return False
        if not isinstance(adapter, OpenAICompatibleModelAdapter):
            return True
        catalog_entry = next((item for item in model_provider_catalog() if item.name == provider), None)
        if catalog_entry is None:
            return True
        return catalog_entry.enabled and (
            catalog_entry.real_calls_configured or not catalog_entry.requires_credentials
        )

    def _complete_single(self, request: ModelRequest) -> ModelResponse:
        adapter = self.adapters.get(request.provider)
        if adapter is None:
            raise _LocalModelRouteRejection(
                f"Model provider not configured: {request.provider}",
                reason="provider_not_configured",
                token=_LOCAL_ROUTE_REJECTION_TOKEN,
                provider=request.provider,
                model=request.model,
                error_class="ProviderNotConfigured",
            )
        self._validate_request_capabilities(request)
        if request.timeout_seconds is not None:
            _validated_positive_timeout(
                request.timeout_seconds,
                "Model request timeout_seconds must be a positive finite number.",
            )
        capacity_timeout = (
            float(request.timeout_seconds)
            if request.timeout_seconds is not None
            else _positive_float_env("TEAM_AGENT_MODEL_TIMEOUT_SECONDS", 180.0)
        )
        deadline = perf_counter() + capacity_timeout
        gate = self._provider_gates[request.provider]
        if not gate.acquire(timeout=capacity_timeout):
            raise _LocalModelRouteRejection(
                f"Provider concurrency wait exceeded the request timeout: {request.provider}",
                reason="provider_capacity_timeout",
                token=_LOCAL_ROUTE_REJECTION_TOKEN,
                provider=request.provider,
                model=request.model,
                error_class="ProviderConcurrencyLimit",
                error_summary="classification=timeout_error;retryable=true",
            )
        try:
            remaining_seconds = deadline - perf_counter()
            if remaining_seconds <= 0:
                raise _LocalModelRouteRejection(
                    f"Provider concurrency wait exhausted the request timeout: {request.provider}",
                    reason="provider_capacity_timeout",
                    token=_LOCAL_ROUTE_REJECTION_TOKEN,
                    provider=request.provider,
                    model=request.model,
                    error_class="ProviderConcurrencyLimit",
                    error_summary="classification=timeout_error;retryable=true",
                )
            return adapter.complete(replace(request, timeout_seconds=remaining_seconds))
        finally:
            gate.release()

    def _complete_with_continuations(
        self,
        request: ModelRequest,
        *,
        provider: str,
        model: str,
        allowed_tools: set[str],
        allow_provider_attempts: bool,
        deadline: float | None,
    ) -> ModelResponse:
        response = self._complete_single(request)
        validated = _validate_model_response(
            response,
            provider=provider,
            model=model,
            allowed_tools=allowed_tools,
            allow_provider_attempts=allow_provider_attempts,
        )
        if not validated.partial:
            return validated

        continuation_deadline = deadline
        if continuation_deadline is None:
            continuation_timeout = request.timeout_seconds or _positive_float_env(
                "TEAM_AGENT_MODEL_TIMEOUT_SECONDS", 180.0
            )
            continuation_deadline = perf_counter() + continuation_timeout

        responses = [validated]
        assembled_text = validated.text
        for continuation_index in range(1, request.max_continuations + 1):
            remaining_seconds = continuation_deadline - perf_counter()
            if remaining_seconds <= 0:
                break
            continuation_request = _continuation_request(
                request,
                assembled_text,
                continuation_index,
                remaining_seconds,
            )
            response = self._complete_single(continuation_request)
            validated = _validate_model_response(
                response,
                provider=provider,
                model=model,
                allowed_tools=allowed_tools,
                allow_provider_attempts=allow_provider_attempts,
            )
            responses.append(validated)
            assembled_text = _merge_continuation_text(assembled_text, validated.text)
            if not validated.partial:
                return replace(
                    validated,
                    text=assembled_text,
                    usage=_sum_model_usage(responses),
                    latency_ms=sum(item.latency_ms for item in responses),
                    partial=False,
                    continuation_count=continuation_index,
                    provider_attempts=[
                        attempt
                        for item in responses
                        for attempt in item.provider_attempts
                    ],
                )

        raise ModelRuntimeError(
            "Model response remained incomplete after bounded continuations.",
            provider=provider,
            model=model,
            error_class="IncompleteModelResponse",
            error_summary=f"finish_reason={responses[-1].finish_reason}",
            elapsed_ms=sum(item.latency_ms for item in responses),
            provider_attempts=[
                attempt
                for item in responses
                for attempt in item.provider_attempts
            ],
        )

    def _validate_request_capabilities(self, request: ModelRequest) -> None:
        required = request.metadata.get("required_model_capabilities", [])
        if not isinstance(required, list) or any(
            type(item) is not str or item not in {"tools", "vision", "reasoning", "web_sidecar"}
            for item in required
        ):
            raise _LocalModelRouteRejection(
                "Model request contains invalid capability requirements.",
                reason="capability_mismatch",
                token=_LOCAL_ROUTE_REJECTION_TOKEN,
                provider=request.provider,
                model=request.model,
                error_class="ModelCapabilityError",
                error_summary="classification=capability_error;retryable=false",
            )
        required_set = set(required)
        try:
            match = self.capability_registry.require(
                request.provider,
                request.model,
                tools=bool(request.tools) or "tools" in required_set,
                vision="vision" in required_set,
                reasoning="reasoning" in required_set,
                web_sidecar="web_sidecar" in required_set,
            )
            capability = match.capability
            if (
                capability is not None
                and capability.max_output_tokens is not None
                and request.max_tokens is not None
                and request.max_tokens > capability.max_output_tokens
            ):
                raise CapabilityError(
                    f"Requested max_tokens exceeds the known limit for {request.provider}/{request.model}"
                )
        except CapabilityError as exc:
            raise _LocalModelRouteRejection(
                "Model route does not satisfy the request capability contract.",
                reason="capability_mismatch",
                token=_LOCAL_ROUTE_REJECTION_TOKEN,
                provider=request.provider,
                model=request.model,
                error_class="ModelCapabilityError",
                error_summary="classification=capability_error;retryable=false",
            ) from exc

    def route_prices(self, request: ModelRequest) -> list[ModelRoutePrice | None]:
        try:
            candidates = route_candidates_from_request(
                request.provider,
                request.model,
                request.fallbacks,
                input_usd_per_million=request.input_usd_per_million,
                output_usd_per_million=request.output_usd_per_million,
            )
        except (RoutePolicyError, ValueError) as exc:
            raise ModelRuntimeError("Model request contains an invalid route candidate.") from exc
        return [self._route_price(candidate) for candidate in candidates]

    def _route_price(self, candidate: Any) -> ModelRoutePrice | None:
        if candidate.input_usd_per_million is not None:
            return ModelRoutePrice(
                provider=candidate.provider,
                model=candidate.model,
                input_usd_per_million=float(candidate.input_usd_per_million),
                output_usd_per_million=float(candidate.output_usd_per_million),
                source="route_override",
            )
        capability = self.capability_registry.resolve(candidate.provider, candidate.model).capability
        if capability is None or capability.input_price is None or capability.output_price is None:
            return None
        return ModelRoutePrice(
            provider=candidate.provider,
            model=candidate.model,
            input_usd_per_million=float(capability.input_price),
            output_usd_per_million=float(capability.output_price),
            source="capability_registry",
        )

    def _estimate_route_cost(
        self,
        price: ModelRoutePrice | None,
        usage: dict[str, int],
    ) -> float | None:
        if price is None:
            return None
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        if type(input_tokens) is not int or type(output_tokens) is not int:
            return None
        return (
            input_tokens * price.input_usd_per_million
            + output_tokens * price.output_usd_per_million
        ) / 1_000_000


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
        route_receipt: list[dict[str, Any]] | None = None,
        provider_attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = (
            _safe_model_identifier(provider, "unknown_provider") if provider is not None else None
        )
        self.model = _safe_model_identifier(model, "unknown_model") if model is not None else None
        self.adapter = (
            _safe_model_identifier(adapter, "unknown_adapter") if adapter is not None else None
        )
        self.error_class = _safe_model_error_class(error_class) if error_class is not None else None
        self.error_summary = _normalize_model_error_summary(error_summary)
        self.elapsed_ms = elapsed_ms
        self.route_receipt = _safe_route_receipt(route_receipt)
        self.provider_attempts = _validated_provider_attempts(provider_attempts or [])


class _LocalModelRouteRejection(ModelRuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason: str,
        token: object,
        **kwargs: Any,
    ) -> None:
        if token is not _LOCAL_ROUTE_REJECTION_TOKEN or reason not in _LOCAL_ROUTE_REJECTION_REASONS:
            raise ValueError("Unsupported local route rejection")
        super().__init__(message, **kwargs)
        self.reason = reason


def _is_trusted_attempt_adapter(adapter: Any) -> bool:
    # This is an in-process trust boundary, not a plugin sandbox. Only the exact
    # built-in transport may attest to its own physical HTTP attempts.
    return type(adapter) is OpenAICompatibleModelAdapter


def _record_provider_attempt_started(request: ModelRequest, provider_attempt: int) -> None:
    recorder = request.provider_attempt_recorder
    if recorder is None:
        return
    evidence = {
        "provider_attempt": provider_attempt,
        "provider": _safe_model_identifier(request.provider, "unknown_provider"),
        "model": _safe_model_identifier(request.model, "unknown_model"),
        "outcome": "dispatch_started",
        "usage_known": False,
    }
    for key in ("agent_run_id", "agent_id", "step_name"):
        value = request.metadata.get(key)
        if type(value) is str:
            evidence[key] = _safe_model_identifier(value, f"unknown_{key}")
    for key in ("route_attempt", "agent_loop_step", "response_retry_attempt", "continuation_attempt"):
        value = request.metadata.get(key)
        if type(value) is int and value > 0:
            evidence[key] = value
    try:
        recorder(evidence)
    except Exception as exc:
        raise _LocalModelRouteRejection(
            "Provider attempt evidence could not be persisted before dispatch.",
            reason="provider_attempt_record_failed",
            token=_LOCAL_ROUTE_REJECTION_TOKEN,
            provider=request.provider,
            model=request.model,
            error_class="ProviderAttemptRecordError",
            error_summary="classification=provider_error;retryable=false",
        ) from exc


def model_runtime_error_payload(exc: Exception) -> dict[str, Any]:
    raw_provider = getattr(exc, "provider", None)
    raw_model = getattr(exc, "model", None)
    raw_adapter = getattr(exc, "adapter", None)
    raw_error_class = getattr(exc, "error_class", None)
    raw_elapsed_ms = getattr(exc, "elapsed_ms", None)
    payload = {
        "provider": (
            _safe_model_identifier(raw_provider, "unknown_provider")
            if raw_provider is not None
            else None
        ),
        "model": (
            _safe_model_identifier(raw_model, "unknown_model") if raw_model is not None else None
        ),
        "adapter": (
            _safe_model_identifier(raw_adapter, "unknown_adapter")
            if raw_adapter is not None
            else None
        ),
        "error_class": (
            _safe_model_error_class(raw_error_class) if raw_error_class is not None else None
        ),
        "error_summary": getattr(exc, "error_summary", None),
        "elapsed_ms": (
            raw_elapsed_ms if type(raw_elapsed_ms) is int and raw_elapsed_ms >= 0 else None
        ),
        "route_receipt": _safe_route_receipt(getattr(exc, "route_receipt", None)),
    }
    if payload["error_summary"] is not None:
        payload["error_summary"] = _normalize_model_error_summary(str(payload["error_summary"]))
    if not payload["route_receipt"]:
        payload.pop("route_receipt", None)
    return {key: value for key, value in payload.items() if value is not None}


REAL_MODEL_PROVIDER_API_KEY_ENVS = {
    "openai": "OPENAI_OFFICIAL_API_KEY",
    "gpt_relay": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "litellm_proxy": "LITELLM_API_KEY",
}


REAL_MODEL_PROVIDERS = set(REAL_MODEL_PROVIDER_API_KEY_ENVS)


ROUTABLE_MODEL_PROVIDERS = {"mock", *REAL_MODEL_PROVIDERS}


DEFAULT_REAL_MODEL_REASONING_EFFORT = "xhigh"


def model_provider_credentials_configured(provider: str) -> bool:
    if provider == "openai":
        return bool(_official_openai_api_key())
    if provider == "gpt_relay":
        return bool(os.environ.get("OPENAI_API_KEY")) and bool(os.environ.get("OPENAI_API_BASE"))
    env_name = REAL_MODEL_PROVIDER_API_KEY_ENVS.get(provider)
    return bool(env_name and os.environ.get(env_name))


def default_reasoning_effort_for_model(provider: str, model: str) -> str | None:
    if provider in REAL_MODEL_PROVIDERS:
        return DEFAULT_REAL_MODEL_REASONING_EFFORT
    return None


def default_model_adapters() -> dict[str, ModelAdapter]:
    return {
        "mock": MockModelAdapter(),
        "openai": OpenAICompatibleModelAdapter(
            provider="openai",
            api_key_env="OPENAI_OFFICIAL_API_KEY",
            api_key_resolver=_official_openai_api_key,
        ),
        "gpt_relay": OpenAICompatibleModelAdapter(
            provider="gpt_relay",
            api_key_env="OPENAI_API_KEY",
            base_url=_gpt_relay_base_url,
            endpoint=gpt_relay_protocol(),
            max_attempts=_positive_int_env("TEAM_AGENT_GPT_RELAY_MAX_ATTEMPTS", 2),
        ),
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
    relay_configured = bool(os.environ.get("OPENAI_API_KEY")) and bool(
        os.environ.get("OPENAI_API_BASE")
    )
    relay_ready = relay_configured
    if relay_ready:
        try:
            _gpt_relay_base_url()
            gpt_relay_protocol()
        except ModelRuntimeError:
            relay_ready = False
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
            enabled=_real_model_calls_allowed() and bool(_official_openai_api_key()),
            real_calls=True,
            real_calls_configured=bool(_official_openai_api_key()),
            requires_credentials=True,
            description=(
                "Official OpenAI adapter is available when OPENAI_OFFICIAL_API_KEY is set, "
                "an agent explicitly opts in, and real calls are explicitly enabled."
            ),
            protocol="responses",
        ),
        ModelProviderInfo(
            name="gpt_relay",
            adapter=(
                "openai_compatible_chat"
                if gpt_relay_protocol() == "chat_completions"
                else "openai_compatible"
            ),
            enabled=(
                _real_model_calls_allowed()
                and relay_ready
            ),
            real_calls=True,
            real_calls_configured=relay_configured,
            requires_credentials=True,
            description=(
                "Direct GPT relay adapter uses OPENAI_API_KEY and OPENAI_API_BASE without "
                "starting the local LiteLLM process."
            ),
            protocol=gpt_relay_protocol(),
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
            protocol="chat_completions",
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
            protocol="chat_completions",
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


def _required_model_capabilities(request: ModelRequest) -> list[str]:
    value = request.metadata.get("required_model_capabilities", [])
    required = list(value) if isinstance(value, list) else []
    if _request_contains_image_block(request) and "vision" not in required:
        required.append("vision")
    return required


def _request_contains_image_block(request: ModelRequest) -> bool:
    return any(
        isinstance(block, dict) and block.get("type") == "image_ref"
        for message in request.messages
        if isinstance(message.content, list)
        for block in message.content
    )


def _continuation_request(
    request: ModelRequest,
    assembled_text: str,
    continuation_index: int,
    timeout_seconds: float,
) -> ModelRequest:
    return replace(
        request,
        messages=[
            *request.messages,
            ModelMessage(role="assistant", content=assembled_text),
            ModelMessage(
                role="user",
                content=(
                    "Continue the previous response from exactly where it stopped. "
                    "Do not repeat any text already present. Preserve the same structure "
                    "and finish the requested deliverable."
                ),
            ),
        ],
        timeout_seconds=timeout_seconds,
        metadata={
            **request.metadata,
            "continuation_attempt": continuation_index,
        },
    )


def _merge_continuation_text(existing: str, continuation: str) -> str:
    continuation = continuation.lstrip()
    if not continuation:
        return existing
    max_overlap = min(2_000, len(existing), len(continuation))
    for overlap in range(max_overlap, 0, -1):
        if existing.endswith(continuation[:overlap]):
            return existing + continuation[overlap:]
    return f"{existing}\n{continuation}"


def _sum_model_usage(responses: list[ModelResponse]) -> dict[str, int]:
    usage_keys = ("input_tokens", "output_tokens", "total_tokens")
    if any(any(key not in response.usage for key in usage_keys) for response in responses):
        return {}
    return {
        key: sum(response.usage[key] for response in responses)
        for key in usage_keys
    }


def _route_receipt_entry(
    *,
    attempt: int,
    provider: str,
    model: str,
    outcome: str,
    reason: str,
    error_class: str | None = None,
    error_summary: str | None = None,
    mocked: bool | None = None,
    latency_ms: int | None = None,
    usage: dict[str, int] | None = None,
    cost_usd: float | None = None,
    provider_attempt: int | None = None,
    usage_known: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "attempt": attempt if type(attempt) is int and attempt > 0 else 1,
        "provider": _safe_model_identifier(provider, "unknown_provider"),
        "model": _safe_model_identifier(model, "unknown_model"),
        "outcome": outcome if outcome in {"rejected", "failed", "succeeded"} else "failed",
        "reason": _safe_model_identifier(reason, "invalid_evidence"),
    }
    if error_class is not None:
        payload["error_class"] = _safe_model_error_class(error_class)
    if error_summary is not None:
        payload["error_summary"] = _normalize_model_error_summary(error_summary)
    if mocked is not None:
        payload["mocked"] = mocked
    if latency_ms is not None:
        payload["latency_ms"] = max(0, int(latency_ms))
    if usage:
        payload["usage"] = dict(usage)
    if cost_usd is not None and isfinite(float(cost_usd)):
        payload["cost_usd"] = round(max(0.0, float(cost_usd)), 8)
    if type(provider_attempt) is int and provider_attempt > 0:
        payload["provider_attempt"] = provider_attempt
    if usage_known is not None:
        payload["usage_known"] = usage_known
    return payload


def _provider_attempt_evidence(
    provider_attempt: int,
    *,
    error_class: str,
    error_summary: str | None,
    retryable: bool,
) -> dict[str, Any]:
    safe_error_summary = (
        _normalize_model_error_summary(error_summary)
        or "classification=unclassified_model_runtime_error"
    )
    return {
        "provider_attempt": provider_attempt,
        "outcome": "failed",
        "reason": "retryable_error" if retryable else "non_retryable_error",
        "error_class": _safe_model_error_class(error_class),
        "error_summary": safe_error_summary,
        "usage_known": False,
    }


def _safe_model_identifier(value: Any, fallback: str) -> str:
    normalized = value.strip() if type(value) is str else ""
    return (
        normalized
        if normalized
        and len(normalized) <= _MAX_MODEL_RESPONSE_IDENTIFIER_LENGTH
        and all(character in _MODEL_RESPONSE_IDENTIFIER_CHARS for character in normalized)
        and not contains_secret_like_text(normalized)
        else fallback
    )


def _safe_model_error_class(value: Any) -> str:
    return _safe_model_identifier(value, "ProviderError")


def _safe_route_receipt(value: Any) -> list[dict[str, Any]]:
    if type(value) is not list:
        return []
    safe: list[dict[str, Any]] = []
    for raw in value:
        if type(raw) is not dict:
            continue
        usage = raw.get("usage")
        safe_usage = (
            {
                key: item
                for key, item in usage.items()
                if key in _MODEL_RESPONSE_USAGE_KEYS and type(item) is int and item >= 0
            }
            if type(usage) is dict
            else None
        )
        safe.append(
            _route_receipt_entry(
                attempt=raw.get("attempt"),
                provider=raw.get("provider"),
                model=raw.get("model"),
                outcome=raw.get("outcome"),
                reason=raw.get("reason"),
                error_class=raw.get("error_class") if raw.get("error_class") is not None else None,
                error_summary=(
                    raw.get("error_summary") if raw.get("error_summary") is not None else None
                ),
                mocked=raw.get("mocked") if type(raw.get("mocked")) is bool else None,
                latency_ms=(
                    raw.get("latency_ms")
                    if type(raw.get("latency_ms")) is int and raw.get("latency_ms") >= 0
                    else None
                ),
                usage=safe_usage,
                cost_usd=(
                    raw.get("cost_usd")
                    if type(raw.get("cost_usd")) in {int, float}
                    and not isinstance(raw.get("cost_usd"), bool)
                    and isfinite(float(raw.get("cost_usd")))
                    else None
                ),
                provider_attempt=raw.get("provider_attempt"),
                usage_known=(
                    raw.get("usage_known") if type(raw.get("usage_known")) is bool else None
                ),
            )
        )
    return safe


def _validated_provider_attempts(value: Any) -> list[dict[str, Any]]:
    if type(value) is not list:
        _raise_invalid_model_response_metadata()
    required_keys = {
        "provider_attempt",
        "outcome",
        "reason",
        "error_class",
        "error_summary",
        "usage_known",
    }
    validated: list[dict[str, Any]] = []
    for index, raw_attempt in enumerate(value, start=1):
        if type(raw_attempt) is not dict or set(raw_attempt) != required_keys:
            _raise_invalid_model_response_metadata()
        if (
            raw_attempt.get("provider_attempt") != index
            or raw_attempt.get("outcome") != "failed"
            or raw_attempt.get("reason") not in {"retryable_error", "non_retryable_error"}
            or raw_attempt.get("usage_known") is not False
        ):
            _raise_invalid_model_response_metadata()
        error_class = _validated_model_response_identifier(raw_attempt.get("error_class"))
        error_summary = _normalize_model_error_summary(raw_attempt.get("error_summary"))
        validated.append(
            {
                "provider_attempt": index,
                "outcome": "failed",
                "reason": raw_attempt["reason"],
                "error_class": error_class,
                "error_summary": error_summary or "classification=unclassified_model_runtime_error",
                "usage_known": False,
            }
        )
    return validated


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
    timeout_seconds: float | None = None,
) -> ModelRequest:
    provider = str(model_config.get("provider", "mock"))
    model = str(model_config.get("model", "mock-model"))
    reasoning_effort = _optional_str(model_config.get("reasoning_effort")) or default_reasoning_effort_for_model(
        provider,
        model,
    )
    model_content_blocks = context.get("_model_content_blocks")
    required_capabilities = [
        item
        for item in context.get("required_model_capabilities", [])
        if isinstance(item, str)
    ]
    if isinstance(model_content_blocks, list) and any(
        isinstance(item, dict) and item.get("type") == "image_ref" for item in model_content_blocks
    ) and "vision" not in required_capabilities:
        required_capabilities.append("vision")
    messages = [
        ModelMessage(role="user", content=f"Task: {task_title}\nGoal: {task_goal}"),
        ModelMessage(role="user", content=f"Step: {step_name}\nAgent: {agent_role}"),
        ModelMessage(role="user", content=context_message_from_envelope(context)),
    ]
    if isinstance(model_content_blocks, list) and model_content_blocks:
        messages.append(ModelMessage(role="user", content=model_content_blocks))
    return ModelRequest(
        provider=provider,
        model=model,
        system_prompt=system_prompt,
        messages=messages,
        temperature=_optional_float(model_config.get("temperature")),
        max_tokens=_optional_int(model_config.get("max_tokens")),
        max_continuations=max(0, _optional_int(model_config.get("continuation_attempts")) or 0),
        timeout_seconds=timeout_seconds,
        reasoning_effort=reasoning_effort,
        tools_allowed=allowed_tools,
        fallbacks=model_fallbacks_from_config(model_config),
        input_usd_per_million=_optional_float(model_config.get("input_usd_per_million")),
        output_usd_per_million=_optional_float(model_config.get("output_usd_per_million")),
        metadata={
            "task_title": task_title,
            "step_name": step_name,
            "agent_id": agent_id,
            "agent_run_id": context.get("agent_run_id"),
            "agent_role": agent_role,
            "context_keys": sorted(context.keys()),
            "allow_mock_fallback": model_allow_mock_fallback_from_config(model_config),
            "required_model_capabilities": required_capabilities,
            "run_bound": isinstance(context.get("run_id"), str) and bool(context.get("run_id")),
            "real_model_access_confirmed": context.get("real_model_access_confirmed") is True,
            "content_block_hashes": [
                str(item.get("sha256"))
                for item in context.get("content_blocks", [])
                if isinstance(item, dict) and item.get("sha256")
            ],
        },
    )


def model_fallbacks_from_config(model_config: dict[str, Any]) -> list[dict[str, Any]]:
    values = model_config.get("fallbacks", [])
    if values is None:
        return []
    if not isinstance(values, list):
        raise ModelRuntimeError("Model fallback configuration must be a list.")
    try:
        return [
            candidate.public_dict()
            for candidate in route_candidates_from_request("mock", "mock-model", values)[1:]
        ]
    except (RoutePolicyError, ValueError) as exc:
        raise ModelRuntimeError("Model fallback configuration is invalid.") from exc


def model_allow_mock_fallback_from_config(model_config: dict[str, Any]) -> bool:
    return _model_config_bool(model_config, "allow_mock_fallback", False)


def _model_config_bool(model_config: dict[str, Any], name: str, default: bool) -> bool:
    value = model_config.get(name, default)
    if type(value) is not bool:
        raise ModelRuntimeError(f"Model configuration {name} must be boolean.")
    return value


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
        "content_blocks",
        "vision_preprocess",
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


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ModelRuntimeError(f"{name} must be a positive number.") from exc
    if not isfinite(value) or value <= 0:
        raise ModelRuntimeError(f"{name} must be a positive finite number.")
    return value


def _validated_positive_timeout(value: Any, message: str) -> float:
    if (
        type(value) not in {int, float}
        or isinstance(value, bool)
        or not isfinite(float(value))
        or float(value) <= 0
    ):
        raise _LocalModelRouteRejection(
            message,
            reason="invalid_request",
            token=_LOCAL_ROUTE_REJECTION_TOKEN,
            error_class="InvalidModelRequest",
            error_summary="classification=provider_error;retryable=false",
        )
    return float(value)


def _estimate_tokens(value: object) -> int:
    if isinstance(value, list):
        return max(1, sum(_estimate_tokens(item) for item in value))
    if isinstance(value, dict):
        return max(1, sum(_estimate_tokens(key) + _estimate_tokens(item) for key, item in value.items()))
    if isinstance(value, str) and value.startswith("data:"):
        return max(1, (len(value.encode("utf-8")) + 3) // 4)
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return max(1, len(value.split()))


def _format_timeout_header(value: float) -> str:
    numeric = float(value)
    return str(int(numeric)) if numeric.is_integer() else str(numeric)


def _message_text_content(content: str | list[dict[str, Any]]) -> str:
    if not isinstance(content, str):
        raise ModelRuntimeError("Tool result messages must contain text content.")
    return content


def _chat_content(content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ModelRuntimeError("Model message content must be text or content blocks.")
    if len(content) > 16:
        raise ModelRuntimeError("Model message contains too many content blocks.")
    converted: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ModelRuntimeError("Model message contains an invalid content block.")
        block_type = block["type"]
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str) or not text:
                raise ModelRuntimeError("Text content blocks must contain non-empty text.")
            converted.append({"type": "text", "text": text})
        elif block_type == "image_ref":
            data_uri = _validated_image_data_uri(block.get("data_uri"))
            converted.append({"type": "image_url", "image_url": {"url": data_uri}})
        elif block_type == "file_ref":
            raise ModelRuntimeError("file_ref must be converted to untrusted text by the multimodal preprocessor.")
        else:
            raise ModelRuntimeError("Model message contains an unsupported content block type.")
    return converted


def _responses_content(content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ModelRuntimeError("Model message content must be text or content blocks.")
    if len(content) > 16:
        raise ModelRuntimeError("Model message contains too many content blocks.")
    converted: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ModelRuntimeError("Model message contains an invalid content block.")
        block_type = block["type"]
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str) or not text:
                raise ModelRuntimeError("Text content blocks must contain non-empty text.")
            converted.append({"type": "input_text", "text": text})
        elif block_type == "image_ref":
            data_uri = _validated_image_data_uri(block.get("data_uri"))
            converted.append({"type": "input_image", "image_url": data_uri})
        elif block_type == "file_ref":
            raise ModelRuntimeError("file_ref must be converted to untrusted text by the multimodal preprocessor.")
        else:
            raise ModelRuntimeError("Model message contains an unsupported content block type.")
    return converted


def _validated_image_data_uri(value: Any) -> str:
    if not isinstance(value, str) or len(value.encode("ascii", errors="ignore")) > _MAX_MODEL_DATA_URI_BYTES:
        raise ModelRuntimeError("Image content blocks require a bounded validated data URI.")
    match = _DATA_URI_RE.fullmatch(value)
    if match is None:
        raise ModelRuntimeError("Image content blocks require a valid base64 data URI.")
    mime_type, encoded = match.groups()
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ModelRuntimeError("Image content blocks contain invalid base64 data.") from exc
    if not decoded or len(decoded) > _MAX_MODEL_IMAGE_BYTES:
        raise ModelRuntimeError("Image content blocks exceed the decoded size limit.")
    magic = {
        "image/jpeg": decoded.startswith(b"\xff\xd8\xff"),
        "image/png": decoded.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": decoded.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": decoded.startswith(b"RIFF") and decoded[8:12] == b"WEBP",
    }
    if not magic.get(mime_type, False):
        raise ModelRuntimeError("Image content blocks do not match their declared MIME type.")
    return value


def _chat_message(message: ModelMessage) -> dict[str, Any]:
    if message.role == "tool":
        if not message.tool_call_id:
            raise ModelRuntimeError("Tool result messages require tool_call_id.")
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id,
            "content": _message_text_content(message.content),
        }
    if message.role not in {"system", "developer", "user", "assistant"}:
        raise ModelRuntimeError("Model message contains an unsupported role.")
    payload: dict[str, Any] = {"role": message.role, "content": _chat_content(message.content)}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.arguments, ensure_ascii=False, separators=(",", ":")),
                },
            }
            for tool_call in message.tool_calls
        ]
    return payload


def _responses_input_items(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            if not message.tool_call_id:
                raise ModelRuntimeError("Tool result messages require tool_call_id.")
            items.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": _message_text_content(message.content),
                }
            )
            continue
        if message.role not in {"system", "developer", "user", "assistant"}:
            raise ModelRuntimeError("Model message contains an unsupported role.")
        items.append({"role": message.role, "content": _responses_content(message.content)})
        items.extend(
            {
                "type": "function_call",
                "call_id": tool_call.id,
                "name": tool_call.name,
                "arguments": json.dumps(tool_call.arguments, ensure_ascii=False, separators=(",", ":")),
            }
            for tool_call in message.tool_calls
        )
    return items


def _validated_tool_definitions(tools: list[ModelToolDefinition]) -> list[ModelToolDefinition]:
    names: set[str] = set()
    validated: list[ModelToolDefinition] = []
    for tool in tools:
        name = _validated_model_response_identifier(tool.name)
        if name in names:
            raise ModelRuntimeError("Model request declares duplicate tool names.")
        names.add(name)
        if not isinstance(tool.description, str) or not tool.description.strip() or len(tool.description) > 2000:
            raise ModelRuntimeError("Model request contains an invalid tool description.")
        if not isinstance(tool.input_schema, dict) or tool.input_schema.get("type") != "object":
            raise ModelRuntimeError("Model request contains an invalid tool input schema.")
        try:
            encoded_schema = json.dumps(tool.input_schema, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ModelRuntimeError("Model request contains a non-serializable tool input schema.") from exc
        if len(encoded_schema) > 100_000:
            raise ModelRuntimeError("Model request tool input schema is too large.")
        validated.append(ModelToolDefinition(name=name, description=tool.description.strip(), input_schema=tool.input_schema))
    return validated


def _request_options(request: ModelRequest) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if request.temperature is not None and _supports_temperature(request):
        options["temperature"] = request.temperature
    if request.max_tokens is not None:
        options["max_output_tokens"] = request.max_tokens
    transport = reasoning_effort_transport(request)
    if transport.reasoning_effort_sent and transport.sent_reasoning_effort is not None:
        options["reasoning"] = {"effort": transport.sent_reasoning_effort}
    if request.tools:
        options["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            }
            for tool in _validated_tool_definitions(request.tools)
        ]
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
    if request.tools:
        options["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in _validated_tool_definitions(request.tools)
        ]
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
    if request.provider in {"gpt_relay", "litellm_proxy"} and _is_litellm_gpt_reasoning_model(request.model):
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
    if not _is_litellm_gpt_reasoning_model(request.model):
        return False
    if request.provider == "gpt_relay":
        return os.environ.get("TEAM_AGENT_GPT_RELAY_XHIGH_PASSTHROUGH") == "1"
    if request.provider != "litellm_proxy":
        return False
    return os.environ.get("TEAM_AGENT_LITELLM_XHIGH_REASONING_PASSTHROUGH") == "1"


def _supports_openai_reasoning_effort(request: ModelRequest) -> bool:
    if request.provider == "openai":
        return _is_known_openai_reasoning_model(request.model)
    if request.provider in {"gpt_relay", "litellm_proxy"}:
        return _is_litellm_gpt_reasoning_model(request.model)
    return False


def _is_litellm_gpt_reasoning_model(model: str) -> bool:
    return model.lower().strip() in {"gpt5.5", "gpt5.6-sol", "gpt-5.5", "gpt-5.6-sol"}


def _is_known_openai_reasoning_model(model: str) -> bool:
    normalized = model.lower().strip()
    return (
        normalized == "gpt5.5"
        or normalized == "gpt5.6-sol"
        or normalized.startswith("gpt-5")
        or normalized.startswith("gpt5")
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
_MAX_TOOL_CALLS_PER_RESPONSE = 16
_MAX_TOOL_ARGUMENT_CHARS = 100_000


def model_response_is_complete(response: ModelResponse) -> bool:
    return response.finish_reason in _COMPLETE_RESPONSE_STATES


def _validate_model_response(
    response: Any,
    *,
    provider: str,
    model: str,
    allowed_tools: set[str],
    allow_provider_attempts: bool = False,
) -> ModelResponse:
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
        provider_attempts = _validated_provider_attempts(response.provider_attempts)
        if provider_attempts and not allow_provider_attempts:
            _raise_invalid_model_response_metadata()
        if response.selected_provider is not None:
            selected_provider = _validated_model_response_identifier(response.selected_provider)
            if selected_provider != provider:
                _raise_invalid_model_response_metadata()
        if response.selected_model is not None:
            selected_model = _validated_model_response_identifier(response.selected_model)
            if selected_model != model:
                _raise_invalid_model_response_metadata()
        if (
            raw_provider != provider
            or type(response.mocked) is not bool
            or type(response.partial) is not bool
            or type(response.continuation_count) is not int
            or response.continuation_count < 0
        ):
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
        tool_calls = _validated_model_tool_calls(response.tool_calls)
        _ensure_tool_calls_allowed(tool_calls, allowed_tools)
        finish_reason = _validated_finish_reason(
            response.finish_reason,
            allow_tool_calls=bool(tool_calls),
            allow_incomplete=response.partial,
        )
        text = _validated_response_text(response.text, allow_empty=bool(tool_calls))
        if response.partial and not text.strip():
            raise ModelRuntimeError(
                "Partial model response did not contain usable text.",
                error_class="InvalidModelResponse",
                error_summary="response_text=empty",
            )
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
        selected_provider=provider,
        selected_model=model,
        adapter=adapter,
        mocked=response.mocked,
        tool_calls=tool_calls,
        provider_attempts=provider_attempts,
        partial=response.partial,
        continuation_count=response.continuation_count,
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


def _validated_model_tool_calls(value: Any) -> list[ModelToolCall]:
    if not isinstance(value, list) or len(value) > _MAX_TOOL_CALLS_PER_RESPONSE:
        _raise_invalid_model_response_metadata()
    calls: list[ModelToolCall] = []
    ids: set[str] = set()
    for item in value:
        if not isinstance(item, ModelToolCall) or not isinstance(item.arguments, dict):
            _raise_invalid_model_response_metadata()
        call_id = _validated_model_response_identifier(item.id)
        name = _validated_model_response_identifier(item.name)
        if call_id in ids:
            _raise_invalid_model_response_metadata()
        ids.add(call_id)
        try:
            encoded = json.dumps(item.arguments, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            _raise_invalid_model_response_metadata()
        if len(encoded) > _MAX_TOOL_ARGUMENT_CHARS:
            _raise_invalid_model_response_metadata()
        calls.append(ModelToolCall(id=call_id, name=name, arguments=item.arguments))
    return calls


def _ensure_tool_calls_allowed(tool_calls: list[ModelToolCall], allowed_tools: set[str]) -> None:
    unknown = sorted({tool_call.name for tool_call in tool_calls} - allowed_tools)
    if unknown:
        _raise_unsupported_response_shape("tool_call")


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
        if getattr(message, "function_call", None):
            _raise_unsupported_response_shape("tool_call")
        if getattr(message, "refusal", None) is not None:
            _raise_unsupported_response_shape("refusal")

    output = getattr(response, "output", None) or []
    for item in output:
        item_type = _normalized_response_item_type(item)
        if item_type == "refusal" or getattr(item, "refusal", None) is not None:
            _raise_unsupported_response_shape("refusal")
        for content in getattr(item, "content", None) or []:
            content_type = _normalized_response_item_type(content)
            if content_type == "refusal" or getattr(content, "refusal", None) is not None:
                _raise_unsupported_response_shape("refusal")


def _normalized_response_item_type(item: Any) -> str | None:
    item_type = getattr(item, "type", None)
    if not isinstance(item_type, str):
        return None
    return item_type.strip().lower()


def _is_tool_call_item_type(item_type: str | None) -> bool:
    return item_type is not None and (item_type == "function_call" or item_type.endswith("_call"))


def _response_tool_calls(response: Any) -> list[ModelToolCall]:
    calls: list[ModelToolCall] = []
    choices = getattr(response, "choices", None) or []
    for choice in choices:
        message = getattr(choice, "message", None)
        for raw_call in getattr(message, "tool_calls", None) or []:
            function = getattr(raw_call, "function", None)
            calls.append(
                _parsed_tool_call(
                    getattr(raw_call, "id", None),
                    getattr(function, "name", None),
                    getattr(function, "arguments", None),
                )
            )
    for item in getattr(response, "output", None) or []:
        if not _is_tool_call_item_type(_normalized_response_item_type(item)):
            continue
        calls.append(
            _parsed_tool_call(
                getattr(item, "call_id", None) or getattr(item, "id", None),
                getattr(item, "name", None),
                getattr(item, "arguments", None),
            )
        )
    return _validated_model_tool_calls(calls)


def _response_has_tool_calls(response: Any) -> bool:
    for choice in getattr(response, "choices", None) or []:
        if getattr(getattr(choice, "message", None), "tool_calls", None):
            return True
    return any(
        _is_tool_call_item_type(_normalized_response_item_type(item))
        for item in getattr(response, "output", None) or []
    )


def _parsed_tool_call(call_id: Any, name: Any, raw_arguments: Any) -> ModelToolCall:
    try:
        validated_id = _validated_model_response_identifier(call_id)
        validated_name = _validated_model_response_identifier(name)
        if not isinstance(raw_arguments, str) or len(raw_arguments) > _MAX_TOOL_ARGUMENT_CHARS:
            raise ValueError("invalid arguments")
        arguments = json.loads(raw_arguments)
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ModelRuntimeError(
            "Model response included an invalid tool call.",
            error_class="InvalidModelResponse",
            error_summary="response_shape=tool_call",
        ) from None
    return ModelToolCall(id=validated_id, name=validated_name, arguments=arguments)


def _validate_requested_tool_calls(request: ModelRequest, tool_calls: list[ModelToolCall]) -> None:
    declared_tools = {tool.name for tool in _validated_tool_definitions(request.tools)}
    _ensure_tool_calls_allowed(tool_calls, declared_tools)


def _raise_unsupported_response_shape(shape: str) -> None:
    raise ModelRuntimeError(
        "Model response included unsupported tool or refusal content.",
        error_class="InvalidModelResponse",
        error_summary=f"response_shape={shape}",
    )


def _response_text(response: Any, *, allow_empty: bool = False) -> str:
    choices = getattr(response, "choices", None) or []
    for choice in choices:
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        return _validated_response_text(content, allow_empty=allow_empty)
    output_text = getattr(response, "output_text", _MISSING_RESPONSE_FIELD)
    if output_text is not _MISSING_RESPONSE_FIELD:
        return _validated_response_text(output_text, allow_empty=allow_empty)
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
        return _validated_response_text("\n".join(parts), allow_empty=allow_empty)
    if allow_empty:
        return ""
    raise ModelRuntimeError(
        "Model response did not contain non-empty text.",
        error_class="InvalidModelResponse",
        error_summary="response_text=missing",
    )


def _validated_response_text(value: Any, *, allow_empty: bool = False) -> str:
    if allow_empty and value is None:
        return ""
    if not isinstance(value, str):
        raise ModelRuntimeError(
            "Model response did not contain non-empty text.",
            error_class="InvalidModelResponse",
            error_summary="response_text_type=non_string",
        )
    if not value.strip() and not allow_empty:
        raise ModelRuntimeError(
            "Model response did not contain non-empty text.",
            error_class="InvalidModelResponse",
            error_summary="response_text=empty",
        )
    return value


def _normalize_provider_text(value: str) -> str:
    """Remove provider-specific tool envelopes that are returned as plain text."""
    if _DSML_MARKER not in value:
        return value
    match = _DSML_CONTENT_PARAMETER_RE.search(value)
    if match is None:
        raise ModelRuntimeError(
            "Model response contained an unsupported provider tool envelope.",
            error_class="InvalidModelResponse",
            error_summary="response_shape=provider_tool_markup",
        )
    content = html.unescape(match.group("content")).strip()
    if not content:
        raise ModelRuntimeError(
            "Model response provider tool envelope contained empty content.",
            error_class="InvalidModelResponse",
            error_summary="response_text=empty",
        )
    return content


def _response_usage(response: Any) -> dict[str, int]:
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        prompt_tokens = _response_usage_field(usage, "prompt_tokens")
        completion_tokens = _response_usage_field(usage, "completion_tokens")
        total_tokens = _response_usage_field(usage, "total_tokens")
        if prompt_tokens is not _MISSING_RESPONSE_FIELD or completion_tokens is not _MISSING_RESPONSE_FIELD:
            raw_usage = _present_response_usage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        else:
            input_tokens = _response_usage_field(usage, "input_tokens")
            output_tokens = _response_usage_field(usage, "output_tokens")
            if (
                input_tokens is _MISSING_RESPONSE_FIELD
                and output_tokens is _MISSING_RESPONSE_FIELD
                and total_tokens is _MISSING_RESPONSE_FIELD
            ):
                _raise_invalid_model_response_metadata()
            raw_usage = _present_response_usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            )
        return _validated_model_response_usage(raw_usage)
    except Exception:
        _raise_invalid_model_response_metadata()


def _response_usage_field(usage: Any, name: str) -> Any:
    if isinstance(usage, dict):
        return usage.get(name, _MISSING_RESPONSE_FIELD)
    return getattr(usage, name, _MISSING_RESPONSE_FIELD)


def _present_response_usage(**values: Any) -> dict[str, int]:
    return {
        key: _validated_model_response_counter(value)
        for key, value in values.items()
        if value is not _MISSING_RESPONSE_FIELD and value is not None
    }


def _response_finish_reason(response: Any, *, allow_tool_calls: bool = False) -> str:
    return _validated_finish_reason(
        _raw_response_finish_reason(response),
        allow_tool_calls=allow_tool_calls,
    )


def _raw_response_finish_reason(response: Any) -> Any:
    choices = getattr(response, "choices", None) or []
    for choice in choices:
        return getattr(choice, "finish_reason", None)
    return getattr(response, "status", None)


def _validated_finish_reason(
    value: Any,
    *,
    allow_tool_calls: bool = False,
    allow_incomplete: bool = False,
) -> str:
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
    if finish_reason == "tool_calls" and allow_tool_calls:
        return finish_reason
    if finish_reason in {"length", "incomplete"} and allow_incomplete:
        return finish_reason
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
    status_code = _provider_error_status_code(exc)
    if status_code is not None:
        return status_code in {408, 429} or 500 <= status_code <= 599
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


def model_error_is_retryable(exc: Exception) -> bool:
    summary = getattr(exc, "error_summary", None)
    if isinstance(summary, str) and "retryable=true" in summary:
        return True
    if isinstance(summary, str) and "retryable=false" in summary:
        return False
    return _is_transient_model_error(exc)


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
            "capability_error",
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


def gpt_route_mode() -> str:
    value = os.environ.get("TEAM_AGENT_GPT_ROUTE_MODE", "direct").strip().lower()
    if value not in {"direct", "litellm"}:
        raise ModelRuntimeError("TEAM_AGENT_GPT_ROUTE_MODE must be direct or litellm.")
    return value


def gpt_relay_protocol() -> str:
    value = os.environ.get("TEAM_AGENT_GPT_RELAY_PROTOCOL", "chat_completions").strip().lower()
    if value not in {"chat_completions", "responses"}:
        raise ModelRuntimeError(
            "TEAM_AGENT_GPT_RELAY_PROTOCOL must be chat_completions or responses."
        )
    return value


def _official_openai_api_key() -> str | None:
    explicit = os.environ.get("OPENAI_OFFICIAL_API_KEY")
    if explicit:
        return explicit
    if os.environ.get("OPENAI_API_BASE"):
        return None
    return os.environ.get("OPENAI_API_KEY") or None


def _gpt_relay_base_url() -> str:
    value = os.environ.get("OPENAI_API_BASE", "").strip().rstrip("/")
    parsed = urlparse(value)
    if not value or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ModelRuntimeError("OPENAI_API_BASE must be an http(s) URL for gpt_relay.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelRuntimeError(
            "OPENAI_API_BASE must not include credentials, query, or fragment."
        )
    if not _is_loopback_host(parsed.hostname or "") and parsed.scheme != "https":
        raise ModelRuntimeError("Remote GPT relay URLs must use https.")
    return value


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
