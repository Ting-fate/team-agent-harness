from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationInfo,
    field_validator,
    model_validator,
)


CONFIRMABLE_REAL_WEB_TOOL_NAMES = frozenset(
    {"web_search", "fetch_page", "browser_search", "browser_fetch"}
)
ALLOW_LEGACY_REAL_WEB_SNAPSHOT_CONTEXT = "allow_legacy_real_web_snapshot"


def normalize_confirmed_real_web_tools(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    duplicates = sorted({name for name in value if value.count(name) > 1})
    if duplicates:
        raise ValueError(
            "confirmed_real_web_tools contains duplicate tool names: "
            f"{', '.join(duplicates)}"
        )
    unsupported = sorted(set(value) - CONFIRMABLE_REAL_WEB_TOOL_NAMES)
    if unsupported:
        raise ValueError(f"Unsupported real web tool names: {', '.join(unsupported)}")
    return sorted(value)


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class HarnessModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ConfirmedRealWebToolRoute(HarnessModel):
    name: str = Field(min_length=1, max_length=100)
    provider: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = normalize_confirmed_real_web_tools([value])
        assert normalized is not None
        return normalized[0]

    @field_validator("provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        return value.lower()


def normalize_confirmed_real_web_tool_routes(
    value: list[ConfirmedRealWebToolRoute] | None,
) -> list[ConfirmedRealWebToolRoute] | None:
    if value is None:
        return None
    identities = [(route.name, route.provider) for route in value]
    duplicates = sorted({identity for identity in identities if identities.count(identity) > 1})
    if duplicates:
        formatted = ", ".join(f"{name}@{provider}" for name, provider in duplicates)
        raise ValueError(
            "confirmed_real_web_tool_routes contains duplicate routes: "
            f"{formatted}"
        )
    return sorted(value, key=lambda route: (route.name, route.provider))


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AgentRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AgentSessionStatus(StrEnum):
    ACTIVE = "active"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class RuntimeJobStatus(StrEnum):
    RECORDED = "recorded"
    APPROVAL_REQUIRED = "approval_required"
    APPROVED = "approved"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class RunQueueItemStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunLockStatus(StrEnum):
    ACQUIRED = "acquired"
    RELEASED = "released"


class ArtifactType(StrEnum):
    DESIGN_DOC = "design_doc"
    PATCH = "patch"
    TEST_REPORT = "test_report"
    SOURCE_SUMMARY = "source_summary"
    RESEARCH_NOTE = "research_note"
    IMAGE_DESCRIPTION = "image_description"
    FINAL_REPORT = "final_report"


class TraceEventType(StrEnum):
    MODEL_ACTION = "model_action"
    WORKFLOW_EVENT = "workflow_event"
    RUNTIME_EVENT = "runtime_event"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    HANDOFF = "handoff"
    ARTIFACT_CREATED = "artifact_created"
    EVAL_RESULT = "eval_result"
    ERROR = "error"


class EvalStatus(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class ArtifactValidationStatus(StrEnum):
    UNVALIDATED = "unvalidated"
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class Task(HarnessModel):
    id: str = Field(default_factory=new_id, min_length=1)
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    workflow_pack: str = Field(min_length=1)
    inputs: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    created_by: str = Field(default="system", min_length=1)
    created_at: datetime = Field(default_factory=utc_now)


class Run(HarnessModel):
    _legacy_real_web_snapshot_run_id: str | None = PrivateAttr(default=None)

    id: str = Field(default_factory=new_id, min_length=1)
    task_id: str = Field(min_length=1)
    real_model_access_confirmed: bool = False
    real_web_access_confirmed: bool = False
    confirmed_real_web_tools: list[str] | None = Field(default_factory=list, max_length=4)
    confirmed_real_web_tool_routes: list[ConfirmedRealWebToolRoute] | None = Field(
        default_factory=list,
        max_length=4,
    )
    content_block_snapshot: list[dict[str, Any]] = Field(default_factory=list, max_length=16)
    content_block_snapshot_hash: str | None = Field(default=None, min_length=64, max_length=64)
    content_block_snapshot_files: dict[str, str] = Field(default_factory=dict, max_length=16)
    allow_external_model_inputs_snapshot: bool = False
    vision_preprocess_snapshot: dict[str, Any] | None = None
    approved_side_effect_tools: list[str] = Field(default_factory=list, max_length=32)
    execution_plan: dict[str, Any] | None = None
    execution_plan_hash: str | None = Field(default=None, min_length=64, max_length=64)
    execution_plan_redacted: bool = False
    status: RunStatus = RunStatus.QUEUED
    current_step: str | None = Field(default=None, min_length=1)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    final_artifact_id: str | None = Field(default=None, min_length=1)

    @field_validator("confirmed_real_web_tools")
    @classmethod
    def validate_confirmed_real_web_tools(cls, value: list[str] | None) -> list[str] | None:
        return normalize_confirmed_real_web_tools(value)

    @field_validator("confirmed_real_web_tool_routes")
    @classmethod
    def validate_confirmed_real_web_tool_routes(
        cls,
        value: list[ConfirmedRealWebToolRoute] | None,
    ) -> list[ConfirmedRealWebToolRoute] | None:
        return normalize_confirmed_real_web_tool_routes(value)

    @model_validator(mode="after")
    def validate_real_web_snapshot(self, info: ValidationInfo) -> Run:
        names = self.confirmed_real_web_tools
        routes = self.confirmed_real_web_tool_routes
        if names is None or routes is None:
            legacy_allowed = bool(
                info.context
                and info.context.get(ALLOW_LEGACY_REAL_WEB_SNAPSHOT_CONTEXT) is True
            )
            if names is None and routes is None and legacy_allowed:
                return self
            raise ValueError(
                "None real-web snapshots are reserved for legacy persisted Run records."
            )
        route_names = [route.name for route in routes]
        if names != route_names:
            raise ValueError(
                "confirmed_real_web_tools must exactly match confirmed_real_web_tool_routes."
            )
        if not self.real_web_access_confirmed and names:
            raise ValueError(
                "Non-empty real-web snapshots require real_web_access_confirmed=true."
            )
        return self

    @model_validator(mode="after")
    def validate_execution_plan_pair(self) -> Run:
        if (self.execution_plan is None) != (self.execution_plan_hash is None):
            raise ValueError(
                "execution_plan and execution_plan_hash must either both be present or both be absent."
            )
        return self


class AgentDefinition(HarnessModel):
    id: str = Field(default_factory=new_id, min_length=1)
    pack_name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    system_prompt: str = Field(min_length=1)
    model_settings: dict[str, Any] = Field(default_factory=dict, alias="model_config")
    tool_permissions: list[str] = Field(default_factory=list)
    runtime_limits: dict[str, Any] = Field(default_factory=dict)
    effective_skill_ids: list[str] = Field(default_factory=list)


class AgentRun(HarnessModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    step_name: str = Field(min_length=1)
    input_context: dict[str, Any] = Field(default_factory=dict)
    status: AgentRunStatus = AgentRunStatus.QUEUED
    started_at: datetime | None = None
    finished_at: datetime | None = None
    output_summary: str | None = Field(default=None, min_length=1)


class AgentSession(HarnessModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    agent_run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    step_name: str = Field(min_length=1)
    runtime: str = Field(min_length=1)
    status: AgentSessionStatus = AgentSessionStatus.ACTIVE
    resume_strategy: str = Field(default="none", min_length=1)
    requires_approval: bool = False
    external_ref: str | None = Field(default=None, min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeJob(HarnessModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    agent_run_id: str = Field(min_length=1)
    agent_session_id: str | None = Field(default=None, min_length=1)
    step_name: str = Field(min_length=1)
    runtime: str = Field(min_length=1)
    status: RuntimeJobStatus = RuntimeJobStatus.RECORDED
    approval_required: bool = False
    approved_at: datetime | None = None
    external_ref: str | None = Field(default=None, min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunQueueItem(HarnessModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    action: str = Field(min_length=1)
    status: RunQueueItemStatus = RunQueueItemStatus.QUEUED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    message: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunLock(HarnessModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    status: RunLockStatus = RunLockStatus.ACQUIRED
    acquired_at: datetime = Field(default_factory=utc_now)
    released_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Handoff(HarnessModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    from_agent_run_id: str = Field(min_length=1)
    to_agent_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    artifact_refs: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    next_objective: str = Field(min_length=1)
    constraints_to_preserve: list[str] = Field(default_factory=list)
    risk_notes: list[str] = Field(default_factory=list)


class Artifact(HarnessModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    agent_run_id: str = Field(min_length=1)
    type: ArtifactType
    path: str = Field(min_length=1)
    content_hash: str | None = Field(default=None, min_length=1)
    source_refs: list[str] = Field(default_factory=list)
    validation_status: ArtifactValidationStatus = ArtifactValidationStatus.UNVALIDATED
    created_at: datetime = Field(default_factory=utc_now)


class TraceEvent(HarnessModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    agent_run_id: str | None = Field(default=None, min_length=1)
    event_type: TraceEventType
    payload: dict[str, Any] = Field(default_factory=dict)
    duration_ms: int | None = Field(default=None, ge=0)
    created_at: datetime = Field(default_factory=utc_now)


class EvalResult(HarnessModel):
    id: str = Field(default_factory=new_id, min_length=1)
    run_id: str = Field(min_length=1)
    artifact_id: str | None = Field(default=None, min_length=1)
    check_name: str = Field(min_length=1)
    status: EvalStatus
    message: str = ""
    created_at: datetime = Field(default_factory=utc_now)
