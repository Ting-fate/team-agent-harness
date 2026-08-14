from datetime import timedelta

import pytest
from pydantic import ValidationError

from app.core.artifacts import ArtifactStore
from app.core.benchmark import BenchmarkCase, BenchmarkSuite, BenchmarkTrial, ModelPrice, evaluate_benchmark
from app.core.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    ArtifactType,
    EvalResult,
    EvalStatus,
    Run,
    RunStatus,
    Task,
    TraceEventType,
    utc_now,
)
from app.core.execution_plan import ExecutionPlan, ExecutionPlanStep
from app.core.quality import (
    RunQualityCriteria,
    evaluate_run_quality,
    quality_criteria_from_execution_plan,
)
from app.packs.base import EvalCheck, StepAcceptanceCriterion
from app.core.storage import SQLiteStorage
from app.core.trace import TraceLogger


@pytest.fixture
def quality_env(tmp_path):
    with SQLiteStorage(tmp_path / "harness.sqlite3") as storage:
        storage.init_schema()
        logger = TraceLogger(storage)
        artifact_store = ArtifactStore(tmp_path / "artifacts", storage, logger)
        yield storage, logger, artifact_store


def _completed_run(storage, logger, artifact_store, *, run_id: str, model: str = "mock-model") -> Run:
    task = storage.create_task(
        Task(id=f"task-{run_id}", title="Repair", goal="Fix it", workflow_pack="code_rd_institutional")
    )
    started_at = utc_now()
    run = storage.create_run(
        Run(id=run_id, task_id=task.id, status=RunStatus.RUNNING, started_at=started_at)
    )
    agent = storage.get_agent_definition_by_pack_role("code_rd_institutional", "FinalApprover")
    if agent is None:
        agent = storage.create_agent_definition(
            AgentDefinition(
                id="shared-agent",
                pack_name="code_rd_institutional",
                role="FinalApprover",
                system_prompt="Approve.",
            )
        )
    agent_run = storage.create_agent_run(
        AgentRun(
            id=f"agent-{run_id}",
            run_id=run.id,
            agent_id=agent.id,
            step_name="final_approval",
            status=AgentRunStatus.COMPLETED,
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=2),
            output_summary="done",
        )
    )
    artifact = artifact_store.write_text(
        run_id=run.id,
        agent_run_id=agent_run.id,
        artifact_type=ArtifactType.FINAL_REPORT,
        filename="final.md",
        content="# Final\n\nVerified delivery.",
    )
    eval_result = storage.create_eval_result(
        EvalResult(
            run_id=run.id,
            check_name="acceptance",
            status=EvalStatus.PASS,
            message="passed",
        )
    )
    logger.record(
        run_id=run.id,
        agent_run_id=agent_run.id,
        event_type=TraceEventType.EVAL_RESULT,
        payload={
            "eval_result_id": eval_result.id,
            "check_name": eval_result.check_name,
            "status": eval_result.status.value,
            "scope": "pack",
        },
    )
    logger.record(
        run_id=run.id,
        agent_run_id=agent_run.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "model_response",
            "provider": "mock",
            "model": model,
            "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
        },
    )
    completed = run.model_copy(
        update={
            "status": RunStatus.COMPLETED,
            "finished_at": started_at + timedelta(seconds=4),
            "final_artifact_id": artifact.id,
        }
    )
    storage.update_run(completed)
    return completed


def test_quality_report_uses_latest_completed_attempt_and_latest_eval(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    run = _completed_run(storage, logger, artifact_store, run_id="run-quality")
    regressed = storage.create_eval_result(
        EvalResult(run_id=run.id, check_name="acceptance", status=EvalStatus.FAIL, message="regressed")
    )
    logger.record(
        run_id=run.id,
        agent_run_id="agent-run-quality",
        event_type=TraceEventType.EVAL_RESULT,
        payload={
            "eval_result_id": regressed.id,
            "check_name": regressed.check_name,
            "status": regressed.status.value,
            "scope": "pack",
        },
    )

    report = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(
            required_artifact_types=[ArtifactType.FINAL_REPORT],
            required_eval_checks=["acceptance"],
            final_artifact_type=ArtifactType.FINAL_REPORT,
        ),
    )

    assert report.passed is False
    assert {check.name: check.status for check in report.checks}["eval:acceptance"] == "fail"
    assert report.metrics.total_tokens == 120
    assert report.metrics.duration_seconds == 4


def test_quality_rejects_old_attempt_pass_when_latest_completed_attempt_has_no_eval(
    quality_env,
) -> None:
    storage, logger, artifact_store = quality_env
    run = _completed_run(storage, logger, artifact_store, run_id="run-stale-step-eval")
    agent = storage.get_agent_definition_by_pack_role("code_rd_institutional", "FinalApprover")
    assert agent is not None
    latest_attempt = storage.create_agent_run(
        AgentRun(
            id="agent-run-stale-step-eval-latest",
            run_id=run.id,
            agent_id=agent.id,
            step_name="final_approval",
            status=AgentRunStatus.COMPLETED,
            started_at=utc_now(),
            finished_at=utc_now(),
            output_summary="latest",
        )
    )
    latest_artifact = artifact_store.write_text(
        run_id=run.id,
        agent_run_id=latest_attempt.id,
        artifact_type=ArtifactType.FINAL_REPORT,
        filename="latest-final.md",
        content="# Latest final\n\nNo acceptance eval was recorded.",
    )
    stale_step_eval = storage.create_eval_result(
        EvalResult(
            run_id=run.id,
            check_name="final_approval:acceptance:nonempty-final",
            status=EvalStatus.PASS,
            message="old attempt passed",
        )
    )
    logger.record(
        run_id=run.id,
        agent_run_id="agent-run-stale-step-eval",
        event_type=TraceEventType.EVAL_RESULT,
        payload={
            "eval_result_id": stale_step_eval.id,
            "check_name": stale_step_eval.check_name,
            "status": stale_step_eval.status.value,
            "scope": "step_acceptance",
        },
    )
    storage.update_run(run.model_copy(update={"final_artifact_id": latest_artifact.id}))

    report = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(
            required_artifact_types=[ArtifactType.FINAL_REPORT],
            required_eval_checks=["final_approval:acceptance:nonempty-final"],
            final_artifact_type=ArtifactType.FINAL_REPORT,
        ),
    )

    assert report.passed is False
    assert {
        check.name: check.status for check in report.checks
    }["eval:final_approval:acceptance:nonempty-final"] == "fail"


