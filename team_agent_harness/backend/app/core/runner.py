from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.core.artifacts import ArtifactStore, ArtifactStoreError
from app.core.context_injection import ContextInjector
from app.core.model_runtime import (
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ModelRuntimeError,
    model_request_from_agent,
    model_runtime_error_payload,
    reasoning_effort_trace_payload,
)
from app.core.models import (
    AgentDefinition,
    AgentRun,
    AgentRunStatus,
    AgentSession,
    AgentSessionStatus,
    Artifact,
    ArtifactType,
    EvalResult,
    EvalStatus,
    Handoff,
    Run,
    RunStatus,
    RuntimeJob,
    RuntimeJobStatus,
    Task,
    TraceEventType,
)
from app.core.registry import AgentRegistry
from app.core.sensitive_text import redact_secret_like_text
from app.core.storage import SQLiteStorage
from app.core.task_intake import analyze_task_intake
from app.core.trace import TraceLogger
from app.packs.base import EvalCheck, WorkflowPack, WorkflowStep


class WorkflowRunnerError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentArtifactOutput:
    type: ArtifactType | str
    filename: str
    content: str
    source_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AgentStepOutput:
    summary: str
    artifacts: list[AgentArtifactOutput] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    eval_results: list[EvalResult] = field(default_factory=list)
    model_request: ModelRequest | None = None
    model_response: ModelResponse | None = None


class AgentExecutor(Protocol):
    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        ...


class DeterministicMockExecutor:
    def __init__(self, model_gateway: ModelGateway | None = None) -> None:
        self.model_gateway = model_gateway or ModelGateway()

    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        model_request = model_request_from_agent(
            task_title=task.title,
            task_goal=task.goal,
            step_name=step.name,
            agent_id=agent.id,
            agent_role=agent.role,
            system_prompt=agent.system_prompt,
            model_config=agent.model_settings,
            allowed_tools=step.allowed_tools,
            context=context,
        )
        model_response = self.model_gateway.complete(model_request)
        content = f"# {step.name}\n\n{model_response.text}\nRun: {run.id}\n"
        return AgentStepOutput(
            summary=model_response.text.splitlines()[0],
            artifacts=[
                AgentArtifactOutput(
                    type=step.produces_artifact_type or ArtifactType.FINAL_REPORT,
                    filename=f"{step.name}.md",
                    content=content,
                )
            ],
            risk_notes=_default_risk_notes_for_step(step),
            model_request=model_request,
            model_response=model_response,
        )


@dataclass(frozen=True)
class _PreparedStepExecution:
    step: WorkflowStep
    agent: AgentDefinition
    agent_run: AgentRun
    agent_session: AgentSession | None
    runtime_job: RuntimeJob | None
    context: dict[str, Any] | None = None


@dataclass(frozen=True)
class _ExecutedStepOutput:
    prepared: _PreparedStepExecution
    output: AgentStepOutput | Exception


@dataclass(frozen=True)
class _CommitResult:
    agent_run: AgentRun
    agent_session: AgentSession | None
    runtime_job: RuntimeJob | None


class _StepCommitError(RuntimeError):
    def __init__(
        self,
        original: Exception,
        *,
        step: WorkflowStep,
        agent: AgentDefinition,
        agent_run: AgentRun,
        agent_session: AgentSession | None,
        runtime_job: RuntimeJob | None,
    ) -> None:
        super().__init__(str(original))
        self.original = original
        self.step = step
        self.agent = agent
        self.agent_run = agent_run
        self.agent_session = agent_session
        self.runtime_job = runtime_job


class _StepPreparationError(RuntimeError):
    def __init__(
        self,
        original: Exception,
        *,
        step: WorkflowStep,
        agent: AgentDefinition | None,
        agent_run: AgentRun | None,
        agent_session: AgentSession | None,
        runtime_job: RuntimeJob | None,
    ) -> None:
        super().__init__(str(original))
        self.original = original
        self.step = step
        self.agent = agent
        self.agent_run = agent_run
        self.agent_session = agent_session
        self.runtime_job = runtime_job


