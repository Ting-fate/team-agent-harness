from __future__ import annotations

from typing import Any

from app.core.models import TraceEvent, TraceEventType
from app.core.storage import SQLiteStorage


class TraceLogger:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def record(
        self,
        *,
        run_id: str,
        event_type: TraceEventType | str,
        payload: dict[str, Any] | None = None,
        agent_run_id: str | None = None,
        duration_ms: int | None = None,
    ) -> TraceEvent:
        event = TraceEvent(
            run_id=run_id,
            agent_run_id=agent_run_id,
            event_type=event_type,
            payload=payload or {},
            duration_ms=duration_ms,
        )
        return self.storage.append_trace_event(event)

    def list_for_run(self, run_id: str) -> list[TraceEvent]:
        return self.storage.list_trace_events_for_run(run_id)