def test_quality_binds_pack_eval_to_terminal_attempt_not_final_artifact_attempt(
    quality_env,
) -> None:
    storage, logger, artifact_store = quality_env
    task = storage.create_task(
        Task(
            id="task-terminal-pack-eval",
            title="Research",
            goal="Write and review a report.",
            workflow_pack="research",
        )
    )
    run = storage.create_run(
        Run(
            id="run-terminal-pack-eval",
            task_id=task.id,
            status=RunStatus.RUNNING,
            started_at=utc_now(),
        )
    )
    writer = storage.create_agent_definition(
        AgentDefinition(
            id="quality-writer",
            pack_name="research",
            role="Writer",
            system_prompt="Write.",
        )
    )
    reviewer = storage.create_agent_definition(
        AgentDefinition(
            id="quality-reviewer",
            pack_name="research",
            role="Reviewer",
            system_prompt="Review.",
        )
    )
    writer_attempt = storage.create_agent_run(
        AgentRun(
            id="quality-writer-attempt",
            run_id=run.id,
            agent_id=writer.id,
            step_name="draft_report",
            status=AgentRunStatus.COMPLETED,
            started_at=utc_now(),
            finished_at=utc_now(),
        )
    )
    reviewer_attempt = storage.create_agent_run(
        AgentRun(
            id="quality-reviewer-attempt",
            run_id=run.id,
            agent_id=reviewer.id,
            step_name="review_report",
            status=AgentRunStatus.COMPLETED,
            started_at=utc_now(),
            finished_at=utc_now(),
        )
    )
    final_artifact = artifact_store.write_text(
        run_id=run.id,
        agent_run_id=writer_attempt.id,
        artifact_type=ArtifactType.FINAL_REPORT,
        filename="research-final.md",
        content="# Research report\n\nReviewed delivery.",
    )
    pack_eval = storage.create_eval_result(
        EvalResult(
            run_id=run.id,
            check_name="final_report_present",
            status=EvalStatus.PASS,
            message="passed",
        )
    )
    logger.record(
        run_id=run.id,
        agent_run_id=reviewer_attempt.id,
        event_type=TraceEventType.EVAL_RESULT,
        payload={
            "eval_result_id": pack_eval.id,
            "check_name": pack_eval.check_name,
            "status": pack_eval.status.value,
            "scope": "pack",
        },
    )
    storage.update_run(
        run.model_copy(
            update={
                "status": RunStatus.COMPLETED,
                "finished_at": utc_now(),
                "final_artifact_id": final_artifact.id,
            }
        )
    )

    report = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(
            required_artifact_types=[ArtifactType.FINAL_REPORT],
            required_eval_checks=["final_report_present"],
            final_artifact_type=ArtifactType.FINAL_REPORT,
            pack_eval_step_name="review_report",
        ),
    )

    assert report.passed is True


def test_quality_rejects_stale_pack_eval_from_old_terminal_attempt(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    run = _completed_run(storage, logger, artifact_store, run_id="run-stale-pack-eval")
    agent = storage.get_agent_definition_by_pack_role("code_rd_institutional", "FinalApprover")
    assert agent is not None
    storage.create_agent_run(
        AgentRun(
            id="agent-run-stale-pack-eval-latest",
            run_id=run.id,
            agent_id=agent.id,
            step_name="final_approval",
            status=AgentRunStatus.COMPLETED,
            started_at=utc_now(),
            finished_at=utc_now(),
            output_summary="latest attempt without a pack eval",
        )
    )

    report = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(
            required_eval_checks=["acceptance"],
            final_artifact_type=ArtifactType.FINAL_REPORT,
            pack_eval_step_name="final_approval",
        ),
    )

    assert report.passed is False
    assert {check.name: check.status for check in report.checks}["eval:acceptance"] == "fail"


