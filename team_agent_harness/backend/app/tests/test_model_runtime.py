import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.model_runtime import (
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRuntimeError,
    MockModelAdapter,
    OpenAICompatibleModelAdapter,
    ProviderStubAdapter,
    default_model_adapters,
    model_provider_catalog,
    model_request_from_agent,
    model_runtime_error_payload,
    reasoning_effort_trace_payload,
    reasoning_effort_transport,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_mock_model_adapter_returns_deterministic_response_with_usage() -> None:
    request = ModelRequest(
        provider="mock",
        model="mock-model",
        system_prompt="You are a planner.",
        messages=[ModelMessage(role="user", content="Plan the task.")],
        metadata={"step_name": "plan", "agent_role": "Planner", "task_title": "Demo"},
    )

    response = MockModelAdapter().complete(request)

    assert response.raw_provider == "mock"
    assert response.adapter == "mock"
    assert response.mocked is True
    assert "Planner completed plan." in response.text
    assert response.usage["input_tokens"] > 0
    assert response.usage["output_tokens"] > 0
    assert response.latency_ms >= 1


def test_model_gateway_routes_by_provider() -> None:
    request = ModelRequest(
        provider="mock",
        model="mock-model",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    response = ModelGateway().complete(request)

    assert response.raw_provider == "mock"


def test_model_gateway_rejects_unconfigured_provider() -> None:
    request = ModelRequest(
        provider="missing",
        model="missing-model",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    with pytest.raises(ModelRuntimeError, match="Model provider not configured"):
        ModelGateway().complete(request)


@pytest.mark.parametrize(
    "error_summary",
    [
        "EXTERNAL_EVIDENCE_BODY",
        "classification=EXTERNAL_EVIDENCE_BODY",
        "response_text=sk-secret",
        "finish_reason=secret",
        "status_code=200;retryable=false",
        "classification=provider_error;classification=provider_error",
        "classification=provider_error;status_code=999;retryable=false",
    ],
)
def test_model_runtime_error_payload_rejects_arbitrary_summary_text(error_summary: str) -> None:
    payload = model_runtime_error_payload(
        ModelRuntimeError(
            "Provider failed.",
            error_summary=error_summary,
        )
    )

    assert payload["error_summary"] == "classification=unclassified_model_runtime_error"
    assert "EXTERNAL_EVIDENCE_BODY" not in str(payload)


@pytest.mark.parametrize(
    ("text", "finish_reason", "error_pattern"),
    [
        ("", "stop", "non-empty text"),
        ("raw-body-must-not-leak", "length", "did not complete"),
        ("safe body", "raw-finish-reason-must-not-leak", "did not complete"),
    ],
)
def test_model_gateway_validates_injected_adapter_responses(
    text: str,
    finish_reason: str,
    error_pattern: str,
) -> None:
    response = ModelResponse(
        text=text,
        finish_reason=finish_reason,
        raw_provider="injected",
        adapter="injected-adapter",
        mocked=False,
    )
    request = ModelRequest(
        provider="injected",
        model="injected-model",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )
    adapter = SimpleNamespace(complete=lambda request: response)

    with pytest.raises(ModelRuntimeError, match=error_pattern) as exc_info:
        ModelGateway(adapters={"injected": adapter}).complete(request)

    payload = model_runtime_error_payload(exc_info.value)
    assert payload["provider"] == "injected"
    assert payload["model"] == "injected-model"
    assert "adapter" not in payload
    if text:
        assert text not in str(exc_info.value)
        assert text not in str(payload)
    if finish_reason.startswith("raw-"):
        assert finish_reason not in str(exc_info.value)
        assert finish_reason not in str(payload)


@pytest.mark.parametrize(
    "metadata",
    [
        {"usage": {"input_tokens": "EXTERNAL_EVIDENCE_BODY"}},
        {"usage": {"EXTERNAL_EVIDENCE_BODY": 1}},
        {"latency_ms": "EXTERNAL_EVIDENCE_BODY"},
        {"raw_provider": "EXTERNAL_EVIDENCE_BODY"},
        {"adapter": "EXTERNAL EVIDENCE BODY"},
        {"mocked": "EXTERNAL_EVIDENCE_BODY"},
    ],
    ids=["usage-value", "usage-key", "latency", "provider", "adapter", "mocked"],
)
def test_model_gateway_rejects_untrusted_adapter_response_metadata_without_leaking(
    metadata: dict[str, object],
) -> None:
    response_values: dict[str, object] = {
        "text": "safe response",
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "latency_ms": 3,
        "finish_reason": "stop",
        "raw_provider": "injected",
        "adapter": "injected-adapter",
        "mocked": False,
    }
    response_values.update(metadata)
    response = ModelResponse(**response_values)  # type: ignore[arg-type]
    request = ModelRequest(
        provider="injected",
        model="injected-model",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )
    adapter = SimpleNamespace(complete=lambda request: response)

    with pytest.raises(ModelRuntimeError, match="invalid response metadata") as exc_info:
        ModelGateway(adapters={"injected": adapter}).complete(request)

    payload = model_runtime_error_payload(exc_info.value)
    assert payload == {
        "provider": "injected",
        "model": "injected-model",
        "error_class": "InvalidModelResponse",
        "error_summary": "response_type=invalid",
    }
    assert "EXTERNAL_EVIDENCE_BODY" not in str(exc_info.value)
    assert "EXTERNAL_EVIDENCE_BODY" not in str(payload)


def test_model_gateway_normalizes_valid_injected_adapter_response_metadata() -> None:
    response = ModelResponse(
        text="safe response",
        usage={"output_tokens": 2, "input_tokens": 1},
        latency_ms=3,
        finish_reason=" STOP ",
        raw_provider=" injected ",
        adapter=" injected-adapter ",
        mocked=False,
    )
    request = ModelRequest(
        provider="injected",
        model="injected-model",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )
    adapter = SimpleNamespace(complete=lambda request: response)

    normalized = ModelGateway(adapters={"injected": adapter}).complete(request)

    assert normalized == ModelResponse(
        text="safe response",
        usage={"input_tokens": 1, "output_tokens": 2},
        latency_ms=3,
        finish_reason="stop",
        raw_provider="injected",
        adapter="injected-adapter",
        mocked=False,
    )


def test_model_request_from_agent_includes_curated_context_envelope() -> None:
    request = model_request_from_agent(
        task_title="Demo",
        task_goal="Use context.",
        step_name="write",
        agent_id="agent-writer",
        agent_role="Writer",
        system_prompt="System",
        model_config={"provider": "mock", "model": "mock-model"},
        allowed_tools=["read_file"],
        context={
            "task": {"inputs": {"secret": "not included"}},
            "task_objective": {"goal": "Use context.", "constraints": ["Keep scope small."]},
            "state_breadcrumb": {"current_step": "write"},
            "artifact_refs": [{"id": "artifact-1", "type": "research_note"}],
            "artifact_excerpts": [
                {"id": "artifact-1", "type": "research_note", "excerpt": "bounded context"}
            ],
            "research_tool_evidence": {
                "trust": "untrusted_external_data",
                "kind": "search_results",
                "items": [{"url": "https://example.com", "snippet": "bounded evidence"}],
            },
            "context_manifest": {"schema": "context-envelope-v1"},
        },
    )

    context_message = request.messages[-1].content
    assert context_message.startswith("Context envelope:")
    assert "artifact-1" in context_message
    assert "bounded context" in context_message
    assert "untrusted_external_data" in context_message
    assert "bounded evidence" in context_message
    assert "state_breadcrumb" in context_message
    assert "secret" not in context_message
    context_payload = json.loads(context_message.removeprefix("Context envelope:\n"))
    assert context_message == "Context envelope:\n" + json.dumps(
        context_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    assert context_message.index('"task_objective"') < context_message.index('"artifact_excerpts"')


def test_provider_stub_adapter_rejects_real_calls_without_credentials_or_network() -> None:
    request = ModelRequest(
        provider="openai",
        model="gpt-placeholder",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    with pytest.raises(ModelRuntimeError, match="provider skeleton is not enabled"):
        ProviderStubAdapter("openai").complete(request)


def test_default_model_gateway_knows_real_provider_skeletons_but_keeps_them_disabled() -> None:
    request = ModelRequest(
        provider="openai",
        model="gpt-placeholder",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    with pytest.raises(ModelRuntimeError, match="Real provider calls are not implemented"):
        ModelGateway(adapters={"openai": ProviderStubAdapter("openai")}).complete(request)


def test_openai_compatible_adapter_maps_request_and_response_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    fake_client = FakeOpenAICompatibleClient()
    request = ModelRequest(
        provider="deepseek",
        model="deepseek-v4-pro",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
        temperature=0.1,
        max_tokens=128,
    )

    response = OpenAICompatibleModelAdapter(
        provider="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        client=fake_client,
    ).complete(request)

    assert fake_client.responses.calls == [
        {
            "model": "deepseek-v4-pro",
            "input": [
                {"role": "system", "content": "System"},
                {"role": "user", "content": "Hello"},
            ],
            "temperature": 0.1,
            "max_output_tokens": 128,
            "timeout": 180.0,
        }
    ]
    assert response.text == "adapter response"
    assert response.raw_provider == "deepseek"
    assert response.adapter == "openai_compatible"
    assert response.mocked is False
    assert response.finish_reason == "completed"
    assert response.usage == {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}


@pytest.mark.parametrize(
    "output_text",
    [None, "", " \t\r\n", 7],
    ids=["none", "empty", "whitespace", "non-string"],
)
def test_openai_compatible_adapter_rejects_missing_empty_or_non_string_text(
    monkeypatch: pytest.MonkeyPatch,
    output_text: object,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    raw_reasoning = "reasoning-content-must-not-be-used"
    response = SimpleNamespace(
        output_text=output_text,
        status="completed",
        reasoning_content=raw_reasoning,
        usage=FakeUsage(),
    )

    with pytest.raises(ModelRuntimeError, match="non-empty text") as exc_info:
        _complete_static_response(response)

    payload = model_runtime_error_payload(exc_info.value)
    assert payload["provider"] == "litellm_proxy"
    assert payload["model"] == "deepseek-v4-pro"
    assert payload["adapter"] == "openai_compatible"
    assert payload["error_class"] == "InvalidModelResponse"
    assert raw_reasoning not in str(exc_info.value)
    assert raw_reasoning not in str(payload)


def test_openai_compatible_adapter_rejects_sdk_repr_as_response_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    response = SDKReprSentinelResponse()

    with pytest.raises(ModelRuntimeError, match="non-empty text") as exc_info:
        _complete_static_response(response)

    payload = model_runtime_error_payload(exc_info.value)
    assert SDKReprSentinelResponse.raw_response_sentinel not in str(exc_info.value)
    assert SDKReprSentinelResponse.raw_response_sentinel not in str(payload)


@pytest.mark.parametrize("endpoint", ["responses", "chat_completions"])
def test_openai_compatible_adapter_rejects_untrusted_usage_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    response = SimpleNamespace(
        output_text="safe response",
        status="completed",
        usage=SimpleNamespace(
            input_tokens="EXTERNAL_EVIDENCE_BODY",
            output_tokens=2,
            total_tokens=3,
        ),
    )
    if endpoint == "chat_completions":
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="safe response"),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens="EXTERNAL_EVIDENCE_BODY",
                completion_tokens=2,
                total_tokens=3,
            ),
        )

    with pytest.raises(ModelRuntimeError, match="invalid response metadata") as exc_info:
        _complete_static_response(response, endpoint=endpoint)

    payload = model_runtime_error_payload(exc_info.value)
    assert payload["provider"] == "litellm_proxy"
    assert payload["model"] == "deepseek-v4-pro"
    assert payload["adapter"] == (
        "openai_compatible_chat" if endpoint == "chat_completions" else "openai_compatible"
    )
    assert payload["error_class"] == "InvalidModelResponse"
    assert payload["error_summary"] == "response_type=invalid"
    assert "EXTERNAL_EVIDENCE_BODY" not in str(exc_info.value)
    assert "EXTERNAL_EVIDENCE_BODY" not in str(payload)


@pytest.mark.parametrize(
    ("shape", "expected_summary"),
    [("tool_call", "response_shape=tool_call"), ("refusal", "response_shape=refusal")],
)
def test_openai_compatible_adapter_rejects_chat_text_mixed_with_tool_or_refusal(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    expected_summary: str,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    raw_sentinel = "raw-chat-shape-must-not-leak"
    message = SimpleNamespace(content="apparently safe", tool_calls=None, refusal=None)
    if shape == "tool_call":
        message.tool_calls = [SimpleNamespace(id=raw_sentinel)]
    else:
        message.refusal = raw_sentinel
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=FakeChatUsage(),
    )

    with pytest.raises(ModelRuntimeError, match="tool or refusal") as exc_info:
        _complete_static_response(response, endpoint="chat_completions")

    payload = model_runtime_error_payload(exc_info.value)
    assert payload["error_class"] == "InvalidModelResponse"
    assert payload["error_summary"] == expected_summary
    assert raw_sentinel not in str(exc_info.value)
    assert raw_sentinel not in str(payload)


@pytest.mark.parametrize(
    ("shape", "expected_summary"),
    [
        ("function_call_item", "response_shape=tool_call"),
        ("refusal_item", "response_shape=refusal"),
        ("refusal_content", "response_shape=refusal"),
    ],
)
def test_openai_compatible_adapter_rejects_response_text_mixed_with_tool_or_refusal(
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    expected_summary: str,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    raw_sentinel = "raw-response-shape-must-not-leak"
    if shape == "function_call_item":
        output = [SimpleNamespace(type="function_call", arguments=raw_sentinel, content=[])]
    elif shape == "refusal_item":
        output = [SimpleNamespace(type="refusal", refusal=raw_sentinel, content=[])]
    else:
        output = [
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="refusal", refusal=raw_sentinel)],
            )
        ]
    response = SimpleNamespace(
        output_text="apparently safe",
        output=output,
        status="completed",
        usage=FakeUsage(),
    )

    with pytest.raises(ModelRuntimeError, match="tool or refusal") as exc_info:
        _complete_static_response(response)

    payload = model_runtime_error_payload(exc_info.value)
    assert payload["error_class"] == "InvalidModelResponse"
    assert payload["error_summary"] == expected_summary
    assert raw_sentinel not in str(exc_info.value)
    assert raw_sentinel not in str(payload)


def test_openai_compatible_adapter_builds_client_with_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("TEAM_AGENT_MODEL_TIMEOUT_SECONDS", raising=False)
    fake_openai = CapturingOpenAIConstructor()
    monkeypatch.setattr("openai.OpenAI", fake_openai)

    OpenAICompatibleModelAdapter(
        provider="openai",
        api_key_env="OPENAI_API_KEY",
    )._build_client()

    assert fake_openai.calls == [{"api_key": "test-key", "timeout": 180.0, "max_retries": 0}]


def test_openai_compatible_adapter_builds_client_with_env_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("TEAM_AGENT_MODEL_TIMEOUT_SECONDS", "12.5")
    fake_openai = CapturingOpenAIConstructor()
    monkeypatch.setattr("openai.OpenAI", fake_openai)

    OpenAICompatibleModelAdapter(
        provider="openai",
        api_key_env="OPENAI_API_KEY",
    )._build_client()

    assert fake_openai.calls == [{"api_key": "test-key", "timeout": 12.5, "max_retries": 0}]


def test_openai_compatible_adapter_rejects_invalid_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("TEAM_AGENT_MODEL_TIMEOUT_SECONDS", "0")

    with pytest.raises(ModelRuntimeError, match="positive number"):
        OpenAICompatibleModelAdapter(
            provider="openai",
            api_key_env="OPENAI_API_KEY",
        )._build_client()


def test_openai_compatible_adapter_can_use_chat_completions_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    fake_client = FakeChatCompatibleClient()
    request = ModelRequest(
        provider="deepseek",
        model="deepseek-v4-pro",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
        temperature=0.1,
        max_tokens=128,
    )

    response = OpenAICompatibleModelAdapter(
        provider="deepseek",
        api_key_env="DEEPSEEK_API_KEY",
        base_url="https://api.deepseek.com",
        client=fake_client,
        endpoint="chat_completions",
    ).complete(request)

    assert fake_client.chat.completions.calls == [
        {
            "model": "deepseek-v4-pro",
            "messages": [
                {"role": "system", "content": "System"},
                {"role": "user", "content": "Hello"},
            ],
            "temperature": 0.1,
            "max_tokens": 128,
            "timeout": 180.0,
        }
    ]
    assert response.text == "chat adapter response"
    assert response.raw_provider == "deepseek"
    assert response.adapter == "openai_compatible_chat"
    assert response.mocked is False
    assert response.finish_reason == "stop"
    assert response.usage == {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7}


@pytest.mark.parametrize(
    ("endpoint", "finish_reason", "expected_summary"),
    [
        ("chat_completions", "length", "finish_reason=length"),
        ("chat_completions", "content_filter", "finish_reason=content_filter"),
        ("chat_completions", "tool_calls", "finish_reason=tool_calls"),
        ("responses", "failed", "finish_reason=failed"),
        ("responses", "incomplete", "finish_reason=incomplete"),
        ("responses", "unexpected", "finish_reason=unsupported"),
    ],
)
def test_openai_compatible_adapter_rejects_non_complete_finish_reasons(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    finish_reason: str,
    expected_summary: str,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    response = SimpleNamespace(
        output_text="partial response",
        status=finish_reason,
        usage=FakeUsage(),
    )
    if endpoint == "chat_completions":
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="partial response"),
                    finish_reason=finish_reason,
                )
            ],
            usage=FakeChatUsage(),
        )

    with pytest.raises(ModelRuntimeError, match="did not complete") as exc_info:
        _complete_static_response(response, endpoint=endpoint)

    assert exc_info.value.error_class == "IncompleteModelResponse"
    assert exc_info.value.error_summary == expected_summary


