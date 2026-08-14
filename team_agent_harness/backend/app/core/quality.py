from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Literal

from pydantic import Field

from app.core.artifacts import ArtifactStore, ArtifactStoreError
from app.core.execution_plan import ExecutionPlan
from app.core.model_runtime import REAL_MODEL_PROVIDERS
from app.core.models import (
    AgentRun,
    AgentRunStatus,
    Artifact,
    ArtifactType,
    EvalStatus,
    HarnessModel,
    RunStatus,
    TraceEvent,
    TraceEventType,
)
from app.core.storage import SQLiteStorage


MODEL_REQUEST_STARTED_TRACE_ACTION = "model_request_started"
MODEL_RESPONSE_TRACE_ACTIONS = frozenset({"model_response", "vision_preprocess_response"})


class RunQualityCriteria(HarnessModel):
    required_artifact_types: list[ArtifactType] = Field(default_factory=list)
    required_step_artifacts: dict[str, ArtifactType] = Field(default_factory=dict, max_length=32)
    required_eval_checks: list[str] = Field(default_factory=list)
    final_artifact_type: ArtifactType
    pack_eval_step_name: str | None = Field(default=None, min_length=1, max_length=100)
    min_final_artifact_chars: int = Field(default=1, ge=0, le=1_000_000)
    require_completed_run: bool = True
    verify_artifact_hashes: bool = True


class QualityCheck(HarnessModel):
    name: str = Field(min_length=1)
    status: Literal["pass", "fail"]
    message: str = ""


class RunQualityMetrics(HarnessModel):
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    usage_complete: bool = True
    unmetered_model_calls: int = Field(default=0, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)


class RunQualityReport(HarnessModel):
    run_id: str = Field(min_length=1)
    passed: bool
    checks: list[QualityCheck]
    metrics: RunQualityMetrics
    criteria: RunQualityCriteria
    execution_plan_hash: str | None = Field(default=None, min_length=64, max_length=64)


def quality_criteria_from_execution_plan(plan: ExecutionPlan) -> RunQualityCriteria:
    artifact_types = list(
        dict.fromkeys(
            artifact_type
            for step in plan.steps
            for artifact_type in step.expected_artifacts
        )
    )
    eval_checks = [
        f"{step.step_id}:acceptance:{criterion.name}"
        for step in plan.steps
        for criterion in step.acceptance_criteria
    ]
    eval_checks.extend(
        check.name
        for check in plan.eval_checks
        if check.severity == "blocker"
    )
    return RunQualityCriteria(
        required_artifact_types=artifact_types,
        required_step_artifacts={
            step.step_id: step.expected_artifacts[0]
            for step in plan.steps
        },
        required_eval_checks=list(dict.fromkeys(eval_checks)),
        final_artifact_type=plan.final_artifact_type,
        pack_eval_step_name=_last_ordered_plan_step_name(plan),
    )


