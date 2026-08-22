from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest

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
    RunLock,
    RunLockStatus,
    RunQueueItem,
    RunQueueItemStatus,
    RunStatus,
    RuntimeJob,
    RuntimeJobStatus,
    Task,
    TraceEvent,
    TraceEventType,
)
from app.core.storage import (
    RunRecordIntegrityError,
    SQLiteStorage,
    StorageError,
    StorageIntegrityError,
)


@pytest.fixture
def storage(tmp_path):
    with SQLiteStorage(tmp_path / "harness.sqlite3") as db:
        db.init_schema()
        yield db


def test_task_create_get_list(storage: SQLiteStorage) -> None:
    task = Task(
        id="task-1",
        title="Build storage",
        goal="Persist core models.",
        workflow_pack="code_rd",
        inputs={"priority": "high"},
    )

    storage.create_task(task)

    assert storage.get_task("task-1") == task
    assert storage.get_task("missing") is None
    assert storage.list_tasks() == [task]


def test_run_create_update_get_list(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = Run(id="run-1", task_id=task.id)

    storage.create_run(run)
    updated = run.model_copy(
        update={
            "status": RunStatus.RUNNING,
            "current_step": "Clarifier",
            "started_at": datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
        }
    )
    storage.update_run(updated)

    assert storage.get_run("run-1") == updated
    assert storage.list_runs() == [updated]


def test_purge_terminal_run_records_preserves_active_runs_and_tasks(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-purge", title="Purge", goal="Purge history.", workflow_pack="research"))
    completed = storage.create_run(
        Run(id="run-purge-completed", task_id=task.id, status=RunStatus.COMPLETED)
    )
    failed = storage.create_run(
        Run(id="run-purge-failed", task_id=task.id, status=RunStatus.FAILED)
    )
    running = storage.create_run(
        Run(id="run-purge-running", task_id=task.id, status=RunStatus.RUNNING)
    )
    locked = storage.create_run(
        Run(id="run-purge-locked", task_id=task.id, status=RunStatus.COMPLETED)
    )
    storage.create_run_lock(
        RunLock(id="lock-purge", run_id=locked.id, owner="test", status=RunLockStatus.ACQUIRED)
    )
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-purge",
            pack_name="research",
            role="Reader",
            system_prompt="Read the source material.",
        )
    )
    agent_run = storage.create_agent_run(
        AgentRun(
            id="agent-run-purge",
            run_id=completed.id,
            agent_id=agent.id,
            step_name="read_sources",
            status=AgentRunStatus.COMPLETED,
        )
    )

    with storage.transaction():
        storage.conn.execute(
            "INSERT INTO artifacts (id, run_id, agent_run_id, type, data, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                "artifact-purge",
                completed.id,
                agent_run.id,
                ArtifactType.FINAL_REPORT.value,
                json.dumps(
                    {
                        "id": "artifact-purge",
                        "run_id": completed.id,
                        "agent_run_id": "agent-run-purge",
                        "type": ArtifactType.FINAL_REPORT.value,
                        "path": "run-purge-completed/final.md",
                        "content_hash": "a" * 64,
                        "source_refs": [],
                    }
                ),
                datetime.now(UTC).isoformat(),
            ),
        )

    preview = storage.preview_terminal_run_records()
    summary = storage.purge_terminal_run_records(
        expected_run_ids=preview["run_ids"],
        expected_artifacts=preview["artifacts"],
    )

    assert preview["run_ids"] == ["run-purge-completed", "run-purge-failed"]
    assert preview["artifact_paths"] == ["run-purge-completed/final.md"]
    assert summary["run_ids"] == ["run-purge-completed", "run-purge-failed"]
    assert summary["artifact_paths"] == ["run-purge-completed/final.md"]
    assert summary["runs_deleted"] == 2
    assert storage.get_run(completed.id) is None
    assert storage.get_run(failed.id) is None
    assert storage.get_run(running.id) == running
    assert storage.get_run(locked.id) == locked
    assert storage.get_task(task.id) == task
    assert storage.conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_purge_terminal_run_records_rejects_candidate_drift(storage: SQLiteStorage) -> None:
    task = storage.create_task(
        Task(id="task-purge-drift", title="Purge drift", goal="Freeze deletion scope.", workflow_pack="research")
    )
    run = storage.create_run(
        Run(id="run-purge-drift", task_id=task.id, status=RunStatus.COMPLETED)
    )
    preview = storage.preview_terminal_run_records()
    storage.create_run_lock(
        RunLock(id="lock-purge-drift", run_id=run.id, owner="test", status=RunLockStatus.ACQUIRED)
    )

    with pytest.raises(StorageError, match="candidates changed"):
        storage.purge_terminal_run_records(
            expected_run_ids=preview["run_ids"],
            expected_artifacts=preview["artifacts"],
        )

    assert storage.get_run(run.id) == run


