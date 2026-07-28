from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import shlex
import stat
import subprocess
import sys
from typing import Any, BinaryIO, Iterator

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

from app.core.model_runtime import (
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    context_message_from_envelope,
    default_reasoning_effort_for_model,
)
from app.core.models import AgentDefinition, ArtifactType, EvalResult, EvalStatus, Run, Task
from app.core.runner import AgentArtifactOutput, AgentStepOutput, WorkflowRunnerError
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


@dataclass(frozen=True)
class RepositorySnapshot:
    source_path: Path
    workspace_path: Path
    files: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


class LocalCodeExecutor:
    def __init__(
        self,
        *,
        model_gateway: ModelGateway | None = None,
        workspace_root: str | Path = "output/local_code_workspaces",
    ) -> None:
        self.model_gateway = model_gateway or ModelGateway()
        self.workspace_root = Path(workspace_root).expanduser().resolve()

    def supports(self, task: Task, step: WorkflowStep) -> bool:
        return (
            task.workflow_pack == CODE_EXECUTOR_PACK
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
        source_path = _resolve_repository_path(task.inputs.get("repository_path"))
        workspace_path = self._workspace_path(run, step)
        _prepare_workspace(source_path, workspace_path)
        snapshot = _snapshot_repository(
            source_path=source_path,
            workspace_path=workspace_path,
            focus_paths=task.inputs.get("focus_paths"),
        )

        if step.name == "prepare_patch":
            return self._prepare_patch(task, run, step, agent, context, snapshot)
        if step.name == "test_changes":
            return self._test_changes(task, run, step, agent, context, snapshot)
        raise WorkflowRunnerError(f"Local code executor does not support step: {step.name}")

    def _workspace_path(self, run: Run, step: WorkflowStep) -> Path:
        return self.workspace_root / run.id / step.name / "repo"

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
        model_response = self.model_gateway.complete(model_request)
        content = _patch_artifact_content(task, run, snapshot, model_response)
        return AgentStepOutput(
            summary=f"Prepared proposed patch for {snapshot.source_path.name}.",
            artifacts=[
                AgentArtifactOutput(
                    type=ArtifactType.PATCH,
                    filename="local_code_patch.md",
                    content=content,
                    source_refs=sorted(snapshot.files),
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
    ) -> AgentStepOutput:
        command = task.inputs.get("test_command")
        timeout_seconds = _positive_int(
            task.inputs.get("test_timeout_seconds"),
            default=_DEFAULT_TEST_TIMEOUT_SECONDS,
            maximum=900,
        )
        test_result = _run_test_command(command, snapshot.workspace_path, timeout_seconds)
        model_request = _model_request(
            task=task,
            step=step,
            agent=agent,
            context={
                **context,
                "test_command": command,
                "test_exit_code": test_result.exit_code,
            },
            snapshot=snapshot,
            instruction=(
                "Review the local test result. Summarize pass/fail status, likely causes, "
                "and residual risk. Do not claim any patch was applied."
            ),
            extra=f"\n\n## Test Result\n\n{test_result.markdown()}\n",
            max_tokens=_max_tokens(agent, default=3000),
        )
        model_response = self.model_gateway.complete(model_request)
        content = _test_artifact_content(task, run, snapshot, test_result, model_response)
        eval_status = EvalStatus.PASS if test_result.exit_code == 0 else EvalStatus.WARN
        if test_result.exit_code is None:
            eval_status = EvalStatus.WARN
        return AgentStepOutput(
            summary=test_result.summary,
            artifacts=[
                AgentArtifactOutput(
                    type=ArtifactType.TEST_REPORT,
                    filename="local_code_test_report.md",
                    content=content,
                    source_refs=sorted(snapshot.files),
                )
            ],
            risk_notes=[
                "Tests ran in an isolated workspace copy, not the original repository."
                if test_result.command
                else "No test_command was provided; no local tests were executed.",
            ],
            eval_results=[
                EvalResult(
                    run_id=run.id,
                    check_name="local_test_command",
                    status=eval_status,
                    message=test_result.summary,
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

    @property
    def summary(self) -> str:
        if not self.command:
            return "No test command provided; local tests were not run."
        if self.timed_out:
            return f"Test command timed out after execution: {self.command}"
        if self.exit_code == 0:
            return f"Test command passed: {self.command}"
        return f"Test command failed with exit code {self.exit_code}: {self.command}"

    def markdown(self) -> str:
        return (
            f"- Command: `{self.command or 'not provided'}`\n"
            f"- Exit code: `{self.exit_code if self.exit_code is not None else 'not run'}`\n"
            f"- Timed out: `{self.timed_out}`\n\n"
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
        raise WorkflowRunnerError(f"repository_path is not a directory: {path}")
    if path == Path(path.anchor):
        raise WorkflowRunnerError("repository_path must not be a drive root.")
    return path


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
    return ModelRequest(
        provider=provider,
        model=model,
        system_prompt=agent.system_prompt,
        messages=[
            ModelMessage(role="user", content=f"Task: {task.title}\nGoal: {task.goal}"),
            ModelMessage(role="user", content=context_message_from_envelope(context)),
            ModelMessage(
                role="user",
                content=(
                    f"Step: {step.name}\n"
                    f"Instruction: {instruction}\n"
                    f"Constraints: {task.constraints}\n"
                    f"Acceptance criteria: {task.acceptance_criteria}\n"
                    f"Repository context:\n{_snapshot_markdown(snapshot)}"
                    f"{extra}"
                ),
            ),
        ],
        temperature=_optional_float(agent.model_settings.get("temperature")),
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        tools_allowed=step.allowed_tools,
        metadata={
            "task_title": task.title,
            "step_name": step.name,
            "agent_id": agent.id,
            "agent_role": agent.role,
            "context_keys": sorted(context.keys()),
            "local_code_executor": True,
            "repository_files": sorted(snapshot.files),
        },
    )


def _snapshot_markdown(snapshot: RepositorySnapshot) -> str:
    parts = [
        f"- Source repository: `{snapshot.source_path}`",
        f"- Isolated workspace: `{snapshot.workspace_path}`",
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
        f"- Task: `{task.title}`\n"
        f"- Source repository: `{snapshot.source_path}`\n"
        f"- Isolated workspace: `{snapshot.workspace_path}`\n"
        "- Original repository modified: `false`\n\n"
        "## Included Repository Files\n\n"
        + "\n".join(f"- `{path}`" for path in sorted(snapshot.files))
        + "\n\n## Skipped Files\n\n"
        + ("\n".join(f"- {item}" for item in snapshot.skipped) if snapshot.skipped else "- None")
        + "\n\n## Model Patch Proposal\n\n"
        + _redact(model_response.text)
        + "\n"
    )


def _test_artifact_content(
    task: Task,
    run: Run,
    snapshot: RepositorySnapshot,
    test_result: TestCommandResult,
    model_response: ModelResponse,
) -> str:
    return (
        "# Local Code Test Report\n\n"
        f"- Run: `{run.id}`\n"
        f"- Task: `{task.title}`\n"
        f"- Source repository: `{snapshot.source_path}`\n"
        f"- Isolated workspace: `{snapshot.workspace_path}`\n"
        "- Original repository modified: `false`\n\n"
        "## Command Result\n\n"
        f"Summary: {test_result.summary}\n\n"
        + test_result.markdown()
        + "\n## Model Test Review\n\n"
        + _redact(model_response.text)
        + "\n"
    )


def _run_test_command(command: Any, workspace_path: Path, timeout_seconds: int) -> TestCommandResult:
    if command is None or command == "":
        return TestCommandResult(command=None, exit_code=None)
    if not isinstance(command, str):
        raise WorkflowRunnerError("test_command must be a string.")
    args = _parse_allowed_test_command(command)
    try:
        completed = subprocess.run(
            args,
            cwd=workspace_path,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_sanitized_environment(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return TestCommandResult(
            command=command,
            exit_code=None,
            stdout=_truncate_output(_redact(exc.stdout or "")),
            stderr=_truncate_output(_redact(exc.stderr or "")),
            timed_out=True,
        )
    return TestCommandResult(
        command=command,
        exit_code=completed.returncode,
        stdout=_truncate_output(_redact(completed.stdout)),
        stderr=_truncate_output(_redact(completed.stderr)),
    )


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

    for argument in pytest_args:
        if _pytest_argument_can_escape_workspace(argument):
            raise WorkflowRunnerError("test_command paths must stay inside the isolated workspace.")
    return [sys.executable, "-m", "pytest", *pytest_args]


def _pytest_argument_can_escape_workspace(argument: str) -> bool:
    value = argument.strip().strip("\"'")
    if value.startswith("@"):
        return True
    if "=" in value:
        value = value.split("=", 1)[1].strip().strip("\"'")
    if not value or ("/" not in value and "\\" not in value and not PureWindowsPath(value).drive):
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


def _sanitized_environment() -> dict[str, str]:
    sanitized = {}
    for key, value in os.environ.items():
        normalized = key.lower()
        if any(marker in normalized for marker in _SENSITIVE_NAME_MARKERS):
            continue
        sanitized[key] = value
    return sanitized


def _truncate_output(value: str) -> str:
    if len(value) <= _MAX_COMMAND_OUTPUT_CHARS:
        return value
    return value[:_MAX_COMMAND_OUTPUT_CHARS] + "\n[truncated]\n"


def _redact(value: str) -> str:
    redacted = value
    redacted = redacted.replace("\x00", "")
    redacted = re.sub(r"(?i)(Authorization\s*:\s*Bearer\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(Bearer\s+)[^\s,;]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(api[_-]?key\s*=\s*)[^\s,;&]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(token\s*=\s*)[^\s,;&]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(secret\s*=\s*)[^\s,;&]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"(?i)(payload\s*=\s*)[^\s,;&]+", r"\1[REDACTED]", redacted)
    redacted = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-[REDACTED]", redacted)
    return redacted


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
