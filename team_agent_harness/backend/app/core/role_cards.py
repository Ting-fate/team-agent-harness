from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator

from app.core.model_routing import ModelRoute
from app.core.model_runtime import REAL_MODEL_PROVIDER_API_KEY_ENVS, default_reasoning_effort_for_model
from app.core.models import HarnessModel
from app.core.sensitive_text import contains_secret_like_text


ROLE_CARD_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")
MAX_ROLE_CARD_BYTES = 64 * 1024
LOCAL_ROUTING_CONFIG = Path("config/model-routing.local.json")
ROLE_CARD_DIR = Path("config/roles")

_SENSITIVE_FIELD_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)

class RoleCardError(RuntimeError):
    pass


class RoleCardWrite(HarnessModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    color: str = Field(default="", max_length=40)
    emoji: str = Field(default="", max_length=40)
    vibe: str = Field(default="", max_length=500)
    content: str = Field(min_length=1, max_length=MAX_ROLE_CARD_BYTES)

class RoleCard(HarnessModel):
    id: str
    path: str
    frontmatter: dict[str, str] = Field(default_factory=dict)
    content: str = ""


class AgentBindingWrite(HarnessModel):
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0, le=200000)
    reasoning_effort: str | None = Field(default=None, min_length=1)
    role_card_id: str | None = Field(default=None, min_length=1)
    allow_real_calls: bool = False

    @field_validator("role_card_id")
    @classmethod
    def validate_role_card_id(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_role_card_id(value):
            raise ValueError("role_card_id may contain only letters, numbers, underscores, and dashes")
        return value


class AgentBinding(HarnessModel):
    agent_id: str
    provider: str
    model: str
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    role_card_id: str | None = None
    role_file: str | None = None
    allow_real_calls: bool = False
    restart_required: bool = True


def is_valid_role_card_id(value: str) -> bool:
    return bool(ROLE_CARD_ID_PATTERN.fullmatch(value))


def role_card_path(root: Path, role_card_id: str) -> Path:
    if not is_valid_role_card_id(role_card_id):
        raise RoleCardError("role card id may contain only letters, numbers, underscores, and dashes")
    roles_dir = (root / ROLE_CARD_DIR).resolve()
    path = (roles_dir / f"{role_card_id}.md").resolve()
    if not _is_within(path, roles_dir):
        raise RoleCardError("role card path must stay inside config/roles")
    return path


def local_routing_config_path(root: Path) -> Path:
    return (root / LOCAL_ROUTING_CONFIG).resolve()


def list_role_cards(root: Path, include_content: bool = False) -> list[RoleCard]:
    roles_dir = (root / ROLE_CARD_DIR).resolve()
    if not roles_dir.exists():
        return []
    cards = []
    for path in sorted(roles_dir.glob("*.md")):
        card_id = path.stem
        if not is_valid_role_card_id(card_id):
            continue
        cards.append(read_role_card(root, card_id, include_content=include_content))
    return cards


def read_role_card(root: Path, role_card_id: str, include_content: bool = True) -> RoleCard:
    path = role_card_path(root, role_card_id)
    if not path.is_file():
        raise RoleCardError("role card not found")
    if path.stat().st_size > MAX_ROLE_CARD_BYTES:
        raise RoleCardError("role card is too large")
    raw = path.read_text(encoding="utf-8-sig")
    _reject_sensitive_text(raw)
    frontmatter, content = parse_role_card_markdown(raw)
    return RoleCard(
        id=role_card_id,
        path=_relative_to_root(root, path),
        frontmatter=frontmatter,
        content=content if include_content else "",
    )


def write_role_card(root: Path, role_card_id: str, payload: RoleCardWrite) -> RoleCard:
    path = role_card_path(root, role_card_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_sensitive_text(
        {
            "name": payload.name,
            "description": payload.description,
            "color": payload.color,
            "emoji": payload.emoji,
            "vibe": payload.vibe,
            "content": payload.content,
        }
    )
    content = format_role_card_markdown(payload)
    if len(content.encode("utf-8")) > MAX_ROLE_CARD_BYTES:
        raise RoleCardError("role card is too large")
    _atomic_write_text(path, content)
    return read_role_card(root, role_card_id, include_content=True)


def delete_role_card(root: Path, role_card_id: str) -> None:
    path = role_card_path(root, role_card_id)
    if not path.exists():
        raise RoleCardError("role card not found")
    path.unlink()
    remove_role_card_bindings(root, role_card_id)


def parse_role_card_markdown(raw: str) -> tuple[dict[str, str], str]:
    if not raw.startswith("---"):
        return {}, raw.strip()
    lines = raw.splitlines()
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            frontmatter = _parse_simple_frontmatter(lines[1:index])
            return frontmatter, "\n".join(lines[index + 1 :]).strip()
    raise RoleCardError("role card frontmatter is malformed")


def format_role_card_markdown(payload: RoleCardWrite) -> str:
    frontmatter = {
        "name": payload.name,
        "description": payload.description,
        "color": payload.color,
        "emoji": payload.emoji,
        "vibe": payload.vibe,
    }
    lines = ["---"]
    for key, value in frontmatter.items():
        if value:
            lines.append(f"{key}: {_frontmatter_value(value)}")
    lines.extend(["---", "", payload.content.strip(), ""])
    return "\n".join(lines)


def read_agent_bindings(root: Path) -> list[AgentBinding]:
    raw = _read_local_routing_config(root)
    return [
        _binding_from_route(agent_id, route)
        for agent_id, route in sorted(raw.get("agents", {}).items())
        if isinstance(route, dict)
    ]


def upsert_agent_binding(root: Path, agent_id: str, payload: AgentBindingWrite) -> AgentBinding:
    raw = _read_local_routing_config(root)
    agents = raw.setdefault("agents", {})
    if not isinstance(agents, dict):
        raise RoleCardError("local model routing config agents must be an object")

    route: dict[str, Any] = {
        "provider": payload.provider,
        "model": payload.model,
    }
    _reject_sensitive_text(route)
    if payload.temperature is not None:
        route["temperature"] = payload.temperature
    if payload.max_tokens is not None:
        route["max_tokens"] = payload.max_tokens
    route["reasoning_effort"] = payload.reasoning_effort or default_reasoning_effort_for_model(
        payload.provider,
        payload.model,
    )
    if route["reasoning_effort"] is None:
        route.pop("reasoning_effort")
    if payload.role_card_id:
        path = role_card_path(root, payload.role_card_id)
        if not path.is_file():
            raise RoleCardError("role card not found")
        route["role_file"] = f"roles/{payload.role_card_id}.md"
    if payload.allow_real_calls:
        route["allow_real_calls"] = True

    try:
        # Validate provider/model/numeric bounds with the same model used by runtime config loading.
        ModelRoute.model_validate(route)
    except ValidationError as exc:
        raise RoleCardError("agent binding model route is invalid") from exc
    _validate_real_provider_binding(agent_id, route)
    agents[agent_id] = route
    _write_local_routing_config(root, raw)
    return _binding_from_route(agent_id, route)


def remove_role_card_bindings(root: Path, role_card_id: str) -> None:
    raw = _read_local_routing_config(root)
    agents = raw.setdefault("agents", {})
    if not isinstance(agents, dict):
        return
    role_file = f"roles/{role_card_id}.md"
    for route in list(agents.values()):
        if isinstance(route, dict) and route.get("role_file") == role_file:
            route.pop("role_file", None)
    _write_local_routing_config(root, raw)


def delete_agent_binding(root: Path, agent_id: str) -> None:
    raw = _read_local_routing_config(root)
    agents = raw.setdefault("agents", {})
    if isinstance(agents, dict):
        agents.pop(agent_id, None)
    _write_local_routing_config(root, raw)


def _read_local_routing_config(root: Path) -> dict[str, Any]:
    path = local_routing_config_path(root)
    if not path.exists():
        return {"agents": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RoleCardError("local model routing config is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise RoleCardError("local model routing config must be a JSON object")
    raw.setdefault("agents", {})
    return raw


def _write_local_routing_config(root: Path, raw: dict[str, Any]) -> None:
    path = local_routing_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")


def _binding_from_route(agent_id: str, route: dict[str, Any]) -> AgentBinding:
    role_file = route.get("role_file")
    role_card_id = None
    if isinstance(role_file, str) and role_file.startswith("roles/") and role_file.endswith(".md"):
        role_card_id = Path(role_file).stem
    return AgentBinding(
        agent_id=agent_id,
        provider=route.get("provider", ""),
        model=route.get("model", ""),
        temperature=route.get("temperature"),
        max_tokens=route.get("max_tokens"),
        reasoning_effort=route.get("reasoning_effort")
        or default_reasoning_effort_for_model(str(route.get("provider", "")), str(route.get("model", ""))),
        role_card_id=role_card_id,
        role_file=role_file,
        allow_real_calls=bool(route.get("allow_real_calls", False)),
        restart_required=True,
    )


def _validate_real_provider_binding(agent_id: str, route: dict[str, Any]) -> None:
    provider = str(route.get("provider", ""))
    if provider not in REAL_MODEL_PROVIDER_API_KEY_ENVS:
        return
    if os.environ.get("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS") != "1":
        raise RoleCardError(
            f"real provider binding for {agent_id}:{provider} requires TEAM_AGENT_ALLOW_REAL_MODEL_CALLS=1"
        )
    if route.get("allow_real_calls") is not True:
        raise RoleCardError(f"real provider binding for {agent_id}:{provider} requires allow_real_calls=true")
    env_name = REAL_MODEL_PROVIDER_API_KEY_ENVS[provider]
    if not os.environ.get(env_name):
        raise RoleCardError(f"real provider binding for {agent_id}:{provider} requires {env_name}")


def _reject_sensitive_text(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = str(key).lower()
            if any(marker in normalized_key for marker in _SENSITIVE_FIELD_MARKERS):
                raise RoleCardError("role cards and bindings must not contain secret-like fields")
            _reject_sensitive_text(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_text(item)
    elif isinstance(value, str):
        if contains_secret_like_text(value):
            raise RoleCardError("role cards and bindings must not contain secrets, API keys, tokens, or credentials")


def _parse_simple_frontmatter(lines: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _frontmatter_value(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").strip()


def _relative_to_root(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _atomic_write_text(path: Path, content: str) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)
