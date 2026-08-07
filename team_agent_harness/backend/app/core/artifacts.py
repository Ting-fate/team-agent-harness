from __future__ import annotations

import codecs
from hashlib import sha256
import os
from pathlib import Path
import stat

from app.core.models import Artifact, ArtifactType, TraceEventType
from app.core.storage import SQLiteStorage
from app.core.trace import TraceLogger


class ArtifactStoreError(RuntimeError):
    pass


class ArtifactStore:
    def __init__(self, root_dir: str | Path, storage: SQLiteStorage, trace_logger: TraceLogger) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.storage = storage
        self.trace_logger = trace_logger

    def write_text(
        self,
        *,
        run_id: str,
        agent_run_id: str,
        artifact_type: ArtifactType | str,
        filename: str,
        content: str,
        source_refs: list[str] | None = None,
    ) -> Artifact:
        artifact_path = self._artifact_path(run_id, filename)
        self.storage._ensure_agent_run_belongs_to_run(agent_run_id, run_id)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with artifact_path.open("x", encoding="utf-8", newline="") as handle:
                handle.write(content)
        except FileExistsError as exc:
            raise ArtifactStoreError(f"artifact already exists: {artifact_path.name}") from exc

        content_hash = sha256(artifact_path.read_bytes()).hexdigest()

        artifact = Artifact(
            run_id=run_id,
            agent_run_id=agent_run_id,
            type=artifact_type,
            path=artifact_path.relative_to(self.root_dir).as_posix(),
            content_hash=content_hash,
            source_refs=source_refs or [],
        )
        return self._create_artifact_record(artifact, artifact_path=artifact_path, delete_file_on_error=True)

    def _create_artifact_record(
        self,
        artifact: Artifact,
        *,
        artifact_path: Path,
        delete_file_on_error: bool,
    ) -> Artifact:
        created: Artifact | None = None
        try:
            created = self.storage.create_artifact(artifact)
            self.trace_logger.record(
                run_id=artifact.run_id,
                agent_run_id=artifact.agent_run_id,
                event_type=TraceEventType.ARTIFACT_CREATED,
                payload={
                    "artifact_id": created.id,
                    "artifact_type": created.type.value,
                    "path": created.path,
                    "content_hash": created.content_hash,
                },
            )
        except Exception:
            if created is not None:
                self.storage.delete_artifact(created.id)
            if delete_file_on_error:
                artifact_path.unlink(missing_ok=True)
            raise
        return created

    def write_text_idempotent(
        self,
        *,
        run_id: str,
        agent_run_id: str,
        artifact_type: ArtifactType | str,
        filename: str,
        content: str,
        source_refs: list[str] | None = None,
    ) -> Artifact:
        """Write an artifact once and reuse an identical durable copy on retry."""
        content_hash = sha256(content.encode("utf-8")).hexdigest()
        expected_path = f"{run_id}/{filename}"
        matches = [
            artifact
            for artifact in self.storage.list_artifacts_for_run(run_id)
            if artifact.path == expected_path
        ]
        if len(matches) > 1:
            raise ArtifactStoreError(f"multiple durable artifact records exist for {expected_path}")
        existing = matches[0] if matches else None
        if existing is not None:
            if existing.agent_run_id != agent_run_id or existing.type != ArtifactType(artifact_type):
                raise ArtifactStoreError(f"artifact path is already owned by another attempt: {expected_path}")
            if existing.content_hash != content_hash:
                raise ArtifactStoreError(f"artifact path already contains different content: {expected_path}")
            try:
                if self.read_text_verified(existing) == content:
                    return existing
            except ArtifactStoreError as exc:
                raise ArtifactStoreError(f"durable artifact copy is missing or invalid: {expected_path}") from exc
            raise ArtifactStoreError(f"durable artifact content does not match metadata: {expected_path}")
        artifact_path = self._artifact_path(run_id, filename)
        try:
            orphan_stat = artifact_path.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ArtifactStoreError(f"orphan artifact copy could not be inspected: {expected_path}") from exc
        else:
            self._assert_regular_unlinked_stat(orphan_stat)
            try:
                orphan_content = artifact_path.read_bytes()
            except OSError as exc:
                raise ArtifactStoreError(f"orphan artifact copy could not be read: {expected_path}") from exc
            if orphan_content != content.encode("utf-8"):
                raise ArtifactStoreError(f"orphan artifact copy contains different content: {expected_path}")
            artifact = Artifact(
                run_id=run_id,
                agent_run_id=agent_run_id,
                type=artifact_type,
                path=expected_path,
                content_hash=content_hash,
                source_refs=source_refs or [],
            )
            return self._create_artifact_record(
                artifact,
                artifact_path=artifact_path,
                delete_file_on_error=False,
            )
        return self.write_text(
            run_id=run_id,
            agent_run_id=agent_run_id,
            artifact_type=artifact_type,
            filename=filename,
            content=content,
            source_refs=source_refs,
        )

    def stage_input_bytes(self, *, run_id: str, content_hash: str, content: bytes) -> str:
        """Persist a validated run input outside the artifact table for restart replay."""
        if not isinstance(content_hash, str) or len(content_hash) != 64 or any(
            char not in "0123456789abcdef" for char in content_hash
        ):
            raise ArtifactStoreError("staged input content_hash must be lowercase hexadecimal SHA-256")
        if sha256(content).hexdigest() != content_hash:
            raise ArtifactStoreError("staged input content hash does not match bytes")
        filename = f"input-{content_hash}.bin"
        artifact_path = self._artifact_path(run_id, filename)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        relative_path = artifact_path.relative_to(self.root_dir).as_posix()
        try:
            artifact_path.lstat()
        except FileNotFoundError:
            pass
        else:
            # Reuse only a verified regular file. A link or hard-link would
            # weaken the run-local ownership and recovery guarantees.
            self._read_staged_input(
                artifact_path,
                content_hash=content_hash,
                max_size=len(content),
            )
            return relative_path
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(artifact_path, flags, 0o600)
        except FileExistsError:
            return self.stage_input_bytes(run_id=run_id, content_hash=content_hash, content=content)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._assert_regular_unlinked_file(artifact_path)
        except Exception:
            artifact_path.unlink(missing_ok=True)
            raise
        return relative_path

    def read_staged_input(self, relative_path: str, *, content_hash: str, max_size: int) -> bytes:
        if (
            not isinstance(content_hash, str)
            or len(content_hash) != 64
            or any(char not in "0123456789abcdef" for char in content_hash)
        ):
            raise ArtifactStoreError("staged input content_hash must be lowercase hexadecimal SHA-256")
        if not isinstance(max_size, int) or max_size <= 0:
            raise ArtifactStoreError("staged input max_size must be positive")
        staged_path = self._staged_path(relative_path, content_hash=content_hash)
        return self._read_staged_input(
            staged_path,
            content_hash=content_hash,
            max_size=max_size,
        )

    def _read_staged_input(
        self,
        staged_path: Path,
        *,
        content_hash: str,
        max_size: int,
    ) -> bytes:
        self._ensure_no_reparse_components(staged_path, allow_missing=False)
        try:
            before_path = staged_path.lstat()
        except OSError as exc:
            raise ArtifactStoreError("staged input could not be read") from exc
        self._assert_regular_unlinked_stat(before_path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(staged_path, flags)
        except OSError as exc:
            raise ArtifactStoreError("staged input could not be read") from exc
        try:
            before = os.fstat(descriptor)
            self._assert_regular_unlinked_stat(before)
            if not _same_file_identity(before_path, before):
                raise ArtifactStoreError("staged input changed while it was being opened")
            content = os.read(descriptor, max_size + 1)
            after = os.fstat(descriptor)
            try:
                after_path = staged_path.lstat()
            except OSError as exc:
                raise ArtifactStoreError("staged input changed while it was being read") from exc
            self._assert_regular_unlinked_stat(after)
            self._assert_regular_unlinked_stat(after_path)
            if not _same_file_identity(before, after) or not _same_file_identity(after, after_path):
                raise ArtifactStoreError("staged input changed while it was being read")
            if before.st_size != after.st_size or len(content) > max_size:
                raise ArtifactStoreError("staged input content exceeds its durable size limit")
        except ArtifactStoreError:
            raise
        except OSError as exc:
            raise ArtifactStoreError("staged input could not be read") from exc
        finally:
            os.close(descriptor)
        if sha256(content).hexdigest() != content_hash:
            raise ArtifactStoreError("staged input content hash does not match durable metadata")
        return content

    def read_text(self, artifact: Artifact) -> str:
        artifact_path = self._existing_artifact_path(artifact.path)
        return artifact_path.read_text(encoding="utf-8")

    def read_text_verified(self, artifact: Artifact) -> str:
        artifact_path = self._existing_artifact_path(artifact.path)
        try:
            content = artifact_path.read_bytes()
        except OSError as exc:
            raise ArtifactStoreError("artifact content could not be read") from exc
        if artifact.content_hash is None or sha256(content).hexdigest() != artifact.content_hash:
            raise ArtifactStoreError("artifact content hash does not match durable metadata")
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArtifactStoreError("text artifact is not valid UTF-8") from exc

    def read_text_excerpt(self, artifact: Artifact, *, max_chars: int) -> tuple[str, bool]:
        if max_chars < 0:
            raise ArtifactStoreError("max_chars must be non-negative")
        artifact_path = self._existing_artifact_path(artifact.path)
        digest = sha256()
        decoder = codecs.getincrementaldecoder("utf-8")()
        excerpt = ""
        truncated = False
        try:
            with artifact_path.open("rb") as handle:
                while chunk := handle.read(64 * 1024):
                    digest.update(chunk)
                    decoded = decoder.decode(chunk)
                    remaining = max_chars + 1 - len(excerpt)
                    if remaining > 0:
                        excerpt += decoded[:remaining]
                    if len(decoded) > remaining:
                        truncated = True
                decoded = decoder.decode(b"", final=True)
                remaining = max_chars + 1 - len(excerpt)
                if remaining > 0:
                    excerpt += decoded[:remaining]
                if len(decoded) > remaining:
                    truncated = True
        except OSError as exc:
            raise ArtifactStoreError("artifact content could not be read") from exc
        except UnicodeDecodeError as exc:
            raise ArtifactStoreError("text artifact is not valid UTF-8") from exc
        if artifact.content_hash is None or digest.hexdigest() != artifact.content_hash:
            raise ArtifactStoreError("artifact content hash does not match durable metadata")
        return excerpt[:max_chars], truncated or len(excerpt) > max_chars

    def _artifact_path(self, run_id: str, filename: str) -> Path:
        self._ensure_simple_segment(run_id, "run_id")
        raw_path = Path(filename)
        if raw_path.is_absolute():
            raise ArtifactStoreError("filename must be a simple relative file name")
        self._ensure_simple_segment(filename, "filename")

        artifact_path = self.root_dir / run_id / filename
        self._ensure_under_root(artifact_path)
        self._ensure_no_reparse_components(artifact_path, allow_missing=True)
        return artifact_path

    def _ensure_simple_segment(self, value: str, field_name: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or "\x00" in value
            or ":" in value
            or Path(value).name != value
            or value in {".", ".."}
        ):
            raise ArtifactStoreError(f"{field_name} must be a single path segment")

    def _staged_path(self, relative_path: str, *, content_hash: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path:
            raise ArtifactStoreError("staged input path must be a relative path")
        raw_path = Path(relative_path)
        if (
            raw_path.is_absolute()
            or any(part in {"", ".", ".."} for part in raw_path.parts)
            or len(raw_path.parts) != 2
            or raw_path.name != f"input-{content_hash}.bin"
        ):
            raise ArtifactStoreError("staged input path has an invalid durable shape")
        self._ensure_simple_segment(raw_path.parts[0], "staged input run_id")
        staged_path = self.root_dir / raw_path
        self._ensure_under_root(staged_path)
        self._ensure_no_reparse_components(staged_path, allow_missing=False)
        return staged_path

    def _existing_artifact_path(self, relative_path: str) -> Path:
        if not isinstance(relative_path, str) or not relative_path:
            raise ArtifactStoreError("artifact path must be a relative path")
        raw_path = Path(relative_path)
        if raw_path.is_absolute() or any(part in {"", ".", ".."} for part in raw_path.parts):
            raise ArtifactStoreError("artifact path must be a normalized relative path")
        artifact_path = self.root_dir / raw_path
        self._ensure_under_root(artifact_path)
        self._ensure_no_reparse_components(artifact_path, allow_missing=False)
        return artifact_path

    def _ensure_no_reparse_components(self, path: Path, *, allow_missing: bool) -> None:
        try:
            relative = path.relative_to(self.root_dir)
        except ValueError as exc:
            raise ArtifactStoreError("artifact path must stay under artifact root") from exc
        current = self.root_dir
        for part in relative.parts:
            current /= part
            try:
                current_stat = current.lstat()
            except FileNotFoundError:
                if allow_missing:
                    return
                raise ArtifactStoreError("artifact path does not exist")
            except OSError as exc:
                raise ArtifactStoreError("artifact path could not be inspected") from exc
            if _is_reparse_point(current, current_stat):
                raise ArtifactStoreError("artifact path cannot traverse a symbolic link or reparse point")

    def _assert_regular_unlinked_file(self, path: Path) -> None:
        try:
            self._assert_regular_unlinked_stat(path.lstat())
        except OSError as exc:
            raise ArtifactStoreError("staged input could not be verified") from exc

    @staticmethod
    def _assert_regular_unlinked_stat(file_stat: os.stat_result) -> None:
        if not stat.S_ISREG(file_stat.st_mode):
            raise ArtifactStoreError("staged input must be a regular file")
        if getattr(file_stat, "st_nlink", 1) != 1:
            raise ArtifactStoreError("staged input cannot be hard-linked")

    def _ensure_under_root(self, path: Path) -> None:
        if not path.is_relative_to(self.root_dir):
            raise ArtifactStoreError("artifact path must stay under artifact root")


def _is_reparse_point(path: Path, file_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(file_stat.st_mode):
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse_attribute and getattr(file_stat, "st_file_attributes", 0) & reparse_attribute)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino
