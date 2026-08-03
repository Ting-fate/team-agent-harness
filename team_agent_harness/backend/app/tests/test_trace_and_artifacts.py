from hashlib import sha256
from pathlib import Path

import pytest

from app.core.artifacts import ArtifactStore, ArtifactStoreError
from app.core.models import (
    AgentDefinition,
    AgentRun,
    ArtifactType,
    Run,
    Task,
    TraceEventType,
)
from app.core.storage import SQLiteStorage, StorageError
from app.core.trace import TraceLogger


@pytest.fixture
def seeded_storage(tmp_path):
    with SQLiteStorage(tmp_path / "harness.sqlite3") as db:
        db.init_schema()
        task = db.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
        run = db.create_run(Run(id="run-1", task_id=task.id))
        agent = db.create_agent_definition(
            AgentDefinition(
                id="agent-1",
                pack_name="code_rd",
                role="Clarifier",
                system_prompt="Clarify requirements.",
            )
        )
        agent_run = db.create_agent_run(
            AgentRun(
                id="agent-run-1",
                run_id=run.id,
                agent_id=agent.id,
                step_name="Clarifier",
            )
        )
        yield db, run, agent_run, tmp_path


def test_trace_logger_records_and_lists_events(seeded_storage) -> None:
    db, run, agent_run, _ = seeded_storage
    logger = TraceLogger(db)

    event = logger.record(
        run_id=run.id,
        agent_run_id=agent_run.id,
        event_type=TraceEventType.MODEL_ACTION,
        payload={"action": "draft_requirements"},
        duration_ms=5,
    )

    assert event.event_type == TraceEventType.MODEL_ACTION
    assert event.payload == {"action": "draft_requirements"}
    assert logger.list_for_run(run.id) == [event]


def test_artifact_store_writes_file_metadata_and_trace(seeded_storage) -> None:
    db, run, agent_run, tmp_path = seeded_storage
    logger = TraceLogger(db)
    store = ArtifactStore(tmp_path / "artifacts", db, logger)
    content = "# Requirements\n\n- Keep scope small.\n"

    artifact = store.write_text(
        run_id=run.id,
        agent_run_id=agent_run.id,
        artifact_type=ArtifactType.DESIGN_DOC,
        filename="requirements.md",
        content=content,
        source_refs=["task-1"],
    )

    assert artifact.path == "run-1/requirements.md"
    assert artifact.content_hash == sha256(content.encode("utf-8")).hexdigest()
    assert artifact.source_refs == ["task-1"]
    assert store.read_text(artifact) == content
    excerpt, truncated = store.read_text_excerpt(artifact, max_chars=8)
    assert excerpt == content[:8]
    assert truncated is True
    assert db.get_artifact(artifact.id) == artifact

    trace_events = logger.list_for_run(run.id)
    assert len(trace_events) == 1
    assert trace_events[0].event_type == TraceEventType.ARTIFACT_CREATED
    assert trace_events[0].payload["artifact_id"] == artifact.id


def test_artifact_excerpt_rejects_content_changed_after_metadata_persisted(seeded_storage) -> None:
    db, run, agent_run, tmp_path = seeded_storage
    store = ArtifactStore(tmp_path / "artifacts", db, TraceLogger(db))
    artifact = store.write_text(
        run_id=run.id,
        agent_run_id=agent_run.id,
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content="# original patch\n",
    )
    (store.root_dir / artifact.path).write_text("# tampered patch\n", encoding="utf-8")

    with pytest.raises(ArtifactStoreError, match="content hash"):
        store.read_text_excerpt(artifact, max_chars=8)


def test_artifact_store_rejects_unsafe_filenames(seeded_storage) -> None:
    db, run, agent_run, tmp_path = seeded_storage
    store = ArtifactStore(tmp_path / "artifacts", db, TraceLogger(db))

    for filename in ["../escape.md", "nested/file.md", "", "."]:
        with pytest.raises(ArtifactStoreError):
            store.write_text(
                run_id=run.id,
                agent_run_id=agent_run.id,
                artifact_type=ArtifactType.DESIGN_DOC,
                filename=filename,
                content="safe",
            )


def test_artifact_store_rejects_unsafe_run_ids(seeded_storage) -> None:
    db, _, agent_run, tmp_path = seeded_storage
    store = ArtifactStore(tmp_path / "artifacts", db, TraceLogger(db))

    for run_id in ["", ".", "..", "nested/run"]:
        with pytest.raises(ArtifactStoreError):
            store.write_text(
                run_id=run_id,
                agent_run_id=agent_run.id,
                artifact_type=ArtifactType.DESIGN_DOC,
                filename="requirements.md",
                content="safe",
            )


