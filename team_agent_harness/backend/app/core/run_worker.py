from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from queue import Queue
from threading import Condition, Event, Lock, Thread
from time import monotonic

from app.core.models import (
    AgentSession,
    Run,
    RunQueueItem,
    RunQueueItemStatus,
    RunStatus,
    RuntimeJob,
    RuntimeJobStatus,
    TraceEventType,
    utc_now,
)
from app.core.run_control import RunCoordinationConflict, RunCoordinator
from app.core.runner import WorkflowRunner
from app.core.runtime_control import RuntimeActionResult, RuntimeControlConflict, RuntimeController
from app.core.storage import SQLiteStorage, StorageError
from app.core.trace import TraceLogger
from app.packs.base import WorkflowPack


class RunWorkerError(RuntimeError):
    pass


RUN_QUEUE_READ_ATTEMPTS = 3
RUN_QUEUE_READ_RETRY_SECONDS = 0.05
RUN_WORKER_STOP_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class BackgroundApprovalSubmission:
    run: Run
    session: AgentSession | None
    job: RuntimeJob
    queue_item: RunQueueItem | None


class RunWorker:
    def __init__(
        self,
        *,
        storage: SQLiteStorage,
        trace_logger: TraceLogger,
        packs: dict[str, WorkflowPack],
        runner_factory: Callable[[], WorkflowRunner],
    ) -> None:
        self.storage = storage
        self.trace_logger = trace_logger
        self.packs = packs
        self.runner_factory = runner_factory
        self._queue: Queue[tuple[str, str] | None] = Queue()
        self._scheduled: set[str] = set()
        self._pending_schedules: dict[str, tuple[str, str]] = {}
        self._scheduled_lock = Lock()
        self._state_lock = Lock()
        self._admission_condition = Condition(self._state_lock)
        self._inflight_admissions = 0
        self._thread: Thread | None = None
        self._stop_requested = Event()
        self._active_queue_item_id: str | None = None
        self._accepting = False

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_running:
            return
        self._queue = Queue()
        with self._scheduled_lock:
            self._scheduled.clear()
            self._pending_schedules.clear()
        with self._admission_condition:
            if self._inflight_admissions:
                raise RunWorkerError("Background run worker still has an in-flight submission.")
            self._stop_requested.clear()
            self._active_queue_item_id = None
            self._accepting = False
        self._recover_interrupted_runs()
        self._thread = Thread(target=self._run_loop, name="team-agent-run-worker", daemon=True)
        self._thread.start()
        self._schedule_persisted_queued_runs()
        with self._state_lock:
            self._accepting = True

    def stop(self, *, timeout: float = RUN_WORKER_STOP_TIMEOUT_SECONDS) -> bool:
        thread = self._thread
        if thread is None:
            with self._admission_condition:
                self._accepting = False
            return True
        deadline = monotonic() + max(timeout, 0.0)
        with self._admission_condition:
            self._accepting = False
            self._stop_requested.set()
        self._queue.put(None)
        with self._admission_condition:
            while self._inflight_admissions:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._admission_condition.wait(timeout=remaining)
        thread.join(timeout=max(0.0, deadline - monotonic()))
        if thread.is_alive():
            return False
        self._thread = None
        with self._scheduled_lock:
            self._scheduled.clear()
            self._pending_schedules.clear()
        return True

    def submit(self, run: Run) -> Run:
        self._begin_admission()
        try:
            submission = RunCoordinator(self.storage, self.trace_logger).enqueue_new_run(
                run,
                "background_start_run",
                background_worker=True,
            )
            self._record("background_run_queued", run.id, submission.queue_item.id)
            self._schedule(submission.queue_item)
            return submission.run
        finally:
            self._finish_admission()

    def approve_and_resume(self, run_id: str, job_id: str) -> BackgroundApprovalSubmission:
        self._begin_admission()
        try:
            existing = self._existing_background_approval(run_id, job_id)
            if existing is not None:
                return existing

            coordinator = RunCoordinator(self.storage, self.trace_logger)
            try:
                submission = coordinator.execute_exclusive(
                    run_id,
                    "background_approve_runtime_job",
                    lambda: self._prepare_background_approval(run_id, job_id, coordinator),
                )
            except RunCoordinationConflict:
                existing = self._existing_background_approval(run_id, job_id)
                if existing is not None:
                    return existing
                raise

            if submission.queue_item is not None:
                self._record(
                    "background_run_queued",
                    submission.run.id,
                    submission.queue_item.id,
                    "approved_resume",
                )
                self._schedule(submission.queue_item)
            return submission
        finally:
            self._finish_admission()

    def _begin_admission(self) -> None:
        with self._admission_condition:
            if not self._accepting or self._stop_requested.is_set():
                raise RunWorkerError("Background run worker is not accepting submissions.")
            self._inflight_admissions += 1

    def _finish_admission(self) -> None:
        with self._admission_condition:
            self._inflight_admissions -= 1
            self._admission_condition.notify_all()

    def _prepare_background_approval(
        self,
        run_id: str,
        job_id: str,
        coordinator: RunCoordinator,
    ) -> BackgroundApprovalSubmission:
        controller = RuntimeController(self.storage, self.trace_logger)
        action_state = controller.get_action_state(run_id, job_id)
        if action_state.job.status == RuntimeJobStatus.COMPLETED:
            return _background_approval_submission(action_state, queue_item=None)
        controller.ensure_current_resumable_job(action_state)
        if action_state.job.status == RuntimeJobStatus.APPROVAL_REQUIRED:
            action_state = controller.approve(run_id, job_id)
        elif action_state.job.status != RuntimeJobStatus.APPROVED:
            raise RuntimeControlConflict(
                f"Runtime job is not resumable after approval: {action_state.job.status.value}"
            )

        with self.storage.transaction():
            run = self.storage.get_run(run_id)
            if run is None:
                raise RunWorkerError(f"Run not found: {run_id}")
            if run.status == RunStatus.WAITING:
                run = self.storage.update_run(
                    run.model_copy(
                        update={
                            "status": RunStatus.QUEUED,
                            "finished_at": None,
                        }
                    )
                )
            elif run.status != RunStatus.QUEUED:
                raise RuntimeControlConflict(
                    f"Approved runtime job cannot resume run from status: {run.status.value}"
                )

            queue_item = self._active_background_queue_item(run.id)
            if queue_item is None:
                queue_item = coordinator.enqueue_existing_run(
                    run.id,
                    "background_approved_resume",
                    background_worker=True,
                )
            return BackgroundApprovalSubmission(
                run=run,
                session=action_state.session,
                job=action_state.job,
                queue_item=queue_item,
            )

    def _existing_background_approval(
        self,
        run_id: str,
        job_id: str,
    ) -> BackgroundApprovalSubmission | None:
        action_state = RuntimeController(self.storage, self.trace_logger).get_action_state(run_id, job_id)
        if action_state.job.status == RuntimeJobStatus.COMPLETED:
            return _background_approval_submission(action_state, queue_item=None)
        if action_state.job.status != RuntimeJobStatus.APPROVED:
            return None
        RuntimeController(self.storage, self.trace_logger).ensure_current_resumable_job(action_state)
        if action_state.run.status not in {RunStatus.QUEUED, RunStatus.RUNNING}:
            return None
        queue_item = self._active_background_queue_item(run_id)
        if queue_item is None:
            return None
        return _background_approval_submission(action_state, queue_item=queue_item)

    def _active_background_queue_item(self, run_id: str) -> RunQueueItem | None:
        matches = [
            item
            for item in self.storage.list_run_queue_items_for_run(run_id)
            if item.status in {RunQueueItemStatus.QUEUED, RunQueueItemStatus.RUNNING}
            and item.metadata.get("background_worker_started") is True
        ]
        return matches[-1] if matches else None

    def _schedule_persisted_queued_runs(self) -> None:
        coordinator = RunCoordinator(self.storage, self.trace_logger)
        for run in self.storage.list_runs_by_statuses({RunStatus.QUEUED}):
            queued_items = [
                item
                for item in self.storage.list_run_queue_items_for_run(run.id)
                if item.status == RunQueueItemStatus.QUEUED
                and item.metadata.get("background_worker_started") is True
            ]
            queue_item = (
                queued_items[-1]
                if queued_items
                else coordinator.enqueue_existing_run(
                    run.id,
                    "background_recovered_queued_run",
                    background_worker=True,
                )
            )
            self._schedule(queue_item)

    def _recover_interrupted_runs(self) -> None:
        coordinator = RunCoordinator(self.storage, self.trace_logger)
        for run in self.storage.list_runs_requiring_worker_recovery():
            coordinator.release_orphaned_locks(run.id)
            queue_items = self.storage.list_run_queue_items_for_run(run.id)
            if run.status == RunStatus.QUEUED:
                self._cancel_interrupted_queue_items(
                    queue_items,
                    statuses={RunQueueItemStatus.RUNNING},
                )
                continue
            if run.status == RunStatus.RUNNING:
                self._cancel_interrupted_queue_items(
                    queue_items,
                    statuses={RunQueueItemStatus.QUEUED, RunQueueItemStatus.RUNNING},
                )
                task = self.storage.get_task(run.task_id)
                pack = self.packs.get(task.workflow_pack) if task is not None else None
                self.runner_factory().requeue_interrupted_run(run.id, pack)
                continue
            if run.status == RunStatus.WAITING:
                controller = RuntimeController(self.storage, self.trace_logger)
                terminal_job = controller.terminal_intent_requiring_recovery(run)
                if terminal_job is not None:
                    with self.storage.transaction():
                        self._cancel_interrupted_queue_items(
                            queue_items,
                            statuses={RunQueueItemStatus.QUEUED, RunQueueItemStatus.RUNNING},
                        )
                        controller.recover_terminal_intent(run.id, terminal_job.id)
                    continue
                if self._has_approved_runtime_job(run):
                    self._cancel_interrupted_queue_items(
                        queue_items,
                        statuses={RunQueueItemStatus.QUEUED, RunQueueItemStatus.RUNNING},
                    )
                    self.storage.update_run(
                        run.model_copy(
                            update={
                                "status": RunStatus.QUEUED,
                                "finished_at": None,
                            }
                        )
                    )
                    self._record("approved_waiting_run_requeued", run.id, "startup-recovery")
                    continue
            if run.status in {
                RunStatus.WAITING,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                for item in queue_items:
                    if item.status not in {RunQueueItemStatus.QUEUED, RunQueueItemStatus.RUNNING}:
                        continue
                    coordinator.reconcile_queue_item(item)

    def _cancel_interrupted_queue_items(
        self,
        queue_items: list[RunQueueItem],
        *,
        statuses: set[RunQueueItemStatus],
    ) -> None:
        for item in queue_items:
            if item.status not in statuses:
                continue
            self.storage.update_run_queue_item(
                item.model_copy(
                    update={
                        "status": RunQueueItemStatus.CANCELLED,
                        "updated_at": utc_now(),
                        "message": "Worker process ended before this queue segment completed.",
                    }
                )
            )

    def _has_approved_runtime_job(self, run: Run) -> bool:
        controller = RuntimeController(self.storage, self.trace_logger)
        for job in self.storage.list_runtime_jobs_for_run(run.id):
            if job.status != RuntimeJobStatus.APPROVED:
                continue
            try:
                controller.ensure_current_resumable_job(controller.get_action_state(run.id, job.id))
            except RuntimeControlConflict:
                continue
            return True
        return False

    def _schedule(self, queue_item: RunQueueItem) -> None:
        wake_item = (queue_item.id, queue_item.run_id)
        with self._scheduled_lock:
            if queue_item.run_id in self._scheduled:
                self._pending_schedules[queue_item.run_id] = wake_item
                return
            self._scheduled.add(queue_item.run_id)
        self._queue.put(wake_item)

    def _run_loop(self) -> None:
        while not self._stop_requested.is_set():
            wake_item = self._queue.get()
            if wake_item is None:
                return
            queue_item_id, scheduled_run_id = wake_item
            keep_scheduled = False
            queue_item: RunQueueItem | None = None
            try:
                if self._stop_requested.is_set():
                    return
                try:
                    queue_item = self._get_queue_item_with_retry(queue_item_id)
                except StorageError:
                    if not self._stop_requested.is_set():
                        self._queue.put(wake_item)
                        keep_scheduled = True
                        self._stop_requested.wait(RUN_QUEUE_READ_RETRY_SECONDS)
                    continue
                if queue_item is None:
                    continue
                with self._state_lock:
                    if self._stop_requested.is_set():
                        return
                    self._active_queue_item_id = queue_item.id
                try:
                    self._execute(queue_item)
                finally:
                    with self._state_lock:
                        if self._active_queue_item_id == queue_item.id:
                            self._active_queue_item_id = None
            except StorageError:
                if not self._stop_requested.is_set():
                    self._queue.put(wake_item)
                    keep_scheduled = True
                    self._stop_requested.wait(RUN_QUEUE_READ_RETRY_SECONDS)
            except Exception as exc:
                if queue_item is not None:
                    self._terminalize_unhandled_failure(queue_item)
                    self._record(
                        "background_run_failed",
                        queue_item.run_id,
                        queue_item.id,
                        exc.__class__.__name__,
                    )
            finally:
                if not keep_scheduled:
                    pending_wake_item = None
                    with self._scheduled_lock:
                        pending_wake_item = self._pending_schedules.pop(scheduled_run_id, None)
                        if pending_wake_item is None:
                            self._scheduled.discard(scheduled_run_id)
                    if pending_wake_item is not None:
                        self._queue.put(pending_wake_item)

    def _get_queue_item_with_retry(self, queue_item_id: str) -> RunQueueItem | None:
        for attempt in range(RUN_QUEUE_READ_ATTEMPTS):
            try:
                return self.storage.get_run_queue_item(queue_item_id)
            except StorageError:
                if attempt + 1 >= RUN_QUEUE_READ_ATTEMPTS or self._stop_requested.is_set():
                    raise
                self._stop_requested.wait(RUN_QUEUE_READ_RETRY_SECONDS)
        return None

    def _execute(self, queue_item: RunQueueItem) -> None:
        run = self.storage.get_run(queue_item.run_id)
        if run is None:
            return
        if run.status != RunStatus.QUEUED:
            if queue_item.status in {RunQueueItemStatus.QUEUED, RunQueueItemStatus.RUNNING}:
                RunCoordinator(self.storage, self.trace_logger).reconcile_queue_item(queue_item)
            return
        task = self.storage.get_task(run.task_id)
        pack = self.packs.get(task.workflow_pack) if task is not None else None
        if task is None or pack is None:
            now = utc_now()
            failed = run.model_copy(update={"status": RunStatus.FAILED, "finished_at": now})
            self.storage.update_run(failed)
            self.storage.update_run_queue_item(
                queue_item.model_copy(
                    update={
                        "status": RunQueueItemStatus.FAILED,
                        "updated_at": now,
                        "message": "Background run configuration is unavailable.",
                    }
                )
            )
            self._record("background_run_failed", run.id, queue_item.id, "configuration")
            return

        self._record("background_run_started", run.id, queue_item.id)
        try:
            result = RunCoordinator(self.storage, self.trace_logger).execute_queue_item(
                queue_item.id,
                lambda: self.runner_factory().run(run, pack),
            ).result
        except StorageError:
            raise
        except Exception as exc:
            self._terminalize_unhandled_failure(queue_item)
            self._record("background_run_failed", run.id, queue_item.id, exc.__class__.__name__)
            return
        self._record(
            "background_run_completed",
            run.id,
            queue_item.id,
            result.status.value,
        )

    def _record(
        self,
        action: str,
        run_id: str,
        queue_item_id: str,
        outcome: str | None = None,
    ) -> None:
        payload = {"action": action, "queue_item_id": queue_item_id}
        if outcome is not None:
            payload["outcome"] = outcome
        try:
            self.trace_logger.record(
                run_id=run_id,
                event_type=TraceEventType.RUNTIME_EVENT,
                payload=payload,
            )
        except Exception:
            return

    def _terminalize_unhandled_failure(self, queue_item: RunQueueItem) -> None:
        try:
            RuntimeController(self.storage, self.trace_logger).terminalize_open_runtime_state(
                queue_item.run_id,
                reason="background_worker_internal_error",
            )
        except Exception:
            pass
        try:
            current_item = self.storage.get_run_queue_item(queue_item.id)
            if current_item is not None and current_item.status in {
                RunQueueItemStatus.QUEUED,
                RunQueueItemStatus.RUNNING,
            }:
                self.storage.update_run_queue_item(
                    current_item.model_copy(
                        update={
                            "status": RunQueueItemStatus.FAILED,
                            "updated_at": utc_now(),
                            "message": "Background worker stopped this queue segment after an internal error.",
                        }
                    )
                )
        except Exception:
            pass
        try:
            current_run = self.storage.get_run(queue_item.run_id)
            if current_run is not None and current_run.status in {
                RunStatus.QUEUED,
                RunStatus.RUNNING,
                RunStatus.WAITING,
            }:
                self.storage.update_run(
                    current_run.model_copy(
                        update={"status": RunStatus.FAILED, "finished_at": utc_now()}
                    )
                )
        except Exception:
            pass


def _background_approval_submission(
    action_state: RuntimeActionResult,
    *,
    queue_item: RunQueueItem | None,
) -> BackgroundApprovalSubmission:
    return BackgroundApprovalSubmission(
        run=action_state.run,
        session=action_state.session,
        job=action_state.job,
        queue_item=queue_item,
    )