def test_quality_rejects_eval_result_bound_to_multiple_attempts(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    run = _completed_run(storage, logger, artifact_store, run_id="run-ambiguous-pack-eval")
    stale_eval = storage.list_eval_results_for_run(run.id)[0]
    agent = storage.get_agent_definition_by_pack_role("code_rd_institutional", "FinalApprover")
    assert agent is not None
    latest_attempt = storage.create_agent_run(
        AgentRun(
            id="agent-run-ambiguous-pack-eval-latest",
            run_id=run.id,
            agent_id=agent.id,
            step_name="final_approval",
            status=AgentRunStatus.COMPLETED,
            started_at=utc_now(),
            finished_at=utc_now(),
            output_summary="latest",
        )
    )
    latest_artifact = artifact_store.write_text(
        run_id=run.id,
        agent_run_id=latest_attempt.id,
        artifact_type=ArtifactType.FINAL_REPORT,
        filename="ambiguous-eval-final.md",
        content="# Latest final\n",
    )
    logger.record(
        run_id=run.id,
        agent_run_id=latest_attempt.id,
        event_type=TraceEventType.EVAL_RESULT,
        payload={
            "eval_result_id": stale_eval.id,
            "check_name": stale_eval.check_name,
            "status": stale_eval.status.value,
            "scope": "pack",
        },
    )
    storage.update_run(run.model_copy(update={"final_artifact_id": latest_artifact.id}))

    report = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(
            required_eval_checks=["acceptance"],
            final_artifact_type=ArtifactType.FINAL_REPORT,
            pack_eval_step_name="final_approval",
        ),
    )

    assert report.passed is False
    assert {check.name: check.status for check in report.checks}["eval:acceptance"] == "fail"


def test_quality_requires_each_step_to_own_its_expected_artifact(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    run = _completed_run(storage, logger, artifact_store, run_id="run-step-artifact-owner")
    agent = storage.get_agent_definition_by_pack_role("code_rd_institutional", "FinalApprover")
    assert agent is not None
    review_attempt = storage.create_agent_run(
        AgentRun(
            id="agent-run-step-artifact-owner-review",
            run_id=run.id,
            agent_id=agent.id,
            step_name="review",
            status=AgentRunStatus.COMPLETED,
            started_at=utc_now(),
            finished_at=utc_now(),
            output_summary="reviewed",
        )
    )
    deleted_artifact = artifact_store.write_text(
        run_id=run.id,
        agent_run_id=review_attempt.id,
        artifact_type=ArtifactType.FINAL_REPORT,
        filename="deleted-review-artifact.md",
        content="# Review\n",
    )
    review_eval = storage.create_eval_result(
        EvalResult(
            run_id=run.id,
            check_name="review:acceptance:executor-pass",
            status=EvalStatus.PASS,
            message="executor passed before artifact metadata was lost",
        )
    )
    logger.record(
        run_id=run.id,
        agent_run_id=review_attempt.id,
        event_type=TraceEventType.EVAL_RESULT,
        payload={
            "eval_result_id": review_eval.id,
            "check_name": review_eval.check_name,
            "status": review_eval.status.value,
            "scope": "step_acceptance",
        },
    )
    storage.delete_artifact(deleted_artifact.id)

    report = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(
            required_artifact_types=[ArtifactType.FINAL_REPORT],
            required_step_artifacts={
                "review": ArtifactType.FINAL_REPORT,
                "final_approval": ArtifactType.FINAL_REPORT,
            },
            required_eval_checks=["review:acceptance:executor-pass"],
            final_artifact_type=ArtifactType.FINAL_REPORT,
        ),
    )

    checks = {check.name: check.status for check in report.checks}
    assert report.passed is False
    assert checks["artifact:final_report"] == "pass"
    assert checks["artifact:review:final_report"] == "fail"
    assert checks["artifact:final_approval:final_report"] == "pass"


@pytest.mark.parametrize("case", ["missing_trace", "duplicate_results"])
def test_quality_requires_exactly_one_trace_bound_eval(quality_env, case: str) -> None:
    storage, logger, artifact_store = quality_env
    run = _completed_run(storage, logger, artifact_store, run_id=f"run-eval-{case}")
    check_name = f"gate-{case}"
    result_count = 1 if case == "missing_trace" else 2
    for index in range(result_count):
        result = storage.create_eval_result(
            EvalResult(
                run_id=run.id,
                check_name=check_name,
                status=EvalStatus.PASS,
                message="passed",
            )
        )
        if case == "duplicate_results":
            logger.record(
                run_id=run.id,
                agent_run_id=f"agent-{run.id}",
                event_type=TraceEventType.EVAL_RESULT,
                payload={
                    "eval_result_id": result.id,
                    "check_name": result.check_name,
                    "status": result.status.value,
                    "scope": "pack",
                    "index": index,
                },
            )

    report = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(
            required_eval_checks=[check_name],
            final_artifact_type=ArtifactType.FINAL_REPORT,
        ),
    )

    assert report.passed is False
    assert {check.name: check.status for check in report.checks}[f"eval:{check_name}"] == "fail"


def test_quality_metrics_count_sidecar_calls_and_expose_incomplete_usage(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    run = _completed_run(storage, logger, artifact_store, run_id="run-sidecar-metrics")
    logger.record(
        run_id=run.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "vision_preprocess_response",
            "provider": "mock",
            "model": "mock-model",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )

    measured = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT),
    )

    assert measured.metrics.model_calls == 2
    assert measured.metrics.input_tokens == 110
    assert measured.metrics.output_tokens == 25
    assert measured.metrics.total_tokens == 135
    assert measured.metrics.usage_complete is True
    assert measured.metrics.unmetered_model_calls == 0

    logger.record(
        run_id=run.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "model_response",
            "provider": "mock",
            "model": "mock-model",
            "usage": {},
        },
    )
    incomplete = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT),
    )

    assert incomplete.metrics.model_calls == 3
    assert incomplete.metrics.usage_complete is False
    assert incomplete.metrics.unmetered_model_calls == 1


