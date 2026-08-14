from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from app.core.execution_plan import ExecutionPlan
from app.core.model_capabilities import CapabilityRegistry, default_model_capability_registry
from app.core.model_routing import ModelFallbackRoute, ModelRoute
from app.core.model_runtime import default_reasoning_effort_for_model
from app.core.models import AgentDefinition, HarnessModel
from app.core.role_cards import RoleCardError, is_valid_role_card_id, read_role_card
from app.core.sensitive_text import contains_secret_like_text
from app.packs.base import WorkflowPack


TEAM_SELECTION_VERSION = "team-selection-v1"
ModelFamily = Literal["gpt", "deepseek"]
TeamModelProvider = Literal["openai", "deepseek", "litellm_proxy"]
_TEAM_SELECTION_MANIFEST_KEY = "team_selection_manifest_hash"

_FROZEN_MODEL_CONFIG = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)
_STRICT_SNAPSHOT_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)


class TeamFallbackRoute(HarnessModel):
    model_config = _FROZEN_MODEL_CONFIG

    family: ModelFamily
    provider: TeamModelProvider
    model: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_target(self) -> "TeamFallbackRoute":
        _validate_family_provider_model(self.family, self.provider, self.model)
        return self


class TeamModelRoute(HarnessModel):
    model_config = _FROZEN_MODEL_CONFIG

    family: ModelFamily
    provider: TeamModelProvider
    model: str = Field(min_length=1, max_length=200)
    reasoning_effort: str | None = Field(default=None, min_length=1)
    fallbacks: tuple[TeamFallbackRoute, ...] = Field(default_factory=tuple, max_length=4)

    @model_validator(mode="after")
    def validate_target(self) -> "TeamModelRoute":
        _validate_family_provider_model(self.family, self.provider, self.model)
        return self


