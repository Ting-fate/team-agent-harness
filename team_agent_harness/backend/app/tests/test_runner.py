from __future__ import annotations

import time
from datetime import UTC, datetime
from threading import Lock
from typing import Any

import pytest

from app.core.artifacts import ArtifactStore
from app.core.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    AgentSessionStatus,
    Artifact,
    ArtifactType,
    EvalResult,
    EvalStatus,
    Run,
    RunStatus,
    RuntimeJobStatus,
    Task,
    TraceEventType,
)
from app.core.registry import AgentRegistry
from app.core.model_runtime import ModelRuntimeError
from app.core.runner import (
    AgentArtifactOutput,
    AgentStepOutput,
    WorkflowRunner,
    WorkflowRunnerError,
)
from app.core.runtime_control import RuntimeController
from app.core.storage import SQLiteStorage, StorageError
from app.core.trace import TraceLogger
from app.packs.base import ContextPolicy, EvalCheck, ReturnContract, SessionPolicy, WorkflowPack, WorkflowStep


@pytest.fixture
def storage(tmp_path):
    with SQLiteStorage(tmp_path / "harness.sqlite3") as db:
        db.init_schema()
        yield db


@pytest.fixture
def runner_factory(tmp_path, storage: SQLiteStorage):
    def make_runner(executor: Any | None = None) -> WorkflowRunner:
        trace_logger = TraceLogger(storage)
        artifact_store = ArtifactStore(tmp_path / "artifacts", storage, trace_logger)
        return WorkflowRunner(
            storage=storage,
            registry=AgentRegistry(),
            artifact_store=artifact_store,
            trace_logger=trace_logger,
            executor=executor,
        )

    return make_runner


def test_runner_completes_two_step_workflow_with_artifacts_handoff_trace_and_eval(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Run a demo workflow.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
            constraints=["No real model calls."],
        )
    )
    pack = _demo_pack()
    run = Run(id="run-1", task_id=task.id)

    final_run = runner_factory(DemoExecutor()).run(run, pack)

    assert final_run.status == RunStatus.COMPLETED
    assert final_run.current_step is None
    agent_runs = storage.list_agent_runs_for_run(final_run.id)
    assert [agent_run.step_name for agent_run in agent_runs] == ["plan", "write"]
    assert {agent_run.status for agent_run in agent_runs} == {AgentRunStatus.COMPLETED}

    artifacts = storage.list_artifacts_for_run(final_run.id)
    assert len(artifacts) == 2
    assert {artifact.type for artifact in artifacts} == {ArtifactType.RESEARCH_NOTE, ArtifactType.FINAL_REPORT}
    assert final_run.final_artifact_id == artifacts[-1].id
    assert [artifact.path.split("/")[-1] for artifact in artifacts] == [
        "plan-1-output.md",
        "write-1-output.md",
    ]

    handoffs = storage.list_handoffs_for_run(final_run.id)
    assert len(handoffs) == 1
    assert handoffs[0].from_agent_run_id == agent_runs[0].id
    assert handoffs[0].to_agent_id == "agent-writer"
    assert handoffs[0].constraints_to_preserve == ["No real model calls."]

    eval_results = storage.list_eval_results_for_run(final_run.id)
    assert {result.check_name for result in eval_results} == {
        "plan:artifacts_created",
        "write:artifacts_created",
        "final_report_present",
    }
    assert {result.status for result in eval_results} == {EvalStatus.PASS}

    event_types = [event.event_type for event in storage.list_trace_events_for_run(final_run.id)]
    assert TraceEventType.ARTIFACT_CREATED in event_types
    assert TraceEventType.HANDOFF in event_types
    assert TraceEventType.EVAL_RESULT in event_types
    assert TraceEventType.WORKFLOW_EVENT in event_types
    assert TraceEventType.ERROR not in event_types
    workflow_events = [
        event for event in storage.list_trace_events_for_run(final_run.id) if event.event_type == TraceEventType.WORKFLOW_EVENT
    ]
    assert workflow_events[0].payload["action"] == "task_intake_analyzed"
    context_events = [event for event in workflow_events if event.payload.get("action") == "context_envelope_built"]
    assert len(context_events) == 2
    assert "state_breadcrumb" in context_events[0].payload["context_keys"]


def test_runner_persists_completed_only_after_runtime_cleanup_succeeds(
    storage: SQLiteStorage,
    runner_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = storage.create_task(
        Task(
            id="task-cleanup-order",
            title="Cleanup ordering",
            goal="Do not expose a completed state before terminal cleanup succeeds.",
            workflow_pack="demo",
        )
    )
    runner = runner_factory(DemoExecutor())
    original_terminalize = runner._terminalize_open_runtime_state
    observed_statuses: list[tuple[str, RunStatus]] = []

    def fail_completed_cleanup_once(run_id: str, *, reason: str) -> None:
        persisted = storage.get_run(run_id)
        assert persisted is not None
        observed_statuses.append((reason, persisted.status))
        if reason == "run_completed":
            raise StorageError("injected terminal cleanup failure")
        original_terminalize(run_id, reason=reason)

    monkeypatch.setattr(runner, "_terminalize_open_runtime_state", fail_completed_cleanup_once)

    with pytest.raises(WorkflowRunnerError, match="terminal cleanup failure"):
        runner.run(Run(id="run-cleanup-order", task_id=task.id), _demo_pack())

    assert observed_statuses[0] == ("run_completed", RunStatus.RUNNING)
    persisted_run = storage.get_run("run-cleanup-order")
    assert persisted_run is not None
    assert persisted_run.status == RunStatus.FAILED


def test_default_runner_executor_records_mock_model_runtime_trace(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Run through the model runtime.",
            workflow_pack="demo",
            inputs={"brief": "Use the default executor."},
        )
    )
    pack = _single_step_pack()
    run = Run(id="run-1", task_id=task.id)

    final_run = runner_factory().run(run, pack)

    assert final_run.status == RunStatus.COMPLETED
    trace = storage.list_trace_events_for_run(final_run.id)
    model_requests = [
        event for event in trace if event.event_type == TraceEventType.MODEL_ACTION and event.payload.get("action") == "model_request"
    ]
    model_responses = [
        event for event in trace if event.event_type == TraceEventType.MODEL_ACTION and event.payload.get("action") == "model_response"
    ]
    assert len(model_requests) == 1
    assert len(model_responses) == 1
    assert {
        event.payload.get("action")
        for event in trace
        if event.event_type == TraceEventType.MODEL_ACTION
    } == {"model_request", "model_response"}
    assert model_requests[0].payload["provider"] == "mock"
    assert model_requests[0].payload["model"] == "demo-model"
    assert model_requests[0].payload["agent_id"] == "agent-writer"
    assert model_requests[0].payload["step_name"] == "write"
    assert model_requests[0].payload["adapter"] == "mock"
    assert model_requests[0].payload["mocked"] is True
    assert model_requests[0].payload["tools_allowed"] == ["write_artifact"]
    assert model_responses[0].payload["model"] == "demo-model"
    assert model_responses[0].payload["agent_id"] == "agent-writer"
    assert model_responses[0].payload["step_name"] == "write"
    assert model_responses[0].payload["adapter"] == "mock"
    assert model_responses[0].payload["mocked"] is True
    assert model_responses[0].payload["usage"]["output_tokens"] > 0
    assert model_responses[0].payload["latency_ms"] >= 1