class WorkflowRunner:
    def __init__(
        self,
        *,
        storage: SQLiteStorage,
        registry: AgentRegistry,
        artifact_store: ArtifactStore,
        trace_logger: TraceLogger,
        executor: AgentExecutor | None = None,
    ) -> None:
        self.storage = storage
        self.registry = registry
        self.artifact_store = artifact_store
        self.trace_logger = trace_logger
        self.executor = executor or DeterministicMockExecutor()
        self.context_injector = ContextInjector()

    def run_task(self, task_id: str, packs: dict[str, WorkflowPack]) -> Run:
        task = self.storage.get_task(task_id)
        if task is None:
            raise WorkflowRunnerError(f"Task not found: {task_id}")

        run = Run(task_id=task.id)
        pack = packs.get(task.workflow_pack)
        if pack is None:
            active_run = self._start_run(run)
            error = WorkflowRunnerError(f"Workflow pack not found: {task.workflow_pack}")
            self._fail_run(active_run, None, None, None, error)
            raise error

        return self.run(run, pack)

    def run(self, run: Run, pack: WorkflowPack) -> Run:
        return self._run(run, pack, resume_waiting=False)

    def resume_run(self, run_id: str, pack: WorkflowPack) -> Run:
        run = self.storage.get_run(run_id)
        if run is None:
            raise WorkflowRunnerError(f"Run not found: {run_id}")
        return self._run(run, pack, resume_waiting=True)

    def requeue_interrupted_run(self, run_id: str, pack: WorkflowPack | None = None) -> Run:
        run = self.storage.get_run(run_id)
        if run is None:
            raise WorkflowRunnerError(f"Run not found: {run_id}")
        if run.status != RunStatus.RUNNING:
            raise WorkflowRunnerError(f"Run is not interrupted and cannot be requeued: {run.id}")
        if pack is not None:
            self._invalidate_incomplete_checkpoints(run.id, pack)
        self._terminalize_open_runtime_state(run.id, reason="worker_process_interrupted")
        requeued = run.model_copy(
            update={
                "status": RunStatus.QUEUED,
                "finished_at": None,
            }
        )
        self.storage.update_run(requeued)
        completed_step_count = len(
            [
                agent_run
                for agent_run in self.storage.list_agent_runs_for_run(run.id)
                if agent_run.status == AgentRunStatus.COMPLETED
            ]
        )
        self.trace_logger.record(
            run_id=run.id,
            event_type=TraceEventType.RUNTIME_EVENT,
            payload={
                "action": "interrupted_run_requeued",
                "last_completed_step_count": completed_step_count,
            },
        )
        return requeued

    def _invalidate_incomplete_checkpoints(self, run_id: str, pack: WorkflowPack) -> None:
        ordered_steps = self._ordered_steps(pack)
        steps_by_name = {step.name: step for step in ordered_steps}
        agents_by_role = {agent.role: agent for agent in pack.agents}
        handoffs = self.storage.list_handoffs_for_run(run_id)
        eval_events = [
            event
            for event in self.storage.list_trace_events_for_run(run_id)
            if event.event_type == TraceEventType.EVAL_RESULT
        ]
        pack_eval_check_names = {check.name for check in pack.eval_checks}

        agent_runs = self.storage.list_agent_runs_for_run(run_id)
        artifacts_by_agent_run: dict[str, list[Artifact]] = {}
        for artifact in self.storage.list_artifacts_for_run(run_id):
            artifacts_by_agent_run.setdefault(artifact.agent_run_id, []).append(artifact)
        latest_completed_by_step: dict[str, AgentRun] = {}
        for candidate in agent_runs:
            if candidate.status == AgentRunStatus.COMPLETED and candidate.step_name in steps_by_name:
                latest_completed_by_step[candidate.step_name] = candidate
        invalidated_steps: set[str] = set()

        for step in ordered_steps:
            agent_run = latest_completed_by_step.get(step.name)
            if agent_run is None:
                continue

            reasons: list[str] = []
            for artifact in artifacts_by_agent_run.get(agent_run.id, []):
                try:
                    self.artifact_store.read_text_excerpt(artifact, max_chars=0)
                except ArtifactStoreError:
                    reasons.append(
                        f"checkpoint artifact content is missing, invalid, or does not match durable metadata: {artifact.id}"
                    )
            dependency_lineage = agent_run.input_context.get("dependency_lineage")
            if not step.depends_on:
                if "dependency_lineage" in agent_run.input_context:
                    reasons.append("step without dependencies must not declare dependency lineage")
            elif not isinstance(dependency_lineage, dict) or set(dependency_lineage) != set(step.depends_on):
                reasons.append("checkpoint dependency lineage keys do not exactly match depends_on")

            for dependency_name in step.depends_on:
                dependency_attempt = latest_completed_by_step.get(dependency_name)
                if dependency_attempt is None or dependency_name in invalidated_steps:
                    reasons.append(f"completed dependency attempt is missing for: {dependency_name}")
                    continue
                try:
                    incoming_handoff = self._validated_dependency_handoff(
                        run_id=run_id,
                        dependency_name=dependency_name,
                        dependency_step=steps_by_name[dependency_name],
                        dependency_attempt=dependency_attempt,
                        consumer_step=step,
                        consumer_agent=agents_by_role[step.agent_role],
                        handoffs=handoffs,
                    )
                except WorkflowRunnerError as exc:
                    reasons.append(str(exc))
                    continue
                lineage_entry = (
                    dependency_lineage.get(dependency_name)
                    if isinstance(dependency_lineage, dict)
                    else None
                )
                if not isinstance(lineage_entry, dict) or (
                    lineage_entry.get("handoff_id") != incoming_handoff.id
                    or lineage_entry.get("from_agent_run_id") != dependency_attempt.id
                ):
                    reasons.append(
                        f"checkpoint dependency provenance does not match the current {dependency_name} attempt"
                    )

            step_eval_events = [event for event in eval_events if event.agent_run_id == agent_run.id]
            structural_check = f"{step.name}:artifacts_created"
            structural_events = [
                event
                for event in step_eval_events
                if event.payload.get("check_name") == structural_check
                and event.payload.get("status") == EvalStatus.PASS.value
                and (
                    event.payload.get("scope") == "step_structural"
                    or (
                        event.payload.get("scope") is None
                        and structural_check not in pack_eval_check_names
                    )
                )
            ]
            if len(structural_events) != 1:
                reasons.append("passing structural evaluation is missing or ambiguous")

            if step.requires_eval_pass:
                gate_events = [
                    event
                    for event in step_eval_events
                    if event.payload.get("check_name") != structural_check
                    and event.payload.get("scope") != "pack"
                    and not (
                        event.payload.get("scope") is None
                        and event.payload.get("check_name") in pack_eval_check_names
                    )
                ]
                if not gate_events:
                    reasons.append("required evaluation gate is missing")
                elif any(event.payload.get("status") != EvalStatus.PASS.value for event in gate_events):
                    reasons.append("required evaluation gate did not pass")
                for required_check in step.required_eval_checks:
                    matching_events = [
                        event
                        for event in step_eval_events
                        if event.payload.get("check_name") == required_check
                        and event.payload.get("scope") == "step_executor"
                    ]
                    if len(matching_events) != 1:
                        reasons.append(f"required evaluation is missing or ambiguous: {required_check}")
                    elif matching_events[0].payload.get("status") != EvalStatus.PASS.value:
                        reasons.append(f"required evaluation did not pass: {required_check}")

            next_steps = self._next_steps(pack, ordered_steps, step)
            expected_handoff_targets = sorted(next_step.name for next_step in next_steps)
            outgoing_handoffs = [
                handoff for handoff in handoffs if handoff.from_agent_run_id == agent_run.id
            ]
            persisted_handoff_targets = sorted(
                handoff.next_objective for handoff in outgoing_handoffs
            )
            if persisted_handoff_targets != expected_handoff_targets:
                reasons.append("outgoing handoffs do not exactly match the declared dependency edges")
            else:
                for next_step in next_steps:
                    try:
                        self._validated_dependency_handoff(
                            run_id=run_id,
                            dependency_name=step.name,
                            dependency_step=step,
                            dependency_attempt=agent_run,
                            consumer_step=next_step,
                            consumer_agent=agents_by_role[next_step.agent_role],
                            handoffs=outgoing_handoffs,
                        )
                    except WorkflowRunnerError as exc:
                        reasons.append(str(exc))

            if not reasons:
                continue
            invalidated_steps.add(step.name)
            for candidate in agent_runs:
                if candidate.step_name != step.name or candidate.status != AgentRunStatus.COMPLETED:
                    continue
                invalidated = candidate.model_copy(
                    update={
                        "status": AgentRunStatus.CANCELLED,
                        "output_summary": f"Incomplete recovery checkpoint: {'; '.join(reasons)}.",
                    }
                )
                self.storage.update_agent_run(invalidated)
                try:
                    self.trace_logger.record(
                        run_id=run_id,
                        agent_run_id=candidate.id,
                        event_type=TraceEventType.RUNTIME_EVENT,
                        payload={
                            "action": "incomplete_checkpoint_invalidated",
                            "step_name": step.name,
                            "reasons": reasons,
                        },
                    )
                except Exception:
                    pass

    def _run(self, run: Run, pack: WorkflowPack, *, resume_waiting: bool) -> Run:
        active_run = self._start_run(run, resume_waiting=resume_waiting)
        task = self._task_for_run(active_run)
        last_completed_agent_run: AgentRun | None = None
        current_agent_run: AgentRun | None = None
        current_step: WorkflowStep | None = None
        current_agent: AgentDefinition | None = None
        current_agent_session: AgentSession | None = None
        current_runtime_job: RuntimeJob | None = None

        try:
            if task.workflow_pack != pack.name:
                raise WorkflowRunnerError(
                    f"Run task expects workflow pack {task.workflow_pack}, got {pack.name}"
                )

            final_artifact_type = self._artifact_type(pack.final_artifact_type)
            self._register_pack_agents(pack)
            self._invalidate_incomplete_checkpoints(active_run.id, pack)
            self._record_task_intake_trace(task, active_run, pack)
            ready_batches = self._ready_batches(pack)
            self._validate_ready_batch_ownership(ready_batches)
            self._record_ready_batch_trace(active_run, ready_batches)

            ordered_steps = self._ordered_steps(pack)
            has_explicit_dependencies = any(step.depends_on for step in pack.steps)
            handoffs_by_target_step = self._handoffs_by_target_step(active_run.id)
            agent_runs_by_step, artifacts_by_step = self._completed_step_state(active_run.id, ordered_steps)
            completed_steps = set(agent_runs_by_step)
            if agent_runs_by_step:
                last_completed_agent_run = _last_ordered_agent_run(ordered_steps, agent_runs_by_step)

            while len(completed_steps) < len(ordered_steps):
                ready_steps = self._ready_steps(
                    ordered_steps,
                    completed_steps,
                    has_explicit_dependencies=has_explicit_dependencies,
                )
                if not ready_steps:
                    pending_steps = self._pending_approval_step_names(active_run.id)
                    if pending_steps:
                        return self._wait_run(active_run, pending_steps[0])
                    incomplete = [step.name for step in ordered_steps if step.name not in completed_steps]
                    raise WorkflowRunnerError(f"Workflow did not complete; unresolved steps: {', '.join(incomplete)}")

                parallel_candidate = (
                    has_explicit_dependencies
                    and self._can_prepare_batch_for_parallel(task, ready_steps)
                )
                steps_to_prepare = (
                    ready_steps
                    if parallel_candidate or any(step.session_policy.requires_approval for step in ready_steps)
                    else ready_steps[:1]
                )
                prepared_steps: list[_PreparedStepExecution] = []
                for step in steps_to_prepare:
                    try:
                        active_run, prepared = self._prepare_step_execution(
                            active_run,
                            task,
                            pack,
                            step,
                            agent_runs_by_step,
                            handoffs_by_target_step,
                        )
                    except _StepPreparationError as exc:
                        current_step = exc.step
                        current_agent = exc.agent
                        current_agent_run = exc.agent_run
                        current_agent_session = exc.agent_session
                        current_runtime_job = exc.runtime_job
                        active_run = active_run.model_copy(update={"current_step": exc.step.name})
                        self.storage.update_run(active_run)
                        raise exc.original
                    current_step = prepared.step
                    current_agent = prepared.agent
                    current_agent_run = prepared.agent_run
                    current_agent_session = prepared.agent_session
                    current_runtime_job = prepared.runtime_job
                    if (
                        prepared.runtime_job is not None
                        and prepared.runtime_job.status == RuntimeJobStatus.APPROVAL_REQUIRED
                    ):
                        continue
                    prepared_steps.append(prepared)

                if not prepared_steps:
                    pending_steps = self._pending_approval_step_names(active_run.id)
                    if pending_steps:
                        return self._wait_run(active_run, pending_steps[0])
                    incomplete = [step.name for step in ordered_steps if step.name not in completed_steps]
                    raise WorkflowRunnerError(f"Workflow did not complete; unresolved steps: {', '.join(incomplete)}")

                executed_steps = self._execute_prepared_steps(
                    active_run,
                    task,
                    prepared_steps,
                    allow_parallel=(
                        parallel_candidate
                        and self._can_execute_batch_in_parallel(task, prepared_steps)
                    ),
                )
                for executed_step in executed_steps:
                    prepared = executed_step.prepared
                    current_step = prepared.step
                    current_agent = prepared.agent
                    current_agent_run = prepared.agent_run
                    current_agent_session = prepared.agent_session
                    current_runtime_job = prepared.runtime_job
                    if isinstance(executed_step.output, Exception):
                        active_run = active_run.model_copy(update={"current_step": prepared.step.name})
                        self.storage.update_run(active_run)
                        self._cancel_uncommitted_prepared_steps(
                            active_run,
                            prepared_steps,
                            failed_step=prepared,
                            reason="parallel_step_failed",
                        )
                        raise executed_step.output

                    try:
                        commit_result = self._commit_step_output(
                            active_run,
                            task,
                            pack,
                            ordered_steps,
                            executed_step,
                            handoffs_by_target_step,
                            agent_runs_by_step,
                            artifacts_by_step,
                            completed_steps,
                        )
                    except _StepCommitError as exc:
                        current_step = exc.step
                        current_agent = exc.agent
                        current_agent_run = exc.agent_run
                        current_agent_session = exc.agent_session
                        current_runtime_job = exc.runtime_job
                        active_run = active_run.model_copy(update={"current_step": exc.step.name})
                        self.storage.update_run(active_run)
                        self._cancel_uncommitted_prepared_steps(
                            active_run,
                            prepared_steps,
                            failed_step=executed_step.prepared,
                            reason="parallel_step_commit_failed",
                        )
                        raise exc.original

                    current_agent_run = commit_result.agent_run
                    current_agent_session = commit_result.agent_session
                    current_runtime_job = commit_result.runtime_job
                    last_completed_agent_run = commit_result.agent_run

            final_artifact = self._select_final_artifact(pack, final_artifact_type, artifacts_by_step)
            blocking_failures = self._evaluate_pack(active_run, last_completed_agent_run, pack)
            if blocking_failures:
                failed_checks = ", ".join(result.check_name for result in blocking_failures)
                raise WorkflowRunnerError(f"Blocking evaluation failed: {failed_checks}")

            self._terminalize_open_runtime_state(active_run.id, reason="run_completed")
            final_run = active_run.model_copy(
                update={
                    "status": RunStatus.COMPLETED,
                    "current_step": None,
                    "finished_at": utc_now(),
                    "final_artifact_id": final_artifact.id,
                }
            )
            self.storage.update_run(final_run)
            return final_run
        except Exception as exc:
            failed_run = self._fail_run(
                active_run,
                current_agent_run,
                current_step,
                current_agent,
                exc,
                current_agent_session,
                current_runtime_job,
            )
            self._terminalize_open_runtime_state(failed_run.id, reason="run_failed")
            if isinstance(exc, WorkflowRunnerError):
                raise
            raise WorkflowRunnerError(_safe_error_message(exc)) from exc

    def _start_run(self, run: Run, *, resume_waiting: bool = False) -> Run:
        existing = self.storage.get_run(run.id)
        if existing is None:
            if resume_waiting:
                raise WorkflowRunnerError(f"Run not found: {run.id}")
            started = run.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "current_step": None,
                    "started_at": utc_now(),
                    "finished_at": None,
                    "final_artifact_id": None,
                }
            )
            self.storage.create_run(started)
            return started

        if resume_waiting and existing.status == RunStatus.WAITING:
            resumed = existing.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "finished_at": None,
                }
            )
            self.storage.update_run(resumed)
            return resumed

        if existing.status != RunStatus.QUEUED:
            raise WorkflowRunnerError(f"Run is not queued and cannot be started: {existing.id}")

        started = existing.model_copy(
            update={
                "status": RunStatus.RUNNING,
                "current_step": None,
                "started_at": existing.started_at or utc_now(),
                "finished_at": None,
                "final_artifact_id": None,
            }
        )
        self.storage.update_run(started)
        return started

    def _task_for_run(self, run: Run) -> Task:
        task = self.storage.get_task(run.task_id)
        if task is None:
            raise WorkflowRunnerError(f"Task not found for run: {run.task_id}")
        return task

    def _register_pack_agents(self, pack: WorkflowPack) -> None:
        for agent in pack.agents:
            registered_by_id = self.registry.get_agent(agent.id)
            registered_by_role = self.registry.get_agent_for_role(agent.pack_name, agent.role)
            if registered_by_id is None and registered_by_role is None:
                self.registry.register_agent(agent)
            elif registered_by_id != agent or registered_by_role != agent:
                raise WorkflowRunnerError(f"Agent registry conflict for {agent.pack_name}/{agent.role}")

            stored = self.storage.get_agent_definition(agent.id)
            stored_by_role = self.storage.get_agent_definition_by_pack_role(agent.pack_name, agent.role)
            try:
                self.storage.upsert_agent_definition(agent)
            except Exception as exc:
                raise WorkflowRunnerError(
                    "Stored agent definition conflict: "
                    f"incoming={agent.id}/{agent.pack_name}/{agent.role}, "
                    f"stored_by_id={_agent_identity(stored)}, "
                    f"stored_by_role={_agent_identity(stored_by_role)}"
                ) from exc

    def _start_agent_run(
        self,
        run: Run,
        agent: AgentDefinition,
        step: WorkflowStep,
        *,
        status: AgentRunStatus = AgentRunStatus.RUNNING,
    ) -> AgentRun:
        agent_run = AgentRun(
            run_id=run.id,
            agent_id=agent.id,
            step_name=step.name,
            input_context={
                "required_inputs": step.required_inputs,
                "required_artifacts": step.required_artifacts,
                "allowed_tools": step.allowed_tools,
                "depends_on": step.depends_on,
                "phase": step.phase,
                "coordination_role": step.coordination_role,
                "controller_step": step.controller_step,
                "return_contract": step.return_contract.model_dump(mode="json")
                if step.return_contract
                else None,
                "runtime": step.runtime,
                "session_policy": step.session_policy.model_dump(mode="json"),
                "requires_eval_pass": step.requires_eval_pass,
                "required_eval_checks": step.required_eval_checks,
                "requires_artifact": step.requires_artifact,
                "ownership": step.ownership,
            },
            status=status,
            started_at=utc_now(),
        )
        return self.storage.create_agent_run(agent_run)

    def _activate_agent_run(self, agent_run: AgentRun) -> AgentRun:
        activated = agent_run.model_copy(update={"status": AgentRunStatus.RUNNING})
        self.storage.update_agent_run(activated)
        return activated

    def _agent_run_for_step(self, run_id: str, step_name: str) -> AgentRun | None:
        matches = [
            agent_run
            for agent_run in self.storage.list_agent_runs_for_run(run_id)
            if agent_run.step_name == step_name
        ]
        return matches[-1] if matches else None

    def _agent_session_for_agent_run(self, run_id: str, agent_run_id: str) -> AgentSession | None:
        matches = [
            session
            for session in self.storage.list_agent_sessions_for_run(run_id)
            if session.agent_run_id == agent_run_id
        ]
        return matches[-1] if matches else None

    def _runtime_job_for_agent_run(self, run_id: str, agent_run_id: str) -> RuntimeJob | None:
        matches = [
            job
            for job in self.storage.list_runtime_jobs_for_run(run_id)
            if job.agent_run_id == agent_run_id
        ]
        return matches[-1] if matches else None

    def _handoffs_by_target_step(self, run_id: str) -> dict[str, list[Handoff]]:
        latest_completed_by_step: dict[str, AgentRun] = {}
        for agent_run in self.storage.list_agent_runs_for_run(run_id):
            if agent_run.status == AgentRunStatus.COMPLETED:
                latest_completed_by_step[agent_run.step_name] = agent_run

        handoffs_by_target: dict[str, list[Handoff]] = {}
        for handoff in self.storage.list_handoffs_for_run(run_id):
            source_agent_run = self.storage.get_agent_run(handoff.from_agent_run_id)
            if source_agent_run is None or source_agent_run.status != AgentRunStatus.COMPLETED:
                continue
            latest_source = latest_completed_by_step.get(source_agent_run.step_name)
            if latest_source is None or latest_source.id != source_agent_run.id:
                continue
            handoffs_by_target.setdefault(handoff.next_objective, []).append(handoff)
        return handoffs_by_target

    def _completed_step_state(
        self,
        run_id: str,
        ordered_steps: list[WorkflowStep],
    ) -> tuple[dict[str, AgentRun], dict[str, list[Artifact]]]:
        step_order = {step.name: index for index, step in enumerate(ordered_steps)}
        completed_agent_runs = {
            agent_run.step_name: agent_run
            for agent_run in self.storage.list_agent_runs_for_run(run_id)
            if agent_run.status == AgentRunStatus.COMPLETED and agent_run.step_name in step_order
        }
        artifacts_by_agent_run: dict[str, list[Artifact]] = {}
        for artifact in self.storage.list_artifacts_for_run(run_id):
            artifacts_by_agent_run.setdefault(artifact.agent_run_id, []).append(artifact)
        artifacts_by_step = {
            step_name: artifacts_by_agent_run.get(agent_run.id, [])
            for step_name, agent_run in completed_agent_runs.items()
        }
        return completed_agent_runs, artifacts_by_step

    def _pending_approval_step_names(self, run_id: str) -> list[str]:
        return [
            job.step_name
            for job in self.storage.list_runtime_jobs_for_run(run_id)
            if job.status == RuntimeJobStatus.APPROVAL_REQUIRED
        ]

    def _ready_steps(
        self,
        ordered_steps: list[WorkflowStep],
        completed_steps: set[str],
        *,
        has_explicit_dependencies: bool,
    ) -> list[WorkflowStep]:
        if has_explicit_dependencies:
            return [
                step
                for step in ordered_steps
                if step.name not in completed_steps
                and all(dependency in completed_steps for dependency in step.depends_on)
            ]
        for step in ordered_steps:
            if step.name not in completed_steps:
                return [step]
        return []

    def _prepare_step_execution(
        self,
        active_run: Run,
        task: Task,
        pack: WorkflowPack,
        step: WorkflowStep,
        agent_runs_by_step: dict[str, AgentRun],
        handoffs_by_target_step: dict[str, list[Handoff]],
    ) -> tuple[Run, _PreparedStepExecution]:
        active_run = active_run.model_copy(update={"current_step": step.name})
        self.storage.update_run(active_run)
        agent: AgentDefinition | None = None
        agent_run: AgentRun | None = None
        agent_session: AgentSession | None = None
        runtime_job: RuntimeJob | None = None
        try:
            agent = self._agent_for_step(pack, step)
            agent_run = self._agent_run_for_step(active_run.id, step.name)
            if agent_run is None or agent_run.status in {
                AgentRunStatus.CANCELLED,
                AgentRunStatus.FAILED,
            }:
                agent_run = self._start_agent_run(
                    active_run,
                    agent,
                    step,
                    status=AgentRunStatus.WAITING if step.session_policy.requires_approval else AgentRunStatus.RUNNING,
                )
                agent_session, runtime_job = self._record_runtime_state_if_needed(
                    active_run,
                    agent_run,
                    agent,
                    step,
                )
            else:
                agent_session = self._agent_session_for_agent_run(active_run.id, agent_run.id)
                runtime_job = self._runtime_job_for_agent_run(active_run.id, agent_run.id)

            if (
                agent_session is not None
                and agent_session.status == AgentSessionStatus.WAITING_APPROVAL
                and runtime_job is not None
                and runtime_job.status == RuntimeJobStatus.APPROVED
            ):
                agent_session = self._activate_approved_agent_session(agent_session)

            self._validate_step_inputs(task, step)
            self._validate_step_artifacts(active_run, step, pack)
            if runtime_job is not None and runtime_job.status == RuntimeJobStatus.APPROVAL_REQUIRED:
                return active_run, _PreparedStepExecution(
                    step=step,
                    agent=agent,
                    agent_run=agent_run,
                    agent_session=agent_session,
                    runtime_job=runtime_job,
                )

            if agent_run.status in {AgentRunStatus.WAITING, AgentRunStatus.QUEUED}:
                agent_run = self._activate_agent_run(agent_run)
            elif agent_run.status in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.CANCELLED,
            }:
                raise WorkflowRunnerError(
                    f"Step {step.name} cannot run from agent_run status {agent_run.status.value}."
                )

            upstream_handoffs, dependency_lineage = self._dependency_context_for_step(
                run_id=active_run.id,
                pack=pack,
                step=step,
                agent=agent,
                candidate_handoffs=handoffs_by_target_step.get(step.name, []),
                agent_runs_by_step=agent_runs_by_step,
            )
            if dependency_lineage:
                agent_run = agent_run.model_copy(
                    update={
                        "input_context": {
                            **agent_run.input_context,
                            "dependency_lineage": dependency_lineage,
                        }
                    }
                )
                self.storage.update_agent_run(agent_run)
            previous_agent_run, previous_handoff = self._previous_context_for_step(
                step,
                upstream_handoffs,
                agent_runs_by_step,
            )
            context = self._build_context(
                task,
                active_run,
                step,
                agent_run,
                previous_agent_run,
                previous_handoff,
                upstream_handoffs,
                agent_session,
                runtime_job,
            )
            return active_run, _PreparedStepExecution(
                step=step,
                agent=agent,
                agent_run=agent_run,
                agent_session=agent_session,
                runtime_job=runtime_job,
                context=context,
            )
        except Exception as exc:
            raise _StepPreparationError(
                exc,
                step=step,
                agent=agent,
                agent_run=agent_run,
                agent_session=agent_session,
                runtime_job=runtime_job,
            ) from exc

    def _execute_prepared_steps(
        self,
        active_run: Run,
        task: Task,
        prepared_steps: list[_PreparedStepExecution],
        *,
        allow_parallel: bool,
    ) -> list[_ExecutedStepOutput]:
        if allow_parallel and len(prepared_steps) > 1:
            self.trace_logger.record(
                run_id=active_run.id,
                event_type=TraceEventType.WORKFLOW_EVENT,
                payload={
                    "action": "parallel_step_batch_executed",
                    "steps": [prepared.step.name for prepared in prepared_steps],
                    "ownership": {
                        prepared.step.name: prepared.step.ownership
                        for prepared in prepared_steps
                    },
                    "true_parallel_execution": True,
                },
            )
            outputs_by_step: dict[str, AgentStepOutput | Exception] = {}
            with ThreadPoolExecutor(max_workers=len(prepared_steps)) as executor:
                futures = {
                    executor.submit(
                        self._execute_prepared_step,
                        active_run,
                        task,
                        prepared,
                    ): prepared
                    for prepared in prepared_steps
                }
                for future in as_completed(futures):
                    prepared = futures[future]
                    try:
                        outputs_by_step[prepared.step.name] = future.result()
                    except Exception as exc:  # preserve stable commit/error ordering below
                        outputs_by_step[prepared.step.name] = exc
            return [
                _ExecutedStepOutput(prepared=prepared, output=outputs_by_step[prepared.step.name])
                for prepared in prepared_steps
            ]

        return [
            executed_step
            for executed_step in self._execute_prepared_steps_serial(active_run, task, prepared_steps)
        ]

    def _execute_prepared_steps_serial(
        self,
        active_run: Run,
        task: Task,
        prepared_steps: list[_PreparedStepExecution],
    ) -> list[_ExecutedStepOutput]:
        executed_steps: list[_ExecutedStepOutput] = []
        for prepared in prepared_steps:
            try:
                output: AgentStepOutput | Exception = self._execute_prepared_step(active_run, task, prepared)
            except Exception as exc:
                output = exc
                executed_steps.append(_ExecutedStepOutput(prepared=prepared, output=output))
                break
            executed_steps.append(_ExecutedStepOutput(prepared=prepared, output=output))
        return executed_steps

    def _execute_prepared_step(
        self,
        active_run: Run,
        task: Task,
        prepared: _PreparedStepExecution,
    ) -> AgentStepOutput:
        if prepared.context is None:
            raise WorkflowRunnerError(f"Step {prepared.step.name} has no executable context.")
        return self.executor.execute(
            task=task,
            run=active_run,
            step=prepared.step,
            agent=prepared.agent,
            context=prepared.context,
        )

    def _can_execute_batch_in_parallel(self, task: Task, prepared_steps: list[_PreparedStepExecution]) -> bool:
        if len(prepared_steps) <= 1:
            return False
        if not self._executor_allows_parallel_execution(task, prepared_steps):
            return False
        for prepared in prepared_steps:
            if prepared.step.session_policy.requires_approval:
                return False
            if not prepared.step.ownership:
                return False
        return self._batch_ownership_conflicts([prepared.step for prepared in prepared_steps]) == []

    def _can_prepare_batch_for_parallel(self, task: Task, ready_steps: list[WorkflowStep]) -> bool:
        if len(ready_steps) <= 1:
            return False
        if not self._executor_allows_parallel_execution_for_steps(task, ready_steps):
            return False
        for step in ready_steps:
            if step.session_policy.requires_approval:
                return False
            if not step.ownership:
                return False
        return self._batch_ownership_conflicts(ready_steps) == []

    def _executor_allows_parallel_execution(
        self,
        task: Task,
        prepared_steps: list[_PreparedStepExecution],
    ) -> bool:
        return self._executor_allows_parallel_execution_for_steps(
            task,
            [prepared.step for prepared in prepared_steps],
        )

    def _executor_allows_parallel_execution_for_steps(
        self,
        task: Task,
        steps: list[WorkflowStep],
    ) -> bool:
        capability = getattr(self.executor, "supports_parallel_execution", False)
        if callable(capability):
            return bool(
                capability(
                    task=task,
                    steps=steps,
                )
            )
        return bool(capability)

    def _commit_step_output(
        self,
        active_run: Run,
        task: Task,
        pack: WorkflowPack,
        ordered_steps: list[WorkflowStep],
        executed_step: _ExecutedStepOutput,
        handoffs_by_target_step: dict[str, list[Handoff]],
        agent_runs_by_step: dict[str, AgentRun],
        artifacts_by_step: dict[str, list[Artifact]],
        completed_steps: set[str],
    ) -> _CommitResult:
        prepared = executed_step.prepared
        step = prepared.step
        output = executed_step.output
        if isinstance(output, Exception):
            raise output

        agent_run = prepared.agent_run
        agent_session = prepared.agent_session
        runtime_job = prepared.runtime_job
        completed_agent_run = agent_run
        try:
            self._record_model_runtime_trace(active_run, agent_run, output)
            output_artifacts = [
                (
                    self._artifact_type(artifact.type),
                    artifact,
                )
                for artifact in output.artifacts
            ]
            artifact_types = [artifact_type for artifact_type, _artifact in output_artifacts]
            self._validate_step_outputs(step, artifact_types)
            self._validate_return_contract(step, output, artifact_types)

            artifacts = []
            for index, (artifact_type, artifact) in enumerate(output_artifacts):
                artifacts.append(
                    self.artifact_store.write_text(
                        run_id=active_run.id,
                        agent_run_id=agent_run.id,
                        artifact_type=artifact_type,
                        filename=self._step_scoped_filename(
                            active_run.id,
                            agent_run,
                            step,
                            index,
                            artifact.filename,
                        ),
                        content=artifact.content,
                        source_refs=artifact.source_refs,
                    )
                )

            self._record_eval_result(
                active_run,
                agent_run,
                f"{step.name}:artifacts_created",
                EvalStatus.PASS if artifacts else EvalStatus.WARN,
                "Step created one or more artifacts." if artifacts else "Step completed without artifacts.",
                artifacts[-1].id if artifacts else None,
                scope="step_structural",
            )

            for eval_result in output.eval_results:
                if eval_result.run_id != active_run.id:
                    raise WorkflowRunnerError("Executor returned eval result for a different run.")
                if eval_result.check_name == f"{step.name}:artifacts_created":
                    raise WorkflowRunnerError(
                        "Executor eval results cannot reuse the structural artifact check name."
                    )
                self.storage.create_eval_result(eval_result)
                self.trace_logger.record(
                    run_id=active_run.id,
                    agent_run_id=agent_run.id,
                    event_type=TraceEventType.EVAL_RESULT,
                    payload={
                        "eval_result_id": eval_result.id,
                        "check_name": eval_result.check_name,
                        "status": eval_result.status.value,
                        "scope": "step_executor",
                    },
                )
                if eval_result.status == EvalStatus.FAIL:
                    raise WorkflowRunnerError(f"Executor evaluation failed: {eval_result.check_name}")
            self._validate_step_eval_gate(step, output.eval_results)

            next_steps = self._next_steps(pack, ordered_steps, step)
            for next_step in next_steps:
                handoff = Handoff(
                    run_id=active_run.id,
                    from_agent_run_id=agent_run.id,
                    to_agent_id=self._agent_for_step(pack, next_step).id,
                    summary=output.summary,
                    artifact_refs=[artifact.id for artifact in artifacts],
                    open_questions=output.open_questions,
                    next_objective=next_step.name,
                    constraints_to_preserve=task.constraints,
                    risk_notes=output.risk_notes,
                )
                stored_handoff = self.storage.create_handoff(handoff)
                handoffs_by_target_step.setdefault(next_step.name, []).append(stored_handoff)
                self.trace_logger.record(
                    run_id=active_run.id,
                    agent_run_id=agent_run.id,
                    event_type=TraceEventType.HANDOFF,
                    payload={"handoff_id": stored_handoff.id, "to_agent_id": stored_handoff.to_agent_id},
                )

            if agent_session is not None:
                agent_session = self._complete_agent_session(agent_session)
            if runtime_job is not None:
                runtime_job = self._complete_runtime_job(runtime_job)
            completed_agent_run = agent_run.model_copy(
                update={
                    "status": AgentRunStatus.COMPLETED,
                    "finished_at": utc_now(),
                    "output_summary": output.summary,
                }
            )
            self.storage.update_agent_run(completed_agent_run)
            agent_runs_by_step[step.name] = completed_agent_run
            completed_steps.add(step.name)
            artifacts_by_step[step.name] = artifacts
            return _CommitResult(
                agent_run=completed_agent_run,
                agent_session=agent_session,
                runtime_job=runtime_job,
            )
        except Exception as exc:
            raise _StepCommitError(
                exc,
                step=step,
                agent=prepared.agent,
                agent_run=completed_agent_run,
                agent_session=agent_session,
                runtime_job=runtime_job,
            ) from exc

    def _cancel_uncommitted_prepared_steps(
        self,
        active_run: Run,
        prepared_steps: list[_PreparedStepExecution],
        *,
        failed_step: _PreparedStepExecution,
        reason: str,
    ) -> None:
        if len(prepared_steps) <= 1:
            return
        cancelled_steps: list[str] = []
        now = utc_now()
        for prepared in prepared_steps:
            if prepared.step.name == failed_step.step.name:
                continue
            if self._cancel_uncommitted_prepared_step(prepared, now):
                cancelled_steps.append(prepared.step.name)

        if cancelled_steps:
            self.trace_logger.record(
                run_id=active_run.id,
                event_type=TraceEventType.WORKFLOW_EVENT,
                payload={
                    "action": "parallel_step_batch_aborted",
                    "failed_step": failed_step.step.name,
                    "cancelled_uncommitted_steps": cancelled_steps,
                    "reason": reason,
                },
            )

    def _cancel_uncommitted_prepared_step(self, prepared: _PreparedStepExecution, now: datetime) -> bool:
        agent_run_cancelled = self._cancel_uncommitted_agent_run(prepared, now)
        self._cancel_uncommitted_agent_session(prepared, now)
        self._cancel_uncommitted_runtime_job(prepared, now)
        return agent_run_cancelled

    def _cancel_uncommitted_agent_run(self, prepared: _PreparedStepExecution, now: datetime) -> bool:
        agent_run = self.storage.get_agent_run(prepared.agent_run.id) or prepared.agent_run
        if _is_terminal_agent_run_status(agent_run.status):
            return False
        self.storage.update_agent_run(
            agent_run.model_copy(
                update={
                    "status": AgentRunStatus.CANCELLED,
                    "finished_at": now,
                    "output_summary": "Parallel batch aborted before this step output was committed.",
                }
            )
        )
        return True

    def _cancel_uncommitted_agent_session(self, prepared: _PreparedStepExecution, now: datetime) -> None:
        if prepared.agent_session is None:
            return
        agent_session = self.storage.get_agent_session(prepared.agent_session.id) or prepared.agent_session
        if _is_terminal_agent_session_status(agent_session.status):
            return
        self.storage.update_agent_session(
            agent_session.model_copy(update={"status": AgentSessionStatus.CANCELLED, "updated_at": now})
        )

    def _cancel_uncommitted_runtime_job(self, prepared: _PreparedStepExecution, now: datetime) -> None:
        if prepared.runtime_job is None:
            return
        runtime_job = self.storage.get_runtime_job(prepared.runtime_job.id) or prepared.runtime_job
        if _is_terminal_runtime_job_status(runtime_job.status):
            return
        self.storage.update_runtime_job(
            runtime_job.model_copy(
                update={
                    "status": RuntimeJobStatus.CANCELLED,
                    "updated_at": now,
                    "message": "Parallel batch aborted before this step output was committed.",
                }
            )
        )

    def _terminalize_open_runtime_state(self, run_id: str, *, reason: str) -> None:
        now = utc_now()
        cancelled_agent_runs: list[str] = []
        cancelled_sessions: list[str] = []
        cancelled_jobs: list[str] = []
        message = f"Run terminalized before this runtime state completed: {reason}."

        for agent_run in self.storage.list_agent_runs_for_run(run_id):
            if _is_terminal_agent_run_status(agent_run.status):
                continue
            self.storage.update_agent_run(
                agent_run.model_copy(
                    update={
                        "status": AgentRunStatus.CANCELLED,
                        "finished_at": now,
                        "output_summary": message,
                    }
                )
            )
            cancelled_agent_runs.append(agent_run.step_name)

        for session in self.storage.list_agent_sessions_for_run(run_id):
            if _is_terminal_agent_session_status(session.status):
                continue
            self.storage.update_agent_session(
                session.model_copy(
                    update={
                        "status": AgentSessionStatus.CANCELLED,
                        "updated_at": now,
                        "metadata": {
                            **session.metadata,
                            "terminalized_reason": reason,
                            "external_runtime_started": False,
                        },
                    }
                )
            )
            cancelled_sessions.append(session.step_name)

        for job in self.storage.list_runtime_jobs_for_run(run_id):
            if _is_terminal_runtime_job_status(job.status):
                continue
            self.storage.update_runtime_job(
                job.model_copy(
                    update={
                        "status": RuntimeJobStatus.CANCELLED,
                        "updated_at": now,
                        "message": message,
                        "metadata": {
                            **job.metadata,
                            "terminalized_reason": reason,
                            "external_runtime_started": False,
                        },
                    }
                )
            )
            cancelled_jobs.append(job.step_name)

        if cancelled_agent_runs or cancelled_sessions or cancelled_jobs:
            self.trace_logger.record(
                run_id=run_id,
                event_type=TraceEventType.RUNTIME_EVENT,
                payload={
                    "action": "open_runtime_state_terminalized",
                    "reason": reason,
                    "cancelled_agent_runs": cancelled_agent_runs,
                    "cancelled_sessions": cancelled_sessions,
                    "cancelled_jobs": cancelled_jobs,
                    "external_runtime_started": False,
                },
            )

    def _wait_run(self, run: Run, current_step: str) -> Run:
        waiting_run = run.model_copy(
            update={
                "status": RunStatus.WAITING,
                "current_step": current_step,
                "finished_at": None,
            }
        )
        self.storage.update_run(waiting_run)
        self.trace_logger.record(
            run_id=run.id,
            agent_run_id=None,
            event_type=TraceEventType.RUNTIME_EVENT,
            payload={
                "action": "run_waiting_for_local_approval",
                "step_name": current_step,
                "external_runtime_started": False,
            },
        )
        return waiting_run

    def _agent_for_step(self, pack: WorkflowPack, step: WorkflowStep) -> AgentDefinition:
        agent = self.registry.get_agent_for_role(pack.name, step.agent_role)
        if agent is None:
            raise WorkflowRunnerError(f"No agent registered for step {step.name}: {step.agent_role}")
        return agent

    def _build_context(
        self,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent_run: AgentRun,
        previous_agent_run: AgentRun | None,
        previous_handoff: Handoff | None,
        upstream_handoffs: list[Handoff] | None = None,
        agent_session: AgentSession | None = None,
        runtime_job: RuntimeJob | None = None,
    ) -> dict[str, Any]:
        completed_agent_run_ids = {
            stored_agent_run.id
            for stored_agent_run in self.storage.list_agent_runs_for_run(run.id)
            if stored_agent_run.status == AgentRunStatus.COMPLETED
        }
        artifacts = [
            artifact
            for artifact in self.storage.list_artifacts_for_run(run.id)
            if artifact.agent_run_id in completed_agent_run_ids
        ]
        max_artifacts = step.context_policy.max_artifacts
        selected_artifacts = artifacts[-max_artifacts:] if max_artifacts else []
        artifact_texts: dict[str, str] = {}
        truncated_artifact_text_ids: set[str] = set()
        if step.context_policy.artifact_excerpt_chars:
            for artifact in selected_artifacts:
                text, truncated = self.artifact_store.read_text_excerpt(
                    artifact,
                    max_chars=step.context_policy.artifact_excerpt_chars,
                )
                artifact_texts[artifact.id] = text
                if truncated:
                    truncated_artifact_text_ids.add(artifact.id)
        result = self.context_injector.build(
            task=task,
            run=run,
            step=step,
            agent_run=agent_run,
            previous_agent_run=previous_agent_run,
            previous_handoff=previous_handoff,
            upstream_handoffs=upstream_handoffs,
            agent_session=agent_session,
            runtime_job=runtime_job,
            artifacts=selected_artifacts,
            total_artifacts=artifacts,
            artifact_texts=artifact_texts,
            truncated_artifact_text_ids=truncated_artifact_text_ids,
        )
        self.trace_logger.record(
            run_id=run.id,
            agent_run_id=agent_run.id,
            event_type=TraceEventType.WORKFLOW_EVENT,
            payload=result.trace_summary,
        )
        return result.context

    def _record_task_intake_trace(self, task: Task, run: Run, pack: WorkflowPack) -> None:
        result = analyze_task_intake(task)
        self.trace_logger.record(
            run_id=run.id,
            event_type=TraceEventType.WORKFLOW_EVENT,
            payload={
                "action": "task_intake_analyzed",
                "task_type": result.task_type,
                "complexity": result.complexity,
                "risk": result.risk,
                "domain": result.domain,
                "recommended_pack": result.recommended_pack,
                "actual_pack": task.workflow_pack,
                "confidence": result.confidence,
                "reasons": result.reasons,
                "constraints": result.constraints,
            },
        )

    def _previous_context_for_step(
        self,
        step: WorkflowStep,
        upstream_handoffs: list[Handoff],
        agent_runs_by_step: dict[str, AgentRun],
    ) -> tuple[AgentRun | None, Handoff | None]:
        if len(upstream_handoffs) == 1:
            handoff = upstream_handoffs[0]
            return self.storage.get_agent_run(handoff.from_agent_run_id), handoff
        if len(step.depends_on) == 1:
            return agent_runs_by_step.get(step.depends_on[0]), None
        return None, None

    def _dependency_context_for_step(
        self,
        *,
        run_id: str,
        pack: WorkflowPack,
        step: WorkflowStep,
        agent: AgentDefinition,
        candidate_handoffs: list[Handoff],
        agent_runs_by_step: dict[str, AgentRun],
    ) -> tuple[list[Handoff], dict[str, dict[str, str]]]:
        steps_by_name = {candidate.name: candidate for candidate in pack.steps}
        upstream_handoffs: list[Handoff] = []
        lineage: dict[str, dict[str, str]] = {}
        if not step.depends_on and not any(candidate.depends_on for candidate in pack.steps):
            ordered_steps = self._ordered_steps(pack)
            step_index = next(
                index for index, candidate in enumerate(ordered_steps) if candidate.name == step.name
            )
            if step_index == 0:
                return upstream_handoffs, lineage
            dependency_step = ordered_steps[step_index - 1]
            dependency_attempt = agent_runs_by_step.get(dependency_step.name)
            if dependency_attempt is None:
                raise WorkflowRunnerError(
                    f"Step {step.name} is missing the completed previous attempt: {dependency_step.name}"
                )
            upstream_handoffs.append(
                self._validated_dependency_handoff(
                    run_id=run_id,
                    dependency_name=dependency_step.name,
                    dependency_step=dependency_step,
                    dependency_attempt=dependency_attempt,
                    consumer_step=step,
                    consumer_agent=agent,
                    handoffs=candidate_handoffs,
                )
            )
            return upstream_handoffs, lineage
        for dependency_name in step.depends_on:
            dependency_attempt = agent_runs_by_step.get(dependency_name)
            if (
                dependency_attempt is None
                or dependency_attempt.run_id != run_id
                or dependency_attempt.step_name != dependency_name
                or dependency_attempt.status != AgentRunStatus.COMPLETED
            ):
                raise WorkflowRunnerError(
                    f"Step {step.name} is missing the completed dependency attempt: {dependency_name}"
                )
            handoff = self._validated_dependency_handoff(
                run_id=run_id,
                dependency_name=dependency_name,
                dependency_step=steps_by_name[dependency_name],
                dependency_attempt=dependency_attempt,
                consumer_step=step,
                consumer_agent=agent,
                handoffs=candidate_handoffs,
            )
            upstream_handoffs.append(handoff)
            lineage[dependency_name] = {
                "handoff_id": handoff.id,
                "from_agent_run_id": handoff.from_agent_run_id,
            }
        return upstream_handoffs, lineage

    def _validated_dependency_handoff(
        self,
        *,
        run_id: str,
        dependency_name: str,
        dependency_step: WorkflowStep,
        dependency_attempt: AgentRun,
        consumer_step: WorkflowStep,
        consumer_agent: AgentDefinition,
        handoffs: list[Handoff],
    ) -> Handoff:
        matching_handoffs = [
            handoff
            for handoff in handoffs
            if handoff.from_agent_run_id == dependency_attempt.id
            and handoff.next_objective == consumer_step.name
        ]
        if len(matching_handoffs) != 1:
            raise WorkflowRunnerError(
                f"Step {consumer_step.name} requires exactly one current handoff from {dependency_name}."
            )
        handoff = matching_handoffs[0]
        if handoff.run_id != run_id or handoff.to_agent_id != consumer_agent.id:
            raise WorkflowRunnerError(
                f"Step {consumer_step.name} handoff from {dependency_name} targets a different run or agent."
            )
        if len(handoff.artifact_refs) != len(set(handoff.artifact_refs)):
            raise WorkflowRunnerError(
                f"Step {consumer_step.name} handoff from {dependency_name} has duplicate artifact references."
            )
        referenced_artifacts = [
            self.storage.get_artifact(artifact_id) for artifact_id in handoff.artifact_refs
        ]
        if any(
            artifact is None
            or artifact.run_id != run_id
            or artifact.agent_run_id != dependency_attempt.id
            for artifact in referenced_artifacts
        ):
            raise WorkflowRunnerError(
                f"Step {consumer_step.name} handoff artifacts do not belong to the current {dependency_name} attempt."
            )
        produced_type = dependency_step.produces_artifact_type
        produced_artifacts = [
            artifact
            for artifact in referenced_artifacts
            if artifact is not None and artifact.type.value == produced_type
        ]
        if produced_type is not None and not produced_artifacts:
            raise WorkflowRunnerError(
                f"Step {consumer_step.name} handoff from {dependency_name} must reference a {produced_type} artifact."
            )
        if (
            produced_type == ArtifactType.PATCH.value
            and ArtifactType.PATCH.value in consumer_step.required_artifacts
            and len(produced_artifacts) != 1
        ):
            raise WorkflowRunnerError(
                f"Step {consumer_step.name} handoff from {dependency_name} requires exactly one patch artifact."
            )
        return handoff

    def _record_runtime_state_if_needed(
        self,
        run: Run,
        agent_run: AgentRun,
        agent: AgentDefinition,
        step: WorkflowStep,
    ) -> tuple[AgentSession | None, RuntimeJob | None]:
        if step.runtime == "model" and not step.session_policy.persistent and not step.session_policy.requires_approval:
            return None, None

        now = utc_now()
        session_status = (
            AgentSessionStatus.WAITING_APPROVAL
            if step.session_policy.requires_approval
            else AgentSessionStatus.ACTIVE
        )
        session = self.storage.create_agent_session(
            AgentSession(
                run_id=run.id,
                agent_run_id=agent_run.id,
                agent_id=agent.id,
                step_name=step.name,
                runtime=step.runtime,
                status=session_status,
                resume_strategy=step.session_policy.resume_strategy,
                requires_approval=step.session_policy.requires_approval,
                created_at=now,
                updated_at=now,
                metadata={
                    "phase": step.phase,
                    "coordination_role": step.coordination_role,
                    "persistent": step.session_policy.persistent,
                    "local_only": True,
                    "external_runtime_started": False,
                },
            )
        )
        job_status = (
            RuntimeJobStatus.APPROVAL_REQUIRED
            if step.session_policy.requires_approval
            else RuntimeJobStatus.RECORDED
        )
        job = self.storage.create_runtime_job(
            RuntimeJob(
                run_id=run.id,
                agent_run_id=agent_run.id,
                agent_session_id=session.id,
                step_name=step.name,
                runtime=step.runtime,
                status=job_status,
                approval_required=step.session_policy.requires_approval,
                created_at=now,
                updated_at=now,
                message=(
                    "External ACP execution is not launched in Phase 23; approval intent is recorded locally."
                    if step.session_policy.requires_approval
                    else "Persistent runtime intent recorded locally; no live child session is launched in Phase 23."
                ),
                metadata={
                    "resume_strategy": step.session_policy.resume_strategy,
                    "local_only": True,
                    "external_runtime_started": False,
                },
            )
        )
        self.trace_logger.record(
            run_id=run.id,
            agent_run_id=agent_run.id,
            event_type=TraceEventType.RUNTIME_EVENT,
            payload={
                "action": "runtime_recorded",
                "runtime": step.runtime,
                "agent_session_id": session.id,
                "runtime_job_id": job.id,
                "job_status": job.status.value,
                "approval_required": job.approval_required,
                "external_runtime_started": False,
            },
        )
        return session, job

    def _complete_agent_session(self, session: AgentSession) -> AgentSession:
        if session.status == AgentSessionStatus.WAITING_APPROVAL:
            return session
        completed = session.model_copy(update={"status": AgentSessionStatus.COMPLETED, "updated_at": utc_now()})
        self.storage.update_agent_session(completed)
        return completed

    def _activate_approved_agent_session(self, session: AgentSession) -> AgentSession:
        active = session.model_copy(
            update={
                "status": AgentSessionStatus.ACTIVE,
                "updated_at": utc_now(),
                "metadata": {
                    **session.metadata,
                    "external_runtime_started": False,
                    "local_approval_intent": "approved",
                },
            }
        )
        self.storage.update_agent_session(active)
        return active

    def _complete_runtime_job(self, job: RuntimeJob) -> RuntimeJob:
        if job.status == RuntimeJobStatus.APPROVAL_REQUIRED:
            return job
        completed = job.model_copy(update={"status": RuntimeJobStatus.COMPLETED, "updated_at": utc_now()})
        self.storage.update_runtime_job(completed)
        self.trace_logger.record(
            run_id=job.run_id,
            agent_run_id=job.agent_run_id,
            event_type=TraceEventType.RUNTIME_EVENT,
            payload={
                "action": "runtime_job_completed",
                "runtime": job.runtime,
                "agent_session_id": job.agent_session_id,
                "runtime_job_id": job.id,
                "job_status": completed.status.value,
                "approval_required": job.approval_required,
                "external_runtime_started": False,
            },
        )
        return completed

    def _ordered_steps(self, pack: WorkflowPack) -> list[WorkflowStep]:
        completed: set[str] = set()
        remaining = list(pack.steps)
        ordered: list[WorkflowStep] = []

        while remaining:
            ready = [step for step in remaining if set(step.depends_on).issubset(completed)]
            if not ready:
                blocked = ", ".join(step.name for step in remaining)
                raise WorkflowRunnerError(f"Workflow step dependencies cannot be resolved: {blocked}")
            for step in ready:
                ordered.append(step)
                completed.add(step.name)
                remaining.remove(step)

        return ordered

    def _ready_batches(self, pack: WorkflowPack) -> list[list[WorkflowStep]]:
        if not any(step.depends_on for step in pack.steps):
            return [[step] for step in pack.steps]

        completed: set[str] = set()
        remaining = list(pack.steps)
        batches: list[list[WorkflowStep]] = []

        while remaining:
            ready = [step for step in remaining if set(step.depends_on).issubset(completed)]
            if not ready:
                blocked = ", ".join(step.name for step in remaining)
                raise WorkflowRunnerError(f"Workflow step dependencies cannot be resolved: {blocked}")
            batches.append(ready)
            for step in ready:
                completed.add(step.name)
                remaining.remove(step)

        return batches

    def _validate_ready_batch_ownership(self, ready_batches: list[list[WorkflowStep]]) -> None:
        for batch in ready_batches:
            conflicts = self._batch_ownership_conflicts(batch)
            if conflicts:
                raise WorkflowRunnerError(f"Ready batch ownership conflict: {'; '.join(conflicts)}")

    def _batch_ownership_conflicts(self, batch: list[WorkflowStep]) -> list[str]:
        owners: dict[tuple[str, str], str] = {}
        conflicts: list[str] = []
        for step in batch:
            for resource_type, resources in step.ownership.items():
                for resource in resources:
                    key = (resource_type, resource)
                    previous_step = owners.get(key)
                    if previous_step is not None:
                        conflicts.append(f"{resource_type}:{resource} claimed by {previous_step} and {step.name}")
                    else:
                        owners[key] = step.name
        return conflicts

    def _record_ready_batch_trace(self, run: Run, ready_batches: list[list[WorkflowStep]]) -> None:
        self.trace_logger.record(
            run_id=run.id,
            event_type=TraceEventType.WORKFLOW_EVENT,
            payload={
                "action": "ready_batches_planned",
                "batches": [
                    {
                        "steps": [step.name for step in batch],
                        "parallel_candidate": len(batch) > 1,
                        "ownership": {
                            step.name: step.ownership
                            for step in batch
                            if step.ownership
                        },
                    }
                    for batch in ready_batches
                ],
                "true_parallel_execution": False,
            },
        )

    def _next_steps(
        self,
        pack: WorkflowPack,
        ordered_steps: list[WorkflowStep],
        current: WorkflowStep,
    ) -> list[WorkflowStep]:
        dependents = [step for step in pack.steps if current.name in step.depends_on]
        if dependents:
            ordered_names = {step.name: index for index, step in enumerate(ordered_steps)}
            return sorted(dependents, key=lambda step: ordered_names[step.name])

        if any(step.depends_on for step in pack.steps):
            return []

        steps = ordered_steps
        index = steps.index(current)
        if index + 1 >= len(steps):
            return []
        return [steps[index + 1]]

    def _validate_step_inputs(self, task: Task, step: WorkflowStep) -> None:
        available_inputs = set(task.inputs)
        available_inputs.update({"title", "goal", "constraints", "acceptance_criteria", "created_by"})
        missing = [name for name in step.required_inputs if name not in available_inputs]
        if missing:
            raise WorkflowRunnerError(f"Step {step.name} is missing required inputs: {', '.join(missing)}")

    def _validate_step_artifacts(self, run: Run, step: WorkflowStep, pack: WorkflowPack) -> None:
        existing_types = {artifact.type.value for artifact in self.storage.list_artifacts_for_run(run.id)}
        missing_required = [
            artifact_type for artifact_type in step.required_artifacts if artifact_type not in existing_types
        ]
        if missing_required:
            raise WorkflowRunnerError(
                f"Step {step.name} is missing required artifacts: {', '.join(missing_required)}"
            )

        if not step.requires_artifact:
            return
        upstream_steps = _upstream_steps(step.name, pack.steps)
        artifacts_by_agent_run: dict[str, list[Artifact]] = {}
        for artifact in self.storage.list_artifacts_for_run(run.id):
            artifacts_by_agent_run.setdefault(artifact.agent_run_id, []).append(artifact)
        upstream_artifact_types = {
            artifact.type.value
            for agent_run in self.storage.list_agent_runs_for_run(run.id)
            if agent_run.step_name in upstream_steps
            for artifact in artifacts_by_agent_run.get(agent_run.id, [])
        }
        missing_gate_artifacts = [
            artifact_type for artifact_type in step.requires_artifact if artifact_type not in upstream_artifact_types
        ]
        if missing_gate_artifacts:
            raise WorkflowRunnerError(
                f"Step {step.name} is missing upstream gate artifacts: {', '.join(missing_gate_artifacts)}"
            )

    def _validate_step_eval_gate(self, step: WorkflowStep, eval_results: list[EvalResult]) -> None:
        if not step.requires_eval_pass:
            return
        if not eval_results:
            raise WorkflowRunnerError(f"Step {step.name} requires eval_results to pass.")
        missing_checks: list[str] = []
        ambiguous_checks: list[str] = []
        for check_name in step.required_eval_checks:
            matching_results = [
                result for result in eval_results if result.check_name == check_name
            ]
            if not matching_results:
                missing_checks.append(check_name)
            elif len(matching_results) != 1:
                ambiguous_checks.append(check_name)
        if missing_checks:
            raise WorkflowRunnerError(
                f"Step {step.name} is missing required eval results: {', '.join(missing_checks)}"
            )
        if ambiguous_checks:
            raise WorkflowRunnerError(
                f"Step {step.name} has ambiguous required eval results: {', '.join(ambiguous_checks)}"
            )
        non_pass = [result.check_name for result in eval_results if result.status != EvalStatus.PASS]
        if non_pass:
            raise WorkflowRunnerError(
                f"Step {step.name} requires passing eval results: {', '.join(non_pass)}"
            )

    def _evaluate_pack(
        self,
        run: Run,
        agent_run: AgentRun | None,
        pack: WorkflowPack,
    ) -> list[EvalResult]:
        blocking_failures: list[EvalResult] = []
        for check in pack.eval_checks:
            result = self._evaluate_check(run, agent_run, check)
            if result.status == EvalStatus.FAIL:
                blocking_failures.append(result)
        return blocking_failures

    def _evaluate_check(self, run: Run, agent_run: AgentRun | None, check: EvalCheck) -> EvalResult:
        existing_types = {artifact.type.value for artifact in self.storage.list_artifacts_for_run(run.id)}
        missing = [artifact_type for artifact_type in check.required_artifact_types if artifact_type not in existing_types]
        if not missing:
            return self._record_eval_result(
                run,
                agent_run,
                check.name,
                EvalStatus.PASS,
                "Required artifact types are present.",
                scope="pack",
            )

        status = EvalStatus.FAIL if check.severity == "blocker" else EvalStatus.WARN
        result = self._record_eval_result(
            run,
            agent_run,
            check.name,
            status,
            f"Missing artifact types: {', '.join(missing)}",
            scope="pack",
        )
        return result

    def _validate_step_outputs(self, step: WorkflowStep, artifact_types: list[ArtifactType]) -> None:
        if not step.produces_artifact_type:
            return
        expected_type = self._artifact_type(step.produces_artifact_type)
        if expected_type not in artifact_types:
            raise WorkflowRunnerError(
                f"Step {step.name} declared produced artifact type {expected_type.value} "
                "but executor did not return it."
            )

    def _validate_return_contract(
        self,
        step: WorkflowStep,
        output: AgentStepOutput,
        artifact_types: list[ArtifactType],
    ) -> None:
        if step.return_contract is None:
            return

        if step.return_contract.require_summary and not output.summary.strip():
            raise WorkflowRunnerError(f"Step {step.name} return contract requires a summary.")

        missing_artifact_types = [
            required
            for required in step.return_contract.required_artifact_types
            if self._artifact_type(required) not in artifact_types
        ]
        if missing_artifact_types:
            raise WorkflowRunnerError(
                f"Step {step.name} return contract missing artifact types: "
                f"{', '.join(missing_artifact_types)}"
            )

        if step.return_contract.require_open_questions and not output.open_questions:
            raise WorkflowRunnerError(f"Step {step.name} return contract requires open_questions.")

        if step.return_contract.require_risk_notes and not output.risk_notes:
            raise WorkflowRunnerError(f"Step {step.name} return contract requires risk_notes.")

    def _record_eval_result(
        self,
        run: Run,
        agent_run: AgentRun | None,
        check_name: str,
        status: EvalStatus,
        message: str,
        artifact_id: str | None = None,
        *,
        scope: str = "system",
    ) -> EvalResult:
        result = self.storage.create_eval_result(
            EvalResult(
                run_id=run.id,
                artifact_id=artifact_id,
                check_name=check_name,
                status=status,
                message=message,
            )
        )
        self.trace_logger.record(
            run_id=run.id,
            agent_run_id=agent_run.id if agent_run is not None else None,
            event_type=TraceEventType.EVAL_RESULT,
            payload={
                "eval_result_id": result.id,
                "check_name": result.check_name,
                "status": result.status.value,
                "scope": scope,
            },
        )
        return result

    def _record_model_runtime_trace(
        self,
        run: Run,
        agent_run: AgentRun,
        output: AgentStepOutput,
    ) -> None:
        if output.model_request is not None:
            request_metadata = output.model_request.metadata
            self.trace_logger.record(
                run_id=run.id,
                agent_run_id=agent_run.id,
                event_type=TraceEventType.MODEL_ACTION,
                payload={
                    "action": "model_request",
                    "provider": output.model_request.provider,
                    "model": output.model_request.model,
                    "agent_id": request_metadata.get("agent_id"),
                    "step_name": request_metadata.get("step_name"),
                    **reasoning_effort_trace_payload(output.model_request),
                    "tools_allowed": output.model_request.tools_allowed,
                    "adapter": output.model_response.adapter if output.model_response is not None else None,
                    "mocked": output.model_response.mocked if output.model_response is not None else None,
                    "context_keys": request_metadata.get("context_keys", []),
                },
            )
        if output.model_response is not None:
            response_metadata = output.model_request.metadata if output.model_request is not None else {}
            self.trace_logger.record(
                run_id=run.id,
                agent_run_id=agent_run.id,
                event_type=TraceEventType.MODEL_ACTION,
                payload={
                    "action": "model_response",
                    "provider": output.model_response.raw_provider,
                    "model": output.model_request.model if output.model_request is not None else None,
                    "agent_id": response_metadata.get("agent_id"),
                    "step_name": response_metadata.get("step_name"),
                    **(
                        reasoning_effort_trace_payload(output.model_request)
                        if output.model_request is not None
                        else {}
                    ),
                    "adapter": output.model_response.adapter,
                    "mocked": output.model_response.mocked,
                    "usage": output.model_response.usage,
                    "latency_ms": output.model_response.latency_ms,
                    "finish_reason": output.model_response.finish_reason,
                    "output_length": len(output.model_response.text),
                },
                duration_ms=output.model_response.latency_ms,
            )

    def _select_final_artifact(
        self,
        pack: WorkflowPack,
        final_artifact_type: ArtifactType,
        artifacts_by_step: dict[str, list[Artifact]],
    ) -> Artifact:
        producer_steps = {
            step.name for step in pack.steps if step.produces_artifact_type == final_artifact_type.value
        }
        if not producer_steps and not any(step.produces_artifact_type for step in pack.steps):
            matching = [
                artifact
                for artifacts in artifacts_by_step.values()
                for artifact in artifacts
                if artifact.type == final_artifact_type
            ]
            if matching:
                return matching[-1]

        matching = [
            artifact
            for step in pack.steps
            if step.name in producer_steps
            for artifact in artifacts_by_step.get(step.name, [])
            if artifact.type == final_artifact_type
        ]
        if not matching:
            raise WorkflowRunnerError(f"Final artifact type not produced: {final_artifact_type.value}")
        return matching[-1]

    def _artifact_type(self, value: ArtifactType | str) -> ArtifactType:
        try:
            return value if isinstance(value, ArtifactType) else ArtifactType(value)
        except ValueError as exc:
            raise WorkflowRunnerError(f"Unsupported artifact type: {value}") from exc

    def _step_scoped_filename(
        self,
        run_id: str,
        agent_run: AgentRun,
        step: WorkflowStep,
        index: int,
        filename: str,
    ) -> str:
        raw_path = Path(filename)
        if raw_path.is_absolute() or raw_path.name != filename or filename in {"", ".", ".."}:
            raise WorkflowRunnerError("Artifact filename must be a simple relative file name.")
        attempts = [
            stored_agent_run
            for stored_agent_run in self.storage.list_agent_runs_for_run(run_id)
            if stored_agent_run.step_name == step.name
        ]
        attempt = next(
            (index for index, stored_agent_run in enumerate(attempts, start=1) if stored_agent_run.id == agent_run.id),
            len(attempts),
        )
        step_prefix = _slug(step.name)
        if attempt > 1:
            step_prefix = f"{step_prefix}-attempt-{attempt}"
        return f"{step_prefix}-{index + 1}-{filename}"

    def _fail_run(
        self,
        run: Run,
        agent_run: AgentRun | None,
        step: WorkflowStep | None,
        agent: AgentDefinition | None,
        exc: Exception,
        agent_session: AgentSession | None = None,
        runtime_job: RuntimeJob | None = None,
    ) -> Run:
        if runtime_job is not None and runtime_job.status not in {
            RuntimeJobStatus.COMPLETED,
            RuntimeJobStatus.FAILED,
            RuntimeJobStatus.REJECTED,
            RuntimeJobStatus.CANCELLED,
        }:
            failed_job = runtime_job.model_copy(
                update={
                    "status": RuntimeJobStatus.FAILED,
                    "updated_at": utc_now(),
                    "message": _safe_error_message(exc),
                }
            )
            self.storage.update_runtime_job(failed_job)

        if agent_session is not None and agent_session.status not in {
            AgentSessionStatus.COMPLETED,
            AgentSessionStatus.FAILED,
            AgentSessionStatus.REJECTED,
            AgentSessionStatus.CANCELLED,
        }:
            failed_session = agent_session.model_copy(
                update={"status": AgentSessionStatus.FAILED, "updated_at": utc_now()}
            )
            self.storage.update_agent_session(failed_session)

        if agent_run is not None and agent_run.status not in {
            AgentRunStatus.COMPLETED,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }:
            failed_agent_run = agent_run.model_copy(
                update={"status": AgentRunStatus.FAILED, "finished_at": utc_now()}
            )
            self.storage.update_agent_run(failed_agent_run)

        failed_run = run.model_copy(update={"status": RunStatus.FAILED, "finished_at": utc_now()})
        self.storage.update_run(failed_run)
        error_payload = {
            "step_name": step.name if step is not None else None,
            "agent_id": agent.id if agent is not None else None,
            "error_type": exc.__class__.__name__,
            "message": _safe_error_message(exc),
        }
        error_payload.update(model_runtime_error_payload(exc))
        self.trace_logger.record(
            run_id=run.id,
            agent_run_id=agent_run.id if agent_run is not None else None,
            event_type=TraceEventType.ERROR,
            payload=error_payload,
        )
        return failed_run