def test_purge_terminal_run_records_binds_artifact_identity(storage: SQLiteStorage) -> None:
    task = storage.create_task(
        Task(id="task-purge-artifact-drift", title="Artifact drift", goal="Freeze artifacts.", workflow_pack="research")
    )
    run = storage.create_run(
        Run(id="run-purge-artifact-drift", task_id=task.id, status=RunStatus.COMPLETED)
    )
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-purge-artifact-drift",
            pack_name="research",
            role="ArtifactDrift",
            system_prompt="Test artifact identity.",
        )
    )
    agent_run = storage.create_agent_run(
        AgentRun(
            id="agent-run-purge-artifact-drift",
            run_id=run.id,
            agent_id=agent.id,
            step_name="artifact_drift",
        )
    )
    original = storage.create_artifact(
        Artifact(
            id="artifact-purge-original",
            run_id=run.id,
            agent_run_id=agent_run.id,
            type=ArtifactType.FINAL_REPORT,
            path=f"{run.id}/final.md",
            content_hash="a" * 64,
        )
    )
    preview = storage.preview_terminal_run_records()
    storage.delete_artifact(original.id)
    replacement = storage.create_artifact(
        original.model_copy(
            update={
                "id": "artifact-purge-replacement",
                "content_hash": "b" * 64,
            }
        )
    )

    with pytest.raises(StorageError, match="artifact candidates changed"):
        storage.purge_terminal_run_records(
            expected_run_ids=preview["run_ids"],
            expected_artifacts=preview["artifacts"],
        )

    assert storage.get_run(run.id) == run
    assert storage.get_artifact(replacement.id) == replacement


def test_purge_terminal_run_records_rejects_invalid_artifact_metadata(
    storage: SQLiteStorage,
) -> None:
    task = storage.create_task(
        Task(id="task-purge-invalid-artifact", title="Invalid artifact", goal="Fail closed.", workflow_pack="research")
    )
    run = storage.create_run(
        Run(id="run-purge-invalid-artifact", task_id=task.id, status=RunStatus.COMPLETED)
    )
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-purge-invalid-artifact",
            pack_name="research",
            role="InvalidArtifact",
            system_prompt="Test invalid metadata.",
        )
    )
    agent_run = storage.create_agent_run(
        AgentRun(
            id="agent-run-purge-invalid-artifact",
            run_id=run.id,
            agent_id=agent.id,
            step_name="invalid_artifact",
        )
    )
    artifact = storage.create_artifact(
        Artifact(
            id="artifact-purge-invalid-metadata",
            run_id=run.id,
            agent_run_id=agent_run.id,
            type=ArtifactType.FINAL_REPORT,
            path=f"{run.id}/final.md",
        )
    )
    with storage.transaction():
        storage.conn.execute(
            "UPDATE artifacts SET data = ? WHERE id = ?",
            ("{}", artifact.id),
        )

    with pytest.raises(StorageIntegrityError, match="metadata is invalid"):
        storage.preview_terminal_run_records()

    assert storage.get_run(run.id) == run


def test_purge_terminal_run_records_rejects_path_owned_by_retained_artifact(
    storage: SQLiteStorage,
) -> None:
    task = storage.create_task(
        Task(id="task-purge-shared-path", title="Shared path", goal="Protect active files.", workflow_pack="research")
    )
    terminal_run = storage.create_run(
        Run(id="run-purge-shared-path", task_id=task.id, status=RunStatus.COMPLETED)
    )
    active_run = storage.create_run(
        Run(id="run-retained-shared-path", task_id=task.id, status=RunStatus.RUNNING)
    )
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-purge-shared-path",
            pack_name="research",
            role="SharedPath",
            system_prompt="Test shared path ownership.",
        )
    )
    terminal_attempt = storage.create_agent_run(
        AgentRun(
            id="attempt-purge-shared-path",
            run_id=terminal_run.id,
            agent_id=agent.id,
            step_name="terminal",
        )
    )
    active_attempt = storage.create_agent_run(
        AgentRun(
            id="attempt-retained-shared-path",
            run_id=active_run.id,
            agent_id=agent.id,
            step_name="active",
        )
    )
    shared_path = f"{terminal_run.id}/final.md"
    storage.create_artifact(
        Artifact(
            id="artifact-purge-shared-path",
            run_id=terminal_run.id,
            agent_run_id=terminal_attempt.id,
            type=ArtifactType.FINAL_REPORT,
            path=shared_path,
        )
    )
    storage.create_artifact(
        Artifact(
            id="artifact-retained-shared-path",
            run_id=active_run.id,
            agent_run_id=active_attempt.id,
            type=ArtifactType.FINAL_REPORT,
            path=shared_path,
        )
    )

    with pytest.raises(StorageIntegrityError, match="retained artifact"):
        storage.preview_terminal_run_records()

    assert storage.get_run(terminal_run.id) == terminal_run
    assert storage.get_run(active_run.id) == active_run


def test_purge_terminal_run_records_batches_below_sqlite_variable_limit(
    storage: SQLiteStorage,
) -> None:
    task = storage.create_task(
        Task(
            id="task-purge-batched",
            title="Batched purge",
            goal="Purge histories larger than one SQLite parameter batch.",
            workflow_pack="research",
        )
    )
    run_ids = [f"run-purge-batched-{index:02d}" for index in range(25)]
    for run_id in run_ids:
        storage.create_run(Run(id=run_id, task_id=task.id, status=RunStatus.COMPLETED))
    previous_limit = storage.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 20)
    try:
        summary = storage.purge_terminal_run_records()
    finally:
        storage.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, previous_limit)

    assert summary["run_ids"] == run_ids
    assert summary["runs_deleted"] == len(run_ids)
    assert storage.list_runs() == []


