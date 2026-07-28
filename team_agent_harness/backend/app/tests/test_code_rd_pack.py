from __future__ import annotations

from typing import Any

import pytest

from app.core.artifacts import ArtifactStore
from app.core.models import (
    AgentDefinition,
    AgentRunStatus,
    ArtifactType,
    EvalResult,
    EvalStatus,
    Run,
    RunStatus,
    Task,
    TraceEventType,
)
from app.core.registry import AgentRegistry
from app.core.runner import AgentArtifactOutput, AgentStepOutput, WorkflowRunner, WorkflowRunnerError
from app.core.storage import SQLiteStorage
from app.core.trace import TraceLogger
from app.packs.base import WorkflowStep
from app.packs.code_rd import CODE_RD_PACK_NAME, get_code_rd_pack


@pytest.fixture
def storage(tmp_path):
    with SQLiteStorage(tmp_path / "harness.sqlite3") as db:
        db.init_schema()
        yield db


@pytest.fixture
def runner_factory(tmp_path, storage: SQLiteStorage):
    def make_runner(executor: Any) -> WorkflowRunner:
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


def test_code_rd_pack_declares_expected_agents_steps_tools_and_evals() -> None:
    pack = get_code_rd_pack()

    assert pack.name == CODE_RD_PACK_NAME
    assert [agent.role for agent in pack.agents] == [
        "Clarifier",
        "Architect",
        "Coder",
        "Tester",
        "Reviewer",
        "Finalizer",
    ]
    assert [agent.model_settings["model"] for agent in pack.agents] == [
        "mock-code-planner",
        "mock-code-planner",
        "mock-code-builder",
        "mock-code-builder",
        "mock-code-reviewer",
        "mock-code-reviewer",
    ]
    assert {agent.model_settings["provider"] for agent in pack.agents} == {"mock"}
    assert len({agent.model_settings["model"] for agent in pack.agents}) >= 2
    assert [step.name for step in pack.steps] == [
        "clarify_requirements",
        "design_implementation",
        "prepare_patch",
        "test_changes",
        "review_delivery",
        "finalize_delivery",
    ]
    assert pack.steps[0].required_inputs == ["goal"]
    assert pack.steps[2].required_artifacts == [ArtifactType.DESIGN_DOC.value]
    assert pack.steps[3].allowed_tools == ["read_file", "run_test_command", "write_artifact"]
    assert pack.final_artifact_type == ArtifactType.FINAL_REPORT.value
    assert {check.name for check in pack.eval_checks} == {
        "requirements_summary_exists",
        "implementation_design_exists",
        "patch_summary_exists",
        "test_report_exists",
        "review_report_exists",
        "final_delivery_summary_exists",
    }
    for step in pack.steps:
        for artifact_type in step.required_artifacts:
            assert ArtifactType(artifact_type)
    for check in pack.eval_checks:
        for artifact_type in check.required_artifact_types:
            assert ArtifactType(artifact_type)


def test_code_rd_pack_runs_to_finalizer_with_mocked_executor(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Add project health check",
            goal="Implement a small code change with tests and review.",
            workflow_pack=CODE_RD_PACK_NAME,
            inputs={"repository_path": "workspace/app"},
            constraints=["Do not call real shell commands."],
            acceptance_criteria=["Final summary includes test and review status."],
        )
    )
    run = Run(id="run-1", task_id=task.id)

    final_run = runner_factory(CodeRDMappedExecutor()).run(run, get_code_rd_pack())

    assert final_run.status == RunStatus.COMPLETED
    assert final_run.current_step is None
    agent_runs = storage.list_agent_runs_for_run(final_run.id)
    assert [agent_run.step_name for agent_run in agent_runs] == [
        "clarify_requirements",
        "design_implementation",
        "prepare_patch",
        "test_changes",
        "review_delivery",
        "finalize_delivery",
    ]
    assert {agent_run.status for agent_run in agent_runs} == {AgentRunStatus.COMPLETED}

    artifacts = storage.list_artifacts_for_run(final_run.id)
    assert [artifact.type for artifact in artifacts] == [
        ArtifactType.SOURCE_SUMMARY,
        ArtifactType.DESIGN_DOC,
        ArtifactType.PATCH,
        ArtifactType.TEST_REPORT,
        ArtifactType.RESEARCH_NOTE,
        ArtifactType.FINAL_REPORT,
    ]
    assert final_run.final_artifact_id == artifacts[-1].id
    assert len(storage.list_handoffs_for_run(final_run.id)) == 5

    eval_results = storage.list_eval_results_for_run(final_run.id)
    assert {result.status for result in eval_results} == {EvalStatus.PASS}
    assert {"review_report_exists", "final_delivery_summary_exists"}.issubset(
        {result.check_name for result in eval_results}
    )

    event_types = [event.event_type for event in storage.list_trace_events_for_run(final_run.id)]
    assert TraceEventType.ERROR not in event_types
    assert event_types.count(TraceEventType.HANDOFF) == 5