def utc_now() -> datetime:
    return datetime.now(UTC)


def _slug(value: str) -> str:
    allowed = [char.lower() if char.isalnum() or char in {"-", "_", "."} else "-" for char in value]
    slug = "".join(allowed).strip("-._")
    return slug or "step"


def _agent_identity(agent: AgentDefinition | None) -> str:
    if agent is None:
        return "none"
    return f"{agent.id}/{agent.pack_name}/{agent.role}"


def _last_ordered_agent_run(
    ordered_steps: list[WorkflowStep],
    agent_runs_by_step: dict[str, AgentRun],
) -> AgentRun | None:
    for step in reversed(ordered_steps):
        agent_run = agent_runs_by_step.get(step.name)
        if agent_run is not None:
            return agent_run
    return None


def _upstream_steps(step_name: str, steps: list[WorkflowStep]) -> set[str]:
    dependencies = {step.name: step.depends_on for step in steps}
    upstream: set[str] = set()
    pending = list(dependencies.get(step_name, []))
    while pending:
        dependency = pending.pop()
        if dependency in upstream:
            continue
        upstream.add(dependency)
        pending.extend(dependencies.get(dependency, []))
    return upstream


def _default_risk_notes_for_step(step: WorkflowStep) -> list[str]:
    if step.return_contract is not None and step.return_contract.require_risk_notes:
        return ["No additional risks reported by the deterministic mock executor."]
    return []


def _is_terminal_agent_run_status(status: AgentRunStatus) -> bool:
    return status in {
        AgentRunStatus.COMPLETED,
        AgentRunStatus.FAILED,
        AgentRunStatus.CANCELLED,
    }


def _is_terminal_agent_session_status(status: AgentSessionStatus) -> bool:
    return status in {
        AgentSessionStatus.COMPLETED,
        AgentSessionStatus.FAILED,
        AgentSessionStatus.REJECTED,
        AgentSessionStatus.CANCELLED,
    }


def _is_terminal_runtime_job_status(status: RuntimeJobStatus) -> bool:
    return status in {
        RuntimeJobStatus.COMPLETED,
        RuntimeJobStatus.FAILED,
        RuntimeJobStatus.REJECTED,
        RuntimeJobStatus.CANCELLED,
    }


def _redact_error_message(message: str) -> str:
    return redact_secret_like_text(message)


def _safe_error_message(exc: Exception) -> str:
    if isinstance(exc, ModelRuntimeError):
        return "Model runtime call failed. See structured error metadata."
    return _redact_error_message(str(exc))