def evaluate_run_quality(
    storage: SQLiteStorage,
    artifact_store: ArtifactStore,
    run_id: str,
    criteria: RunQualityCriteria,
) -> RunQualityReport:
    run = storage.get_run(run_id)
    if run is None:
        raise ValueError(f"Run not found: {run_id}")

    checks: list[QualityCheck] = []
    if criteria.require_completed_run:
        completed = run.status == RunStatus.COMPLETED
        checks.append(
            QualityCheck(
                name="run_completed",
                status="pass" if completed else "fail",
                message=f"Run status is {run.status.value}.",
            )
        )

    latest_completed_by_step = _latest_completed_attempts(storage, run_id)
    artifacts = _latest_completed_attempt_artifacts(
        storage,
        run_id,
        latest_completed_by_step=latest_completed_by_step,
    )
    artifact_types = {artifact.type for artifact in artifacts}
    for artifact_type in criteria.required_artifact_types:
        present = artifact_type in artifact_types
        checks.append(
            QualityCheck(
                name=f"artifact:{artifact_type.value}",
                status="pass" if present else "fail",
                message=(
                    f"Artifact type {artifact_type.value} is present in a completed attempt."
                    if present
                    else f"Artifact type {artifact_type.value} is missing from completed attempts."
                ),
            )
        )

    artifacts_by_attempt: dict[str, set[ArtifactType]] = {}
    for artifact in artifacts:
        artifacts_by_attempt.setdefault(artifact.agent_run_id, set()).add(artifact.type)
    for step_name, artifact_type in criteria.required_step_artifacts.items():
        attempt = latest_completed_by_step.get(step_name)
        present = (
            attempt is not None
            and artifact_type in artifacts_by_attempt.get(attempt.id, set())
        )
        checks.append(
            QualityCheck(
                name=f"artifact:{step_name}:{artifact_type.value}",
                status="pass" if present else "fail",
                message=(
                    f"Step {step_name}'s latest completed attempt owns {artifact_type.value}."
                    if present
                    else f"Step {step_name}'s latest completed attempt is missing {artifact_type.value}."
                ),
            )
        )

    final_artifact = storage.get_artifact(run.final_artifact_id) if run.final_artifact_id else None
    final_attempt_id = (
        final_artifact.agent_run_id
        if final_artifact is not None
        and any(
            attempt.id == final_artifact.agent_run_id
            for attempt in latest_completed_by_step.values()
        )
        else None
    )
    for check_name in criteria.required_eval_checks:
        step_name, separator, _criterion_name = check_name.partition(":acceptance:")
        if separator:
            attempt = latest_completed_by_step.get(step_name)
            target_attempt_id = attempt.id if attempt is not None else None
            expected_scope = "step_acceptance"
        else:
            pack_eval_attempt = (
                latest_completed_by_step.get(criteria.pack_eval_step_name)
                if criteria.pack_eval_step_name is not None
                else None
            )
            target_attempt_id = (
                pack_eval_attempt.id
                if pack_eval_attempt is not None
                else final_attempt_id
                if criteria.pack_eval_step_name is None
                else None
            )
            expected_scope = "pack"
        passed = _has_unique_attempt_eval_pass(
            storage,
            run_id,
            check_name,
            target_attempt_id=target_attempt_id,
            expected_scope=expected_scope,
        )
        checks.append(
            QualityCheck(
                name=f"eval:{check_name}",
                status="pass" if passed else "fail",
                message=(
                    f"Latest eval {check_name} passed."
                    if passed
                    else f"Latest eval {check_name} is missing or did not pass."
                ),
            )
        )

    if criteria.verify_artifact_hashes:
        invalid_artifacts = _invalid_artifact_ids(artifact_store, artifacts)
        checks.append(
            QualityCheck(
                name="artifact_hashes",
                status="fail" if invalid_artifacts else "pass",
                message=(
                    f"Artifact verification failed for {len(invalid_artifacts)} completed-attempt artifact(s)."
                    if invalid_artifacts
                    else "All completed-attempt artifact hashes are valid."
                ),
            )
        )

    belongs_to_run = final_artifact is not None and final_artifact.run_id == run.id
    checks.append(
        QualityCheck(
            name="final_artifact_run",
            status="pass" if belongs_to_run else "fail",
            message=(
                "Final artifact belongs to the evaluated run."
                if belongs_to_run
                else "Final artifact is missing or belongs to a different run."
            ),
        )
    )
    latest_artifact_ids = {artifact.id for artifact in artifacts}
    from_latest_completed_attempt = (
        belongs_to_run
        and final_artifact is not None
        and final_artifact.id in latest_artifact_ids
    )
    checks.append(
        QualityCheck(
            name="final_artifact_latest_completed_attempt",
            status="pass" if from_latest_completed_attempt else "fail",
            message=(
                "Final artifact comes from a step's latest completed attempt."
                if from_latest_completed_attempt
                else "Final artifact does not come from a step's latest completed attempt."
            ),
        )
    )
    expected_type = criteria.final_artifact_type
    has_expected_type = (
        belongs_to_run
        and final_artifact is not None
        and final_artifact.type == expected_type
    )
    checks.append(
        QualityCheck(
            name="final_artifact_type",
            status="pass" if has_expected_type else "fail",
            message=(
                f"Final artifact type is {expected_type.value}."
                if has_expected_type
                else f"Final artifact is missing or is not type {expected_type.value}."
            ),
        )
    )

    if criteria.min_final_artifact_chars:
        final_chars = _verified_text_length(artifact_store, final_artifact)
        passed = final_chars is not None and final_chars >= criteria.min_final_artifact_chars
        checks.append(
            QualityCheck(
                name="final_artifact_content",
                status="pass" if passed else "fail",
                message=(
                    f"Final artifact contains {final_chars} characters."
                    if final_chars is not None
                    else "Final artifact is missing or failed content verification."
                ),
            )
        )

    return RunQualityReport(
        run_id=run.id,
        passed=all(check.status == "pass" for check in checks),
        checks=checks,
        metrics=_run_metrics(storage, run_id, run.started_at, run.finished_at),
        criteria=criteria,
    )