def test_storage_injects_legacy_web_snapshot_only_when_both_raw_fields_are_missing(
    storage: SQLiteStorage,
) -> None:
    task = storage.create_task(
        Task(id="task-legacy-web", title="Task", goal="Goal", workflow_pack="research")
    )
    raw_run = {
        "id": "run-legacy-web",
        "task_id": task.id,
        "real_web_access_confirmed": True,
    }
    with storage.transaction():
        storage.conn.execute(
            "INSERT INTO runs (id, task_id, status, data) VALUES (?, ?, ?, ?)",
            (raw_run["id"], task.id, "queued", json.dumps(raw_run)),
        )

    restored = storage.get_run(raw_run["id"])

    assert restored is not None
    assert restored.confirmed_real_web_tools is None
    assert restored.confirmed_real_web_tool_routes is None
    assert storage.list_runs() == [restored]

    updated = storage.update_run(
        restored.model_copy(update={"status": RunStatus.RUNNING})
    )
    raw_updated = json.loads(
        storage.conn.execute(
            "SELECT data FROM runs WHERE id = ?",
            (raw_run["id"],),
        ).fetchone()["data"]
    )
    assert "confirmed_real_web_tools" not in raw_updated
    assert "confirmed_real_web_tool_routes" not in raw_updated

    reloaded = storage.get_run(raw_run["id"])
    assert reloaded is not None
    assert reloaded.status == updated.status
    assert reloaded.confirmed_real_web_tools is None
    assert reloaded.confirmed_real_web_tool_routes is None


def test_storage_rejects_null_web_snapshots_without_complete_legacy_provenance(
    storage: SQLiteStorage,
) -> None:
    task = storage.create_task(
        Task(id="task-null-web", title="Task", goal="Goal", workflow_pack="research")
    )
    forged_legacy = Run(id="run-forged-legacy", task_id=task.id).model_copy(
        update={
            "confirmed_real_web_tools": None,
            "confirmed_real_web_tool_routes": None,
        }
    )

    with pytest.raises(RunRecordIntegrityError):
        storage.create_run(forged_legacy)
    assert storage.get_run(forged_legacy.id) is None

    raw_run = {
        "id": "run-partial-legacy",
        "task_id": task.id,
        "real_web_access_confirmed": True,
    }
    with storage.transaction():
        storage.conn.execute(
            "INSERT INTO runs (id, task_id, status, data) VALUES (?, ?, ?, ?)",
            (raw_run["id"], task.id, "queued", json.dumps(raw_run)),
        )
    restored = storage.get_run(raw_run["id"])
    assert restored is not None
    cloned_legacy = restored.model_copy(update={"id": "run-cloned-legacy"})
    with pytest.raises(RunRecordIntegrityError):
        storage.create_run(cloned_legacy)
    assert storage.get_run(cloned_legacy.id) is None

    partial = restored.model_copy(update={"confirmed_real_web_tools": []})

    with pytest.raises(RunRecordIntegrityError):
        storage.update_run(partial)

    reloaded = storage.get_run(raw_run["id"])
    assert reloaded is not None
    assert reloaded.confirmed_real_web_tools is None
    assert reloaded.confirmed_real_web_tool_routes is None


def test_storage_rejects_run_payload_identity_mismatch(storage: SQLiteStorage) -> None:
    task = storage.create_task(
        Task(id="task-run-identity", title="Task", goal="Goal", workflow_pack="research")
    )
    payload = {
        "id": "payload-run-id",
        "task_id": task.id,
        "confirmed_real_web_tools": [],
        "confirmed_real_web_tool_routes": [],
    }
    with storage.transaction():
        storage.conn.execute(
            "INSERT INTO runs (id, task_id, status, data) VALUES (?, ?, ?, ?)",
            ("row-run-id", task.id, "queued", json.dumps(payload)),
        )

    with pytest.raises(RunRecordIntegrityError) as get_error:
        storage.get_run("row-run-id")
    with pytest.raises(RunRecordIntegrityError) as list_error:
        storage.list_runs()
    with pytest.raises(RunRecordIntegrityError) as status_list_error:
        storage.list_runs_by_statuses({RunStatus.QUEUED})

    assert get_error.value.run_id == "row-run-id"
    assert list_error.value.run_id == "row-run-id"
    assert status_list_error.value.run_id == "row-run-id"


@pytest.mark.parametrize(
    "snapshot_fields",
    [
        {"confirmed_real_web_tools": ["web_search"]},
        {
            "confirmed_real_web_tool_routes": [
                {"name": "web_search", "provider": "tavily"},
            ]
        },
        {
            "confirmed_real_web_tools": None,
            "confirmed_real_web_tool_routes": None,
        },
    ],
)
def test_storage_rejects_partial_or_explicit_null_persisted_web_snapshots(
    storage: SQLiteStorage,
    snapshot_fields: dict[str, object],
) -> None:
    task = storage.create_task(
        Task(id="task-invalid-web", title="Task", goal="Goal", workflow_pack="research")
    )
    raw_run = {
        "id": "run-invalid-web",
        "task_id": task.id,
        "real_web_access_confirmed": True,
        **snapshot_fields,
    }
    with storage.transaction():
        storage.conn.execute(
            "INSERT INTO runs (id, task_id, status, data) VALUES (?, ?, ?, ?)",
            (raw_run["id"], task.id, "queued", json.dumps(raw_run)),
        )

    with pytest.raises(RunRecordIntegrityError):
        storage.get_run(raw_run["id"])


def test_storage_does_not_classify_composite_corruption_as_incomplete_plan(
    storage: SQLiteStorage,
) -> None:
    task = storage.create_task(
        Task(id="task-composite-run-corruption", title="Task", goal="Goal", workflow_pack="research")
    )
    raw_run = {
        "id": "run-composite-corruption",
        "task_id": task.id,
        "execution_plan": {},
        "execution_plan_hash": None,
        "confirmed_real_web_tools": None,
        "confirmed_real_web_tool_routes": None,
    }
    with storage.transaction():
        storage.conn.execute(
            "INSERT INTO runs (id, task_id, status, data) VALUES (?, ?, ?, ?)",
            (raw_run["id"], task.id, "queued", json.dumps(raw_run)),
        )

    with pytest.raises(RunRecordIntegrityError) as error:
        storage.get_run(raw_run["id"])

    assert error.value.reason == "invalid_payload"


