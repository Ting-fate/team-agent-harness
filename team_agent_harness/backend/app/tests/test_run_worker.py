from threading import Event, Thread
from time import monotonic, sleep

from fastapi.testclient import TestClient
import pytest

from app.core import run_control
from app.core.models import (
    AgentRun,
    AgentRunStatus,
    AgentSessionStatus,
    ArtifactType,
    EvalResult,
    EvalStatus,
    Handoff,
    Run,
    RunLock,
    RunLockStatus,
    RunQueueItem,
    RunQueueItemStatus,
    RunStatus,
    RuntimeJobStatus,
    Task,
    TraceEventType,
    utc_now,
)
from app.core.runner import AgentArtifactOutput, AgentStepOutput
from app.core.storage import StorageError
from app.main import create_app


class BlockingExecutor:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()
        self.steps: list[str] = []

    def execute(self, *, task, run, step, agent, context) -> AgentStepOutput:
        self.steps.append(step.name)
        if not self.started.is_set():
            self.started.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("blocking executor was not released")
        artifact_type = step.produces_artifact_type or ArtifactType.FINAL_REPORT.value
        return AgentStepOutput(
            summary=f"completed {step.name}",
            artifacts=[
                AgentArtifactOutput(
                    type=artifact_type,
                    filename=f"{step.name}.md",
                    content=f"# {step.name}\n",
                )
            ],
            eval_results=[
                EvalResult(run_id=run.id, check_name=check_name, status=EvalStatus.PASS)
                for check_name in step.required_eval_checks
            ],
        )


class RecordingExecutor:
    def __init__(self) -> None:
        self.steps: list[str] = []

    def execute(self, *, task, run, step, agent, context) -> AgentStepOutput:
        self.steps.append(step.name)
        artifact_type = step.produces_artifact_type or ArtifactType.FINAL_REPORT.value
        return AgentStepOutput(
            summary=f"completed {step.name}",
            artifacts=[
                AgentArtifactOutput(
                    type=artifact_type,
                    filename=f"{step.name}.md",
                    content=f"# {step.name}\n",
                )
            ],
            eval_results=[
                EvalResult(run_id=run.id, check_name=check_name, status=EvalStatus.PASS)
                for check_name in step.required_eval_checks
            ],
        )


class ApprovalBlockingExecutor(RecordingExecutor):
    def __init__(self, blocked_step: str) -> None:
        super().__init__()
        self.blocked_step = blocked_step
        self.started = Event()
        self.release = Event()

    def execute(self, *, task, run, step, agent, context) -> AgentStepOutput:
        if step.name == self.blocked_step:
            self.started.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("approval executor was not released")
        output = super().execute(task=task, run=run, step=step, agent=agent, context=context)
        return AgentStepOutput(
            summary=output.summary,
            artifacts=output.artifacts,
            risk_notes=["No unresolved test risk."],
            eval_results=output.eval_results,
        )