def test_artifact_store_rejects_absolute_filename(seeded_storage) -> None:
    db, run, agent_run, tmp_path = seeded_storage
    store = ArtifactStore(tmp_path / "artifacts", db, TraceLogger(db))

    with pytest.raises(ArtifactStoreError):
        store.write_text(
            run_id=run.id,
            agent_run_id=agent_run.id,
            artifact_type=ArtifactType.DESIGN_DOC,
            filename=str(tmp_path / "escape.md"),
            content="safe",
        )


def test_artifact_store_rejects_overwrite(seeded_storage) -> None:
    db, run, agent_run, tmp_path = seeded_storage
    store = ArtifactStore(tmp_path / "artifacts", db, TraceLogger(db))

    store.write_text(
        run_id=run.id,
        agent_run_id=agent_run.id,
        artifact_type=ArtifactType.DESIGN_DOC,
        filename="requirements.md",
        content="first",
    )

    with pytest.raises(ArtifactStoreError):
        store.write_text(
            run_id=run.id,
            agent_run_id=agent_run.id,
            artifact_type=ArtifactType.DESIGN_DOC,
            filename="requirements.md",
            content="second",
        )


def test_artifact_store_supports_relative_root_dir(seeded_storage, monkeypatch, tmp_path) -> None:
    db, run, agent_run, tmp_path = seeded_storage
    monkeypatch.chdir(tmp_path)
    store = ArtifactStore("relative-artifacts", db, TraceLogger(db))

    artifact = store.write_text(
        run_id=run.id,
        agent_run_id=agent_run.id,
        artifact_type=ArtifactType.DESIGN_DOC,
        filename="requirements.md",
        content="safe",
    )

    assert artifact.path == "run-1/requirements.md"
    assert Path("relative-artifacts", artifact.path).read_text(encoding="utf-8") == "safe"


def test_artifact_store_removes_file_if_metadata_write_fails(seeded_storage, tmp_path) -> None:
    db, run, _, tmp_path = seeded_storage
    store = ArtifactStore(tmp_path / "artifacts", db, TraceLogger(db))

    with pytest.raises(StorageError):
        store.write_text(
            run_id=run.id,
            agent_run_id="missing-agent-run",
            artifact_type=ArtifactType.DESIGN_DOC,
            filename="requirements.md",
            content="safe",
        )

    assert not (tmp_path / "artifacts" / run.id / "requirements.md").exists()


def test_artifact_store_removes_metadata_and_file_if_trace_write_fails(seeded_storage, tmp_path) -> None:
    db, run, agent_run, tmp_path = seeded_storage

    class FailingTraceLogger(TraceLogger):
        def record(self, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError("trace failed")

    store = ArtifactStore(tmp_path / "artifacts", db, FailingTraceLogger(db))

    with pytest.raises(RuntimeError, match="trace failed"):
        store.write_text(
            run_id=run.id,
            agent_run_id=agent_run.id,
            artifact_type=ArtifactType.DESIGN_DOC,
            filename="requirements.md",
            content="safe",
        )

    assert not (tmp_path / "artifacts" / run.id / "requirements.md").exists()
    assert db.list_artifacts_for_run(run.id) == []


def test_artifact_store_read_rejects_path_outside_root(seeded_storage, tmp_path) -> None:
    db, run, agent_run, tmp_path = seeded_storage
    store = ArtifactStore(tmp_path / "artifacts", db, TraceLogger(db))
    artifact = store.write_text(
        run_id=run.id,
        agent_run_id=agent_run.id,
        artifact_type=ArtifactType.DESIGN_DOC,
        filename="requirements.md",
        content="safe",
    )
    polluted = artifact.model_copy(update={"path": "../secret.txt"})

    with pytest.raises(ArtifactStoreError):
        store.read_text(polluted)


def test_trace_event_agent_run_must_belong_to_same_run(seeded_storage) -> None:
    db, run, agent_run, _ = seeded_storage
    second_run = db.create_run(Run(id="run-2", task_id="task-1"))
    logger = TraceLogger(db)

    with pytest.raises(StorageError):
        logger.record(
            run_id=second_run.id,
            agent_run_id=agent_run.id,
            event_type=TraceEventType.MODEL_ACTION,
            payload={"action": "start"},
        )
