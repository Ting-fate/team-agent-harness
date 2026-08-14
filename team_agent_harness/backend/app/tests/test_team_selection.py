import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.execution_plan import execution_plan_from_pack
from app.core.model_capabilities import CapabilityRegistry, ModelCapability
from app.core.model_runtime import model_request_from_agent
from app.core.models import AgentDefinition, ArtifactType
from app.core.team_selection import (
    TeamAssignment,
    TeamFallbackRoute,
    TeamModelRoute,
    TeamSelection,
    TeamSelectionError,
    resolve_team_selection,
    team_selection_receipt_from_plan,
)
from app.packs.base import WorkflowPack, WorkflowStep


def _pack() -> WorkflowPack:
    return WorkflowPack(
        name="selection_test",
        description="Fixed two-slot workflow used to verify team selection.",
        agents=[
            AgentDefinition(
                id="selection_test-planner",
                pack_name="selection_test",
                role="Planner",
                system_prompt="Original planner prompt.",
                model_config={"provider": "mock", "model": "mock-planner"},
                tool_permissions=["read_file"],
                runtime_limits={"max_steps": 4, "max_total_tokens": 20_000},
                effective_skill_ids=["skills-planner"],
            ),
            AgentDefinition(
                id="selection_test-reviewer",
                pack_name="selection_test",
                role="Reviewer",
                system_prompt="Original reviewer prompt.",
                model_config={"provider": "mock", "model": "mock-reviewer"},
                tool_permissions=["read_artifact"],
                runtime_limits={"max_steps": 2, "max_total_tokens": 10_000},
                effective_skill_ids=["skills-reviewer"],
            ),
        ],
        steps=[
            WorkflowStep(
                name="plan",
                agent_role="Planner",
                allowed_tools=["read_file"],
                produces_artifact_type=ArtifactType.DESIGN_DOC.value,
            ),
            WorkflowStep(
                name="review",
                agent_role="Reviewer",
                allowed_tools=["read_artifact"],
                depends_on=["plan"],
                required_artifacts=[ArtifactType.DESIGN_DOC.value],
                produces_artifact_type=ArtifactType.FINAL_REPORT.value,
            ),
        ],
        final_artifact_type=ArtifactType.FINAL_REPORT.value,
        max_parallel_steps=1,
        allow_dynamic_execution_plans=False,
    )


def _selection(
    planner_route: TeamModelRoute,
    reviewer_route: TeamModelRoute | None = None,
    *,
    planner_role_card_id: str | None = None,
) -> TeamSelection:
    return TeamSelection(
        pack_name="selection_test",
        assignments=[
            TeamAssignment(
                slot="Planner",
                role_card_id=planner_role_card_id,
                route=planner_route,
            ),
            TeamAssignment(
                slot="Reviewer",
                route=reviewer_route
                or TeamModelRoute(
                    family="deepseek",
                    provider="deepseek",
                    model="deepseek-chat",
                ),
            ),
        ],
    )


@pytest.mark.parametrize(
    ("route", "expected_provider", "expected_model", "expected_family"),
    [
        (
            TeamModelRoute(family="gpt", provider="openai", model="gpt-5"),
            "openai",
            "gpt-5",
            "gpt",
        ),
        (
            TeamModelRoute(
                family="deepseek",
                provider="deepseek",
                model="deepseek-reasoner",
            ),
            "deepseek",
            "deepseek-reasoner",
            "deepseek",
        ),
        (
            TeamModelRoute(
                family="gpt",
                provider="litellm_proxy",
                model="gpt5.5",
            ),
            "litellm_proxy",
            "gpt5.5",
            "gpt",
        ),
    ],
)
def test_resolve_team_selection_accepts_supported_gpt_deepseek_and_litellm_routes(
    tmp_path: Path,
    route: TeamModelRoute,
    expected_provider: str,
    expected_model: str,
    expected_family: str,
) -> None:
    resolved = resolve_team_selection(_pack(), _selection(route), project_root=tmp_path)

    planner = next(agent for agent in resolved.pack.agents if agent.role == "Planner")
    planner_receipt = next(item for item in resolved.receipt.assignments if item.slot == "Planner")
    assert planner.model_settings["provider"] == expected_provider
    assert planner.model_settings["model"] == expected_model
    assert planner.model_settings["reasoning_effort"] == "xhigh"
    assert "family" not in planner.model_settings
    assert planner.model_settings["model_family"] == expected_family
    assert planner.model_settings["team_selection_version"] == "team-selection-v1"
    assert "role_card_id" not in planner.model_settings
    assert planner_receipt.model_family == expected_family
    assert planner_receipt.provider == expected_provider
    assert planner_receipt.model == expected_model
    assert planner_receipt.reasoning_effort == "xhigh"


