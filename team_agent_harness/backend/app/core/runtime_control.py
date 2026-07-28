from __future__ import annotations

from dataclasses import dataclass

from app.core.models import (
    AgentRunStatus,
    AgentSession,
    AgentSessionStatus,
    Run,
    RunStatus,
    RuntimeJob,
    RuntimeJobStatus,
    TraceEventType,
    utc_now,
)
from app.core.storage import SQLiteStorage
from app.core.trace import TraceLogger


class RuntimeControlError(RuntimeError):
    pass


class RuntimeControlConflict(RuntimeControlError):
    pass


@dataclass(frozen=True)
class RuntimeActionResult:
    run: Run
    session: AgentSession | None
    job: RuntimeJob


class RuntimeController:
    def __init__(self, storage: SQLiteStorage, trace_logger: TraceLogger) -> None:
        self.storage = storage
        self.trace_logger = trace_logger

    def get_action_state(self, run_id: str, job_id: str) -> RuntimeActionResult:
        run, session, job = self._load_for_action(run_id, job_id)
        return RuntimeActionResult(run=run, session=session, job=job)

    def approve(self, run_id: str, job_id: str) -> RuntimeActionResult:
        run, session, job = self._load_for_action(run_id, job_id)
        if run.status != RunStatus.WAITING:
            raise RuntimeControlConflict("Only waiting runs can approve local runtime jobs.")
        if job.status != RuntimeJobStatus.APPROVAL_REQUIRED:
            raise RuntimeControlConflict(f"Runtime job is not waiting for approval: {job.status.value}")
        if not job.approval_required:
            raise RuntimeControlConflict("Runtime job does not require approval.")
        self.ensure_current_resumable_job(RuntimeActionResult(run=run, session=session, job=job))

        now = utc_now()
        metadata = {**job.metadata, "external_runtime_started": False, "local_approval_intent": "approved"}
        approved_job = job.model_copy(
            update={
                "status": RuntimeJobStatus.APPROVED,
                "approved_at": now,
                "updated_at": now,
                "message": "Local approval intent recorded. External ACP remains not started.",
                "metadata": metadata,
            }
        )
        self.storage.update_runtime_job(approved_job)

        updated_session = None
        if session is not None:
            updated_session = session.model_copy(
                update={
                    "status": AgentSessionStatus.ACTIVE,
                    "updated_at": now,
                    "metadata": {
                        **session.metadata,
                        "external_runtime_started": False,
                        "local_approval_intent": "approved",
                    },
                }
            )
            self.storage.update_agent_session(updated_session)

        self._record_action(run_id, approved_job, "runtime_job_approved")
        return RuntimeActionResult(run=run, session=updated_session, job=approved_job)

    def reject(self, run_id: str, job_id: str) -> RuntimeActionResult:
        run, session, job = self._load_for_action(run_id, job_id)
        if run.status != RunStatus.WAITING:
            raise RuntimeControlConflict("Only waiting runs can reject local runtime jobs.")
        if job.status != RuntimeJobStatus.APPROVAL_REQUIRED:
            raise RuntimeControlConflict(f"Runtime job is not waiting for approval: {job.status.value}")
        self.ensure_current_resumable_job(RuntimeActionResult(run=run, session=session, job=job))
        result = self._terminal_action(
            run=run,
            session=session,
            job=job,
            job_status=RuntimeJobStatus.REJECTED,
            session_status=AgentSessionStatus.REJECTED,
            run_status=RunStatus.CANCELLED,
            action="runtime_job_rejected",
            message="Local approval intent rejected. External ACP was not started.",
        )
        return result

    def cancel(self, run_id: str, job_id: str) -> RuntimeActionResult:
        run, session, job = self._load_for_action(run_id, job_id)
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise RuntimeControlConflict(f"Terminal run cannot cancel runtime jobs: {run.status.value}")
        if job.status in {RuntimeJobStatus.COMPLETED, RuntimeJobStatus.FAILED, RuntimeJobStatus.REJECTED}:
            raise RuntimeControlConflict(f"Terminal runtime job cannot be cancelled: {job.status.value}")
        if job.status == RuntimeJobStatus.CANCELLED:
            return RuntimeActionResult(run=run, session=session, job=job)
        self.ensure_current_resumable_job(RuntimeActionResult(run=run, session=session, job=job))
        return self._terminal_action(
            run=run,
            session=session,
            job=job,
            job_status=RuntimeJobStatus.CANCELLED,
            session_status=AgentSessionStatus.CANCELLED,
            run_status=RunStatus.CANCELLED,
            action="runtime_job_cancelled",
            message="Local runtime job cancelled. External ACP was not started.",
        )

    def ensure_current_resumable_job(self, action_state: RuntimeActionResult) -> None:
        self._ensure_current_approval_job(action_state, allow_terminal_agent_run=False)

    def recover_terminal_intent(self, run_id: str, job_id: str) -> RuntimeActionResult:
        run, session, job = self._load_for_action(run_id, job_id)
        if run.status != RunStatus.WAITING:
            raise RuntimeControlConflict("Only waiting runs can recover terminal runtime intents.")
        if job.status not in {RuntimeJobStatus.REJECTED, RuntimeJobStatus.CANCELLED}:
            raise RuntimeControlConflict(f"Runtime job has no terminal intent to recover: {job.status.value}")
        current_terminal_job = self.terminal_intent_requiring_recovery(run)
        if current_terminal_job is None or current_terminal_job.id != job.id:
            raise RuntimeControlConflict("A newer runtime approval state supersedes this terminal intent.")
        self._ensure_current_approval_job(
            RuntimeActionResult(run=run, session=session, job=job),
            allow_terminal_agent_run=True,
        )

        if job.status == RuntimeJobStatus.REJECTED:
            return self._terminal_action(
                run=run,
                session=session,
                job=job,
                job_status=RuntimeJobStatus.REJECTED,
                session_status=AgentSessionStatus.REJECTED,
                run_status=RunStatus.CANCELLED,
                action="runtime_job_rejection_recovered",
                message="Recovered a persisted rejection intent left by an interrupted older process.",
            )
        return self._terminal_action(
            run=run,
            session=session,
            job=job,
            job_status=RuntimeJobStatus.CANCELLED,
            session_status=AgentSessionStatus.CANCELLED,
            run_status=RunStatus.CANCELLED,
            action="runtime_job_cancellation_recovered",
            message="Recovered a persisted cancellation intent left by an interrupted older process.",
        )

    def terminal_intent_requiring_recovery(self, run: Run) -> RuntimeJob | None:
        approval_jobs = [
            job for job in self.storage.list_runtime_jobs_for_run(run.id) if job.approval_required
        ]
        if not approval_jobs:
            return None
        _, latest_job = max(
            enumerate(approval_jobs),
            key=lambda item: (item[1].updated_at, item[1].created_at, item[0]),
        )
        if latest_job.status in {RuntimeJobStatus.REJECTED, RuntimeJobStatus.CANCELLED}:
            return latest_job
        return None

    def _ensure_current_approval_job(
        self,
        action_state: RuntimeActionResult,
        *,
        allow_terminal_agent_run: bool,
    ) -> None:
        run = action_state.run
        job = action_state.job
        if not job.approval_required:
            raise RuntimeControlConflict("Runtime job does not require approval.")

        agent_run = self.storage.get_agent_run(job.agent_run_id)
        if (
            agent_run is None
            or agent_run.run_id != run.id
            or agent_run.step_name != job.step_name
            or (
                _is_terminal_agent_run_status(agent_run.status)
                and (not allow_terminal_agent_run or agent_run.status != AgentRunStatus.CANCELLED)
            )
        ):
            raise RuntimeControlConflict("Runtime job is not attached to the current resumable agent attempt.")

        matching_agent_runs = [
            candidate
            for candidate in self.storage.list_agent_runs_for_run(run.id)
            if candidate.step_name == job.step_name
        ]
        if not matching_agent_runs or matching_agent_runs[-1].id != agent_run.id:
            raise RuntimeControlConflict("A newer agent attempt supersedes this runtime approval job.")

        matching_jobs = [
            candidate
            for candidate in self.storage.list_runtime_jobs_for_run(run.id)
            if candidate.step_name == job.step_name and candidate.approval_required
        ]
        if not matching_jobs or matching_jobs[-1].id != job.id:
            raise RuntimeControlConflict("A newer runtime approval job supersedes this job.")

    def _terminal_action(
        self,
        *,
        run: Run,
        session: AgentSession | None,
        job: RuntimeJob,
        job_status: RuntimeJobStatus,
        session_status: AgentSessionStatus,
        run_status: RunStatus,
        action: str,
        message: str,
    ) -> RuntimeActionResult:
        now = utc_now()
        updated_job = job.model_copy(
            update={
                "status": job_status,
                "updated_at": now,
                "message": message,
                "metadata": {
                    **job.metadata,
                    "external_runtime_started": False,
                    "local_approval_intent": job_status.value,
                },
            }
        )
        with self.storage.transaction():
            self.storage.update_runtime_job(updated_job)

            updated_session = None
            if session is not None:
                updated_session = session.model_copy(
                    update={
                        "status": session_status,
                        "updated_at": now,
                        "metadata": {
                            **session.metadata,
                            "external_runtime_started": False,
                            "local_approval_intent": job_status.value,
                        },
                    }
                )
                self.storage.update_agent_session(updated_session)

            agent_run = self.storage.get_agent_run(job.agent_run_id)
            if agent_run is not None and agent_run.status not in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
                AgentRunStatus.CANCELLED,
            }:
                self.storage.update_agent_run(
                    agent_run.model_copy(
                        update={
                            "status": AgentRunStatus.CANCELLED,
                            "finished_at": now,
                            "output_summary": message,
                        }
                    )
                )

            updated_run = run
            if run.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                updated_run = run.model_copy(
                    update={
                        "status": run_status,
                        "finished_at": now,
                        "current_step": job.step_name,
                    }
                )
                self.storage.update_run(updated_run)

            self.terminalize_open_runtime_state(run.id, reason=action)
            self._record_action(run.id, updated_job, action)
        return RuntimeActionResult(run=updated_run, session=updated_session, job=updated_job)

    def terminalize_open_runtime_state(self, run_id: str, *, reason: str) -> None:
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

    def _load_for_action(self, run_id: str, job_id: str) -> tuple[Run, AgentSession | None, RuntimeJob]:
        run = self.storage.get_run(run_id)
        if run is None:
            raise RuntimeControlError("Run not found")
        job = self.storage.get_runtime_job(job_id)
        if job is None:
            raise RuntimeControlError("Runtime job not found")
        if job.run_id != run_id:
            raise RuntimeControlError("Runtime job does not belong to run.")
        session = self.storage.get_agent_session(job.agent_session_id) if job.agent_session_id else None
        if session is not None:
            mismatches: list[str] = []
            if session.run_id != job.run_id:
                mismatches.append("run_id")
            if session.agent_run_id != job.agent_run_id:
                mismatches.append("agent_run_id")
            if session.step_name != job.step_name:
                mismatches.append("step_name")
            if session.runtime != job.runtime:
                mismatches.append("runtime")
            if mismatches:
                raise RuntimeControlConflict(f"Runtime job/session mismatch: {', '.join(mismatches)}")
        return run, session, job

    def _record_action(self, run_id: str, job: RuntimeJob, action: str) -> None:
        self.trace_logger.record(
            run_id=run_id,
            agent_run_id=job.agent_run_id,
            event_type=TraceEventType.RUNTIME_EVENT,
            payload={
                "action": action,
                "runtime": job.runtime,
                "runtime_job_id": job.id,
                "agent_session_id": job.agent_session_id,
                "job_status": job.status.value,
                "approval_required": job.approval_required,
                "external_runtime_started": False,
            },
        )


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