def test_new_run_and_initial_queue_item_are_persisted_atomically(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness
    task = state.storage.create_task(
        Task(
            id="atomic-enqueue-task",
            title="Atomic enqueue",
            goal="Never persist a queued run without its durable queue item.",
            workflow_pack="code_rd",
        )
    )
    run = Run(id="atomic-enqueue-run", task_id=task.id)
    original_create_queue_item = state.storage.create_run_queue_item

    def fail_queue_insert(_item):
        raise StorageError("injected initial queue insert failure")

    monkeypatch.setattr(state.storage, "create_run_queue_item", fail_queue_insert)
    try:
        with pytest.raises(StorageError, match="injected initial queue insert failure"):
            run_control.RunCoordinator(state.storage, state.trace_logger).enqueue_new_run(
                run,
                "background_start_run",
                background_worker=True,
            )

        assert state.storage.get_run(run.id) is None
        assert state.storage.list_run_queue_items_for_run(run.id) == []

        monkeypatch.setattr(state.storage, "create_run_queue_item", original_create_queue_item)
        submission = run_control.RunCoordinator(state.storage, state.trace_logger).enqueue_new_run(
            run,
            "background_start_run",
            background_worker=True,
        )
        assert submission.run == run
        assert submission.queue_item.run_id == run.id
    finally:
        state.close()


def test_background_submission_linearizes_before_shutdown(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness
    worker = state.run_worker
    task = state.storage.create_task(
        Task(
            id="shutdown-race-task",
            title="Shutdown race",
            goal="Do not accept work after worker shutdown linearizes.",
            workflow_pack="code_rd",
        )
    )
    run = Run(id="shutdown-race-run", task_id=task.id)
    entered_persistence = Event()
    allow_persistence = Event()
    stop_finished = Event()
    submit_errors: list[Exception] = []
    stop_results: list[bool] = []
    original_enqueue = run_control.RunCoordinator.enqueue_new_run

    def pause_enqueue(coordinator, *args, **kwargs):
        entered_persistence.set()
        if not allow_persistence.wait(timeout=5):
            raise RuntimeError("submission persistence was not released")
        return original_enqueue(coordinator, *args, **kwargs)

    def submit() -> None:
        try:
            worker.submit(run)
        except Exception as exc:  # pragma: no cover - asserted below
            submit_errors.append(exc)

    def stop() -> None:
        stop_results.append(worker.stop(timeout=2))
        stop_finished.set()

    monkeypatch.setattr(run_control.RunCoordinator, "enqueue_new_run", pause_enqueue)
    worker.start()
    submit_thread = Thread(target=submit)
    stop_thread = Thread(target=stop)
    try:
        submit_thread.start()
        assert entered_persistence.wait(timeout=1)
        stop_thread.start()
        shutdown_overtook_submission = stop_finished.wait(timeout=0.1)
        allow_persistence.set()
        submit_thread.join(timeout=2)
        stop_thread.join(timeout=2)

        assert shutdown_overtook_submission is False
        assert submit_errors == []
        assert stop_results == [True]
        persisted = state.storage.get_run(run.id)
        assert persisted is not None and persisted.status == RunStatus.QUEUED
        active_queue_items = [
            item
            for item in state.storage.list_run_queue_items_for_run(run.id)
            if item.status in {RunQueueItemStatus.QUEUED, RunQueueItemStatus.RUNNING}
        ]
        assert len(active_queue_items) == 1
    finally:
        allow_persistence.set()
        submit_thread.join(timeout=2)
        stop_thread.join(timeout=2)
        worker.stop(timeout=2)
        state.close()


def test_shutdown_timeout_includes_inflight_submission(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness
    worker = state.run_worker
    task = state.storage.create_task(
        Task(
            id="shutdown-timeout-task",
            title="Shutdown timeout",
            goal="Bound shutdown while accepted persistence is stalled.",
            workflow_pack="code_rd",
        )
    )
    run = Run(id="shutdown-timeout-run", task_id=task.id)
    entered_persistence = Event()
    allow_persistence = Event()
    submit_errors: list[Exception] = []
    original_enqueue = run_control.RunCoordinator.enqueue_new_run

    def pause_enqueue(coordinator, *args, **kwargs):
        entered_persistence.set()
        if not allow_persistence.wait(timeout=5):
            raise RuntimeError("submission persistence was not released")
        return original_enqueue(coordinator, *args, **kwargs)

    def submit() -> None:
        try:
            worker.submit(run)
        except Exception as exc:  # pragma: no cover - asserted below
            submit_errors.append(exc)

    monkeypatch.setattr(run_control.RunCoordinator, "enqueue_new_run", pause_enqueue)
    worker.start()
    submit_thread = Thread(target=submit)
    try:
        submit_thread.start()
        assert entered_persistence.wait(timeout=1)
        started = monotonic()
        assert worker.stop(timeout=0.05) is False
        elapsed = monotonic() - started

        assert elapsed < 0.5
        allow_persistence.set()
        submit_thread.join(timeout=2)
        assert submit_errors == []
        assert worker.stop(timeout=1) is True
    finally:
        allow_persistence.set()
        submit_thread.join(timeout=2)
        worker.stop(timeout=2)
        state.close()


def test_background_approval_linearizes_before_shutdown(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    worker = app.state.harness.run_worker
    entered_approval = Event()
    allow_approval = Event()
    stop_finished = Event()
    approval_errors: list[Exception] = []
    stop_results: list[bool] = []

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Approval shutdown race",
                "goal": "Persist and schedule approval before shutdown linearizes.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        waiting_run = client.post("/runs", json={"task_id": task["id"]}).json()
        patch_job = next(
            job
            for job in client.get(f"/runs/{waiting_run['id']}/runtime-jobs").json()
            if job["step_name"] == "prepare_patch"
        )
        original_prepare = worker._prepare_background_approval

        def pause_prepare(*args, **kwargs):
            entered_approval.set()
            if not allow_approval.wait(timeout=5):
                raise RuntimeError("background approval was not released")
            return original_prepare(*args, **kwargs)

        def approve() -> None:
            try:
                worker.approve_and_resume(waiting_run["id"], patch_job["id"])
            except Exception as exc:  # pragma: no cover - asserted below
                approval_errors.append(exc)

        def stop() -> None:
            stop_results.append(worker.stop(timeout=2))
            stop_finished.set()

        monkeypatch.setattr(worker, "_prepare_background_approval", pause_prepare)
        approval_thread = Thread(target=approve)
        stop_thread = Thread(target=stop)
        try:
            approval_thread.start()
            assert entered_approval.wait(timeout=1)
            stop_thread.start()
            shutdown_overtook_approval = stop_finished.wait(timeout=0.1)
            allow_approval.set()
            approval_thread.join(timeout=2)
            stop_thread.join(timeout=2)

            assert shutdown_overtook_approval is False
            assert approval_errors == []
            assert stop_results == [True]
            persisted_job = app.state.harness.storage.get_runtime_job(patch_job["id"])
            assert persisted_job is not None
            assert persisted_job.status in {RuntimeJobStatus.APPROVED, RuntimeJobStatus.COMPLETED}
            assert any(
                item.metadata.get("background_worker_started") is True
                for item in app.state.harness.storage.list_run_queue_items_for_run(waiting_run["id"])
            )
        finally:
            allow_approval.set()
            approval_thread.join(timeout=2)
            stop_thread.join(timeout=2)


def test_background_run_returns_before_executor_completes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(run_control, "RUN_LOCK_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    executor = BlockingExecutor()
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=lambda: executor,
    )
    background_run_completed = _observe_worker_action(
        monkeypatch,
        app.state.harness.run_worker,
        "background_run_completed",
        expected_outcome=RunStatus.COMPLETED.value,
    )
    original_update_run_lock = app.state.harness.storage.update_run_lock
    heartbeat_failures = 0

    def fail_first_heartbeat(lock):
        nonlocal heartbeat_failures
        if lock.status == RunLockStatus.ACQUIRED and heartbeat_failures == 0:
            heartbeat_failures += 1
            raise StorageError("transient heartbeat failure")
        return original_update_run_lock(lock)

    monkeypatch.setattr(app.state.harness.storage, "update_run_lock", fail_first_heartbeat)

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Background run",
                "goal": "Complete outside the request thread.",
                "workflow_pack": "code_rd",
            },
        ).json()

        response = client.post("/runs", json={"task_id": task["id"], "background": True})

        assert response.status_code == 201
        submitted = response.json()
        assert submitted["status"] in {RunStatus.QUEUED.value, RunStatus.RUNNING.value}
        assert executor.started.wait(timeout=5)
        assert client.get(f"/runs/{submitted['id']}").json()["status"] == RunStatus.RUNNING.value
        active_lock = app.state.harness.storage.get_active_run_lock(submitted["id"])
        assert active_lock is not None
        first_heartbeat = active_lock.metadata["heartbeat_at"]
        _wait_for_lock_heartbeat(app.state.harness.storage, active_lock.id, first_heartbeat)

        executor.release.set()
        _wait_for_event(background_run_completed, "background run completion")
        completed = app.state.harness.storage.get_run(submitted["id"])

        assert completed is not None
        assert completed.status == RunStatus.COMPLETED
        assert heartbeat_failures == 1
        _wait_for_queue_status(
            app.state.harness.storage,
            submitted["id"],
            [RunQueueItemStatus.COMPLETED],
        )
        queue_state = client.get(f"/runs/{submitted['id']}/queue-state").json()
        assert [item["status"] for item in queue_state] == ["completed"]


def test_background_approval_returns_before_resumed_step_and_is_idempotent(tmp_path) -> None:
    executor = ApprovalBlockingExecutor("prepare_patch")
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=lambda: executor,
    )

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Background approval",
                "goal": "Resume an approved long step outside the HTTP request.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        waiting_run = client.post("/runs", json={"task_id": task["id"]}).json()
        assert waiting_run["status"] == RunStatus.WAITING.value
        patch_job = next(
            job
            for job in client.get(f"/runs/{waiting_run['id']}/runtime-jobs").json()
            if job["step_name"] == "prepare_patch"
        )

        response = client.post(
            f"/runs/{waiting_run['id']}/runtime-jobs/{patch_job['id']}/approve?background=true"
        )
        assert response.status_code == 202
        assert response.json()["run"]["status"] in {
            RunStatus.QUEUED.value,
            RunStatus.RUNNING.value,
        }
        assert executor.started.wait(timeout=5)

        duplicate = client.post(
            f"/runs/{waiting_run['id']}/runtime-jobs/{patch_job['id']}/approve?background=true"
        )
        assert duplicate.status_code == 202
        active_items = [
            item
            for item in app.state.harness.storage.list_run_queue_items_for_run(waiting_run["id"])
            if item.status in {RunQueueItemStatus.QUEUED, RunQueueItemStatus.RUNNING}
            and item.metadata.get("background_worker_started") is True
        ]
        assert len(active_items) == 1

        executor.release.set()
        resumed = _wait_for_run_status(
            client,
            waiting_run["id"],
            RunStatus.WAITING.value,
        )
        assert resumed["current_step"] == "test_changes"

        queue_count = len(app.state.harness.storage.list_run_queue_items_for_run(waiting_run["id"]))
        completed_retry = client.post(
            f"/runs/{waiting_run['id']}/runtime-jobs/{patch_job['id']}/approve?background=true"
        )
        assert completed_retry.status_code == 200
        assert completed_retry.json()["run"]["current_step"] == "test_changes"
        assert len(app.state.harness.storage.list_run_queue_items_for_run(waiting_run["id"])) == queue_count


def test_background_approval_queues_next_segment_during_previous_segment_cleanup(tmp_path, monkeypatch) -> None:
    executor = ApprovalBlockingExecutor("prepare_patch")
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=lambda: executor,
    )
    worker = app.state.harness.run_worker
    assert worker is not None
    execute = worker._execute
    previous_segment_returned = Event()
    allow_previous_cleanup = Event()

    def pause_after_first_approval_segment(queue_item):
        execute(queue_item)
        if queue_item.action == "background_approved_resume" and not previous_segment_returned.is_set():
            previous_segment_returned.set()
            if not allow_previous_cleanup.wait(timeout=5):
                raise RuntimeError("previous queue segment cleanup was not released")

    monkeypatch.setattr(worker, "_execute", pause_after_first_approval_segment)
    background_run_completed = _observe_worker_action(
        monkeypatch,
        worker,
        "background_run_completed",
        expected_outcome=RunStatus.COMPLETED.value,
    )

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Back-to-back background approvals",
                "goal": "Do not lose the next durable wake-up during queue cleanup.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        waiting_run = client.post("/runs", json={"task_id": task["id"]}).json()
        patch_job = next(
            job
            for job in client.get(f"/runs/{waiting_run['id']}/runtime-jobs").json()
            if job["step_name"] == "prepare_patch"
        )
        first = client.post(
            f"/runs/{waiting_run['id']}/runtime-jobs/{patch_job['id']}/approve?background=true"
        )
        assert first.status_code == 202
        _wait_for_event(executor.started, "approved step execution")
        executor.release.set()
        _wait_for_event(previous_segment_returned, "previous segment return")

        current_run = client.get(f"/runs/{waiting_run['id']}").json()
        assert current_run["status"] == RunStatus.WAITING.value
        assert current_run["current_step"] == "test_changes"
        test_job = next(
            job
            for job in client.get(f"/runs/{waiting_run['id']}/runtime-jobs").json()
            if job["step_name"] == "test_changes"
        )
        second = client.post(
            f"/runs/{waiting_run['id']}/runtime-jobs/{test_job['id']}/approve?background=true"
        )
        assert second.status_code == 202

        allow_previous_cleanup.set()
        _wait_for_event(background_run_completed, "background run completion")
        completed = app.state.harness.storage.get_run(waiting_run["id"])
        assert completed is not None
        assert completed.status == RunStatus.COMPLETED
        assert app.state.harness.storage.get_runtime_job(test_job["id"]).status == RuntimeJobStatus.COMPLETED


def test_old_approved_job_cannot_resume_current_approval_live_or_after_restart(tmp_path) -> None:
    db_path = tmp_path / "harness.sqlite3"
    artifact_root = tmp_path / "artifacts"
    app = create_app(db_path, artifact_root)

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Reject stale approval",
                "goal": "Only the current runtime job may resume a waiting run.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        first_wait = client.post("/runs", json={"task_id": task["id"]}).json()
        first_job = next(
            job
            for job in client.get(f"/runs/{first_wait['id']}/runtime-jobs").json()
            if job["step_name"] == "prepare_patch"
        )
        second_wait = client.post(
            f"/runs/{first_wait['id']}/runtime-jobs/{first_job['id']}/approve"
        ).json()["run"]
        assert second_wait["status"] == RunStatus.WAITING.value
        assert second_wait["current_step"] == "test_changes"

        stale_job = app.state.harness.storage.get_runtime_job(first_job["id"])
        assert stale_job is not None and stale_job.status == RuntimeJobStatus.COMPLETED
        app.state.harness.storage.update_runtime_job(
            stale_job.model_copy(update={"status": RuntimeJobStatus.APPROVED})
        )
        queue_count = len(app.state.harness.storage.list_run_queue_items_for_run(first_wait["id"]))

        stale_retry = client.post(
            f"/runs/{first_wait['id']}/runtime-jobs/{first_job['id']}/approve?background=true"
        )
        assert stale_retry.status_code == 409
        persisted_run = client.get(f"/runs/{first_wait['id']}").json()
        assert persisted_run["status"] == RunStatus.WAITING.value
        assert persisted_run["current_step"] == "test_changes"
        assert len(app.state.harness.storage.list_run_queue_items_for_run(first_wait["id"])) == queue_count

    recovered_app = create_app(db_path, artifact_root)
    with TestClient(recovered_app) as client:
        sleep(0.1)
        recovered_run = client.get(f"/runs/{first_wait['id']}").json()
        assert recovered_run["status"] == RunStatus.WAITING.value
        assert recovered_run["current_step"] == "test_changes"
        active_items = [
            item
            for item in recovered_app.state.harness.storage.list_run_queue_items_for_run(first_wait["id"])
            if item.status in {RunQueueItemStatus.QUEUED, RunQueueItemStatus.RUNNING}
        ]
        assert active_items == []


def test_worker_requeues_interrupted_run_from_last_completed_step(tmp_path) -> None:
    executor = RecordingExecutor()
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=lambda: executor,
    )
    state = app.state.harness
    pack = state.packs["code_rd"]
    task = state.storage.create_task(
        Task(
            id="recovery-task",
            title="Recover run",
            goal="Continue after a hard interruption.",
            workflow_pack=pack.name,
        )
    )
    run = state.storage.create_run(
        Run(
            id="recovery-run",
            task_id=task.id,
            status=RunStatus.RUNNING,
            current_step="design_implementation",
            started_at=utc_now(),
        )
    )
    original_started_at = run.started_at
    for agent in pack.agents:
        state.storage.upsert_agent_definition(agent)

    clarifier = next(agent for agent in pack.agents if agent.role == "Clarifier")
    completed = state.storage.create_agent_run(
        AgentRun(
            id="completed-clarifier",
            run_id=run.id,
            agent_id=clarifier.id,
            step_name="clarify_requirements",
            status=AgentRunStatus.COMPLETED,
            started_at=utc_now(),
            finished_at=utc_now(),
            output_summary="requirements complete",
        )
    )
    state.artifact_store.write_text(
        run_id=run.id,
        agent_run_id=completed.id,
        artifact_type=ArtifactType.SOURCE_SUMMARY,
        filename="clarify_requirements-1-clarify_requirements.md",
        content="# requirements\n",
    )

    architect = next(agent for agent in pack.agents if agent.role == "Architect")
    state.storage.create_handoff(
        Handoff(
            run_id=run.id,
            from_agent_run_id=completed.id,
            to_agent_id=architect.id,
            summary="requirements complete",
            artifact_refs=[
                artifact.id
                for artifact in state.storage.list_artifacts_for_run(run.id)
                if artifact.agent_run_id == completed.id
            ],
            next_objective="design_implementation",
        )
    )
    state.trace_logger.record(
        run_id=run.id,
        agent_run_id=completed.id,
        event_type=TraceEventType.EVAL_RESULT,
        payload={
            "check_name": "clarify_requirements:artifacts_created",
            "status": "pass",
        },
    )
    interrupted = state.storage.create_agent_run(
        AgentRun(
            id="interrupted-architect",
            run_id=run.id,
            agent_id=architect.id,
            step_name="design_implementation",
            status=AgentRunStatus.RUNNING,
            started_at=utc_now(),
        )
    )
    orphan = state.artifact_store.write_text(
        run_id=run.id,
        agent_run_id=interrupted.id,
        artifact_type=ArtifactType.DESIGN_DOC,
        filename="design_implementation-1-design_implementation.md",
        content="# incomplete design\n",
    )
    old_queue_item = state.storage.create_run_queue_item(
        RunQueueItem(
            id="interrupted-queue-item",
            run_id=run.id,
            action="background_start_run",
            status=RunQueueItemStatus.RUNNING,
            message="worker stopped",
            metadata={"background_worker_started": True},
        )
    )
    old_lock = state.storage.create_run_lock(
        RunLock(
            id="interrupted-lock",
            run_id=run.id,
            owner="api:background_start_run",
            status=RunLockStatus.ACQUIRED,
            acquired_at=utc_now(),
        )
    )

    with TestClient(app) as client:
        recovered = _wait_for_terminal_run(client, run.id)

        assert recovered["status"] == RunStatus.COMPLETED.value
        assert state.storage.get_run(run.id).started_at == original_started_at  # type: ignore[union-attr]
        assert executor.steps == [
            "design_implementation",
            "prepare_patch",
            "test_changes",
            "review_delivery",
            "finalize_delivery",
        ]
        attempts = [
            agent_run
            for agent_run in state.storage.list_agent_runs_for_run(run.id)
            if agent_run.step_name == "design_implementation"
        ]
        assert [attempt.status for attempt in attempts] == [
            AgentRunStatus.CANCELLED,
            AgentRunStatus.COMPLETED,
        ]
        design_artifacts = [
            artifact
            for artifact in state.storage.list_artifacts_for_run(run.id)
            if artifact.type == ArtifactType.DESIGN_DOC
        ]
        assert design_artifacts[0].id == orphan.id
        assert len({artifact.path for artifact in design_artifacts}) == 2
        assert "attempt-2" in design_artifacts[1].path

        recovered_lock = state.storage.get_run_lock(old_lock.id)
        assert recovered_lock is not None
        assert recovered_lock.status == RunLockStatus.RELEASED
        queue_items = state.storage.list_run_queue_items_for_run(run.id)
        assert queue_items[0].id == old_queue_item.id
        assert [item.status for item in queue_items] == [
            RunQueueItemStatus.CANCELLED,
            RunQueueItemStatus.COMPLETED,
        ]
        actions = [event.payload.get("action") for event in state.storage.list_trace_events_for_run(run.id)]
        assert "interrupted_run_requeued" in actions


def test_worker_recovers_queued_run_with_running_queue_item_and_orphaned_lock(tmp_path) -> None:
    executor = RecordingExecutor()
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=lambda: executor,
    )
    state = app.state.harness
    task = state.storage.create_task(
        Task(
            id="queued-recovery-task",
            title="Recover pre-start crash",
            goal="Recover a crash between queue activation and run activation.",
            workflow_pack="code_rd",
        )
    )
    run = state.storage.create_run(
        Run(id="queued-recovery-run", task_id=task.id, status=RunStatus.QUEUED)
    )
    old_queue_item = state.storage.create_run_queue_item(
        RunQueueItem(
            id="queued-recovery-item",
            run_id=run.id,
            action="background_start_run",
            status=RunQueueItemStatus.RUNNING,
            metadata={"background_worker_started": True},
        )
    )
    old_lock = state.storage.create_run_lock(
        RunLock(
            id="queued-recovery-lock",
            run_id=run.id,
            owner="api:background_start_run",
            status=RunLockStatus.ACQUIRED,
            acquired_at=utc_now(),
        )
    )

    with TestClient(app) as client:
        recovered = _wait_for_terminal_run(client, run.id)

        assert recovered["status"] == RunStatus.COMPLETED.value
        assert executor.steps
        persisted_lock = state.storage.get_run_lock(old_lock.id)
        assert persisted_lock is not None
        assert persisted_lock.status == RunLockStatus.RELEASED
        _wait_for_queue_status(
            state.storage,
            run.id,
            [RunQueueItemStatus.CANCELLED, RunQueueItemStatus.COMPLETED],
        )
        queue_items = state.storage.list_run_queue_items_for_run(run.id)
        assert queue_items[0].id == old_queue_item.id
        assert [item.status for item in queue_items] == [
            RunQueueItemStatus.CANCELLED,
            RunQueueItemStatus.COMPLETED,
        ]


def test_worker_retries_transient_queue_item_read_without_stopping(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness
    original_get = state.storage.get_run_queue_item
    failures = 0
    background_run_completed = _observe_worker_action(
        monkeypatch,
        state.run_worker,
        "background_run_completed",
        expected_outcome=RunStatus.COMPLETED.value,
    )

    def fail_first_queue_item_read(item_id):
        nonlocal failures
        if failures == 0:
            failures += 1
            raise StorageError("transient queue read failure")
        return original_get(item_id)

    monkeypatch.setattr(state.storage, "get_run_queue_item", fail_first_queue_item_read)

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Transient queue read",
                "goal": "Complete after one transient queue read failure.",
                "workflow_pack": "code_rd",
            },
        ).json()
        submitted = client.post(
            "/runs",
            json={"task_id": task["id"], "background": True},
        ).json()
        _wait_for_event(background_run_completed, "background run completion")
        completed = state.storage.get_run(submitted["id"])

        assert completed is not None
        assert completed.status == RunStatus.COMPLETED
        assert failures == 1
        assert state.run_worker.is_running is True
        _wait_for_worker_unscheduled(state.run_worker, submitted["id"])


def test_worker_recovers_storage_error_after_run_activation(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness
    original_get_task = state.storage.get_task
    original_update_queue_item = state.storage.update_run_queue_item
    original_record = state.run_worker._record
    injected = False
    terminal_queue_write_started = Event()
    background_run_completed = Event()
    allow_terminal_queue_write = Event()
    terminal_window: dict[str, object] = {}

    def fail_once_after_run_activation(task_id):
        nonlocal injected
        if not injected and state.storage.list_runs_by_statuses({RunStatus.RUNNING}):
            injected = True
            raise StorageError("transient task read after run activation")
        return original_get_task(task_id)

    def pause_first_terminal_queue_write(item):
        should_pause = (
            item.status == RunQueueItemStatus.COMPLETED
            and not terminal_queue_write_started.is_set()
        )
        if should_pause:
            persisted_run = state.storage.get_run(item.run_id)
            persisted_item = state.storage.get_run_queue_item(item.id)
            terminal_window["run_status"] = persisted_run.status if persisted_run else None
            terminal_window["queue_status"] = persisted_item.status if persisted_item else None
            terminal_queue_write_started.set()
            if not allow_terminal_queue_write.wait(timeout=15):
                raise RuntimeError("terminal queue write was not released")
        return original_update_queue_item(item)

    def observe_worker_record(action, run_id, queue_item_id, outcome=None):
        original_record(action, run_id, queue_item_id, outcome)
        if action == "background_run_completed":
            background_run_completed.set()

    monkeypatch.setattr(state.storage, "get_task", fail_once_after_run_activation)
    monkeypatch.setattr(
        state.storage,
        "update_run_queue_item",
        pause_first_terminal_queue_write,
    )
    monkeypatch.setattr(state.run_worker, "_record", observe_worker_record)

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Recover activated storage failure",
                "goal": "Resume without leaving RUNNING/FAILED state behind.",
                "workflow_pack": "code_rd",
            },
        ).json()
        submitted = client.post(
            "/runs",
            json={"task_id": task["id"], "background": True},
        ).json()
        try:
            _wait_for_event(
                terminal_queue_write_started,
                "terminal queue write",
            )
            assert terminal_window == {
                "run_status": RunStatus.COMPLETED,
                "queue_status": RunQueueItemStatus.RUNNING,
            }
        finally:
            allow_terminal_queue_write.set()

        _wait_for_event(
            background_run_completed,
            "background run completion",
        )

        completed = state.storage.get_run(submitted["id"])
        assert completed is not None
        assert injected is True
        assert completed.status == RunStatus.COMPLETED
        queue_items = state.storage.list_run_queue_items_for_run(submitted["id"])
        assert queue_items[-1].status == RunQueueItemStatus.COMPLETED
        assert state.run_worker.is_running is True


def test_worker_stop_leaves_backlog_persisted_and_restartable(tmp_path) -> None:
    executor = BlockingExecutor()
    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=lambda: executor,
    )
    state = app.state.harness

    with TestClient(app) as client:
        task_ids = []
        for index in range(2):
            task = client.post(
                "/tasks",
                json={
                    "title": f"Shutdown task {index}",
                    "goal": "Verify graceful shutdown does not drain queued backlog.",
                    "workflow_pack": "code_rd",
                },
            ).json()
            task_ids.append(task["id"])

        first = client.post(
            "/runs",
            json={"task_id": task_ids[0], "background": True},
        ).json()
        assert executor.started.wait(timeout=1)
        second = client.post(
            "/runs",
            json={"task_id": task_ids[1], "background": True},
        ).json()

        assert state.run_worker.stop(timeout=0.01) is False
        assert executor.steps == ["clarify_requirements"]
        executor.release.set()
        assert state.run_worker.stop(timeout=5) is True
        assert _wait_for_terminal_run(client, first["id"])["status"] == RunStatus.COMPLETED.value
        assert client.get(f"/runs/{second['id']}").json()["status"] == RunStatus.QUEUED.value
        assert [
            item.status for item in state.storage.list_run_queue_items_for_run(second["id"])
        ] == [RunQueueItemStatus.QUEUED]

        state.run_worker.start()
        assert _wait_for_terminal_run(client, second["id"])["status"] == RunStatus.COMPLETED.value


def test_worker_retries_terminal_queue_write_without_state_contradiction(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness
    original_update = state.storage.update_run_queue_item
    failures = 0
    background_run_completed = _observe_worker_action(
        monkeypatch,
        state.run_worker,
        "background_run_completed",
        expected_outcome=RunStatus.COMPLETED.value,
    )

    def fail_first_terminal_queue_write(item):
        nonlocal failures
        if item.status == RunQueueItemStatus.COMPLETED and failures == 0:
            failures += 1
            raise StorageError("transient terminal queue write failure")
        return original_update(item)

    monkeypatch.setattr(state.storage, "update_run_queue_item", fail_first_terminal_queue_write)

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Terminal queue retry",
                "goal": "Keep terminal run and queue state consistent.",
                "workflow_pack": "code_rd",
            },
        ).json()
        submitted = client.post(
            "/runs",
            json={"task_id": task["id"], "background": True},
        ).json()
        _wait_for_event(background_run_completed, "background run completion")
        completed = state.storage.get_run(submitted["id"])

        assert completed is not None
        assert completed.status == RunStatus.COMPLETED
        _wait_for_queue_status(
            state.storage,
            submitted["id"],
            [RunQueueItemStatus.COMPLETED],
        )
        assert failures == 1
        assert state.run_worker.is_running is True


def test_worker_retries_transient_terminal_lock_release_failure(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness
    original_update = state.storage.update_run_lock
    failures = 0
    background_run_completed = _observe_worker_action(
        monkeypatch,
        state.run_worker,
        "background_run_completed",
        expected_outcome=RunStatus.COMPLETED.value,
    )

    def fail_first_release(lock):
        nonlocal failures
        if lock.status == RunLockStatus.RELEASED and failures == 0:
            failures += 1
            raise StorageError("transient lock release failure")
        return original_update(lock)

    monkeypatch.setattr(state.storage, "update_run_lock", fail_first_release)

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Lock release retry",
                "goal": "Release the run lock after a transient storage failure.",
                "workflow_pack": "code_rd",
            },
        ).json()
        submitted = client.post(
            "/runs",
            json={"task_id": task["id"], "background": True},
        ).json()
        _wait_for_event(background_run_completed, "background run completion")
        completed = state.storage.get_run(submitted["id"])

        assert completed is not None
        assert completed.status == RunStatus.COMPLETED
        locks = state.storage.list_run_locks_for_run(submitted["id"])
        assert locks and all(lock.status == RunLockStatus.RELEASED for lock in locks)
        assert failures == 1
        assert state.run_worker.is_running is True


def test_worker_reconciles_terminal_queue_state_after_persistent_write_failure(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "harness.sqlite3"
    artifact_root = tmp_path / "artifacts"
    app = create_app(db_path, artifact_root)
    state = app.state.harness
    original_update = state.storage.update_run_queue_item
    fail_terminal_writes = True
    terminal_queue_write_attempted = Event()

    def fail_terminal_queue_writes(item):
        if fail_terminal_writes and item.status == RunQueueItemStatus.COMPLETED:
            terminal_queue_write_attempted.set()
            raise StorageError("persistent terminal queue write failure")
        return original_update(item)

    monkeypatch.setattr(state.storage, "update_run_queue_item", fail_terminal_queue_writes)

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Terminal queue restart recovery",
                "goal": "Reconcile a terminal run after queue persistence recovers.",
                "workflow_pack": "code_rd",
            },
        ).json()
        submitted = client.post(
            "/runs",
            json={"task_id": task["id"], "background": True},
        ).json()
        _wait_for_event(terminal_queue_write_attempted, "terminal queue write attempt")
        completed = state.storage.get_run(submitted["id"])
        assert completed is not None
        assert completed.status == RunStatus.COMPLETED
        assert state.storage.list_run_queue_items_for_run(submitted["id"])[0].status == RunQueueItemStatus.RUNNING

    fail_terminal_writes = False
    recovered_app = create_app(db_path, artifact_root)
    with TestClient(recovered_app):
        _wait_for_queue_status(
            recovered_app.state.harness.storage,
            submitted["id"],
            [RunQueueItemStatus.COMPLETED],
        )


def test_worker_requeues_persisted_waiting_approved_run_on_startup(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "harness.sqlite3"
    artifact_root = tmp_path / "artifacts"
    app = create_app(db_path, artifact_root)
    state = app.state.harness

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Recover approved run",
                "goal": "Continue approved work after the approving request is interrupted.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
        patch_job = next(job for job in jobs if job["step_name"] == "prepare_patch")
        original_record = state.trace_logger.record

        def fail_approval_trace(*, run_id, event_type, payload, agent_run_id=None, duration_ms=None):
            if payload.get("action") == "runtime_job_approved":
                raise StorageError("approval request interrupted after persistence")
            return original_record(
                run_id=run_id,
                event_type=event_type,
                payload=payload,
                agent_run_id=agent_run_id,
                duration_ms=duration_ms,
            )

        monkeypatch.setattr(state.trace_logger, "record", fail_approval_trace)
        response = client.post(f"/runs/{run['id']}/runtime-jobs/{patch_job['id']}/approve")
        assert response.status_code == 400
        persisted_job = state.storage.get_runtime_job(patch_job["id"])
        assert persisted_job is not None
        assert persisted_job.status == RuntimeJobStatus.APPROVED

    recovered_app = create_app(db_path, artifact_root)
    with TestClient(recovered_app) as client:
        completed_job = _wait_for_runtime_job_status(
            client,
            run["id"],
            patch_job["id"],
            RuntimeJobStatus.COMPLETED.value,
        )
        recovered_run = _wait_for_run_status(
            client,
            run["id"],
            RunStatus.WAITING.value,
        )

        assert completed_job["status"] == RuntimeJobStatus.COMPLETED.value
        assert recovered_run["status"] == RunStatus.WAITING.value
        assert recovered_run["current_step"] == "test_changes"


@pytest.mark.parametrize(
    ("job_status", "job_step", "expected_session_status", "expected_action"),
    [
        (
            RuntimeJobStatus.REJECTED,
            "prepare_patch",
            AgentSessionStatus.REJECTED,
            "runtime_job_rejection_recovered",
        ),
        (
            RuntimeJobStatus.CANCELLED,
            "test_changes",
            AgentSessionStatus.CANCELLED,
            "runtime_job_cancellation_recovered",
        ),
    ],
)
def test_worker_terminalizes_legacy_partial_runtime_intent_on_startup(
    tmp_path,
    job_status,
    job_step,
    expected_session_status,
    expected_action,
) -> None:
    db_path = tmp_path / "harness.sqlite3"
    artifact_root = tmp_path / "artifacts"
    app = create_app(db_path, artifact_root)
    state = app.state.harness

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": f"Recover partial {job_status.value} intent",
                "goal": "Finish a terminal runtime intent left by an older non-atomic action.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        run = client.post("/runs", json={"task_id": task["id"]}).json()
        if job_step == "test_changes":
            initial_jobs = client.get(f"/runs/{run['id']}/runtime-jobs").json()
            prepare_patch_job = next(
                job for job in initial_jobs if job["step_name"] == "prepare_patch"
            )
            approve_patch = client.post(
                f"/runs/{run['id']}/runtime-jobs/{prepare_patch_job['id']}/approve"
            )
            assert approve_patch.status_code == 200
        patch_job = next(
            job
            for job in client.get(f"/runs/{run['id']}/runtime-jobs").json()
            if job["step_name"] == job_step
        )
        stored_job = state.storage.get_runtime_job(patch_job["id"])
        assert stored_job is not None
        stored_session = state.storage.get_agent_session(stored_job.agent_session_id)
        stored_agent_run = state.storage.get_agent_run(stored_job.agent_run_id)
        assert stored_session is not None
        assert stored_agent_run is not None

        # Simulate an older process that persisted only the terminal job intent.
        state.storage.update_runtime_job(
            stored_job.model_copy(update={"status": job_status, "updated_at": utc_now()})
        )
        assert state.storage.get_run(run["id"]).status == RunStatus.WAITING  # type: ignore[union-attr]
        assert state.storage.get_agent_session(stored_session.id).status == AgentSessionStatus.WAITING_APPROVAL  # type: ignore[union-attr]
        assert state.storage.get_agent_run(stored_agent_run.id).status == AgentRunStatus.WAITING  # type: ignore[union-attr]

    recovered_app = create_app(db_path, artifact_root)
    with TestClient(recovered_app):
        recovered_storage = recovered_app.state.harness.storage
        recovered_run = recovered_storage.get_run(run["id"])
        recovered_job = recovered_storage.get_runtime_job(patch_job["id"])
        recovered_session = recovered_storage.get_agent_session(stored_session.id)
        recovered_agent_run = recovered_storage.get_agent_run(stored_agent_run.id)

        assert recovered_run is not None
        assert recovered_run.status == RunStatus.CANCELLED
        assert recovered_run.finished_at is not None
        assert recovered_job is not None and recovered_job.status == job_status
        assert recovered_session is not None and recovered_session.status == expected_session_status
        assert recovered_agent_run is not None
        assert recovered_agent_run.status == AgentRunStatus.CANCELLED
        assert recovered_agent_run.finished_at is not None
        assert all(
            job.status
            in {
                RuntimeJobStatus.COMPLETED,
                RuntimeJobStatus.FAILED,
                RuntimeJobStatus.REJECTED,
                RuntimeJobStatus.CANCELLED,
            }
            for job in recovered_storage.list_runtime_jobs_for_run(run["id"])
        )
        assert all(
            session.status
            in {
                AgentSessionStatus.COMPLETED,
                AgentSessionStatus.FAILED,
                AgentSessionStatus.REJECTED,
                AgentSessionStatus.CANCELLED,
            }
            for session in recovered_storage.list_agent_sessions_for_run(run["id"])
        )
        assert expected_action in {
            event.payload.get("action")
            for event in recovered_storage.list_trace_events_for_run(run["id"])
        }


def test_worker_terminalizes_queue_item_when_workflow_pack_is_missing(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness
    task = state.storage.create_task(
        Task(
            id="missing-pack-task",
            title="Missing pack",
            goal="Fail with consistent persisted state.",
            workflow_pack="missing-pack",
        )
    )
    run = state.storage.create_run(Run(id="missing-pack-run", task_id=task.id))
    queue_item = state.storage.create_run_queue_item(
        RunQueueItem(
            id="missing-pack-queue",
            run_id=run.id,
            action="background_start_run",
            metadata={"background_worker_started": True},
        )
    )

    with TestClient(app) as client:
        failed = _wait_for_terminal_run(client, run.id)

        assert failed["status"] == RunStatus.FAILED.value
        assert failed["finished_at"] is not None
        persisted_queue_item = state.storage.get_run_queue_item(queue_item.id)
        assert persisted_queue_item is not None
        assert persisted_queue_item.status == RunQueueItemStatus.FAILED


def test_worker_missing_pack_terminalizes_open_runtime_state(tmp_path) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Missing pack with approval state",
                "goal": "Cancel every open runtime record when configuration disappears.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        waiting = client.post("/runs", json={"task_id": task["id"]}).json()
        assert waiting["status"] == RunStatus.WAITING.value
        run = state.storage.get_run(waiting["id"])
        assert run is not None
        open_agent_run_ids = [
            agent_run.id
            for agent_run in state.storage.list_agent_runs_for_run(run.id)
            if agent_run.status not in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.CANCELLED,
            }
        ]
        open_session_ids = [
            session.id
            for session in state.storage.list_agent_sessions_for_run(run.id)
            if session.status not in {
                AgentSessionStatus.COMPLETED,
                AgentSessionStatus.FAILED,
                AgentSessionStatus.REJECTED,
                AgentSessionStatus.CANCELLED,
            }
        ]
        open_job_ids = [
            job.id
            for job in state.storage.list_runtime_jobs_for_run(run.id)
            if job.status not in {
                RuntimeJobStatus.COMPLETED,
                RuntimeJobStatus.FAILED,
                RuntimeJobStatus.REJECTED,
                RuntimeJobStatus.CANCELLED,
            }
        ]
        assert open_agent_run_ids and open_session_ids and open_job_ids
        state.storage.update_run(run.model_copy(update={"status": RunStatus.QUEUED}))
        queue_item = state.storage.create_run_queue_item(
            RunQueueItem(
                id="missing-pack-open-runtime-queue",
                run_id=run.id,
                action="background_approved_resume",
                metadata={"background_worker_started": True},
            )
        )
        state.run_worker.packs.pop("code_rd_institutional")

        state.run_worker._execute(queue_item)

        assert state.storage.get_run(run.id).status == RunStatus.FAILED  # type: ignore[union-attr]
        assert all(
            state.storage.get_agent_run(agent_run_id).status == AgentRunStatus.CANCELLED  # type: ignore[union-attr]
            for agent_run_id in open_agent_run_ids
        )
        assert all(
            state.storage.get_agent_session(session_id).status == AgentSessionStatus.CANCELLED  # type: ignore[union-attr]
            for session_id in open_session_ids
        )
        assert all(
            state.storage.get_runtime_job(job_id).status == RuntimeJobStatus.CANCELLED  # type: ignore[union-attr]
            for job_id in open_job_ids
        )


def test_worker_rolls_back_missing_pack_terminal_state_when_queue_write_fails(
    tmp_path,
    monkeypatch,
) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness
    task = state.storage.create_task(
        Task(
            id="missing-pack-task",
            title="Missing pack",
            goal="Keep run and queue state atomic.",
            workflow_pack="missing-pack",
        )
    )
    run = state.storage.create_run(Run(id="missing-pack-run", task_id=task.id))
    queue_item = state.storage.create_run_queue_item(
        RunQueueItem(
            id="missing-pack-queue",
            run_id=run.id,
            action="background_start_run",
            metadata={"background_worker_started": True},
        )
    )
    original_update = state.storage.update_run_queue_item

    def fail_terminal_queue_write(item):
        if item.id == queue_item.id and item.status == RunQueueItemStatus.FAILED:
            raise StorageError("terminal queue write failed")
        return original_update(item)

    monkeypatch.setattr(state.storage, "update_run_queue_item", fail_terminal_queue_write)

    with pytest.raises(StorageError, match="terminal queue write failed"):
        state.run_worker._execute(queue_item)

    persisted_run = state.storage.get_run(run.id)
    persisted_queue_item = state.storage.get_run_queue_item(queue_item.id)
    assert persisted_run is not None
    assert persisted_queue_item is not None
    assert persisted_run.status == RunStatus.QUEUED
    assert persisted_run.finished_at is None
    assert persisted_queue_item.status == RunQueueItemStatus.QUEUED


def test_run_loop_retries_atomic_terminalization_after_unhandled_queue_write_failure(
    tmp_path,
    monkeypatch,
) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness
    task = state.storage.create_task(
        Task(
            id="missing-pack-task",
            title="Missing pack",
            goal="Retry the full terminal transaction from the real worker loop.",
            workflow_pack="missing-pack",
        )
    )
    run = state.storage.create_run(Run(id="missing-pack-run", task_id=task.id))
    queue_item = state.storage.create_run_queue_item(
        RunQueueItem(
            id="missing-pack-queue",
            run_id=run.id,
            action="background_start_run",
            metadata={"background_worker_started": True},
        )
    )
    original_update = state.storage.update_run_queue_item
    terminal_attempts = 0

    def fail_first_terminal_queue_write(item):
        nonlocal terminal_attempts
        if item.id == queue_item.id and item.status == RunQueueItemStatus.FAILED:
            terminal_attempts += 1
            if terminal_attempts == 1:
                raise RuntimeError("first terminal queue write failed")
        return original_update(item)

    monkeypatch.setattr(state.storage, "update_run_queue_item", fail_first_terminal_queue_write)

    with TestClient(app) as client:
        failed = _wait_for_terminal_run(client, run.id)
        persisted_queue_item = state.storage.get_run_queue_item(queue_item.id)
        assert failed["status"] == RunStatus.FAILED.value
        assert persisted_queue_item is not None
        assert persisted_queue_item.status == RunQueueItemStatus.FAILED
        assert terminal_attempts >= 2


def test_worker_terminalizes_run_when_executor_factory_fails(tmp_path) -> None:
    def failing_executor_factory():
        raise RuntimeError("injected executor factory failure")

    app = create_app(
        tmp_path / "harness.sqlite3",
        tmp_path / "artifacts",
        executor_factory=failing_executor_factory,
    )

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Executor factory failure",
                "goal": "Fail with consistent persisted run and queue state.",
                "workflow_pack": "code_rd",
            },
        ).json()
        submitted = client.post(
            "/runs",
            json={"task_id": task["id"], "background": True},
        ).json()

        persisted_run = _wait_for_terminal_run(client, submitted["id"])
        assert persisted_run["status"] == RunStatus.FAILED.value
        assert persisted_run["finished_at"] is not None
        queue_items = app.state.harness.storage.list_run_queue_items_for_run(submitted["id"])
        assert queue_items and queue_items[-1].status == RunQueueItemStatus.FAILED
        assert app.state.harness.run_worker.is_running is True