def test_outer_transaction_rolls_back_nested_model_writes(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-atomic", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = Run(id="run-atomic", task_id=task.id)

    with pytest.raises(RuntimeError, match="injected failure"):
        with storage.transaction():
            storage.create_run(run)
            storage.create_run_queue_item(
                RunQueueItem(id="queue-atomic", run_id=run.id, action="start_run")
            )
            raise RuntimeError("injected failure")

    assert storage.get_run(run.id) is None
    assert storage.list_run_queue_items_for_run(run.id) == []


def test_worker_recovery_queries_skip_clean_terminal_history(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-recovery-query", title="Task", goal="Goal", workflow_pack="code_rd"))
    queued = storage.create_run(Run(id="run-queued", task_id=task.id, status=RunStatus.QUEUED))
    running = storage.create_run(Run(id="run-running", task_id=task.id, status=RunStatus.RUNNING))
    waiting_with_approval = storage.create_run(
        Run(id="run-waiting-approved", task_id=task.id, status=RunStatus.WAITING)
    )
    storage.create_run(Run(id="run-completed-clean", task_id=task.id, status=RunStatus.COMPLETED))
    completed_with_queue = storage.create_run(
        Run(id="run-completed-active-queue", task_id=task.id, status=RunStatus.COMPLETED)
    )
    failed_with_lock = storage.create_run(
        Run(id="run-failed-active-lock", task_id=task.id, status=RunStatus.FAILED)
    )
    storage.create_run_queue_item(
        RunQueueItem(
            id="queue-terminal-anomaly",
            run_id=completed_with_queue.id,
            action="recover-terminal-queue",
            status=RunQueueItemStatus.RUNNING,
        )
    )
    storage.create_run_lock(
        RunLock(
            id="lock-terminal-anomaly",
            run_id=failed_with_lock.id,
            owner="test",
            status=RunLockStatus.ACQUIRED,
            acquired_at=datetime(2026, 6, 15, 12, 0, tzinfo=UTC),
        )
    )
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-recovery-query",
            pack_name="code_rd",
            role="RecoveryQuery",
            system_prompt="Test worker recovery selection.",
        )
    )
    agent_run = storage.create_agent_run(
        AgentRun(
            id="agent-run-recovery-query",
            run_id=waiting_with_approval.id,
            agent_id=agent.id,
            step_name="approval_step",
            status=AgentRunStatus.WAITING,
        )
    )
    storage.create_runtime_job(
        RuntimeJob(
            id="job-recovery-query",
            run_id=waiting_with_approval.id,
            agent_run_id=agent_run.id,
            step_name="approval_step",
            runtime="session",
            status=RuntimeJobStatus.APPROVED,
            approval_required=True,
        )
    )

    assert storage.list_runs_by_statuses({RunStatus.QUEUED}) == [queued]
    assert {run.id for run in storage.list_runs_requiring_worker_recovery()} == {
        queued.id,
        running.id,
        waiting_with_approval.id,
        completed_with_queue.id,
        failed_with_lock.id,
    }


def test_agent_definition_create_get_list_and_filter(storage: SQLiteStorage) -> None:
    code_agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-code-reviewer",
            pack_name="code_rd",
            role="Reviewer",
            system_prompt="Review code quality.",
            model_config={"model": "gpt-test"},
            tool_permissions=["read_file"],
        )
    )
    research_agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-researcher",
            pack_name="research",
            role="Searcher",
            system_prompt="Search sources.",
            model_config={"model": "gpt-test"},
            tool_permissions=["web_search"],
        )
    )

    assert storage.get_agent_definition(code_agent.id) == code_agent
    assert storage.get_agent_definition_by_pack_role("code_rd", "Reviewer") == code_agent
    assert storage.get_agent_definition_by_pack_role("code_rd", "Missing") is None
    assert storage.list_agent_definitions() == [code_agent, research_agent]
    assert storage.list_agent_definitions(pack_name="code_rd") == [code_agent]


def test_agent_definition_rejects_duplicate_pack_role(storage: SQLiteStorage) -> None:
    storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd",
            role="Reviewer",
            system_prompt="Review code quality.",
        )
    )

    with pytest.raises(StorageError, match="role already exists"):
        storage.create_agent_definition(
            AgentDefinition(
                id="agent-2",
                pack_name="code_rd",
                role="Reviewer",
                system_prompt="Review code quality.",
            )
        )


def test_agent_definition_upsert_updates_same_pack_role_and_rejects_conflicts(storage: SQLiteStorage) -> None:
    original = storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd",
            role="Reviewer",
            system_prompt="Review code quality.",
            model_config={"provider": "mock", "model": "old-model"},
        )
    )
    updated = original.model_copy(
        update={
            "system_prompt": "Review code quality and security.",
            "model_settings": {"provider": "mock", "model": "new-model"},
        }
    )

    assert storage.upsert_agent_definition(updated) == updated
    assert storage.get_agent_definition("agent-1") == updated
    assert storage.get_agent_definition_by_pack_role("code_rd", "Reviewer") == updated

    with pytest.raises(StorageError, match="role already exists"):
        storage.upsert_agent_definition(
            AgentDefinition(
                id="agent-2",
                pack_name="code_rd",
                role="Reviewer",
                system_prompt="Conflicting reviewer.",
            )
        )


