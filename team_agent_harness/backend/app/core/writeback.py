from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
from threading import RLock
from typing import Any

from app.core.artifacts import ArtifactStore
from app.core.local_code_executor import (
    _DEFAULT_TEST_TIMEOUT_SECONDS,
    _EXCLUDED_DIR_NAMES,
    _is_probably_text,
    _is_sensitive_name,
    _positive_int,
    _prepare_workspace,
    _resolve_repository_path,
    _run_test_command,
)
from app.core.models import Artifact, ArtifactType, Run, Task, TraceEventType
from app.core.trace import TraceLogger


class WritebackError(RuntimeError):
    pass


class WritebackConflict(WritebackError):
    pass


_WRITEBACK_APPROVAL_LOCK = RLock()


@dataclass(frozen=True)
class _DiffLine:
    kind: str
    text: str


@dataclass(frozen=True)
class _DiffHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[_DiffLine]


@dataclass(frozen=True)
class _FilePatch:
    path: str
    hunks: list[_DiffHunk]


@dataclass(frozen=True)
class _FileChange:
    path: str
    action: str
    base_hash: str
    new_hash: str
    base_content: str
    new_content: str


@dataclass(frozen=True)
class WritebackPlan:
    writeback_id: str
    patch_artifact_id: str
    patch_hash: str
    repository_path: Path
    files: list[_FileChange]

    @property
    def base_hashes(self) -> dict[str, str]:
        return {change.path: change.base_hash for change in self.files}

    def public_dict(self, *, dry_run_status: str) -> dict[str, Any]:
        return {
            "writeback_id": self.writeback_id,
            "patch_artifact_id": self.patch_artifact_id,
            "patch_hash": self.patch_hash,
            "repository_path": str(self.repository_path),
            "dry_run_status": dry_run_status,
            "files_changed": [
                {
                    "path": change.path,
                    "action": change.action,
                    "base_hash": change.base_hash,
                    "new_hash": change.new_hash,
                }
                for change in self.files
            ],
            "base_hashes": self.base_hashes,
        }


@dataclass(frozen=True)
class _TransactionFile:
    path: str
    base_hash: str
    new_hash: str
    temp_path: str | None


@dataclass(frozen=True)
class _WritebackTransaction:
    version: int
    run_id: str
    task_id: str | None
    patch_artifact_id: str
    writeback_id: str
    patch_hash: str
    repository_path: Path
    state: str
    files: list[_TransactionFile]