def test_openai_compatible_adapter_rejects_missing_completion_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    response = SimpleNamespace(
        output_text="apparently complete",
        status=None,
        usage=FakeUsage(),
    )

    with pytest.raises(ModelRuntimeError, match="completion status"):
        _complete_static_response(response)


@pytest.mark.parametrize(
    "relative_path",
    ["config/model-routing.local.json", "config/model-routing.litellm.example.json"],
)
def test_checked_in_deepseek_routes_have_minimum_response_budget(relative_path: str) -> None:
    config = json.loads((PROJECT_ROOT / relative_path).read_text(encoding="utf-8"))
    deepseek_routes = [
        route for route in config["agents"].values() if route.get("model") == "deepseek-v4-pro"
    ]

    assert deepseek_routes
    assert all(route.get("max_tokens", 0) >= 4096 for route in deepseek_routes)


def test_litellm_gpt55_chat_request_maps_xhigh_reasoning_effort_to_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.delenv("TEAM_AGENT_LITELLM_XHIGH_REASONING_PASSTHROUGH", raising=False)
    fake_client = FakeChatCompatibleClient()
    request = ModelRequest(
        provider="litellm_proxy",
        model="gpt5.5",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
        reasoning_effort="xhigh",
    )

    OpenAICompatibleModelAdapter(
        provider="litellm_proxy",
        api_key_env="LITELLM_API_KEY",
        client=fake_client,
        endpoint="chat_completions",
    ).complete(request)

    assert fake_client.chat.completions.calls[0]["reasoning_effort"] == "high"
    assert fake_client.chat.completions.calls[0]["timeout"] == 180.0
    assert fake_client.chat.completions.calls[0]["extra_headers"] == {"x-litellm-timeout": "180"}
    assert reasoning_effort_trace_payload(request) == {
        "reasoning_effort": "xhigh",
        "configured_reasoning_effort": "xhigh",
        "sent_reasoning_effort": "high",
        "reasoning_effort_sent": True,
        "reasoning_effort_ignored": False,
        "reasoning_effort_mapping": "xhigh->high",
    }