def test_code_rd_reviewer_blocker_stops_before_finalizer(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Add project health check",
            goal="Implement a small code change with tests and review.",
            workflow_pack=CODE_RD_PACK_NAME,
            inputs={"repository_path": "workspace/app"},
        )
    )
    run = Run(id="run-1", task_id=task.id)

    with pytest.raises(WorkflowRunnerError, match="review_has_blocker"):
        runner_factory(ReviewBlockerExecutor()).run(run, get_code_rd_pack())

    failed_run = storage.get_run(run.id)
    assert failed_run is not None
    assert failed_run.status == RunStatus.FAILED
    assert failed_run.current_step == "review_delivery"
    assert failed_run.final_artifact_id is None

    agent_runs = storage.list_agent_runs_for_run(run.id)
    assert [agent_run.step_name for agent_run in agent_runs] == [
        "clarify_requirements",
        "design_implementation",
        "prepare_patch",
        "test_changes",
        "review_delivery",
    ]
    assert [agent_run.status for agent_run in agent_runs] == [
        AgentRunStatus.COMPLETED,
        AgentRunStatus.COMPLETED,
        AgentRunStatus.COMPLETED,
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
    ]
    assert "finalize_delivery" not in [agent_run.step_name for agent_run in agent_runs]
    assert len(storage.list_handoffs_for_run(run.id)) == 4

    eval_results = storage.list_eval_results_for_run(run.id)
    review_failure = next(result for result in eval_results if result.check_name == "review_has_blocker")
    assert review_failure.status == EvalStatus.FAIL
    assert ArtifactType.FINAL_REPORT not in {artifact.type for artifact in storage.list_artifacts_for_run(run.id)}

    errors = [event for event in storage.list_trace_events_for_run(run.id) if event.event_type == TraceEventType.ERROR]
    assert len(errors) == 1
    assert errors[0].payload["step_name"] == "review_delivery"


def test_code_rd_required_artifact_gating_stops_after_bad_clarifier(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Add project health check",
            goal="Implement a small code change with tests and review.",
            workflow_pack=CODE_RD_PACK_NAME,
            inputs={"repository_path": "workspace/app"},
        )
    )
    run = Run(id="run-1", task_id=task.id)

    with pytest.raises(WorkflowRunnerError, match="declared produced artifact type source_summary"):
        runner_factory(BadClarifierExecutor()).run(run, get_code_rd_pack())

    failed_run = storage.get_run(run.id)
    assert failed_run is not None
    assert failed_run.status == RunStatus.FAILED
    assert failed_run.current_step == "clarify_requirements"
    agent_runs = storage.list_agent_runs_for_run(run.id)
    assert [(agent_run.step_name, agent_run.status) for agent_run in agent_runs] == [
        ("clarify_requirements", AgentRunStatus.FAILED),
    ]
    assert len(storage.list_handoffs_for_run(run.id)) == 0
    assert failed_run.final_artifact_id is None
    assert "finalize_delivery" not in [agent_run.step_name for agent_run in agent_runs]


class CodeRDMappedExecutor:
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        artifact_type = _artifact_type_for_step(step.name)
        return AgentStepOutput(
            summary=f"{agent.role} completed {step.name}.",
            artifacts=[
                AgentArtifactOutput(
                    type=artifact_type,
                    filename=f"{step.name}.md",
                    content=f"# {step.name}\n\nTask: {task.title}\nAgent: {agent.role}\n",
                )
            ],
        )


class ReviewBlockerExecutor(CodeRDMappedExecutor):
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        output = super().execute(task=task, run=run, step=step, agent=agent, context=context)
        if step.name != "review_delivery":
            return output

        return AgentStepOutput(
            summary=output.summary,
            artifacts=output.artifacts,
            eval_results=[
                EvalResult(
                    run_id=run.id,
                    check_name="review_has_blocker",
                    status=EvalStatus.FAIL,
                    message="Reviewer found an unresolved blocker.",
                )
            ],
        )


class BadClarifierExecutor(CodeRDMappedExecutor):
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        if step.name != "clarify_requirements":
            return super().execute(task=task, run=run, step=step, agent=agent, context=context)

        return AgentStepOutput(
            summary=f"{agent.role} completed {step.name} with the wrong artifact type.",
            artifacts=[
                AgentArtifactOutput(
                    type=ArtifactType.FINAL_REPORT,
                    filename=f"{step.name}.md",
                    content="# wrong artifact\n",
                )
            ],
        )


def _artifact_type_for_step(step_name: str) -> ArtifactType:
    return {
        "clarify_requirements": ArtifactType.SOURCE_SUMMARY,
        "design_implementation": ArtifactType.DESIGN_DOC,
        "prepare_patch": ArtifactType.PATCH,
        "test_changes": ArtifactType.TEST_REPORT,
        "review_delivery": ArtifactType.RESEARCH_NOTE,
        "finalize_delivery": ArtifactType.FINAL_REPORT,
    }[step_name]