def test_litellm_team_routes_require_an_exact_matching_family_attestation(
    tmp_path: Path,
) -> None:
    known_deepseek = TeamModelRoute(
        family="deepseek",
        provider="litellm_proxy",
        model="deepseek-v4-pro",
    )
    resolved = resolve_team_selection(
        _pack(),
        _selection(known_deepseek),
        project_root=tmp_path,
    )
    planner = next(agent for agent in resolved.pack.agents if agent.role == "Planner")
    assert planner.model_settings["model_family"] == "deepseek"

    for route in (
        TeamModelRoute(family="deepseek", provider="litellm_proxy", model="gpt5.5"),
        TeamModelRoute(family="gpt", provider="litellm_proxy", model="unattested-alias"),
    ):
        with pytest.raises(TeamSelectionError, match="exact model capability entry"):
            resolve_team_selection(_pack(), _selection(route), project_root=tmp_path)


def test_litellm_team_routes_accept_custom_registry_family_attestation(
    tmp_path: Path,
) -> None:
    registry = CapabilityRegistry(
        [
            ModelCapability(
                provider="litellm_proxy",
                model_pattern="custom-gpt",
                protocol="chat_completions",
                model_family="gpt",
            )
        ],
        source="test",
    )
    route = TeamModelRoute(family="gpt", provider="litellm_proxy", model="custom-gpt")

    resolved = resolve_team_selection(
        _pack(),
        _selection(route),
        project_root=tmp_path,
        capability_registry=registry,
    )

    planner = next(agent for agent in resolved.pack.agents if agent.role == "Planner")
    assert planner.model_settings["model"] == "custom-gpt"


def test_litellm_team_routes_reject_wildcard_family_attestation(tmp_path: Path) -> None:
    registry = CapabilityRegistry(
        [
            ModelCapability(
                provider="litellm_proxy",
                model_pattern="*",
                protocol="chat_completions",
                model_family="gpt",
            )
        ],
        source="test",
    )
    route = TeamModelRoute(family="gpt", provider="litellm_proxy", model="custom-gpt")

    with pytest.raises(TeamSelectionError, match="exact model capability entry"):
        resolve_team_selection(
            _pack(),
            _selection(route),
            project_root=tmp_path,
            capability_registry=registry,
        )


def test_litellm_team_fallback_requires_matching_family_attestation(tmp_path: Path) -> None:
    route = TeamModelRoute(
        family="gpt",
        provider="litellm_proxy",
        model="gpt5.5",
        fallbacks=[
            TeamFallbackRoute(
                family="gpt",
                provider="litellm_proxy",
                model="deepseek-v4-pro",
            )
        ],
    )

    with pytest.raises(TeamSelectionError, match="fallback.*exact model capability entry"):
        resolve_team_selection(_pack(), _selection(route), project_root=tmp_path)


def test_team_selection_requires_every_pack_slot_exactly_once(tmp_path: Path) -> None:
    route = TeamModelRoute(family="gpt", provider="openai", model="gpt-5")
    missing = TeamSelection(
        pack_name="selection_test",
        assignments=[TeamAssignment(slot="Planner", route=route)],
    )
    extra = TeamSelection(
        pack_name="selection_test",
        assignments=[
            TeamAssignment(slot="Planner", route=route),
            TeamAssignment(slot="Reviewer", route=route),
            TeamAssignment(slot="UntrustedExtra", route=route),
        ],
    )

    with pytest.raises(TeamSelectionError, match="missing.*Reviewer"):
        resolve_team_selection(_pack(), missing, project_root=tmp_path)
    with pytest.raises(TeamSelectionError, match="extra.*UntrustedExtra"):
        resolve_team_selection(_pack(), extra, project_root=tmp_path)

    with pytest.raises(ValidationError, match="duplicate agent slots"):
        TeamSelection(
            pack_name="selection_test",
            assignments=[
                TeamAssignment(slot="Planner", route=route),
                TeamAssignment(slot="Planner", route=route),
            ],
        )

    mutated = _selection(route)
    mutated = mutated.model_copy(
        update={
            "assignments": [
                *mutated.assignments,
                TeamAssignment(slot="Planner", route=route),
            ]
        }
    )
    with pytest.raises(TeamSelectionError, match="duplicate agent slots.*Planner"):
        resolve_team_selection(_pack(), mutated, project_root=tmp_path)