def test_agent_definition_upsert_preserves_existing_foreign_keys(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = storage.create_run(Run(id="run-1", task_id=task.id))
    old_agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd",
            role="Reviewer",
            system_prompt="Old reviewer.",
            model_config={"provider": "mock", "model": "old-model"},
        )
    )
    agent_run = storage.create_agent_run(
        AgentRun(
            id="agent-run-1",
            run_id=run.id,
            agent_id=old_agent.id,
            step_name="review",
        )
    )
    updated_agent = old_agent.model_copy(
        update={
            "system_prompt": "Updated reviewer.",
            "model_settings": {"provider": "mock", "model": "new-model"},
        }
    )

    storage.upsert_agent_definition(updated_agent)
    artifact = storage.create_artifact(
        Artifact(
            id="artifact-1",
            run_id=run.id,
            agent_run_id=agent_run.id,
            type=ArtifactType.RESEARCH_NOTE,
            path="data/artifacts/run-1/review.md",
        )
    )

    assert storage.get_agent_run(agent_run.id) == agent_run
    assert storage.get_agent_definition(old_agent.id) == updated_agent
    assert artifact.agent_run_id == agent_run.id


def test_update_missing_run_raises_storage_error(storage: SQLiteStorage) -> None:
    with pytest.raises(StorageError):
        storage.update_run(Run(id="missing", task_id="task-1", status=RunStatus.FAILED))


def test_foreign_key_rejects_run_without_task(storage: SQLiteStorage) -> None:
    with pytest.raises(StorageError):
        storage.create_run(Run(id="run-1", task_id="missing-task"))


def test_foreign_key_rejects_agent_run_without_agent_definition(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = storage.create_run(Run(id="run-1", task_id=task.id))

    with pytest.raises(StorageError):
        storage.create_agent_run(
            AgentRun(
                id="agent-run-1",
                run_id=run.id,
                agent_id="missing-agent",
                step_name="Clarifier",
            )
        )


def test_agent_run_handoff_artifact_trace_eval_round_trip(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = storage.create_run(Run(id="run-1", task_id=task.id))
    source_agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd",
            role="Clarifier",
            system_prompt="Clarify requirements.",
        )
    )
    target_agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-2",
            pack_name="code_rd",
            role="Architect",
            system_prompt="Design implementation.",
        )
    )
    agent_run = storage.create_agent_run(
        AgentRun(
            id="agent-run-1",
            run_id=run.id,
            agent_id=source_agent.id,
            step_name="Clarifier",
            status=AgentRunStatus.COMPLETED,
            output_summary="Requirements clarified.",
        )
    )
    artifact = storage.create_artifact(
        Artifact(
            id="artifact-1",
            run_id=run.id,
            agent_run_id=agent_run.id,
            type=ArtifactType.DESIGN_DOC,
            path="data/artifacts/run-1/design.md",
            source_refs=["handoff-1"],
        )
    )
    handoff = storage.create_handoff(
        Handoff(
            id="handoff-1",
            run_id=run.id,
            from_agent_run_id=agent_run.id,
            to_agent_id=target_agent.id,
            summary="Ready for design.",
            artifact_refs=[artifact.id],
            next_objective="Design implementation.",
            constraints_to_preserve=["Do not add runner yet."],
        )
    )
    trace = storage.append_trace_event(
        TraceEvent(
            id="trace-1",
            run_id=run.id,
            agent_run_id=agent_run.id,
            event_type=TraceEventType.ARTIFACT_CREATED,
            payload={"artifact_id": artifact.id},
            duration_ms=3,
        )
    )
    eval_result = storage.create_eval_result(
        EvalResult(
            id="eval-1",
            run_id=run.id,
            artifact_id=artifact.id,
            check_name="design_exists",
            status=EvalStatus.PASS,
            message="Design artifact exists.",
        )
    )

    assert storage.get_agent_run(agent_run.id) == agent_run
    updated_agent_run = agent_run.model_copy(
        update={
            "status": AgentRunStatus.FAILED,
            "finished_at": datetime(2026, 6, 15, 12, 30, tzinfo=UTC),
            "output_summary": "Failed after validation.",
        }
    )
    storage.update_agent_run(updated_agent_run)

    assert storage.get_agent_run(agent_run.id) == updated_agent_run
    assert storage.list_agent_runs_for_run(run.id) == [updated_agent_run]
    assert storage.get_handoff(handoff.id) == handoff
    assert storage.list_handoffs_for_run(run.id) == [handoff]
    assert storage.get_artifact(artifact.id) == artifact
    assert storage.list_artifacts_for_run(run.id) == [artifact]
    assert storage.list_trace_events_for_run(run.id) == [trace]
    assert storage.get_eval_result(eval_result.id) == eval_result
    assert storage.list_eval_results_for_run(run.id) == [eval_result]


def test_agent_session_and_runtime_job_round_trip(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = storage.create_run(Run(id="run-1", task_id=task.id))
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd",
            role="Coder",
            system_prompt="Implement changes.",
        )
    )
    agent_run = storage.create_agent_run(
        AgentRun(id="agent-run-1", run_id=run.id, agent_id=agent.id, step_name="prepare_patch")
    )
    session = storage.create_agent_session(
        AgentSession(
            id="session-1",
            run_id=run.id,
            agent_run_id=agent_run.id,
            agent_id=agent.id,
            step_name="prepare_patch",
            runtime="acp",
            status=AgentSessionStatus.WAITING_APPROVAL,
            resume_strategy="latest_artifact_and_trace",
            requires_approval=True,
        )
    )
    job = storage.create_runtime_job(
        RuntimeJob(
            id="job-1",
            run_id=run.id,
            agent_run_id=agent_run.id,
            agent_session_id=session.id,
            step_name="prepare_patch",
            runtime="acp",
            status=RuntimeJobStatus.APPROVAL_REQUIRED,
            approval_required=True,
        )
    )

    updated_session = session.model_copy(update={"status": AgentSessionStatus.FAILED})
    updated_job = job.model_copy(update={"status": RuntimeJobStatus.FAILED, "message": "failed safely"})
    storage.update_agent_session(updated_session)
    storage.update_runtime_job(updated_job)

    assert storage.get_agent_session(session.id) == updated_session
    assert storage.list_agent_sessions_for_run(run.id) == [updated_session]
    assert storage.get_runtime_job(job.id) == updated_job
    assert storage.list_runtime_jobs_for_run(run.id) == [updated_job]
    assert storage.list_runtime_jobs_for_session(session.id) == [updated_job]