def test_litellm_gpt55_chat_request_drops_unsupported_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    fake_client = FakeChatCompatibleClient()
    request = ModelRequest(
        provider="litellm_proxy",
        model="gpt5.5",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
        temperature=0.2,
        reasoning_effort="xhigh",
    )

    OpenAICompatibleModelAdapter(
        provider="litellm_proxy",
        api_key_env="LITELLM_API_KEY",
        client=fake_client,
        endpoint="chat_completions",
    ).complete(request)

    assert "temperature" not in fake_client.chat.completions.calls[0]
    assert fake_client.chat.completions.calls[0]["reasoning_effort"] == "high"


def test_litellm_gpt55_chat_request_can_passthrough_xhigh_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("TEAM_AGENT_LITELLM_XHIGH_REASONING_PASSTHROUGH", "1")
    fake_client = FakeChatCompatibleClient()
    request = ModelRequest(
        provider="litellm_proxy",
        model="gpt5.5",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
        reasoning_effort="xhigh",
    )

    OpenAICompatibleModelAdapter(
        provider="litellm_proxy",
        api_key_env="LITELLM_API_KEY",
        client=fake_client,
        endpoint="chat_completions",
        timeout_seconds=12.5,
    ).complete(request)

    assert fake_client.chat.completions.calls[0]["reasoning_effort"] == "xhigh"
    assert fake_client.chat.completions.calls[0]["extra_headers"] == {"x-litellm-timeout": "12.5"}


