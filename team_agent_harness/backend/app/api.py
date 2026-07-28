from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import Field

from app.core.artifacts import ArtifactStore
from app.core.browser_tools import (
    BrowserToolProvider,
    browser_fetch_access_enabled,
    browser_search_access_enabled,
    browser_tool_provider_catalog,
)
from app.core.context_injection import ContextBudgetExceeded, UNTRUSTED_EXTERNAL_DATA_SAFETY_NOTICE
from app.core.model_routing import (
    ModelRoutingConfig,
    ROUTING_CONFIG_ENV,
    apply_model_routing_config,
    load_model_routing_config,
)
from app.core.local_code_executor import LocalCodeExecutor
from app.core.model_runtime import (
    MockModelAdapter,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ModelRuntimeError,
    context_message_from_envelope,
    model_provider_catalog,
    model_request_from_agent,
    model_runtime_error_payload,
    reasoning_effort_trace_payload,
)
from app.core.models import (
    AgentDefinition,
    AgentRun,
    AgentSessionStatus,
    Artifact,
    ArtifactType,
    EvalResult,
    HarnessModel,
    Handoff,
    Run,
    RunLockStatus,
    RunQueueItemStatus,
    RunStatus,
    RuntimeJobStatus,
    Task,
    TraceEvent,
    TraceEventType,
)
from app.core.registry import AgentRegistry
from app.core.run_control import RunCoordinationConflict, RunCoordinator
from app.core.run_worker import RunWorker, RunWorkerError
from app.core.runner import AgentArtifactOutput, AgentExecutor, AgentStepOutput, WorkflowRunner, WorkflowRunnerError
from app.core.runtime_control import RuntimeControlConflict, RuntimeController, RuntimeControlError
from app.core.role_cards import (
    AgentBindingWrite,
    RoleCardError,
    RoleCardWrite,
    delete_agent_binding,
    delete_role_card,
    is_valid_role_card_id,
    list_role_cards,
    read_agent_bindings,
    read_role_card,
    upsert_agent_binding,
    write_role_card,
)
from app.core.skill_library import (
    AutoSkillRoute,
    SkillBindingWrite,
    SkillLibrary,
    SkillLibraryError,
    apply_auto_skill_routes_to_packs,
    apply_skill_bindings_to_packs,
    apply_task_skill_routes_to_agent,
    delete_skill_binding,
    load_skill_library,
    read_skill_bindings,
    upsert_skill_binding,
)
from app.core.storage import SQLiteStorage, StorageError
from app.core.task_intake import TaskIntakeRequest, analyze_task_intake
from app.core.trace import TraceLogger
from app.core.tool_gateway import ToolContext, ToolGatewayError, create_mock_gateway
from app.core.web_tools import (
    WebToolProvider,
    _validated_status_code,
    bounded_external_text,
    normalize_public_source_url,
    web_tool_provider_catalog,
)
from app.core.writeback import WritebackConflict, WritebackError, WritebackService
from app.packs.base import WorkflowPack, WorkflowStep
from app.packs.code_rd import get_code_rd_pack
from app.packs.code_rd_institutional import get_code_rd_institutional_pack
from app.packs.research import get_research_pack


_TASK_INPUT_SECRET_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
_TASK_INPUT_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\b[A-Za-z0-9_-]*(?:api[_-]?key|apikey|authorization|credential|password|private[_-]?key|secret|token)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bAuthorization\s*:\s*Bearer\s+\S+"),
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]+"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)
_WEB_TOOL_NAMES = {"web_search", "fetch_page", "browser_search", "browser_fetch"}
_MAX_TASK_PAYLOAD_CHARS = 100_000
_MAX_TASK_PAYLOAD_BYTES = 300_000
_MAX_TASK_CONTAINER_ITEMS = 128
_MAX_TASK_NESTING_DEPTH = 8
_RESEARCH_SEARCH_MAX_RESULTS = 3
_RESEARCH_FETCH_MAX_ITEMS = 2
_RESEARCH_FETCH_MAX_BYTES = 8 * 1024
_RESEARCH_EVIDENCE_SAFETY_NOTICE = UNTRUSTED_EXTERNAL_DATA_SAFETY_NOTICE


class TaskCreateRequest(HarnessModel):
    title: str = Field(min_length=1, max_length=500)
    goal: str = Field(min_length=1, max_length=50_000)
    workflow_pack: str = Field(default="auto", min_length=1, max_length=100)
    inputs: dict[str, Any] = Field(default_factory=dict, max_length=_MAX_TASK_CONTAINER_ITEMS)
    constraints: list[str] = Field(default_factory=list, max_length=64)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=64)
    created_by: str = Field(default="system", min_length=1, max_length=200)


class RunCreateRequest(HarnessModel):
    task_id: str = Field(min_length=1)
    confirm_real_models: bool = False
    confirm_real_web: bool = False
    background: bool = False


class RunRuntimeSessionResponse(HarnessModel):
    id: str
    run_id: str
    agent_run_id: str
    agent_id: str
    step_name: str
    runtime: str
    status: AgentSessionStatus
    resume_strategy: str
    requires_approval: bool
    created_at: str
    updated_at: str
    local_only: bool


class RunRuntimeJobMetadataResponse(HarnessModel):
    external_runtime_started: bool


class RunRuntimeJobResponse(HarnessModel):
    id: str
    run_id: str
    agent_run_id: str
    agent_session_id: str | None
    step_name: str
    runtime: str
    status: RuntimeJobStatus
    approval_required: bool
    approved_at: str | None
    created_at: str
    updated_at: str
    message: str
    metadata: RunRuntimeJobMetadataResponse
    local_only: bool


class RunQueueItemResponse(HarnessModel):
    id: str
    run_id: str
    action: str
    status: RunQueueItemStatus
    created_at: str
    updated_at: str
    message: str
    local_only: bool
    background_worker_started: bool


class RunLockResponse(HarnessModel):
    id: str
    run_id: str
    resource_type: str
    resource_label: str
    status: RunLockStatus
    acquired_at: str
    released_at: str | None
    local_only: bool


class RunDetailResponse(HarnessModel):
    run: Run
    task: Task
    agent_runs: list[AgentRun]
    handoffs: list[Handoff]
    trace: list[TraceEvent]
    artifacts: list[Artifact]
    eval_results: list[EvalResult]
    runtime_sessions: list[RunRuntimeSessionResponse]
    runtime_jobs: list[RunRuntimeJobResponse]
    queue_state: list[RunQueueItemResponse]
    lock_state: list[RunLockResponse]


class WritebackPreviewRequest(HarnessModel):
    patch_artifact_id: str = Field(min_length=1)


class WritebackApproveRequest(HarnessModel):
    patch_artifact_id: str = Field(min_length=1)
    writeback_id: str = Field(min_length=1)
    confirm_repository_path: str = Field(min_length=1)
    confirm_patch_hash: str = Field(min_length=1)
    expected_base_hashes: dict[str, str] = Field(default_factory=dict)


