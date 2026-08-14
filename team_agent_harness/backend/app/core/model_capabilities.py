from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import json
import math
import os
from pathlib import Path
from typing import Any, Literal


CAPABILITIES_CONFIG_ENV = "TEAM_AGENT_MODEL_CAPABILITIES_CONFIG"
CAPABILITY_SCHEMA_VERSION = 1


class CapabilityError(ValueError):
    """Raised when a model request requires a capability the route cannot prove."""


@dataclass(frozen=True)
class ModelCapability:
    provider: str
    model_pattern: str
    protocol: str
    model_family: Literal["gpt", "deepseek"] | None = None
    supports_tools: bool = False
    supports_streaming: bool = False
    supports_vision: bool = False
    supports_reasoning: bool = False
    context_window: int | None = None
    max_output_tokens: int | None = None
    input_price: float | None = None
    output_price: float | None = None
    supports_web_sidecar: bool = False
    schema_version: int = CAPABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model_pattern.strip() or not self.protocol.strip():
            raise ValueError("provider, model_pattern, and protocol must be non-empty")
        if self.schema_version != CAPABILITY_SCHEMA_VERSION:
            raise ValueError(f"Unsupported capability schema_version: {self.schema_version}")
        if self.provider == "mock" and self.supports_vision:
            raise ValueError("The mock provider cannot declare vision support")
        if self.model_family not in {None, "gpt", "deepseek"}:
            raise ValueError("model_family must be gpt, deepseek, or null")
        for name in ("context_window", "max_output_tokens"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(f"{name} must be a positive integer or null")
        for name in ("input_price", "output_price"):
            value = getattr(self, name)
            if value is not None and (
                type(value) not in {int, float}
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be a finite non-negative number or null")

    def matches(self, provider: str, model: str) -> bool:
        return self.provider == provider and fnmatch.fnmatchcase(model, self.model_pattern)

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_pattern": self.model_pattern,
            "protocol": self.protocol,
            "model_family": self.model_family,
            "supports_tools": self.supports_tools,
            "supports_streaming": self.supports_streaming,
            "supports_vision": self.supports_vision,
            "supports_reasoning": self.supports_reasoning,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "input_price": self.input_price,
            "output_price": self.output_price,
            "supports_web_sidecar": self.supports_web_sidecar,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CapabilityMatch:
    provider: str
    model: str
    capability: ModelCapability | None
    source: str

    @property
    def known(self) -> bool:
        return self.capability is not None

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "known": self.known,
            "source": self.source,
            "capability": self.capability.public_dict() if self.capability else None,
        }


class CapabilityRegistry:
    def __init__(self, capabilities: list[ModelCapability], *, source: str = "builtin") -> None:
        if not capabilities:
            raise ValueError("Capability registry must contain at least one entry")
        self.capabilities = tuple(capabilities)
        self.source = source

    def resolve(self, provider: str, model: str) -> CapabilityMatch:
        candidates = [entry for entry in self.capabilities if entry.matches(provider, model)]
        if not candidates:
            return CapabilityMatch(provider=provider, model=model, capability=None, source="unknown")
        selected = sorted(candidates, key=lambda entry: _pattern_specificity(entry.model_pattern), reverse=True)[0]
        return CapabilityMatch(provider=provider, model=model, capability=selected, source=self.source)

    def require(
        self,
        provider: str,
        model: str,
        *,
        tools: bool = False,
        vision: bool = False,
        reasoning: bool = False,
        web_sidecar: bool = False,
    ) -> CapabilityMatch:
        match = self.resolve(provider, model)
        required = {
            "tools": tools,
            "vision": vision,
            "reasoning": reasoning,
            "web_sidecar": web_sidecar,
        }
        if not any(required.values()):
            return match
        if match.capability is None:
            names = ", ".join(name for name, enabled in required.items() if enabled)
            raise CapabilityError(f"Capabilities are unknown for {provider}/{model}; required: {names}")
        supported = {
            "tools": match.capability.supports_tools,
            "vision": match.capability.supports_vision,
            "reasoning": match.capability.supports_reasoning,
            "web_sidecar": match.capability.supports_web_sidecar,
        }
        missing = [name for name, enabled in required.items() if enabled and not supported[name]]
        if missing:
            raise CapabilityError(
                f"Model {provider}/{model} does not support required capabilities: {', '.join(missing)}"
            )
        return match


def load_capability_registry(path: str | Path | None = None) -> CapabilityRegistry:
    raw_path = path or os.environ.get(CAPABILITIES_CONFIG_ENV)
    if not raw_path:
        return default_model_capability_registry()
    config_path = Path(raw_path).expanduser().resolve()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CapabilityError(f"Model capability config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise CapabilityError(f"Model capability config is not valid JSON: {config_path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != CAPABILITY_SCHEMA_VERSION:
        raise CapabilityError("Model capability config must declare the supported schema_version")
    entries = payload.get("models")
    if not isinstance(entries, list) or not entries:
        raise CapabilityError("Model capability config must contain a non-empty models list")
    allowed = set(ModelCapability.__dataclass_fields__)
    capabilities: list[ModelCapability] = []
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict) or set(raw_entry) - allowed:
            raise CapabilityError(f"Invalid model capability entry at models[{index}]")
        try:
            capabilities.append(ModelCapability(**raw_entry))
        except (TypeError, ValueError) as exc:
            raise CapabilityError(f"Invalid model capability entry at models[{index}]") from exc
    return CapabilityRegistry(capabilities, source="external")


def default_model_capability_registry() -> CapabilityRegistry:
    return CapabilityRegistry(
        [
            ModelCapability(
                provider="mock",
                model_pattern="*",
                protocol="mock",
                supports_tools=True,
                supports_streaming=True,
                supports_vision=False,
                supports_reasoning=True,
                context_window=200_000,
                max_output_tokens=200_000,
                input_price=0.0,
                output_price=0.0,
                supports_web_sidecar=True,
            ),
            ModelCapability(
                provider="openai",
                model_pattern="gpt*",
                protocol="responses",
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                supports_reasoning=True,
                context_window=400_000,
                max_output_tokens=128_000,
                supports_web_sidecar=True,
            ),
            ModelCapability(
                provider="deepseek",
                model_pattern="deepseek-*",
                protocol="chat_completions",
                supports_tools=True,
                supports_streaming=True,
                supports_vision=False,
                supports_reasoning=True,
                context_window=128_000,
                max_output_tokens=8_000,
                input_price=0.14,
                output_price=0.28,
            ),
            ModelCapability(
                provider="litellm_proxy",
                model_pattern="gpt5.5",
                protocol="chat_completions",
                model_family="gpt",
                supports_tools=True,
                supports_streaming=True,
                supports_vision=True,
                supports_reasoning=True,
                supports_web_sidecar=True,
            ),
            ModelCapability(
                provider="litellm_proxy",
                model_pattern="deepseek-v4-pro",
                protocol="chat_completions",
                model_family="deepseek",
                supports_tools=True,
                supports_streaming=True,
                context_window=None,
                max_output_tokens=None,
            ),
            ModelCapability(
                provider="litellm_proxy",
                model_pattern="*",
                protocol="chat_completions",
                supports_tools=True,
                supports_streaming=True,
                context_window=None,
                max_output_tokens=None,
            ),
        ]
    )


def _pattern_specificity(pattern: str) -> tuple[int, int]:
    wildcard_count = sum(pattern.count(marker) for marker in "*?[")
    return (len(pattern) - wildcard_count, len(pattern))
