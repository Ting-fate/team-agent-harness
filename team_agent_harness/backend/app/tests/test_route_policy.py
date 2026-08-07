import pytest

from app.core.model_capabilities import default_model_capability_registry
from app.core.provider_health import ProviderHealthRegistry
from app.core.route_policy import RouteCandidate, RouteRequirements, explain_route


def test_route_policy_rejects_all_candidates_without_vision_capability() -> None:
    decision = explain_route(
        [
            RouteCandidate("deepseek", "deepseek-chat", reason="primary"),
            RouteCandidate("mock", "mock-model", reason="fallback"),
        ],
        requirements=RouteRequirements(vision=True),
        capabilities=default_model_capability_registry(),
        configured_providers={"deepseek", "mock"},
        allow_mock_fallback=True,
    )

    assert decision.selected is None
    assert [rejection.reason for rejection in decision.rejected] == [
        "capability_mismatch",
        "capability_mismatch",
    ]


def test_route_policy_never_uses_mock_fallback_without_explicit_opt_in() -> None:
    decision = explain_route(
        [
            RouteCandidate("deepseek", "deepseek-chat"),
            RouteCandidate("mock", "mock-model"),
        ],
        requirements=RouteRequirements(vision=True),
        capabilities=default_model_capability_registry(),
        configured_providers={"deepseek", "mock"},
    )

    assert decision.selected is None
    assert decision.rejected[-1].reason == "mock_fallback_disabled"


def test_route_policy_respects_provider_circuit_state() -> None:
    health = ProviderHealthRegistry(failure_threshold=1, cooldown_seconds=30)
    health.record_failure("deepseek", "timeout", retryable=True)

    decision = explain_route(
        [RouteCandidate("deepseek", "deepseek-chat"), RouteCandidate("mock", "mock-model")],
        requirements=RouteRequirements(),
        capabilities=default_model_capability_registry(),
        configured_providers={"deepseek", "mock"},
        health=health,
        allow_mock_fallback=True,
    )

    assert decision.selected is not None
    assert decision.selected.provider == "mock"
    assert decision.rejected[0].reason == "provider_circuit_open"


def test_route_candidate_mapping_rejects_malformed_values() -> None:
    with pytest.raises(ValueError):
        RouteCandidate("", "model")


@pytest.mark.parametrize("field_name", ["input_usd_per_million", "output_usd_per_million"])
@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf")],
    ids=["nan", "positive-infinity", "negative-infinity"],
)
def test_route_candidate_rejects_non_finite_price_overrides(
    field_name: str,
    value: float,
) -> None:
    prices = {
        "input_usd_per_million": 1.0,
        "output_usd_per_million": 1.0,
        field_name: value,
    }

    with pytest.raises(ValueError, match="non-negative numbers"):
        RouteCandidate("mock", "mock-model", **prices)