def _latest_completed_attempts(storage: SQLiteStorage, run_id: str) -> dict[str, AgentRun]:
    return {
        agent_run.step_name: agent_run
        for agent_run in storage.list_agent_runs_for_run(run_id)
        if agent_run.status == AgentRunStatus.COMPLETED
    }


def _last_ordered_plan_step_name(plan: ExecutionPlan) -> str:
    completed: set[str] = set()
    remaining = list(plan.steps)
    ordered_step_names: list[str] = []
    while remaining:
        ready = [step for step in remaining if set(step.dependencies).issubset(completed)]
        if not ready:
            raise ValueError("Execution plan step dependencies cannot be resolved.")
        for step in ready:
            ordered_step_names.append(step.step_id)
            completed.add(step.step_id)
            remaining.remove(step)
    return ordered_step_names[-1]


def _latest_completed_attempt_artifacts(
    storage: SQLiteStorage,
    run_id: str,
    *,
    latest_completed_by_step: dict[str, AgentRun] | None = None,
) -> list[Artifact]:
    latest_attempts = latest_completed_by_step or _latest_completed_attempts(storage, run_id)
    accepted_agent_run_ids = {agent_run.id for agent_run in latest_attempts.values()}
    return [
        artifact
        for artifact in storage.list_artifacts_for_run(run_id)
        if artifact.agent_run_id in accepted_agent_run_ids
    ]


def _has_unique_attempt_eval_pass(
    storage: SQLiteStorage,
    run_id: str,
    check_name: str,
    *,
    target_attempt_id: str | None,
    expected_scope: str,
) -> bool:
    if target_attempt_id is None:
        return False
    results_by_id = {
        result.id: result
        for result in storage.list_eval_results_for_run(run_id)
        if result.check_name == check_name
    }
    binding_events_by_result_id: dict[str, list[TraceEvent]] = {}
    for event in storage.list_trace_events_for_run(run_id):
        if event.event_type != TraceEventType.EVAL_RESULT:
            continue
        result_id = event.payload.get("eval_result_id")
        if isinstance(result_id, str) and result_id in results_by_id:
            binding_events_by_result_id.setdefault(result_id, []).append(event)

    matching_results = []
    for result_id, result in results_by_id.items():
        binding_events = binding_events_by_result_id.get(result_id, [])
        if len(binding_events) != 1:
            continue
        event = binding_events[0]
        if (
            event.agent_run_id != target_attempt_id
            or event.payload.get("check_name") != result.check_name
            or event.payload.get("status") != result.status.value
            or event.payload.get("scope") != expected_scope
        ):
            continue
        matching_results.append(result)
    return len(matching_results) == 1 and matching_results[0].status == EvalStatus.PASS


def _invalid_artifact_ids(artifact_store: ArtifactStore, artifacts: list[Artifact]) -> list[str]:
    invalid: list[str] = []
    for artifact in artifacts:
        try:
            artifact_store.read_text_verified(artifact)
        except ArtifactStoreError:
            invalid.append(artifact.id)
    return invalid


def _verified_text_length(artifact_store: ArtifactStore, artifact: Artifact | None) -> int | None:
    if artifact is None:
        return None
    try:
        return len(artifact_store.read_text_verified(artifact))
    except ArtifactStoreError:
        return None


def _run_metrics(
    storage: SQLiteStorage,
    run_id: str,
    started_at: datetime | None,
    finished_at: datetime | None,
) -> RunQualityMetrics:
    trace_events = storage.list_trace_events_for_run(run_id)
    model_calls = 0
    tool_calls = 0
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    unmetered_model_calls = 0
    for event in trace_events:
        if event.payload.get("action") in MODEL_RESPONSE_TRACE_ACTIONS:
            model_calls += 1
            usage = event.payload.get("usage")
            if not isinstance(usage, dict):
                unmetered_model_calls += 1
                continue
            raw_input_tokens = usage.get("input_tokens")
            raw_output_tokens = usage.get("output_tokens")
            split_complete = _is_counter(raw_input_tokens) and _is_counter(raw_output_tokens)
            if not split_complete:
                unmetered_model_calls += 1
                total_tokens += _safe_counter(usage.get("total_tokens"))
                continue
            call_input_tokens = _safe_counter(raw_input_tokens)
            call_output_tokens = _safe_counter(raw_output_tokens)
            split_total = call_input_tokens + call_output_tokens
            input_tokens += call_input_tokens
            output_tokens += call_output_tokens
            raw_total_tokens = usage.get("total_tokens")
            total_tokens += (
                max(raw_total_tokens, split_total)
                if _is_counter(raw_total_tokens)
                else split_total
            )
        if event.event_type.value == "tool_call":
            tool_calls += 1
    additional_unmetered_calls = count_additional_unmetered_model_calls(trace_events)
    model_calls += additional_unmetered_calls
    unmetered_model_calls += additional_unmetered_calls
    duration_seconds = None
    if started_at is not None and finished_at is not None:
        duration_seconds = max(0.0, (finished_at - started_at).total_seconds())
    return RunQualityMetrics(
        model_calls=model_calls,
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        usage_complete=unmetered_model_calls == 0,
        unmetered_model_calls=unmetered_model_calls,
        duration_seconds=duration_seconds,
    )