class TeamAssignment(HarnessModel):
    model_config = _FROZEN_MODEL_CONFIG

    slot: str = Field(min_length=1, max_length=200)
    route: TeamModelRoute
    role_card_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("role_card_id")
    @classmethod
    def validate_role_card_id(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_role_card_id(value):
            raise ValueError(
                "role_card_id may contain only letters, numbers, underscores, and dashes"
            )
        return value


class TeamSelection(HarnessModel):
    model_config = _FROZEN_MODEL_CONFIG

    version: Literal["team-selection-v1"] = TEAM_SELECTION_VERSION
    pack_name: str = Field(min_length=1, max_length=100)
    assignments: tuple[TeamAssignment, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_unique_slots(self) -> "TeamSelection":
        duplicates = _duplicates([assignment.slot for assignment in self.assignments])
        if duplicates:
            raise ValueError(f"Team selection has duplicate agent slots: {', '.join(duplicates)}")
        return self


class TeamRouteReceipt(HarnessModel):
    model_config = _FROZEN_MODEL_CONFIG

    model_family: ModelFamily
    provider: TeamModelProvider
    model: str


class TeamAssignmentReceipt(HarnessModel):
    model_config = _FROZEN_MODEL_CONFIG

    slot: str
    agent_id: str
    role_card_id: str | None = None
    model_family: ModelFamily
    provider: TeamModelProvider
    model: str
    reasoning_effort: str = Field(min_length=1)
    fallbacks: tuple[TeamRouteReceipt, ...] = Field(default_factory=tuple)


class TeamSelectionReceipt(HarnessModel):
    model_config = _FROZEN_MODEL_CONFIG

    version: Literal["team-selection-v1"] = TEAM_SELECTION_VERSION
    pack_name: str
    assignments: tuple[TeamAssignmentReceipt, ...]


class _TeamSnapshotFallback(HarnessModel):
    model_config = _STRICT_SNAPSHOT_MODEL_CONFIG

    model_family: ModelFamily
    provider: TeamModelProvider
    model: str = Field(min_length=1, max_length=200)
    reason: Literal["team_selection_fallback"]
    allow_real_calls: Literal[True]

    @model_validator(mode="after")
    def validate_target(self) -> "_TeamSnapshotFallback":
        _validate_family_provider_model(self.model_family, self.provider, self.model)
        return self


class _TeamSnapshotSettings(HarnessModel):
    model_config = _STRICT_SNAPSHOT_MODEL_CONFIG

    provider: TeamModelProvider
    model: str = Field(min_length=1, max_length=200)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0, le=200_000)
    reasoning_effort: str = Field(min_length=1)
    fallbacks: list[_TeamSnapshotFallback] = Field(default_factory=list, max_length=4)
    model_family: ModelFamily
    team_selection_version: Literal["team-selection-v1"]
    team_selection_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    role_card_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("role_card_id")
    @classmethod
    def validate_role_card_id(cls, value: str | None) -> str | None:
        if value is not None and not is_valid_role_card_id(value):
            raise ValueError("role_card_id is invalid")
        return value

    @model_validator(mode="after")
    def validate_target(self) -> "_TeamSnapshotSettings":
        _validate_family_provider_model(self.model_family, self.provider, self.model)
        return self


@dataclass(frozen=True)
class ResolvedTeamSelection:
    pack: WorkflowPack
    receipt: TeamSelectionReceipt


class TeamSelectionError(RuntimeError):
    pass


def resolve_team_selection(
    pack: WorkflowPack,
    selection: TeamSelection,
    *,
    project_root: Path,
    capability_registry: CapabilityRegistry | None = None,
) -> ResolvedTeamSelection:
    try:
        selection = TeamSelection.model_validate(
            selection.model_dump(mode="python", warnings=False)
        )
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]
        detail = str(first_error.get("msg", "Team selection validation failed."))
        detail = detail.removeprefix("Value error, ")
        raise TeamSelectionError(detail) from exc
    except (AttributeError, TypeError) as exc:
        raise TeamSelectionError("Team selection is invalid.") from exc

    if selection.pack_name != pack.name:
        raise TeamSelectionError("Team selection pack_name does not match the selected workflow Pack.")

    supplied_slot_list = [assignment.slot for assignment in selection.assignments]
    duplicate_slots = _duplicates(supplied_slot_list)
    if duplicate_slots:
        raise TeamSelectionError(
            f"Team selection has duplicate agent slots: {', '.join(duplicate_slots)}"
        )
    assignments_by_slot = {assignment.slot: assignment for assignment in selection.assignments}
    expected_slots = {agent.role for agent in pack.agents}
    supplied_slots = set(assignments_by_slot)
    missing_slots = sorted(expected_slots - supplied_slots)
    extra_slots = sorted(supplied_slots - expected_slots)
    if missing_slots or extra_slots:
        details: list[str] = []
        if missing_slots:
            details.append(f"missing slots: {', '.join(missing_slots)}")
        if extra_slots:
            details.append(f"extra slots: {', '.join(extra_slots)}")
        raise TeamSelectionError(
            "Team selection must cover every workflow Pack agent slot exactly once; " + "; ".join(details)
        )

    active_capability_registry = capability_registry or default_model_capability_registry()
    resolved_agents: list[AgentDefinition] = []
    receipt_assignments: list[TeamAssignmentReceipt] = []
    for agent in pack.agents:
        assignment = assignments_by_slot[agent.role]
        model_route = _validated_model_route(
            assignment,
            agent,
            agent.role,
            capability_registry=active_capability_registry,
        )
        model_settings = _model_settings_from_route(model_route, assignment.route)
        model_settings["team_selection_version"] = TEAM_SELECTION_VERSION
        if assignment.role_card_id is not None:
            model_settings["role_card_id"] = assignment.role_card_id
        system_prompt = _role_prompt(
            project_root=project_root,
            role_card_id=assignment.role_card_id,
            slot=agent.role,
            default_prompt=agent.system_prompt,
        )
        resolved_agents.append(
            agent.model_copy(
                update={
                    "system_prompt": system_prompt,
                    "model_settings": model_settings,
                },
                deep=True,
            )
        )
        receipt_assignments.append(
            TeamAssignmentReceipt(
                slot=agent.role,
                agent_id=agent.id,
                role_card_id=assignment.role_card_id,
                model_family=assignment.route.family,
                provider=assignment.route.provider,
                model=assignment.route.model,
                reasoning_effort=str(model_settings["reasoning_effort"]),
                fallbacks=[
                    TeamRouteReceipt(
                        model_family=fallback.family,
                        provider=fallback.provider,
                        model=fallback.model,
                    )
                    for fallback in assignment.route.fallbacks
                ],
            )
        )

    receipt = TeamSelectionReceipt(
        pack_name=pack.name,
        assignments=receipt_assignments,
    )
    _reject_sensitive_receipt(receipt)
    manifest_hash = _team_selection_manifest_hash(
        pack.name,
        [
            (agent.role, agent.id, agent.model_settings)
            for agent in resolved_agents
        ],
    )
    resolved_agents = [
        agent.model_copy(
            update={
                "model_settings": {
                    **agent.model_settings,
                    _TEAM_SELECTION_MANIFEST_KEY: manifest_hash,
                }
            },
            deep=True,
        )
        for agent in resolved_agents
    ]
    resolved_pack = pack.model_copy(update={"agents": resolved_agents}, deep=True)
    return ResolvedTeamSelection(pack=resolved_pack, receipt=receipt)