def test_team_selection_nested_collections_are_immutable() -> None:
    route = TeamModelRoute(
        family="gpt",
        provider="openai",
        model="gpt-5",
        fallbacks=[
            TeamFallbackRoute(
                family="deepseek",
                provider="deepseek",
                model="deepseek-chat",
            ),
        ],
    )
    selection = _selection(route)

    with pytest.raises(AttributeError):
        selection.assignments.append(selection.assignments[0])
    with pytest.raises(AttributeError):
        route.fallbacks.append(route.fallbacks[0])


def test_team_selection_resolver_revalidates_model_copy_updates(tmp_path: Path) -> None:
    route = TeamModelRoute(family="gpt", provider="openai", model="gpt-5")
    selection = _selection(route)
    invalid_route = route.model_copy(
        update={
            "fallbacks": [
                {
                    "family": "gpt",
                    "provider": "mock",
                    "model": "mock-model",
                }
            ]
        }
    )
    invalid_assignment = selection.assignments[0].model_copy(update={"route": invalid_route})
    invalid_selection = selection.model_copy(
        update={"assignments": [invalid_assignment, *selection.assignments[1:]]}
    )

    with pytest.raises(TeamSelectionError):
        resolve_team_selection(_pack(), invalid_selection, project_root=tmp_path)


@pytest.mark.parametrize(
    "route",
    [
        {"family": "deepseek", "provider": "openai", "model": "deepseek-chat"},
        {"family": "gpt", "provider": "openai", "model": "deepseek-chat"},
        {"family": "gpt", "provider": "deepseek", "model": "gpt-5"},
        {"family": "deepseek", "provider": "deepseek", "model": "gpt-5"},
        {"family": "gpt", "provider": "mock", "model": "gpt-5"},
        {"family": "gpt", "provider": "anthropic", "model": "claude"},
        {"family": "claude", "provider": "litellm_proxy", "model": "claude"},
        {"provider": "litellm_proxy", "model": "gpt5.5"},
    ],
)
def test_team_model_route_rejects_unknown_or_mismatched_family_provider_and_model(
    route: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        TeamModelRoute.model_validate(route)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", 0.1),
        ("max_tokens", 200_000),
        ("input_usd_per_million", 0),
        ("output_usd_per_million", 0),
    ],
)
def test_team_model_route_forbids_client_runtime_and_price_overrides(
    field: str,
    value: object,
) -> None:
    route = {
        "family": "gpt",
        "provider": "openai",
        "model": "gpt-5",
        field: value,
    }

    with pytest.raises(ValidationError, match=field):
        TeamModelRoute.model_validate(route)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("temperature", 0.1),
        ("max_tokens", 200_000),
        ("input_usd_per_million", 0),
        ("output_usd_per_million", 0),
    ],
)
def test_team_fallback_route_forbids_client_runtime_and_price_overrides(
    field: str,
    value: object,
) -> None:
    fallback = {
        "family": "deepseek",
        "provider": "deepseek",
        "model": "deepseek-chat",
        field: value,
    }

    with pytest.raises(ValidationError, match=field):
        TeamFallbackRoute.model_validate(fallback)


