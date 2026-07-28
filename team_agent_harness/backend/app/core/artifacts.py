from __future__ import annotations

from hashlib import sha256
from pathlib import Path

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
        created: Artifact | None = None
        try:
            created = self.storage.create_artifact(artifact)
            self.trace_logger.record(
                run_id=run_id,
                agent_run_id=agent_run_id,
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
            artifact_path.unlink(missing_ok=True)
            raise
        return created

    def read_text(self, artifact: Artifact) -> str:
        artifact_path = (self.root_dir / artifact.path).resolve()
        self._ensure_under_root(artifact_path)
        return artifact_path.read_text(encoding="utf-8")

    def read_text_excerpt(self, artifact: Artifact, *, max_chars: int) -> tuple[str, bool]:
        if max_chars < 0:
            raise ArtifactStoreError("max_chars must be non-negative")
        artifact_path = (self.root_dir / artifact.path).resolve()
        self._ensure_under_root(artifact_path)
        with artifact_path.open(encoding="utf-8") as handle:
            content = handle.read(max_chars + 1)
        return content[:max_chars], len(content) > max_chars

    def _artifact_path(self, run_id: str, filename: str) -> Path:
        self._ensure_simple_segment(run_id, "run_id")
        raw_path = Path(filename)
        if raw_path.is_absolute():
            raise ArtifactStoreError("filename must be a simple relative file name")
        self._ensure_simple_segment(filename, "filename")

        artifact_path = (self.root_dir / run_id / filename).resolve()
        self._ensure_under_root(artifact_path)
        return artifact_path

    def _ensure_simple_segment(self, value: str, field_name: str) -> None:
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ArtifactStoreError(f"{field_name} must be a single path segment")

    def _ensure_under_root(self, path: Path) -> None:
        if not path.is_relative_to(self.root_dir):
            raise ArtifactStoreError("artifact path must stay under artifact root")