def test_runner_marks_run_and_current_agent_run_failed_when_executor_raises(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Run a demo workflow.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    run = Run(id="run-1", task_id=task.id)

    with pytest.raises(WorkflowRunnerError, match="boom"):
        runner_factory(FailingSecondStepExecutor()).run(run, _demo_pack())

    failed_run = storage.get_run(run.id)
    assert failed_run is not None
    assert failed_run.status == RunStatus.FAILED
    assert failed_run.current_step == "write"

    agent_runs = storage.list_agent_runs_for_run(run.id)
    assert [(agent_run.step_name, agent_run.status) for agent_run in agent_runs] == [
        ("plan", AgentRunStatus.COMPLETED),
        ("write", AgentRunStatus.FAILED),
    ]
    assert len(storage.list_artifacts_for_run(run.id)) == 1

    errors = [
        event for event in storage.list_trace_events_for_run(run.id) if event.event_type == TraceEventType.ERROR
    ]
    assert len(errors) == 1
    assert errors[0].agent_run_id == agent_runs[1].id
    assert errors[0].payload["step_name"] == "write"
    assert errors[0].payload["agent_id"] == "agent-writer"
    assert errors[0].payload["error_type"] == "RuntimeError"
    assert "boom" in errors[0].payload["message"]


def test_run_task_marks_run_failed_when_pack_is_missing(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(id="task-1", title="Demo task", goal="Run a demo workflow.", workflow_pack="missing")
    )

    with pytest.raises(WorkflowRunnerError, match="Workflow pack not found"):
        runner_factory().run_task(task.id, packs={})

    runs = storage.list_runs()
    assert len(runs) == 1
    assert runs[0].task_id == task.id
    assert runs[0].status == RunStatus.FAILED
    errors = [event for event in storage.list_trace_events_for_run(runs[0].id) if event.event_type == TraceEventType.ERROR]
    assert errors[0].payload["message"] == "Workflow pack not found: missing"


def test_runner_rejects_existing_non_queued_run(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Run a demo workflow.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    storage.create_run(Run(id="run-1", task_id=task.id, status=RunStatus.COMPLETED))

    with pytest.raises(WorkflowRunnerError, match="not queued"):
        runner_factory().run(Run(id="run-1", task_id=task.id), _demo_pack())

    assert storage.get_run("run-1").status == RunStatus.COMPLETED  # type: ignore[union-attr]
    assert storage.list_agent_runs_for_run("run-1") == []


def test_runner_updates_existing_pack_agent_definition_for_same_id_and_role(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    storage.create_agent_definition(
        AgentDefinition(
            id="agent-writer",
            pack_name="demo",
            role="Writer",
            system_prompt="Old writer.",
            model_config={"provider": "mock", "model": "old-model"},
        )
    )
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Run through an updated pack agent definition.",
            workflow_pack="demo",
            inputs={"brief": "Use the default executor."},
        )
    )

    final_run = runner_factory().run(Run(id="run-1", task_id=task.id), _single_step_pack())

    assert final_run.status == RunStatus.COMPLETED
    stored_agent = storage.get_agent_definition("agent-writer")
    assert stored_agent is not None
    assert stored_agent.system_prompt == "Write work."
    assert stored_agent.model_settings["model"] == "demo-model"


def test_runner_rejects_existing_pack_agent_definition_with_different_id(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    storage.create_agent_definition(
        AgentDefinition(
            id="agent-writer-old",
            pack_name="demo",
            role="Writer",
            system_prompt="Old writer.",
            model_config={"provider": "mock", "model": "old-model"},
        )
    )
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Reject conflicting pack agent definition.",
            workflow_pack="demo",
            inputs={"brief": "Use the default executor."},
        )
    )

    with pytest.raises(WorkflowRunnerError, match="Stored agent definition conflict"):
        runner_factory().run(Run(id="run-1", task_id=task.id), _single_step_pack())

    assert storage.list_agent_runs_for_run("run-1") == []
    assert storage.get_agent_definition("agent-writer") is None


def test_runner_fails_when_step_requires_missing_input(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(id="task-1", title="Demo task", goal="Run a demo workflow.", workflow_pack="demo")
    )
    pack = _demo_pack(first_step_inputs=["brief"])
    run = Run(id="run-1", task_id=task.id)

    with pytest.raises(WorkflowRunnerError, match="missing required inputs: brief"):
        runner_factory().run(run, pack)

    assert storage.get_run(run.id).status == RunStatus.FAILED  # type: ignore[union-attr]
    agent_runs = storage.list_agent_runs_for_run(run.id)
    assert len(agent_runs) == 1
    assert agent_runs[0].status == AgentRunStatus.FAILED


