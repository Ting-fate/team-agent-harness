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
from app.packs.research import RESEARCH_PACK_NAME, get_research_pack


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


def test_research_pack_declares_expected_agents_steps_tools_and_evals() -> None:
    pack = get_research_pack()

    assert pack.name == RESEARCH_PACK_NAME
    assert [agent.role for agent in pack.agents] == [
        "Planner",
        "Searcher",
        "Reader",
        "Verifier",
        "Writer",
        "Reviewer",
    ]
    assert [agent.model_settings["model"] for agent in pack.agents] == [
        "mock-research-planner",
        "mock-research-planner",
        "mock-research-reader",
        "mock-research-verifier",
        "mock-research-writer",
        "mock-research-verifier",
    ]
    assert {agent.model_settings["provider"] for agent in pack.agents} == {"mock"}
    assert len({agent.model_settings["model"] for agent in pack.agents}) >= 2
    assert [step.name for step in pack.steps] == [
        "plan_research",
        "collect_sources",
        "read_sources",
        "verify_claims",
        "draft_report",
        "review_report",
    ]
    assert pack.steps[0].required_inputs == ["goal"]
    assert pack.steps[1].allowed_tools == ["web_search", "browser_search", "write_artifact"]
    assert pack.steps[2].allowed_tools == ["fetch_page", "browser_fetch", "write_artifact"]
    assert pack.steps[3].allowed_tools == ["fetch_page", "browser_fetch", "write_artifact"]
    assert pack.steps[4].required_artifacts == [ArtifactType.TEST_REPORT.value]
    assert pack.final_artifact_type == ArtifactType.FINAL_REPORT.value
    assert {check.name for check in pack.eval_checks} == {
        "research_plan_exists",
        "source_list_exists",
        "source_notes_exist",
        "claim_verification_exists",
        "final_research_report_exists",
    }
    for step in pack.steps:
        for artifact_type in step.required_artifacts:
            assert ArtifactType(artifact_type)
    for check in pack.eval_checks:
        for artifact_type in check.required_artifact_types:
            assert ArtifactType(artifact_type)


def test_research_pack_runs_to_reviewer_with_mocked_executor(storage: SQLiteStorage, runner_factory) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Research multi-agent harness patterns",
            goal="Produce a sourced research report about multi-agent harness architecture.",
            workflow_pack=RESEARCH_PACK_NAME,
            inputs={"recency": "not required"},
            constraints=["Do not call real web search."],
            acceptance_criteria=["Final report references source notes and verification status."],
        )
    )
    run = Run(id="run-1", task_id=task.id)

    final_run = runner_factory(ResearchMappedExecutor()).run(run, get_research_pack())

    assert final_run.status == RunStatus.COMPLETED
    assert final_run.current_step is None
    agent_runs = storage.list_agent_runs_for_run(final_run.id)
    assert [agent_run.step_name for agent_run in agent_runs] == [
        "plan_research",
        "collect_sources",
        "read_sources",
        "verify_claims",
        "draft_report",
        "review_report",
    ]
    assert {agent_run.status for agent_run in agent_runs} == {AgentRunStatus.COMPLETED}

    artifacts = storage.list_artifacts_for_run(final_run.id)
    assert [artifact.type for artifact in artifacts] == [
        ArtifactType.DESIGN_DOC,
        ArtifactType.SOURCE_SUMMARY,
        ArtifactType.RESEARCH_NOTE,
        ArtifactType.TEST_REPORT,
        ArtifactType.FINAL_REPORT,
        ArtifactType.RESEARCH_NOTE,
    ]
    assert final_run.final_artifact_id == artifacts[-2].id
    assert len(storage.list_handoffs_for_run(final_run.id)) == 5

    eval_results = storage.list_eval_results_for_run(final_run.id)
    assert {result.status for result in eval_results} == {EvalStatus.PASS}
    assert {"claim_verification_exists", "final_research_report_exists"}.issubset(
        {result.check_name for result in eval_results}
    )

    event_types = [event.event_type for event in storage.list_trace_events_for_run(final_run.id)]
    assert TraceEventType.ERROR not in event_types
    assert event_types.count(TraceEventType.HANDOFF) == 5


def test_research_unsupported_claim_blocker_stops_before_review_completion(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Research multi-agent harness patterns",
            goal="Produce a sourced research report about multi-agent harness architecture.",
            workflow_pack=RESEARCH_PACK_NAME,
            inputs={"recency": "not required"},
        )
    )
    run = Run(id="run-1", task_id=task.id)

    with pytest.raises(WorkflowRunnerError, match="unsupported_claim"):
        runner_factory(UnsupportedClaimExecutor()).run(run, get_research_pack())

    failed_run = storage.get_run(run.id)
    assert failed_run is not None
    assert failed_run.status == RunStatus.FAILED
    assert failed_run.current_step == "verify_claims"
    assert failed_run.final_artifact_id is None

    agent_runs = storage.list_agent_runs_for_run(run.id)
    assert [agent_run.step_name for agent_run in agent_runs] == [
        "plan_research",
        "collect_sources",
        "read_sources",
        "verify_claims",
    ]
    assert [agent_run.status for agent_run in agent_runs] == [
        AgentRunStatus.COMPLETED,
        AgentRunStatus.COMPLETED,
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
    ]
    assert "review_report" not in [agent_run.step_name for agent_run in agent_runs]

    failure = next(
        result for result in storage.list_eval_results_for_run(run.id) if result.check_name == "unsupported_claim"
    )
    assert failure.status == EvalStatus.FAIL
    assert ArtifactType.FINAL_REPORT not in {artifact.type for artifact in storage.list_artifacts_for_run(run.id)}


