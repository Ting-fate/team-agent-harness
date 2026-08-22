from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, model_validator

from app.core.models import AgentDefinition, HarnessModel
from app.core.model_runtime import (
    REAL_MODEL_PROVIDER_API_KEY_ENVS,
    ROUTABLE_MODEL_PROVIDERS,
    default_reasoning_effort_for_model,
    gpt_route_mode,
    model_provider_credentials_configured,
)
from app.core.sensitive_text import contains_secret_like_text
from app.packs.base import WorkflowPack


ROUTING_CONFIG_ENV = "TEAM_AGENT_MODEL_ROUTING_CONFIG"

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_MAX_ROLE_FILE_BYTES = 64 * 1024

_ALLOWED_FIELD_NAMES = {
    "agents",
    "provider",
    "model",
    "temperature",
    "max_tokens",
    "continuation_attempts",
    "reasoning_effort",
    "allow_real_calls",
    "allow_mock_fallback",
    "fallbacks",
    "input_usd_per_million",
    "output_usd_per_million",
    "reason",
    "role_file",
}

REASONING_EFFORT_VALUES = {"minimal", "low", "medium", "high", "xhigh"}

_FORBIDDEN_FIELD_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "headers",
    "password",
    "secret",
    "token",
)


class ModelFallbackRoute(HarnessModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reason: str = Field(default="fallback", min_length=1, max_length=128)
    allow_real_calls: bool = False
    input_usd_per_million: float | None = Field(default=None, ge=0)
    output_usd_per_million: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_fallback(self) -> "ModelFallbackRoute":
        if self.provider not in ROUTABLE_MODEL_PROVIDERS:
            raise ValueError(f"Unsupported model provider in routing config: {self.provider}")
        _validate_price_pair(self.input_usd_per_million, self.output_usd_per_million)
        return self


class ModelRoute(HarnessModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0, le=200000)
    continuation_attempts: int = Field(default=0, ge=0, le=3)
    reasoning_effort: str | None = Field(default=None, min_length=1)
    allow_real_calls: bool = False
    allow_mock_fallback: bool = False
    input_usd_per_million: float | None = Field(default=None, ge=0)
    output_usd_per_million: float | None = Field(default=None, ge=0)
    fallbacks: list[ModelFallbackRoute] = Field(default_factory=list, max_length=4)
    role_file: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_route(self) -> "ModelRoute":
        if self.provider not in ROUTABLE_MODEL_PROVIDERS:
            raise ValueError(f"Unsupported model provider in routing config: {self.provider}")
        if self.temperature is not None and not math.isfinite(self.temperature):
            raise ValueError("temperature must be finite")
        if self.reasoning_effort is not None and self.reasoning_effort not in REASONING_EFFORT_VALUES:
            raise ValueError("reasoning_effort must be one of minimal, low, medium, high, xhigh")
        _validate_price_pair(self.input_usd_per_million, self.output_usd_per_million)
        targets = [(self.provider, self.model), *[(item.provider, item.model) for item in self.fallbacks]]
        if len(set(targets)) != len(targets):
            raise ValueError("Model route candidates must not contain duplicate provider/model targets")
        if any(item.provider == "mock" for item in self.fallbacks) and not self.allow_mock_fallback:
            raise ValueError("Mock fallback requires allow_mock_fallback=true")
        return self


class ModelRoutingConfig(HarnessModel):
    agents: dict[str, ModelRoute] = Field(default_factory=dict)


class ModelRoutingError(RuntimeError):
    pass


def _validate_price_pair(input_price: float | None, output_price: float | None) -> None:
    if (input_price is None) != (output_price is None):
        raise ValueError("input/output model prices must be configured together")
    if any(value is not None and not math.isfinite(value) for value in (input_price, output_price)):
        raise ValueError("model prices must be finite")


def load_model_routing_config(path: str | Path | None = None) -> ModelRoutingConfig:
    raw_path = path or os.environ.get(ROUTING_CONFIG_ENV)
    if not raw_path:
        return ModelRoutingConfig()

    config_path = Path(raw_path).expanduser().resolve()
    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ModelRoutingError(f"Model routing config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ModelRoutingError(f"Model routing config is not valid JSON: {config_path}") from exc

    _reject_sensitive_fields(raw_config)
    raw_config = _normalize_role_file_paths(raw_config, config_path)
    try:
        return ModelRoutingConfig.model_validate(raw_config)
    except ValidationError as exc:
        raise ModelRoutingError("Model routing config is invalid. Check provider, model, and numeric bounds.") from exc


def apply_model_routing_config(
    packs: dict[str, WorkflowPack],
    routing: ModelRoutingConfig,
) -> dict[str, WorkflowPack]:
    if not routing.agents:
        return packs

    known_agents = {agent.id for pack in packs.values() for agent in pack.agents}
    unknown_agents = sorted(set(routing.agents) - known_agents)
    if unknown_agents:
        raise ModelRoutingError(f"Model routing config references unknown agents: {', '.join(unknown_agents)}")

    effective_routing = _routing_for_gpt_mode(routing)
    _validate_real_provider_opt_in(effective_routing)

    return {
        pack_name: pack.model_copy(
            update={
                "agents": [
                    _apply_agent_route(agent, effective_routing.agents.get(agent.id))
                    for agent in pack.agents
                ]
            }
        )
        for pack_name, pack in packs.items()
    }


def _routing_for_gpt_mode(routing: ModelRoutingConfig) -> ModelRoutingConfig:
    if gpt_route_mode() != "direct":
        return routing

    def remap_fallback(fallback: ModelFallbackRoute) -> ModelFallbackRoute:
        if fallback.provider == "litellm_proxy" and fallback.model in {"gpt5.5", "gpt5.6-sol"}:
            return fallback.model_copy(
                update={
                    "provider": "gpt_relay",
                    "model": "gpt-5.6-sol" if fallback.model == "gpt5.6-sol" else "gpt-5.5",
                }
            )
        return fallback

    remapped: dict[str, ModelRoute] = {}
    for agent_id, route in routing.agents.items():
        update: dict[str, Any] = {
            "fallbacks": [remap_fallback(fallback) for fallback in route.fallbacks]
        }
        if route.provider == "litellm_proxy" and route.model in {"gpt5.5", "gpt5.6-sol"}:
            update["provider"] = "gpt_relay"
            update["model"] = "gpt-5.6-sol" if route.model == "gpt5.6-sol" else "gpt-5.5"
        remapped[agent_id] = route.model_copy(update=update)
    return ModelRoutingConfig(agents=remapped)


def _apply_agent_route(agent: AgentDefinition, route: ModelRoute | None) -> AgentDefinition:
    if route is None:
        return agent
    model_settings = route.model_dump(exclude={"allow_real_calls", "role_file"}, exclude_none=True)
    if not route.allow_mock_fallback:
        model_settings.pop("allow_mock_fallback", None)
    if not route.fallbacks:
        model_settings.pop("fallbacks", None)
    if model_settings.get("continuation_attempts") == 0:
        model_settings.pop("continuation_attempts", None)
    model_settings.setdefault(
        "reasoning_effort",
        default_reasoning_effort_for_model(route.provider, route.model),
    )
    model_settings = {key: value for key, value in model_settings.items() if value is not None}
    update: dict[str, Any] = {
        "model_settings": model_settings
    }
    if route.role_file:
        update["system_prompt"] = _read_role_prompt(Path(route.role_file))
    return agent.model_copy(
        update=update
    )


def _validate_real_provider_opt_in(routing: ModelRoutingConfig) -> None:
    real_routes: list[tuple[str, str, bool]] = []
    for agent_id, route in routing.agents.items():
        if route.provider in REAL_MODEL_PROVIDER_API_KEY_ENVS:
            real_routes.append((agent_id, route.provider, route.allow_real_calls))
        real_routes.extend(
            (f"{agent_id}.fallbacks[{index}]", fallback.provider, fallback.allow_real_calls)
            for index, fallback in enumerate(route.fallbacks)
            if fallback.provider in REAL_MODEL_PROVIDER_API_KEY_ENVS
        )
    if not real_routes:
        return

    if os.environ.get("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS") != "1":
        providers = sorted({provider for _, provider, _ in real_routes})
        raise ModelRoutingError(
            "Model routing config enables real providers "
            f"({', '.join(providers)}) but TEAM_AGENT_ALLOW_REAL_MODEL_CALLS is not set to 1."
        )

    unapproved = sorted(
        f"{route_id}:{provider}"
        for route_id, provider, allow_real_calls in real_routes
        if not allow_real_calls
    )
    if unapproved:
        raise ModelRoutingError(
            "Model routing config enables real providers without per-agent allow_real_calls=true: "
            f"{', '.join(unapproved)}"
        )

    missing = sorted(
        f"{route_id}:{provider}:{REAL_MODEL_PROVIDER_API_KEY_ENVS[provider]}"
        for route_id, provider, _ in real_routes
        if not model_provider_credentials_configured(provider)
    )
    if missing:
        raise ModelRoutingError(f"Model routing config enables providers without credentials: {', '.join(missing)}")


def _reject_sensitive_fields(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized not in _ALLOWED_FIELD_NAMES and any(marker in normalized for marker in _FORBIDDEN_FIELD_MARKERS):
                raise ModelRoutingError(f"Model routing config must not contain sensitive field: {path}.{key}")
            _reject_sensitive_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_sensitive_fields(item, f"{path}[{index}]")


def _normalize_role_file_paths(value: Any, config_path: Path) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("agents"), dict):
        return value

    updated = dict(value)
    updated_agents: dict[str, Any] = {}
    for agent_id, route in value["agents"].items():
        if not isinstance(route, dict) or "role_file" not in route or route["role_file"] is None:
            updated_agents[agent_id] = route
            continue
        updated_route = dict(route)
        updated_route["role_file"] = str(_resolve_role_file_path(route["role_file"], config_path))
        updated_agents[agent_id] = updated_route
    updated["agents"] = updated_agents
    return updated


def _resolve_role_file_path(value: Any, config_path: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ModelRoutingError("role_file must be a non-empty markdown path.")

    raw_role_path = Path(value.strip()).expanduser()
    if raw_role_path.suffix.lower() != ".md":
        raise ModelRoutingError("role_file must point to a .md file.")

    config_dir = config_path.parent.resolve()
    candidates = (
        [raw_role_path]
        if raw_role_path.is_absolute()
        else [config_dir / raw_role_path, _PROJECT_ROOT / raw_role_path]
    )
    role_path = next((candidate.resolve() for candidate in candidates if candidate.exists()), candidates[0].resolve())

    if not (_is_within(role_path, config_dir) or _is_within(role_path, _PROJECT_ROOT)):
        raise ModelRoutingError("role_file must stay inside the routing config directory or project root.")
    if not role_path.is_file():
        raise ModelRoutingError(f"role_file not found or not a file: {role_path}")
    return role_path


def _read_role_prompt(role_path: Path) -> str:
    try:
        if role_path.stat().st_size > _MAX_ROLE_FILE_BYTES:
            raise ModelRoutingError("role_file is too large; keep role prompts under 64KB.")
        content = role_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ModelRoutingError(f"role_file could not be read: {role_path}") from exc

    if contains_secret_like_text(content):
        raise ModelRoutingError("role_file contains secret-like content and cannot be loaded.")
    prompt = _strip_frontmatter(content).strip()
    if not prompt:
        raise ModelRoutingError(f"role_file is empty after frontmatter: {role_path}")
    return prompt


def _strip_frontmatter(content: str) -> str:
    if not content.startswith("---"):
        return content
    lines = content.splitlines()
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :])
    return content


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
