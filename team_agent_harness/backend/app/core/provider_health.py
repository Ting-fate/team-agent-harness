from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from time import monotonic
from typing import Any


@dataclass(frozen=True)
class ProviderHealthSnapshot:
    provider: str
    consecutive_failures: int
    last_error_class: str | None
    last_latency_ms: int | None
    average_latency_ms: float | None
    circuit_open: bool
    cooldown_remaining_seconds: float

    def public_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "consecutive_failures": self.consecutive_failures,
            "last_error_class": self.last_error_class,
            "last_latency_ms": self.last_latency_ms,
            "average_latency_ms": self.average_latency_ms,
            "circuit_open": self.circuit_open,
            "cooldown_remaining_seconds": round(self.cooldown_remaining_seconds, 3),
        }


@dataclass
class _ProviderHealthState:
    consecutive_failures: int = 0
    last_error_class: str | None = None
    last_latency_ms: int | None = None
    average_latency_ms: float | None = None
    opened_at: float | None = None


class ProviderHealthRegistry:
    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 30.0) -> None:
        if type(failure_threshold) is not int or failure_threshold <= 0:
            raise ValueError("failure_threshold must be a positive integer")
        if type(cooldown_seconds) not in {int, float} or cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be a positive number")
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = float(cooldown_seconds)
        self._states: dict[str, _ProviderHealthState] = {}
        self._lock = Lock()

    def record_success(self, provider: str, latency_ms: int | None = None) -> None:
        with self._lock:
            state = self._states.setdefault(provider, _ProviderHealthState())
            state.consecutive_failures = 0
            state.last_error_class = None
            state.opened_at = None
            if latency_ms is not None:
                state.last_latency_ms = max(0, int(latency_ms))
                state.average_latency_ms = (
                    float(state.last_latency_ms)
                    if state.average_latency_ms is None
                    else state.average_latency_ms * 0.8 + state.last_latency_ms * 0.2
                )

    def record_failure(self, provider: str, error_class: str | None, *, retryable: bool) -> None:
        if not retryable:
            return
        with self._lock:
            state = self._states.setdefault(provider, _ProviderHealthState())
            state.consecutive_failures += 1
            state.last_error_class = error_class or "provider_error"
            if state.consecutive_failures >= self.failure_threshold and state.opened_at is None:
                state.opened_at = monotonic()

    def is_circuit_open(self, provider: str) -> bool:
        return self.snapshot(provider).circuit_open

    def snapshot(self, provider: str) -> ProviderHealthSnapshot:
        with self._lock:
            state = self._states.get(provider, _ProviderHealthState())
            remaining = self._cooldown_remaining(state)
            circuit_open = remaining > 0
            if state.opened_at is not None and not circuit_open:
                state.opened_at = None
                state.consecutive_failures = 0
            return ProviderHealthSnapshot(
                provider=provider,
                consecutive_failures=state.consecutive_failures,
                last_error_class=state.last_error_class,
                last_latency_ms=state.last_latency_ms,
                average_latency_ms=state.average_latency_ms,
                circuit_open=circuit_open,
                cooldown_remaining_seconds=remaining,
            )

    def snapshots(self, providers: set[str] | list[str] | tuple[str, ...]) -> list[ProviderHealthSnapshot]:
        return [self.snapshot(provider) for provider in sorted(set(providers))]

    def _cooldown_remaining(self, state: _ProviderHealthState) -> float:
        if state.opened_at is None:
            return 0.0
        return max(0.0, self.cooldown_seconds - (monotonic() - state.opened_at))