def test_worker_terminalizes_waiting_run_after_segment_internal_failure(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    state = app.state.harness
    failure_injected = Event()
    terminalization_finished = Event()
    original_update = run_control.RunCoordinator._update_queue_item_for_current_run
    original_record = state.run_worker._record

    def fail_after_run_waits(coordinator, item):
        persisted_run = coordinator.storage.get_run(item.run_id)
        if persisted_run is not None and persisted_run.status == RunStatus.WAITING:
            failure_injected.set()
            raise RuntimeError("injected queue segment finalization failure")
        return original_update(coordinator, item)

    monkeypatch.setattr(
        run_control.RunCoordinator,
        "_update_queue_item_for_current_run",
        fail_after_run_waits,
    )

    def observe_worker_record(action, run_id, queue_item_id, outcome=None):
        original_record(action, run_id, queue_item_id, outcome)
        if action == "background_run_failed":
            terminalization_finished.set()

    monkeypatch.setattr(state.run_worker, "_record", observe_worker_record)

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Waiting segment failure",
                "goal": "Fail consistently after the run reaches an approval wait.",
                "workflow_pack": "code_rd_institutional",
            },
        ).json()
        submitted = client.post(
            "/runs",
            json={"task_id": task["id"], "background": True},
        ).json()

        _wait_for_event(
            failure_injected,
            "waiting-run failure injection",
        )
        _wait_for_event(
            terminalization_finished,
            "waiting-run terminalization",
        )
        persisted_run = state.storage.get_run(submitted["id"])
        queue_items = state.storage.list_run_queue_items_for_run(submitted["id"])
        assert persisted_run is not None
        assert persisted_run.status == RunStatus.FAILED
        assert persisted_run.finished_at is not None
        assert [item.status for item in queue_items] == [RunQueueItemStatus.FAILED]
        agent_runs = state.storage.list_agent_runs_for_run(submitted["id"])
        sessions = state.storage.list_agent_sessions_for_run(submitted["id"])
        jobs = state.storage.list_runtime_jobs_for_run(submitted["id"])
        assert agent_runs and any(item.status == AgentRunStatus.CANCELLED for item in agent_runs)
        assert all(
            item.status in {AgentRunStatus.COMPLETED, AgentRunStatus.FAILED, AgentRunStatus.CANCELLED}
            for item in agent_runs
        )
        assert all(item.finished_at is not None for item in agent_runs)
        assert sessions and any(item.status == AgentSessionStatus.CANCELLED for item in sessions)
        assert all(
            item.status
            in {
                AgentSessionStatus.COMPLETED,
                AgentSessionStatus.FAILED,
                AgentSessionStatus.REJECTED,
                AgentSessionStatus.CANCELLED,
            }
            for item in sessions
        )
        assert jobs and any(item.status == RuntimeJobStatus.CANCELLED for item in jobs)
        assert all(
            item.status
            in {
                RuntimeJobStatus.COMPLETED,
                RuntimeJobStatus.FAILED,
                RuntimeJobStatus.REJECTED,
                RuntimeJobStatus.CANCELLED,
            }
            for item in jobs
        )
        assert state.run_worker.is_running is True