def team_selection_receipt_from_plan(
    plan: ExecutionPlan,
) -> TeamSelectionReceipt | None:
    snapshots = plan.agent_snapshots
    if not snapshots:
        raise TeamSelectionError("Execution plan is missing agent snapshots.")
    if any(not isinstance(snapshot.model_settings, dict) for snapshot in snapshots):
        raise TeamSelectionError("Execution plan contains invalid team selection model metadata.")

    version_markers = [
        "team_selection_version" in snapshot.model_settings
        for snapshot in snapshots
    ]
    manifest_markers = [
        _TEAM_SELECTION_MANIFEST_KEY in snapshot.model_settings
        for snapshot in snapshots
    ]
    if not any(version_markers) and not any(manifest_markers):
        if any(_has_team_selection_residue(snapshot.model_settings) for snapshot in snapshots):
            raise TeamSelectionError("Execution plan contains partial team selection metadata.")
        return None
    if not all(version_markers) or not all(manifest_markers):
        raise TeamSelectionError("Execution plan contains partial team selection metadata.")

    duplicate_slots = _duplicates([snapshot.role for snapshot in snapshots])
    duplicate_agent_ids = _duplicates([snapshot.agent_id for snapshot in snapshots])
    if duplicate_slots or duplicate_agent_ids:
        raise TeamSelectionError("Execution plan contains duplicate team selection snapshots.")

    assignments: list[TeamAssignmentReceipt] = []
    canonical_snapshots: list[tuple[str, str, dict[str, object]]] = []
    for snapshot in snapshots:
        settings = snapshot.model_settings
        selected_assignment, canonical_settings = _assignment_from_snapshot(
            settings,
            snapshot.role,
        )
        selected_route = selected_assignment.route
        canonical_snapshots.append((snapshot.role, snapshot.agent_id, canonical_settings))
        assignments.append(
            TeamAssignmentReceipt(
                slot=snapshot.role,
                agent_id=snapshot.agent_id,
                role_card_id=selected_assignment.role_card_id,
                model_family=selected_route.family,
                provider=selected_route.provider,
                model=selected_route.model,
                reasoning_effort=selected_route.reasoning_effort,
                fallbacks=[
                    TeamRouteReceipt(
                        model_family=fallback.family,
                        provider=fallback.provider,
                        model=fallback.model,
                    )
                    for fallback in selected_route.fallbacks
                ],
            )
        )

    manifest_values = {
        snapshot.model_settings[_TEAM_SELECTION_MANIFEST_KEY]
        for snapshot in snapshots
    }
    expected_manifest = _team_selection_manifest_hash(plan.workflow_pack, canonical_snapshots)
    if manifest_values != {expected_manifest}:
        raise TeamSelectionError(
            "Execution plan contains non-canonical team selection metadata; "
            "the manifest does not match."
        )

    try:
        receipt = TeamSelectionReceipt(
            pack_name=plan.workflow_pack,
            assignments=assignments,
        )
    except ValidationError as exc:
        raise TeamSelectionError("Execution plan contains invalid team selection receipt metadata.") from exc
    _reject_sensitive_receipt(receipt)
    return receipt


