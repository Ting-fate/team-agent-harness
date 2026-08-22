from __future__ import annotations

import json
import sqlite3
from threading import RLock
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel, ValidationError

from app.core.models import (
    ALLOW_LEGACY_REAL_WEB_SNAPSHOT_CONTEXT,
    AgentDefinition,
    AgentRun,
    AgentSession,
    AgentSessionStatus,
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


class StorageIntegrityError(StorageError):
    pass


class RunRecordIntegrityError(StorageIntegrityError):
    def __init__(self, run_id: str, *, reason: str = "invalid_payload") -> None:
        super().__init__(f"Persisted run record is invalid: {run_id}")
        self.run_id = run_id
        self.reason = reason


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
                error_type = (
                    StorageIntegrityError
                    if isinstance(exc, sqlite3.IntegrityError)
                    else StorageError
                )
                raise error_type(str(exc)) from exc
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
        with self._lock:
            row = self.conn.execute("SELECT data FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return _load_persisted_run(row["data"], fallback_run_id=run_id)

    def list_runs(self, *, limit: int | None = None, offset: int = 0) -> list[Run]:
        order_by = "rowid DESC" if limit is not None else "id ASC"
        return self._list_models("runs", Run, order_by, limit=limit, offset=offset)

    def list_run_summaries(self, *, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        if type(limit) is not int or limit <= 0 or type(offset) is not int or offset < 0:
            raise StorageError("Run summary pagination must use a positive limit and non-negative offset.")
        try:
            with self._lock:
                rows = self.conn.execute(
                    """
                    SELECT
                        id,
                        task_id,
                        status,
                        json_extract(data, '$.id') AS payload_id,
                        json_extract(data, '$.task_id') AS payload_task_id,
                        json_extract(data, '$.status') AS payload_status,
                        json_extract(data, '$.current_step') AS current_step,
                        started_at,
                        finished_at,
                        json_extract(data, '$.final_artifact_id') AS final_artifact_id
                    FROM runs
                    ORDER BY rowid DESC
                    LIMIT ? OFFSET ?
                    """,
                    (limit, offset),
                ).fetchall()
        except sqlite3.Error as exc:
            raise StorageError("Run summary rows could not be read.") from exc
        summaries: list[dict[str, Any]] = []
        for row in rows:
            if (
                row["payload_id"] != row["id"]
                or row["payload_task_id"] != row["task_id"]
                or row["payload_status"] != row["status"]
            ):
                raise StorageIntegrityError(
                    "Run summary identity or status does not match its persisted payload."
                )
            summary = dict(row)
            summary.pop("payload_id")
            summary.pop("payload_task_id")
            summary.pop("payload_status")
            summaries.append(summary)
        return summaries

    def preview_terminal_run_records(self) -> dict[str, Any]:
        """Return the exact terminal Run and artifact set eligible for deletion."""
        with self.transaction():
            run_ids = self._eligible_terminal_run_ids()
            artifacts = self._artifact_purge_candidates(run_ids)
        return {
            "run_ids": run_ids,
            "artifact_paths": [artifact["path"] for artifact in artifacts],
            "artifacts": artifacts,
            "runs_deleted": 0,
        }

    def purge_terminal_run_records(
        self,
        *,
        expected_run_ids: list[str] | None = None,
        expected_artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Delete terminal run records while retaining task definitions."""
        with self.transaction():
            eligible_run_ids = self._eligible_terminal_run_ids()
            if expected_run_ids is None:
                run_ids = eligible_run_ids
            else:
                if (
                    any(not isinstance(run_id, str) or not run_id for run_id in expected_run_ids)
                    or len(expected_run_ids) != len(set(expected_run_ids))
                ):
                    raise StorageError("Expected terminal Run ids must be unique non-empty strings.")
                missing = sorted(set(expected_run_ids) - set(eligible_run_ids))
                if missing:
                    raise StorageError("Terminal Run purge candidates changed before deletion.")
                run_ids = list(expected_run_ids)
            artifacts = self._artifact_purge_candidates(run_ids)
            if expected_artifacts is not None and artifacts != expected_artifacts:
                raise StorageError("Terminal Run artifact candidates changed before deletion.")
            self._delete_run_records(run_ids)

        return {
            "run_ids": run_ids,
            "artifact_paths": [artifact["path"] for artifact in artifacts],
            "artifacts": artifacts,
            "runs_deleted": len(run_ids),
        }

    def _eligible_terminal_run_ids(self) -> list[str]:
        terminal_statuses = (
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.CANCELLED.value,
        )
        placeholders = ", ".join("?" for _ in terminal_statuses)
        active_queue_statuses = (
            RunQueueItemStatus.QUEUED.value,
            RunQueueItemStatus.RUNNING.value,
            RunQueueItemStatus.WAITING.value,
        )
        active_job_statuses = (
            RuntimeJobStatus.RECORDED.value,
            RuntimeJobStatus.APPROVAL_REQUIRED.value,
            RuntimeJobStatus.APPROVED.value,
        )
        active_session_statuses = (
            AgentSessionStatus.ACTIVE.value,
            AgentSessionStatus.WAITING_APPROVAL.value,
        )
        rows = self.conn.execute(
            f"""
            SELECT id
            FROM runs
            WHERE status IN ({placeholders})
              AND NOT EXISTS (
                  SELECT 1 FROM run_queue_items
                  WHERE run_queue_items.run_id = runs.id
                    AND run_queue_items.status IN ({', '.join('?' for _ in active_queue_statuses)})
              )
              AND NOT EXISTS (
                  SELECT 1 FROM run_locks
                  WHERE run_locks.run_id = runs.id
                    AND run_locks.status = ?
              )
              AND NOT EXISTS (
                  SELECT 1 FROM runtime_jobs
                  WHERE runtime_jobs.run_id = runs.id
                    AND runtime_jobs.status IN ({', '.join('?' for _ in active_job_statuses)})
              )
              AND NOT EXISTS (
                  SELECT 1 FROM agent_sessions
                  WHERE agent_sessions.run_id = runs.id
                    AND agent_sessions.status IN ({', '.join('?' for _ in active_session_statuses)})
              )
            ORDER BY id ASC
            """,
            (
                *terminal_statuses,
                *active_queue_statuses,
                RunLockStatus.ACQUIRED.value,
                *active_job_statuses,
                *active_session_statuses,
            ),
        ).fetchall()
        return [str(row["id"]) for row in rows]

    def _artifact_purge_candidates(self, run_ids: list[str]) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for batch in self._run_id_batches(run_ids):
            run_placeholders = ", ".join("?" for _ in batch)
            artifact_rows = self.conn.execute(
                f"SELECT id, run_id, data FROM artifacts WHERE run_id IN ({run_placeholders}) ORDER BY id ASC",
                batch,
            ).fetchall()
            for row in artifact_rows:
                try:
                    artifact = Artifact.model_validate_json(row["data"])
                except (TypeError, ValueError, ValidationError) as exc:
                    raise StorageIntegrityError(
                        "Terminal Run artifact metadata is invalid."
                    ) from exc
                if artifact.id != row["id"] or artifact.run_id != row["run_id"]:
                    raise StorageIntegrityError(
                        "Terminal Run artifact identity does not match its database row."
                    )
                raw_path = Path(artifact.path)
                if (
                    raw_path.is_absolute()
                    or len(raw_path.parts) != 2
                    or raw_path.parts[0] != artifact.run_id
                    or raw_path.name != raw_path.parts[1]
                ):
                    raise StorageIntegrityError(
                        "Terminal Run artifact path does not match its Run identity."
                    )
                artifacts.append(
                    {
                        "id": artifact.id,
                        "run_id": artifact.run_id,
                        "path": artifact.path,
                        "content_hash": artifact.content_hash,
                    }
                )
        candidate_ids = {artifact["id"] for artifact in artifacts}
        candidate_paths = {artifact["path"] for artifact in artifacts}
        if candidate_paths:
            rows = self.conn.execute(
                "SELECT id, run_id, data FROM artifacts ORDER BY id ASC"
            ).fetchall()
            for row in rows:
                if row["id"] in candidate_ids:
                    continue
                try:
                    other = Artifact.model_validate_json(row["data"])
                except (TypeError, ValueError, ValidationError) as exc:
                    raise StorageIntegrityError(
                        "Artifact metadata is invalid while checking purge ownership."
                    ) from exc
                if other.id != row["id"] or other.run_id != row["run_id"]:
                    raise StorageIntegrityError(
                        "Artifact identity is invalid while checking purge ownership."
                    )
                if other.path in candidate_paths:
                    raise StorageIntegrityError(
                        "Terminal Run artifact path is also owned by a retained artifact."
                    )
        return artifacts

    def _delete_run_records(self, run_ids: list[str]) -> None:
        for batch in self._run_id_batches(run_ids):
            run_placeholders = ", ".join("?" for _ in batch)
            # Delete in dependency order because the schema deliberately does
            # not use cascading deletes for recovery safety.
            for table in (
                "eval_results",
                "trace_events",
                "handoffs",
                "runtime_jobs",
                "agent_sessions",
                "run_queue_items",
                "run_locks",
                "artifacts",
                "agent_runs",
            ):
                self.conn.execute(
                    f"DELETE FROM {table} WHERE run_id IN ({run_placeholders})",
                    batch,
                )
            self.conn.execute(
                f"DELETE FROM runs WHERE id IN ({run_placeholders})",
                batch,
            )

    def _run_id_batches(self, run_ids: list[str]) -> Iterable[list[str]]:
        variable_limit = self.conn.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        batch_size = max(1, min(500, variable_limit))
        for batch_start in range(0, len(run_ids), batch_size):
            yield run_ids[batch_start : batch_start + batch_size]

    def list_runs_by_statuses(self, statuses: Iterable[RunStatus]) -> list[Run]:
        status_values = sorted({status.value for status in statuses})
        if not status_values:
            return []
        placeholders = ", ".join("?" for _ in status_values)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT id, data FROM runs WHERE status IN ({placeholders}) ORDER BY id ASC",
                status_values,
            ).fetchall()
        return [
            _load_persisted_run(row["data"], fallback_run_id=row["id"])
            for row in rows
        ]

    def list_runs_requiring_worker_recovery(self) -> list[Run]:
        runs: list[Run] = []
        for run_id in self.list_run_ids_requiring_worker_recovery():
            run = self.get_run(run_id)
            if run is not None:
                runs.append(run)
        return runs

    def list_run_ids(self) -> list[str]:
        with self._lock:
            rows = self.conn.execute("SELECT id FROM runs ORDER BY id ASC").fetchall()
        return [str(row["id"]) for row in rows]

    def list_run_ids_requiring_worker_recovery(self) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT runs.id
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
        return [str(row["id"]) for row in rows]

    def terminalize_incomplete_execution_plan_pair(self, run_id: str) -> Run:
        with self.transaction():
            row = self.conn.execute(
                "SELECT data FROM runs WHERE id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise StorageError(f"runs row not found: {run_id}")
            try:
                payload = json.loads(row["data"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise StorageError(f"Persisted run record is not valid JSON: {run_id}") from exc
            if type(payload) is not dict:
                raise StorageError(f"Persisted run record is not an object: {run_id}")
            if payload.get("id") != run_id:
                raise RunRecordIntegrityError(run_id, reason="identity_mismatch")

            plan_present = payload.get("execution_plan") is not None
            hash_present = payload.get("execution_plan_hash") is not None
            if plan_present == hash_present:
                raise StorageError(
                    f"Persisted run does not have an incomplete execution plan pair: {run_id}"
                )

            _complete_execution_plan_pair_with_failure_sentinel(payload)

            now = datetime.now(UTC)
            payload.update(
                {
                    "status": RunStatus.FAILED.value,
                    "current_step": None,
                    "finished_at": now.isoformat(),
                }
            )
            try:
                legacy_snapshot = _prepare_persisted_run_snapshot(payload, run_id)
                failed = _validate_persisted_run(payload, legacy_snapshot=legacy_snapshot)
            except (RunRecordIntegrityError, ValidationError) as exc:
                raise StorageError(f"Persisted run record cannot be terminalized: {run_id}") from exc
            self._update_model(
                "runs",
                failed,
                {
                    "task_id": failed.task_id,
                    "status": failed.status.value,
                    "started_at": _dt(failed.started_at),
                    "finished_at": _dt(failed.finished_at),
                },
            )
        return failed

    def quarantine_invalid_run_record(self, run_id: str) -> None:
        now = datetime.now(UTC)
        with self.transaction():
            cursor = self.conn.execute(
                """
                UPDATE runs
                SET status = ?, finished_at = ?
                WHERE id = ?
                """,
                (RunStatus.FAILED.value, now.isoformat(), run_id),
            )
            if cursor.rowcount != 1:
                raise StorageError(f"runs row not found: {run_id}")

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
            raise StorageIntegrityError(
                f"Agent role already exists for pack {agent.pack_name}: {agent.role}"
            )
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
            raise StorageIntegrityError(
                f"Agent role already exists for pack {agent.pack_name}: {agent.role}"
            )

        existing_by_id = self.get_agent_definition(agent.id)
        if existing_by_id is None:
            return self.create_agent_definition(agent)
        if existing_by_id.pack_name != agent.pack_name or existing_by_id.role != agent.role:
            raise StorageIntegrityError(f"Agent id already exists for another pack/role: {agent.id}")

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
            raise StorageIntegrityError(
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

    def create_run_lock(self, lock: RunLock) -> RunLock:
        with self._lock:
            self._ensure_run_exists(lock.run_id)
            active_lock = self.get_active_run_lock(lock.run_id)
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

    def get_active_run_lock(self, run_id: str) -> RunLock | None:
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
        return RunLock.model_validate_json(row["data"])

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
            raise StorageIntegrityError(f"run row not found: {run_id}")

    def _ensure_agent_run_belongs_to_run(self, agent_run_id: str, run_id: str) -> None:
        with self._lock:
            row = self.conn.execute("SELECT run_id FROM agent_runs WHERE id = ?", (agent_run_id,)).fetchone()
        if row is None:
            raise StorageIntegrityError(f"agent_run row not found: {agent_run_id}")
        if row["run_id"] != run_id:
            raise StorageIntegrityError(
                f"agent_run {agent_run_id} belongs to run {row['run_id']}, not run {run_id}"
            )

    def _ensure_artifact_belongs_to_run(self, artifact_id: str, run_id: str) -> None:
        with self._lock:
            row = self.conn.execute("SELECT run_id FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
        if row is None:
            raise StorageIntegrityError(f"artifact row not found: {artifact_id}")
        if row["run_id"] != run_id:
            raise StorageIntegrityError(
                f"artifact {artifact_id} belongs to run {row['run_id']}, not run {run_id}"
            )

    def _ensure_agent_session_belongs_to_run(self, session_id: str, run_id: str) -> None:
        with self._lock:
            row = self.conn.execute("SELECT run_id FROM agent_sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise StorageIntegrityError(f"agent_session row not found: {session_id}")
        if row["run_id"] != run_id:
            raise StorageIntegrityError(
                f"agent_session {session_id} belongs to run {row['run_id']}, not run {run_id}"
            )

    def _ensure_runtime_job_session_matches(self, job: RuntimeJob) -> None:
        with self._lock:
            row = self.conn.execute("SELECT data FROM agent_sessions WHERE id = ?", (job.agent_session_id,)).fetchone()
        if row is None:
            raise StorageIntegrityError(f"agent_session row not found: {job.agent_session_id}")
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
            raise StorageIntegrityError(
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
                raise StorageIntegrityError(f"{table} row not found: {model.id}")

    def _get_model[T: BaseModel](self, table: str, model_id: str, model_type: type[T]) -> T | None:
        with self._lock:
            row = self.conn.execute(f"SELECT data FROM {table} WHERE id = ?", (model_id,)).fetchone()
        if row is None:
            return None
        if model_type is Run:
            return _load_persisted_run(row["data"], fallback_run_id=model_id)
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
                f"SELECT id, data FROM {table}{where_sql} ORDER BY {order_by}{page_sql}",
                params,
            ).fetchall()
        if model_type is Run:
            return [
                _load_persisted_run(row["data"], fallback_run_id=row["id"])
                for row in rows
            ]
        return [model_type.model_validate_json(row["data"]) for row in rows]


def _dump(model: BaseModel) -> str:
    if isinstance(model, Run):
        names = model.confirmed_real_web_tools
        routes = model.confirmed_real_web_tool_routes
        if names is None or routes is None:
            if (
                names is not None
                or routes is not None
                or model._legacy_real_web_snapshot_run_id != model.id
            ):
                raise RunRecordIntegrityError(model.id)
            return model.model_dump_json(
                by_alias=True,
                exclude={
                    "confirmed_real_web_tools",
                    "confirmed_real_web_tool_routes",
                },
            )
    return model.model_dump_json(by_alias=True)


def _load_persisted_run(raw_data: str, *, fallback_run_id: str | None = None) -> Run:
    run_id = fallback_run_id or "unknown"
    try:
        payload = json.loads(raw_data)
        if type(payload) is not dict:
            raise ValueError("Persisted Run JSON must be an object.")
        payload_run_id = payload.get("id")
        if fallback_run_id is not None and payload_run_id != fallback_run_id:
            raise RunRecordIntegrityError(fallback_run_id, reason="identity_mismatch")
        if isinstance(payload_run_id, str) and payload_run_id:
            run_id = payload_run_id
        if _has_incomplete_execution_plan_pair(payload):
            repaired_payload = payload.copy()
            _complete_execution_plan_pair_with_failure_sentinel(repaired_payload)
            legacy_snapshot = _prepare_persisted_run_snapshot(repaired_payload, run_id)
            _validate_persisted_run(repaired_payload, legacy_snapshot=legacy_snapshot)
            raise RunRecordIntegrityError(
                run_id,
                reason="incomplete_execution_plan_pair",
            )
        legacy_snapshot = _prepare_persisted_run_snapshot(payload, run_id)
        return _validate_persisted_run(payload, legacy_snapshot=legacy_snapshot)
    except RunRecordIntegrityError:
        raise
    except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
        raise RunRecordIntegrityError(run_id) from exc


def _prepare_persisted_run_snapshot(payload: dict[str, Any], run_id: str) -> bool:
    names_present = "confirmed_real_web_tools" in payload
    routes_present = "confirmed_real_web_tool_routes" in payload
    if names_present != routes_present:
        raise RunRecordIntegrityError(run_id, reason="invalid_real_web_snapshot")
    if names_present:
        return False
    payload["confirmed_real_web_tools"] = None
    payload["confirmed_real_web_tool_routes"] = None
    return True


def _has_incomplete_execution_plan_pair(payload: dict[str, Any]) -> bool:
    plan_present = payload.get("execution_plan") is not None
    hash_present = payload.get("execution_plan_hash") is not None
    return plan_present != hash_present


def _complete_execution_plan_pair_with_failure_sentinel(payload: dict[str, Any]) -> None:
    if payload.get("execution_plan") is None:
        payload["execution_plan"] = {}
    if payload.get("execution_plan_hash") is not None:
        return

    sentinel_hash = "0" * 64
    try:
        from app.core.execution_plan import ExecutionPlan, execution_plan_hash

        expected_hash = execution_plan_hash(
            ExecutionPlan.model_validate(payload["execution_plan"])
        )
    except (TypeError, ValueError):
        expected_hash = None
    if expected_hash == sentinel_hash:
        sentinel_hash = "1" * 64
    payload["execution_plan_hash"] = sentinel_hash


def _validate_persisted_run(
    payload: dict[str, Any],
    *,
    legacy_snapshot: bool,
) -> Run:
    run = Run.model_validate(
        payload,
        context=(
            {ALLOW_LEGACY_REAL_WEB_SNAPSHOT_CONTEXT: True}
            if legacy_snapshot
            else None
        ),
    )
    if legacy_snapshot:
        run._legacy_real_web_snapshot_run_id = run.id
    return run


def _dt(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat()
