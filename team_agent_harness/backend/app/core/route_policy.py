from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Callable, Iterable

from app.core.model_capabilities import CapabilityError, CapabilityRegistry
from app.core.provider_health import ProviderHealthRegistry


class RoutePolicyError(ValueError):
    pass


@dataclass(frozen=True)
class RouteCandidate:
    provider: str
    model: str
    reason: str = "configured"
    allow_real_calls: bool = False
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None

    def __post_init__(self) -> None:
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("Route candidate provider and model must be non-empty")
        if type(self.allow_real_calls) is not bool:
            raise ValueError("Route candidate allow_real_calls must be boolean")
        prices = (self.input_usd_per_million, self.output_usd_per_million)
        if (prices[0] is None) != (prices[1] is None):
            raise ValueError("Route candidate prices must be configured as an input/output pair")
        if any(
            value is not None
            and (type(value) not in {int, float} or value < 0 or not math.isfinite(value))
            for value in prices
        ):
            raise ValueError("Route candidate prices must be non-negative numbers")

    @classmethod
    def from_mapping(cls, value: Any, *, default_reason: str = "fallback") -> "RouteCandidate":
        if not isinstance(value, dict):
            raise RoutePolicyError("Route candidate must be an object")
        provider = value.get("provider")
        model = value.get("model")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise RoutePolicyError("Route candidate provider and model must be strings")
        reason = value.get("reason", default_reason)
        allow_real_calls = value.get("allow_real_calls", False)
        input_price = value.get("input_usd_per_million")
        output_price = value.get("output_usd_per_million")
        if not isinstance(reason, str) or not isinstance(allow_real_calls, bool):
            raise RoutePolicyError("Route candidate reason and allow_real_calls are invalid")
        return cls(
            provider=provider.strip(),
            model=model.strip(),
            reason=reason.strip() or default_reason,
            allow_real_calls=allow_real_calls,
            input_usd_per_million=input_price,
            output_usd_per_million=output_price,
        )

    def public_dict(self) -> dict[str, Any]:
        payload = {
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "allow_real_calls": self.allow_real_calls,
        }
        if self.input_usd_per_million is not None:
            payload["input_usd_per_million"] = self.input_usd_per_million
            payload["output_usd_per_million"] = self.output_usd_per_million
        return payload


@dataclass(frozen=True)
class RouteRequirements:
    tools: bool = False
    vision: bool = False
    reasoning: bool = False
    web_sidecar: bool = False

    def public_dict(self) -> dict[str, bool]:
        return {
            "tools": self.tools,
            "vision": self.vision,
            "reasoning": self.reasoning,
            "web_sidecar": self.web_sidecar,
        }


@dataclass(frozen=True)
class RouteRejection:
    candidate: RouteCandidate
    reason: str

    def public_dict(self) -> dict[str, Any]:
        return {"candidate": self.candidate.public_dict(), "reason": self.reason}


@dataclass(frozen=True)
class RouteDecision:
    selected: RouteCandidate | None
    considered: tuple[RouteCandidate, ...]
    rejected: tuple[RouteRejection, ...]
    requirements: RouteRequirements

    @property
    def usable(self) -> bool:
        return self.selected is not None

    def public_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected.public_dict() if self.selected else None,
            "considered": [item.public_dict() for item in self.considered],
            "rejected": [item.public_dict() for item in self.rejected],
            "requirements": self.requirements.public_dict(),
        }


def explain_route(
    candidates: Iterable[RouteCandidate],
    *,
    requirements: RouteRequirements,
    capabilities: CapabilityRegistry,
    configured_providers: set[str],
    health: ProviderHealthRegistry | None = None,
    allow_mock_fallback: bool = False,
    provider_ready: Callable[[RouteCandidate], bool] | None = None,
) -> RouteDecision:
    considered = tuple(candidates)
    rejected: list[RouteRejection] = []
    selected: RouteCandidate | None = None
    for index, candidate in enumerate(considered):
        if candidate.provider not in configured_providers:
            rejected.append(RouteRejection(candidate, "provider_not_configured"))
            continue
        if candidate.provider == "mock" and index > 0 and not allow_mock_fallback:
            rejected.append(RouteRejection(candidate, "mock_fallback_disabled"))
            continue
        if health is not None and health.is_circuit_open(candidate.provider):
            rejected.append(RouteRejection(candidate, "provider_circuit_open"))
            continue
        if provider_ready is not None and not provider_ready(candidate):
            rejected.append(RouteRejection(candidate, "provider_not_ready"))
            continue
        try:
            capabilities.require(
                candidate.provider,
                candidate.model,
                tools=requirements.tools,
                vision=requirements.vision,
                reasoning=requirements.reasoning,
                web_sidecar=requirements.web_sidecar,
            )
        except CapabilityError:
            rejected.append(RouteRejection(candidate, "capability_mismatch"))
            continue
        selected = candidate
        break
    return RouteDecision(
        selected=selected,
        considered=considered,
        rejected=tuple(rejected),
        requirements=requirements,
    )


def route_candidates_from_request(
    provider: str,
    model: str,
    fallback_values: list[dict[str, Any]] | None = None,
    *,
    input_usd_per_million: float | None = None,
    output_usd_per_million: float | None = None,
) -> list[RouteCandidate]:
    candidates = [
        RouteCandidate(
            provider=provider,
            model=model,
            reason="primary",
            input_usd_per_million=input_usd_per_million,
            output_usd_per_million=output_usd_per_million,
        )
    ]
    for value in fallback_values or []:
        candidates.append(RouteCandidate.from_mapping(value))
    return candidates