def test_quality_metrics_mark_unmatched_run_bound_real_request_as_unmetered(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    run = _completed_run(storage, logger, artifact_store, run_id="run-crashed-model-request")
    agent = storage.get_agent_definition_by_pack_role("code_rd_institutional", "FinalApprover")
    assert agent is not None
    interrupted_attempt = storage.create_agent_run(
        AgentRun(
            id="agent-run-interrupted-model-request",
            run_id=run.id,
            agent_id=agent.id,
            step_name="final_approval",
            status=AgentRunStatus.FAILED,
            started_at=utc_now(),
            finished_at=utc_now(),
        )
    )
    logger.record(
        run_id=run.id,
        agent_run_id=interrupted_attempt.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "model_request_started",
            "provider": "openai",
            "model": "gpt-5",
            "run_bound": True,
            "real_model_access_confirmed": True,
        },
    )
    for payload in (
        {
            "action": "model_request_started",
            "provider": "mock",
            "model": "mock-model",
            "run_bound": True,
            "real_model_access_confirmed": True,
        },
        {
            "action": "model_request_started",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "run_bound": False,
            "real_model_access_confirmed": True,
        },
        {
            "action": "model_request_started",
            "provider": "openai",
            "model": "gpt-5",
            "run_bound": True,
            "real_model_access_confirmed": False,
        },
    ):
        logger.record(
            run_id=run.id,
            event_type=TraceEventType.MODEL_ACTION,
            payload=payload,
        )
    preflight_attempt = storage.create_agent_run(
        AgentRun(
            id="agent-run-local-preflight-rejection",
            run_id=run.id,
            agent_id=agent.id,
            step_name="local_preflight",
            status=AgentRunStatus.FAILED,
            started_at=utc_now(),
            finished_at=utc_now(),
        )
    )
    logger.record(
        run_id=run.id,
        agent_run_id=preflight_attempt.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "model_request_started",
            "provider": "openai",
            "model": "gpt-5",
            "run_bound": True,
            "real_model_access_confirmed": True,
        },
    )
    logger.record(
        run_id=run.id,
        agent_run_id=preflight_attempt.id,
        event_type=TraceEventType.ERROR,
        payload={
            "error_class": "ProviderNotConfigured",
            "route_receipt": [
                {
                    "attempt": 1,
                    "provider": "openai",
                    "model": "gpt-5",
                    "outcome": "rejected",
                    "reason": "provider_not_ready",
                }
            ],
        },
    )

    report = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT),
    )

    assert report.metrics.model_calls == 2
    assert report.metrics.input_tokens == 100
    assert report.metrics.output_tokens == 20
    assert report.metrics.total_tokens == 120
    assert report.metrics.usage_complete is False
    assert report.metrics.unmetered_model_calls == 1


def test_quality_metrics_match_run_bound_sidecar_and_agent_loop_responses(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    run = _completed_run(storage, logger, artifact_store, run_id="run-matched-request-paths")
    agent_run = storage.list_agent_runs_for_run(run.id)[0]
    interactions = (
        ("vision_preprocess_response", "openai", "gpt-5", 10, 5, None),
        ("model_response", "deepseek", "deepseek-chat", 20, 8, 1),
        ("model_response", "deepseek", "deepseek-chat", 12, 4, 2),
    )
    for response_action, provider, model, input_tokens, output_tokens, loop_step in interactions:
        logger.record(
            run_id=run.id,
            agent_run_id=agent_run.id,
            event_type=TraceEventType.MODEL_ACTION,
            payload={
                "action": "model_request_started",
                "provider": provider,
                "model": model,
                "run_bound": True,
                "real_model_access_confirmed": True,
            },
        )
        logger.record(
            run_id=run.id,
            agent_run_id=agent_run.id,
            event_type=TraceEventType.MODEL_ACTION,
            payload={
                "action": response_action,
                "provider": provider,
                "model": model,
                "agent_loop_step": loop_step,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            },
        )

    report = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT),
    )

    assert report.metrics.model_calls == 4
    assert report.metrics.input_tokens == 142
    assert report.metrics.output_tokens == 37
    assert report.metrics.total_tokens == 179
    assert report.metrics.usage_complete is True
    assert report.metrics.unmetered_model_calls == 0


