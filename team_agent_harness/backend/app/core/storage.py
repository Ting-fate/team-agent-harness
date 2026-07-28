from __future__ import annotations

import sqlite3
from threading import RLock
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel

from app.core.models import (
    AgentDefinition,
    AgentRun,
    AgentSession,
    Artifact,
    EvalResult,
    Handoff,
    Run,
    RunLock,
    RunLockStatus,
    RunQueueItem,
    RunQueueItemStatus,
    RunStatus,
    RuntimeJob,
    RuntimeJobStatus,
    Task,
    TraceEvent,
)


class StorageError(RuntimeError):
    pass


class SQLiteStorage:
    def __init__(self, db_path: str | Path, *, check_same_thread: bool = True) -> None:
        self.db_path = Path(db_path)
        self.check_same_thread = check_same_thread
        self._lock = RLock()
        self._transaction_depth = 0

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=self.check_same_thread)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        if hasattr(self, "_conn"):
            self._conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        if not hasattr(self, "_conn"):
            raise StorageError("SQLiteStorage.connect() must be called before use.")
        return self._conn

    @contextmanager
    def transaction(self) -> Iterable[None]:
        with self._lock:
            outermost = self._transaction_depth == 0
            try:
                if outermost:
                    self.conn.execute("BEGIN")
                self._transaction_depth += 1
                try:
                    yield
                except Exception:
                    if outermost and self.conn.in_transaction:
                        self.conn.rollback()
                    raise
                else:
                    if outermost:
                        self.conn.commit()
            except sqlite3.Error as exc:
                if outermost and self.conn.in_transaction:
                    self.conn.rollback()
                raise StorageError(str(exc)) from exc
            finally:
                if self._transaction_depth:
                    self._transaction_depth -= 1

    def init_schema(self) -> None:
        with self.transaction():
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY (task_id) REFERENCES tasks(id)
                );

                CREATE TABLE IF NOT EXISTS agent_definitions (
                    id TEXT PRIMARY KEY,
                    pack_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    data TEXT NOT NULL,
                    UNIQUE (pack_name, role)
                );

                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(id),
                    FOREIGN KEY (agent_id) REFERENCES agent_definitions(id)
                );

                CREATE TABLE IF NOT EXISTS agent_sessions (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    agent_run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    step_name TEXT NOT NULL,
                    runtime TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id),
                    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id),
                    FOREIGN KEY (agent_id) REFERENCES agent_definitions(id)
                );

                CREATE TABLE IF NOT EXISTS runtime_jobs (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    agent_run_id TEXT NOT NULL,
                    agent_session_id TEXT,
                    step_name TEXT NOT NULL,
                    runtime TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id),
                    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id),
                    FOREIGN KEY (agent_session_id) REFERENCES agent_sessions(id)
                );

                CREATE TABLE IF NOT EXISTS run_queue_items (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS run_locks (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    released_at TEXT,
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                );

                CREATE TABLE IF NOT EXISTS handoffs (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    from_agent_run_id TEXT NOT NULL,
                    to_agent_id TEXT NOT NULL,
                    data TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id),
                    FOREIGN KEY (from_agent_run_id) REFERENCES agent_runs(id),
                    FOREIGN KEY (to_agent_id) REFERENCES agent_definitions(id)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    agent_run_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id),
                    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id)
                );

                CREATE TABLE IF NOT EXISTS trace_events (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    agent_run_id TEXT,
                    event_type TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id),
                    FOREIGN KEY (agent_run_id) REFERENCES agent_runs(id)
                );

                CREATE TABLE IF NOT EXISTS eval_results (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    artifact_id TEXT,
                    check_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(id),
                    FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
                );

                CREATE INDEX IF NOT EXISTS idx_runs_task_id ON runs(task_id);
                CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_run_id ON agent_runs(run_id);
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_run_id ON agent_sessions(run_id);
                CREATE INDEX IF NOT EXISTS idx_runtime_jobs_run_id ON runtime_jobs(run_id);
                CREATE INDEX IF NOT EXISTS idx_runtime_jobs_session_id ON runtime_jobs(agent_session_id);
                CREATE INDEX IF NOT EXISTS idx_run_queue_items_run_id ON run_queue_items(run_id);
                CREATE INDEX IF NOT EXISTS idx_run_queue_items_status ON run_queue_items(status);
                CREATE INDEX IF NOT EXISTS idx_run_locks_run_id ON run_locks(run_id);
                CREATE INDEX IF NOT EXISTS idx_run_locks_status ON run_locks(status);
                CREATE INDEX IF NOT EXISTS idx_handoffs_run_id ON handoffs(run_id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id);
                CREATE INDEX IF NOT EXISTS idx_trace_events_run_id ON trace_events(run_id);
                CREATE INDEX IF NOT EXISTS idx_eval_results_run_id ON eval_results(run_id);
                """
            )

    def create_task(self, task: Task) -> Task:
        self._insert_model(
            "tasks",
            task,
            {
                "created_at": _dt(task.created_at),
            },
        )
        return task

    def get_task(self, task_id: str) -> Task | None:
        return self._get_model("tasks", task_id, Task)

    def list_tasks(self, *, limit: int | None = None, offset: int = 0) -> list[Task]:
        order_by = "created_at DESC, id DESC" if limit is not None else "created_at ASC"
        return self._list_models("tasks", Task, order_by, limit=limit, offset=offset)

    def create_run(self, run: Run) -> Run:
        if run.final_artifact_id is not None:
            self._ensure_artifact_belongs_to_run(run.final_artifact_id, run.id)
        self._insert_model(
            "runs",
            run,
            {
                "task_id": run.task_id,
                "status": run.status.value,
                "started_at": _dt(run.started_at),
                "finished_at": _dt(run.finished_at),
            },
        )
        return run

    def get_run(self, run_id: str) -> Run | None:
        return self._get_model("runs", run_id, Run)

    def list_runs(self, *, limit: int | None = None, offset: int = 0) -> list[Run]:
        order_by = "rowid DESC" if limit is not None else "id ASC"
        return self._list_models("runs", Run, order_by, limit=limit, offset=offset)

    def list_runs_by_statuses(self, statuses: Iterable[RunStatus]) -> list[Run]:
        status_values = sorted({status.value for status in statuses})
        if not status_values:
            return []
        placeholders = ", ".join("?" for _ in status_values)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT data FROM runs WHERE status IN ({placeholders}) ORDER BY id ASC",
                status_values,
            ).fetchall()
        return [Run.model_validate_json(row["data"]) for row in rows]

    def list_runs_requiring_worker_recovery(self) -> list[Run]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT runs.data
                FROM runs
                WHERE runs.status IN (?, ?)
                   OR EXISTS (
                       SELECT 1
                       FROM run_queue_items
                       WHERE run_queue_items.run_id = runs.id
                         AND run_queue_items.status IN (?, ?)
                   )
                   OR EXISTS (
                       SELECT 1
                       FROM run_locks
                       WHERE run_locks.run_id = runs.id
                         AND run_locks.status = ?
                   )
                   OR (
                       runs.status = ?
                       AND EXISTS (
                           SELECT 1
                           FROM runtime_jobs
                            WHERE runtime_jobs.run_id = runs.id
                              AND runtime_jobs.status IN (?, ?, ?)
                       )
                   )
                ORDER BY runs.id ASC
                """,
                (
                    RunStatus.QUEUED.value,
                    RunStatus.RUNNING.value,
                    RunQueueItemStatus.QUEUED.value,
                    RunQueueItemStatus.RUNNING.value,
                    RunLockStatus.ACQUIRED.value,
                    RunStatus.WAITING.value,
                    RuntimeJobStatus.APPROVED.value,
                    RuntimeJobStatus.REJECTED.value,
                    RuntimeJobStatus.CANCELLED.value,
                ),
            ).fetchall()
        return [Run.model_validate_json(row["data"]) for row in rows]

    def update_run(self, run: Run) -> Run:
        if run.final_artifact_id is not None:
            self._ensure_artifact_belongs_to_run(run.final_artifact_id, run.id)
        self._update_model(
            "runs",
            run,
            {
                "task_id": run.task_id,
                "status": run.status.value,
                "started_at": _dt(run.started_at),
                "finished_at": _dt(run.finished_at),
            },
        )
        return run

    def create_agent_definition(self, agent: AgentDefinition) -> AgentDefinition:
        existing = self.get_agent_definition_by_pack_role(agent.pack_name, agent.role)
        if existing is not None and existing.id != agent.id:
            raise StorageError(f"Agent role already exists for pack {agent.pack_name}: {agent.role}")
        self._insert_model(
            "agent_definitions",
            agent,
            {
                "pack_name": agent.pack_name,
                "role": agent.role,
            },
        )
        return agent

    def upsert_agent_definition(self, agent: AgentDefinition) -> AgentDefinition:
        existing = self.get_agent_definition_by_pack_role(agent.pack_name, agent.role)
        if existing is not None and existing.id != agent.id:
            raise StorageError(f"Agent role already exists for pack {agent.pack_name}: {agent.role}")

        existing_by_id = self.get_agent_definition(agent.id)
        if existing_by_id is None:
            return self.create_agent_definition(agent)
        if existing_by_id.pack_name != agent.pack_name or existing_by_id.role != agent.role:
            raise StorageError(f"Agent id already exists for another pack/role: {agent.id}")

        self._update_model(
            "agent_definitions",
            agent,
            {
                "pack_name": agent.pack_name,
                "role": agent.role,
            },
        )
        return agent

    def get_agent_definition(self, agent_id: str) -> AgentDefinition | None:
        return self._get_model("agent_definitions", agent_id, AgentDefinition)

    def get_agent_definition_by_pack_role(self, pack_name: str, role: str) -> AgentDefinition | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT data FROM agent_definitions WHERE pack_name = ? AND role = ?",
                (pack_name, role),
            ).fetchone()
        if row is None:
            return None
        return AgentDefinition.model_validate_json(row["data"])

    def list_agent_definitions(self, pack_name: str | None = None) -> list[AgentDefinition]:
        where = ("pack_name", pack_name) if pack_name is not None else None
        return self._list_models("agent_definitions", AgentDefinition, "id ASC", where=where)

    def create_agent_run(self, agent_run: AgentRun) -> AgentRun:
        self._insert_model(
            "agent_runs",
            agent_run,
            {
                "run_id": agent_run.run_id,
                "agent_id": agent_run.agent_id,
                "status": agent_run.status.value,
                "started_at": _dt(agent_run.started_at),
                "finished_at": _dt(agent_run.finished_at),
            },
        )
        return agent_run

    def get_agent_run(self, agent_run_id: str) -> AgentRun | None:
        return self._get_model("agent_runs", agent_run_id, AgentRun)

    def update_agent_run(self, agent_run: AgentRun) -> AgentRun:
        self._update_model(
            "agent_runs",
            agent_run,
            {
                "run_id": agent_run.run_id,
                "agent_id": agent_run.agent_id,
                "status": agent_run.status.value,
                "started_at": _dt(agent_run.started_at),
                "finished_at": _dt(agent_run.finished_at),
            },
        )
        return agent_run

    def list_agent_runs_for_run(self, run_id: str) -> list[AgentRun]:
        return self._list_models("agent_runs", AgentRun, "rowid ASC", where=("run_id", run_id))

    def create_agent_session(self, session: AgentSession) -> AgentSession:
        self._ensure_agent_run_belongs_to_run(session.agent_run_id, session.run_id)
        agent_run = self.get_agent_run(session.agent_run_id)
        if agent_run is not None and agent_run.agent_id != session.agent_id:
            raise StorageError(
                f"agent_session agent {session.agent_id} does not match agent_run {agent_run.agent_id}"
            )
        self._insert_model(
            "agent_sessions",
            session,
            {
                "run_id": session.run_id,
                "agent_run_id": session.agent_run_id,
                "agent_id": session.agent_id,
                "step_name": session.step_name,
                "runtime": session.runtime,
                "status": session.status.value,
                "created_at": _dt(session.created_at),
                "updated_at": _dt(session.updated_at),
            },
        )
        return session

    def get_agent_session(self, session_id: str) -> AgentSession | None:
        return self._get_model("agent_sessions", session_id, AgentSession)

    def update_agent_session(self, session: AgentSession) -> AgentSession:
        self._ensure_agent_run_belongs_to_run(session.agent_run_id, session.run_id)
        self._update_model(
            "agent_sessions",
            session,
            {
                "run_id": session.run_id,
                "agent_run_id": session.agent_run_id,
                "agent_id": session.agent_id,
                "step_name": session.step_name,
                "runtime": session.runtime,
                "status": session.status.value,
                "created_at": _dt(session.created_at),
                "updated_at": _dt(session.updated_at),
            },
        )
        return session

    def list_agent_sessions_for_run(self, run_id: str) -> list[AgentSession]:
        return self._list_models("agent_sessions", AgentSession, "rowid ASC", where=("run_id", run_id))

    def create_runtime_job(self, job: RuntimeJob) -> RuntimeJob:
        self._ensure_agent_run_belongs_to_run(job.agent_run_id, job.run_id)
        if job.agent_session_id is not None:
            self._ensure_runtime_job_session_matches(job)
        self._insert_model(
            "runtime_jobs",
            job,
            {
                "run_id": job.run_id,
                "agent_run_id": job.agent_run_id,
                "agent_session_id": job.agent_session_id,
                "step_name": job.step_name,
                "runtime": job.runtime,
                "status": job.status.value,
                "created_at": _dt(job.created_at),
                "updated_at": _dt(job.updated_at),
            },
        )
        return job

    def get_runtime_job(self, job_id: str) -> RuntimeJob | None:
        return self._get_model("runtime_jobs", job_id, RuntimeJob)

    def update_runtime_job(self, job: RuntimeJob) -> RuntimeJob:
        self._ensure_agent_run_belongs_to_run(job.agent_run_id, job.run_id)
        if job.agent_session_id is not None:
            self._ensure_runtime_job_session_matches(job)
        self._update_model(
            "runtime_jobs",
            job,
            {
                "run_id": job.run_id,
                "agent_run_id": job.agent_run_id,
                "agent_session_id": job.agent_session_id,
                "step_name": job.step_name,
                "runtime": job.runtime,
                "status": job.status.value,
                "created_at": _dt(job.created_at),
                "updated_at": _dt(job.updated_at),
            },
        )
        return job

    def list_runtime_jobs_for_run(self, run_id: str) -> list[RuntimeJob]:
        return self._list_models("runtime_jobs", RuntimeJob, "rowid ASC", where=("run_id", run_id))

    def list_runtime_jobs_for_session(self, session_id: str) -> list[RuntimeJob]:
        return self._list_models(
            "runtime_jobs",
            RuntimeJob,
            "rowid ASC",
            where=("agent_session_id", session_id),
        )

    def create_run_queue_item(self, item: RunQueueItem) -> RunQueueItem:
        self._ensure_run_exists(item.run_id)
        self._insert_model(
            "run_queue_items",
            item,
            {
                "run_id": item.run_id,
                "action": item.action,
                "status": item.status.value,
                "created_at": _dt(item.created_at),
                "updated_at": _dt(item.updated_at),
            },
        )
        return item

    def get_run_queue_item(self, item_id: str) -> RunQueueItem | None:
        return self._get_model("run_queue_items", item_id, RunQueueItem)

    def update_run_queue_item(self, item: RunQueueItem) -> RunQueueItem:
        self._ensure_run_exists(item.run_id)
        self._update_model(
            "run_queue_items",
            item,
            {
                "run_id": item.run_id,
                "action": item.action,
                "status": item.status.value,
                "created_at": _dt(item.created_at),
                "updated_at": _dt(item.updated_at),
            },
        )
        return item

    def list_run_queue_items_for_run(self, run_id: str) -> list[RunQueueItem]:
        return self._list_models("run_queue_items", RunQueueItem, "created_at ASC, id ASC", where=("run_id", run_id))

    def create_run_lock(self, lock: RunLock, *, stale_after_seconds: int | None = None) -> RunLock:
        with self._lock:
            self._ensure_run_exists(lock.run_id)
            active_lock = self.get_active_run_lock(lock.run_id, stale_after_seconds=stale_after_seconds)
            if lock.status == RunLockStatus.ACQUIRED and active_lock is not None:
                raise StorageError(f"Run already has an active lock: {lock.run_id}")
            self._insert_model(
                "run_locks",
                lock,
                {
                    "run_id": lock.run_id,
                    "owner": lock.owner,
                    "status": lock.status.value,
                    "acquired_at": _dt(lock.acquired_at),
                    "released_at": _dt(lock.released_at),
                },
            )
        return lock

    def get_run_lock(self, lock_id: str) -> RunLock | None:
        return self._get_model("run_locks", lock_id, RunLock)

    def update_run_lock(self, lock: RunLock) -> RunLock:
        with self._lock:
            self._ensure_run_exists(lock.run_id)
            if lock.status == RunLockStatus.ACQUIRED:
                active = self.get_active_run_lock(lock.run_id)
                if active is not None and active.id != lock.id:
                    raise StorageError(f"Run already has an active lock: {lock.run_id}")
            self._update_model(
                "run_locks",
                lock,
                {
                    "run_id": lock.run_id,
                    "owner": lock.owner,
                    "status": lock.status.value,
                    "acquired_at": _dt(lock.acquired_at),
                    "released_at": _dt(lock.released_at),
                },
            )
        return lock

    def list_run_locks_for_run(self, run_id: str) -> list[RunLock]:
        return self._list_models("run_locks", RunLock, "acquired_at ASC, id ASC", where=("run_id", run_id))

    def get_active_run_lock(self, run_id: str, *, stale_after_seconds: int | None = None) -> RunLock | None:
        with self._lock:
            row = self.conn.execute(
                """
                SELECT data
                FROM run_locks
                WHERE run_id = ? AND status = ?
                ORDER BY acquired_at DESC, id DESC
                LIMIT 1
                """,
                (run_id, RunLockStatus.ACQUIRED.value),
            ).fetchone()
        if row is None:
            return None
        lock = RunLock.model_validate_json(row["data"])
        if stale_after_seconds is not None and _is_stale_lock(lock, stale_after_seconds):
            recovered = lock.model_copy(
                update={
                    "status": RunLockStatus.RELEASED,
                    "released_at": datetime.now(lock.acquired_at.tzinfo),
                    "metadata": {
                        **lock.metadata,
                        "stale_recovered": True,
                        "stale_after_seconds": stale_after_seconds,
                    },
                }
            )
            self.update_run_lock(recovered)
            return None
        return lock

    def create_handoff(self, handoff: Handoff) -> Handoff:
        self._ensure_agent_run_belongs_to_run(handoff.from_agent_run_id, handoff.run_id)
        for artifact_id in handoff.artifact_refs:
            self._ensure_artifact_belongs_to_run(artifact_id, handoff.run_id)
        self._insert_model(
            "handoffs",
            handoff,
            {
                "run_id": handoff.run_id,
                "from_agent_run_id": handoff.from_agent_run_id,
                "to_agent_id": handoff.to_agent_id,
            },
        )
        return handoff

    def get_handoff(self, handoff_id: str) -> Handoff | None:
        return self._get_model("handoffs", handoff_id, Handoff)

    def list_handoffs_for_run(self, run_id: str) -> list[Handoff]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT handoffs.data
                FROM handoffs
                JOIN agent_runs ON handoffs.from_agent_run_id = agent_runs.id
                WHERE handoffs.run_id = ?
                ORDER BY agent_runs.started_at ASC, agent_runs.finished_at ASC, agent_runs.id ASC, handoffs.id ASC
                """,
                (run_id,),
            ).fetchall()
        return [Handoff.model_validate_json(row["data"]) for row in rows]

    def create_artifact(self, artifact: Artifact) -> Artifact:
        self._ensure_agent_run_belongs_to_run(artifact.agent_run_id, artifact.run_id)
        self._insert_model(
            "artifacts",
            artifact,
            {
                "run_id": artifact.run_id,
                "agent_run_id": artifact.agent_run_id,
                "type": artifact.type.value,
                "created_at": _dt(artifact.created_at),
            },
        )
        return artifact

    def get_artifact(self, artifact_id: str) -> Artifact | None:
        return self._get_model("artifacts", artifact_id, Artifact)

    def delete_artifact(self, artifact_id: str) -> None:
        with self.transaction():
            self.conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))

    def list_artifacts_for_run(self, run_id: str) -> list[Artifact]:
        return self._list_models("artifacts", Artifact, "created_at ASC, id ASC", where=("run_id", run_id))

    def append_trace_event(self, event: TraceEvent) -> TraceEvent:
        if event.agent_run_id is not None:
            self._ensure_agent_run_belongs_to_run(event.agent_run_id, event.run_id)
        self._insert_model(
            "trace_events",
            event,
            {
                "run_id": event.run_id,
                "agent_run_id": event.agent_run_id,
                "event_type": event.event_type.value,
                "created_at": _dt(event.created_at),
            },
        )
        return event

    def list_trace_events_for_run(self, run_id: str) -> list[TraceEvent]:
        return self._list_models("trace_events", TraceEvent, "created_at ASC, id ASC", where=("run_id", run_id))

    def create_eval_result(self, result: EvalResult) -> EvalResult:
        if result.artifact_id is not None:
            self._ensure_artifact_belongs_to_run(result.artifact_id, result.run_id)
        self._insert_model(
            "eval_results",
            result,
            {
                "run_id": result.run_id,
                "artifact_id": result.artifact_id,
                "check_name": result.check_name,
                "status": result.status.value,
                "created_at": _dt(result.created_at),
            },
        )
        return result

    def get_eval_result(self, result_id: str) -> EvalResult | None:
        return self._get_model("eval_results", result_id, EvalResult)

    def list_eval_results_for_run(self, run_id: str) -> list[EvalResult]:
        return self._list_models("eval_results", EvalResult, "created_at ASC, id ASC", where=("run_id", run_id))

    def _ensure_run_exists(self, run_id: str) -> None:
        with self._lock:
            row = self.conn.execute("SELECT id FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise StorageError(f"run row not found: {run_id}")

    def _ensure_agent_run_belongs_to_run(self, agent_run_id: str, run_id: str) -> None:
        with self._lock:
            row = self.conn.execute("SELECT run_id FROM agent_runs WHERE id = ?", (agent_run_id,)).fetchone()
        if row is None:
            raise StorageError(f"agent_run row not found: {agent_run_id}")
        if row["run_id"] != run_id:
            raise StorageError(
                f"agent_run {agent_run_id} belongs to run {row['run_id']}, not run {run_id}"
            )

    def _ensure_artifact_belongs_to_run(self, artifact_id: str, run_id: str) -> None:
        with self._lock:
            row = self.conn.execute("SELECT run_id FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise StorageError(f"artifact row not found: {artifact_id}")
        if row["run_id"] != run_id:
            raise StorageError(f"artifact {artifact_id} belongs to run {row['run_id']}, not run {run_id}")

    def _ensure_agent_session_belongs_to_run(self, session_id: str, run_id: str) -> None:
        with self._lock:
            row = self.conn.execute("SELECT run_id FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise StorageError(f"agent_session row not found: {session_id}")
        if row["run_id"] != run_id:
            raise StorageError(f"agent_session {session_id} belongs to run {row['run_id']}, not run {run_id}")

    def _ensure_runtime_job_session_matches(self, job: RuntimeJob) -> None:
        with self._lock:
            row = self.conn.execute("SELECT data FROM agent_sessions WHERE id = ?", (job.agent_session_id,)).fetchone()
        if row is None:
            raise StorageError(f"agent_session row not found: {job.agent_session_id}")
        session = AgentSession.model_validate_json(row["data"])
        mismatches: list[str] = []
        if session.run_id != job.run_id:
            mismatches.append(f"run_id={session.run_id}")
        if session.agent_run_id != job.agent_run_id:
            mismatches.append(f"agent_run_id={session.agent_run_id}")
        if session.step_name != job.step_name:
            mismatches.append(f"step_name={session.step_name}")
        if session.runtime != job.runtime:
            mismatches.append(f"runtime={session.runtime}")
        if mismatches:
            raise StorageError(
                f"runtime_job session mismatch for {job.agent_session_id}: {', '.join(mismatches)}"
            )

    def _insert_model(self, table: str, model: BaseModel, columns: dict[str, Any]) -> None:
        payload = _dump(model)
        values = {"id": model.id, "data": payload, **columns}
        column_sql = ", ".join(values.keys())
        placeholder_sql = ", ".join(["?"] * len(values))
        with self.transaction():
            self.conn.execute(
                f"INSERT INTO {table} ({column_sql}) VALUES ({placeholder_sql})",
                list(values.values()),
            )

    def _update_model(self, table: str, model: BaseModel, columns: dict[str, Any]) -> None:
        payload = _dump(model)
        values = {"data": payload, **columns}
        set_sql = ", ".join(f"{key} = ?" for key in values)
        with self.transaction():
            cursor = self.conn.execute(
                f"UPDATE {table} SET {set_sql} WHERE id = ?",
                [*values.values(), model.id],
            )
            if cursor.rowcount != 1:
                raise StorageError(f"{table} row not found: {model.id}")

    def _get_model[T: BaseModel](self, table: str, model_id: str, model_type: type[T]) -> T | None:
        with self._lock:
            row = self.conn.execute(f"SELECT data FROM {table} WHERE id = ?", (model_id,)).fetchone()
        if row is None:
            return None
        return model_type.model_validate_json(row["data"])

    def _list_models[T: BaseModel](
        self,
        table: str,
        model_type: type[T],
        order_by: str,
        where: tuple[str, str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[T]:
        params: list[str | int] = []
        where_sql = ""
        if where is not None:
            where_sql = f" WHERE {where[0]} = ?"
            params.append(where[1])
        page_sql = ""
        if limit is not None:
            if limit <= 0 or offset < 0:
                raise StorageError("list limit must be positive and offset must be non-negative")
            page_sql = " LIMIT ? OFFSET ?"
            params.extend([limit, offset])
        elif offset:
            raise StorageError("list offset requires a limit")
        with self._lock:
            rows = self.conn.execute(
                f"SELECT data FROM {table}{where_sql} ORDER BY {order_by}{page_sql}",
                params,
            ).fetchall()
        return [model_type.model_validate_json(row["data"]) for row in rows]


def _dump(model: BaseModel) -> str:
    return model.model_dump_json(by_alias=True)


def _dt(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _is_stale_lock(lock: RunLock, stale_after_seconds: int) -> bool:
    if stale_after_seconds <= 0:
        return False
    reference_time = lock.acquired_at
    heartbeat_value = lock.metadata.get("heartbeat_at")
    if isinstance(heartbeat_value, str):
        try:
            heartbeat_at = datetime.fromisoformat(heartbeat_value)
        except ValueError:
            pass
        else:
            if heartbeat_at.tzinfo is not None and heartbeat_at.utcoffset() is not None:
                reference_time = heartbeat_at
    now = datetime.now(reference_time.tzinfo)
    return now - reference_time > timedelta(seconds=stale_after_seconds)