@dataclass
class HarnessAppState:
    storage: SQLiteStorage
    artifact_store: ArtifactStore
    trace_logger: TraceLogger
    packs: dict[str, WorkflowPack]
    executor_factory: Callable[[], AgentExecutor]
    model_routing: ModelRoutingConfig
    config_root: Path
    skill_library: SkillLibrary
    auto_skill_routes: list[AutoSkillRoute]
    skill_roots_override: list[str | Path] | None
    web_tool_provider: WebToolProvider
    browser_tool_provider: BrowserToolProvider
    custom_executor_factory: bool = False
    run_worker: RunWorker | None = None

    def start(self) -> None:
        _writeback_service(self).recover_pending_transactions()
        if self.run_worker is not None:
            self.run_worker.start()

    def close(self) -> None:
        worker_stopped = True
        if self.run_worker is not None:
            worker_stopped = self.run_worker.stop()
        if worker_stopped:
            self.storage.close()


def create_harness_state(
    db_path: str | Path,
    artifact_root: str | Path,
    executor_factory: Callable[[], AgentExecutor] | None = None,
    config_root: str | Path | None = None,
    web_tool_provider: WebToolProvider | None = None,
    browser_tool_provider: BrowserToolProvider | None = None,
    skill_roots_override: list[str | Path] | None = None,
) -> HarnessAppState:
    resolved_config_root = Path(config_root).expanduser().resolve() if config_root is not None else Path.cwd().resolve()
    storage = SQLiteStorage(db_path, check_same_thread=False)
    try:
        storage.connect()
        storage.init_schema()
        trace_logger = TraceLogger(storage)
        artifact_store = ArtifactStore(artifact_root, storage, trace_logger)
        model_routing = _load_model_routing_for_config_root(resolved_config_root)
        skill_library = load_skill_library(resolved_config_root, roots_override=skill_roots_override)
        packs, auto_skill_routes = _load_packs_with_skills(
            resolved_config_root,
            model_routing,
            skill_library,
        )
        web_tool_provider = web_tool_provider or WebToolProvider()
        browser_tool_provider = browser_tool_provider or BrowserToolProvider()
        state = HarnessAppState(
            storage=storage,
            artifact_store=artifact_store,
            trace_logger=trace_logger,
            packs=packs,
            executor_factory=executor_factory or (lambda: _default_executor_factory(state)),
            model_routing=model_routing,
            config_root=resolved_config_root,
            skill_library=skill_library,
            auto_skill_routes=auto_skill_routes,
            skill_roots_override=skill_roots_override,
            web_tool_provider=web_tool_provider,
            browser_tool_provider=browser_tool_provider,
            custom_executor_factory=executor_factory is not None,
        )
        state.run_worker = RunWorker(
            storage=storage,
            trace_logger=trace_logger,
            packs=packs,
            runner_factory=lambda: _workflow_runner(state),
        )
        return state
    except BaseException:
        storage.close()
        raise


def create_api_router(state: HarnessAppState) -> APIRouter:
    router = APIRouter()
    _register_task_routes(router, state)
    _register_run_routes(router, state)
    _register_runtime_job_routes(router, state)
    _register_writeback_routes(router, state)
    _register_catalog_routes(router, state)
    _register_role_card_routes(router, state)
    _register_skill_routes(router, state)
    return router