def test_team_selection_inherits_trusted_pack_limits_but_not_route_prices(tmp_path: Path) -> None:
    pack = _pack()
    trusted_agents = [
        agent.model_copy(
            update={
                "model_settings": {
                    **agent.model_settings,
                    "temperature": 0.35,
                    "max_tokens": 4321,
                    "input_usd_per_million": 999,
                    "output_usd_per_million": 999,
                }
            },
            deep=True,
        )
        if agent.role == "Planner"
        else agent
        for agent in pack.agents
    ]
    trusted_pack = pack.model_copy(update={"agents": trusted_agents}, deep=True)
    route = TeamModelRoute(
        family="gpt",
        provider="litellm_proxy",
        model="gpt5.5",
        fallbacks=[
            TeamFallbackRoute(
                family="deepseek",
                provider="deepseek",
                model="deepseek-chat",
            )
        ],
    )

    resolved = resolve_team_selection(trusted_pack, _selection(route), project_root=tmp_path)
    planner = next(agent for agent in resolved.pack.agents if agent.role == "Planner")

    assert planner.model_settings["temperature"] == 0.35
    assert planner.model_settings["max_tokens"] == 4321
    assert "input_usd_per_million" not in planner.model_settings
    assert "output_usd_per_million" not in planner.model_settings
    assert "input_usd_per_million" not in planner.model_settings["fallbacks"][0]
    assert "output_usd_per_million" not in planner.model_settings["fallbacks"][0]
    assert team_selection_receipt_from_plan(execution_plan_from_pack(resolved.pack)) == resolved.receipt


def test_fallbacks_are_validated_and_family_metadata_is_only_in_receipt(tmp_path: Path) -> None:
    route = TeamModelRoute(
        family="gpt",
        provider="litellm_proxy",
        model="gpt5.5",
        fallbacks=[
            TeamFallbackRoute(
                family="deepseek",
                provider="deepseek",
                model="deepseek-chat",
            )
        ],
    )

    resolved = resolve_team_selection(_pack(), _selection(route), project_root=tmp_path)
    planner = next(agent for agent in resolved.pack.agents if agent.role == "Planner")
    fallback = planner.model_settings["fallbacks"][0]
    planner_receipt = next(item for item in resolved.receipt.assignments if item.slot == "Planner")

    assert fallback == {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "reason": "team_selection_fallback",
        "allow_real_calls": True,
        "model_family": "deepseek",
    }
    assert "family" not in fallback
    assert planner_receipt.fallbacks[0].model_family == "deepseek"
    assert planner_receipt.fallbacks[0].provider == "deepseek"
    assert planner_receipt.fallbacks[0].model == "deepseek-chat"

    request = model_request_from_agent(
        task_title="Selection test",
        task_goal="Verify route metadata isolation.",
        step_name="plan",
        agent_id=planner.id,
        agent_role=planner.role,
        system_prompt=planner.system_prompt,
        model_config=planner.model_settings,
        allowed_tools=[],
        context={},
    )
    assert request.provider == "litellm_proxy"
    assert request.model == "gpt5.5"
    assert request.fallbacks == [
        {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "reason": "team_selection_fallback",
            "allow_real_calls": True,
        }
    ]
    assert "model_family" not in request.metadata
    assert "team_selection_version" not in request.metadata
    assert "team_selection_manifest_hash" not in request.metadata
    assert "role_card_id" not in request.metadata

    with pytest.raises(ValidationError):
        TeamFallbackRoute.model_validate(
            {"family": "gpt", "provider": "mock", "model": "mock-model"}
        )
    with pytest.raises(ValidationError):
        TeamFallbackRoute.model_validate(
            {"family": "gpt", "provider": "deepseek", "model": "gpt-5"}
        )


def test_role_card_only_overrides_prompt_and_receipt_stays_safe(tmp_path: Path) -> None:
    roles_dir = tmp_path / "config" / "roles"
    roles_dir.mkdir(parents=True)
    roles_dir.joinpath("careful-reviewer.md").write_text(
        "---\nname: Careful Reviewer\n---\n\n# Selected role body\n\nChallenge every assumption.",
        encoding="utf-8",
    )
    pack = _pack()
    before = pack.model_dump()
    resolved = resolve_team_selection(
        pack,
        _selection(
            TeamModelRoute(family="gpt", provider="openai", model="gpt-5"),
            planner_role_card_id="careful-reviewer",
        ),
        project_root=tmp_path,
    )

    planner = next(agent for agent in resolved.pack.agents if agent.role == "Planner")
    original_planner = next(agent for agent in pack.agents if agent.role == "Planner")
    assert planner.system_prompt == "# Selected role body\n\nChallenge every assumption."
    assert planner.model_settings["role_card_id"] == "careful-reviewer"
    assert planner.model_settings["team_selection_version"] == "team-selection-v1"
    assert planner.tool_permissions == original_planner.tool_permissions
    assert planner.runtime_limits == original_planner.runtime_limits
    assert planner.effective_skill_ids == original_planner.effective_skill_ids
    assert resolved.pack.steps == pack.steps
    assert resolved.pack.max_parallel_steps == pack.max_parallel_steps
    assert resolved.pack.allow_dynamic_execution_plans == pack.allow_dynamic_execution_plans
    assert pack.model_dump() == before

    receipt_text = json.dumps(resolved.receipt.model_dump(), sort_keys=True)
    assert "careful-reviewer" in receipt_text
    assert '"model_family": "gpt"' in receipt_text
    assert "Selected role body" not in receipt_text
    assert "Challenge every assumption" not in receipt_text
    assert "system_prompt" not in receipt_text
    assert "role_file" not in receipt_text