def test_runner_fails_when_step_requires_missing_artifact(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Run a demo workflow.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    pack = _demo_pack(second_step_artifacts=["patch"])
    run = Run(id="run-1", task_id=task.id)

    with pytest.raises(WorkflowRunnerError, match="missing required artifacts: patch"):
        runner_factory().run(run, pack)

    agent_runs = storage.list_agent_runs_for_run(run.id)
    assert [(agent_run.step_name, agent_run.status) for agent_run in agent_runs] == [
        ("plan", AgentRunStatus.COMPLETED),
        ("write", AgentRunStatus.FAILED),
    ]
    assert len(storage.list_artifacts_for_run(run.id)) == 1


def test_blocker_eval_failure_marks_run_failed_but_warning_does_not(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Run a demo workflow.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    warning_pack = _demo_pack(
        eval_checks=[
            EvalCheck(
                name="missing_patch_warning",
                description="Patch is useful but not required for this workflow.",
                severity="warning",
                required_artifact_types=["patch"],
            )
        ]
    )

    final_run = runner_factory(DemoExecutor()).run(Run(id="run-1", task_id=task.id), warning_pack)

    assert final_run.status == RunStatus.COMPLETED
    warning = storage.list_eval_results_for_run(final_run.id)[-1]
    assert warning.check_name == "missing_patch_warning"
    assert warning.status == EvalStatus.WARN

    task_2 = storage.create_task(
        Task(
            id="task-2",
            title="Demo task 2",
            goal="Run a demo workflow.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    blocker_pack = _demo_pack(
        eval_checks=[
            EvalCheck(
                name="missing_patch_blocker",
                description="Patch must exist.",
                severity="blocker",
                required_artifact_types=["patch"],
            )
        ]
    )

    with pytest.raises(WorkflowRunnerError, match="Blocking evaluation failed"):
        runner_factory(DemoExecutor()).run(Run(id="run-2", task_id=task_2.id), blocker_pack)

    assert storage.get_run("run-2").status == RunStatus.FAILED  # type: ignore[union-attr]
    assert storage.get_run("run-2").current_step == "write"  # type: ignore[union-attr]
    assert storage.get_run("run-2").final_artifact_id is None  # type: ignore[union-attr]
    assert [agent_run.status for agent_run in storage.list_agent_runs_for_run("run-2")] == [
        AgentRunStatus.COMPLETED,
        AgentRunStatus.COMPLETED,
    ]
    failure = storage.list_eval_results_for_run("run-2")[-1]
    assert failure.check_name == "missing_patch_blocker"
    assert failure.status == EvalStatus.FAIL


def test_runner_rejects_eval_result_for_different_run(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Run a demo workflow.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    run = Run(id="run-1", task_id=task.id)

    with pytest.raises(WorkflowRunnerError, match="different run"):
        runner_factory(CrossRunEvalExecutor()).run(run, _demo_pack())

    assert storage.get_run(run.id).status == RunStatus.FAILED  # type: ignore[union-attr]


def test_runner_fails_when_executor_eval_references_artifact_from_other_run(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    other_task = storage.create_task(
        Task(id="other-task", title="Other task", goal="Prepare other artifact.", workflow_pack="demo")
    )
    other_run = storage.create_run(Run(id="other-run", task_id=other_task.id))
    other_agent = storage.create_agent_definition(
        AgentDefinition(id="other-agent", pack_name="other-pack", role="Other", system_prompt="Other agent.")
    )
    other_agent_run = storage.create_agent_run(
        AgentRun(id="other-agent-run", run_id=other_run.id, agent_id=other_agent.id, step_name="other")
    )
    other_artifact = storage.create_artifact(
        Artifact(
            id="other-artifact",
            run_id=other_run.id,
            agent_run_id=other_agent_run.id,
            type=ArtifactType.RESEARCH_NOTE,
            path="other-run/other.md",
        )
    )
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Run a demo workflow.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    run = Run(id="run-1", task_id=task.id)

    with pytest.raises(WorkflowRunnerError, match="belongs to run"):
        runner_factory(CrossRunEvalArtifactExecutor(other_artifact.id)).run(run, _demo_pack())

    assert storage.get_run(run.id).status == RunStatus.FAILED  # type: ignore[union-attr]
    errors = [event for event in storage.list_trace_events_for_run(run.id) if event.event_type == TraceEventType.ERROR]
    assert len(errors) == 1
    assert "belongs to run" in errors[0].payload["message"]


def test_executor_eval_failure_does_not_create_handoff(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Run a demo workflow.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    run = Run(id="run-1", task_id=task.id)

    with pytest.raises(WorkflowRunnerError, match="Executor evaluation failed"):
        runner_factory(FailingFirstStepEvalExecutor()).run(run, _demo_pack())

    assert storage.get_run(run.id).status == RunStatus.FAILED  # type: ignore[union-attr]
    agent_runs = storage.list_agent_runs_for_run(run.id)
    assert [(agent_run.step_name, agent_run.status) for agent_run in agent_runs] == [
        ("plan", AgentRunStatus.FAILED)
    ]
    assert storage.list_handoffs_for_run(run.id) == []
    failure = next(
        result for result in storage.list_eval_results_for_run(run.id) if result.check_name == "first_step_blocker"
    )
    assert failure.check_name == "first_step_blocker"
    assert failure.status == EvalStatus.FAIL


def test_handoff_must_persist_before_agent_run_becomes_checkpoint(
    storage: SQLiteStorage,
    runner_factory,
    monkeypatch,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Checkpoint ordering",
            goal="Do not checkpoint a step before its handoff is durable.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    run = Run(id="run-1", task_id=task.id)
    runner = runner_factory(DemoExecutor())
    original_record = runner.trace_logger.record

    def fail_handoff_trace(*, run_id, event_type, payload, agent_run_id=None, duration_ms=None):
        if event_type == TraceEventType.HANDOFF:
            raise StorageError("handoff durability boundary failure")
        return original_record(
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            agent_run_id=agent_run_id,
            duration_ms=duration_ms,
        )

    monkeypatch.setattr(runner.trace_logger, "record", fail_handoff_trace)

    with pytest.raises(WorkflowRunnerError, match="handoff durability boundary failure"):
        runner.run(run, _demo_pack())

    agent_runs = storage.list_agent_runs_for_run(run.id)
    assert [(agent_run.step_name, agent_run.status) for agent_run in agent_runs] == [
        ("plan", AgentRunStatus.FAILED)
    ]
    assert len(storage.list_handoffs_for_run(run.id)) == 1


def test_recovery_invalidates_completed_step_without_durable_gate_and_handoff(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    pack = _demo_pack()
    task = storage.create_task(
        Task(
            id="task-1",
            title="Unsafe checkpoint",
            goal="Reject a pre-fix completion marker that precedes its durable outputs.",
            workflow_pack=pack.name,
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    run = storage.create_run(
        Run(
            id="run-1",
            task_id=task.id,
            status=RunStatus.RUNNING,
            current_step="plan",
            started_at=datetime.now(UTC),
        )
    )
    for agent in pack.agents:
        storage.upsert_agent_definition(agent)
    planner = next(agent for agent in pack.agents if agent.role == "Planner")
    unsafe_checkpoint = storage.create_agent_run(
        AgentRun(
            id="unsafe-plan",
            run_id=run.id,
            agent_id=planner.id,
            step_name="plan",
            status=AgentRunStatus.COMPLETED,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
            output_summary="Completion marker persisted too early.",
        )
    )

    requeued = runner_factory(DemoExecutor()).requeue_interrupted_run(run.id, pack)

    recovered_checkpoint = storage.get_agent_run(unsafe_checkpoint.id)
    assert requeued.status == RunStatus.QUEUED
    assert recovered_checkpoint is not None
    assert recovered_checkpoint.status == AgentRunStatus.CANCELLED
    assert "Incomplete recovery checkpoint" in recovered_checkpoint.output_summary


def test_third_step_receives_second_step_handoff(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Demo task",
            goal="Run a three-step workflow.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    executor = CapturingExecutor()
    final_run = runner_factory(executor).run(Run(id="run-1", task_id=task.id), _three_step_pack())

    assert final_run.status == RunStatus.COMPLETED
    assert executor.previous_handoffs_by_step["plan"] is None
    assert executor.previous_handoffs_by_step["write"]["next_objective"] == "write"
    assert executor.previous_handoffs_by_step["review"]["next_objective"] == "review"
    assert executor.previous_handoffs_by_step["review"]["summary"] == "Completed write."


def test_dag_branch_steps_receive_only_direct_upstream_handoff(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="DAG task",
            goal="Run a diamond workflow.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    executor = CapturingExecutor()
    runner = runner_factory(executor)
    pack = _diamond_pack()

    waiting_run = runner.run(Run(id="run-1", task_id=task.id), pack)

    assert waiting_run.status == RunStatus.WAITING
    assert waiting_run.current_step == "branch_a"
    assert set(executor.previous_handoffs_by_step) == {"start"}
    runtime_jobs = storage.list_runtime_jobs_for_run(waiting_run.id)
    assert {job.step_name for job in runtime_jobs if job.status == RuntimeJobStatus.APPROVAL_REQUIRED} == {
        "branch_a",
        "branch_b",
    }

    controller = RuntimeController(storage, TraceLogger(storage))
    jobs_by_step = {job.step_name: job for job in runtime_jobs}
    controller.approve(waiting_run.id, jobs_by_step["branch_a"].id)
    waiting_run = runner.resume_run(waiting_run.id, pack)

    assert waiting_run.status == RunStatus.WAITING
    assert waiting_run.current_step == "branch_b"
    assert "branch_a" in executor.previous_handoffs_by_step

    jobs_by_step = {job.step_name: job for job in storage.list_runtime_jobs_for_run(waiting_run.id)}
    controller.approve(waiting_run.id, jobs_by_step["branch_b"].id)
    final_run = runner.resume_run(waiting_run.id, pack)

    assert final_run.status == RunStatus.COMPLETED
    assert executor.previous_handoffs_by_step["start"] is None
    assert executor.previous_handoffs_by_step["branch_a"]["next_objective"] == "branch_a"
    assert executor.previous_handoffs_by_step["branch_b"]["next_objective"] == "branch_b"
    assert executor.previous_handoffs_by_step["branch_b"]["summary"] == "Completed start."
    assert executor.previous_handoffs_by_step["merge"] is None
    assert [handoff["next_objective"] for handoff in executor.upstream_handoffs_by_step["merge"]] == [
        "merge",
        "merge",
    ]
    assert executor.coordination_context_by_step["branch_a"]["coordination_role"] == "subagent"
    assert executor.coordination_context_by_step["branch_a"]["controller_step"] == "start"
    assert executor.coordination_context_by_step["branch_a"]["return_contract"]["required_artifact_types"] == ["patch"]
    assert executor.coordination_context_by_step["branch_a"]["runtime"] == "acp"
    assert executor.coordination_context_by_step["branch_a"]["session_policy"]["persistent"] is True
    assert executor.coordination_context_by_step["branch_a"]["session_policy"]["requires_approval"] is True
    assert executor.coordination_context_by_step["branch_a"]["agent_session_id"]
    assert executor.coordination_context_by_step["branch_a"]["runtime_job_id"]
    assert executor.coordination_context_by_step["branch_a"]["runtime_job_status"] == "approved"
    assert executor.coordination_context_by_step["merge"]["coordination_role"] == "synthesizer"
    assert executor.coordination_context_by_step["merge"]["runtime"] == "session"
    assert executor.coordination_context_by_step["merge"]["runtime_job_status"] == "recorded"
    merge_context_events = [
        event
        for event in storage.list_trace_events_for_run(final_run.id)
        if event.event_type == TraceEventType.WORKFLOW_EVENT
        and event.payload.get("action") == "context_envelope_built"
        and event.payload.get("step_name") == "merge"
    ]
    assert merge_context_events
    assert set(merge_context_events[-1].payload["artifact_refs"])
    runtime_sessions = storage.list_agent_sessions_for_run(final_run.id)
    runtime_jobs = storage.list_runtime_jobs_for_run(final_run.id)
    assert len(runtime_sessions) == 4
    assert len(runtime_jobs) == 4
    assert {job.step_name for job in runtime_jobs if job.status == RuntimeJobStatus.APPROVAL_REQUIRED} == set()
    assert {job.step_name for job in runtime_jobs if job.status == RuntimeJobStatus.COMPLETED} == {
        "start",
        "branch_a",
        "branch_b",
        "merge",
    }
    runtime_events = [
        event for event in storage.list_trace_events_for_run(final_run.id) if event.event_type == TraceEventType.RUNTIME_EVENT
    ]
    assert len(runtime_events) >= 8
    assert all(event.payload["external_runtime_started"] is False for event in runtime_events)
    ready_batch_events = [
        event
        for event in storage.list_trace_events_for_run(final_run.id)
        if event.event_type == TraceEventType.WORKFLOW_EVENT
        and event.payload.get("action") == "ready_batches_planned"
    ]
    assert ready_batch_events
    assert any(batch["steps"] == ["branch_a", "branch_b"] for batch in ready_batch_events[0].payload["batches"])
    assert ready_batch_events[0].payload["true_parallel_execution"] is False


@pytest.mark.parametrize("action", ["reject", "cancel"])
def test_runtime_actions_roll_back_all_state_when_session_update_fails(
    storage: SQLiteStorage,
    runner_factory,
    monkeypatch,
    action: str,
) -> None:
    task = storage.create_task(
        Task(
            id=f"task-atomic-{action}",
            title="Atomic runtime action",
            goal="Keep run, job, session, and attempt state consistent after a storage failure.",
            workflow_pack="demo",
        )
    )
    waiting_run = runner_factory(CapturingExecutor()).run(
        Run(id=f"run-atomic-{action}", task_id=task.id),
        _diamond_pack(),
    )
    job = next(
        item
        for item in storage.list_runtime_jobs_for_run(waiting_run.id)
        if item.step_name == "branch_a"
    )
    before_run = storage.get_run(waiting_run.id)
    before_jobs = storage.list_runtime_jobs_for_run(waiting_run.id)
    before_sessions = storage.list_agent_sessions_for_run(waiting_run.id)
    before_agent_runs = storage.list_agent_runs_for_run(waiting_run.id)
    before_trace = storage.list_trace_events_for_run(waiting_run.id)
    original_update_session = storage.update_agent_session

    def fail_target_session_update(session):
        if session.id == job.agent_session_id:
            raise StorageError("injected session update failure")
        return original_update_session(session)

    monkeypatch.setattr(storage, "update_agent_session", fail_target_session_update)
    controller = RuntimeController(storage, TraceLogger(storage))

    with pytest.raises(StorageError, match="injected session update failure"):
        getattr(controller, action)(waiting_run.id, job.id)

    assert storage.get_run(waiting_run.id) == before_run
    assert storage.list_runtime_jobs_for_run(waiting_run.id) == before_jobs
    assert storage.list_agent_sessions_for_run(waiting_run.id) == before_sessions
    assert storage.list_agent_runs_for_run(waiting_run.id) == before_agent_runs
    assert storage.list_trace_events_for_run(waiting_run.id) == before_trace


def test_explicit_dag_ready_branches_execute_in_parallel(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Parallel DAG task",
            goal="Run independent ready branches in parallel.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    executor = SlowParallelExecutor(delay_seconds=0.2)

    final_run = runner_factory(executor).run(Run(id="run-1", task_id=task.id), _parallel_diamond_pack())

    assert final_run.status == RunStatus.COMPLETED
    assert executor.call_order[0] == "start"
    assert set(executor.call_order[1:3]) == {"branch_a", "branch_b"}
    assert executor.call_order[-1] == "merge"
    assert executor.started_at["branch_b"] < executor.finished_at["branch_a"]
    assert executor.started_at["branch_a"] < executor.finished_at["branch_b"]
    assert executor.started_at["merge"] > executor.finished_at["branch_a"]
    assert executor.started_at["merge"] > executor.finished_at["branch_b"]

    agent_runs = storage.list_agent_runs_for_run(final_run.id)
    assert [agent_run.step_name for agent_run in agent_runs] == ["start", "branch_a", "branch_b", "merge"]
    artifacts = storage.list_artifacts_for_run(final_run.id)
    assert [artifact.type for artifact in artifacts] == [
        ArtifactType.RESEARCH_NOTE,
        ArtifactType.PATCH,
        ArtifactType.TEST_REPORT,
        ArtifactType.FINAL_REPORT,
    ]
    parallel_events = [
        event
        for event in storage.list_trace_events_for_run(final_run.id)
        if event.event_type == TraceEventType.WORKFLOW_EVENT
        and event.payload.get("action") == "parallel_step_batch_executed"
    ]
    assert len(parallel_events) == 1
    assert parallel_events[0].payload["steps"] == ["branch_a", "branch_b"]
    assert parallel_events[0].payload["true_parallel_execution"] is True


def test_parallel_batch_failure_marks_uncommitted_siblings_terminal(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Parallel failure DAG task",
            goal="Do not leave parallel siblings running after one branch fails.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )

    with pytest.raises(WorkflowRunnerError, match="branch_a boom"):
        runner_factory(FailingParallelBranchExecutor()).run(
            Run(id="run-1", task_id=task.id),
            _parallel_diamond_pack(),
        )

    failed_run = storage.get_run("run-1")
    assert failed_run is not None
    assert failed_run.status == RunStatus.FAILED
    assert failed_run.current_step == "branch_a"
    assert [(agent_run.step_name, agent_run.status) for agent_run in storage.list_agent_runs_for_run("run-1")] == [
        ("start", AgentRunStatus.COMPLETED),
        ("branch_a", AgentRunStatus.FAILED),
        ("branch_b", AgentRunStatus.CANCELLED),
    ]
    assert [artifact.type for artifact in storage.list_artifacts_for_run("run-1")] == [
        ArtifactType.RESEARCH_NOTE,
    ]
    workflow_events = [
        event
        for event in storage.list_trace_events_for_run("run-1")
        if event.event_type == TraceEventType.WORKFLOW_EVENT
    ]
    assert any(event.payload.get("action") == "parallel_step_batch_aborted" for event in workflow_events)


def test_legacy_pack_without_dependencies_stays_serial(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Legacy serial task",
            goal="Keep implicit workflow order serial.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    executor = SlowParallelExecutor(delay_seconds=0.05)

    final_run = runner_factory(executor).run(Run(id="run-1", task_id=task.id), _legacy_serial_pack())

    assert final_run.status == RunStatus.COMPLETED
    assert executor.call_order == ["plan", "write", "review"]
    assert executor.started_at["write"] >= executor.finished_at["plan"]
    assert executor.started_at["review"] >= executor.finished_at["write"]
    workflow_events = [
        event
        for event in storage.list_trace_events_for_run(final_run.id)
        if event.event_type == TraceEventType.WORKFLOW_EVENT
    ]
    ready_event = next(event for event in workflow_events if event.payload.get("action") == "ready_batches_planned")
    assert [batch["steps"] for batch in ready_event.payload["batches"]] == [["plan"], ["write"], ["review"]]
    assert not any(event.payload.get("action") == "parallel_step_batch_executed" for event in workflow_events)


def test_runner_enforces_step_return_contract(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Contract task",
            goal="Reject subagent output that omits required risk notes.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )

    runner = runner_factory(DemoExecutor())
    pack = _diamond_pack()
    waiting_run = runner.run(Run(id="run-1", task_id=task.id), pack)
    branch_a_job = next(
        job for job in storage.list_runtime_jobs_for_run(waiting_run.id) if job.step_name == "branch_a"
    )
    RuntimeController(storage, TraceLogger(storage)).approve(waiting_run.id, branch_a_job.id)

    with pytest.raises(WorkflowRunnerError, match="return contract requires risk_notes"):
        runner.resume_run(waiting_run.id, pack)

    assert storage.get_run("run-1").status == RunStatus.FAILED  # type: ignore[union-attr]
    failed_run = storage.get_run("run-1")
    assert failed_run is not None
    assert failed_run.current_step == "branch_a"
    runtime_sessions = storage.list_agent_sessions_for_run("run-1")
    runtime_jobs = storage.list_runtime_jobs_for_run("run-1")
    assert [session.step_name for session in runtime_sessions] == ["start", "branch_a", "branch_b"]
    assert [job.step_name for job in runtime_jobs] == ["start", "branch_a", "branch_b"]
    assert {session.step_name: session.status for session in runtime_sessions} == {
        "start": AgentSessionStatus.COMPLETED,
        "branch_a": AgentSessionStatus.FAILED,
        "branch_b": AgentSessionStatus.CANCELLED,
    }
    assert {job.step_name: job.status for job in runtime_jobs} == {
        "start": RuntimeJobStatus.COMPLETED,
        "branch_a": RuntimeJobStatus.FAILED,
        "branch_b": RuntimeJobStatus.CANCELLED,
    }
    runtime_events = [
        event
        for event in storage.list_trace_events_for_run("run-1")
        if event.event_type == TraceEventType.RUNTIME_EVENT
    ]
    assert any(
        event.payload.get("action") == "open_runtime_state_terminalized"
        and event.payload.get("cancelled_jobs") == ["branch_b"]
        for event in runtime_events
    )


def test_runner_fails_when_declared_step_artifact_type_is_not_returned(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Wrong artifact task",
            goal="Reject wrong step output.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    pack = _single_step_pack(
        produces_artifact_type=ArtifactType.PATCH.value,
        final_artifact_type=ArtifactType.PATCH.value,
    )

    with pytest.raises(WorkflowRunnerError, match="declared produced artifact type patch"):
        runner_factory(WrongDeclaredArtifactExecutor()).run(Run(id="run-1", task_id=task.id), pack)

    assert storage.get_run("run-1").status == RunStatus.FAILED  # type: ignore[union-attr]
    agent_runs = storage.list_agent_runs_for_run("run-1")
    assert [(agent_run.step_name, agent_run.status) for agent_run in agent_runs] == [
        ("write", AgentRunStatus.FAILED)
    ]
    assert storage.list_artifacts_for_run("run-1") == []


def test_runner_enforces_step_eval_pass_gate(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Eval gate task",
            goal="Require an explicit step eval result.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    pack = _single_step_pack().model_copy(
        update={
            "steps": [
                _single_step_pack().steps[0].model_copy(update={"requires_eval_pass": True})
            ]
        }
    )

    with pytest.raises(WorkflowRunnerError, match="requires eval_results to pass"):
        runner_factory(DemoExecutor()).run(Run(id="run-1", task_id=task.id), pack)

    assert storage.get_run("run-1").status == RunStatus.FAILED  # type: ignore[union-attr]


def test_runner_rejects_ready_batch_ownership_conflicts(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Ownership conflict task",
            goal="Reject conflicting ready batch ownership.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    base_pack = _diamond_pack()
    steps = []
    for step in base_pack.steps:
        if step.name in {"branch_a", "branch_b"}:
            steps.append(step.model_copy(update={"ownership": {"files": ["app/api.py"]}}))
        else:
            steps.append(step)
    pack = base_pack.model_copy(update={"steps": steps})

    with pytest.raises(WorkflowRunnerError, match="ownership conflict"):
        runner_factory(CapturingExecutor()).run(Run(id="run-1", task_id=task.id), pack)

    assert storage.get_run("run-1").status == RunStatus.FAILED  # type: ignore[union-attr]


def test_runner_allows_step_eval_pass_gate_when_executor_returns_pass(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Eval gate pass task",
            goal="Require a passing step eval result.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    base_pack = _single_step_pack()
    pack = base_pack.model_copy(
        update={
            "steps": [
                base_pack.steps[0].model_copy(update={"requires_eval_pass": True})
            ]
        }
    )

    final_run = runner_factory(PassingEvalExecutor()).run(Run(id="run-1", task_id=task.id), pack)

    assert final_run.status == RunStatus.COMPLETED


def test_runner_requires_artifact_gate_only_accepts_upstream_artifacts(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Sibling artifact gate task",
            goal="Do not let sibling branch artifacts satisfy gate requirements.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    pack = WorkflowPack(
        name="demo",
        description="Sibling gate workflow pack.",
        agents=[
            AgentDefinition(id="agent-planner", pack_name="demo", role="Planner", system_prompt="Plan work."),
            AgentDefinition(id="agent-writer", pack_name="demo", role="Writer", system_prompt="Write work."),
            AgentDefinition(id="agent-tester", pack_name="demo", role="Tester", system_prompt="Test work."),
        ],
        steps=[
            WorkflowStep(name="start", agent_role="Planner", produces_artifact_type="source_summary"),
            WorkflowStep(
                name="branch_a",
                agent_role="Writer",
                depends_on=["start"],
                produces_artifact_type="research_note",
            ),
            WorkflowStep(
                name="branch_b",
                agent_role="Tester",
                depends_on=["start"],
                requires_artifact=["research_note"],
                produces_artifact_type="final_report",
            ),
        ],
        final_artifact_type="final_report",
    )

    with pytest.raises(WorkflowRunnerError, match="missing upstream gate artifacts: research_note"):
        runner_factory(BranchArtifactExecutor()).run(Run(id="run-1", task_id=task.id), pack)

    assert storage.get_run("run-1").status == RunStatus.FAILED  # type: ignore[union-attr]


def test_context_injector_truncates_upstream_handoffs_by_budget(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Wide merge task",
            goal="Keep merge context bounded.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )
    branch_steps = [
        WorkflowStep(
            name=f"branch_{index}",
            agent_role="Worker",
            depends_on=["start"],
            produces_artifact_type="research_note",
        )
        for index in range(9)
    ]
    pack = WorkflowPack(
        name="demo",
        description="Wide merge workflow pack.",
        agents=[
            AgentDefinition(id="agent-planner", pack_name="demo", role="Planner", system_prompt="Plan work."),
            AgentDefinition(id="agent-worker", pack_name="demo", role="Worker", system_prompt="Work."),
            AgentDefinition(id="agent-reviewer", pack_name="demo", role="Reviewer", system_prompt="Review work."),
        ],
        steps=[
            WorkflowStep(name="start", agent_role="Planner", produces_artifact_type="source_summary"),
            *branch_steps,
            WorkflowStep(
                name="merge",
                agent_role="Reviewer",
                depends_on=[step.name for step in branch_steps],
                produces_artifact_type="final_report",
            ),
        ],
        final_artifact_type="final_report",
    )
    executor = CapturingExecutor()

    final_run = runner_factory(executor).run(Run(id="run-1", task_id=task.id), pack)

    assert final_run.status == RunStatus.COMPLETED
    assert len(executor.upstream_handoffs_by_step["merge"]) == 8
    context_events = [
        event
        for event in storage.list_trace_events_for_run("run-1")
        if event.event_type == TraceEventType.WORKFLOW_EVENT
        and event.payload.get("action") == "context_envelope_built"
        and event.payload.get("step_name") == "merge"
    ]
    assert context_events[-1].payload["upstream_handoff_count"] == 8
    assert context_events[-1].payload["total_upstream_handoff_count"] == 9
    assert context_events[-1].payload["dropped_context"][0]["source"] == "upstream_handoffs"


def test_context_injector_reads_bounded_excerpts_from_completed_attempts_only(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(id="task-1", title="Context task", goal="Use bounded context.", workflow_pack="demo")
    )
    run = storage.create_run(Run(id="run-1", task_id=task.id, status=RunStatus.RUNNING))
    producer = storage.create_agent_definition(
        AgentDefinition(id="agent-producer", pack_name="demo", role="Producer", system_prompt="Produce.")
    )
    reviewer = storage.create_agent_definition(
        AgentDefinition(id="agent-reviewer", pack_name="demo", role="Reviewer", system_prompt="Review.")
    )
    first_completed = storage.create_agent_run(
        AgentRun(
            id="completed-1",
            run_id=run.id,
            agent_id=producer.id,
            step_name="first",
            status=AgentRunStatus.COMPLETED,
        )
    )
    second_completed = storage.create_agent_run(
        AgentRun(
            id="completed-2",
            run_id=run.id,
            agent_id=producer.id,
            step_name="second",
            status=AgentRunStatus.COMPLETED,
        )
    )
    failed_attempt = storage.create_agent_run(
        AgentRun(
            id="failed-attempt",
            run_id=run.id,
            agent_id=producer.id,
            step_name="second",
            status=AgentRunStatus.FAILED,
        )
    )
    current = storage.create_agent_run(
        AgentRun(
            id="current-review",
            run_id=run.id,
            agent_id=reviewer.id,
            step_name="review",
            status=AgentRunStatus.RUNNING,
        )
    )
    runner = runner_factory(CapturingExecutor())
    runner.artifact_store.write_text(
        run_id=run.id,
        agent_run_id=first_completed.id,
        artifact_type=ArtifactType.RESEARCH_NOTE,
        filename="first.md",
        content="first completed artifact",
    )
    retained = runner.artifact_store.write_text(
        run_id=run.id,
        agent_run_id=second_completed.id,
        artifact_type=ArtifactType.RESEARCH_NOTE,
        filename="second.md",
        content="SECOND-COMPLETED-CONTEXT",
    )
    runner.artifact_store.write_text(
        run_id=run.id,
        agent_run_id=failed_attempt.id,
        artifact_type=ArtifactType.RESEARCH_NOTE,
        filename="failed.md",
        content="FAILED-ATTEMPT-MUST-NOT-APPEAR",
    )
    step = WorkflowStep(
        name="review",
        agent_role="Reviewer",
        context_policy=ContextPolicy(
            artifact_excerpt_chars=7,
            max_artifacts=1,
            max_upstream_handoffs=1,
        ),
    )

    context = runner._build_context(
        task,
        run,
        step,
        current,
        second_completed,
        None,
        [],
    )

    assert context["artifact_ids"] == [retained.id]
    assert context["artifact_excerpts"] == [
        {
            "id": retained.id,
            "type": ArtifactType.RESEARCH_NOTE.value,
            "excerpt": "SECOND-",
            "truncated": True,
        }
    ]
    manifest = context["context_manifest"]
    assert manifest["artifact_ref_count"] == 1
    assert manifest["total_artifact_count"] == 2
    assert manifest["artifact_excerpt_count"] == 1
    assert manifest["excerpt_chars"] == 7
    trace_event = storage.list_trace_events_for_run(run.id)[-1]
    assert trace_event.payload["excerpt_chars"] == 7
    assert "SECOND-" not in str(trace_event.payload)


def test_final_artifact_is_selected_from_declared_producer_step(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Final producer task",
            goal="Keep final artifact scoped to declared producer.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )

    final_run = runner_factory(FinalReportReviewExecutor()).run(
        Run(id="run-1", task_id=task.id),
        _final_producer_pack(),
    )

    artifacts = storage.list_artifacts_for_run(final_run.id)
    assert [artifact.type for artifact in artifacts] == [
        ArtifactType.RESEARCH_NOTE,
        ArtifactType.FINAL_REPORT,
        ArtifactType.FINAL_REPORT,
    ]
    assert final_run.final_artifact_id == artifacts[1].id


def test_legacy_pack_without_declared_producer_selects_last_matching_final_artifact(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Legacy final artifact task",
            goal="Keep legacy packs working when they omit producer metadata.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )

    final_run = runner_factory(FinalReportReviewExecutor()).run(
        Run(id="run-1", task_id=task.id),
        _legacy_final_pack(),
    )

    artifacts = storage.list_artifacts_for_run(final_run.id)
    assert [artifact.type for artifact in artifacts] == [
        ArtifactType.RESEARCH_NOTE,
        ArtifactType.FINAL_REPORT,
        ArtifactType.FINAL_REPORT,
    ]
    assert final_run.final_artifact_id == artifacts[-1].id


def test_runner_redacts_secret_like_error_messages(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Secret error task",
            goal="Do not expose secret-looking values in error trace.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )

    with pytest.raises(WorkflowRunnerError):
        runner_factory(SecretFailingExecutor()).run(Run(id="run-1", task_id=task.id), _single_step_pack())

    errors = [
        event for event in storage.list_trace_events_for_run("run-1") if event.event_type == TraceEventType.ERROR
    ]
    assert len(errors) == 1
    message = errors[0].payload["message"]
    assert "Authorization: Bearer [REDACTED]" in message
    assert "api_key=[REDACTED]" in message
    assert "sk-[REDACTED]" in message
    assert "sk-secret" not in message
    assert "abc123" not in message


def test_runner_records_structured_model_failure_payload(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Structured provider error task",
            goal="Record safe provider failure details.",
            workflow_pack="demo",
            inputs={"brief": "Build a deterministic runner."},
        )
    )

    with pytest.raises(WorkflowRunnerError) as exc_info:
        runner_factory(StructuredModelFailingExecutor()).run(Run(id="run-1", task_id=task.id), _single_step_pack())

    errors = [
        event for event in storage.list_trace_events_for_run("run-1") if event.event_type == TraceEventType.ERROR
    ]
    assert len(errors) == 1
    payload = errors[0].payload
    assert payload["provider"] == "litellm_proxy"
    assert payload["model"] == "gpt-planner"
    assert payload["adapter"] == "openai_compatible_chat"
    assert payload["error_class"] == "RuntimeError"
    assert payload["elapsed_ms"] == 1234
    assert payload["message"] == "Model runtime call failed. See structured error metadata."
    assert payload["error_summary"] == "classification=unclassified_model_runtime_error"
    dumped = str(payload)
    assert "EXTERNAL_EVIDENCE_BODY" not in dumped
    assert "EXTERNAL_EVIDENCE_BODY" not in str(exc_info.value)


class FailingSecondStepExecutor:
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        if step.name == "write":
            raise RuntimeError("boom")
        return _step_output(step, ArtifactType.RESEARCH_NOTE)


class FailingFirstStepEvalExecutor:
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        return AgentStepOutput(
            summary=f"{agent.role} completed {step.name}.",
            artifacts=[
                AgentArtifactOutput(
                    type=ArtifactType.RESEARCH_NOTE,
                    filename="output.md",
                    content=f"# {step.name}\n",
                )
            ],
            eval_results=[
                EvalResult(run_id=run.id, check_name="first_step_blocker", status=EvalStatus.FAIL)
            ],
        )


class DemoExecutor:
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        return _step_output(step, _artifact_type_for_step(step))


class CapturingExecutor:
    def __init__(self) -> None:
        self.previous_handoffs_by_step: dict[str, dict[str, Any] | None] = {}
        self.upstream_handoffs_by_step: dict[str, list[dict[str, Any]]] = {}
        self.coordination_context_by_step: dict[str, dict[str, Any]] = {}

    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        self.previous_handoffs_by_step[step.name] = context["previous_handoff"]
        self.upstream_handoffs_by_step[step.name] = context["upstream_handoffs"]
        self.coordination_context_by_step[step.name] = {
            "coordination_role": context["coordination_role"],
            "controller_step": context["controller_step"],
            "return_contract": context["return_contract"],
            "runtime": context["runtime"],
            "session_policy": context["session_policy"],
            "agent_session_id": context["agent_session_id"],
            "runtime_job_id": context["runtime_job_id"],
            "runtime_job_status": context["runtime_job_status"],
        }
        return _step_output_with_contract(step, _declared_or_default_artifact_type(step))


class WrongDeclaredArtifactExecutor:
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        return _step_output(step, ArtifactType.FINAL_REPORT)


class FinalReportReviewExecutor:
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        if step.name == "plan":
            return _step_output(step, ArtifactType.RESEARCH_NOTE)
        return _step_output(step, ArtifactType.FINAL_REPORT)


class SecretFailingExecutor:
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        raise RuntimeError(
            "provider failed Authorization: Bearer sk-secret api_key=abc123 token=tok123 key=sk-standalone"
        )


class StructuredModelFailingExecutor:
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        raise ModelRuntimeError(
            "EXTERNAL_EVIDENCE_BODY",
            provider="litellm_proxy",
            model="gpt-planner",
            adapter="openai_compatible_chat",
            error_class="RuntimeError",
            error_summary="EXTERNAL_EVIDENCE_BODY",
            elapsed_ms=1234,
        )


class CrossRunEvalExecutor:
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        return AgentStepOutput(
            summary=f"{agent.role} completed {step.name}.",
            artifacts=[
                AgentArtifactOutput(
                    type=_artifact_type_for_step(step),
                    filename="output.md",
                    content=f"# {step.name}\n",
                )
            ],
            eval_results=[
                EvalResult(run_id="other-run", check_name="cross_run_eval", status=EvalStatus.PASS)
            ],
        )


class CrossRunEvalArtifactExecutor:
    def __init__(self, artifact_id: str) -> None:
        self.artifact_id = artifact_id

    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        return AgentStepOutput(
            summary=f"{agent.role} completed {step.name}.",
            artifacts=[
                AgentArtifactOutput(
                    type=_artifact_type_for_step(step),
                    filename="output.md",
                    content=f"# {step.name}\n",
                )
            ],
            eval_results=[
                EvalResult(
                    run_id=run.id,
                    artifact_id=self.artifact_id,
                    check_name="cross_run_artifact_eval",
                    status=EvalStatus.PASS,
                )
            ],
        )


class PassingEvalExecutor:
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        output = _step_output(step, _artifact_type_for_step(step))
        return AgentStepOutput(
            summary=output.summary,
            artifacts=output.artifacts,
            eval_results=[
                EvalResult(run_id=run.id, check_name=f"{step.name}:quality_gate", status=EvalStatus.PASS)
            ],
        )


class BranchArtifactExecutor:
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        return _step_output(step, _declared_or_default_artifact_type(step))


class SlowParallelExecutor:
    supports_parallel_execution = True

    def __init__(self, *, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.started_at: dict[str, float] = {}
        self.finished_at: dict[str, float] = {}
        self.call_order: list[str] = []
        self._lock = Lock()

    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        started = time.perf_counter()
        with self._lock:
            self.started_at[step.name] = started
        if step.name in {"branch_a", "branch_b"}:
            time.sleep(self.delay_seconds)
        output = _step_output_with_contract(step, _declared_or_default_artifact_type(step))
        finished = time.perf_counter()
        with self._lock:
            self.finished_at[step.name] = finished
            self.call_order.append(step.name)
        return output


class FailingParallelBranchExecutor(SlowParallelExecutor):
    def __init__(self) -> None:
        super().__init__(delay_seconds=0.05)

    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        if step.name == "branch_a":
            with self._lock:
                self.started_at[step.name] = time.perf_counter()
            time.sleep(self.delay_seconds)
            raise RuntimeError("branch_a boom")
        return super().execute(task=task, run=run, step=step, agent=agent, context=context)


def _demo_pack(
    *,
    first_step_inputs: list[str] | None = None,
    second_step_artifacts: list[str] | None = None,
    eval_checks: list[EvalCheck] | None = None,
) -> WorkflowPack:
    return WorkflowPack(
        name="demo",
        description="Demo workflow pack.",
        agents=[
            AgentDefinition(id="agent-planner", pack_name="demo", role="Planner", system_prompt="Plan work."),
            AgentDefinition(id="agent-writer", pack_name="demo", role="Writer", system_prompt="Write work."),
        ],
        steps=[
            WorkflowStep(
                name="plan",
                agent_role="Planner",
                required_inputs=first_step_inputs or [],
                produces_artifact_type="research_note",
            ),
            WorkflowStep(
                name="write",
                agent_role="Writer",
                required_artifacts=second_step_artifacts or ["research_note"],
                produces_artifact_type="final_report",
            ),
        ],
        eval_checks=eval_checks
        if eval_checks is not None
        else [
            EvalCheck(
                name="final_report_present",
                description="Final report must exist.",
                severity="blocker",
                required_artifact_types=["final_report"],
            )
        ],
        final_artifact_type="final_report",
    )


def _parallel_diamond_pack() -> WorkflowPack:
    return WorkflowPack(
        name="demo",
        description="Parallel diamond DAG workflow pack.",
        agents=[
            AgentDefinition(id="agent-planner", pack_name="demo", role="Planner", system_prompt="Plan work."),
            AgentDefinition(id="agent-writer", pack_name="demo", role="Writer", system_prompt="Write work."),
            AgentDefinition(id="agent-tester", pack_name="demo", role="Tester", system_prompt="Test work."),
            AgentDefinition(id="agent-reviewer", pack_name="demo", role="Reviewer", system_prompt="Review work."),
        ],
        steps=[
            WorkflowStep(
                name="start",
                agent_role="Planner",
                produces_artifact_type="research_note",
            ),
            WorkflowStep(
                name="branch_a",
                agent_role="Writer",
                depends_on=["start"],
                required_artifacts=["research_note"],
                produces_artifact_type="patch",
                return_contract=ReturnContract(
                    required_artifact_types=["patch"],
                    require_risk_notes=True,
                ),
                ownership={"artifacts": ["patch"], "workspaces": ["branch_a"]},
            ),
            WorkflowStep(
                name="branch_b",
                agent_role="Tester",
                depends_on=["start"],
                required_artifacts=["research_note"],
                produces_artifact_type="test_report",
                return_contract=ReturnContract(
                    required_artifact_types=["test_report"],
                    require_risk_notes=True,
                ),
                ownership={"artifacts": ["test_report"], "workspaces": ["branch_b"]},
            ),
            WorkflowStep(
                name="merge",
                agent_role="Reviewer",
                depends_on=["branch_a", "branch_b"],
                required_artifacts=["patch", "test_report"],
                produces_artifact_type="final_report",
            ),
        ],
        eval_checks=[
            EvalCheck(
                name="final_report_present",
                description="Final report must exist.",
                severity="blocker",
                required_artifact_types=["final_report"],
            )
        ],
        final_artifact_type="final_report",
    )


def _legacy_serial_pack() -> WorkflowPack:
    return WorkflowPack(
        name="demo",
        description="Implicit-order legacy workflow pack.",
        agents=[
            AgentDefinition(id="agent-planner", pack_name="demo", role="Planner", system_prompt="Plan work."),
            AgentDefinition(id="agent-writer", pack_name="demo", role="Writer", system_prompt="Write work."),
            AgentDefinition(id="agent-reviewer", pack_name="demo", role="Reviewer", system_prompt="Review work."),
        ],
        steps=[
            WorkflowStep(
                name="plan",
                agent_role="Planner",
                produces_artifact_type="research_note",
                ownership={"workspaces": ["plan"]},
            ),
            WorkflowStep(
                name="write",
                agent_role="Writer",
                required_artifacts=["research_note"],
                produces_artifact_type="patch",
                ownership={"workspaces": ["write"]},
            ),
            WorkflowStep(
                name="review",
                agent_role="Reviewer",
                required_artifacts=["patch"],
                produces_artifact_type="final_report",
                ownership={"workspaces": ["review"]},
            ),
        ],
        eval_checks=[
            EvalCheck(
                name="final_report_present",
                description="Final report must exist.",
                severity="blocker",
                required_artifact_types=["final_report"],
            )
        ],
        final_artifact_type="final_report",
    )


def _single_step_pack(
    produces_artifact_type: str = "final_report",
    final_artifact_type: str = "final_report",
) -> WorkflowPack:
    return WorkflowPack(
        name="demo",
        description="Single-step demo workflow pack.",
        agents=[
            AgentDefinition(
                id="agent-writer",
                pack_name="demo",
                role="Writer",
                system_prompt="Write work.",
                model_config={"provider": "mock", "model": "demo-model"},
            )
        ],
        steps=[
            WorkflowStep(
                name="write",
                agent_role="Writer",
                allowed_tools=["write_artifact"],
                produces_artifact_type=produces_artifact_type,
            ),
        ],
        eval_checks=[
                EvalCheck(
                    name=f"{final_artifact_type}_present",
                    description="Final artifact must exist.",
                    severity="blocker",
                    required_artifact_types=[final_artifact_type],
                )
            ],
        final_artifact_type=final_artifact_type,
    )


def _three_step_pack() -> WorkflowPack:
    return WorkflowPack(
        name="demo",
        description="Demo workflow pack.",
        agents=[
            AgentDefinition(id="agent-planner", pack_name="demo", role="Planner", system_prompt="Plan work."),
            AgentDefinition(id="agent-writer", pack_name="demo", role="Writer", system_prompt="Write work."),
            AgentDefinition(id="agent-reviewer", pack_name="demo", role="Reviewer", system_prompt="Review work."),
        ],
        steps=[
            WorkflowStep(name="plan", agent_role="Planner", produces_artifact_type="research_note"),
            WorkflowStep(
                name="write",
                agent_role="Writer",
                required_artifacts=["research_note"],
                produces_artifact_type="final_report",
            ),
            WorkflowStep(
                name="review",
                agent_role="Reviewer",
                required_artifacts=["final_report"],
                produces_artifact_type="research_note",
            ),
        ],
        eval_checks=[
            EvalCheck(
                name="final_report_present",
                description="Final report must exist.",
                severity="blocker",
                required_artifact_types=["final_report"],
            )
        ],
        final_artifact_type="final_report",
    )


def _diamond_pack() -> WorkflowPack:
    return WorkflowPack(
        name="demo",
        description="Diamond DAG workflow pack.",
        agents=[
            AgentDefinition(id="agent-planner", pack_name="demo", role="Planner", system_prompt="Plan work."),
            AgentDefinition(id="agent-writer", pack_name="demo", role="Writer", system_prompt="Write work."),
            AgentDefinition(id="agent-tester", pack_name="demo", role="Tester", system_prompt="Test work."),
            AgentDefinition(id="agent-reviewer", pack_name="demo", role="Reviewer", system_prompt="Review work."),
        ],
        steps=[
            WorkflowStep(
                name="start",
                agent_role="Planner",
                produces_artifact_type="research_note",
                coordination_role="controller",
                runtime="session",
                session_policy=SessionPolicy(
                    persistent=True,
                    resume_strategy="latest_artifact_and_trace",
                ),
            ),
            WorkflowStep(
                name="branch_a",
                agent_role="Writer",
                depends_on=["start"],
                required_artifacts=["research_note"],
                produces_artifact_type="patch",
                coordination_role="subagent",
                controller_step="start",
                return_contract=ReturnContract(
                    required_artifact_types=["patch"],
                    require_risk_notes=True,
                ),
                runtime="acp",
                session_policy=SessionPolicy(
                    persistent=True,
                    resume_strategy="latest_artifact_and_trace",
                    requires_approval=True,
                ),
            ),
            WorkflowStep(
                name="branch_b",
                agent_role="Tester",
                depends_on=["start"],
                required_artifacts=["research_note"],
                produces_artifact_type="test_report",
                coordination_role="subagent",
                controller_step="start",
                return_contract=ReturnContract(
                    required_artifact_types=["test_report"],
                    require_risk_notes=True,
                ),
                runtime="acp",
                session_policy=SessionPolicy(
                    persistent=True,
                    resume_strategy="latest_artifact_and_trace",
                    requires_approval=True,
                ),
            ),
            WorkflowStep(
                name="merge",
                agent_role="Reviewer",
                depends_on=["branch_a", "branch_b"],
                required_artifacts=["patch", "test_report"],
                produces_artifact_type="final_report",
                coordination_role="synthesizer",
                runtime="session",
                session_policy=SessionPolicy(
                    persistent=True,
                    resume_strategy="latest_artifact_and_trace",
                ),
            ),
        ],
        eval_checks=[
            EvalCheck(
                name="final_report_present",
                description="Final report must exist.",
                severity="blocker",
                required_artifact_types=["final_report"],
            )
        ],
        final_artifact_type="final_report",
    )


def _final_producer_pack() -> WorkflowPack:
    return WorkflowPack(
        name="demo",
        description="Final producer workflow pack.",
        agents=[
            AgentDefinition(id="agent-planner", pack_name="demo", role="Planner", system_prompt="Plan work."),
            AgentDefinition(id="agent-writer", pack_name="demo", role="Writer", system_prompt="Write work."),
            AgentDefinition(id="agent-reviewer", pack_name="demo", role="Reviewer", system_prompt="Review work."),
        ],
        steps=[
            WorkflowStep(name="plan", agent_role="Planner", produces_artifact_type="research_note"),
            WorkflowStep(
                name="write",
                agent_role="Writer",
                required_artifacts=["research_note"],
                produces_artifact_type="final_report",
            ),
            WorkflowStep(name="review", agent_role="Reviewer", required_artifacts=["final_report"]),
        ],
        eval_checks=[
            EvalCheck(
                name="final_report_present",
                description="Final report must exist.",
                severity="blocker",
                required_artifact_types=["final_report"],
            )
        ],
        final_artifact_type="final_report",
    )


def _legacy_final_pack() -> WorkflowPack:
    return WorkflowPack(
        name="demo",
        description="Legacy final producer workflow pack.",
        agents=[
            AgentDefinition(id="agent-planner", pack_name="demo", role="Planner", system_prompt="Plan work."),
            AgentDefinition(id="agent-writer", pack_name="demo", role="Writer", system_prompt="Write work."),
            AgentDefinition(id="agent-reviewer", pack_name="demo", role="Reviewer", system_prompt="Review work."),
        ],
        steps=[
            WorkflowStep(name="plan", agent_role="Planner"),
            WorkflowStep(name="write", agent_role="Writer", required_artifacts=["research_note"]),
            WorkflowStep(name="review", agent_role="Reviewer", required_artifacts=["final_report"]),
        ],
        eval_checks=[
            EvalCheck(
                name="final_report_present",
                description="Final report must exist.",
                severity="blocker",
                required_artifact_types=["final_report"],
            )
        ],
        final_artifact_type="final_report",
    )


def _step_output(step: WorkflowStep, artifact_type: ArtifactType) -> AgentStepOutput:
    return AgentStepOutput(
        summary=f"Completed {step.name}.",
        artifacts=[
            AgentArtifactOutput(
                type=artifact_type,
                filename="output.md",
                content=f"# {step.name}\n",
            )
        ],
    )


def _step_output_with_contract(step: WorkflowStep, artifact_type: ArtifactType) -> AgentStepOutput:
    output = _step_output(step, artifact_type)
    if step.return_contract is not None and step.return_contract.require_risk_notes:
        return AgentStepOutput(
            summary=output.summary,
            artifacts=output.artifacts,
            risk_notes=["No additional risks in test executor."],
        )
    return output


def _artifact_type_for_step(step: WorkflowStep) -> ArtifactType:
    by_step = {
        "plan": ArtifactType.RESEARCH_NOTE,
        "start": ArtifactType.RESEARCH_NOTE,
        "branch_a": ArtifactType.PATCH,
        "branch_b": ArtifactType.TEST_REPORT,
        "merge": ArtifactType.FINAL_REPORT,
    }
    if step.name in by_step:
        return by_step[step.name]
    if step.name == "review":
        return ArtifactType.RESEARCH_NOTE
    if step.produces_artifact_type:
        return ArtifactType(step.produces_artifact_type)
    if step.name == "plan":
        return ArtifactType.RESEARCH_NOTE
    return ArtifactType.FINAL_REPORT


def _declared_or_default_artifact_type(step: WorkflowStep) -> ArtifactType:
    if step.produces_artifact_type:
        return ArtifactType(step.produces_artifact_type)
    return _artifact_type_for_step(step)