def _safe_counter(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _is_counter(value: object) -> bool:
    return type(value) is int and value >= 0


def count_unmatched_run_bound_model_requests(events: list[TraceEvent]) -> int:
    """Count persisted external dispatch intents that have no terminal trace evidence."""
    pending_by_agent_run: dict[str | None, int] = {}
    for event in events:
        key = event.agent_run_id
        if event.event_type == TraceEventType.MODEL_ACTION:
            action = event.payload.get("action")
            if action == MODEL_REQUEST_STARTED_TRACE_ACTION:
                if _may_have_reached_real_provider(event.payload):
                    pending_by_agent_run[key] = pending_by_agent_run.get(key, 0) + 1
                continue
            if action in MODEL_RESPONSE_TRACE_ACTIONS:
                if key is not None:
                    _consume_pending_request(pending_by_agent_run, key)
                continue
        if key is not None and _is_confirmed_local_route_rejection(event.payload):
            _consume_pending_request(pending_by_agent_run, key)
    return sum(pending_by_agent_run.values())


def count_additional_unmetered_model_calls(events: list[TraceEvent]) -> int:
    return (
        count_unmatched_run_bound_model_requests(events)
        + _count_failed_real_route_attempts(events)
    )


def _count_failed_real_route_attempts(events: list[TraceEvent]) -> int:
    response_attempts: dict[str, Counter[tuple[object, ...]]] = {}
    workflow_attempts: dict[str, Counter[tuple[object, ...]]] = {}
    for event in events:
        if event.agent_run_id is None:
            continue
        action = event.payload.get("action")
        if event.event_type == TraceEventType.MODEL_ACTION and action in MODEL_RESPONSE_TRACE_ACTIONS:
            target = response_attempts
        elif event.event_type == TraceEventType.WORKFLOW_EVENT and action == "model_provider_fallback":
            target = workflow_attempts
        else:
            continue
        route_receipt = event.payload.get("route_receipt")
        if not isinstance(route_receipt, list):
            continue
        failed_attempts = [
            _route_attempt_fingerprint(attempt)
            for attempt in route_receipt
            if isinstance(attempt, dict)
            and attempt.get("outcome") == "failed"
            and attempt.get("provider") in REAL_MODEL_PROVIDERS
        ]
        if failed_attempts:
            target.setdefault(event.agent_run_id, Counter()).update(failed_attempts)
    total = 0
    for agent_run_id in response_attempts.keys() | workflow_attempts.keys():
        distinct_evidence = (
            response_attempts.get(agent_run_id, Counter())
            | workflow_attempts.get(agent_run_id, Counter())
        )
        total += sum(distinct_evidence.values())
    return total


def _route_attempt_fingerprint(attempt: dict[str, object]) -> tuple[object, ...]:
    raw_attempt = attempt.get("attempt")
    return (
        raw_attempt if type(raw_attempt) is int else None,
        str(attempt.get("provider", "")),
        str(attempt.get("model", "")),
    )


def _may_have_reached_real_provider(payload: dict[str, object]) -> bool:
    return (
        payload.get("provider") in REAL_MODEL_PROVIDERS
        and payload.get("run_bound") is True
        and payload.get("real_model_access_confirmed") is True
    )


def _is_confirmed_local_route_rejection(payload: dict[str, object]) -> bool:
    route_receipt = payload.get("route_receipt")
    return (
        isinstance(route_receipt, list)
        and bool(route_receipt)
        and all(
            isinstance(attempt, dict) and attempt.get("outcome") == "rejected"
            for attempt in route_receipt
        )
    )


def _consume_pending_request(
    pending_by_agent_run: dict[str | None, int],
    key: str | None,
) -> None:
    pending = pending_by_agent_run.get(key, 0)
    if pending > 1:
        pending_by_agent_run[key] = pending - 1
    elif pending == 1:
        pending_by_agent_run.pop(key)
