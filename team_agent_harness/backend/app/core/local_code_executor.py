from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import shlex
import stat
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from typing import Any, BinaryIO, Callable, Iterator, Protocol
from uuid import uuid4

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

from app.core.artifacts import ArtifactStore, ArtifactStoreError
from app.core.model_runtime import (
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    context_message_from_envelope,
    default_reasoning_effort_for_model,
    model_allow_mock_fallback_from_config,
    model_fallbacks_from_config,
)
from app.core.multimodal import multimodal_source_refs
from app.core.models import (
    AgentDefinition,
    AgentRunStatus,
    Artifact,
    ArtifactType,
    EvalResult,
    EvalStatus,
    Run,
    Task,
)
from app.core.runner import AgentArtifactOutput, AgentStepOutput, WorkflowRunnerError
from app.core.sensitive_text import redact_secret_like_text
from app.packs.base import WorkflowStep


CODE_EXECUTOR_PACK = "code_rd_institutional"
CODE_EXECUTOR_STEPS = {"prepare_patch", "test_changes"}

_EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "data",
    "dist",
    "node_modules",
    "output",
    "target",
    "venv",
}
_SENSITIVE_NAME_MARKERS = {
    ".env",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
}
_TEXT_EXTENSIONS = {
    ".bat",
    ".cfg",
    ".css",
    ".csv",
    ".dockerfile",
    ".env.example",
    ".gitignore",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".ps1",
    ".py",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_MAX_FILE_BYTES = 20_000
_MAX_TOTAL_BYTES = 120_000
_MAX_FILES = 80
_MAX_COMMAND_OUTPUT_CHARS = 20_000
_DEFAULT_TEST_TIMEOUT_SECONDS = 120
_MAX_WORKSPACE_COPY_FILES = 20_000
_MAX_WORKSPACE_COPY_BYTES = 500_000_000
_NON_EXECUTING_PYTEST_OPTIONS = {
    "--cache-show",
    "--co",
    "--collect-only",
    "--fixtures",
    "--fixtures-per-test",
    "--funcargs",
    "--help",
    "--markers",
    "--setup-only",
    "--setup-plan",
    "--trace-config",
    "--version",
}
_ALLOWED_PYTEST_FLAG_OPTIONS = {
    "--disable-warnings",
    "--exitfirst",
    "--failed-first",
    "--last-failed",
    "--new-first",
    "--no-header",
    "--no-summary",
    "--quiet",
    "--stepwise",
    "--stepwise-skip",
    "--strict-config",
    "--strict-markers",
    "--verbose",
}
_ALLOWED_PYTEST_VALUE_OPTIONS = {
    "--capture",
    "--color",
    "--durations",
    "--durations-min",
    "--maxfail",
    "--tb",
    "--verbosity",
}
_TEST_ENVIRONMENT_ALLOWLIST = {
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "NUMBER_OF_PROCESSORS",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
    "SYSTEMROOT",
    "TZ",
    "WINDIR",
}


@dataclass(frozen=True)
class RepositorySnapshot:
    source_path: Path
    workspace_path: Path
    files: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


class PatchWorkspacePreparer(Protocol):
    def prepare_patched_workspace(
        self,
        *,
        run: Run,
        task: Task,
        artifact: Artifact,
        workspace_path: Path,
    ) -> dict[str, Any]:
        ...


class LocalCodeExecutor:
    def __init__(
        self,
        *,
        model_gateway: ModelGateway | None = None,
        artifact_store: ArtifactStore | None = None,
        patch_workspace_preparer: PatchWorkspacePreparer | None = None,
        model_request_binder: Callable[[Run, ModelRequest], ModelRequest] | None = None,
        workspace_root: str | Path = "output/local_code_workspaces",
    ) -> None:
        self.model_gateway = model_gateway or ModelGateway()
        self.artifact_store = artifact_store
        self.patch_workspace_preparer = patch_workspace_preparer
        self.model_request_binder = model_request_binder
        self.workspace_root = Path(workspace_root).expanduser().resolve()

    def supports(self, task: Task, step: WorkflowStep) -> bool:
        return (
            task.workflow_pack == CODE_EXECUTOR_PACK
            and step.execution_source == "workflow_pack"
            and step.name in CODE_EXECUTOR_STEPS
            and bool(task.inputs.get("repository_path"))
        )

    def execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        try:
            return self._execute(
                task=task,
                run=run,
                step=step,
                agent=agent,
                context=context,
            )
        except Exception as exc:
            sanitized = _sanitize_local_executor_error(
                exc,
                repository_value=task.inputs.get("repository_path"),
                workspace_root=self.workspace_root,
            )
            if sanitized == str(exc):
                raise
            raise WorkflowRunnerError(sanitized) from exc

    def _execute(
        self,
        *,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> AgentStepOutput:
        source_path = _resolve_repository_path(task.inputs.get("repository_path"))
        workspace_path = self._workspace_path(run, step)
        _validate_isolated_workspace(source_path, workspace_path)
        patch_artifact: Artifact | None = None
        patch_details: dict[str, Any] = {}
        if step.name == "prepare_patch":
            _prepare_workspace(source_path, workspace_path)
        elif step.name == "test_changes":
            _require_test_command(task.inputs.get("test_command"))
            _require_host_test_execution_opt_in(task.inputs.get("allow_host_test_execution"))
            patch_artifact = self._patch_artifact_for_test(
                run=run,
                step=step,
                agent=agent,
                context=context,
            )
            if self.patch_workspace_preparer is None:
                raise WorkflowRunnerError("Patched workspace preparation is not configured.")
            patch_details = self.patch_workspace_preparer.prepare_patched_workspace(
                run=run,
                task=task,
                artifact=patch_artifact,
                workspace_path=workspace_path,
            )
        else:
            raise WorkflowRunnerError(f"Local code executor does not support step: {step.name}")
        snapshot = _snapshot_repository(
            source_path=source_path,
            workspace_path=workspace_path,
            focus_paths=task.inputs.get("focus_paths"),
        )

        if step.name == "prepare_patch":
            return self._prepare_patch(task, run, step, agent, context, snapshot)
        return self._test_changes(
            task,
            run,
            step,
            agent,
            context,
            snapshot,
            patch_artifact,
            patch_details,
        )

    def _workspace_path(self, run: Run, step: WorkflowStep) -> Path:
        workspace_path = (self.workspace_root / run.id / step.name / "repo").resolve()
        if not workspace_path.is_relative_to(self.workspace_root) or workspace_path == self.workspace_root:
            raise WorkflowRunnerError("Local code workspace path must stay under workspace_root.")
        return workspace_path

    def _patch_artifact_for_test(
        self,
        *,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
    ) -> Artifact:
        if self.artifact_store is None:
            raise WorkflowRunnerError("Artifact storage is not configured for patched workspace testing.")
        handoff_payload = context.get("previous_handoff")
        if not isinstance(handoff_payload, dict):
            raise WorkflowRunnerError("test_changes requires a completed prepare_patch handoff.")
        handoff_id = handoff_payload.get("id")
        if not isinstance(handoff_id, str) or not handoff_id:
            raise WorkflowRunnerError("test_changes patch handoff is missing its canonical id.")
        handoff = self.artifact_store.storage.get_handoff(handoff_id)
        if handoff is None:
            raise WorkflowRunnerError("test_changes patch handoff was not found in durable storage.")
        if (
            handoff.run_id != run.id
            or handoff.next_objective != step.name
            or handoff.to_agent_id != agent.id
        ):
            raise WorkflowRunnerError("test_changes received a patch handoff for a different run or step.")
        source_agent_run_id = handoff.from_agent_run_id
        artifact_refs = handoff.artifact_refs
        if (
            not artifact_refs
            or any(not isinstance(artifact_id, str) or not artifact_id for artifact_id in artifact_refs)
            or len(set(artifact_refs)) != len(artifact_refs)
        ):
            raise WorkflowRunnerError("test_changes patch handoff has invalid artifact references.")

        source_agent_run = self.artifact_store.storage.get_agent_run(source_agent_run_id)
        if (
            source_agent_run is None
            or source_agent_run.run_id != run.id
            or source_agent_run.step_name != "prepare_patch"
            or source_agent_run.status != AgentRunStatus.COMPLETED
        ):
            raise WorkflowRunnerError("test_changes patch must come from a completed prepare_patch attempt.")
        completed_patch_attempts = [
            candidate
            for candidate in self.artifact_store.storage.list_agent_runs_for_run(run.id)
            if candidate.step_name == "prepare_patch"
            and candidate.status == AgentRunStatus.COMPLETED
        ]
        if not completed_patch_attempts or completed_patch_attempts[-1].id != source_agent_run.id:
            raise WorkflowRunnerError("test_changes patch must come from the current prepare_patch attempt.")
        dependency_lineage = context.get("dependency_lineage")
        lineage_entry = (
            dependency_lineage.get("prepare_patch")
            if isinstance(dependency_lineage, dict)
            else None
        )
        if not isinstance(lineage_entry, dict) or (
            lineage_entry.get("handoff_id") != handoff.id
            or lineage_entry.get("from_agent_run_id") != source_agent_run.id
        ):
            raise WorkflowRunnerError("test_changes dependency lineage does not match its patch handoff.")

        referenced_artifacts: list[Artifact] = []
        for artifact_id in artifact_refs:
            artifact = self.artifact_store.storage.get_artifact(artifact_id)
            if (
                artifact is None
                or artifact.run_id != run.id
                or artifact.agent_run_id != source_agent_run.id
            ):
                raise WorkflowRunnerError(
                    "test_changes patch handoff references an artifact outside its completed attempt."
                )
            referenced_artifacts.append(artifact)
        patch_artifacts = [artifact for artifact in referenced_artifacts if artifact.type == ArtifactType.PATCH]
        if len(patch_artifacts) != 1:
            raise WorkflowRunnerError("test_changes requires exactly one patch artifact from prepare_patch.")
        patch_artifact = patch_artifacts[0]
        try:
            self.artifact_store.read_text_verified(patch_artifact)
        except ArtifactStoreError as exc:
            raise WorkflowRunnerError(
                "test_changes patch artifact content hash does not match durable metadata."
            ) from exc
        return patch_artifact

    def _prepare_patch(
        self,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
        snapshot: RepositorySnapshot,
    ) -> AgentStepOutput:
        model_request = _model_request(
            task=task,
            step=step,
            agent=agent,
            context=context,
            snapshot=snapshot,
            instruction=(
                "Create a proposed implementation patch. Return a concise summary, "
                "then a fenced unified diff using ```diff if possible. "
                "Do not claim changes were applied."
            ),
            max_tokens=_max_tokens(agent, default=4000),
        )
        if self.model_request_binder is not None:
            model_request = self.model_request_binder(run, model_request)
        model_response = self.model_gateway.complete(model_request)
        content = _patch_artifact_content(task, run, snapshot, model_response)
        return AgentStepOutput(
            summary=f"Prepared proposed patch for {snapshot.source_path.name}.",
            artifacts=[
                AgentArtifactOutput(
                    type=ArtifactType.PATCH,
                    filename="local_code_patch.md",
                    content=content,
                    source_refs=_artifact_source_refs(context, snapshot.files),
                )
            ],
            risk_notes=[
                "Patch artifact is a proposal only; no changes were applied to the original repository.",
                "Repository snapshot excludes secret-like files and generated dependency directories.",
            ],
            model_request=model_request,
            model_response=model_response,
        )

    def _test_changes(
        self,
        task: Task,
        run: Run,
        step: WorkflowStep,
        agent: AgentDefinition,
        context: dict[str, Any],
        snapshot: RepositorySnapshot,
        patch_artifact: Artifact | None,
        patch_details: dict[str, Any],
    ) -> AgentStepOutput:
        if patch_artifact is None:
            raise WorkflowRunnerError("test_changes requires a patch artifact.")
        command = _require_test_command(task.inputs.get("test_command"))
        timeout_seconds = _positive_int(
            task.inputs.get("test_timeout_seconds"),
            default=_DEFAULT_TEST_TIMEOUT_SECONDS,
            maximum=900,
        )
        source_hashes_before = _source_hashes_for_test(snapshot, patch_details)
        _validate_source_hashes_match_patch_base(source_hashes_before, patch_details)
        test_result = _run_test_command(
            command,
            snapshot.workspace_path,
            timeout_seconds,
            source_path=snapshot.source_path,
        )
        if _source_hashes_for_test(snapshot, patch_details) != source_hashes_before:
            raise WorkflowRunnerError(
                "Source repository patch targets changed while the host test command was running."
            )
        if self.artifact_store is None:
            raise WorkflowRunnerError("Artifact storage is not configured for patched workspace testing.")
        try:
            self.artifact_store.read_text_verified(patch_artifact)
        except ArtifactStoreError as exc:
            raise WorkflowRunnerError(
                "test_changes patch artifact content hash does not match durable metadata."
            ) from exc
        model_request: ModelRequest | None = None
        model_response: ModelResponse | None = None
        if test_result.passed:
            model_request = _model_request(
                task=task,
                step=step,
                agent=agent,
                context={
                    **context,
                    "test_command": command,
                    "test_exit_code": test_result.exit_code,
                    "tested_patch_artifact_id": patch_artifact.id,
                    "tested_patch_hash": patch_details.get("patch_hash"),
                    "patched_files": patch_details.get("files_changed", []),
                },
                snapshot=snapshot,
                instruction=(
                    "Review the successful local test result and summarize residual risk. "
                    "The patch was applied only to the isolated workspace; do not claim the "
                    "original repository was modified."
                ),
                extra=f"\n\n## Test Result\n\n{test_result.markdown()}\n",
                max_tokens=_max_tokens(agent, default=3000),
            )
            if self.model_request_binder is not None:
                model_request = self.model_request_binder(run, model_request)
            model_response = self.model_gateway.complete(model_request)
        content = _test_artifact_content(
            task,
            run,
            snapshot,
            patch_artifact,
            patch_details,
            test_result,
            model_response,
        )
        eval_status = EvalStatus.PASS if test_result.passed else EvalStatus.FAIL
        safe_summary = _snapshot_safe_text(test_result.summary, snapshot)
        return AgentStepOutput(
            summary=safe_summary,
            artifacts=[
                AgentArtifactOutput(
                    type=ArtifactType.TEST_REPORT,
                    filename="local_code_test_report.md",
                    content=content,
                    source_refs=_artifact_source_refs(context, snapshot.files),
                )
            ],
            risk_notes=[
                "Tests ran in a disjoint working copy under the current host user; this is not an OS security sandbox.",
                "Patch-target files in the source repository were hash-checked before and after the test command.",
            ],
            eval_results=[
                EvalResult(
                    run_id=run.id,
                    check_name="patched_local_test_command",
                    status=eval_status,
                    message=safe_summary,
                )
            ],
            model_request=model_request,
            model_response=model_response,
        )


@dataclass(frozen=True)
class TestCommandResult:
    command: str | None
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    execution_verified: bool | None = None
    total_tests: int = 0
    skipped_tests: int = 0
    failed_tests: int = 0
    error_tests: int = 0
    verification_error: str | None = None

    @property
    def passed(self) -> bool:
        return bool(
            self.command
            and not self.timed_out
            and self.exit_code == 0
            and self.execution_verified is not False
            and self.failed_tests == 0
            and self.error_tests == 0
        )

    @property
    def summary(self) -> str:
        if not self.command:
            return "No test command provided; local tests were not run."
        if self.timed_out:
            return f"Test command timed out after execution: {self.command}"
        if self.exit_code == 0 and self.verification_error:
            return f"Test command rejected because {self.verification_error}: {self.command}"
        if self.passed:
            return f"Test command passed: {self.command}"
        return f"Test command failed with exit code {self.exit_code}: {self.command}"

    def markdown(self) -> str:
        return (
            f"- Command: `{self.command or 'not provided'}`\n"
            f"- Exit code: `{self.exit_code if self.exit_code is not None else 'not run'}`\n"
            f"- Timed out: `{self.timed_out}`\n\n"
            f"- Execution evidence verified: `{self.execution_verified}`\n"
            f"- Test cases: total `{self.total_tests}`, skipped `{self.skipped_tests}`, "
            f"failed `{self.failed_tests}`, errors `{self.error_tests}`\n"
            f"- Evidence error: `{self.verification_error or 'none'}`\n\n"
            "### stdout\n\n"
            f"```text\n{self.stdout}\n```\n\n"
            "### stderr\n\n"
            f"```text\n{self.stderr}\n```\n"
        )


def _resolve_repository_path(value: Any) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowRunnerError("repository_path must be a non-empty local directory path.")
    path = Path(value).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        raise WorkflowRunnerError("repository_path does not reference an existing local directory.")
    if path == Path(path.anchor):
        raise WorkflowRunnerError("repository_path must not be a drive root.")
    return path


def _validate_isolated_workspace(source_path: Path, workspace_path: Path) -> None:
    if (
        source_path == workspace_path
        or source_path.is_relative_to(workspace_path)
        or workspace_path.is_relative_to(source_path)
    ):
        raise WorkflowRunnerError(
            "Local code workspace and source repository must be disjoint."
        )


def _prepare_workspace(source_path: Path, workspace_path: Path) -> None:
    _preflight_repository_copy(source_path)
    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    if workspace_path.exists():
        shutil.rmtree(workspace_path)
    _copy_repository_safely(source_path, workspace_path)


def _preflight_repository_copy(source_path: Path) -> None:
    _walk_repository_safely(source_path, destination_path=None, budget=_CopyBudget())


def _copy_repository_safely(source_path: Path, destination_path: Path) -> None:
    _walk_repository_safely(source_path, destination_path=destination_path, budget=_CopyBudget())


@dataclass
class _CopyBudget:
    file_count: int = 0
    total_bytes: int = 0

    def include(self, size: int) -> None:
        self.file_count += 1
        self.total_bytes += size
        if self.file_count > _MAX_WORKSPACE_COPY_FILES:
            raise WorkflowRunnerError(
                f"repository_path exceeds the isolated-copy file limit of {_MAX_WORKSPACE_COPY_FILES}."
            )
        if self.total_bytes > _MAX_WORKSPACE_COPY_BYTES:
            raise WorkflowRunnerError(
                f"repository_path exceeds the isolated-copy byte limit of {_MAX_WORKSPACE_COPY_BYTES}."
            )


def _walk_repository_safely(
    source_path: Path,
    *,
    destination_path: Path | None,
    budget: _CopyBudget,
) -> None:
    if os.name == "nt":
        _walk_windows_directory(
            source_path,
            destination_path=destination_path,
            budget=budget,
            root_final_path=None,
        )
        return
    _walk_posix_repository(source_path, destination_path=destination_path, budget=budget)


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    del directory
    return {
        name
        for name in names
        if name.lower() in _EXCLUDED_DIR_NAMES or _is_sensitive_name(name)
    }


def _is_unsafe_copy_entry(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return True
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(path_stat.st_mode) or bool(
        reparse_attribute and getattr(path_stat, "st_file_attributes", 0) & reparse_attribute
    ) or (stat.S_ISREG(path_stat.st_mode) and path_stat.st_nlink > 1)


if os.name == "nt":
    _FILE_LIST_DIRECTORY = 0x0001
    _FILE_READ_ATTRIBUTES = 0x0080
    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _create_file.restype = wintypes.HANDLE
    _get_file_information = _kernel32.GetFileInformationByHandle
    _get_file_information.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
    _get_file_information.restype = wintypes.BOOL
    _get_final_path = _kernel32.GetFinalPathNameByHandleW
    _get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _get_final_path.restype = wintypes.DWORD
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL


@dataclass(frozen=True)
class _WindowsHandleInfo:
    final_path: str
    file_index: int
    size: int
    link_count: int


@contextmanager
def _windows_path_guard(
    path: Path,
    *,
    directory: bool,
    root_final_path: str | None,
) -> Iterator[_WindowsHandleInfo | None]:
    if os.name != "nt":  # pragma: no cover - platform dispatch guards this helper
        raise WorkflowRunnerError("Windows path guard used on a non-Windows platform.")
    desired_access = (_FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES) if directory else _GENERIC_READ
    flags = _FILE_FLAG_OPEN_REPARSE_POINT | (
        _FILE_FLAG_BACKUP_SEMANTICS if directory else _FILE_FLAG_SEQUENTIAL_SCAN
    )
    handle = _create_file(
        str(path),
        desired_access,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle in {None, _INVALID_HANDLE_VALUE}:
        error = ctypes.get_last_error()
        raise WorkflowRunnerError(f"repository entry could not be opened safely: {path}") from ctypes.WinError(error)
    try:
        raw_info = _ByHandleFileInformation()
        if not _get_file_information(handle, ctypes.byref(raw_info)):
            error = ctypes.get_last_error()
            raise WorkflowRunnerError(f"repository entry could not be inspected safely: {path}") from ctypes.WinError(error)
        attributes = int(raw_info.dwFileAttributes)
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            yield None
            return
        actual_directory = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
        if actual_directory != directory:
            raise WorkflowRunnerError(f"repository entry changed type while being copied: {path}")
        final_path = _normalized_windows_final_path(_final_windows_handle_path(handle))
        if root_final_path is not None and not _is_path_within_root(final_path, root_final_path):
            yield None
            return
        link_count = int(raw_info.nNumberOfLinks)
        if not directory and link_count != 1:
            yield None
            return
        yield _WindowsHandleInfo(
            final_path=final_path,
            file_index=(int(raw_info.nFileIndexHigh) << 32) | int(raw_info.nFileIndexLow),
            size=(int(raw_info.nFileSizeHigh) << 32) | int(raw_info.nFileSizeLow),
            link_count=link_count,
        )
    finally:
        _close_handle(handle)


def _final_windows_handle_path(handle: object) -> str:
    required = _get_final_path(handle, None, 0, 0)
    if not required:
        error = ctypes.get_last_error()
        raise WorkflowRunnerError("repository entry final path could not be resolved safely.") from ctypes.WinError(error)
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = _get_final_path(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        error = ctypes.get_last_error()
        raise WorkflowRunnerError("repository entry final path could not be resolved safely.") from ctypes.WinError(error)
    return buffer.value


def _normalized_windows_final_path(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.abspath(path))


def _is_path_within_root(candidate: str, root: str) -> bool:
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:
        return False


def _walk_windows_directory(
    source_path: Path,
    *,
    destination_path: Path | None,
    budget: _CopyBudget,
    root_final_path: str | None,
) -> None:
    with _windows_path_guard(
        source_path,
        directory=True,
        root_final_path=root_final_path,
    ) as directory_info:
        if directory_info is None:
            if root_final_path is None:
                raise WorkflowRunnerError("repository_path must not be a reparse point.")
            return
        active_root = root_final_path or directory_info.final_path
        try:
            with os.scandir(source_path) as iterator:
                names = sorted(entry.name for entry in iterator)
        except OSError as exc:
            raise WorkflowRunnerError(f"repository directory could not be inspected safely: {source_path}") from exc
        ignored = _copy_ignore(str(source_path), names)
        if destination_path is not None:
            destination_path.mkdir()

        for name in names:
            if name in ignored:
                continue
            entry_path = source_path / name
            try:
                entry_stat = entry_path.lstat()
            except OSError as exc:
                raise WorkflowRunnerError(f"repository entry changed while being copied: {entry_path}") from exc
            if _is_unsafe_copy_entry(entry_path):
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                _walk_windows_directory(
                    entry_path,
                    destination_path=destination_path / name if destination_path is not None else None,
                    budget=budget,
                    root_final_path=active_root,
                )
            elif stat.S_ISREG(entry_stat.st_mode):
                _copy_windows_file(
                    entry_path,
                    destination_path=destination_path / name if destination_path is not None else None,
                    budget=budget,
                    root_final_path=active_root,
                )


def _copy_windows_file(
    source_path: Path,
    *,
    destination_path: Path | None,
    budget: _CopyBudget,
    root_final_path: str,
) -> None:
    with _windows_path_guard(
        source_path,
        directory=False,
        root_final_path=root_final_path,
    ) as file_info:
        if file_info is None:
            return
        try:
            with source_path.open("rb") as source:
                opened_stat = os.fstat(source.fileno())
                if opened_stat.st_nlink != 1 or opened_stat.st_size != file_info.size:
                    raise WorkflowRunnerError(f"repository file identity changed while being copied: {source_path}")
                if opened_stat.st_ino and file_info.file_index and opened_stat.st_ino != file_info.file_index:
                    raise WorkflowRunnerError(f"repository file identity changed while being copied: {source_path}")
                budget.include(opened_stat.st_size)
                if destination_path is not None:
                    _copy_open_file(source, destination_path)
                final_stat = os.fstat(source.fileno())
                if (
                    final_stat.st_ino != opened_stat.st_ino
                    or final_stat.st_size != opened_stat.st_size
                    or final_stat.st_mtime_ns != opened_stat.st_mtime_ns
                ):
                    raise WorkflowRunnerError(f"repository file changed while being copied: {source_path}")
        except WorkflowRunnerError:
            raise
        except OSError as exc:
            raise WorkflowRunnerError(f"repository file could not be copied safely: {source_path}") from exc


def _walk_posix_repository(
    source_path: Path,
    *,
    destination_path: Path | None,
    budget: _CopyBudget,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(source_path, flags)
    except OSError as exc:
        raise WorkflowRunnerError(f"repository_path could not be opened safely: {source_path}") from exc
    try:
        root_stat = os.fstat(root_fd)
        _walk_posix_directory_fd(
            root_fd,
            source_path=source_path,
            destination_path=destination_path,
            budget=budget,
            root_device=root_stat.st_dev,
        )
    finally:
        os.close(root_fd)


def _walk_posix_directory_fd(
    directory_fd: int,
    *,
    source_path: Path,
    destination_path: Path | None,
    budget: _CopyBudget,
    root_device: int,
) -> None:
    try:
        with os.scandir(directory_fd) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError as exc:
        raise WorkflowRunnerError(f"repository directory could not be inspected safely: {source_path}") from exc
    ignored = _copy_ignore(str(source_path), names)
    if destination_path is not None:
        destination_path.mkdir()

    for name in names:
        if name in ignored:
            continue
        entry_path = source_path / name
        try:
            entry_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise WorkflowRunnerError(f"repository entry changed while being copied: {entry_path}") from exc
        if stat.S_ISLNK(entry_stat.st_mode):
            continue
        if stat.S_ISDIR(entry_stat.st_mode):
            if entry_stat.st_dev != root_device:
                continue
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                child_fd = os.open(name, flags, dir_fd=directory_fd)
            except OSError as exc:
                raise WorkflowRunnerError(f"repository directory changed while being copied: {entry_path}") from exc
            try:
                opened_stat = os.fstat(child_fd)
                if (opened_stat.st_dev, opened_stat.st_ino) != (entry_stat.st_dev, entry_stat.st_ino):
                    raise WorkflowRunnerError(f"repository directory identity changed while being copied: {entry_path}")
                _walk_posix_directory_fd(
                    child_fd,
                    source_path=entry_path,
                    destination_path=destination_path / name if destination_path is not None else None,
                    budget=budget,
                    root_device=root_device,
                )
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(entry_stat.st_mode):
            _copy_posix_file(
                directory_fd,
                name,
                entry_path=entry_path,
                destination_path=destination_path / name if destination_path is not None else None,
                budget=budget,
            )


def _copy_posix_file(
    directory_fd: int,
    name: str,
    *,
    entry_path: Path,
    destination_path: Path | None,
    budget: _CopyBudget,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        file_fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise WorkflowRunnerError(f"repository file changed while being copied: {entry_path}") from exc
    try:
        opened_stat = os.fstat(file_fd)
        current_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_nlink != 1
            or (opened_stat.st_dev, opened_stat.st_ino) != (current_stat.st_dev, current_stat.st_ino)
        ):
            return
        budget.include(opened_stat.st_size)
        with os.fdopen(file_fd, "rb", closefd=False) as source:
            if destination_path is not None:
                _copy_open_file(
                    source,
                    destination_path,
                    file_mode=stat.S_IMODE(opened_stat.st_mode) & 0o777,
                )
        final_stat = os.fstat(file_fd)
        if (
            final_stat.st_size != opened_stat.st_size
            or final_stat.st_mtime_ns != opened_stat.st_mtime_ns
            or final_stat.st_ctime_ns != opened_stat.st_ctime_ns
        ):
            raise WorkflowRunnerError(f"repository file changed while being copied: {entry_path}")
    finally:
        os.close(file_fd)


def _copy_open_file(
    source: BinaryIO,
    destination_path: Path,
    *,
    file_mode: int | None = None,
) -> None:
    with destination_path.open("xb") as destination:
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)
        if file_mode is not None:
            os.fchmod(destination.fileno(), file_mode)


def _snapshot_repository(
    *,
    source_path: Path,
    workspace_path: Path,
    focus_paths: Any = None,
) -> RepositorySnapshot:
    files: dict[str, str] = {}
    skipped: list[str] = []
    total_bytes = 0

    candidates = _candidate_files(workspace_path, focus_paths)
    for file_path in candidates:
        rel = file_path.relative_to(workspace_path).as_posix()
        if len(files) >= _MAX_FILES:
            skipped.append(f"{rel}: skipped after max file count")
            continue
        if _is_sensitive_name(file_path.name):
            skipped.append(f"{rel}: skipped sensitive-looking name")
            continue
        if file_path.stat().st_size > _MAX_FILE_BYTES:
            skipped.append(f"{rel}: skipped large file")
            continue
        if not _is_probably_text(file_path):
            skipped.append(f"{rel}: skipped non-text file")
            continue
        content = _read_text(file_path)
        encoded_len = len(content.encode("utf-8", errors="ignore"))
        if total_bytes + encoded_len > _MAX_TOTAL_BYTES:
            skipped.append(f"{rel}: skipped after max total context bytes")
            continue
        files[rel] = content
        total_bytes += encoded_len

    return RepositorySnapshot(
        source_path=source_path,
        workspace_path=workspace_path,
        files=files,
        skipped=skipped,
    )


def _candidate_files(workspace_path: Path, focus_paths: Any) -> list[Path]:
    if focus_paths:
        if not isinstance(focus_paths, list):
            raise WorkflowRunnerError("focus_paths must be a list of relative paths.")
        results: list[Path] = []
        for raw_focus_path in focus_paths:
            if not isinstance(raw_focus_path, str) or not raw_focus_path.strip():
                raise WorkflowRunnerError("focus_paths must contain non-empty relative paths.")
            focus_path = Path(raw_focus_path)
            if focus_path.is_absolute() or ".." in focus_path.parts:
                raise WorkflowRunnerError("focus_paths must stay inside repository_path.")
            resolved = (workspace_path / focus_path).resolve()
            try:
                resolved.relative_to(workspace_path.resolve())
            except ValueError as exc:
                raise WorkflowRunnerError("focus_paths must stay inside repository_path.") from exc
            if resolved.is_dir():
                results.extend(_walk_files(resolved, workspace_path))
            elif resolved.is_file():
                results.append(resolved)
            else:
                raise WorkflowRunnerError(f"focus_path does not exist: {raw_focus_path}")
        return sorted(set(results), key=lambda path: path.as_posix())
    return _walk_files(workspace_path, workspace_path)


def _walk_files(root: Path, workspace_path: Path) -> list[Path]:
    results: list[Path] = []
    for file_path in root.rglob("*"):
        if _is_unsafe_copy_entry(file_path) or not file_path.is_file():
            continue
        rel_parts = file_path.relative_to(workspace_path).parts
        if any(part.lower() in _EXCLUDED_DIR_NAMES for part in rel_parts[:-1]):
            continue
        results.append(file_path)
    return sorted(results, key=lambda path: path.relative_to(root).as_posix())


def _is_probably_text(path: Path) -> bool:
    if path.suffix.lower() not in _TEXT_EXTENSIONS and path.name.lower() not in {"dockerfile", "makefile"}:
        return False
    try:
        chunk = path.read_bytes()[:1024]
    except OSError:
        return False
    return b"\x00" not in chunk


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    if lowered == ".env" or lowered.startswith(".env."):
        return True
    if lowered in {".netrc", ".npmrc", ".pypirc", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"}:
        return True
    if Path(lowered).suffix in {".jks", ".key", ".p12", ".pem", ".pfx"}:
        return True
    normalized = re.sub(r"[^a-z0-9]+", "_", Path(lowered).stem).strip("_")
    segments = normalized.split("_") if normalized else []
    segment_markers = {"apikey", "authorization", "credential", "credentials", "password", "secret", "secrets", "token"}
    if any(segment in segment_markers for segment in segments):
        return True
    return "api_key" in normalized or "private_key" in normalized


def _artifact_source_refs(context: dict[str, Any], repository_files: dict[str, str]) -> list[str]:
    return list(dict.fromkeys([*sorted(repository_files), *multimodal_source_refs(context)]))


def _model_request(
    *,
    task: Task,
    step: WorkflowStep,
    agent: AgentDefinition,
    context: dict[str, Any],
    snapshot: RepositorySnapshot,
    instruction: str,
    extra: str = "",
    max_tokens: int | None = None,
) -> ModelRequest:
    provider = str(agent.model_settings.get("provider", "mock"))
    model = str(agent.model_settings.get("model", "mock-model"))
    reasoning_effort = _optional_str(agent.model_settings.get("reasoning_effort")) or default_reasoning_effort_for_model(
        provider,
        model,
    )
    model_content_blocks = context.get("_model_content_blocks")
    required_capabilities = [
        item
        for item in context.get("required_model_capabilities", [])
        if isinstance(item, str)
    ]
    if isinstance(model_content_blocks, list) and any(
        isinstance(item, dict) and item.get("type") == "image_ref" for item in model_content_blocks
    ) and "vision" not in required_capabilities:
        required_capabilities.append("vision")
    messages = [
        ModelMessage(
            role="user",
            content=_snapshot_safe_text(f"Task: {task.title}\nGoal: {task.goal}", snapshot),
        ),
        ModelMessage(
            role="user",
            content=context_message_from_envelope(_model_safe_context(context, snapshot)),
        ),
        ModelMessage(
            role="user",
            content=_snapshot_safe_text(
                (
                    f"Step: {step.name}\n"
                    f"Instruction: {instruction}\n"
                    f"Constraints: {task.constraints}\n"
                    f"Acceptance criteria: {task.acceptance_criteria}\n"
                    f"Repository context:\n{_snapshot_markdown(snapshot)}"
                    f"{extra}"
                ),
                snapshot,
            ),
        ),
    ]
    if isinstance(model_content_blocks, list) and model_content_blocks:
        messages.append(ModelMessage(role="user", content=model_content_blocks))
    return ModelRequest(
        provider=provider,
        model=model,
        system_prompt=_snapshot_safe_text(agent.system_prompt, snapshot),
        messages=messages,
        temperature=_optional_float(agent.model_settings.get("temperature")),
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        tools_allowed=step.allowed_tools,
        fallbacks=model_fallbacks_from_config(agent.model_settings),
        metadata={
            "task_title": _snapshot_safe_text(task.title, snapshot),
            "step_name": step.name,
            "agent_id": agent.id,
            "agent_run_id": context.get("agent_run_id"),
            "agent_role": agent.role,
            "context_keys": sorted(context.keys()),
            "local_code_executor": True,
            "repository_files": sorted(snapshot.files),
            "allow_mock_fallback": model_allow_mock_fallback_from_config(agent.model_settings),
            "required_model_capabilities": required_capabilities,
            "run_bound": isinstance(context.get("run_id"), str) and bool(context.get("run_id")),
            "real_model_access_confirmed": context.get("real_model_access_confirmed") is True,
            "content_block_hashes": [
                str(item.get("sha256"))
                for item in context.get("content_blocks", [])
                if isinstance(item, dict) and item.get("sha256")
            ],
        },
    )


def _snapshot_markdown(snapshot: RepositorySnapshot) -> str:
    parts = [
        f"- Repository label: `{snapshot.source_path.name}`",
        "- Workspace: disjoint local working copy",
        f"- Included files: `{len(snapshot.files)}`",
    ]
    if snapshot.skipped:
        parts.append("- Skipped files:")
        parts.extend(f"  - {item}" for item in snapshot.skipped[:40])
    for rel_path, content in snapshot.files.items():
        parts.append(f"\n### {rel_path}\n\n```text\n{_redact(content)}\n```")
    return "\n".join(parts)


def _patch_artifact_content(
    task: Task,
    run: Run,
    snapshot: RepositorySnapshot,
    model_response: ModelResponse,
) -> str:
    return (
        "# Local Code Patch Proposal\n\n"
        f"- Run: `{run.id}`\n"
        f"- Task: `{_snapshot_safe_text(task.title, snapshot)}`\n"
        f"- Repository label: `{snapshot.source_path.name}`\n"
        "- Workspace: disjoint local working copy\n"
        "- Source repository write requested: `false`\n\n"
        "## Included Repository Files\n\n"
        + "\n".join(f"- `{path}`" for path in sorted(snapshot.files))
        + "\n\n## Skipped Files\n\n"
        + ("\n".join(f"- {item}" for item in snapshot.skipped) if snapshot.skipped else "- None")
        + "\n\n## Model Patch Proposal\n\n"
        + _snapshot_safe_text(model_response.text, snapshot)
        + "\n"
    )


def _test_artifact_content(
    task: Task,
    run: Run,
    snapshot: RepositorySnapshot,
    patch_artifact: Artifact,
    patch_details: dict[str, Any],
    test_result: TestCommandResult,
    model_response: ModelResponse | None,
) -> str:
    changed_files = patch_details.get("files_changed")
    changed_files_text = ", ".join(
        str(item.get("path"))
        for item in changed_files
        if isinstance(item, dict) and isinstance(item.get("path"), str)
    ) if isinstance(changed_files, list) else ""
    return (
        "# Local Code Test Report\n\n"
        f"- Run: `{run.id}`\n"
        f"- Task: `{_snapshot_safe_text(task.title, snapshot)}`\n"
        f"- Repository label: `{snapshot.source_path.name}`\n"
        "- Workspace: disjoint local working copy\n"
        f"- Tested patch artifact: `{patch_artifact.id}`\n"
        f"- Tested patch hash: `{patch_details.get('patch_hash', 'unknown')}`\n"
        f"- Patched files: `{changed_files_text or 'unknown'}`\n"
        "- Patch applied to isolated workspace: `true`\n"
        "- Source patch-target hashes unchanged during test: `true`\n"
        "- Host process security sandbox: `false`\n\n"
        "## Command Result\n\n"
        f"Summary: {_snapshot_safe_text(test_result.summary, snapshot)}\n\n"
        + _snapshot_safe_text(test_result.markdown(), snapshot)
        + "\n## Model Test Review\n\n"
        + (
            _snapshot_safe_text(model_response.text, snapshot)
            if model_response is not None
            else "Skipped because the deterministic local test command did not pass."
        )
        + "\n"
    )


def _require_test_command(command: Any) -> str:
    if command is None or command == "":
        raise WorkflowRunnerError("test_command is required for patched workspace testing.")
    if not isinstance(command, str):
        raise WorkflowRunnerError("test_command must be a string.")
    _parse_allowed_test_command(command)
    return command


def _require_host_test_execution_opt_in(value: Any) -> None:
    if value is not True:
        raise WorkflowRunnerError(
            "allow_host_test_execution=true is required because patched tests run with the current host user; "
            "the disjoint working copy is not an OS security sandbox."
        )


def _run_test_command(
    command: Any,
    workspace_path: Path,
    timeout_seconds: int,
    *,
    source_path: Path | None = None,
) -> TestCommandResult:
    if command is None or command == "":
        return TestCommandResult(command=None, exit_code=None)
    if not isinstance(command, str):
        raise WorkflowRunnerError("test_command must be a string.")
    args = _parse_allowed_test_command(command)
    runtime_root = _test_runtime_root(workspace_path)
    evidence_path = runtime_root / f"pytest-evidence-{uuid4().hex}.xml"
    separator_index = args.index("--") if "--" in args else len(args)
    args[separator_index:separator_index] = ["--junitxml", str(evidence_path)]
    environment = _sanitized_environment(workspace_path)
    try:
        completed = subprocess.run(
            args,
            cwd=workspace_path,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return TestCommandResult(
            command=command,
            exit_code=None,
            stdout=_sanitize_test_output(exc.stdout or "", workspace_path, evidence_path, source_path),
            stderr=_sanitize_test_output(exc.stderr or "", workspace_path, evidence_path, source_path),
            timed_out=True,
            execution_verified=False,
            verification_error="pytest execution timed out before evidence could be accepted",
        )
    try:
        evidence = _read_pytest_evidence(evidence_path)
    finally:
        evidence_path.unlink(missing_ok=True)
    return TestCommandResult(
        command=command,
        exit_code=completed.returncode,
        stdout=_sanitize_test_output(completed.stdout, workspace_path, evidence_path, source_path),
        stderr=_sanitize_test_output(completed.stderr, workspace_path, evidence_path, source_path),
        **evidence,
    )


def _read_pytest_evidence(evidence_path: Path) -> dict[str, Any]:
    try:
        root = ElementTree.parse(evidence_path).getroot()
    except (OSError, ElementTree.ParseError):
        return {
            "execution_verified": False,
            "verification_error": "pytest did not produce valid independent test-case evidence",
        }

    tag = root.tag.rsplit("}", 1)[-1]
    if tag == "testsuite":
        suites = [root]
    elif tag == "testsuites":
        suites = [child for child in root if child.tag.rsplit("}", 1)[-1] == "testsuite"]
    else:
        suites = []
    try:
        total = sum(int(suite.attrib.get("tests", "0")) for suite in suites)
        skipped = sum(int(suite.attrib.get("skipped", "0")) for suite in suites)
        failed = sum(int(suite.attrib.get("failures", "0")) for suite in suites)
        errors = sum(int(suite.attrib.get("errors", "0")) for suite in suites)
    except ValueError:
        return {
            "execution_verified": False,
            "verification_error": "pytest test-case evidence contained invalid counters",
        }
    non_skipped = total - skipped
    verification_error = None
    if non_skipped <= 0:
        verification_error = "pytest reported no non-skipped tests"
    elif failed or errors:
        verification_error = "pytest evidence reported failed or errored tests"
    return {
        "execution_verified": verification_error is None,
        "total_tests": total,
        "skipped_tests": skipped,
        "failed_tests": failed,
        "error_tests": errors,
        "verification_error": verification_error,
    }


def _sanitize_test_output(
    value: str,
    workspace_path: Path,
    evidence_path: Path,
    source_path: Path | None = None,
) -> str:
    paths = (workspace_path, evidence_path) if source_path is None else (workspace_path, evidence_path, source_path)
    sanitized = _redact_paths(value, *paths)
    return _truncate_output(_redact(sanitized))


def _parse_allowed_test_command(command: str) -> list[str]:
    if any(marker in command for marker in ["&&", "||", "|", ";", ">", "<", "`"]):
        raise WorkflowRunnerError("test_command contains shell control characters and is not allowed.")
    try:
        args = shlex.split(command, posix=False)
    except ValueError as exc:
        raise WorkflowRunnerError("test_command could not be parsed.") from exc
    if not args:
        raise WorkflowRunnerError("test_command must not be empty.")

    executable = args[0].lower()
    if executable in {"pytest", "pytest.exe"}:
        pytest_args = args[1:]
    elif executable in {"python", "python.exe", "py", "py.exe"} and len(args) >= 3 and args[1:3] == ["-m", "pytest"]:
        pytest_args = args[3:]
    else:
        raise WorkflowRunnerError("test_command is not allowed. Use pytest or python -m pytest.")

    index = 0
    while index < len(pytest_args):
        argument = pytest_args[index]
        long_option, separator, attached_value = argument.partition("=")
        normalized_long_option = long_option.lower()
        if argument in {"-V", "-VV", "-h"} or normalized_long_option in _NON_EXECUTING_PYTEST_OPTIONS:
            raise WorkflowRunnerError("test_command must execute tests, not only inspect or collect them.")
        if normalized_long_option == "--pyargs":
            raise WorkflowRunnerError("test_command option is not allowed: --pyargs")
        if argument == "--":
            for target in pytest_args[index + 1 :]:
                if _pytest_argument_can_escape_workspace(target):
                    raise WorkflowRunnerError("test_command paths must stay inside the isolated workspace.")
            break
        if argument.startswith("--"):
            if normalized_long_option in {"--basetemp", "--confcutdir", "--rootdir"}:
                raise WorkflowRunnerError("test_command paths must stay inside the isolated workspace.")
            if normalized_long_option in _ALLOWED_PYTEST_FLAG_OPTIONS and not separator:
                index += 1
                continue
            if normalized_long_option in _ALLOWED_PYTEST_VALUE_OPTIONS:
                if not separator:
                    index += 1
                    if index >= len(pytest_args) or pytest_args[index].startswith("-"):
                        raise WorkflowRunnerError(f"test_command option requires a value: {long_option}")
                elif not attached_value:
                    raise WorkflowRunnerError(f"test_command option requires a value: {long_option}")
                index += 1
                continue
            raise WorkflowRunnerError(f"test_command option is not allowed: {long_option}")
        if argument.startswith("-") and argument != "-":
            if argument in {"-k", "-m", "-r"}:
                index += 1
                if index >= len(pytest_args):
                    raise WorkflowRunnerError(f"test_command option requires a value: {argument}")
                index += 1
                continue
            if re.fullmatch(r"-(?:q+|v+|s|x)", argument) or re.fullmatch(
                r"-r[fEsxXapPw]+", argument
            ):
                index += 1
                continue
            raise WorkflowRunnerError(f"test_command option is not allowed: {argument}")
        if _pytest_argument_can_escape_workspace(argument):
            raise WorkflowRunnerError("test_command paths must stay inside the isolated workspace.")
        index += 1
    return [sys.executable, "-m", "pytest", "-o", "addopts=", *pytest_args]


def _pytest_argument_can_escape_workspace(argument: str) -> bool:
    value = argument.strip().strip("\"'")
    if value.startswith("@"):
        return True
    if "=" in value:
        value = value.split("=", 1)[1].strip().strip("\"'")
    value = value.split("::", 1)[0]
    if not value:
        return False
    windows_path = PureWindowsPath(value)
    posix_path = PurePosixPath(value)
    return (
        windows_path.is_absolute()
        or bool(windows_path.drive)
        or posix_path.is_absolute()
        or ".." in windows_path.parts
        or ".." in posix_path.parts
    )


def _sanitized_environment(workspace_path: Path) -> dict[str, str]:
    sanitized = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _TEST_ENVIRONMENT_ALLOWLIST
    }
    runtime_root = _test_runtime_root(workspace_path)
    home_path = runtime_root / "home"
    temp_path = runtime_root / "temp"
    home_path.mkdir(parents=True, exist_ok=True)
    temp_path.mkdir(parents=True, exist_ok=True)
    sanitized.update(
        {
            "ALL_PROXY": "http://127.0.0.1:9",
            "APPDATA": str(home_path / "AppData" / "Roaming"),
            "HOME": str(home_path),
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "LOCALAPPDATA": str(home_path / "AppData" / "Local"),
            "NO_PROXY": "",
            "PYTHONDONTWRITEBYTECODE": "1",
            "TEMP": str(temp_path),
            "TMP": str(temp_path),
            "TMPDIR": str(temp_path),
            "USERPROFILE": str(home_path),
        }
    )
    return sanitized


def _test_runtime_root(workspace_path: Path) -> Path:
    runtime_root = (workspace_path.parent / ".team-agent-test-runtime").resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    return runtime_root


def _model_safe_context(context: dict[str, Any], snapshot: RepositorySnapshot) -> dict[str, Any]:
    safe_context = {**context}
    task_payload = context.get("task")
    if isinstance(task_payload, dict):
        safe_task = {**task_payload}
        task_inputs = task_payload.get("inputs")
        if isinstance(task_inputs, dict):
            safe_task["inputs"] = {
                key: value
                for key, value in task_inputs.items()
                if key not in {"repository_path", "confirm_repository_path"}
            }
        safe_context["task"] = safe_task
    return _redact_context_value(safe_context, snapshot.source_path, snapshot.workspace_path)


def _redact_context_value(value: Any, *paths: Path) -> Any:
    if isinstance(value, str):
        return _redact_paths(_redact(value), *paths)
    if isinstance(value, dict):
        return {key: _redact_context_value(item, *paths) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_context_value(item, *paths) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_context_value(item, *paths) for item in value)
    return value


def _source_hashes_for_test(
    snapshot: RepositorySnapshot,
    patch_details: dict[str, Any],
) -> dict[str, str]:
    relative_paths = set(snapshot.files)
    changed_files = patch_details.get("files_changed")
    if isinstance(changed_files, list):
        relative_paths.update(
            item["path"]
            for item in changed_files
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        )
    hashes: dict[str, str] = {}
    source_root = snapshot.source_path.resolve()
    for relative_path in sorted(relative_paths):
        candidate = (source_root / Path(*PurePosixPath(relative_path).parts)).resolve()
        if not candidate.is_relative_to(source_root) or not candidate.is_file() or _is_unsafe_copy_entry(candidate):
            raise WorkflowRunnerError(
                f"Source repository patch target could not be verified safely: {relative_path}"
            )
        hashes[relative_path] = sha256(candidate.read_bytes()).hexdigest()
    return hashes


def _validate_source_hashes_match_patch_base(
    source_hashes: dict[str, str],
    patch_details: dict[str, Any],
) -> None:
    expected = patch_details.get("base_hashes")
    if not isinstance(expected, dict) or not expected or any(
        not isinstance(path, str) or not isinstance(content_hash, str)
        for path, content_hash in expected.items()
    ):
        raise WorkflowRunnerError("Patched workspace did not provide valid source base hashes.")
    if any(source_hashes.get(path) != content_hash for path, content_hash in expected.items()):
        raise WorkflowRunnerError(
            "Source repository patch targets no longer match the patch base used for isolated testing."
        )


def _truncate_output(value: str) -> str:
    if len(value) <= _MAX_COMMAND_OUTPUT_CHARS:
        return value
    return value[:_MAX_COMMAND_OUTPUT_CHARS] + "\n[truncated]\n"


def _redact(value: str) -> str:
    return redact_secret_like_text(value)


def _snapshot_safe_text(value: str, snapshot: RepositorySnapshot) -> str:
    return _redact_paths(_redact(value), snapshot.source_path, snapshot.workspace_path)


def _redact_paths(value: str, *paths: Path) -> str:
    redacted = value
    variants: set[str] = set()
    for path in paths:
        resolved = str(path.resolve())
        variants.add(resolved)
        if "\\" in resolved:
            variants.add(resolved.replace("\\", "/"))
        if resolved.startswith("\\\\?\\"):
            without_prefix = resolved[4:]
            variants.add(without_prefix)
            variants.add(without_prefix.replace("\\", "/"))
    for variant in sorted(variants, key=len, reverse=True):
        redacted = re.sub(re.escape(variant), "[LOCAL_PATH]", redacted, flags=re.IGNORECASE)
    return redacted


def _sanitize_local_executor_error(
    exc: Exception,
    *,
    repository_value: Any,
    workspace_root: Path,
) -> str:
    message = _redact(str(exc))
    if isinstance(repository_value, str) and repository_value.strip():
        repository_path = Path(repository_value).expanduser().resolve()
        message = re.sub(
            re.escape(str(repository_path)),
            "[REPOSITORY_PATH]",
            message,
            flags=re.IGNORECASE,
        )
    return _redact_paths(message, workspace_root)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _max_tokens(agent: AgentDefinition, *, default: int) -> int:
    raw_value = agent.model_settings.get("max_tokens")
    if raw_value is None:
        return default
    return _positive_int(raw_value, default=default, maximum=200_000)


def _positive_int(value: Any, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    parsed = int(value)
    if parsed <= 0:
        raise WorkflowRunnerError("Numeric executor input must be positive.")
    return min(parsed, maximum)