def _validate_family_provider_model(
    family: ModelFamily,
    provider: TeamModelProvider,
    model: str,
) -> None:
    if provider == "openai":
        if family != "gpt":
            raise ValueError("Direct openai routes require family=gpt")
        if not model.startswith("gpt"):
            raise ValueError("Direct openai model names must start with gpt")
    elif provider == "deepseek":
        if family != "deepseek":
            raise ValueError("Direct deepseek routes require family=deepseek")
        if not model.startswith("deepseek-"):
            raise ValueError("Direct deepseek model names must start with deepseek-")


def _assignment_from_snapshot(
    settings: dict[str, object],
    slot: str,
) -> tuple[TeamAssignment, dict[str, object]]:
    try:
        parsed = _TeamSnapshotSettings.model_validate(settings)
    except ValidationError as exc:
        fallback_error = any(error["loc"] and error["loc"][0] == "fallbacks" for error in exc.errors())
        label = " fallback" if fallback_error else ""
        raise TeamSelectionError(
            f"Execution plan contains invalid canonical team selection{label} metadata "
            f"for slot {slot}."
        ) from exc

    selected_route = TeamModelRoute(
        family=parsed.model_family,
        provider=parsed.provider,
        model=parsed.model,
        reasoning_effort=parsed.reasoning_effort,
        fallbacks=[
            TeamFallbackRoute(
                family=fallback.model_family,
                provider=fallback.provider,
                model=fallback.model,
            )
            for fallback in parsed.fallbacks
        ],
    )
    selected_assignment = TeamAssignment(
        slot=slot,
        route=selected_route,
        role_card_id=parsed.role_card_id,
    )
    canonical_settings = _model_settings_from_route(
        _validated_snapshot_model_route(parsed, slot),
        selected_route,
    )
    canonical_settings["team_selection_version"] = TEAM_SELECTION_VERSION
    canonical_settings[_TEAM_SELECTION_MANIFEST_KEY] = parsed.team_selection_manifest_hash
    if parsed.role_card_id is not None:
        canonical_settings["role_card_id"] = parsed.role_card_id
    if settings != canonical_settings:
        raise TeamSelectionError(
            f"Execution plan contains non-canonical team selection metadata for slot {slot}."
        )
    return selected_assignment, canonical_settings


def _validated_model_route(
    assignment: TeamAssignment,
    trusted_agent: AgentDefinition,
    slot: str,
    *,
    capability_registry: CapabilityRegistry,
) -> ModelRoute:
    _require_litellm_family_attestation(
        family=assignment.route.family,
        provider=assignment.route.provider,
        model=assignment.route.model,
        slot=slot,
        capability_registry=capability_registry,
        fallback=False,
    )
    for fallback in assignment.route.fallbacks:
        _require_litellm_family_attestation(
            family=fallback.family,
            provider=fallback.provider,
            model=fallback.model,
            slot=slot,
            capability_registry=capability_registry,
            fallback=True,
        )
    try:
        fallbacks = [
            ModelFallbackRoute(
                provider=fallback.provider,
                model=fallback.model,
                reason="team_selection_fallback",
                allow_real_calls=True,
            )
            for fallback in assignment.route.fallbacks
        ]
        return ModelRoute(
            provider=assignment.route.provider,
            model=assignment.route.model,
            temperature=trusted_agent.model_settings.get("temperature"),
            max_tokens=trusted_agent.model_settings.get("max_tokens"),
            reasoning_effort=assignment.route.reasoning_effort,
            allow_real_calls=True,
            allow_mock_fallback=False,
            fallbacks=fallbacks,
        )
    except ValidationError as exc:
        raise TeamSelectionError(f"Model route for slot {slot} is invalid.") from exc


def _require_litellm_family_attestation(
    *,
    family: ModelFamily,
    provider: TeamModelProvider,
    model: str,
    slot: str,
    capability_registry: CapabilityRegistry,
    fallback: bool,
) -> None:
    if contains_secret_like_text(model):
        raise TeamSelectionError("Team selection contains sensitive-looking metadata.")
    if provider != "litellm_proxy":
        return
    capability = capability_registry.resolve(provider, model).capability
    if (
        capability is not None
        and capability.model_pattern == model
        and capability.model_family == family
    ):
        return
    route_label = "fallback alias" if fallback else "alias"
    raise TeamSelectionError(
        f"LiteLLM {route_label} for slot {slot} is not attested for family={family}; "
        "add an exact model capability entry with the matching model_family."
    )


