from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from threading import Event, Thread
from time import sleep
from typing import TypeVar

from app.core.models import (
    Run,
    RunLock,
    RunLockStatus,
    RunQueueItem,
    RunQueueItemStatus,
    RunStatus,
    utc_now,
)
from app.core.storage import SQLiteStorage, StorageError
from app.core.trace import TraceLogger


T = TypeVar("T")
RUN_LOCK_STALE_AFTER_SECONDS = 10 * 60
RUN_LOCK_HEARTBEAT_INTERVAL_SECONDS = 5.0
RUN_QUEUE_WRITE_ATTEMPTS = 3
RUN_QUEUE_WRITE_RETRY_SECONDS = 0.01


class RunCoordinationConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class RunCoordinationResult[T]:
    result: T
    queue_item: RunQueueItem
    lock: RunLock


@dataclass(frozen=True)
class RunQueueSubmission:
    run: Run
    queue_item: RunQueueItem


class RunCoordinator:
    def __init__(self, storage: SQLiteStorage, trace_logger: TraceLogger) -> None:
        self.storage = storage
        self.trace_logger = trace_logger

    def start_new_run(self, run: Run, execute: Callable[[Run], T]) -> RunCoordinationResult[T]:
        submission = self.enqueue_new_run(run, "start_run", background_worker=False)
        return self.execute_queue_item(submission.queue_item.id, lambda: execute(run))

    def enqueue_new_run(
        self,
        run: Run,
        action: str,
        *,
        background_worker: bool,
    ) -> RunQueueSubmission:
        with self.storage.transaction():
            created = self.storage.create_run(run)
            queue_item = self._create_queue_item(
                run.id,
                action,
                background_worker=background_worker,
            )
        return RunQueueSubmission(run=created, queue_item=queue_item)

    def enqueue_existing_run(
        self,
        run_id: str,
        action: str,
        *,
        background_worker: bool,
    ) -> RunQueueItem:
        if self.storage.get_run(run_id) is None:
            raise RunCoordinationConflict(f"Run not found: {run_id}")
        return self._create_queue_item(
            run_id,
            action,
            background_worker=background_worker,
        )

    def execute(self, run_id: str, action: str, execute: Callable[[], T]) -> RunCoordinationResult[T]:
        queue_item = self._create_queue_item(run_id, action, background_worker=False)
        return self.execute_queue_item(queue_item.id, execute)

    def execute_exclusive(self, run_id: str, action: str, execute: Callable[[], T]) -> T:
        lock: RunLock | None = None
        heartbeat_stop: Event | None = None
        heartbeat_thread: Thread | None = None
        try:
            lock = self._acquire_lock(run_id, action)
            heartbeat_stop, heartbeat_thread = self._start_lock_heartbeat(lock)
            return execute()
        finally:
            if lock is not None:
                if heartbeat_stop is not None and heartbeat_thread is not None:
                    heartbeat_stop.set()
                    heartbeat_thread.join()
                self._release_lock(lock)

    def execute_queue_item(self, queue_item_id: str, execute: Callable[[], T]) -> RunCoordinationResult[T]:
        queue_item = self.storage.get_run_queue_item(queue_item_id)
        if queue_item is None:
            raise RunCoordinationConflict(f"Run queue item not found: {queue_item_id}")
        if queue_item.status != RunQueueItemStatus.QUEUED:
            raise RunCoordinationConflict(
                f"Run queue item is not queued: {queue_item.id} ({queue_item.status.value})"
            )
        lock: RunLock | None = None
        heartbeat_stop: Event | None = None
        heartbeat_thread: Thread | None = None
        execution_started = False
        try:
            lock = self._acquire_lock(queue_item.run_id, queue_item.action)
            heartbeat_stop, heartbeat_thread = self._start_lock_heartbeat(lock)
            queue_item = self._update_queue_item(
                queue_item,
                RunQueueItemStatus.RUNNING,
                (
                    "Local background worker segment is executing."
                    if queue_item.metadata.get("background_worker_started")
                    else "Local synchronous runner segment is executing."
                ),
            )
            execution_started = True
            result = execute()
            queue_item = self._update_queue_item_for_current_run(queue_item)
            return RunCoordinationResult(result=result, queue_item=queue_item, lock=lock)
        except Exception as exc:
            try:
                if execution_started:
                    self._update_queue_item_after_execution_error(queue_item, exc)
                else:
                    self._update_queue_item(
                        queue_item,
                        RunQueueItemStatus.FAILED,
                        _redact_message(str(exc)),
                    )
            except Exception:
                pass
            raise
        finally:
            if lock is not None:
                if heartbeat_stop is not None and heartbeat_thread is not None:
                    heartbeat_stop.set()
                    heartbeat_thread.join()
                self._release_lock(lock)

    def release_orphaned_locks(self, run_id: str) -> list[RunLock]:
        released: list[RunLock] = []
        now = utc_now()
        for lock in self.storage.list_run_locks_for_run(run_id):
            if lock.status != RunLockStatus.ACQUIRED:
                continue
            recovered = lock.model_copy(
                update={
                    "status": RunLockStatus.RELEASED,
                    "released_at": now,
                    "metadata": {
                        **lock.metadata,
                        "orphaned_lock_recovered": True,
                    },
                }
            )
            self.storage.update_run_lock(recovered)
            released.append(recovered)
        return released

    def reconcile_queue_item(self, item: RunQueueItem) -> RunQueueItem:
        return self._update_queue_item_for_current_run(item)

    def _create_queue_item(
        self,
        run_id: str,
        action: str,
        *,
        background_worker: bool,
    ) -> RunQueueItem:
        now = utc_now()
        item = self.storage.create_run_queue_item(
            RunQueueItem(
                run_id=run_id,
                action=action,
                status=RunQueueItemStatus.QUEUED,
                created_at=now,
                updated_at=now,
                message=(
                    "Persisted local queue item waiting for the background worker."
                    if background_worker
                    else "Local queue intent recorded for synchronous execution."
                ),
                metadata={
                    "local_only": True,
                    "background_worker_started": background_worker,
                },
            )
        )
        return item

    def _update_queue_item(
        self,
        item: RunQueueItem,
        status: RunQueueItemStatus,
        message: str,
    ) -> RunQueueItem:
        updated = item.model_copy(update={"status": status, "updated_at": utc_now(), "message": message})
        for attempt in range(RUN_QUEUE_WRITE_ATTEMPTS):
            try:
                self.storage.update_run_queue_item(updated)
                break
            except StorageError:
                if attempt + 1 >= RUN_QUEUE_WRITE_ATTEMPTS:
                    raise
                sleep(RUN_QUEUE_WRITE_RETRY_SECONDS)
        return updated

    def _update_queue_item_after_execution_error(
        self,
        item: RunQueueItem,
        exc: Exception,
    ) -> RunQueueItem:
        run = self.storage.get_run(item.run_id)
        if run is not None and run.status in {
            RunStatus.WAITING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }:
            return self._update_queue_item_for_current_run(item)
        return self._update_queue_item(
            item,
            RunQueueItemStatus.FAILED,
            _redact_message(str(exc)),
        )

    def _update_queue_item_for_current_run(self, item: RunQueueItem) -> RunQueueItem:
        run = self.storage.get_run(item.run_id)
        if run is None:
            return self._update_queue_item(item, RunQueueItemStatus.FAILED, "Run disappeared during execution.")
        status = _queue_status_for_run_status(run.status)
        message = {
            RunQueueItemStatus.WAITING: "Local runner paused for approval.",
            RunQueueItemStatus.COMPLETED: "Local synchronous runner segment completed.",
            RunQueueItemStatus.FAILED: "Local synchronous runner segment failed.",
            RunQueueItemStatus.CANCELLED: "Local synchronous runner segment was cancelled.",
            RunQueueItemStatus.RUNNING: "Local synchronous runner segment is still running.",
            RunQueueItemStatus.QUEUED: "Local synchronous runner segment is still queued.",
        }[status]
        return self._update_queue_item(item, status, message)

    def _acquire_lock(self, run_id: str, action: str) -> RunLock:
        now = utc_now()
        try:
            return self.storage.create_run_lock(
                RunLock(
                    run_id=run_id,
                    owner=f"api:{action}",
                    status=RunLockStatus.ACQUIRED,
                    acquired_at=now,
                    metadata={
                        "local_only": True,
                        "scope": "run",
                        "stale_after_seconds": RUN_LOCK_STALE_AFTER_SECONDS,
                        "heartbeat_at": now.isoformat(),
                    },
                ),
                stale_after_seconds=RUN_LOCK_STALE_AFTER_SECONDS,
            )
        except StorageError as exc:
            raise RunCoordinationConflict(str(exc)) from exc

    def _start_lock_heartbeat(self, lock: RunLock) -> tuple[Event, Thread]:
        stop = Event()
        thread = Thread(
            target=self._maintain_lock_heartbeat,
            args=(lock.id, stop),
            name=f"run-lock-heartbeat-{lock.run_id}",
            daemon=True,
        )
        thread.start()
        return stop, thread

    def _maintain_lock_heartbeat(self, lock_id: str, stop: Event) -> None:
        while not stop.wait(RUN_LOCK_HEARTBEAT_INTERVAL_SECONDS):
            try:
                current = self.storage.get_run_lock(lock_id)
                if current is None or current.status != RunLockStatus.ACQUIRED:
                    return
                heartbeat_at = utc_now()
                self.storage.update_run_lock(
                    current.model_copy(
                        update={
                            "metadata": {
                                **current.metadata,
                                "heartbeat_at": heartbeat_at.isoformat(),
                            }
                        }
                    )
                )
            except StorageError:
                continue

    def _release_lock(self, lock: RunLock) -> RunLock:
        current = self.storage.get_run_lock(lock.id)
        if current is None or current.status == RunLockStatus.RELEASED:
            return lock
        released = current.model_copy(
            update={"status": RunLockStatus.RELEASED, "released_at": utc_now()}
        )
        self.storage.update_run_lock(released)
        return released


def _queue_status_for_run_status(status: RunStatus) -> RunQueueItemStatus:
    return {
        RunStatus.QUEUED: RunQueueItemStatus.QUEUED,
        RunStatus.RUNNING: RunQueueItemStatus.RUNNING,
        RunStatus.WAITING: RunQueueItemStatus.WAITING,
        RunStatus.COMPLETED: RunQueueItemStatus.COMPLETED,
        RunStatus.FAILED: RunQueueItemStatus.FAILED,
        RunStatus.CANCELLED: RunQueueItemStatus.CANCELLED,
    }[status]


def _redact_message(message: str) -> str:
    redacted = message
    redacted = re.sub(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(Bearer\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(api[_-]?key\s*=\s*)[^\s,;&]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(token\s*=\s*)[^\s,;&]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(secret\s*=\s*)[^\s,;&]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-[REDACTED]", redacted)
    return redacted