def test_quality_metrics_expose_each_failed_real_attempt_without_counting_local_routes(
    quality_env,
) -> None:
    storage, logger, artifact_store = quality_env
    failed_real = _completed_run(storage, logger, artifact_store, run_id="run-failed-real-route")
    failed_real_agent_run = storage.list_agent_runs_for_run(failed_real.id)[0]
    logger.record(
        run_id=failed_real.id,
        agent_run_id=failed_real_agent_run.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "model_response",
            "provider": "mock",
            "model": "mock-model",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "route_receipt": [
                {
                    "attempt": 1,
                    "provider_attempt": 1,
                    "provider": "openai",
                    "model": "gpt-5",
                    "outcome": "failed",
                    "reason": "retryable_error",
                },
                {
                    "attempt": 1,
                    "provider_attempt": 2,
                    "provider": "openai",
                    "model": "gpt-5",
                    "outcome": "failed",
                    "reason": "retryable_error",
                },
                {
                    "attempt": 2,
                    "provider": "mock",
                    "model": "mock-model",
                    "outcome": "succeeded",
                    "reason": "selected",
                },
            ],
        },
    )

    failed_real_report = evaluate_run_quality(
        storage,
        artifact_store,
        failed_real.id,
        RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT),
    )

    assert failed_real_report.metrics.model_calls == 4
    assert failed_real_report.metrics.input_tokens == 110
    assert failed_real_report.metrics.output_tokens == 25
    assert failed_real_report.metrics.total_tokens == 135
    assert failed_real_report.metrics.usage_complete is False
    assert failed_real_report.metrics.unmetered_model_calls == 2

    local_only = _completed_run(storage, logger, artifact_store, run_id="run-local-route-outcomes")
    local_only_agent_run = storage.list_agent_runs_for_run(local_only.id)[0]
    logger.record(
        run_id=local_only.id,
        agent_run_id=local_only_agent_run.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "model_response",
            "provider": "mock",
            "model": "mock-model",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "route_receipt": [
                {
                    "attempt": 1,
                    "provider": "mock",
                    "model": "mock-model",
                    "outcome": "failed",
                    "reason": "retryable_error",
                },
                {
                    "attempt": 2,
                    "provider": "openai",
                    "model": "gpt-5",
                    "outcome": "rejected",
                    "reason": "provider_not_ready",
                },
                {
                    "attempt": 3,
                    "provider": "mock",
                    "model": "mock-model",
                    "outcome": "succeeded",
                    "reason": "selected",
                },
            ],
        },
    )

    local_only_report = evaluate_run_quality(
        storage,
        artifact_store,
        local_only.id,
        RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT),
    )

    assert local_only_report.metrics.model_calls == 2
    assert local_only_report.metrics.usage_complete is True
    assert local_only_report.metrics.unmetered_model_calls == 0


def test_quality_metrics_count_legacy_fallback_workflow_receipt_once(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    failed_attempt = {
        "attempt": 1,
        "provider": "litellm_proxy",
        "model": "gpt5.5",
        "outcome": "failed",
        "reason": "non_retryable_error",
    }
    legacy = _completed_run(storage, logger, artifact_store, run_id="run-legacy-fallback")
    legacy_agent_run = storage.list_agent_runs_for_run(legacy.id)[0]
    logger.record(
        run_id=legacy.id,
        agent_run_id=legacy_agent_run.id,
        event_type=TraceEventType.WORKFLOW_EVENT,
        payload={
            "action": "model_provider_fallback",
            "step_name": "dispatch_work",
            "agent_id": "institutional-main",
            "failed_provider": "litellm_proxy",
            "failed_model": "gpt5.5",
            "fallback_provider": "mock",
            "fallback_model": "mock-model",
            "route_receipt": [failed_attempt],
        },
    )
    logger.record(
        run_id=legacy.id,
        agent_run_id=legacy_agent_run.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "model_response",
            "provider": "mock",
            "model": "mock-model",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "route_receipt": [],
        },
    )

    legacy_report = evaluate_run_quality(
        storage,
        artifact_store,
        legacy.id,
        RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT),
    )

    assert legacy_report.metrics.model_calls == 3
    assert legacy_report.metrics.usage_complete is False
    assert legacy_report.metrics.unmetered_model_calls == 1

    duplicated = _completed_run(storage, logger, artifact_store, run_id="run-duplicated-fallback-receipt")
    duplicated_agent_run = storage.list_agent_runs_for_run(duplicated.id)[0]
    logger.record(
        run_id=duplicated.id,
        agent_run_id=duplicated_agent_run.id,
        event_type=TraceEventType.WORKFLOW_EVENT,
        payload={
            "action": "model_provider_fallback",
            "route_receipt": [failed_attempt],
        },
    )
    logger.record(
        run_id=duplicated.id,
        agent_run_id=duplicated_agent_run.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "model_response",
            "provider": "mock",
            "model": "mock-model",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "route_receipt": [failed_attempt],
        },
    )

    duplicated_report = evaluate_run_quality(
        storage,
        artifact_store,
        duplicated.id,
        RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT),
    )

    assert duplicated_report.metrics.model_calls == 3
    assert duplicated_report.metrics.unmetered_model_calls == 1


def test_quality_report_fails_closed_when_artifact_is_tampered(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    run = _completed_run(storage, logger, artifact_store, run_id="run-tampered")
    artifact = storage.get_artifact(run.final_artifact_id)
    assert artifact is not None
    (artifact_store.root_dir / artifact.path).write_text("tampered", encoding="utf-8")

    report = evaluate_run_quality(
        storage,
        artifact_store,
        run.id,
        RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT),
    )

    assert report.passed is False
    assert {check.name: check.status for check in report.checks}["artifact_hashes"] == "fail"


def test_benchmark_case_requires_explicit_final_artifact_contract() -> None:
    with pytest.raises(ValidationError, match="final_artifact_type"):
        BenchmarkCase(
            id="missing-final-artifact-contract",
            quality_criteria={"required_artifact_types": ["final_report"]},
        )


