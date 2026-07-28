from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.core.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    AgentSession,
    AgentSessionStatus,
    Artifact,
    ArtifactType,
    ArtifactValidationStatus,
    EvalResult,
    EvalStatus,
    Handoff,
    Run,
    RunStatus,
    RuntimeJob,
    RuntimeJobStatus,
    Task,
    TraceEvent,
    TraceEventType,
)


def test_task_model_accepts_required_fields() -> None:
    task = Task(
        title="Implement trace view",
        goal="Add a trace viewer for agent runs.",
        workflow_pack="code_rd",
        inputs={"repo": "demo"},
        constraints=["workspace only"],
        acceptance_criteria=["trace is readable"],
        created_by="test-user",
    )

    dumped = task.model_dump(mode="json")

    assert dumped["title"] == "Implement trace view"
    assert dumped["workflow_pack"] == "code_rd"
    assert dumped["inputs"] == {"repo": "demo"}
    assert dumped["created_at"].endswith("Z")


def test_task_model_rejects_missing_goal_or_workflow_pack() -> None:
    with pytest.raises(ValidationError):
        Task(title="Missing goal", workflow_pack="code_rd")

    with pytest.raises(ValidationError):
        Task(title="Missing pack", goal="Do the work")


def test_task_model_strips_and_rejects_blank_strings() -> None:
    task = Task(title="  Valid title  ", goal="  Do the work  ", workflow_pack="  code_rd  ")

    assert task.title == "Valid title"
    assert task.goal == "Do the work"
    assert task.workflow_pack == "code_rd"

    with pytest.raises(ValidationError):
        Task(title="   ", goal="Do the work", workflow_pack="code_rd")


def test_models_reject_extra_fields_and_blank_ids() -> None:
    with pytest.raises(ValidationError):
        Task(title="Task", goal="Goal", workflow_pack="code_rd", unexpected=True)

    with pytest.raises(ValidationError):
        Run(id="   ", task_id="task-1")


def test_run_status_enum_accepts_known_statuses() -> None:
    run = Run(task_id="task-1", status="running")

    assert run.status == RunStatus.RUNNING
    assert run.model_dump(mode="json")["status"] == "running"


def test_legacy_run_json_defaults_real_web_access_to_unconfirmed() -> None:
    run = Run.model_validate_json('{"id":"run-1","task_id":"task-1"}')

    assert run.real_web_access_confirmed is False


def test_run_status_enum_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        Run(task_id="task-1", status="paused")


def test_agent_definition_serializes_config_and_permissions() -> None:
    agent = AgentDefinition(
        pack_name="code_rd",
        role="Reviewer",
        system_prompt="Review correctness and risks.",
        model_config={"model": "gpt-test"},
        tool_permissions=["read_file", "run_tests"],
        runtime_limits={"max_steps": 5},
    )

    dumped = agent.model_dump(mode="json", by_alias=True)

    assert dumped["model_config"] == {"model": "gpt-test"}
    assert agent.model_settings == {"model": "gpt-test"}
    assert dumped["tool_permissions"] == ["read_file", "run_tests"]
    assert dumped["runtime_limits"] == {"max_steps": 5}


def test_agent_definition_accepts_only_external_model_config_alias() -> None:
    agent = AgentDefinition(
        pack_name="code_rd",
        role="Reviewer",
        system_prompt="Review correctness and risks.",
        model_config={"model": "gpt-test"},
    )

    assert agent.model_settings == {"model": "gpt-test"}

    with pytest.raises(ValidationError):
        AgentDefinition(
            pack_name="code_rd",
            role="Reviewer",
            system_prompt="Review correctness and risks.",
            model_settings={"model": "gpt-test"},
        )


def test_agent_run_requires_run_id_agent_id_step_name() -> None:
    run = AgentRun(run_id="run-1", agent_id="agent-1", step_name="review")

    assert run.status == AgentRunStatus.QUEUED
    assert run.model_dump(mode="json")["status"] == "queued"

    with pytest.raises(ValidationError):
        AgentRun(run_id="run-1", agent_id="agent-1")