def test_role_card_secret_and_secret_like_receipt_fields_fail_closed(tmp_path: Path) -> None:
    roles_dir = tmp_path / "config" / "roles"
    roles_dir.mkdir(parents=True)
    roles_dir.joinpath("unsafe.md").write_text(
        "# Unsafe\n\napi_key=sk-abcdefghijklmno",
        encoding="utf-8",
    )
    route = TeamModelRoute(family="gpt", provider="openai", model="gpt-5")

    with pytest.raises(TeamSelectionError, match="role card"):
        resolve_team_selection(
            _pack(),
            _selection(route, planner_role_card_id="unsafe"),
            project_root=tmp_path,
        )

    unsafe_alias = TeamModelRoute(
        family="gpt",
        provider="litellm_proxy",
        model="sk-abcdefghijklmno",
    )
    with pytest.raises(TeamSelectionError, match="sensitive") as exc_info:
        resolve_team_selection(_pack(), _selection(unsafe_alias), project_root=tmp_path)
    assert unsafe_alias.model not in str(exc_info.value)

    with pytest.raises(ValidationError, match="role_card_id"):
        TeamAssignment(slot="Planner", route=route, role_card_id="../../outside")


def test_team_selection_receipt_round_trips_from_frozen_plan(tmp_path: Path) -> None:
    roles_dir = tmp_path / "config" / "roles"
    roles_dir.mkdir(parents=True)
    roles_dir.joinpath("planner-card.md").write_text(
        "# Planner Card\n\nPlan against explicit acceptance criteria.",
        encoding="utf-8",
    )
    resolved = resolve_team_selection(
        _pack(),
        _selection(
            TeamModelRoute(
                family="gpt",
                provider="litellm_proxy",
                model="gpt5.5",
                fallbacks=[
                    TeamFallbackRoute(
                        family="deepseek",
                        provider="litellm_proxy",
                        model="deepseek-v4-pro",
                    )
                ],
            ),
            planner_role_card_id="planner-card",
        ),
        project_root=tmp_path,
    )
    plan = execution_plan_from_pack(resolved.pack)

    rebuilt = team_selection_receipt_from_plan(plan)

    assert rebuilt == resolved.receipt
    assert rebuilt is not None
    planner = next(item for item in rebuilt.assignments if item.slot == "Planner")
    assert planner.role_card_id == "planner-card"
    assert planner.model_family == "gpt"
    assert planner.fallbacks[0].model_family == "deepseek"


def test_team_selection_receipt_from_plan_distinguishes_default_and_partial_metadata(
    tmp_path: Path,
) -> None:
    assert team_selection_receipt_from_plan(execution_plan_from_pack(_pack())) is None

    resolved = resolve_team_selection(
        _pack(),
        _selection(TeamModelRoute(family="gpt", provider="openai", model="gpt-5")),
        project_root=tmp_path,
    )
    plan = execution_plan_from_pack(resolved.pack)
    second = plan.agent_snapshots[1]
    partial_settings = dict(second.model_settings)
    partial_settings.pop("team_selection_version")
    partial_plan = plan.model_copy(
        update={
            "agent_snapshots": [
                plan.agent_snapshots[0],
                second.model_copy(update={"model_settings": partial_settings}),
            ]
        }
    )

    with pytest.raises(TeamSelectionError, match="partial team selection metadata"):
        team_selection_receipt_from_plan(partial_plan)