def test_runtime_state_lists_preserve_insert_order_when_clock_moves_backward(
    storage: SQLiteStorage,
) -> None:
    task = storage.create_task(Task(id="clock-task", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = storage.create_run(Run(id="clock-run", task_id=task.id))
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="clock-agent",
            pack_name="code_rd",
            role="Coder",
            system_prompt="Exercise runtime ordering.",
        )
    )
    later_clock = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    rolled_back_clock = later_clock - timedelta(hours=1)
    first_attempt = storage.create_agent_run(
        AgentRun(
            id="clock-attempt-first",
            run_id=run.id,
            agent_id=agent.id,
            step_name="prepare_patch",
            started_at=later_clock,
        )
    )
    current_attempt = storage.create_agent_run(
        AgentRun(
            id="clock-attempt-current",
            run_id=run.id,
            agent_id=agent.id,
            step_name="prepare_patch",
            started_at=rolled_back_clock,
        )
    )
    first_session = storage.create_agent_session(
        AgentSession(
            id="clock-session-first",
            run_id=run.id,
            agent_run_id=first_attempt.id,
            agent_id=agent.id,
            step_name="prepare_patch",
            runtime="acp",
            created_at=later_clock,
            updated_at=later_clock,
        )
    )
    current_session = storage.create_agent_session(
        AgentSession(
            id="clock-session-current",
            run_id=run.id,
            agent_run_id=current_attempt.id,
            agent_id=agent.id,
            step_name="prepare_patch",
            runtime="acp",
            created_at=rolled_back_clock,
            updated_at=rolled_back_clock,
        )
    )
    first_job = storage.create_runtime_job(
        RuntimeJob(
            id="clock-job-first",
            run_id=run.id,
            agent_run_id=first_attempt.id,
            agent_session_id=first_session.id,
            step_name="prepare_patch",
            runtime="acp",
            approval_required=True,
            created_at=later_clock,
            updated_at=later_clock,
        )
    )
    current_job = storage.create_runtime_job(
        RuntimeJob(
            id="clock-job-current",
            run_id=run.id,
            agent_run_id=current_attempt.id,
            agent_session_id=current_session.id,
            step_name="prepare_patch",
            runtime="acp",
            approval_required=True,
            created_at=rolled_back_clock,
            updated_at=rolled_back_clock,
        )
    )

    assert storage.list_agent_runs_for_run(run.id) == [first_attempt, current_attempt]
    assert storage.list_agent_sessions_for_run(run.id) == [first_session, current_session]
    assert storage.list_runtime_jobs_for_run(run.id) == [first_job, current_job]