class WritebackService:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        trace_logger: TraceLogger,
        workspace_root: str | Path = "output/writeback_workspaces",
    ) -> None:
        self.artifact_store = artifact_store
        self.trace_logger = trace_logger
        self.workspace_root = Path(workspace_root).expanduser().resolve()

    def recover_pending_transactions(self) -> list[str]:
        with _WRITEBACK_APPROVAL_LOCK:
            return self._recover_pending_transactions_locked()

    def preview(self, *, run: Run, task: Task, artifact: Artifact) -> dict[str, Any]:
        with _WRITEBACK_APPROVAL_LOCK:
            plan = self._build_plan(run=run, task=task, artifact=artifact)
            self.trace_logger.record(
                run_id=run.id,
                event_type=TraceEventType.RUNTIME_EVENT,
                payload={
                    "action": "writeback_previewed",
                    "writeback_id": plan.writeback_id,
                    "patch_artifact_id": artifact.id,
                    "patch_hash": plan.patch_hash,
                    "files_changed": [change.path for change in plan.files],
                    "dry_run_status": "ready",
                },
            )
            return plan.public_dict(dry_run_status="ready")

    def approve(
        self,
        *,
        run: Run,
        task: Task,
        artifact: Artifact,
        writeback_id: str,
        confirm_repository_path: str,
        confirm_patch_hash: str,
        expected_base_hashes: dict[str, str],
    ) -> dict[str, Any]:
        source_path = _resolve_repository_path(confirm_repository_path)
        task_source_path = _resolve_repository_path(task.inputs.get("repository_path"))
        if not _same_path(source_path, task_source_path):
            raise WritebackError("confirm_repository_path must match the task repository_path.")

        with _WRITEBACK_APPROVAL_LOCK:
            self._recover_pending_transactions_locked()
            patch_hash, current_writeback_id = self._patch_identity(run=run, artifact=artifact)
            if current_writeback_id != writeback_id:
                raise WritebackConflict("writeback_id does not match the current patch artifact.")
            if patch_hash != confirm_patch_hash:
                raise WritebackConflict("confirm_patch_hash does not match the current patch content.")
            if not expected_base_hashes:
                raise WritebackError("expected_base_hashes is required for writeback approval.")

            completed = self._completed_approval_result(
                run=run,
                artifact=artifact,
                source_path=source_path,
                writeback_id=writeback_id,
                patch_hash=patch_hash,
                expected_base_hashes=expected_base_hashes,
            )
            if completed is not None:
                return completed

            plan = self._build_plan(run=run, task=task, artifact=artifact)
            for change in plan.files:
                if expected_base_hashes.get(change.path) != change.base_hash:
                    raise WritebackConflict(f"Base hash mismatch for {change.path}; original repository changed.")

            workspace_path = self.workspace_root / run.id / plan.writeback_id / "repo"
            _prepare_workspace(source_path, workspace_path)
            self._apply_plan_to_workspace(plan, workspace_path)

            command = task.inputs.get("test_command")
            timeout_seconds = _positive_int(
                task.inputs.get("test_timeout_seconds"),
                default=_DEFAULT_TEST_TIMEOUT_SECONDS,
                maximum=900,
            )
            test_result = _run_test_command(command, workspace_path, timeout_seconds)
            if not test_result.command:
                raise WritebackError("test_command is required before writeback approval.")
            if test_result.command and test_result.exit_code != 0:
                raise WritebackConflict(f"Patched workspace tests did not pass: {test_result.summary}")

            transaction_path, transaction = self._prepare_transaction(run=run, artifact=artifact, plan=plan)
            file_patches = self._validate_transaction_identity(transaction_path, transaction)
            self._validate_transaction_backups(transaction_path, transaction, file_patches)
            try:
                applied_files = self._apply_plan_to_source(plan, transaction)
            except Exception as exc:
                rollback_failures = self._restore_transaction(transaction_path, transaction=transaction)
                if rollback_failures:
                    raise WritebackError(
                        "Writeback failed and rollback also failed for: "
                        + ", ".join(sorted(rollback_failures))
                    ) from exc
                transaction = self._set_transaction_state(transaction_path, transaction, "rolled_back")
                try:
                    _discard_transaction(transaction_path)
                except WritebackError as cleanup_exc:
                    raise WritebackError("Writeback rolled back but transaction cleanup failed.") from cleanup_exc
                raise WritebackError("Writeback failed; all applied files were rolled back.") from exc

            result = _approval_result(plan, artifact, applied_files, test_result)
            try:
                self.trace_logger.record(
                    run_id=run.id,
                    event_type=TraceEventType.RUNTIME_EVENT,
                    payload={
                        "action": "writeback_applied",
                        "writeback_id": plan.writeback_id,
                        "patch_artifact_id": artifact.id,
                        "patch_hash": plan.patch_hash,
                        "files_changed": applied_files,
                        "base_hashes": plan.base_hashes,
                        "new_hashes": {change.path: change.new_hash for change in plan.files},
                        "test_command": test_result.command,
                        "test_exit_code": test_result.exit_code,
                        "test_timed_out": test_result.timed_out,
                        "test_summary": test_result.summary,
                    },
                )
            except Exception as exc:
                rollback_failures = self._restore_transaction(transaction_path, transaction=transaction)
                if rollback_failures:
                    raise WritebackError(
                        "Writeback audit persistence failed and rollback also failed for: "
                        + ", ".join(sorted(rollback_failures))
                    ) from exc
                transaction = self._set_transaction_state(transaction_path, transaction, "rolled_back")
                try:
                    _discard_transaction(transaction_path)
                except WritebackError as cleanup_exc:
                    raise WritebackError(
                        "Writeback audit persistence failed and rollback cleanup also failed."
                    ) from cleanup_exc
                raise WritebackError(
                    "Writeback audit persistence failed; all applied files were rolled back."
                ) from exc
            try:
                transaction = self._set_transaction_state(transaction_path, transaction, "committed")
                _discard_transaction(transaction_path)
            except WritebackError as exc:
                raise WritebackError("Writeback committed but transaction cleanup is pending.") from exc
            return result

    def _patch_identity(self, *, run: Run, artifact: Artifact) -> tuple[str, str]:
        _validate_patch_artifact(run, artifact)
        artifact_content = self.artifact_store.read_text(artifact)
        diff_text = _extract_unified_diff(artifact_content)
        patch_hash = sha256(diff_text.encode("utf-8")).hexdigest()
        writeback_id = sha256(f"{run.id}:{artifact.id}:{patch_hash}".encode("utf-8")).hexdigest()[:24]
        return patch_hash, writeback_id

    def _completed_approval_result(
        self,
        *,
        run: Run,
        artifact: Artifact,
        source_path: Path,
        writeback_id: str,
        patch_hash: str,
        expected_base_hashes: dict[str, str],
    ) -> dict[str, Any] | None:
        for event in reversed(self.trace_logger.list_for_run(run.id)):
            payload = event.payload
            if not (
                payload.get("action") == "writeback_applied"
                and payload.get("writeback_id") == writeback_id
                and payload.get("patch_artifact_id") == artifact.id
                and payload.get("patch_hash") == patch_hash
            ):
                continue
            base_hashes = payload.get("base_hashes")
            new_hashes = payload.get("new_hashes")
            files_changed = payload.get("files_changed")
            if not isinstance(base_hashes, dict) or not isinstance(new_hashes, dict) or not isinstance(files_changed, list):
                return None
            for path, base_hash in base_hashes.items():
                if expected_base_hashes.get(path) != base_hash:
                    raise WritebackConflict(f"Base hash mismatch for {path}; approval does not match completed writeback.")
            for path, new_hash in new_hashes.items():
                target = _safe_repo_file(source_path, path, focus_paths=None)
                if _content_hash(_read_utf8_exact(target)) != new_hash:
                    raise WritebackConflict(f"Applied file changed after writeback: {path}")
            return {
                "writeback_id": writeback_id,
                "patch_artifact_id": artifact.id,
                "patch_hash": patch_hash,
                "repository_path": str(source_path),
                "applied_files": files_changed,
                "test": {
                    "command": payload.get("test_command"),
                    "exit_code": payload.get("test_exit_code"),
                    "timed_out": bool(payload.get("test_timed_out", False)),
                    "summary": payload.get("test_summary", "Writeback tests passed."),
                },
                "original_repository_modified": True,
            }
        return None

    def _build_plan(self, *, run: Run, task: Task, artifact: Artifact) -> WritebackPlan:
        _validate_patch_artifact(run, artifact)
        source_path = _resolve_repository_path(task.inputs.get("repository_path"))
        artifact_content = self.artifact_store.read_text(artifact)
        diff_text = _extract_unified_diff(artifact_content)
        patch_hash = sha256(diff_text.encode("utf-8")).hexdigest()
        file_patches = _parse_unified_diff(diff_text)
        if not file_patches:
            raise WritebackError("No supported file patches were found in the unified diff.")

        focus_paths = task.inputs.get("focus_paths")
        changes = [
            _build_file_change(source_path, file_patch, focus_paths)
            for file_patch in file_patches
        ]
        if not changes:
            raise WritebackError("Patch does not change any supported files.")
        _reject_duplicate_file_changes(source_path, changes)
        writeback_id = sha256(f"{run.id}:{artifact.id}:{patch_hash}".encode("utf-8")).hexdigest()[:24]
        return WritebackPlan(
            writeback_id=writeback_id,
            patch_artifact_id=artifact.id,
            patch_hash=patch_hash,
            repository_path=source_path,
            files=changes,
        )

    def _apply_plan_to_workspace(self, plan: WritebackPlan, workspace_path: Path) -> None:
        for change in plan.files:
            target = _safe_repo_file(workspace_path, change.path, focus_paths=None)
            target.write_text(change.new_content, encoding="utf-8", newline="")

    def _apply_plan_to_source(
        self,
        plan: WritebackPlan,
        transaction: _WritebackTransaction,
    ) -> list[str]:
        transaction_files = {file.path: file for file in transaction.files}
        applied: list[str] = []
        for change in plan.files:
            target = _safe_repo_file(plan.repository_path, change.path, focus_paths=None)
            current_hash = _bytes_hash(target.read_bytes())
            if current_hash != change.base_hash:
                raise WritebackConflict(f"Base hash mismatch for {change.path}; original repository changed.")
            transaction_file = transaction_files[change.path]
            temp_path = _owned_source_temp_path(plan.repository_path, transaction, transaction_file)
            _replace_with_owned_temp(
                target,
                change.new_content.encode("utf-8"),
                temp_path,
                expected_current_hash=change.base_hash,
            )
            applied.append(change.path)
        return applied

    def _prepare_transaction(
        self,
        *,
        run: Run,
        artifact: Artifact,
        plan: WritebackPlan,
    ) -> tuple[Path, _WritebackTransaction]:
        transaction_path = self._transaction_path(plan.writeback_id)
        preparing_path = transaction_path.with_name(f".{transaction_path.name}.preparing")
        _discard_transaction(preparing_path)
        if transaction_path.exists():
            raise WritebackError(f"Pending writeback transaction already exists: {plan.writeback_id}")

        transaction = _WritebackTransaction(
            version=2,
            run_id=run.id,
            task_id=run.task_id,
            patch_artifact_id=artifact.id,
            writeback_id=plan.writeback_id,
            patch_hash=plan.patch_hash,
            repository_path=plan.repository_path,
            state="prepared",
            files=_transaction_files_for_plan(plan),
        )
        backup_root = preparing_path / "base"
        for change in plan.files:
            backup_path = _transaction_backup_path(backup_root, change.path)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_bytes(backup_path, change.base_content.encode("utf-8"))
        _write_transaction_journal(preparing_path, transaction)
        transaction_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(preparing_path, transaction_path)
        _sync_directory(transaction_path.parent)
        return transaction_path, transaction

    def _recover_pending_transactions_locked(self) -> list[str]:
        transaction_root = self.workspace_root / "_transactions"
        if not transaction_root.exists():
            return []
        if _is_reparse_point(transaction_root):
            raise WritebackError("Writeback transaction root must not be a reparse point.")

        journal_paths: list[Path] = []
        for transaction_path in sorted(transaction_root.iterdir(), key=lambda path: path.name):
            if re.fullmatch(r"\.[0-9a-f]{24}\.preparing", transaction_path.name):
                _discard_transaction(transaction_path)
                continue
            if transaction_path.name == ".writeback.lock":
                continue
            if not re.fullmatch(r"[0-9a-f]{24}", transaction_path.name):
                raise WritebackError(f"Unexpected entry in writeback transaction root: {transaction_path.name}")
            if _is_reparse_point(transaction_path) or not transaction_path.is_dir():
                raise WritebackError(f"Pending writeback transaction path is invalid: {transaction_path.name}")
            journal_path = transaction_path / "journal.json"
            if not journal_path.is_file() or _is_reparse_point(journal_path):
                raise WritebackError(f"Pending writeback transaction has no valid journal: {transaction_path.name}")
            journal_paths.append(journal_path)

        recovered: list[str] = []
        for journal_path in journal_paths:
            transaction_path = journal_path.parent
            transaction = _load_transaction(journal_path)
            file_patches = self._validate_transaction_identity(transaction_path, transaction)
            target_hashes = self._transaction_target_hashes(transaction)
            unknown_files = [
                file.path
                for file in transaction.files
                if target_hashes[file.path] not in {file.base_hash, file.new_hash}
            ]
            if unknown_files:
                raise WritebackError(
                    "Pending writeback recovery found files changed outside the transaction: "
                    + ", ".join(sorted(unknown_files))
                )
            all_base = all(target_hashes[file.path] == file.base_hash for file in transaction.files)
            all_new = all(target_hashes[file.path] == file.new_hash for file in transaction.files)
            if transaction.state == "rolled_back" and all_base:
                self._remove_transaction_temps(transaction)
                try:
                    _discard_transaction(transaction_path)
                except WritebackError as exc:
                    raise WritebackError("Rolled-back writeback transaction cleanup failed.") from exc
                recovered.append(transaction.writeback_id)
                continue
            if self._has_completed_transaction_trace(transaction) and all_new:
                transaction = self._set_transaction_state(transaction_path, transaction, "committed")
                self._remove_transaction_temps(transaction)
                try:
                    _discard_transaction(transaction_path)
                except WritebackError as exc:
                    raise WritebackError("Committed writeback transaction cleanup failed.") from exc
                continue
            self._validate_transaction_backups(transaction_path, transaction, file_patches)
            rollback_failures = self._restore_transaction(transaction_path, transaction=transaction)
            if rollback_failures:
                raise WritebackError(
                    "Pending writeback recovery could not restore: "
                    + ", ".join(sorted(rollback_failures))
                )
            transaction = self._set_transaction_state(transaction_path, transaction, "rolled_back")
            try:
                _discard_transaction(transaction_path)
            except WritebackError as exc:
                raise WritebackError("Rolled-back writeback transaction cleanup failed.") from exc
            recovered.append(transaction.writeback_id)
        return recovered

    def _set_transaction_state(
        self,
        transaction_path: Path,
        transaction: _WritebackTransaction,
        state: str,
    ) -> _WritebackTransaction:
        if transaction.version != 2:
            return transaction
        if transaction.state == state:
            return transaction
        updated = replace(transaction, state=state)
        _write_transaction_journal(transaction_path, updated)
        return updated

    def _validate_transaction_identity(
        self,
        transaction_path: Path,
        transaction: _WritebackTransaction,
    ) -> list[_FilePatch]:
        expected_root = self.workspace_root / "_transactions"
        if _is_reparse_point(expected_root) or _is_reparse_point(transaction_path):
            raise WritebackError("Pending writeback transaction path must not be a reparse point.")
        if transaction_path.parent.resolve() != expected_root.resolve():
            raise WritebackError("Pending writeback transaction is outside the transaction root.")
        if transaction_path.name != transaction.writeback_id:
            raise WritebackError("Pending writeback directory does not match its writeback_id.")

        storage = self.artifact_store.storage
        run = storage.get_run(transaction.run_id)
        if run is None:
            raise WritebackError("Pending writeback run is not present in SQLite.")
        if transaction.task_id is not None and transaction.task_id != run.task_id:
            raise WritebackError("Pending writeback task_id does not match its run.")
        task = storage.get_task(run.task_id)
        if task is None:
            raise WritebackError("Pending writeback task is not present in SQLite.")
        repository_path = _resolve_repository_path(task.inputs.get("repository_path"))
        if not _same_path(repository_path, transaction.repository_path):
            raise WritebackError("Pending writeback repository does not match its task.")

        artifact = storage.get_artifact(transaction.patch_artifact_id)
        if artifact is None or artifact.run_id != run.id or artifact.type != ArtifactType.PATCH:
            raise WritebackError("Pending writeback patch artifact does not match its run.")
        artifact_content = self.artifact_store.read_text(artifact)
        artifact_hash = sha256(artifact_content.encode("utf-8")).hexdigest()
        if artifact.content_hash is None or artifact_hash != artifact.content_hash:
            raise WritebackError("Pending writeback patch artifact content hash does not match SQLite.")
        diff_text = _extract_unified_diff(artifact_content)
        patch_hash = sha256(diff_text.encode("utf-8")).hexdigest()
        if patch_hash != transaction.patch_hash:
            raise WritebackError("Pending writeback patch hash does not match its artifact.")
        writeback_id = sha256(f"{run.id}:{artifact.id}:{patch_hash}".encode("utf-8")).hexdigest()[:24]
        if writeback_id != transaction.writeback_id:
            raise WritebackError("Pending writeback_id does not match its run and artifact.")

        file_patches = _parse_unified_diff(diff_text)
        journal_paths = [os.path.normcase(file.path) for file in transaction.files]
        artifact_paths = [os.path.normcase(file_patch.path) for file_patch in file_patches]
        if journal_paths != artifact_paths:
            raise WritebackError("Pending writeback file list does not match its patch artifact.")
        for file in transaction.files:
            if file.temp_path is not None:
                _owned_source_temp_path(transaction.repository_path, transaction, file)
        return file_patches

    def _validate_transaction_backups(
        self,
        transaction_path: Path,
        transaction: _WritebackTransaction,
        file_patches: list[_FilePatch],
    ) -> None:
        backup_root = transaction_path / "base"
        if _is_reparse_point(backup_root):
            raise WritebackError("Pending writeback backup root must not be a reparse point.")
        for file, file_patch in zip(transaction.files, file_patches, strict=True):
            backup_path = _transaction_backup_path(backup_root, file.path)
            backup = backup_path.read_bytes()
            if _bytes_hash(backup) != file.base_hash:
                raise WritebackError(f"Pending writeback backup hash mismatch: {file.path}")
            try:
                base_content = backup.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WritebackError(f"Pending writeback backup is not UTF-8 text: {file.path}") from exc
            expected_new_content = _apply_file_patch(file_patch, base_content)
            if _content_hash(expected_new_content) != file.new_hash:
                raise WritebackError(f"Pending writeback new hash does not match its patch: {file.path}")

    def _has_completed_transaction_trace(self, transaction: _WritebackTransaction) -> bool:
        expected_base_hashes = {file.path: file.base_hash for file in transaction.files}
        expected_new_hashes = {file.path: file.new_hash for file in transaction.files}
        expected_files = [file.path for file in transaction.files]
        return any(
            event.payload.get("action") == "writeback_applied"
            and event.payload.get("writeback_id") == transaction.writeback_id
            and event.payload.get("patch_artifact_id") == transaction.patch_artifact_id
            and event.payload.get("patch_hash") == transaction.patch_hash
            and event.payload.get("base_hashes") == expected_base_hashes
            and event.payload.get("new_hashes") == expected_new_hashes
            and event.payload.get("files_changed") == expected_files
            for event in self.trace_logger.list_for_run(transaction.run_id)
        )

    def _transaction_target_hashes(self, transaction: _WritebackTransaction) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for file in transaction.files:
            try:
                target = _safe_repo_file(transaction.repository_path, file.path, focus_paths=None)
                hashes[file.path] = _bytes_hash(target.read_bytes())
            except Exception as exc:
                raise WritebackError(f"Pending writeback target could not be inspected: {file.path}") from exc
        return hashes

    def _remove_transaction_temps(self, transaction: _WritebackTransaction) -> None:
        failures: list[str] = []
        for file in transaction.files:
            if file.temp_path is None:
                continue
            try:
                _remove_owned_source_temp(transaction, file)
            except Exception:
                failures.append(file.path)
        if failures:
            raise WritebackError(
                "Pending writeback temporary files could not be removed: " + ", ".join(sorted(failures))
            )

    def _restore_transaction(
        self,
        transaction_path: Path,
        *,
        transaction: _WritebackTransaction | None = None,
    ) -> list[str]:
        transaction = transaction or _load_transaction(transaction_path / "journal.json")
        failures: list[str] = []
        backup_root = transaction_path / "base"
        for file in reversed(transaction.files):
            try:
                if file.temp_path is not None:
                    _remove_owned_source_temp(transaction, file)
                target = _safe_repo_file(transaction.repository_path, file.path, focus_paths=None)
                current_hash = _bytes_hash(target.read_bytes())
                if current_hash == file.base_hash:
                    continue
                if current_hash != file.new_hash:
                    raise WritebackConflict(f"Applied file changed before rollback: {file.path}")
                backup_path = _transaction_backup_path(backup_root, file.path)
                backup = backup_path.read_bytes()
                if _bytes_hash(backup) != file.base_hash:
                    raise WritebackConflict(f"Writeback backup hash mismatch: {file.path}")
                if file.temp_path is None:
                    _atomic_write_bytes(target, backup)
                else:
                    temp_path = _owned_source_temp_path(transaction.repository_path, transaction, file)
                    _replace_with_owned_temp(
                        target,
                        backup,
                        temp_path,
                        expected_current_hash=file.new_hash,
                    )
            except Exception:
                failures.append(file.path)
        return failures

    def _transaction_path(self, writeback_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{24}", writeback_id):
            raise WritebackError("writeback_id has an invalid transaction identifier.")
        return self.workspace_root / "_transactions" / writeback_id


def _validate_patch_artifact(run: Run, artifact: Artifact) -> None:
    if artifact.run_id != run.id:
        raise WritebackError("Patch artifact does not belong to the run.")
    if artifact.type != ArtifactType.PATCH:
        raise WritebackError("writeback requires a patch artifact.")


def _extract_unified_diff(content: str) -> str:
    fenced = re.findall(r"```(?:diff|patch)\s*\n(.*?)```", content, flags=re.IGNORECASE | re.DOTALL)
    for candidate in fenced:
        diff = candidate.strip()
        if not diff:
            continue
        if "diff --git " in diff or ("\n--- " in f"\n{diff}" and "\n+++ " in f"\n{diff}"):
            return diff + "\n"
    raise WritebackError("Patch artifact does not contain a fenced unified diff.")


def _parse_unified_diff(diff_text: str) -> list[_FilePatch]:
    lines = diff_text.splitlines(keepends=True)
    patches: list[_FilePatch] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            index += 1
            continue
        if not line.startswith("--- "):
            index += 1
            continue

        old_path_raw = _header_path(line, "---")
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise WritebackError("Unified diff file header is incomplete.")
        new_path_raw = _header_path(lines[index], "+++")
        index += 1
        path = _normalized_patch_path(old_path_raw, new_path_raw)
        hunks: list[_DiffHunk] = []

        while index < len(lines):
            line = lines[index]
            if line.startswith("diff --git ") or line.startswith("--- "):
                break
            if not line.startswith("@@ "):
                if line.startswith(("index ", "old mode ", "new mode ")):
                    index += 1
                    continue
                if line.startswith(("new file mode ", "deleted file mode ", "rename from ", "rename to ")):
                    raise WritebackError("Create, delete, and rename patches are not supported.")
                index += 1
                continue
            hunk, index = _parse_hunk(lines, index)
            hunks.append(hunk)

        if not hunks:
            raise WritebackError(f"Patch for {path} does not contain any hunks.")
        patches.append(_FilePatch(path=path, hunks=hunks))
    return patches


def _header_path(line: str, marker: str) -> str:
    value = line[len(marker) :].strip()
    if "\t" in value:
        value = value.split("\t", 1)[0]
    if " " in value:
        value = value.split(" ", 1)[0]
    return value


def _normalized_patch_path(old_path_raw: str, new_path_raw: str) -> str:
    if old_path_raw == "/dev/null" or new_path_raw == "/dev/null":
        raise WritebackError("Create and delete patches are not supported.")
    old_path = _strip_diff_prefix(old_path_raw)
    new_path = _strip_diff_prefix(new_path_raw)
    if old_path != new_path:
        raise WritebackError("Rename patches are not supported.")
    return _validate_relative_patch_path(new_path)


def _strip_diff_prefix(value: str) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith("a/") or normalized.startswith("b/"):
        return normalized[2:]
    return normalized


def _validate_relative_patch_path(value: str) -> str:
    if not value or value in {".", ".."}:
        raise WritebackError("Patch path must be a non-empty relative path.")
    if re.match(r"^[A-Za-z]:/", value) or value.startswith("/"):
        raise WritebackError("Patch paths must be relative.")
    path = PurePosixPath(value)
    parts = path.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise WritebackError("Patch paths must stay inside the repository.")
    if any(part.lower() in _EXCLUDED_DIR_NAMES for part in parts[:-1]):
        raise WritebackError(f"Patch path is inside an excluded directory: {value}")
    if any(_is_sensitive_name(part) for part in parts):
        raise WritebackError(f"Patch path looks sensitive and cannot be written: {value}")
    return path.as_posix()


def _parse_hunk(lines: list[str], start_index: int) -> tuple[_DiffHunk, int]:
    header = lines[start_index]
    match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", header)
    if match is None:
        raise WritebackError("Unified diff hunk header is invalid.")
    old_start = int(match.group(1))
    old_count = int(match.group(2) or "1")
    new_start = int(match.group(3))
    new_count = int(match.group(4) or "1")
    index = start_index + 1
    hunk_lines: list[_DiffLine] = []
    while index < len(lines):
        line = lines[index]
        if line.startswith("@@ ") or line.startswith("diff --git ") or line.startswith("--- "):
            break
        if line.startswith("\\ No newline at end of file"):
            index += 1
            continue
        if not line or line[0] not in {" ", "+", "-"}:
            raise WritebackError("Unified diff hunk line is invalid.")
        hunk_lines.append(_DiffLine(kind=line[0], text=line[1:]))
        index += 1

    actual_old_count = sum(1 for line in hunk_lines if line.kind in {" ", "-"})
    actual_new_count = sum(1 for line in hunk_lines if line.kind in {" ", "+"})
    if actual_old_count != old_count or actual_new_count != new_count:
        raise WritebackError("Unified diff hunk line counts do not match the header.")
    return (
        _DiffHunk(
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            lines=hunk_lines,
        ),
        index,
    )


def _build_file_change(source_path: Path, file_patch: _FilePatch, focus_paths: Any) -> _FileChange:
    target = _safe_repo_file(source_path, file_patch.path, focus_paths=focus_paths)
    original = _read_utf8_exact(target)
    new_content = _apply_file_patch(file_patch, original)
    if new_content == original:
        raise WritebackError(f"Patch for {file_patch.path} does not change content.")
    if "\x00" in new_content:
        raise WritebackError(f"Patch for {file_patch.path} contains NUL bytes and is not supported text.")
    return _FileChange(
        path=file_patch.path,
        action="modify",
        base_hash=_content_hash(original),
        new_hash=_content_hash(new_content),
        base_content=original,
        new_content=new_content,
    )


def _safe_repo_file(source_path: Path, rel_path: str, focus_paths: Any) -> Path:
    rel_path = _validate_relative_patch_path(rel_path)
    if focus_paths is not None and not _path_allowed_by_focus(rel_path, focus_paths, source_path):
        raise WritebackError(f"Patch path is outside focus_paths: {rel_path}")
    unresolved_target = source_path.resolve()
    for part in PurePosixPath(rel_path).parts:
        unresolved_target = unresolved_target / part
        if _is_reparse_point(unresolved_target):
            raise WritebackError(f"Patch target must not be a symlink or reparse point: {rel_path}")
    target = unresolved_target.resolve()
    try:
        target.relative_to(source_path.resolve())
    except ValueError as exc:
        raise WritebackError("Patch target must stay inside repository_path.") from exc
    if not target.exists() or not target.is_file():
        raise WritebackError(f"Patch target must be an existing file: {rel_path}")
    try:
        if target.stat().st_nlink != 1:
            raise WritebackError(f"Patch target must not be a hard link: {rel_path}")
    except OSError as exc:
        raise WritebackError(f"Patch target could not be inspected safely: {rel_path}") from exc
    if not _is_probably_text(target):
        raise WritebackError(f"Patch target must be a supported text file: {rel_path}")
    return target


def _reject_duplicate_file_changes(source_path: Path, changes: list[_FileChange]) -> None:
    seen_paths: set[str] = set()
    seen_files: set[tuple[int, int]] = set()
    for change in changes:
        target = _safe_repo_file(source_path, change.path, focus_paths=None)
        path_key = os.path.normcase(str(target.resolve()))
        file_stat = target.stat()
        file_key = (file_stat.st_dev, file_stat.st_ino)
        if path_key in seen_paths or file_key in seen_files:
            raise WritebackError(f"Patch contains duplicate entries for the same file: {change.path}")
        seen_paths.add(path_key)
        seen_files.add(file_key)


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _path_allowed_by_focus(rel_path: str, focus_paths: Any, source_path: Path) -> bool:
    if not isinstance(focus_paths, list):
        raise WritebackError("focus_paths must be a list of relative paths.")
    for raw_focus_path in focus_paths:
        if not isinstance(raw_focus_path, str) or not raw_focus_path.strip():
            raise WritebackError("focus_paths must contain non-empty relative paths.")
        focus = _validate_relative_patch_path(raw_focus_path.replace("\\", "/"))
        focus_target = (source_path / Path(*PurePosixPath(focus).parts)).resolve()
        try:
            focus_target.relative_to(source_path.resolve())
        except ValueError as exc:
            raise WritebackError("focus_paths must stay inside repository_path.") from exc
        if focus_target.is_file() and rel_path == focus:
            return True
        if focus_target.is_dir() and (rel_path == focus or rel_path.startswith(f"{focus}/")):
            return True
        if not focus_target.exists() and (rel_path == focus or rel_path.startswith(f"{focus}/")):
            return True
    return False


def _apply_file_patch(file_patch: _FilePatch, original: str) -> str:
    original_lines = original.splitlines(keepends=True)
    output: list[str] = []
    cursor = 0
    for hunk in file_patch.hunks:
        target_index = hunk.old_start - 1
        if target_index < cursor or target_index > len(original_lines):
            raise WritebackConflict(f"Patch does not apply cleanly to {file_patch.path}.")
        output.extend(original_lines[cursor:target_index])
        cursor = target_index
        for diff_line in hunk.lines:
            if diff_line.kind == " ":
                _assert_source_line(file_patch.path, original_lines, cursor, diff_line.text)
                output.append(original_lines[cursor])
                cursor += 1
            elif diff_line.kind == "-":
                _assert_source_line(file_patch.path, original_lines, cursor, diff_line.text)
                cursor += 1
            elif diff_line.kind == "+":
                output.append(diff_line.text)
            else:
                raise WritebackError("Unsupported diff line kind.")
    output.extend(original_lines[cursor:])
    return "".join(output)


def _assert_source_line(path: str, original_lines: list[str], cursor: int, expected: str) -> None:
    if cursor >= len(original_lines):
        raise WritebackConflict(f"Patch does not apply cleanly to {path}.")
    if _line_body(original_lines[cursor]) != _line_body(expected):
        raise WritebackConflict(f"Patch does not apply cleanly to {path}.")


def _line_body(value: str) -> str:
    return value.rstrip("\r\n")


def _content_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def _bytes_hash(content: bytes) -> str:
    return sha256(content).hexdigest()


def _approval_result(plan: WritebackPlan, artifact: Artifact, applied_files: list[str], test_result: Any) -> dict[str, Any]:
    return {
        "writeback_id": plan.writeback_id,
        "patch_artifact_id": artifact.id,
        "patch_hash": plan.patch_hash,
        "repository_path": str(plan.repository_path),
        "applied_files": applied_files,
        "test": {
            "command": test_result.command,
            "exit_code": test_result.exit_code,
            "timed_out": test_result.timed_out,
            "summary": test_result.summary,
        },
        "original_repository_modified": True,
    }


def _read_utf8_exact(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WritebackError(f"Patch target must be UTF-8 text: {path.name}") from exc


def _atomic_write_text(path: Path, content: str) -> None:
    _atomic_write_bytes(path, content.encode("utf-8"))


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        temp_path = path.with_name(f".writeback-internal-{secrets.token_hex(16)}.tmp")
        try:
            _write_exclusive_file(temp_path, content)
        except FileExistsError:
            continue
        try:
            os.replace(temp_path, path)
            _sync_directory(path.parent)
            return
        except Exception:
            _unlink_file_path(temp_path)
            raise
    raise WritebackError(f"Could not allocate an exclusive temporary file for {path.name}.")


def _replace_with_owned_temp(
    path: Path,
    content: bytes,
    temp_path: Path,
    *,
    expected_current_hash: str,
) -> None:
    try:
        _write_exclusive_file(temp_path, content)
    except FileExistsError as exc:
        raise WritebackConflict(f"Writeback-owned temporary path already exists: {temp_path.name}") from exc
    try:
        if _bytes_hash(path.read_bytes()) != expected_current_hash:
            raise WritebackConflict(f"Source file changed immediately before replacement: {path.name}")
        os.replace(temp_path, path)
        _sync_directory(path.parent)
    except Exception:
        _unlink_file_path(temp_path)
        raise


def _write_exclusive_file(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _unlink_file_path(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        path.unlink()
    except OSError:
        return


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _transaction_files_for_plan(plan: WritebackPlan) -> list[_TransactionFile]:
    reserved = {
        os.path.normcase(change.path.replace("\\", "/"))
        for change in plan.files
    }
    files: list[_TransactionFile] = []
    for change in plan.files:
        parent = PurePosixPath(change.path).parent
        for _ in range(16):
            name = f".writeback-{plan.writeback_id}-{secrets.token_hex(8)}.tmp"
            candidate = (parent / name).as_posix()
            candidate_key = os.path.normcase(candidate)
            candidate_path = plan.repository_path.joinpath(*PurePosixPath(candidate).parts)
            if candidate_key in reserved or os.path.lexists(candidate_path):
                continue
            reserved.add(candidate_key)
            files.append(
                _TransactionFile(
                    path=change.path,
                    base_hash=change.base_hash,
                    new_hash=change.new_hash,
                    temp_path=candidate,
                )
            )
            break
        else:
            raise WritebackError(f"Could not reserve a writeback temporary path for {change.path}.")
    return files


def _owned_source_temp_path(
    repository_path: Path,
    transaction: _WritebackTransaction,
    file: _TransactionFile,
) -> Path:
    if file.temp_path is None:
        raise WritebackError(f"Writeback journal has no owned temporary path for {file.path}.")
    temp_path = _validate_relative_patch_path(file.temp_path)
    relative_temp = PurePosixPath(temp_path)
    relative_target = PurePosixPath(file.path)
    if relative_temp.parent != relative_target.parent:
        raise WritebackError(f"Writeback temporary path is not beside its target: {file.path}")
    expected_name = re.fullmatch(
        rf"\.writeback-{re.escape(transaction.writeback_id)}-[0-9a-f]{{16}}\.tmp",
        relative_temp.name,
    )
    if expected_name is None:
        raise WritebackError(f"Writeback journal has an invalid temporary path for {file.path}.")

    root = repository_path.resolve()
    parent = root
    for part in relative_temp.parent.parts:
        if part in {"", "."}:
            continue
        parent = parent / part
        if _is_reparse_point(parent):
            raise WritebackError(f"Writeback temporary parent must not be a reparse point: {file.path}")
    resolved_parent = parent.resolve()
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise WritebackError("Writeback temporary path must stay inside repository_path.") from exc
    return resolved_parent / relative_temp.name


def _remove_owned_source_temp(transaction: _WritebackTransaction, file: _TransactionFile) -> None:
    temp_path = _owned_source_temp_path(transaction.repository_path, transaction, file)
    if not os.path.lexists(temp_path):
        return
    if temp_path.is_dir() or (not temp_path.is_symlink() and _is_reparse_point(temp_path)):
        raise WritebackError(f"Writeback temporary path is not a removable file: {file.path}")
    temp_path.unlink()


def _write_transaction_journal(transaction_path: Path, transaction: _WritebackTransaction) -> None:
    journal = {
        "version": transaction.version,
        "state": transaction.state,
        "run_id": transaction.run_id,
        "task_id": transaction.task_id,
        "patch_artifact_id": transaction.patch_artifact_id,
        "writeback_id": transaction.writeback_id,
        "patch_hash": transaction.patch_hash,
        "repository_path": str(transaction.repository_path),
        "files": [
            {
                "path": file.path,
                "base_hash": file.base_hash,
                "new_hash": file.new_hash,
                "temp_path": file.temp_path,
            }
            for file in transaction.files
        ],
    }
    _atomic_write_bytes(
        transaction_path / "journal.json",
        json.dumps(journal, ensure_ascii=True, sort_keys=True).encode("utf-8"),
    )


def _transaction_backup_path(backup_root: Path, rel_path: str) -> Path:
    validated = _validate_relative_patch_path(rel_path)
    target = backup_root.joinpath(*PurePosixPath(validated).parts).resolve()
    try:
        target.relative_to(backup_root.resolve())
    except ValueError as exc:
        raise WritebackError("Writeback backup path must stay inside its transaction.") from exc
    return target


def _load_transaction(journal_path: Path) -> _WritebackTransaction:
    try:
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WritebackError(f"Pending writeback journal is unreadable: {journal_path.parent.name}") from exc
    if not isinstance(payload, dict) or payload.get("version") not in {1, 2}:
        raise WritebackError("Pending writeback journal has an unsupported format.")
    version = payload["version"]

    required_strings = {
        name: payload.get(name)
        for name in ("run_id", "patch_artifact_id", "writeback_id", "patch_hash", "repository_path")
    }
    if any(not isinstance(value, str) or not value for value in required_strings.values()):
        raise WritebackError("Pending writeback journal is missing required fields.")
    if not re.fullmatch(r"[0-9a-f]{24}", required_strings["writeback_id"]):
        raise WritebackError("Pending writeback journal has an invalid writeback_id.")
    if not re.fullmatch(r"[0-9a-f]{64}", required_strings["patch_hash"]):
        raise WritebackError("Pending writeback journal has an invalid patch hash.")
    task_id = payload.get("task_id") if version == 2 else None
    if version == 2 and (not isinstance(task_id, str) or not task_id):
        raise WritebackError("Pending writeback journal has an invalid task_id.")
    state = payload.get("state", "prepared") if version == 2 else "prepared"
    if state not in {"prepared", "committed", "rolled_back"}:
        raise WritebackError("Pending writeback journal has an invalid state.")

    raw_files = payload.get("files")
    if not isinstance(raw_files, list) or not raw_files or len(raw_files) > 20_000:
        raise WritebackError("Pending writeback journal has an invalid file list.")
    files: list[_TransactionFile] = []
    seen_paths: set[str] = set()
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise WritebackError("Pending writeback journal contains an invalid file entry.")
        path = raw_file.get("path")
        base_hash = raw_file.get("base_hash")
        new_hash = raw_file.get("new_hash")
        temp_path = raw_file.get("temp_path") if version == 2 else None
        if not isinstance(path, str):
            raise WritebackError("Pending writeback journal contains an invalid file path.")
        path = _validate_relative_patch_path(path)
        path_key = os.path.normcase(path)
        if path_key in seen_paths:
            raise WritebackError(f"Pending writeback journal repeats file path: {path}")
        if not isinstance(base_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", base_hash):
            raise WritebackError(f"Pending writeback journal has an invalid base hash: {path}")
        if not isinstance(new_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", new_hash):
            raise WritebackError(f"Pending writeback journal has an invalid new hash: {path}")
        if version == 2 and not isinstance(temp_path, str):
            raise WritebackError(f"Pending writeback journal has an invalid temporary path: {path}")
        seen_paths.add(path_key)
        files.append(_TransactionFile(path=path, base_hash=base_hash, new_hash=new_hash, temp_path=temp_path))

    try:
        repository_path = _resolve_repository_path(required_strings["repository_path"])
    except Exception as exc:
        raise WritebackError("Pending writeback repository is unavailable.") from exc
    return _WritebackTransaction(
        version=version,
        run_id=required_strings["run_id"],
        task_id=task_id,
        patch_artifact_id=required_strings["patch_artifact_id"],
        writeback_id=required_strings["writeback_id"],
        patch_hash=required_strings["patch_hash"],
        repository_path=repository_path,
        state=state,
        files=files,
    )


def _discard_transaction(path: Path) -> None:
    if not os.path.lexists(path):
        return
    try:
        if _is_reparse_point(path):
            raise WritebackError(f"Writeback transaction path is a reparse point: {path.name}")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except WritebackError:
        raise
    except OSError as exc:
        raise WritebackError(f"Writeback transaction cleanup failed: {path.name}") from exc
    if os.path.lexists(path):
        raise WritebackError(f"Writeback transaction cleanup did not remove: {path.name}")
    _sync_directory(path.parent)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