def _register_task_routes(router: APIRouter, state: HarnessAppState) -> None:
    @router.post("/tasks", status_code=201)
    def create_task(request: TaskCreateRequest) -> dict[str, Any]:
        workflow_pack = _resolve_task_workflow_pack(state, request)
        task_payload = request.model_dump(by_alias=False)
        task_payload["workflow_pack"] = workflow_pack
        try:
            _validate_task_payload_shape(task_payload)
            _reject_secret_like_task_payload(task_payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        task = Task(**task_payload)
        try:
            return state.storage.create_task(task).model_dump(mode="json")
        except StorageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/task-intake/analyze")
    def analyze_task(request: TaskIntakeRequest) -> dict[str, Any]:
        result = analyze_task_intake(request, available_packs=set(state.packs))
        return result.model_dump(mode="json")

    @router.get("/tasks")
    def list_tasks(
        limit: int = Query(default=500, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return [task.model_dump(mode="json") for task in state.storage.list_tasks(limit=limit, offset=offset)]

    @router.get("/tasks/{task_id}")
    def get_task(task_id: str) -> dict[str, Any]:
        task = state.storage.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return task.model_dump(mode="json")


def _register_run_routes(router: APIRouter, state: HarnessAppState) -> None:
    @router.post("/runs", status_code=201, response_model=Run)
    def create_run(request: RunCreateRequest) -> dict[str, Any]:
        task = state.storage.get_task(request.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        pack = _pack_or_404(state, task.workflow_pack)
        _require_run_confirmations(state, pack, request)
        run = Run(
            task_id=task.id,
            real_web_access_confirmed=request.confirm_real_web,
        )
        try:
            if request.background:
                if state.run_worker is None:
                    raise RunWorkerError("Background run worker is not configured.")
                return state.run_worker.submit(run).model_dump(mode="json")
            runner = _workflow_runner(state)
            return RunCoordinator(state.storage, state.trace_logger).start_new_run(
                run,
                lambda queued_run: runner.run(queued_run, pack),
            ).result.model_dump(mode="json")
        except WorkflowRunnerError as exc:
            failed_run = state.storage.get_run(run.id)
            if failed_run is not None:
                return failed_run.model_dump(mode="json")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RunCoordinationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RunWorkerError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except StorageError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    @router.get("/runs", response_model=list[Run])
    def list_runs(
        limit: int = Query(default=500, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        return [run.model_dump(mode="json") for run in state.storage.list_runs(limit=limit, offset=offset)]

    @router.get("/runs/{run_id}", response_model=Run)
    def get_run(run_id: str) -> dict[str, Any]:
        run = state.storage.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return run.model_dump(mode="json")

    @router.get("/runs/{run_id}/detail", response_model=RunDetailResponse)
    def get_run_detail(run_id: str) -> dict[str, Any]:
        run = _run_or_404(state, run_id)
        task = state.storage.get_task(run.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        return {
            "run": run.model_dump(mode="json"),
            "task": task.model_dump(mode="json"),
            "agent_runs": [
                agent_run.model_dump(mode="json") for agent_run in state.storage.list_agent_runs_for_run(run_id)
            ],
            "handoffs": [handoff.model_dump(mode="json") for handoff in state.storage.list_handoffs_for_run(run_id)],
            "trace": [event.model_dump(mode="json") for event in state.trace_logger.list_for_run(run_id)],
            "artifacts": [artifact.model_dump(mode="json") for artifact in state.storage.list_artifacts_for_run(run_id)],
            "eval_results": [result.model_dump(mode="json") for result in state.storage.list_eval_results_for_run(run_id)],
            "runtime_sessions": [
                _safe_runtime_session(session) for session in state.storage.list_agent_sessions_for_run(run_id)
            ],
            "runtime_jobs": [_safe_runtime_job(job) for job in state.storage.list_runtime_jobs_for_run(run_id)],
            "queue_state": _safe_queue_items(state, run_id),
            "lock_state": _safe_run_locks(state, run_id),
        }

    @router.get("/runs/{run_id}/trace")
    def list_trace(run_id: str) -> list[dict[str, Any]]:
        _run_or_404(state, run_id)
        return [event.model_dump(mode="json") for event in state.trace_logger.list_for_run(run_id)]

    @router.get("/runs/{run_id}/artifacts")
    def list_run_artifacts(run_id: str) -> list[dict[str, Any]]:
        _run_or_404(state, run_id)
        return [artifact.model_dump(mode="json") for artifact in state.storage.list_artifacts_for_run(run_id)]

    @router.get("/runs/{run_id}/agent-runs")
    def list_run_agent_runs(run_id: str) -> list[dict[str, Any]]:
        _run_or_404(state, run_id)
        return [agent_run.model_dump(mode="json") for agent_run in state.storage.list_agent_runs_for_run(run_id)]

    @router.get("/runs/{run_id}/handoffs")
    def list_run_handoffs(run_id: str) -> list[dict[str, Any]]:
        _run_or_404(state, run_id)
        return [handoff.model_dump(mode="json") for handoff in state.storage.list_handoffs_for_run(run_id)]

    @router.get("/runs/{run_id}/eval-results")
    def list_run_eval_results(run_id: str) -> list[dict[str, Any]]:
        _run_or_404(state, run_id)
        return [result.model_dump(mode="json") for result in state.storage.list_eval_results_for_run(run_id)]

    @router.get("/runs/{run_id}/runtime-sessions")
    def list_run_runtime_sessions(run_id: str) -> list[dict[str, Any]]:
        _run_or_404(state, run_id)
        return [_safe_runtime_session(session) for session in state.storage.list_agent_sessions_for_run(run_id)]

    @router.get("/runs/{run_id}/runtime-jobs")
    def list_run_runtime_jobs(run_id: str) -> list[dict[str, Any]]:
        _run_or_404(state, run_id)
        return [_safe_runtime_job(job) for job in state.storage.list_runtime_jobs_for_run(run_id)]

    @router.get("/runs/{run_id}/queue-state")
    def list_run_queue_state(run_id: str) -> list[dict[str, Any]]:
        _run_or_404(state, run_id)
        return _safe_queue_items(state, run_id)

    @router.get("/runs/{run_id}/lock-state")
    def list_run_lock_state(run_id: str) -> list[dict[str, Any]]:
        _run_or_404(state, run_id)
        return _safe_run_locks(state, run_id)


def _workflow_runner(state: HarnessAppState) -> WorkflowRunner:
    return WorkflowRunner(
        storage=state.storage,
        registry=AgentRegistry(),
        artifact_store=state.artifact_store,
        trace_logger=state.trace_logger,
        executor=state.executor_factory(),
    )


def _register_runtime_job_routes(router: APIRouter, state: HarnessAppState) -> None:
    @router.post("/runs/{run_id}/runtime-jobs/{job_id}/approve")
    def approve_runtime_job(
        run_id: str,
        job_id: str,
        response: Response,
        background: bool = Query(default=False),
    ) -> dict[str, Any]:
        _run_or_404(state, run_id)
        try:
            if background:
                if state.run_worker is None:
                    raise RunWorkerError("Background run worker is not configured.")
                submission = state.run_worker.approve_and_resume(run_id, job_id)
                if submission.queue_item is not None:
                    response.status_code = 202
                persisted_run = state.storage.get_run(run_id) or submission.run
                persisted_job = state.storage.get_runtime_job(job_id) or submission.job
                persisted_session = (
                    state.storage.get_agent_session(submission.session.id)
                    if submission.session is not None
                    else None
                )
                return {
                    "run": persisted_run.model_dump(mode="json"),
                    "runtime_job": _safe_runtime_job(persisted_job),
                    "runtime_session": (
                        _safe_runtime_session(persisted_session)
                        if persisted_session is not None
                        else None
                    ),
                }
            result = RunCoordinator(state.storage, state.trace_logger).execute(
                run_id,
                "approve_runtime_job",
                lambda: _approve_and_resume_runtime_job(state, run_id, job_id),
            ).result
            runtime_job = state.storage.get_runtime_job(job_id)
            runtime_session = (
                state.storage.get_agent_session(result["session"].id)
                if result["session"] is not None
                else None
            )
            return {
                "run": result["run"].model_dump(mode="json"),
                "runtime_job": _safe_runtime_job(runtime_job) if runtime_job is not None else None,
                "runtime_session": _safe_runtime_session(runtime_session) if runtime_session is not None else None,
            }
        except RunCoordinationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeControlConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RunWorkerError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except (RuntimeControlError, WorkflowRunnerError, StorageError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/runs/{run_id}/runtime-jobs/{job_id}/reject")
    def reject_runtime_job(run_id: str, job_id: str) -> dict[str, Any]:
        _run_or_404(state, run_id)
        try:
            result = RunCoordinator(state.storage, state.trace_logger).execute(
                run_id,
                "reject_runtime_job",
                lambda: RuntimeController(state.storage, state.trace_logger).reject(run_id, job_id),
            ).result
            return _runtime_action_response(result)
        except RunCoordinationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeControlConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (RuntimeControlError, StorageError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/runs/{run_id}/runtime-jobs/{job_id}/cancel")
    def cancel_runtime_job(run_id: str, job_id: str) -> dict[str, Any]:
        _run_or_404(state, run_id)
        try:
            result = RunCoordinator(state.storage, state.trace_logger).execute(
                run_id,
                "cancel_runtime_job",
                lambda: RuntimeController(state.storage, state.trace_logger).cancel(run_id, job_id),
            ).result
            return _runtime_action_response(result)
        except RunCoordinationConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeControlConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (RuntimeControlError, StorageError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _register_writeback_routes(router: APIRouter, state: HarnessAppState) -> None:
    @router.post("/runs/{run_id}/writeback/preview")
    def preview_writeback(run_id: str, request: WritebackPreviewRequest) -> dict[str, Any]:
        run = _run_or_404(state, run_id)
        task = _task_or_404(state, run.task_id)
        artifact = _artifact_or_404(state, request.patch_artifact_id)
        try:
            return _writeback_service(state).preview(run=run, task=task, artifact=artifact)
        except WritebackConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WritebackError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/runs/{run_id}/writeback/approve")
    def approve_writeback(run_id: str, request: WritebackApproveRequest) -> dict[str, Any]:
        run = _run_or_404(state, run_id)
        task = _task_or_404(state, run.task_id)
        artifact = _artifact_or_404(state, request.patch_artifact_id)
        try:
            return _writeback_service(state).approve(
                run=run,
                task=task,
                artifact=artifact,
                writeback_id=request.writeback_id,
                confirm_repository_path=request.confirm_repository_path,
                confirm_patch_hash=request.confirm_patch_hash,
                expected_base_hashes=request.expected_base_hashes,
            )
        except WritebackConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except WritebackError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    @router.get("/artifacts/{artifact_id}")
    def get_artifact(artifact_id: str) -> dict[str, Any]:
        artifact = state.storage.get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Artifact not found")
        return {
            "artifact": artifact.model_dump(mode="json"),
            "content": state.artifact_store.read_text(artifact),
        }


def _writeback_service(state: HarnessAppState) -> WritebackService:
    return WritebackService(
        artifact_store=state.artifact_store,
        trace_logger=state.trace_logger,
        workspace_root=state.config_root / "output" / "writeback_workspaces",
    )


def _register_catalog_routes(router: APIRouter, state: HarnessAppState) -> None:
    @router.get("/workflow-packs")
    def list_workflow_packs() -> list[dict[str, Any]]:
        return [_safe_workflow_pack(pack) for pack in state.packs.values()]

    @router.get("/workflow-packs/{pack_name}")
    def get_workflow_pack(pack_name: str) -> dict[str, Any]:
        return _safe_workflow_pack(_pack_or_404(state, pack_name))

    @router.get("/model-providers")
    def list_model_providers() -> list[dict[str, Any]]:
        return [provider.__dict__ for provider in model_provider_catalog()]

    @router.get("/tool-providers")
    def list_tool_providers() -> list[dict[str, Any]]:
        providers = web_tool_provider_catalog() + browser_tool_provider_catalog(state.browser_tool_provider)
        return [provider.model_dump(mode="json") for provider in providers]

    @router.get("/agents")
    def list_agents(pack_name: str | None = None) -> list[dict[str, Any]]:
        if pack_name is not None:
            _pack_or_404(state, pack_name)
        agents: list[AgentDefinition] = []
        for pack in state.packs.values():
            if pack_name is None or pack.name == pack_name:
                agents.extend(pack.agents)
        return [_safe_agent_definition(agent) for agent in agents]


def _register_role_card_routes(router: APIRouter, state: HarnessAppState) -> None:
    @router.get("/role-cards")
    def get_role_cards() -> list[dict[str, Any]]:
        try:
            return [card.model_dump(mode="json") for card in list_role_cards(state.config_root, include_content=False)]
        except RoleCardError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/role-cards/{role_card_id}")
    def get_role_card(role_card_id: str) -> dict[str, Any]:
        try:
            if not is_valid_role_card_id(role_card_id):
                raise HTTPException(status_code=422, detail="Invalid role card id")
            return read_role_card(state.config_root, role_card_id, include_content=True).model_dump(mode="json")
        except RoleCardError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.put("/role-cards/{role_card_id}")
    def save_role_card(role_card_id: str, request: RoleCardWrite) -> dict[str, Any]:
        try:
            if not is_valid_role_card_id(role_card_id):
                raise HTTPException(status_code=422, detail="Invalid role card id")
            return write_role_card(state.config_root, role_card_id, request).model_dump(mode="json")
        except RoleCardError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/role-cards/{role_card_id}")
    def remove_role_card(role_card_id: str) -> dict[str, Any]:
        try:
            if not is_valid_role_card_id(role_card_id):
                raise HTTPException(status_code=422, detail="Invalid role card id")
            delete_role_card(state.config_root, role_card_id)
            return {"status": "deleted", "restart_required": True}
        except RoleCardError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.get("/agent-bindings")
    def get_agent_bindings() -> list[dict[str, Any]]:
        try:
            return [binding.model_dump(mode="json") for binding in read_agent_bindings(state.config_root)]
        except RoleCardError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/agent-bindings/{agent_id}")
    def save_agent_binding(agent_id: str, request: AgentBindingWrite) -> dict[str, Any]:
        _agent_or_404(state, agent_id)
        try:
            return upsert_agent_binding(state.config_root, agent_id, request).model_dump(mode="json")
        except RoleCardError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/agent-bindings/{agent_id}")
    def remove_agent_binding(agent_id: str) -> dict[str, Any]:
        _agent_or_404(state, agent_id)
        try:
            delete_agent_binding(state.config_root, agent_id)
            return {"status": "deleted", "restart_required": True}
        except RoleCardError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _register_skill_routes(router: APIRouter, state: HarnessAppState) -> None:
    @router.get("/skills")
    def get_skills() -> list[dict[str, Any]]:
        return [skill.model_dump(mode="json") for skill in state.skill_library.list_skills()]

    @router.get("/skills/{skill_id}")
    def get_skill(skill_id: str) -> dict[str, Any]:
        try:
            return state.skill_library.get_skill(skill_id).model_dump(mode="json")
        except SkillLibraryError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @router.post("/skills/refresh")
    def refresh_skills() -> dict[str, Any]:
        try:
            state.skill_library = load_skill_library(state.config_root, roots_override=state.skill_roots_override)
            state.packs, state.auto_skill_routes = _load_packs_with_skills(
                state.config_root,
                state.model_routing,
                state.skill_library,
            )
            return {
                "status": "refreshed",
                "count": len(state.skill_library.skills),
                "auto_skill_route_count": len(state.auto_skill_routes),
                "restart_required": state.custom_executor_factory,
            }
        except SkillLibraryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/skill-auto-routes")
    def get_skill_auto_routes() -> list[dict[str, Any]]:
        return [route.model_dump(mode="json") for route in state.auto_skill_routes]

    @router.get("/skill-bindings")
    def get_skill_bindings() -> list[dict[str, Any]]:
        try:
            return [binding.model_dump(mode="json") for binding in read_skill_bindings(state.config_root)]
        except SkillLibraryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.put("/skill-bindings/{agent_id}")
    def save_skill_binding(agent_id: str, request: SkillBindingWrite) -> dict[str, Any]:
        _agent_or_404(state, agent_id)
        try:
            known_agent_ids = {agent.id for pack in state.packs.values() for agent in pack.agents}
            return upsert_skill_binding(
                state.config_root,
                agent_id,
                request,
                known_agent_ids=known_agent_ids,
                library=state.skill_library,
            ).model_dump(mode="json")
        except SkillLibraryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.delete("/skill-bindings/{agent_id}")
    def remove_skill_binding(agent_id: str) -> dict[str, Any]:
        _agent_or_404(state, agent_id)
        try:
            delete_skill_binding(state.config_root, agent_id)
            return {"status": "deleted", "restart_required": True}
        except SkillLibraryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


def _load_model_routing_for_config_root(config_root: Path) -> ModelRoutingConfig:
    if os.environ.get(ROUTING_CONFIG_ENV):
        return load_model_routing_config()
    return ModelRoutingConfig()


def _resolve_task_workflow_pack(state: HarnessAppState, request: TaskCreateRequest) -> str:
    requested_pack = request.workflow_pack.strip()
    if requested_pack.lower() == "auto":
        intake = analyze_task_intake(
            TaskIntakeRequest(
                title=request.title,
                goal=request.goal,
                inputs=request.inputs,
                constraints=request.constraints,
                acceptance_criteria=request.acceptance_criteria,
            ),
            available_packs=set(state.packs),
        )
        return _pack_or_404(state, intake.recommended_pack).name
    return _pack_or_404(state, requested_pack).name


class PackMappedExecutor:
    def __init__(
        self,
        model_gateway: ModelGateway | None = None,
        artifact_store: ArtifactStore | None = None,
        trace_logger: TraceLogger | None = None,
        web_tool_provider: WebToolProvider | None = None,
        browser_tool_provider: BrowserToolProvider | None = None,
        skill_library: SkillLibrary | None = None,
    ) -> None:
        self.model_gateway = model_gateway or ModelGateway()
        self.local_code_executor = LocalCodeExecutor(model_gateway=self.model_gateway)
        self.artifact_store = artifact_store
        self.trace_logger = trace_logger
        self.web_tool_provider = web_tool_provider or WebToolProvider()
        self.browser_tool_provider = browser_tool_provider or BrowserToolProvider()
        self.skill_library = skill_library

    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        agent = self._task_routed_agent(task=task, run=run, step=step, agent=agent, context=context)
        if self.local_code_executor.supports(task, step):
            return self.local_code_executor.execute(
                task=task,
                run=run,
                step=step,
                agent=agent,
                context=context,
            )

        if task.workflow_pack == "research" and self._supports_research_tools(step):
            return self._execute_research_tool_step(
                task=task,
                run=run,
                step=step,
                agent=agent,
                context=context,
            )

        artifact_type = _artifact_type_for_step(task.workflow_pack, step)
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
        self._record_model_request_started(run=run, model_request=model_request)
        model_request, model_response = self._complete_model_request(
            task=task,
            run=run,
            step=step,
            agent=agent,
            model_request=model_request,
        )
        return AgentStepOutput(
            summary=model_response.text.splitlines()[0],
            artifacts=[
                AgentArtifactOutput(
                    type=artifact_type,
                    filename=f"{step.name}.md",
                    content=f"# {step.name}\n\n{model_response.text}\nRun: {run.id}\n",
                )
            ],
            risk_notes=_default_risk_notes_for_step(step),
            model_request=model_request,
            model_response=model_response,
        )

    def _complete_model_request(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        model_request: ModelRequest,
    ) -> tuple[ModelRequest, ModelResponse]:
        try:
            return model_request, self.model_gateway.complete(model_request)
        except ModelRuntimeError as exc:
            if not self._can_fallback_to_mock(task=task, step=step, model_request=model_request):
                raise
            error_payload = model_runtime_error_payload(exc)
            fallback_request = replace(
                model_request,
                provider="mock",
                model="mock-model",
                metadata={
                    **model_request.metadata,
                    "fallback_from_provider": model_request.provider,
                    "fallback_from_model": model_request.model,
                },
            )
            fallback_response = MockModelAdapter().complete(fallback_request)
            fallback_response = replace(
                fallback_response,
                text=(
                    "Fallback: real provider failed; mock response used.\n"
                    f"Failed provider: {model_request.provider}\n"
                    f"Failed model: {model_request.model}\n\n"
                    f"{fallback_response.text}"
                ),
            )
            self._record_model_provider_fallback(
                run=run,
                step=step,
                agent=agent,
                failed_request=model_request,
                fallback_request=fallback_request,
                error_payload=error_payload,
            )
            return fallback_request, fallback_response

    def _record_model_request_started(self, *, run: Run, model_request: ModelRequest) -> None:
        if self.trace_logger is None:
            return
        metadata = model_request.metadata
        self.trace_logger.record(
            run_id=run.id,
            agent_run_id=str(metadata.get("agent_run_id")) if metadata.get("agent_run_id") else None,
            event_type=TraceEventType.MODEL_ACTION,
            payload={
                "action": "model_request_started",
                "provider": model_request.provider,
                "model": model_request.model,
                "agent_id": metadata.get("agent_id"),
                "step_name": metadata.get("step_name"),
                **reasoning_effort_trace_payload(model_request),
                "tools_allowed": model_request.tools_allowed,
                "context_keys": metadata.get("context_keys", []),
            },
        )

    def _can_fallback_to_mock(
        self,
        *,
        task: Task,
        step: WorkflowStep,
        model_request: ModelRequest,
    ) -> bool:
        return (
            os.environ.get("TEAM_AGENT_ALLOW_MODEL_FALLBACK_TO_MOCK") == "1"
            and task.workflow_pack == "code_rd_institutional"
            and step.runtime == "session"
            and not step.session_policy.requires_approval
            and model_request.provider != "mock"
        )

    def _record_model_provider_fallback(
        self,
        *,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        failed_request: ModelRequest,
        fallback_request: ModelRequest,
        error_payload: dict[str, Any],
    ) -> None:
        if self.trace_logger is None:
            return
        payload: dict[str, Any] = {
            "action": "model_provider_fallback",
            "step_name": step.name,
            "agent_id": agent.id,
            "failed_provider": error_payload.get("provider", failed_request.provider),
            "failed_model": error_payload.get("model", failed_request.model),
            "failed_adapter": error_payload.get("adapter"),
            "fallback_provider": fallback_request.provider,
            "fallback_model": fallback_request.model,
            "error_class": error_payload.get("error_class"),
            "error_summary": error_payload.get("error_summary"),
            "elapsed_ms": error_payload.get("elapsed_ms"),
        }
        self.trace_logger.record(
            run_id=run.id,
            agent_run_id=str(failed_request.metadata.get("agent_run_id"))
            if failed_request.metadata.get("agent_run_id")
            else None,
            event_type=TraceEventType.WORKFLOW_EVENT,
            payload={key: value for key, value in payload.items() if value is not None},
        )

    def _task_routed_agent(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentDefinition:
        if self.skill_library is None:
            return agent
        routed_agent, _routes = apply_task_skill_routes_to_agent(
            agent,
            task=task,
            step=step,
            library=self.skill_library,
        )
        if _routes and self.trace_logger is not None:
            task_skill_routes = routed_agent.runtime_limits.get("task_skill_routes", [])
            self.trace_logger.record(
                run_id=run.id,
                agent_run_id=str(context.get("agent_run_id")) if context.get("agent_run_id") else None,
                event_type=TraceEventType.WORKFLOW_EVENT,
                payload={
                    "action": "task_skill_routes_applied",
                    "step_name": step.name,
                    "agent_id": agent.id,
                    "routes": task_skill_routes,
                    "skill_ids": [route.skill_id for route in _routes],
                    "injected_bytes": routed_agent.runtime_limits.get("task_skill_injected_bytes", 0),
                },
            )
        return routed_agent

    def _supports_research_tools(self, step: WorkflowStep) -> bool:
        return step.name in {"collect_sources", "read_sources", "verify_claims"}

    def _execute_research_tool_step(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        if self.trace_logger is None:
            raise WorkflowRunnerError(
                f"Research tool step {step.name} requires a trace logger; refusing to skip tool execution."
            )
        gateway = create_mock_gateway(
            self.trace_logger,
            ".",
            artifact_store=self.artifact_store,
            web_tool_provider=self.web_tool_provider,
            browser_tool_provider=self.browser_tool_provider,
        )
        tool_context = ToolContext(
            run_id=run.id,
            agent_run_id=str(context["agent_run_id"]),
            agent=agent,
            allowed_tools=frozenset(step.allowed_tools),
            real_web_access_confirmed=run.real_web_access_confirmed,
        )
        artifact_type = _artifact_type_for_step(task.workflow_pack, step)
        search_result: dict[str, Any] | None = None
        fetched: list[dict[str, Any]] | None = None
        if step.name == "collect_sources":
            query = str(task.inputs.get("topic") or task.goal or task.title)
            search_result = self._call_research_search_tool(
                gateway,
                tool_context,
                {"query": query, "max_results": _RESEARCH_SEARCH_MAX_RESULTS},
            )
            research_evidence = _bounded_research_search_evidence(search_result)
            if not research_evidence["items"]:
                raise WorkflowRunnerError("Research search returned no validated public source URLs.")
        else:
            urls = _source_refs_from_context(context)
            if not urls:
                raise WorkflowRunnerError(
                    f"Research tool step {step.name} requires a validated public source URL."
                )
            fetched = [
                self._call_research_fetch_tool(
                    gateway,
                    tool_context,
                    {"url": url, "max_bytes": _RESEARCH_FETCH_MAX_BYTES},
                )
                for url in urls[:_RESEARCH_FETCH_MAX_ITEMS]
            ]
            research_evidence = _bounded_research_fetch_evidence(fetched)
            if not research_evidence["items"]:
                raise WorkflowRunnerError("Research fetch returned no validated public source URLs.")

        model_context = _research_model_context(context, step, research_evidence)
        model_request = model_request_from_agent(
            task_title=task.title,
            task_goal=task.goal,
            step_name=step.name,
            agent_id=agent.id,
            agent_role=agent.role,
            system_prompt=agent.system_prompt,
            model_config=agent.model_settings,
            allowed_tools=step.allowed_tools,
            context=model_context,
        )
        self._record_model_request_started(run=run, model_request=model_request)
        model_request, model_response = self._complete_model_request(
            task=task,
            run=run,
            step=step,
            agent=agent,
            model_request=model_request,
        )
        if step.name == "collect_sources":
            assert search_result is not None
            content, source_refs = _research_source_summary(step.name, model_response.text, research_evidence)
        else:
            assert fetched is not None
            content, source_refs = _research_fetch_summary(step.name, model_response.text, research_evidence)
        return AgentStepOutput(
            summary=model_response.text.splitlines()[0],
            artifacts=[
                AgentArtifactOutput(
                    type=artifact_type,
                    filename=f"{step.name}.md",
                    content=content,
                    source_refs=source_refs,
                )
            ],
            risk_notes=_default_risk_notes_for_step(step),
            model_request=model_request,
            model_response=model_response,
        )

    def _call_research_search_tool(
        self,
        gateway: Any,
        tool_context: ToolContext,
        payload: dict[str, Any],
    ) -> Any:
        browser_failed = False
        browser_requested = _confirmed_real_browser_requested(
            self.browser_tool_provider,
            tool_context,
        )
        browser_available = (
            tool_context.real_web_access_confirmed
            and browser_search_access_enabled(self.browser_tool_provider)
        )
        if browser_available:
            try:
                return gateway.call_tool(tool_context, "browser_search", payload)
            except ToolGatewayError:
                if not self.web_tool_provider.real_search_access_available():
                    raise
                browser_failed = True
        elif browser_requested:
            if not self.web_tool_provider.real_search_access_available():
                raise WorkflowRunnerError(
                    "Research browser_search fallback requires confirmed real Tavily access."
                )
            browser_failed = True
        result = gateway.call_tool(tool_context, "web_search", payload)
        if browser_failed:
            _require_real_tavily_fallback(result, "web_search")
        return result

    def _call_research_fetch_tool(
        self,
        gateway: Any,
        tool_context: ToolContext,
        payload: dict[str, Any],
    ) -> Any:
        browser_failed = False
        browser_requested = _confirmed_real_browser_requested(
            self.browser_tool_provider,
            tool_context,
        )
        browser_available = (
            tool_context.real_web_access_confirmed
            and browser_fetch_access_enabled(self.browser_tool_provider)
        )
        if browser_available:
            try:
                return gateway.call_tool(tool_context, "browser_fetch", payload)
            except ToolGatewayError:
                if not self.web_tool_provider.real_fetch_access_available():
                    raise
                browser_failed = True
        elif browser_requested:
            if not self.web_tool_provider.real_fetch_access_available():
                raise WorkflowRunnerError(
                    "Research browser_fetch fallback requires confirmed real Tavily access."
                )
            browser_failed = True
        result = gateway.call_tool(tool_context, "fetch_page", payload)
        if browser_failed:
            _require_real_tavily_fallback(result, "fetch_page")
        return result


def _base_packs() -> dict[str, WorkflowPack]:
    return {
        pack.name: pack
        for pack in [
            get_code_rd_pack(),
            get_code_rd_institutional_pack(),
            get_research_pack(),
        ]
    }


def _load_packs_with_skills(
    config_root: Path,
    model_routing: ModelRoutingConfig,
    skill_library: SkillLibrary,
) -> tuple[dict[str, WorkflowPack], list[AutoSkillRoute]]:
    packs = apply_model_routing_config(_base_packs(), model_routing)
    packs = apply_skill_bindings_to_packs(packs, read_skill_bindings(config_root), skill_library)
    return apply_auto_skill_routes_to_packs(packs, skill_library)


def _default_executor_factory(state: HarnessAppState) -> PackMappedExecutor:
    return PackMappedExecutor(
        model_gateway=ModelGateway(),
        artifact_store=state.artifact_store,
        trace_logger=state.trace_logger,
        web_tool_provider=state.web_tool_provider,
        browser_tool_provider=state.browser_tool_provider,
        skill_library=state.skill_library,
    )


def _pack_or_404(state: HarnessAppState, pack_name: str) -> WorkflowPack:
    pack = state.packs.get(pack_name)
    if pack is None:
        raise HTTPException(status_code=404, detail="Workflow pack not found")
    return pack


def _reject_secret_like_task_payload(value: Any, path: str = "task") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(marker in normalized for marker in _TASK_INPUT_SECRET_KEY_MARKERS):
                raise ValueError(f"Sensitive task content is not allowed at {path}.{key}.")
            _reject_secret_like_task_payload(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_like_task_payload(item, f"{path}[{index}]")
        return
    if isinstance(value, str) and _looks_like_secret_value(value):
        raise ValueError(f"Sensitive task content is not allowed at {path}.")


def _validate_task_payload_shape(value: Any) -> None:
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(serialized) > _MAX_TASK_PAYLOAD_CHARS:
        raise ValueError(f"Task payload exceeds {_MAX_TASK_PAYLOAD_CHARS} characters.")
    try:
        encoded = serialized.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Task payload contains invalid Unicode.") from exc
    if len(encoded) > _MAX_TASK_PAYLOAD_BYTES:
        raise ValueError(f"Task payload exceeds {_MAX_TASK_PAYLOAD_BYTES} bytes.")
    _validate_task_value(value, path="task", depth=0)


def _validate_task_value(value: Any, *, path: str, depth: int) -> None:
    if depth > _MAX_TASK_NESTING_DEPTH:
        raise ValueError(f"Task payload nesting exceeds {_MAX_TASK_NESTING_DEPTH} levels at {path}.")
    if isinstance(value, dict):
        if len(value) > _MAX_TASK_CONTAINER_ITEMS:
            raise ValueError(f"Task payload contains too many fields at {path}.")
        for key, item in value.items():
            _validate_task_value(item, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > _MAX_TASK_CONTAINER_ITEMS:
            raise ValueError(f"Task payload contains too many items at {path}.")
        for index, item in enumerate(value):
            _validate_task_value(item, path=f"{path}[{index}]", depth=depth + 1)


def _looks_like_secret_value(value: str) -> bool:
    return any(pattern.search(value) for pattern in _TASK_INPUT_SECRET_VALUE_PATTERNS)


def _require_run_confirmations(
    state: HarnessAppState,
    pack: WorkflowPack,
    request: RunCreateRequest,
) -> None:
    if _pack_has_enabled_real_model_route(pack) and not request.confirm_real_models:
        raise HTTPException(
            status_code=400,
            detail="confirm_real_models=true is required because this workflow has enabled real model routes.",
        )
    if _pack_has_enabled_real_web_route(state, pack) and not request.confirm_real_web:
        raise HTTPException(
            status_code=400,
            detail="confirm_real_web=true is required because this workflow can call enabled real web/browser tools.",
        )


def _pack_has_enabled_real_model_route(pack: WorkflowPack) -> bool:
    enabled_real_providers = {
        provider.name
        for provider in model_provider_catalog()
        if provider.enabled and provider.real_calls
    }
    if not enabled_real_providers:
        return False
    return any(
        str(agent.model_settings.get("provider", "mock")) in enabled_real_providers
        for agent in pack.agents
    )


def _pack_has_enabled_real_web_route(state: HarnessAppState, pack: WorkflowPack) -> bool:
    enabled_real_tools = {
        provider.name
        for provider in web_tool_provider_catalog() + browser_tool_provider_catalog(state.browser_tool_provider)
        if provider.enabled and provider.real_calls
    }
    if not enabled_real_tools:
        return False
    return any(
        tool in enabled_real_tools
        for step in pack.steps
        for tool in step.allowed_tools
        if tool in _WEB_TOOL_NAMES
    )


def _agent_or_404(state: HarnessAppState, agent_id: str) -> AgentDefinition:
    for pack in state.packs.values():
        for agent in pack.agents:
            if agent.id == agent_id:
                return agent
    raise HTTPException(status_code=404, detail="Agent not found")


def _run_or_404(state: HarnessAppState, run_id: str) -> Run:
    run = state.storage.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


def _task_or_404(state: HarnessAppState, task_id: str) -> Task:
    task = state.storage.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


def _artifact_or_404(state: HarnessAppState, artifact_id: str):
    artifact = state.storage.get_artifact(artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact


def _runtime_action_response(result: Any) -> dict[str, Any]:
    return {
        "run": result.run.model_dump(mode="json"),
        "runtime_job": _safe_runtime_job(result.job),
        "runtime_session": _safe_runtime_session(result.session) if result.session is not None else None,
    }


def _approve_and_resume_runtime_job(state: HarnessAppState, run_id: str, job_id: str) -> dict[str, Any]:
    run = _run_or_404(state, run_id)
    task = state.storage.get_task(run.task_id)
    if task is None:
        raise WorkflowRunnerError(f"Task not found: {run.task_id}")
    pack = _pack_or_404(state, task.workflow_pack)
    persisted_job = state.storage.get_runtime_job(job_id)
    if persisted_job is None:
        raise RuntimeControlError("Runtime job not found")
    if persisted_job.run_id != run_id:
        raise RuntimeControlError("Runtime job does not belong to run.")
    if persisted_job.status == RuntimeJobStatus.APPROVED:
        if run.status != RunStatus.WAITING:
            raise RuntimeControlConflict("Only waiting runs can resume approved local runtime jobs.")
        controller = RuntimeController(state.storage, state.trace_logger)
        action_state = controller.get_action_state(run_id, job_id)
        controller.ensure_current_resumable_job(action_state)
        approved_session = action_state.session
    else:
        result = RuntimeController(state.storage, state.trace_logger).approve(run_id, job_id)
        approved_session = result.session
    resumed_run = WorkflowRunner(
        storage=state.storage,
        registry=AgentRegistry(),
        artifact_store=state.artifact_store,
        trace_logger=state.trace_logger,
        executor=state.executor_factory(),
    ).resume_run(run_id, pack)
    return {"run": resumed_run, "session": approved_session}


def _safe_workflow_pack(pack: WorkflowPack) -> dict[str, Any]:
    payload = pack.model_dump(mode="json", by_alias=True)
    payload["agents"] = [_safe_agent_definition(agent) for agent in pack.agents]
    return payload


def _safe_agent_definition(agent: AgentDefinition) -> dict[str, Any]:
    payload = agent.model_dump(mode="json", by_alias=True)
    skill_ids = list(agent.effective_skill_ids)
    payload["system_prompt"] = _redacted_agent_prompt(agent)
    payload["prompt_redacted"] = bool(skill_ids)
    return payload


def _redacted_agent_prompt(agent: AgentDefinition) -> str:
    if not agent.effective_skill_ids:
        return agent.system_prompt
    lines = [
        _base_agent_prompt(agent.system_prompt),
        "",
        "# Local Skills",
        "Skill guidance is attached at runtime and hidden from catalog APIs.",
        f"Effective skill ids: {', '.join(agent.effective_skill_ids)}",
    ]
    return "\n".join(line for line in lines if line is not None).strip()


def _base_agent_prompt(system_prompt: str) -> str:
    markers = [
        "\n\n# Bound Local Skills",
        "\n\n# Auto-Selected Local Skills",
        "\n\n# Task-Selected Local Skills",
    ]
    end = len(system_prompt)
    for marker in markers:
        index = system_prompt.find(marker)
        if index >= 0:
            end = min(end, index)
    return system_prompt[:end].strip()


def _safe_runtime_session(session: Any) -> dict[str, Any]:
    return {
        "id": session.id,
        "run_id": session.run_id,
        "agent_run_id": session.agent_run_id,
        "agent_id": session.agent_id,
        "step_name": session.step_name,
        "runtime": session.runtime,
        "status": session.status.value,
        "resume_strategy": session.resume_strategy,
        "requires_approval": session.requires_approval,
        "created_at": session.created_at.isoformat(),
        "updated_at": session.updated_at.isoformat(),
        "local_only": True,
    }


def _safe_runtime_job(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "run_id": job.run_id,
        "agent_run_id": job.agent_run_id,
        "agent_session_id": job.agent_session_id,
        "step_name": job.step_name,
        "runtime": job.runtime,
        "status": job.status.value,
        "approval_required": job.approval_required,
        "approved_at": job.approved_at.isoformat() if job.approved_at is not None else None,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "message": job.message,
        "metadata": {
            "external_runtime_started": bool(job.metadata.get("external_runtime_started", False)),
        },
        "local_only": True,
    }


def _safe_queue_items(state: HarnessAppState, run_id: str) -> list[dict[str, Any]]:
    items = state.storage.list_run_queue_items_for_run(run_id)
    return [
        {
            "id": item.id,
            "run_id": item.run_id,
            "action": item.action,
            "status": item.status.value,
            "created_at": item.created_at.isoformat(),
            "updated_at": item.updated_at.isoformat(),
            "message": item.message,
            "local_only": True,
            "background_worker_started": False,
        }
        for item in items
    ]


def _safe_run_locks(state: HarnessAppState, run_id: str) -> list[dict[str, Any]]:
    locks = state.storage.list_run_locks_for_run(run_id)
    return [
        {
            "id": lock.id,
            "run_id": lock.run_id,
            "resource_type": "run",
            "resource_label": lock.run_id,
            "status": lock.status.value,
            "acquired_at": lock.acquired_at.isoformat(),
            "released_at": lock.released_at.isoformat() if lock.released_at is not None else None,
            "local_only": True,
        }
        for lock in locks
    ]


def _artifact_type_for_step(pack_name: str, step: WorkflowStep) -> ArtifactType:
    if step.produces_artifact_type:
        try:
            return ArtifactType(step.produces_artifact_type)
        except ValueError as exc:
            raise WorkflowRunnerError(f"Unsupported artifact type: {step.produces_artifact_type}") from exc

    by_pack = {
        "code_rd": {
            "clarify_requirements": ArtifactType.SOURCE_SUMMARY,
            "design_implementation": ArtifactType.DESIGN_DOC,
            "prepare_patch": ArtifactType.PATCH,
            "test_changes": ArtifactType.TEST_REPORT,
            "review_delivery": ArtifactType.RESEARCH_NOTE,
            "finalize_delivery": ArtifactType.FINAL_REPORT,
        },
        "research": {
            "plan_research": ArtifactType.DESIGN_DOC,
            "collect_sources": ArtifactType.SOURCE_SUMMARY,
            "read_sources": ArtifactType.RESEARCH_NOTE,
            "verify_claims": ArtifactType.TEST_REPORT,
            "draft_report": ArtifactType.FINAL_REPORT,
            "review_report": ArtifactType.RESEARCH_NOTE,
        },
    }
    try:
        return by_pack[pack_name][step.name]
    except KeyError as exc:
        raise WorkflowRunnerError(f"No mocked artifact mapping for {pack_name}/{step.name}") from exc


def _default_risk_notes_for_step(step: WorkflowStep) -> list[str]:
    if step.return_contract is not None and step.return_contract.require_risk_notes:
        return ["No additional risks reported by the deterministic mock executor."]
    return []


def _bounded_research_search_evidence(search_result: dict[str, Any]) -> dict[str, Any]:
    raw_items = search_result.get("results", [])
    items = [item for item in raw_items if isinstance(item, dict)] if isinstance(raw_items, list) else []
    normalized_items: list[dict[str, Any]] = []
    for item in items[:_RESEARCH_SEARCH_MAX_RESULTS]:
        url = _normalized_research_source_url(item.get("url"))
        if url is None:
            continue
        normalized_items.append(
            {
                "title": _bounded_external_text(item.get("title"), max_chars=200, max_bytes=800),
                "url": url,
                "snippet": _bounded_external_text(item.get("snippet"), max_chars=500, max_bytes=2_000),
                "published_at": _bounded_external_text(
                    item.get("published_at"), max_chars=40, max_bytes=160
                ),
            }
        )
    return {
        "trust": "untrusted_external_data",
        "safety_notice": _RESEARCH_EVIDENCE_SAFETY_NOTICE,
        "kind": "search_results",
        "provider": _bounded_external_text(search_result.get("provider"), max_chars=80, max_bytes=160),
        "mocked": bool(search_result.get("mocked", False)),
        "items": normalized_items,
    }


def _bounded_research_fetch_evidence(fetched: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_items: list[dict[str, Any]] = []
    for item in fetched[:_RESEARCH_FETCH_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        url = _normalized_research_source_url(item.get("url"))
        if url is None:
            continue
        normalized_items.append(
            {
                "url": url,
                "title": _bounded_external_text(item.get("title"), max_chars=200, max_bytes=800),
                "content": _bounded_external_text(
                    item.get("content"),
                    max_chars=_RESEARCH_FETCH_MAX_BYTES,
                    max_bytes=_RESEARCH_FETCH_MAX_BYTES,
                ),
                "content_type": _bounded_external_text(
                    item.get("content_type"), max_chars=120, max_bytes=480
                ),
                "status_code": _validated_status_code(item.get("status_code")),
            }
        )
    return {
        "trust": "untrusted_external_data",
        "safety_notice": _RESEARCH_EVIDENCE_SAFETY_NOTICE,
        "kind": "fetched_pages",
        "items": normalized_items,
    }


def _bounded_external_text(value: Any, *, max_chars: int, max_bytes: int) -> str:
    return bounded_external_text(value, max_chars=max_chars, max_bytes=max_bytes)


def _normalized_research_source_url(value: Any) -> str | None:
    try:
        return normalize_public_source_url(value)
    except ToolGatewayError:
        return None


def _require_real_tavily_fallback(result: Any, tool_name: str) -> None:
    if (
        not isinstance(result, dict)
        or result.get("provider") != "tavily"
        or result.get("mocked") is not False
    ):
        raise WorkflowRunnerError(f"Research {tool_name} fallback did not return confirmed real Tavily data.")


def _confirmed_real_browser_requested(
    provider: BrowserToolProvider,
    context: ToolContext,
) -> bool:
    return (
        context.real_web_access_confirmed
        and provider.provider_name != "mock"
        and provider.real_calls_enabled
    )


def _research_model_context(
    context: dict[str, Any],
    step: WorkflowStep,
    research_evidence: dict[str, Any],
) -> dict[str, Any]:
    model_context = {**context, "research_tool_evidence": research_evidence}
    manifest = context.get("context_manifest")
    if isinstance(manifest, dict):
        model_context["context_manifest"] = {
            **manifest,
            "retained_keys": sorted(model_context.keys()),
            "research_tool_evidence": {
                "kind": research_evidence.get("kind"),
                "item_count": len(research_evidence.get("items", [])),
            },
        }
    dispatched_context = context_message_from_envelope(model_context)
    context_chars = len(dispatched_context)
    context_bytes = len(dispatched_context.encode("utf-8"))
    if context_chars > step.context_policy.max_context_chars:
        raise ContextBudgetExceeded(
            f"Context envelope exceeds character budget for step {step.name}: "
            f"{context_chars} > {step.context_policy.max_context_chars}"
        )
    if context_bytes > step.context_policy.max_context_bytes:
        raise ContextBudgetExceeded(
            f"Context envelope exceeds byte budget for step {step.name}: "
            f"{context_bytes} > {step.context_policy.max_context_bytes}"
        )
    return model_context


def _research_source_summary(step_name: str, model_text: str, search_evidence: dict[str, Any]) -> tuple[str, list[str]]:
    results = [item for item in search_evidence.get("items", []) if isinstance(item, dict)]
    source_refs = [str(item.get("url", "")) for item in results if item.get("url")]
    lines = [
        f"# {step_name}",
        "",
        model_text,
        "",
        "## Sources",
    ]
    for index, item in enumerate(results, start=1):
        lines.append(
            f"{index}. {item.get('title', 'Untitled')} - {item.get('url', '')}"
        )
        snippet = str(item.get("snippet", "")).strip()
        if snippet:
            lines.append(f"   Summary: {snippet[:300]}")
        published_at = str(item.get("published_at", "")).strip()
        if published_at:
            lines.append(f"   Published: {published_at}")
    return "\n".join(lines) + "\n", source_refs


def _research_fetch_summary(
    step_name: str,
    model_text: str,
    fetch_evidence: dict[str, Any],
) -> tuple[str, list[str]]:
    fetched = [item for item in fetch_evidence.get("items", []) if isinstance(item, dict)]
    source_refs = [str(item.get("url", "")) for item in fetched if item.get("url")]
    lines = [
        f"# {step_name}",
        "",
        model_text,
        "",
        "## Fetched Sources",
    ]
    for index, item in enumerate(fetched, start=1):
        content = str(item.get("content", ""))
        lines.append(f"{index}. {item.get('url', '')}")
        lines.append(f"   Status: {item.get('status_code', '-')}, Content length: {len(content)}")
        if content:
            lines.append(f"   Extract: {content[:500]}")
    return "\n".join(lines) + "\n", source_refs


def _source_refs_from_context(context: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    artifacts = context.get("artifacts", [])
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            for source_ref in artifact.get("source_refs", []) or []:
                normalized = _normalized_research_source_url(source_ref)
                if normalized is not None and normalized not in seen:
                    seen.add(normalized)
                    refs.append(normalized)
    return refs