def test_team_selection_receipt_from_plan_rejects_missing_snapshots() -> None:
    plan = execution_plan_from_pack(_pack()).model_copy(update={"agent_snapshots": []})

    with pytest.raises(TeamSelectionError, match="missing agent snapshots"):
        team_selection_receipt_from_plan(plan)


def test_team_selection_receipt_distinguishes_reasoning_effort(tmp_path: Path) -> None:
    low = resolve_team_selection(
        _pack(),
        _selection(
            TeamModelRoute(
                family="gpt",
                provider="openai",
                model="gpt-5",
                reasoning_effort="low",
            )
        ),
        project_root=tmp_path,
    )
    high = resolve_team_selection(
        _pack(),
        _selection(
            TeamModelRoute(
                family="gpt",
                provider="openai",
                model="gpt-5",
                reasoning_effort="high",
            )
        ),
        project_root=tmp_path,
    )

    low_receipt = team_selection_receipt_from_plan(execution_plan_from_pack(low.pack))
    high_receipt = team_selection_receipt_from_plan(execution_plan_from_pack(high.pack))
    assert low_receipt is not None and high_receipt is not None
    assert low_receipt.assignments[0].reasoning_effort == "low"
    assert high_receipt.assignments[0].reasoning_effort == "high"
    assert low_receipt != high_receipt


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("team_selection_version", "team-selection-v2"),
        ("model_family", "claude"),
        ("provider", None),
        ("model", None),
    ],
)
def test_team_selection_receipt_from_plan_rejects_invalid_required_metadata(
    tmp_path: Path,
    field: str,
    value: str | None,
) -> None:
    resolved = resolve_team_selection(
        _pack(),
        _selection(TeamModelRoute(family="gpt", provider="openai", model="gpt-5")),
        project_root=tmp_path,
    )
    plan = execution_plan_from_pack(resolved.pack)
    first = plan.agent_snapshots[0]
    settings = dict(first.model_settings)
    if value is None:
        settings.pop(field)
    else:
        settings[field] = value
    invalid_plan = plan.model_copy(
        update={
            "agent_snapshots": [
                first.model_copy(update={"model_settings": settings}),
                *plan.agent_snapshots[1:],
            ]
        }
    )

    with pytest.raises(TeamSelectionError):
        team_selection_receipt_from_plan(invalid_plan)


def test_team_selection_receipt_from_plan_rejects_partial_fallback_metadata(
    tmp_path: Path,
) -> None:
    route = TeamModelRoute(
        family="gpt",
        provider="litellm_proxy",
        model="gpt5.5",
        fallbacks=[
            TeamFallbackRoute(
                family="deepseek",
                provider="litellm_proxy",
                model="deepseek-v4-pro",
            )
        ],
    )
    resolved = resolve_team_selection(_pack(), _selection(route), project_root=tmp_path)
    plan = execution_plan_from_pack(resolved.pack)
    first = plan.agent_snapshots[0]
    settings = dict(first.model_settings)
    fallback = dict(settings["fallbacks"][0])
    fallback.pop("model_family")
    settings["fallbacks"] = [fallback]
    invalid_plan = plan.model_copy(
        update={
            "agent_snapshots": [
                first.model_copy(update={"model_settings": settings}),
                *plan.agent_snapshots[1:],
            ]
        }
    )

    with pytest.raises(TeamSelectionError, match="fallback"):
        team_selection_receipt_from_plan(invalid_plan)


def test_team_selection_receipt_from_plan_rejects_incomplete_snapshot_set(
    tmp_path: Path,
) -> None:
    pack = _pack()
    resolved = resolve_team_selection(
        pack,
        _selection(TeamModelRoute(family="gpt", provider="openai", model="gpt-5")),
        project_root=tmp_path,
    )
    plan = execution_plan_from_pack(resolved.pack)
    missing_snapshot_plan = plan.model_copy(
        update={"agent_snapshots": plan.agent_snapshots[:-1]}
    )
    replaced_snapshot = plan.agent_snapshots[0].model_copy(
        update={"agent_id": "selection_test-untrusted"}
    )
    replaced_agent_plan = plan.model_copy(
        update={
            "agent_snapshots": [
                replaced_snapshot,
                *plan.agent_snapshots[1:],
            ]
        }
    )

    with pytest.raises(TeamSelectionError, match="manifest"):
        team_selection_receipt_from_plan(missing_snapshot_plan)
    with pytest.raises(TeamSelectionError, match="manifest"):
        team_selection_receipt_from_plan(replaced_agent_plan)