def test_runtime_session_and_job_defaults_serialize() -> None:
    session = AgentSession(
        run_id="run-1",
        agent_run_id="agent-run-1",
        agent_id="agent-1",
        step_name="prepare_patch",
        runtime="acp",
        status=AgentSessionStatus.WAITING_APPROVAL,
        resume_strategy="latest_artifact_and_trace",
        requires_approval=True,
    )
    job = RuntimeJob(
        run_id="run-1",
        agent_run_id="agent-run-1",
        agent_session_id=session.id,
        step_name="prepare_patch",
        runtime="acp",
        status=RuntimeJobStatus.APPROVAL_REQUIRED,
        approval_required=True,
    )

    assert session.model_dump(mode="json")["status"] == "waiting_approval"
    assert session.model_dump(mode="json")["requires_approval"] is True
    assert job.model_dump(mode="json")["status"] == "approval_required"
    assert job.model_dump(mode="json")["approval_required"] is True

    with pytest.raises(ValidationError):
        RuntimeJob(
            run_id="run-1",
            agent_run_id="agent-run-1",
            step_name="prepare_patch",
            runtime="acp",
            unexpected=True,
        )


def test_handoff_preserves_refs_questions_constraints_and_risks() -> None:
    handoff = Handoff(
        run_id="run-1",
        from_agent_run_id="agent-run-1",
        to_agent_id="agent-2",
        summary="Design is ready for implementation.",
        artifact_refs=["artifact-1"],
        open_questions=["Should tests include edge cases?"],
        next_objective="Implement the approved design.",
        constraints_to_preserve=["Do not touch auth module."],
        risk_notes=["Migration risk is unknown."],
    )

    dumped = handoff.model_dump(mode="json")

    assert dumped["artifact_refs"] == ["artifact-1"]
    assert dumped["open_questions"] == ["Should tests include edge cases?"]
    assert dumped["constraints_to_preserve"] == ["Do not touch auth module."]
    assert dumped["risk_notes"] == ["Migration risk is unknown."]


def test_mutable_defaults_are_not_shared_between_instances() -> None:
    first = Handoff(
        run_id="run-1",
        from_agent_run_id="agent-run-1",
        to_agent_id="agent-2",
        summary="First handoff.",
        next_objective="Continue.",
    )
    second = Handoff(
        run_id="run-2",
        from_agent_run_id="agent-run-2",
        to_agent_id="agent-3",
        summary="Second handoff.",
        next_objective="Continue.",
    )

    first.artifact_refs.append("artifact-1")
    first.open_questions.append("question")
    first.constraints_to_preserve.append("constraint")
    first.risk_notes.append("risk")

    assert second.artifact_refs == []
    assert second.open_questions == []
    assert second.constraints_to_preserve == []
    assert second.risk_notes == []


def test_artifact_type_enum_accepts_expected_types() -> None:
    artifact = Artifact(
        run_id="run-1",
        agent_run_id="agent-run-1",
        type="test_report",
        path="data/artifacts/run-1/test-report.md",
        source_refs=["source-1"],
    )

    assert artifact.type == ArtifactType.TEST_REPORT
    assert artifact.validation_status == ArtifactValidationStatus.UNVALIDATED
    assert artifact.model_dump(mode="json")["type"] == "test_report"


def test_artifact_validation_status_enum_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        Artifact(
            run_id="run-1",
            agent_run_id="agent-run-1",
            type="test_report",
            path="data/artifacts/run-1/test-report.md",
            validation_status="unknown",
        )


def test_trace_event_type_enum_accepts_expected_types() -> None:
    event = TraceEvent(
        run_id="run-1",
        agent_run_id="agent-run-1",
        event_type="tool_call",
        payload={"tool": "read_file"},
        duration_ms=12,
    )

    assert event.event_type == TraceEventType.TOOL_CALL
    assert event.model_dump(mode="json")["event_type"] == "tool_call"


def test_trace_event_rejects_negative_duration() -> None:
    with pytest.raises(ValidationError):
        TraceEvent(run_id="run-1", event_type="tool_call", duration_ms=-1)


def test_eval_result_status_enum_accepts_pass_warn_fail() -> None:
    assert EvalResult(run_id="run-1", check_name="tests", status="pass").status == EvalStatus.PASS
    assert EvalResult(run_id="run-1", check_name="tests", status="warn").status == EvalStatus.WARN
    assert EvalResult(run_id="run-1", check_name="tests", status="fail").status == EvalStatus.FAIL


def test_models_dump_json_round_trip() -> None:
    created_at = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    task = Task(
        id="task-1",
        title="Research harnesses",
        goal="Compare multi-agent harness designs.",
        workflow_pack="research",
        created_at=created_at,
    )

    json_data = task.model_dump_json()
    restored = Task.model_validate_json(json_data)

    assert restored.id == "task-1"
    assert restored.created_at == created_at