def test_research_required_artifact_gating_stops_after_bad_searcher(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Research multi-agent harness patterns",
            goal="Produce a sourced research report about multi-agent harness architecture.",
            workflow_pack=RESEARCH_PACK_NAME,
            inputs={"recency": "not required"},
        )
    )
    run = Run(id="run-1", task_id=task.id)

    with pytest.raises(WorkflowRunnerError, match="declared produced artifact type source_summary"):
        runner_factory(BadSearcherExecutor()).run(run, get_research_pack())

    failed_run = storage.get_run(run.id)
    assert failed_run is not None
    assert failed_run.status == RunStatus.FAILED
    assert failed_run.current_step == "collect_sources"
    agent_runs = storage.list_agent_runs_for_run(run.id)
    assert [(agent_run.step_name, agent_run.status) for agent_run in agent_runs] == [
        ("plan_research", AgentRunStatus.COMPLETED),
        ("collect_sources", AgentRunStatus.FAILED),
    ]
    assert failed_run.final_artifact_id is None
    assert "review_report" not in [agent_run.step_name for agent_run in agent_runs]


def test_research_reviewer_blocker_fails_after_draft_report(
    storage: SQLiteStorage,
    runner_factory,
) -> None:
    task = storage.create_task(
        Task(
            id="task-1",
            title="Research multi-agent harness patterns",
            goal="Produce a sourced research report about multi-agent harness architecture.",
            workflow_pack=RESEARCH_PACK_NAME,
            inputs={"recency": "not required"},
        )
    )
    run = Run(id="run-1", task_id=task.id)

    with pytest.raises(WorkflowRunnerError, match="reviewer_has_blocker"):
        runner_factory(ReviewerBlockerExecutor()).run(run, get_research_pack())

    failed_run = storage.get_run(run.id)
    assert failed_run is not None
    assert failed_run.status == RunStatus.FAILED
    assert failed_run.current_step == "review_report"
    assert failed_run.final_artifact_id is None

    agent_runs = storage.list_agent_runs_for_run(run.id)
    assert [agent_run.step_name for agent_run in agent_runs] == [
        "plan_research",
        "collect_sources",
        "read_sources",
        "verify_claims",
        "draft_report",
        "review_report",
    ]
    assert [agent_run.status for agent_run in agent_runs] == [
        AgentRunStatus.COMPLETED,
        AgentRunStatus.COMPLETED,
        AgentRunStatus.COMPLETED,
        AgentRunStatus.COMPLETED,
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
    ]
    artifacts = storage.list_artifacts_for_run(run.id)
    assert artifacts[-2].type == ArtifactType.FINAL_REPORT
    assert artifacts[-1].type == ArtifactType.RESEARCH_NOTE
    failure = next(
        result for result in storage.list_eval_results_for_run(run.id) if result.check_name == "reviewer_has_blocker"
    )
    assert failure.status == EvalStatus.FAIL


class ResearchMappedExecutor:
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


class UnsupportedClaimExecutor(ResearchMappedExecutor):
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
        if step.name != "verify_claims":
            return output

        return AgentStepOutput(
            summary=output.summary,
            artifacts=output.artifacts,
            eval_results=[
                EvalResult(
                    run_id=run.id,
                    check_name="unsupported_claim",
                    status=EvalStatus.FAIL,
                    message="Verifier found a major claim without evidence.",
                )
            ],
        )


class BadSearcherExecutor(ResearchMappedExecutor):
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        if step.name != "collect_sources":
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


class ReviewerBlockerExecutor(ResearchMappedExecutor):
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
        if step.name != "review_report":
            return output

        return AgentStepOutput(
            summary=output.summary,
            artifacts=output.artifacts,
            eval_results=[
                EvalResult(
                    run_id=run.id,
                    check_name="reviewer_has_blocker",
                    status=EvalStatus.FAIL,
                    message="Reviewer found unsupported or unclear claims.",
                )
            ],
        )


def _artifact_type_for_step(step_name: str) -> ArtifactType:
    return {
        "plan_research": ArtifactType.DESIGN_DOC,
        "collect_sources": ArtifactType.SOURCE_SUMMARY,
        "read_sources": ArtifactType.RESEARCH_NOTE,
        "verify_claims": ArtifactType.TEST_REPORT,
        "draft_report": ArtifactType.FINAL_REPORT,
        "review_report": ArtifactType.RESEARCH_NOTE,
    }[step_name]