def test_quality_report_verifies_final_artifact_run_type_and_latest_attempt(
    quality_env,
    monkeypatch,
) -> None:
    storage, logger, artifact_store = quality_env
    run = _completed_run(storage, logger, artifact_store, run_id="run-final-lineage")
    original_final = storage.get_artifact(run.final_artifact_id)
    assert original_final is not None

    criteria = RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT)
    foreign_final = original_final.model_copy(update={"run_id": "another-run"})
    monkeypatch.setattr(
        storage,
        "get_artifact",
        lambda artifact_id: foreign_final if artifact_id == original_final.id else None,
    )
    foreign_report = evaluate_run_quality(storage, artifact_store, run.id, criteria)
    assert {check.name: check.status for check in foreign_report.checks}[
        "final_artifact_run"
    ] == "fail"

    monkeypatch.undo()
    agent = storage.get_agent_definition_by_pack_role("code_rd_institutional", "FinalApprover")
    assert agent is not None
    latest_attempt = storage.create_agent_run(
        AgentRun(
            id="agent-run-final-lineage-latest",
            run_id=run.id,
            agent_id=agent.id,
            step_name="final_approval",
            status=AgentRunStatus.COMPLETED,
            started_at=utc_now(),
            finished_at=utc_now(),
            output_summary="latest",
        )
    )
    latest_final = artifact_store.write_text(
        run_id=run.id,
        agent_run_id=latest_attempt.id,
        artifact_type=ArtifactType.FINAL_REPORT,
        filename="latest-final.md",
        content="# Latest final\n",
    )
    stale_report = evaluate_run_quality(storage, artifact_store, run.id, criteria)
    stale_checks = {check.name: check.status for check in stale_report.checks}
    assert stale_checks["final_artifact_latest_completed_attempt"] == "fail"

    wrong_type = artifact_store.write_text(
        run_id=run.id,
        agent_run_id=latest_attempt.id,
        artifact_type=ArtifactType.TEST_REPORT,
        filename="wrong-final.md",
        content="# Wrong final type\n",
    )
    storage.update_run(run.model_copy(update={"final_artifact_id": wrong_type.id}))
    wrong_type_report = evaluate_run_quality(storage, artifact_store, run.id, criteria)
    wrong_type_checks = {check.name: check.status for check in wrong_type_report.checks}
    assert wrong_type_checks["final_artifact_run"] == "pass"
    assert wrong_type_checks["final_artifact_latest_completed_attempt"] == "pass"
    assert wrong_type_checks["final_artifact_type"] == "fail"

    storage.update_run(run.model_copy(update={"final_artifact_id": latest_final.id}))
    valid_report = evaluate_run_quality(storage, artifact_store, run.id, criteria)
    valid_checks = {check.name: check.status for check in valid_report.checks}
    assert valid_checks["final_artifact_run"] == "pass"
    assert valid_checks["final_artifact_latest_completed_attempt"] == "pass"
    assert valid_checks["final_artifact_type"] == "pass"


def test_benchmark_compares_variants_against_single_agent_baseline(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    baseline = _completed_run(storage, logger, artifact_store, run_id="run-baseline")
    multi = _completed_run(storage, logger, artifact_store, run_id="run-multi")
    suite = BenchmarkSuite(
        name="demo",
        baseline_variant="single_agent",
        variants=["single_agent", "multi_agent"],
        cases=[
            BenchmarkCase(
                id="case-1",
                quality_criteria=RunQualityCriteria(
                    required_artifact_types=[ArtifactType.FINAL_REPORT],
                    required_eval_checks=["acceptance"],
                    final_artifact_type=ArtifactType.FINAL_REPORT,
                ),
            )
        ],
        prices=[
            ModelPrice(
                provider="mock",
                model="mock-model",
                input_usd_per_million=1,
                output_usd_per_million=2,
            )
        ],
    )
    trials = [
        BenchmarkTrial(case_id="case-1", variant="single_agent", run_id=baseline.id, manual_rework_count=2),
        BenchmarkTrial(case_id="case-1", variant="multi_agent", run_id=multi.id, manual_rework_count=0),
    ]

    report = evaluate_benchmark(storage, artifact_store, suite, trials)

    summaries = {summary.variant: summary for summary in report.variants}
    assert summaries["single_agent"].meets_value_gate is None
    assert summaries["multi_agent"].quality_gain_percentage_points == 0
    assert summaries["multi_agent"].rework_reduction_percent == 100
    assert summaries["multi_agent"].cost_ratio == 1
    assert summaries["multi_agent"].duration_ratio == 1
    assert summaries["multi_agent"].meets_value_gate is True
    assert report.trial_results[0].estimated_cost_usd == pytest.approx(0.00014)


def test_benchmark_never_treats_missing_usage_as_zero_cost(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    baseline = _completed_run(storage, logger, artifact_store, run_id="run-metered-baseline")
    variant = _completed_run(storage, logger, artifact_store, run_id="run-unmetered-variant")
    logger.record(
        run_id=variant.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "vision_preprocess_response",
            "provider": "mock",
            "model": "mock-model",
            "usage": {},
        },
    )
    suite = BenchmarkSuite(
        name="usage-integrity",
        baseline_variant="single_agent",
        variants=["single_agent", "multi_agent"],
        cases=[
            BenchmarkCase(
                id="case-1",
                quality_criteria=RunQualityCriteria(
                    final_artifact_type=ArtifactType.FINAL_REPORT
                ),
            )
        ],
        prices=[
            ModelPrice(
                provider="mock",
                model="mock-model",
                input_usd_per_million=1,
                output_usd_per_million=2,
            )
        ],
    )

    report = evaluate_benchmark(
        storage,
        artifact_store,
        suite,
        [
            BenchmarkTrial(case_id="case-1", variant="single_agent", run_id=baseline.id),
            BenchmarkTrial(case_id="case-1", variant="multi_agent", run_id=variant.id),
        ],
    )

    results = {result.trial.variant: result for result in report.trial_results}
    summaries = {summary.variant: summary for summary in report.variants}
    assert results["single_agent"].estimated_cost_usd == pytest.approx(0.00014)
    assert results["multi_agent"].estimated_cost_usd is None
    assert summaries["multi_agent"].average_total_tokens is None
    assert summaries["multi_agent"].average_cost_usd is None
    assert summaries["multi_agent"].meets_value_gate is False


def test_benchmark_fails_closed_for_unmatched_run_bound_model_request(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    baseline = _completed_run(storage, logger, artifact_store, run_id="run-recovery-baseline")
    variant = _completed_run(storage, logger, artifact_store, run_id="run-recovery-variant")
    agent = storage.get_agent_definition_by_pack_role("code_rd_institutional", "FinalApprover")
    assert agent is not None
    interrupted_attempt = storage.create_agent_run(
        AgentRun(
            id="agent-run-recovery-variant-interrupted",
            run_id=variant.id,
            agent_id=agent.id,
            step_name="final_approval",
            status=AgentRunStatus.FAILED,
            started_at=utc_now(),
            finished_at=utc_now(),
        )
    )
    logger.record(
        run_id=variant.id,
        agent_run_id=interrupted_attempt.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "model_request_started",
            "provider": "openai",
            "model": "gpt-5",
            "run_bound": True,
            "real_model_access_confirmed": True,
        },
    )
    suite = BenchmarkSuite(
        name="recovery-usage-integrity",
        baseline_variant="single_agent",
        variants=["single_agent", "multi_agent"],
        cases=[
            BenchmarkCase(
                id="case-1",
                quality_criteria=RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT),
            )
        ],
        prices=[
            ModelPrice(
                provider="mock",
                model="mock-model",
                input_usd_per_million=1,
                output_usd_per_million=2,
            ),
            ModelPrice(
                provider="openai",
                model="gpt-5",
                input_usd_per_million=2,
                output_usd_per_million=8,
            ),
        ],
    )

    report = evaluate_benchmark(
        storage,
        artifact_store,
        suite,
        [
            BenchmarkTrial(case_id="case-1", variant="single_agent", run_id=baseline.id),
            BenchmarkTrial(case_id="case-1", variant="multi_agent", run_id=variant.id),
        ],
    )

    results = {result.trial.variant: result for result in report.trial_results}
    summaries = {summary.variant: summary for summary in report.variants}
    assert results["multi_agent"].quality.metrics.unmetered_model_calls == 1
    assert results["multi_agent"].quality.metrics.usage_complete is False
    assert results["multi_agent"].estimated_cost_usd is None
    assert summaries["multi_agent"].average_total_tokens is None
    assert summaries["multi_agent"].average_cost_usd is None
    assert summaries["multi_agent"].cost_ratio is None
    assert summaries["multi_agent"].meets_value_gate is False