def test_run_queue_item_and_lock_round_trip(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = storage.create_run(Run(id="run-1", task_id=task.id))

    queue_item = storage.create_run_queue_item(
        RunQueueItem(
            id="queue-1",
            run_id=run.id,
            action="start_run",
            metadata={"lease_token": "secret-token", "local_only": True},
        )
    )
    updated_queue_item = queue_item.model_copy(
        update={"status": RunQueueItemStatus.COMPLETED, "message": "Done."}
    )
    storage.update_run_queue_item(updated_queue_item)

    lock = storage.create_run_lock(
        RunLock(
            id="lock-1",
            run_id=run.id,
            owner="api:start_run",
            metadata={"owner_token": "secret-token", "local_only": True},
        )
    )
    assert storage.get_active_run_lock(run.id) == lock
    released_lock = lock.model_copy(update={"status": RunLockStatus.RELEASED, "released_at": datetime.now(UTC)})
    storage.update_run_lock(released_lock)

    assert storage.get_run_queue_item(queue_item.id) == updated_queue_item
    assert storage.list_run_queue_items_for_run(run.id) == [updated_queue_item]
    assert storage.get_run_lock(lock.id) == released_lock
    assert storage.get_active_run_lock(run.id) is None
    assert storage.list_run_locks_for_run(run.id) == [released_lock]


def test_run_lock_rejects_duplicate_active_lock(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = storage.create_run(Run(id="run-1", task_id=task.id))
    storage.create_run_lock(RunLock(id="lock-1", run_id=run.id, owner="api:first"))

    with pytest.raises(StorageError, match="active lock"):
        storage.create_run_lock(RunLock(id="lock-2", run_id=run.id, owner="api:second"))


def test_run_lock_age_never_releases_active_lock(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = storage.create_run(Run(id="run-1", task_id=task.id))
    stale_lock = storage.create_run_lock(
        RunLock(
            id="lock-1",
            run_id=run.id,
            owner="api:first",
            acquired_at=datetime.now(UTC) - timedelta(minutes=20),
        )
    )

    with pytest.raises(StorageError, match="active lock"):
        storage.create_run_lock(RunLock(id="lock-2", run_id=run.id, owner="api:second"))

    assert storage.get_active_run_lock(run.id) == stale_lock
    assert storage.get_run_lock(stale_lock.id).status == RunLockStatus.ACQUIRED  # type: ignore[union-attr]


def test_run_lock_heartbeat_prevents_false_stale_recovery(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = storage.create_run(Run(id="run-1", task_id=task.id))
    heartbeat_at = datetime.now(UTC)
    active = storage.create_run_lock(
        RunLock(
            id="lock-1",
            run_id=run.id,
            owner="worker:first",
            acquired_at=heartbeat_at - timedelta(minutes=20),
            metadata={"heartbeat_at": heartbeat_at.isoformat()},
        )
    )

    assert storage.get_active_run_lock(run.id) == active
    assert storage.get_run_lock(active.id).status == RunLockStatus.ACQUIRED  # type: ignore[union-attr]


def test_run_queue_and_lock_require_existing_run(storage: SQLiteStorage) -> None:
    with pytest.raises(StorageError, match="run row not found"):
        storage.create_run_queue_item(RunQueueItem(id="queue-1", run_id="missing", action="start_run"))

    with pytest.raises(StorageError, match="run row not found"):
        storage.create_run_lock(RunLock(id="lock-1", run_id="missing", owner="api:start_run"))


def test_runtime_job_session_must_belong_to_same_run(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run_1 = storage.create_run(Run(id="run-1", task_id=task.id))
    run_2 = storage.create_run(Run(id="run-2", task_id=task.id))
    agent = storage.create_agent_definition(
        AgentDefinition(id="agent-1", pack_name="code_rd", role="Coder", system_prompt="Implement changes.")
    )
    agent_run_1 = storage.create_agent_run(
        AgentRun(id="agent-run-1", run_id=run_1.id, agent_id=agent.id, step_name="prepare_patch")
    )
    agent_run_2 = storage.create_agent_run(
        AgentRun(id="agent-run-2", run_id=run_2.id, agent_id=agent.id, step_name="prepare_patch")
    )
    session = storage.create_agent_session(
        AgentSession(
            id="session-1",
            run_id=run_1.id,
            agent_run_id=agent_run_1.id,
            agent_id=agent.id,
            step_name="prepare_patch",
            runtime="acp",
        )
    )

    with pytest.raises(StorageError, match="session mismatch"):
        storage.create_runtime_job(
            RuntimeJob(
                id="job-1",
                run_id=run_2.id,
                agent_run_id=agent_run_2.id,
                agent_session_id=session.id,
                step_name="prepare_patch",
                runtime="acp",
            )
        )


def test_runtime_job_session_must_match_agent_run_step_and_runtime(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = storage.create_run(Run(id="run-1", task_id=task.id))
    agent = storage.create_agent_definition(
        AgentDefinition(id="agent-1", pack_name="code_rd", role="Coder", system_prompt="Implement changes.")
    )
    agent_run_1 = storage.create_agent_run(
        AgentRun(id="agent-run-1", run_id=run.id, agent_id=agent.id, step_name="prepare_patch")
    )
    agent_run_2 = storage.create_agent_run(
        AgentRun(id="agent-run-2", run_id=run.id, agent_id=agent.id, step_name="test_changes")
    )
    session = storage.create_agent_session(
        AgentSession(
            id="session-1",
            run_id=run.id,
            agent_run_id=agent_run_1.id,
            agent_id=agent.id,
            step_name="prepare_patch",
            runtime="acp",
        )
    )

    with pytest.raises(StorageError, match="agent_run_id"):
        storage.create_runtime_job(
            RuntimeJob(
                id="job-1",
                run_id=run.id,
                agent_run_id=agent_run_2.id,
                agent_session_id=session.id,
                step_name="prepare_patch",
                runtime="acp",
            )
        )

    with pytest.raises(StorageError, match="step_name"):
        storage.create_runtime_job(
            RuntimeJob(
                id="job-2",
                run_id=run.id,
                agent_run_id=agent_run_1.id,
                agent_session_id=session.id,
                step_name="test_changes",
                runtime="acp",
            )
        )

    with pytest.raises(StorageError, match="runtime"):
        storage.create_runtime_job(
            RuntimeJob(
                id="job-3",
                run_id=run.id,
                agent_run_id=agent_run_1.id,
                agent_session_id=session.id,
                step_name="prepare_patch",
                runtime="session",
            )
        )


def test_artifact_requires_existing_agent_run(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run = storage.create_run(Run(id="run-1", task_id=task.id))

    with pytest.raises(StorageError):
        storage.create_artifact(
            Artifact(
                id="artifact-1",
                run_id=run.id,
                agent_run_id="missing-agent-run",
                type=ArtifactType.DESIGN_DOC,
                path="data/artifacts/run-1/design.md",
            )
        )


def test_artifact_agent_run_must_belong_to_same_run(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run_1 = storage.create_run(Run(id="run-1", task_id=task.id))
    run_2 = storage.create_run(Run(id="run-2", task_id=task.id))
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd",
            role="Clarifier",
            system_prompt="Clarify requirements.",
        )
    )
    agent_run_for_run_2 = storage.create_agent_run(
        AgentRun(
            id="agent-run-2",
            run_id=run_2.id,
            agent_id=agent.id,
            step_name="Clarifier",
        )
    )

    with pytest.raises(StorageError):
        storage.create_artifact(
            Artifact(
                id="artifact-1",
                run_id=run_1.id,
                agent_run_id=agent_run_for_run_2.id,
                type=ArtifactType.DESIGN_DOC,
                path="data/artifacts/run-1/design.md",
            )
        )


def test_handoff_agent_run_must_belong_to_same_run(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run_1 = storage.create_run(Run(id="run-1", task_id=task.id))
    run_2 = storage.create_run(Run(id="run-2", task_id=task.id))
    source_agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd",
            role="Clarifier",
            system_prompt="Clarify requirements.",
        )
    )
    target_agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-2",
            pack_name="code_rd",
            role="Architect",
            system_prompt="Design implementation.",
        )
    )
    agent_run_for_run_2 = storage.create_agent_run(
        AgentRun(
            id="agent-run-2",
            run_id=run_2.id,
            agent_id=source_agent.id,
            step_name="Clarifier",
        )
    )

    with pytest.raises(StorageError):
        storage.create_handoff(
            Handoff(
                id="handoff-1",
                run_id=run_1.id,
                from_agent_run_id=agent_run_for_run_2.id,
                to_agent_id=target_agent.id,
                summary="Cross-run handoff should fail.",
                next_objective="Design implementation.",
            )
        )


def test_handoff_artifact_refs_must_belong_to_same_run(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run_1 = storage.create_run(Run(id="run-1", task_id=task.id))
    run_2 = storage.create_run(Run(id="run-2", task_id=task.id))
    source_agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd",
            role="Clarifier",
            system_prompt="Clarify requirements.",
        )
    )
    target_agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-2",
            pack_name="code_rd",
            role="Architect",
            system_prompt="Design implementation.",
        )
    )
    agent_run_for_run_1 = storage.create_agent_run(
        AgentRun(
            id="agent-run-1",
            run_id=run_1.id,
            agent_id=source_agent.id,
            step_name="Clarifier",
        )
    )
    agent_run_for_run_2 = storage.create_agent_run(
        AgentRun(
            id="agent-run-2",
            run_id=run_2.id,
            agent_id=source_agent.id,
            step_name="Clarifier",
        )
    )
    artifact_for_run_2 = storage.create_artifact(
        Artifact(
            id="artifact-2",
            run_id=run_2.id,
            agent_run_id=agent_run_for_run_2.id,
            type=ArtifactType.DESIGN_DOC,
            path="data/artifacts/run-2/design.md",
        )
    )

    with pytest.raises(StorageError):
        storage.create_handoff(
            Handoff(
                id="handoff-1",
                run_id=run_1.id,
                from_agent_run_id=agent_run_for_run_1.id,
                to_agent_id=target_agent.id,
                summary="Cross-run artifact ref should fail.",
                artifact_refs=[artifact_for_run_2.id],
                next_objective="Design implementation.",
            )
        )