def test_team_selection_receipt_from_plan_rejects_stripped_selection_markers(
    tmp_path: Path,
) -> None:
    route = TeamModelRoute(
        family="gpt",
        provider="litellm_proxy",
        model="gpt5.5",
        fallbacks=[
            TeamFallbackRoute(
                family="deepseek",
                provider="deepseek",
                model="deepseek-chat",
            ),
        ],
    )
    resolved = resolve_team_selection(_pack(), _selection(route), project_root=tmp_path)
    plan = execution_plan_from_pack(resolved.pack)
    stripped_snapshots = []
    for snapshot in plan.agent_snapshots:
        settings = dict(snapshot.model_settings)
        settings.pop("team_selection_version")
        stripped_snapshots.append(snapshot.model_copy(update={"model_settings": settings}))
    stripped_plan = plan.model_copy(update={"agent_snapshots": stripped_snapshots})

    with pytest.raises(TeamSelectionError, match="partial team selection metadata"):
        team_selection_receipt_from_plan(stripped_plan)


def test_team_selection_receipt_from_plan_rejects_noncanonical_metadata(
    tmp_path: Path,
) -> None:
    route = TeamModelRoute(
        family="gpt",
        provider="litellm_proxy",
        model="gpt5.5",
        fallbacks=[
            TeamFallbackRoute(
                family="deepseek",
                provider="deepseek",
                model="deepseek-chat",
            ),
            TeamFallbackRoute(
                family="gpt",
                provider="openai",
                model="gpt-5",
            ),
        ],
    )
    resolved = resolve_team_selection(_pack(), _selection(route), project_root=tmp_path)
    plan = execution_plan_from_pack(resolved.pack)
    first = plan.agent_snapshots[0]

    tampered_fallback_settings = dict(first.model_settings)
    tampered_fallback = dict(tampered_fallback_settings["fallbacks"][0])
    tampered_fallback["reason"] = "tampered"
    tampered_fallback["allow_real_calls"] = False
    tampered_fallback_settings["fallbacks"] = [tampered_fallback]
    tampered_fallback_plan = plan.model_copy(
        update={
            "agent_snapshots": [
                first.model_copy(update={"model_settings": tampered_fallback_settings}),
                *plan.agent_snapshots[1:],
            ]
        }
    )

    extra_metadata_settings = dict(first.model_settings)
    extra_metadata_settings["allow_mock_fallback"] = True
    extra_metadata_plan = plan.model_copy(
        update={
            "agent_snapshots": [
                first.model_copy(update={"model_settings": extra_metadata_settings}),
                *plan.agent_snapshots[1:],
            ]
        }
    )

    reordered_fallback_settings = dict(first.model_settings)
    reordered_fallback_settings["fallbacks"] = list(
        reversed(reordered_fallback_settings["fallbacks"])
    )
    reordered_fallback_plan = plan.model_copy(
        update={
            "agent_snapshots": [
                first.model_copy(update={"model_settings": reordered_fallback_settings}),
                *plan.agent_snapshots[1:],
            ]
        }
    )

    missing_fallback_settings = dict(first.model_settings)
    missing_fallback_settings["fallbacks"] = missing_fallback_settings["fallbacks"][:-1]
    missing_fallback_plan = plan.model_copy(
        update={
            "agent_snapshots": [
                first.model_copy(update={"model_settings": missing_fallback_settings}),
                *plan.agent_snapshots[1:],
            ]
        }
    )

    with pytest.raises(TeamSelectionError, match="canonical"):
        team_selection_receipt_from_plan(tampered_fallback_plan)
    with pytest.raises(TeamSelectionError, match="canonical"):
        team_selection_receipt_from_plan(extra_metadata_plan)
    with pytest.raises(TeamSelectionError, match="manifest"):
        team_selection_receipt_from_plan(reordered_fallback_plan)
    with pytest.raises(TeamSelectionError, match="manifest"):
        team_selection_receipt_from_plan(missing_fallback_plan)