def test_benchmark_fails_closed_for_failed_real_fallback_attempt(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    baseline = _completed_run(storage, logger, artifact_store, run_id="run-route-baseline")
    variant = _completed_run(storage, logger, artifact_store, run_id="run-route-variant")
    variant_agent_run = storage.list_agent_runs_for_run(variant.id)[0]
    logger.record(
        run_id=variant.id,
        agent_run_id=variant_agent_run.id,
        event_type=TraceEventType.WORKFLOW_EVENT,
        payload={
            "action": "model_provider_fallback",
            "failed_provider": "deepseek",
            "failed_model": "deepseek-chat",
            "fallback_provider": "mock",
            "fallback_model": "mock-model",
            "route_receipt": [
                {
                    "attempt": 1,
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "outcome": "failed",
                    "reason": "retryable_error",
                }
            ],
        },
    )
    logger.record(
        run_id=variant.id,
        agent_run_id=variant_agent_run.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={
            "action": "model_response",
            "provider": "mock",
            "model": "mock-model",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "route_receipt": [],
        },
    )
    suite = BenchmarkSuite(
        name="fallback-usage-integrity",
        baseline_variant="single_agent",
        variants=["single_agent", "multi_agent"],
        cases=[
            BenchmarkCase(
                id="case-1",
                quality_criteria=RunQualityCriteria(final_artifact_type=ArtifactType.FINAL_REPORT),
            )
        ],
        prices=[
            ModelPrice(
                provider="mock",
                model="mock-model",
                input_usd_per_million=1,
                output_usd_per_million=2,
            ),
            ModelPrice(
                provider="deepseek",
                model="deepseek-chat",
                input_usd_per_million=1,
                output_usd_per_million=2,
            ),
        ],
    )

    report = evaluate_benchmark(
        storage,
        artifact_store,
        suite,
        [
            BenchmarkTrial(case_id="case-1", variant="single_agent", run_id=baseline.id),
            BenchmarkTrial(case_id="case-1", variant="multi_agent", run_id=variant.id),
        ],
    )

    results = {result.trial.variant: result for result in report.trial_results}
    summaries = {summary.variant: summary for summary in report.variants}
    assert results["multi_agent"].quality.metrics.model_calls == 3
    assert results["multi_agent"].quality.metrics.unmetered_model_calls == 1
    assert results["multi_agent"].estimated_cost_usd is None
    assert summaries["multi_agent"].average_total_tokens is None
    assert summaries["multi_agent"].average_cost_usd is None
    assert summaries["multi_agent"].meets_value_gate is False


def test_benchmark_requires_complete_case_variant_coverage(quality_env) -> None:
    storage, _, artifact_store = quality_env
    suite = BenchmarkSuite(
        name="demo",
        baseline_variant="single_agent",
        variants=["single_agent", "multi_agent"],
        cases=[
            BenchmarkCase(
                id="case-1",
                quality_criteria=RunQualityCriteria(
                    final_artifact_type=ArtifactType.FINAL_REPORT
                ),
            )
        ],
    )

    with pytest.raises(ValueError, match="coverage is incomplete"):
        evaluate_benchmark(storage, artifact_store, suite, [])


def test_benchmark_rejects_run_reuse_and_unpaired_replicates(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    baseline = _completed_run(storage, logger, artifact_store, run_id="run-reused")
    extra = _completed_run(storage, logger, artifact_store, run_id="run-extra")
    suite = BenchmarkSuite(
        name="demo",
        baseline_variant="single_agent",
        variants=["single_agent", "multi_agent"],
        cases=[
            BenchmarkCase(
                id="case-1",
                quality_criteria=RunQualityCriteria(
                    final_artifact_type=ArtifactType.FINAL_REPORT
                ),
            )
        ],
    )

    with pytest.raises(ValueError, match="run_id is reused"):
        evaluate_benchmark(
            storage,
            artifact_store,
            suite,
            [
                BenchmarkTrial(case_id="case-1", variant="single_agent", run_id=baseline.id),
                BenchmarkTrial(case_id="case-1", variant="multi_agent", run_id=baseline.id),
            ],
        )

    with pytest.raises(ValueError, match="coverage is incomplete"):
        evaluate_benchmark(
            storage,
            artifact_store,
            suite,
            [
                BenchmarkTrial(case_id="case-1", variant="single_agent", replicate=1, run_id=baseline.id),
                BenchmarkTrial(case_id="case-1", variant="single_agent", replicate=2, run_id=extra.id),
                BenchmarkTrial(case_id="case-1", variant="multi_agent", replicate=1, run_id="missing-run"),
            ],
        )


def test_benchmark_compares_case_replicates_before_variant_aggregation(quality_env) -> None:
    storage, logger, artifact_store = quality_env
    baseline_one = _completed_run(storage, logger, artifact_store, run_id="baseline-1")
    baseline_two = _completed_run(storage, logger, artifact_store, run_id="baseline-2")
    variant_one = _completed_run(storage, logger, artifact_store, run_id="variant-1")
    variant_two = _completed_run(storage, logger, artifact_store, run_id="variant-2")

    def set_duration(run: Run, seconds: float) -> None:
        assert run.started_at is not None
        storage.update_run(
            run.model_copy(update={"finished_at": run.started_at + timedelta(seconds=seconds)})
        )

    set_duration(baseline_one, 1)
    set_duration(baseline_two, 100)
    set_duration(variant_one, 2)
    set_duration(variant_two, 100)
    suite = BenchmarkSuite(
        name="paired-demo",
        baseline_variant="single_agent",
        variants=["single_agent", "multi_agent"],
        cases=[
            BenchmarkCase(
                id="case-1",
                quality_criteria=RunQualityCriteria(
                    final_artifact_type=ArtifactType.FINAL_REPORT
                ),
            )
        ],
    )
    trials = [
        BenchmarkTrial(
            case_id="case-1",
            variant="single_agent",
            replicate=1,
            run_id=baseline_one.id,
            manual_rework_count=1,
        ),
        BenchmarkTrial(
            case_id="case-1",
            variant="multi_agent",
            replicate=1,
            run_id=variant_one.id,
            manual_rework_count=0,
        ),
        BenchmarkTrial(
            case_id="case-1",
            variant="single_agent",
            replicate=2,
            run_id=baseline_two.id,
            manual_rework_count=100,
        ),
        BenchmarkTrial(
            case_id="case-1",
            variant="multi_agent",
            replicate=2,
            run_id=variant_two.id,
            manual_rework_count=99,
        ),
    ]

    report = evaluate_benchmark(storage, artifact_store, suite, trials)

    multi = next(summary for summary in report.variants if summary.variant == "multi_agent")
    assert multi.duration_ratio == pytest.approx(1.5)
    assert multi.rework_reduction_percent == pytest.approx(50.5)
    assert [(result.trial.replicate, result.trial.variant) for result in report.trial_results] == [
        (1, "multi_agent"),
        (1, "single_agent"),
        (2, "multi_agent"),
        (2, "single_agent"),
    ]


def test_execution_plan_quality_criteria_require_real_step_and_pack_gates() -> None:
    plan = ExecutionPlan(
        workflow_pack="code_rd",
        source="operator",
        final_artifact_type=ArtifactType.FINAL_REPORT,
        steps=[
            ExecutionPlanStep(
                step_id="solve",
                objective="Solve.",
                agent_role="Finalizer",
                expected_artifacts=[ArtifactType.FINAL_REPORT],
                acceptance_criteria=[
                    StepAcceptanceCriterion(
                        name="nonempty-final",
                        kind="artifact_nonempty",
                        artifact_type=ArtifactType.FINAL_REPORT,
                    )
                ],
            )
        ],
        eval_checks=[
            EvalCheck(name="blocking", description="Must pass.", severity="blocker"),
            EvalCheck(name="advisory", description="May warn.", severity="warning"),
        ],
    )

    criteria = quality_criteria_from_execution_plan(plan)

    assert criteria.required_artifact_types == [ArtifactType.FINAL_REPORT]
    assert criteria.required_step_artifacts == {"solve": ArtifactType.FINAL_REPORT}
    assert criteria.required_eval_checks == ["solve:acceptance:nonempty-final", "blocking"]
    assert criteria.pack_eval_step_name == "solve"