def test_litellm_non_gpt55_chat_request_does_not_send_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    fake_client = FakeChatCompatibleClient()
    request = ModelRequest(
        provider="litellm_proxy",
        model="deepseek-v4-pro",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
        reasoning_effort="xhigh",
    )

    OpenAICompatibleModelAdapter(
        provider="litellm_proxy",
        api_key_env="LITELLM_API_KEY",
        client=fake_client,
        endpoint="chat_completions",
    ).complete(request)

    assert "reasoning_effort" not in fake_client.chat.completions.calls[0]
    assert fake_client.chat.completions.calls[0]["extra_headers"] == {"x-litellm-timeout": "180"}
    assert reasoning_effort_trace_payload(request) == {
        "reasoning_effort": "xhigh",
        "configured_reasoning_effort": "xhigh",
        "reasoning_effort_sent": False,
        "reasoning_effort_ignored": True,
        "reasoning_effort_ignore_reason": "provider_or_model_not_known_to_support_reasoning_effort",
    }


def test_direct_openai_known_reasoning_model_maps_xhigh_reasoning_effort_to_high(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    fake_client = FakeOpenAICompatibleClient()
    request = ModelRequest(
        provider="openai",
        model="gpt-5.5",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
        reasoning_effort="xhigh",
    )

    OpenAICompatibleModelAdapter(
        provider="openai",
        api_key_env="OPENAI_API_KEY",
        client=fake_client,
    ).complete(request)

    assert fake_client.responses.calls[0]["reasoning"] == {"effort": "high"}
    assert reasoning_effort_trace_payload(request) == {
        "reasoning_effort": "xhigh",
        "configured_reasoning_effort": "xhigh",
        "sent_reasoning_effort": "high",
        "reasoning_effort_sent": True,
        "reasoning_effort_ignored": False,
        "reasoning_effort_mapping": "xhigh->high",
    }


def test_direct_openai_unknown_alias_does_not_send_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    fake_client = FakeOpenAICompatibleClient()
    request = ModelRequest(
        provider="openai",
        model="gpt-reviewer",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
        reasoning_effort="xhigh",
    )

    OpenAICompatibleModelAdapter(
        provider="openai",
        api_key_env="OPENAI_API_KEY",
        client=fake_client,
    ).complete(request)

    assert "reasoning" not in fake_client.responses.calls[0]
    assert reasoning_effort_trace_payload(request) == {
        "reasoning_effort": "xhigh",
        "configured_reasoning_effort": "xhigh",
        "reasoning_effort_sent": False,
        "reasoning_effort_ignored": True,
        "reasoning_effort_ignore_reason": "provider_or_model_not_known_to_support_reasoning_effort",
    }


def test_openai_compatible_adapter_redacts_provider_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    fake_client = FailingOpenAICompatibleClient(
        RuntimeError("Authorization: Bearer sk-secret https://proxy.local/v1 payload=secret")
    )
    request = ModelRequest(
        provider="litellm_proxy",
        model="gpt-reviewer",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    with pytest.raises(ModelRuntimeError) as exc_info:
        OpenAICompatibleModelAdapter(
            provider="litellm_proxy",
            api_key_env="LITELLM_API_KEY",
            client=fake_client,
        ).complete(request)

    message = str(exc_info.value)
    assert "litellm_proxy model call failed" in message
    assert "sk-secret" not in message
    assert "Bearer" not in message
    assert "proxy.local" not in message
    assert "payload" not in message
    assert exc_info.value.provider == "litellm_proxy"
    assert exc_info.value.model == "gpt-reviewer"
    assert exc_info.value.adapter == "openai_compatible"
    assert exc_info.value.error_class == "RuntimeError"
    assert exc_info.value.elapsed_ms is not None
    assert exc_info.value.error_summary == "classification=provider_error;retryable=false"
    assert "sk-secret" not in exc_info.value.error_summary
    assert "payload=secret" not in exc_info.value.error_summary
    assert "proxy.local" not in exc_info.value.error_summary


def test_openai_compatible_adapter_retries_transient_connection_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    fake_client = FlakyChatCompatibleClient(
        failures=[
            RuntimeError("OpenAIException - Connection error. Authorization: Bearer sk-secret"),
            RuntimeError("HTTP 500 InternalServerError"),
        ]
    )
    request = ModelRequest(
        provider="litellm_proxy",
        model="gpt-planner",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    response = OpenAICompatibleModelAdapter(
        provider="litellm_proxy",
        api_key_env="LITELLM_API_KEY",
        client=fake_client,
        endpoint="chat_completions",
        retry_delay_seconds=0,
    ).complete(request)

    assert response.text == "chat adapter response"
    assert len(fake_client.chat.completions.calls) == 3


def test_openai_compatible_adapter_does_not_retry_non_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    fake_client = FlakyChatCompatibleClient(
        failures=[RuntimeError("401 invalid api key Authorization: Bearer sk-secret")]
    )
    request = ModelRequest(
        provider="litellm_proxy",
        model="gpt-planner",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    with pytest.raises(ModelRuntimeError) as exc_info:
        OpenAICompatibleModelAdapter(
            provider="litellm_proxy",
            api_key_env="LITELLM_API_KEY",
            client=fake_client,
            endpoint="chat_completions",
            retry_delay_seconds=0,
        ).complete(request)

    assert len(fake_client.chat.completions.calls) == 1
    assert "sk-secret" not in str(exc_info.value)


def test_default_litellm_proxy_adapter_avoids_stacked_harness_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEAM_AGENT_LITELLM_PROXY_MAX_ATTEMPTS", raising=False)

    adapter = default_model_adapters()["litellm_proxy"]

    assert isinstance(adapter, OpenAICompatibleModelAdapter)
    assert adapter.max_attempts == 1


def test_default_litellm_proxy_adapter_rejects_invalid_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_LITELLM_PROXY_MAX_ATTEMPTS", "0")

    with pytest.raises(ModelRuntimeError, match="TEAM_AGENT_LITELLM_PROXY_MAX_ATTEMPTS"):
        default_model_adapters()


def test_default_gateway_does_not_validate_litellm_url_for_mock_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_BASE_URL", "not-a-url")
    request = ModelRequest(
        provider="mock",
        model="mock-model",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    response = ModelGateway().complete(request)

    assert response.raw_provider == "mock"


def test_litellm_base_url_is_validated_when_provider_builds_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.setenv("LITELLM_BASE_URL", "not-a-url")
    request = ModelRequest(
        provider="litellm_proxy",
        model="gpt-reviewer",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    with pytest.raises(ModelRuntimeError, match="LITELLM_BASE_URL"):
        ModelGateway().complete(request)


def test_litellm_base_url_rejects_plaintext_remote_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REMOTE_LITELLM_PROXY", "1")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://proxy.example/v1")
    request = ModelRequest(
        provider="litellm_proxy",
        model="gpt-reviewer",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    with pytest.raises(ModelRuntimeError, match="must use https"):
        ModelGateway().complete(request)


def test_litellm_base_url_rejects_remote_hosts_without_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REMOTE_LITELLM_PROXY", raising=False)
    monkeypatch.setenv("LITELLM_BASE_URL", "https://proxy.example/v1")
    request = ModelRequest(
        provider="litellm_proxy",
        model="gpt-reviewer",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    with pytest.raises(ModelRuntimeError, match="Remote LiteLLM Proxy URLs are disabled"):
        ModelGateway().complete(request)


def test_litellm_base_url_allows_trusted_remote_hosts_with_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("LITELLM_API_KEY", "test-key")
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REMOTE_LITELLM_PROXY", "1")
    monkeypatch.setenv("LITELLM_BASE_URL", "https://proxy.example/v1")
    calls: list[dict[str, object]] = []

    def fake_openai_constructor(**kwargs: object) -> object:
        calls.append(kwargs)
        return FakeChatCompatibleClient()

    monkeypatch.setattr("openai.OpenAI", fake_openai_constructor)
    request = ModelRequest(
        provider="litellm_proxy",
        model="gpt-reviewer",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    response = ModelGateway().complete(request)

    assert calls == [
        {
            "api_key": "test-key",
            "base_url": "https://proxy.example/v1",
            "timeout": 180.0,
            "max_retries": 0,
        }
    ]
    assert response.raw_provider == "litellm_proxy"
    assert response.mocked is False


def test_openai_compatible_adapter_requires_provider_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    request = ModelRequest(
        provider="openai",
        model="gpt-placeholder",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    with pytest.raises(ModelRuntimeError, match="Set OPENAI_API_KEY"):
        OpenAICompatibleModelAdapter(provider="openai", api_key_env="OPENAI_API_KEY").complete(request)


def test_openai_compatible_adapter_requires_explicit_real_call_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    fake_client = FakeOpenAICompatibleClient()
    request = ModelRequest(
        provider="openai",
        model="gpt-placeholder",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )

    with pytest.raises(ModelRuntimeError, match="TEAM_AGENT_ALLOW_REAL_MODEL_CALLS"):
        OpenAICompatibleModelAdapter(
            provider="openai",
            api_key_env="OPENAI_API_KEY",
            client=fake_client,
        ).complete(request)
    assert fake_client.responses.calls == []


def test_model_provider_catalog_marks_provider_key_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("LITELLM_API_KEY", "litellm-key")

    providers = {provider.name: provider for provider in model_provider_catalog()}

    assert set(providers) == {"mock", "openai", "anthropic", "deepseek", "litellm_proxy", "local"}
    assert providers["mock"].enabled is True
    assert providers["mock"].real_calls is False
    assert providers["openai"].enabled is False
    assert providers["openai"].real_calls is True
    assert providers["openai"].real_calls_configured is False
    assert providers["deepseek"].enabled is False
    assert providers["deepseek"].real_calls is True
    assert providers["deepseek"].real_calls_configured is True
    assert providers["litellm_proxy"].enabled is False
    assert providers["litellm_proxy"].real_calls is True
    assert providers["litellm_proxy"].real_calls_configured is True
    for name in ["anthropic", "local"]:
        assert providers[name].enabled is False
        assert providers[name].real_calls is False

    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    providers = {provider.name: provider for provider in model_provider_catalog()}
    assert providers["deepseek"].enabled is True
    assert providers["litellm_proxy"].enabled is True


def test_model_request_from_agent_uses_agent_model_config_and_context_metadata() -> None:
    request = model_request_from_agent(
        task_title="Demo",
        task_goal="Prove the contract.",
        step_name="review",
        agent_id="agent-reviewer",
        agent_role="Reviewer",
        system_prompt="Review carefully.",
        model_config={
            "provider": "mock",
            "model": "mock-reviewer",
            "temperature": 0.2,
            "max_tokens": 512,
            "reasoning_effort": "xhigh",
        },
        allowed_tools=["read_file"],
        context={"run_id": "run-1", "artifact_ids": []},
    )

    assert request.provider == "mock"
    assert request.model == "mock-reviewer"
    assert request.temperature == 0.2
    assert request.max_tokens == 512
    assert request.reasoning_effort == "xhigh"
    assert request.tools_allowed == ["read_file"]
    assert request.metadata["agent_id"] == "agent-reviewer"
    assert request.metadata["context_keys"] == ["artifact_ids", "run_id"]


def test_model_request_from_agent_defaults_real_provider_reasoning_effort_to_xhigh() -> None:
    request = model_request_from_agent(
        task_title="Demo",
        task_goal="Use the default.",
        step_name="review",
        agent_id="agent-reviewer",
        agent_role="Reviewer",
        system_prompt="Review carefully.",
        model_config={"provider": "deepseek", "model": "deepseek-v4-pro"},
        allowed_tools=["read_file"],
        context={"run_id": "run-1"},
    )

    assert request.reasoning_effort == "xhigh"
    transport = reasoning_effort_transport(request)
    assert transport.configured_reasoning_effort == "xhigh"
    assert transport.reasoning_effort_sent is False
    assert transport.reasoning_effort_ignored is True


class FakeOpenAICompatibleClient:
    def __init__(self) -> None:
        self.responses = FakeResponses()


class CapturingOpenAIConstructor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return FakeOpenAICompatibleClient()


class FakeChatCompatibleClient:
    def __init__(self) -> None:
        self.chat = FakeChat()


class FlakyChatCompatibleClient:
    def __init__(self, failures: list[Exception]) -> None:
        self.chat = FakeChat(failures=failures)


class FailingOpenAICompatibleClient:
    def __init__(self, exc: Exception) -> None:
        self.responses = FailingResponses(exc)


class FakeResponses:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return FakeResponse()


class FakeChat:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.completions = FakeChatCompletions(failures=failures)


class FakeChatCompletions:
    def __init__(self, failures: list[Exception] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.failures = list(failures or [])

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        return FakeChatResponse()


class FailingResponses:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def create(self, **kwargs: object) -> object:
        raise self.exc


class FakeResponse:
    output_text = "adapter response"
    status = "completed"

    def __init__(self) -> None:
        self.usage = FakeUsage()


class FakeChatResponse:
    def __init__(self) -> None:
        self.choices = [FakeChatChoice()]
        self.usage = FakeChatUsage()


class FakeChatChoice:
    def __init__(self) -> None:
        self.message = FakeChatMessage()
        self.finish_reason = "stop"


class FakeChatMessage:
    content = "chat adapter response"


class SDKReprSentinelResponse:
    raw_response_sentinel = "ChatCompletion(raw-secret-response-sentinel)"
    status = "completed"

    def __init__(self) -> None:
        self.usage = FakeUsage()

    def __str__(self) -> str:
        return self.raw_response_sentinel


class FakeUsage:
    input_tokens = 3
    output_tokens = 2
    total_tokens = 5


class FakeChatUsage:
    prompt_tokens = 4
    completion_tokens = 3
    total_tokens = 7


def _complete_static_response(response: object, *, endpoint: str = "responses") -> object:
    create = lambda **kwargs: response
    client = SimpleNamespace(responses=SimpleNamespace(create=create))
    if endpoint == "chat_completions":
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    request = ModelRequest(
        provider="litellm_proxy",
        model="deepseek-v4-pro",
        system_prompt="System",
        messages=[ModelMessage(role="user", content="Hello")],
    )
    return OpenAICompatibleModelAdapter(
        provider="litellm_proxy",
        api_key_env="LITELLM_API_KEY",
        client=client,
        endpoint=endpoint,
    ).complete(request)
