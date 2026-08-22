import json

import pytest

from app.core.model_capabilities import (
    CapabilityError,
    ModelCapability,
    default_model_capability_registry,
    load_capability_registry,
)


def test_builtin_registry_exposes_safe_capabilities() -> None:
    registry = default_model_capability_registry()

    match = registry.resolve("deepseek", "deepseek-chat")
    mock_match = registry.resolve("mock", "mock-model")
    gpt_proxy_match = registry.resolve("litellm_proxy", "gpt5.5")
    gpt56_proxy_match = registry.resolve("litellm_proxy", "gpt5.6-sol")
    gpt_relay_match = registry.resolve("gpt_relay", "gpt-5.6-sol")
    deepseek_flash_match = registry.resolve("deepseek", "deepseek-v4-flash")
    deepseek_proxy_match = registry.resolve("litellm_proxy", "deepseek-v4-pro")
    wildcard_proxy_match = registry.resolve("litellm_proxy", "unattested-alias")

    assert match.known is True
    assert match.capability is not None
    assert match.capability.supports_tools is True
    assert match.capability.supports_vision is False
    assert match.capability.input_price == 0.14
    assert mock_match.capability is not None
    assert mock_match.capability.supports_vision is False
    assert mock_match.capability.input_price == 0.0
    assert mock_match.capability.output_price == 0.0
    assert gpt_proxy_match.capability is not None
    assert gpt_proxy_match.capability.model_family == "gpt"
    assert gpt56_proxy_match.capability is not None
    assert gpt56_proxy_match.capability.model_family == "gpt"
    assert gpt_relay_match.capability is not None
    assert gpt_relay_match.capability.model_family == "gpt"
    assert gpt_relay_match.capability.protocol == "configurable_openai_compatible"
    assert deepseek_flash_match.capability is not None
    assert deepseek_flash_match.capability.model_family is None
    assert deepseek_proxy_match.capability is not None
    assert deepseek_proxy_match.capability.model_family == "deepseek"
    assert wildcard_proxy_match.capability is not None
    assert wildcard_proxy_match.capability.model_family is None
    assert "api_key" not in json.dumps(match.public_dict()).lower()


def test_registry_fails_closed_for_unknown_models_when_capability_is_required() -> None:
    registry = default_model_capability_registry()

    with pytest.raises(CapabilityError, match="Capabilities are unknown"):
        registry.require("openai", "future-model", tools=True)


def test_registry_rejects_unsupported_required_capability() -> None:
    registry = default_model_capability_registry()

    with pytest.raises(CapabilityError, match="vision"):
        registry.require("deepseek", "deepseek-chat", vision=True)


def test_mock_capability_cannot_claim_vision_support() -> None:
    with pytest.raises(ValueError, match="mock provider cannot declare vision"):
        ModelCapability(
            provider="mock",
            model_pattern="*",
            protocol="mock",
            supports_vision=True,
        )


def test_model_capability_rejects_unknown_model_family() -> None:
    with pytest.raises(ValueError, match="model_family"):
        ModelCapability(
            provider="litellm_proxy",
            model_pattern="custom-model",
            protocol="chat_completions",
            model_family="claude",
        )


@pytest.mark.parametrize("field_name", ["input_price", "output_price"])
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_model_capability_rejects_non_finite_prices(field_name: str, value: float) -> None:
    with pytest.raises(ValueError, match=field_name):
        ModelCapability(
            provider="custom",
            model_pattern="demo-*",
            protocol="chat_completions",
            **{field_name: value},
        )


def test_registry_loads_versioned_external_config(tmp_path) -> None:
    path = tmp_path / "capabilities.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": [
                    {
                        "provider": "custom",
                        "model_pattern": "demo-*",
                        "protocol": "chat_completions",
                        "supports_tools": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    registry = load_capability_registry(path)

    assert registry.resolve("custom", "demo-1").capability.supports_tools is True


@pytest.mark.parametrize("field_name", ["input_price", "output_price"])
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_external_registry_rejects_non_finite_prices(
    tmp_path,
    field_name: str,
    value: float,
) -> None:
    path = tmp_path / "capabilities.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": [
                    {
                        "provider": "custom",
                        "model_pattern": "demo-*",
                        "protocol": "chat_completions",
                        field_name: value,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CapabilityError, match="Invalid model capability entry"):
        load_capability_registry(path)


def test_registry_rejects_wrong_schema_and_empty_config(tmp_path) -> None:
    path = tmp_path / "capabilities.json"
    path.write_text(json.dumps({"schema_version": 99, "models": []}), encoding="utf-8")

    with pytest.raises(CapabilityError, match="schema_version"):
        load_capability_registry(path)