def _validated_snapshot_model_route(
    settings: _TeamSnapshotSettings,
    slot: str,
) -> ModelRoute:
    try:
        return ModelRoute(
            provider=settings.provider,
            model=settings.model,
            temperature=settings.temperature,
            max_tokens=settings.max_tokens,
            reasoning_effort=settings.reasoning_effort,
            allow_real_calls=True,
            allow_mock_fallback=False,
            fallbacks=[
                ModelFallbackRoute(
                    provider=fallback.provider,
                    model=fallback.model,
                    reason="team_selection_fallback",
                    allow_real_calls=True,
                )
                for fallback in settings.fallbacks
            ],
        )
    except ValidationError as exc:
        raise TeamSelectionError(
            f"Execution plan contains invalid canonical team selection metadata for slot {slot}."
        ) from exc


def _model_settings_from_route(
    route: ModelRoute,
    selected_route: TeamModelRoute,
) -> dict[str, object]:
    model_settings = route.model_dump(
        exclude={"allow_real_calls", "allow_mock_fallback", "role_file"},
        exclude_none=True,
    )
    if not route.fallbacks:
        model_settings.pop("fallbacks", None)
    else:
        fallback_settings = model_settings["fallbacks"]
        if not isinstance(fallback_settings, list):
            raise TeamSelectionError("Validated model fallbacks did not produce a list.")
        for item, selected_fallback in zip(
            fallback_settings,
            selected_route.fallbacks,
            strict=True,
        ):
            if not isinstance(item, dict):
                raise TeamSelectionError("Validated model fallback did not produce an object.")
            item["model_family"] = selected_fallback.family
    model_settings["model_family"] = selected_route.family
    model_settings.setdefault(
        "reasoning_effort",
        default_reasoning_effort_for_model(route.provider, route.model),
    )
    return {key: value for key, value in model_settings.items() if value is not None}


def _role_prompt(
    *,
    project_root: Path,
    role_card_id: str | None,
    slot: str,
    default_prompt: str,
) -> str:
    if role_card_id is None:
        return default_prompt
    try:
        role_card = read_role_card(project_root, role_card_id, include_content=True)
    except (RoleCardError, OSError, UnicodeError) as exc:
        raise TeamSelectionError(f"The role card for slot {slot} could not be loaded.") from exc
    prompt = role_card.content.strip()
    if not prompt:
        raise TeamSelectionError(f"The role card for slot {slot} has no prompt body.")
    return prompt


def _team_selection_manifest_hash(
    pack_name: str,
    snapshots: list[tuple[str, str, dict[str, object]]],
) -> str:
    payload = {
        "pack_name": pack_name,
        "agents": [
            {
                "slot": slot,
                "agent_id": agent_id,
                "model_settings": {
                    key: value
                    for key, value in settings.items()
                    if key != _TEAM_SELECTION_MANIFEST_KEY
                },
            }
            for slot, agent_id, settings in snapshots
        ],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _has_team_selection_residue(settings: dict[str, object]) -> bool:
    if "role_card_id" in settings or "model_family" in settings:
        return True
    fallbacks = settings.get("fallbacks", [])
    return isinstance(fallbacks, list) and any(
        isinstance(fallback, dict)
        and fallback.get("reason") == "team_selection_fallback"
        for fallback in fallbacks
    )


def _reject_sensitive_receipt(receipt: TeamSelectionReceipt) -> None:
    for value in _receipt_strings(receipt.model_dump()):
        if contains_secret_like_text(value):
            raise TeamSelectionError("Team selection receipt contains sensitive-looking metadata.")


def _receipt_strings(value: object) -> list[str]:
    if isinstance(value, dict):
        return [item for nested in value.values() for item in _receipt_strings(nested)]
    if isinstance(value, (list, tuple)):
        return [item for nested in value for item in _receipt_strings(nested)]
    return [value] if isinstance(value, str) else []


def _duplicates(values: list[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)