def test_eval_artifact_must_belong_to_same_run(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run_1 = storage.create_run(Run(id="run-1", task_id=task.id))
    run_2 = storage.create_run(Run(id="run-2", task_id=task.id))
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd",
            role="Clarifier",
            system_prompt="Clarify requirements.",
        )
    )
    agent_run_for_run_2 = storage.create_agent_run(
        AgentRun(
            id="agent-run-2",
            run_id=run_2.id,
            agent_id=agent.id,
            step_name="Clarifier",
        )
    )
    artifact_for_run_2 = storage.create_artifact(
        Artifact(
            id="artifact-2",
            run_id=run_2.id,
            agent_run_id=agent_run_for_run_2.id,
            type=ArtifactType.DESIGN_DOC,
            path="data/artifacts/run-2/design.md",
        )
    )

    with pytest.raises(StorageError):
        storage.create_eval_result(
            EvalResult(
                id="eval-1",
                run_id=run_1.id,
                artifact_id=artifact_for_run_2.id,
                check_name="cross_run_eval",
                status=EvalStatus.PASS,
            )
        )


def test_run_final_artifact_must_belong_to_same_run(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    run_1 = storage.create_run(Run(id="run-1", task_id=task.id))
    run_2 = storage.create_run(Run(id="run-2", task_id=task.id))
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd",
            role="Clarifier",
            system_prompt="Clarify requirements.",
        )
    )
    agent_run_for_run_2 = storage.create_agent_run(
        AgentRun(
            id="agent-run-2",
            run_id=run_2.id,
            agent_id=agent.id,
            step_name="Clarifier",
        )
    )
    artifact_for_run_2 = storage.create_artifact(
        Artifact(
            id="artifact-2",
            run_id=run_2.id,
            agent_run_id=agent_run_for_run_2.id,
            type=ArtifactType.DESIGN_DOC,
            path="data/artifacts/run-2/design.md",
        )
    )

    with pytest.raises(StorageError):
        storage.update_run(run_1.model_copy(update={"final_artifact_id": artifact_for_run_2.id}))


def test_create_run_rejects_final_artifact_from_different_run(storage: SQLiteStorage) -> None:
    task = storage.create_task(Task(id="task-1", title="Task", goal="Goal", workflow_pack="code_rd"))
    storage.create_run(Run(id="run-1", task_id=task.id))
    run_2 = storage.create_run(Run(id="run-2", task_id=task.id))
    agent = storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd",
            role="Clarifier",
            system_prompt="Clarify requirements.",
        )
    )
    agent_run_for_run_2 = storage.create_agent_run(
        AgentRun(
            id="agent-run-2",
            run_id=run_2.id,
            agent_id=agent.id,
            step_name="Clarifier",
        )
    )
    artifact_for_run_2 = storage.create_artifact(
        Artifact(
            id="artifact-2",
            run_id=run_2.id,
            agent_run_id=agent_run_for_run_2.id,
            type=ArtifactType.DESIGN_DOC,
            path="data/artifacts/run-2/design.md",
        )
    )

    with pytest.raises(StorageError):
        storage.create_run(Run(id="run-3", task_id=task.id, final_artifact_id=artifact_for_run_2.id))