def test_worker_trace_failure_does_not_interrupt_background_run(tmp_path, monkeypatch) -> None:
    app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    worker = app.state.harness.run_worker
    original_record = worker.trace_logger.record
    failed_actions: list[str] = []
    background_run_completed = _observe_worker_action(
        monkeypatch,
        worker,
        "background_run_completed",
        expected_outcome=RunStatus.COMPLETED.value,
    )

    def fail_background_trace(*, run_id, event_type, payload, agent_run_id=None, duration_ms=None):
        action = payload.get("action")
        if action in {"background_run_queued", "background_run_started"}:
            failed_actions.append(action)
            raise StorageError("transient trace failure")
        return original_record(
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            agent_run_id=agent_run_id,
            duration_ms=duration_ms,
        )

    monkeypatch.setattr(worker.trace_logger, "record", fail_background_trace)

    with TestClient(app) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "Trace failure",
                "goal": "Complete even when background trace writes fail.",
                "workflow_pack": "code_rd",
            },
        ).json()
        submitted = client.post(
            "/runs",
            json={"task_id": task["id"], "background": True},
        ).json()
        _wait_for_event(background_run_completed, "background run completion")
        completed = app.state.harness.storage.get_run(submitted["id"])

        assert completed is not None
        assert completed.status == RunStatus.COMPLETED
        assert failed_actions == ["background_run_queued", "background_run_started"]
        assert worker.is_running is True


def _wait_for_terminal_run(client: TestClient, run_id: str, timeout: float = 5) -> dict[str, object]:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] in {RunStatus.COMPLETED.value, RunStatus.FAILED.value}:
            return run
        sleep(0.02)
    raise AssertionError(f"run did not finish within {timeout} seconds: {run_id}")


def _wait_for_event(
    event: Event,
    description: str,
    timeout: float = 15,
) -> None:
    if event.wait(timeout=timeout):
        return
    raise AssertionError(f"{description} was not observed within {timeout} seconds")


def _observe_worker_action(
    monkeypatch,
    worker,
    expected_action: str,
    *,
    expected_outcome: str | None = None,
) -> Event:
    observed = Event()
    original_record = worker._record

    def observe(action, run_id, queue_item_id, outcome=None):
        original_record(action, run_id, queue_item_id, outcome)
        if action == expected_action and (
            expected_outcome is None or outcome == expected_outcome
        ):
            observed.set()

    monkeypatch.setattr(worker, "_record", observe)
    return observed


def _wait_for_lock_heartbeat(storage, lock_id: str, previous: str, timeout: float = 1) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        lock = storage.get_run_lock(lock_id)
        if lock is not None and lock.metadata.get("heartbeat_at") != previous:
            return
        sleep(0.01)
    raise AssertionError(f"lock heartbeat did not advance within {timeout} seconds: {lock_id}")


def _wait_for_queue_status(storage, run_id: str, expected, timeout: float = 1) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        statuses = [item.status for item in storage.list_run_queue_items_for_run(run_id)]
        if statuses == expected:
            return
        sleep(0.01)
    raise AssertionError(f"queue state did not reach {expected}: {run_id}")


def _wait_for_worker_unscheduled(worker, run_id: str, timeout: float = 1) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        with worker._scheduled_lock:
            if run_id not in worker._scheduled:
                return
        sleep(0.01)
    raise AssertionError(f"worker did not clear scheduled run: {run_id}")


def _wait_for_runtime_job_status(
    client: TestClient,
    run_id: str,
    job_id: str,
    expected: str,
    timeout: float = 5,
) -> dict[str, object]:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        jobs = client.get(f"/runs/{run_id}/runtime-jobs").json()
        job = next(item for item in jobs if item["id"] == job_id)
        if job["status"] == expected:
            return job
        sleep(0.02)
    raise AssertionError(f"runtime job did not reach {expected}: {job_id}")


def _wait_for_run_status(
    client: TestClient,
    run_id: str,
    expected: str,
    timeout: float = 5,
) -> dict[str, object]:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] == expected:
            return run
        sleep(0.02)
    raise AssertionError(f"run did not reach {expected}: {run_id}")
