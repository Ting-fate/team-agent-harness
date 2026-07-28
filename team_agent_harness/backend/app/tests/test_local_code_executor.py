from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Lock
from time import sleep

import pytest
from fastapi.testclient import TestClient

from app.core.local_code_executor import LocalCodeExecutor, TestCommandResult as LocalTestCommandResult
from app.core.model_runtime import MockModelAdapter, ModelGateway
from app.core.models import AgentDefinition, ArtifactType, Run, Task
from app.core.runner import WorkflowRunnerError
from app.core.writeback import WritebackConflict, WritebackError, WritebackService
from app.main import create_app
from app.packs.base import ReturnContract, SessionPolicy, WorkflowStep


def test_local_code_executor_prepares_patch_without_modifying_source(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    (repo / ".env").write_text("OPENAI_API_KEY=sk-secret\n", encoding="utf-8")
    original = source.read_text(encoding="utf-8")

    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        workspace_root=tmp_path / "workspaces",
    )
    output = executor.execute(
        task=_task(repo),
        run=Run(id="run-1", task_id="task-1"),
        step=_step("prepare_patch"),
        agent=_agent(),
        context={},
    )

    assert source.read_text(encoding="utf-8") == original
    assert output.artifacts[0].type == ArtifactType.PATCH
    assert "Original repository modified: `false`" in output.artifacts[0].content
    assert "app.py" in output.artifacts[0].content
    assert ".env" not in output.artifacts[0].content
    assert "sk-secret" not in output.artifacts[0].content
    assert output.model_response is not None
    assert output.model_response.mocked is True


def test_local_code_executor_runs_allowed_pytest_in_workspace_copy(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        workspace_root=tmp_path / "workspaces",
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})

    output = executor.execute(
        task=task,
        run=Run(id="run-1", task_id="task-1"),
        step=_step("test_changes"),
        agent=_agent(),
        context={},
    )

    assert output.artifacts[0].type == ArtifactType.TEST_REPORT
    assert "Test command passed" in output.summary
    assert "1 passed" in output.artifacts[0].content
    assert output.eval_results[0].status.value == "pass"


def test_local_code_executor_redacts_secret_like_test_output(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text(
        "def test_leaky_output():\n"
        "    print('api_key=abc123 token=tok123 secret=sauce Authorization: Bearer sk-other')\n"
        "    assert False\n",
        encoding="utf-8",
    )
    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        workspace_root=tmp_path / "workspaces",
    )

    output = executor.execute(
        task=_task(repo, inputs={"test_command": "python -m pytest -q -s"}),
        run=Run(id="run-1", task_id="task-1"),
        step=_step("test_changes"),
        agent=_agent(),
        context={},
    )

    content = output.artifacts[0].content
    assert "api_key=[REDACTED]" in content
    assert "token=[REDACTED]" in content
    assert "secret=[REDACTED]" in content
    assert "Authorization: Bearer [REDACTED]" in content
    assert "abc123" not in content
    assert "tok123" not in content
    assert "sauce" not in content
    assert "sk-other" not in content


def test_local_code_executor_isolates_workspace_by_step(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        workspace_root=tmp_path / "workspaces",
    )
    run = Run(id="run-1", task_id="task-1")

    patch_output = executor.execute(
        task=_task(repo),
        run=run,
        step=_step("prepare_patch"),
        agent=_agent(),
        context={},
    )
    test_output = executor.execute(
        task=_task(repo),
        run=run,
        step=_step("test_changes"),
        agent=_agent(),
        context={},
    )

    assert f"workspaces\\{run.id}\\prepare_patch\\repo" in patch_output.artifacts[0].content or (
        f"workspaces/{run.id}/prepare_patch/repo" in patch_output.artifacts[0].content
    )
    assert f"workspaces\\{run.id}\\test_changes\\repo" in test_output.artifacts[0].content or (
        f"workspaces/{run.id}/test_changes/repo" in test_output.artifacts[0].content
    )
    assert (tmp_path / "workspaces" / run.id / "prepare_patch" / "repo").is_dir()
    assert (tmp_path / "workspaces" / run.id / "test_changes" / "repo").is_dir()


def test_local_code_executor_rejects_dangerous_test_command(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_sample.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        workspace_root=tmp_path / "workspaces",
    )

    with pytest.raises(WorkflowRunnerError, match="not allowed|shell control"):
        executor.execute(
            task=_task(repo, inputs={"test_command": "python -m pytest -q && echo bad"}),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("test_changes"),
            agent=_agent(),
            context={},
        )


def test_local_code_executor_rejects_missing_repository_path(tmp_path) -> None:
    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        workspace_root=tmp_path / "workspaces",
    )
    task = Task(id="task-1", title="Demo", goal="Demo", workflow_pack="code_rd_institutional", inputs={})

    assert executor.supports(task, _step("prepare_patch")) is False
    with pytest.raises(WorkflowRunnerError, match="repository_path"):
        executor.execute(
            task=task,
            run=Run(id="run-1", task_id="task-1"),
            step=_step("prepare_patch"),
            agent=_agent(),
            context={},
        )


def test_writeback_preview_and_approve_applies_verified_diff(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import answer\n\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )

    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})
    run = Run(id="run-1", task_id=task.id)
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=tmp_path / "writeback-workspaces",
    )

    preview = service.preview(run=run, task=task, artifact=artifact)
    result = service.approve(
        run=run,
        task=task,
        artifact=artifact,
        writeback_id=preview["writeback_id"],
        confirm_repository_path=str(repo),
        confirm_patch_hash=preview["patch_hash"],
        expected_base_hashes=preview["base_hashes"],
    )

    assert source.read_text(encoding="utf-8") == "def answer():\n    return 42\n"
    assert result["original_repository_modified"] is True
    assert result["applied_files"] == ["app.py"]
    assert result["test"]["exit_code"] == 0
    trace_dump = "\n".join(str(event.payload) for event in trace_logger.list_for_run("run-1"))
    assert "writeback_previewed" in trace_dump
    assert "writeback_applied" in trace_dump
    assert "return 42" not in trace_dump
    storage.close()


def test_writeback_rejects_original_file_changed_after_preview(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    (repo / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})
    run = Run(id="run-1", task_id=task.id)
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=tmp_path / "writeback-workspaces",
    )
    preview = service.preview(run=run, task=task, artifact=artifact)
    source.write_text("def answer():\n    return 43\n", encoding="utf-8")

    with pytest.raises(WritebackConflict, match="does not apply|changed|Base hash"):
        service.approve(
            run=run,
            task=task,
            artifact=artifact,
            writeback_id=preview["writeback_id"],
            confirm_repository_path=str(repo),
            confirm_patch_hash=preview["patch_hash"],
            expected_base_hashes=preview["base_hashes"],
        )
    assert source.read_text(encoding="utf-8") == "def answer():\n    return 43\n"
    storage.close()


def test_writeback_rejects_sensitive_patch_path(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env.example").write_text("TOKEN=old\n", encoding="utf-8")

    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact(".env.example", "TOKEN=old", "TOKEN=new"),
    )
    service = WritebackService(artifact_store=artifact_store, trace_logger=trace_logger)

    with pytest.raises(WritebackError, match="sensitive"):
        service.preview(run=Run(id="run-1", task_id="task-1"), task=_task(repo), artifact=artifact)
    storage.close()


def test_writeback_rejects_unstructured_patch_artifact(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('old')\n", encoding="utf-8")

    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content="Please change app.py to print new.",
    )
    service = WritebackService(artifact_store=artifact_store, trace_logger=trace_logger)

    with pytest.raises(WritebackError, match="unified diff"):
        service.preview(run=Run(id="run-1", task_id="task-1"), task=_task(repo), artifact=artifact)
    storage.close()


def test_writeback_rejects_symlink_target_inside_focused_directory(tmp_path) -> None:
    repo = tmp_path / "repo"
    focus = repo / "focus"
    focus.mkdir(parents=True)
    target = repo / "victim.py"
    target.write_text("def answer():\n    return 41\n", encoding="utf-8")
    link = focus / "alias.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation is unavailable")

    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("focus/alias.py", "    return 41", "    return 42"),
    )
    service = WritebackService(artifact_store=artifact_store, trace_logger=trace_logger)

    with pytest.raises(WritebackError, match="symlink"):
        service.preview(
            run=Run(id="run-1", task_id="task-1"),
            task=_task(repo, inputs={"focus_paths": ["focus"]}),
            artifact=artifact,
        )
    assert target.read_text(encoding="utf-8") == "def answer():\n    return 41\n"
    storage.close()


def test_writeback_rejects_hardlink_target(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("def answer():\n    return 41\n", encoding="utf-8")
    source = repo / "app.py"
    try:
        os.link(outside, source)
    except OSError:
        pytest.skip("Hardlink creation is unavailable")

    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    service = WritebackService(artifact_store=artifact_store, trace_logger=trace_logger)

    with pytest.raises(WritebackError, match="hardlink|hard link"):
        service.preview(run=Run(id="run-1", task_id="task-1"), task=_task(repo), artifact=artifact)
    assert outside.read_text(encoding="utf-8") == "def answer():\n    return 41\n"
    storage.close()


def test_writeback_rejects_duplicate_patch_target(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_multi_patch_artifact(["app.py", "app.py"]),
    )
    service = WritebackService(artifact_store=artifact_store, trace_logger=trace_logger)

    with pytest.raises(WritebackError, match="same file|duplicate"):
        service.preview(run=Run(id="run-1", task_id="task-1"), task=_task(repo), artifact=artifact)
    storage.close()


def test_writeback_rejects_nul_in_patched_content(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    original = source.read_bytes()
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42\x00"),
    )
    service = WritebackService(artifact_store=artifact_store, trace_logger=trace_logger)

    with pytest.raises(WritebackError, match="NUL|binary"):
        service.preview(run=Run(id="run-1", task_id="task-1"), task=_task(repo), artifact=artifact)
    assert source.read_bytes() == original
    storage.close()


def test_writeback_preserves_preexisting_legacy_temp_file(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    legacy_temp = repo / ".app.py.writeback.tmp"
    legacy_temp.write_bytes(b"unrelated bytes")
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})
    run = Run(id="run-1", task_id=task.id)
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=tmp_path / "writeback-workspaces",
    )
    preview = service.preview(run=run, task=task, artifact=artifact)
    monkeypatch.setattr(
        "app.core.writeback._run_test_command",
        lambda *args, **kwargs: LocalTestCommandResult(command="python -m pytest -q", exit_code=0),
    )

    service.approve(
        run=run,
        task=task,
        artifact=artifact,
        writeback_id=preview["writeback_id"],
        confirm_repository_path=str(repo),
        confirm_patch_hash=preview["patch_hash"],
        expected_base_hashes=preview["base_hashes"],
    )

    assert source.read_text(encoding="utf-8") == "def answer():\n    return 42\n"
    assert legacy_temp.read_bytes() == b"unrelated bytes"
    storage.close()


def test_writeback_rolls_back_all_files_when_later_source_write_fails(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first = repo / "first.py"
    second = repo / "second.py"
    original = "def answer():\n    return 41\n"
    first.write_text(original, encoding="utf-8")
    second.write_text(original, encoding="utf-8")

    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_multi_patch_artifact(["first.py", "second.py"]),
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})
    run = Run(id="run-1", task_id=task.id)
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=tmp_path / "writeback-workspaces",
    )
    preview = service.preview(run=run, task=task, artifact=artifact)

    from app.core import writeback as writeback_module

    replace_owned = writeback_module._replace_with_owned_temp

    def fail_second_new_content(path: Path, content: bytes, temp_path: Path, *, expected_current_hash: str) -> None:
        if path.name == "second.py" and b"return 42" in content:
            raise OSError("injected second-file failure")
        replace_owned(path, content, temp_path, expected_current_hash=expected_current_hash)

    monkeypatch.setattr(writeback_module, "_replace_with_owned_temp", fail_second_new_content)
    monkeypatch.setattr(
        writeback_module,
        "_run_test_command",
        lambda *args, **kwargs: LocalTestCommandResult(command="python -m pytest -q", exit_code=0),
    )

    with pytest.raises(WritebackError, match="rolled back"):
        service.approve(
            run=run,
            task=task,
            artifact=artifact,
            writeback_id=preview["writeback_id"],
            confirm_repository_path=str(repo),
            confirm_patch_hash=preview["patch_hash"],
            expected_base_hashes=preview["base_hashes"],
        )

    assert first.read_text(encoding="utf-8") == original
    assert second.read_text(encoding="utf-8") == original
    assert not any(event.payload.get("action") == "writeback_applied" for event in trace_logger.list_for_run(run.id))
    storage.close()


def test_writeback_rolls_back_source_when_success_trace_persistence_fails(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    original = b"def answer():\r\n    return 41\r\n"
    source.write_bytes(original)

    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})
    run = Run(id="run-1", task_id=task.id)
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=tmp_path / "writeback-workspaces",
    )
    preview = service.preview(run=run, task=task, artifact=artifact)

    from app.core import writeback as writeback_module

    monkeypatch.setattr(
        writeback_module,
        "_run_test_command",
        lambda *args, **kwargs: LocalTestCommandResult(command="python -m pytest -q", exit_code=0),
    )
    record = trace_logger.record

    def fail_success_trace(*, run_id, event_type, payload, agent_run_id=None, duration_ms=None):
        if payload.get("action") == "writeback_applied":
            raise OSError("injected writeback audit failure")
        return record(
            run_id=run_id,
            event_type=event_type,
            payload=payload,
            agent_run_id=agent_run_id,
            duration_ms=duration_ms,
        )

    monkeypatch.setattr(trace_logger, "record", fail_success_trace)

    with pytest.raises(WritebackError, match="audit persistence failed.*rolled back"):
        service.approve(
            run=run,
            task=task,
            artifact=artifact,
            writeback_id=preview["writeback_id"],
            confirm_repository_path=str(repo),
            confirm_patch_hash=preview["patch_hash"],
            expected_base_hashes=preview["base_hashes"],
        )

    assert source.read_bytes() == original
    assert not any(event.payload.get("action") == "writeback_applied" for event in trace_logger.list_for_run(run.id))
    storage.close()


def test_writeback_rolls_back_file_when_atomic_writer_raises_after_replace(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first = repo / "first.py"
    second = repo / "second.py"
    original = b"def answer():\r\n    return 41\r\n"
    first.write_bytes(original)
    second.write_bytes(original)

    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_multi_patch_artifact(["first.py", "second.py"]),
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})
    run = Run(id="run-1", task_id=task.id)
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=tmp_path / "writeback-workspaces",
    )
    preview = service.preview(run=run, task=task, artifact=artifact)

    from app.core import writeback as writeback_module

    replace_owned = writeback_module._replace_with_owned_temp

    def fail_after_second_replace(path: Path, content: bytes, temp_path: Path, *, expected_current_hash: str) -> None:
        replace_owned(path, content, temp_path, expected_current_hash=expected_current_hash)
        if path.name == "second.py" and b"return 42" in content:
            raise OSError("injected post-replace failure")

    monkeypatch.setattr(writeback_module, "_replace_with_owned_temp", fail_after_second_replace)
    monkeypatch.setattr(
        writeback_module,
        "_run_test_command",
        lambda *args, **kwargs: LocalTestCommandResult(command="python -m pytest -q", exit_code=0),
    )

    with pytest.raises(WritebackError, match="rolled back"):
        service.approve(
            run=run,
            task=task,
            artifact=artifact,
            writeback_id=preview["writeback_id"],
            confirm_repository_path=str(repo),
            confirm_patch_hash=preview["patch_hash"],
            expected_base_hashes=preview["base_hashes"],
        )

    assert first.read_bytes() == original
    assert second.read_bytes() == original
    assert not any(event.payload.get("action") == "writeback_applied" for event in trace_logger.list_for_run(run.id))
    storage.close()


@pytest.mark.parametrize(
    ("crash_point", "expected_source", "expected_returncode"),
    [
        ("before_first_replace", b"def answer():\r\n    return 41\r\n", 72),
        ("after_first_replace", b"def answer():\r\n    return 41\r\n", 73),
        ("after_success_trace", b"def answer():\r\n    return 42\n", 74),
    ],
)
def test_writeback_transaction_recovers_after_hard_process_exit(
    tmp_path,
    crash_point,
    expected_source,
    expected_returncode,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_bytes(b"def answer():\r\n    return 41\r\n")
    workspace_root = tmp_path / "output" / "writeback_workspaces"

    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})
    run = Run(id="run-1", task_id=task.id)
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=workspace_root,
    )
    preview = service.preview(run=run, task=task, artifact=artifact)
    storage.close()

    child_code = r'''
import os
from pathlib import Path
import sys

from app.core.artifacts import ArtifactStore
from app.core.local_code_executor import TestCommandResult
from app.core.models import Run, Task
from app.core.storage import SQLiteStorage
from app.core.trace import TraceLogger
from app.core import writeback

db_path, artifact_root, workspace_root, repo, artifact_id, writeback_id, patch_hash, base_hash, crash_point = sys.argv[1:]
storage = SQLiteStorage(db_path, check_same_thread=False)
storage.connect()
storage.init_schema()
logger = TraceLogger(storage)
store = ArtifactStore(artifact_root, storage, logger)
service = writeback.WritebackService(artifact_store=store, trace_logger=logger, workspace_root=workspace_root)
run = storage.get_run("run-1")
artifact = storage.get_artifact(artifact_id)
task = Task(
    id="task-1",
    title="Fix answer",
    goal="Change answer from 41 to 42.",
    workflow_pack="code_rd_institutional",
    inputs={"repository_path": repo, "test_command": "python -m pytest -q"},
)
writeback._run_test_command = lambda *args, **kwargs: TestCommandResult(
    command="python -m pytest -q",
    exit_code=0,
)
if crash_point == "before_first_replace":
    replace = writeback.os.replace
    def crash_before_source_replace(source, target):
        if Path(target).parent.resolve() == Path(repo).resolve() and Path(target).name == "app.py":
            os._exit(72)
        replace(source, target)
    writeback.os.replace = crash_before_source_replace
elif crash_point == "after_first_replace":
    replace_owned = writeback._replace_with_owned_temp
    def crash_after_source_replace(path, content, temp_path, *, expected_current_hash):
        replace_owned(path, content, temp_path, expected_current_hash=expected_current_hash)
        if Path(path).parent.resolve() == Path(repo).resolve() and b"return 42" in content:
            os._exit(73)
    writeback._replace_with_owned_temp = crash_after_source_replace
else:
    discard = writeback._discard_transaction
    def crash_before_committed_journal_cleanup(path):
        if Path(path).name == writeback_id and Path(path, "journal.json").exists():
            os._exit(74)
        discard(path)
    writeback._discard_transaction = crash_before_committed_journal_cleanup
service.approve(
    run=run,
    task=task,
    artifact=artifact,
    writeback_id=writeback_id,
    confirm_repository_path=repo,
    confirm_patch_hash=patch_hash,
    expected_base_hashes={"app.py": base_hash},
)
'''
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            child_code,
            str(tmp_path / "harness.sqlite3"),
            str(tmp_path / "artifacts"),
            str(workspace_root),
            str(repo),
            artifact.id,
            preview["writeback_id"],
            preview["patch_hash"],
            preview["base_hashes"]["app.py"],
            crash_point,
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        timeout=30,
    )
    assert completed.returncode == expected_returncode

    recovered_app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(recovered_app):
        assert source.read_bytes() == expected_source
        actions = [
            event.payload.get("action")
            for event in recovered_app.state.harness.trace_logger.list_for_run(run.id)
        ]
        assert "writeback_recovered_rollback" not in actions
        assert not (workspace_root / "_transactions" / preview["writeback_id"]).exists()
        assert not list(repo.glob(".*.writeback*.tmp"))


def test_writeback_success_trace_with_mixed_targets_rolls_back_every_file(tmp_path, monkeypatch) -> None:
    setup = _leave_successful_writeback_journal(tmp_path, monkeypatch)
    setup["first"].write_bytes(setup["original"])

    recovered_app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    with TestClient(recovered_app):
        assert setup["first"].read_bytes() == setup["original"]
        assert setup["second"].read_bytes() == setup["original"]
        assert not setup["transaction_path"].exists()


def test_writeback_success_trace_with_unknown_target_fails_closed(tmp_path, monkeypatch) -> None:
    setup = _leave_successful_writeback_journal(tmp_path, monkeypatch)
    setup["first"].write_bytes(b"user edit that is neither base nor applied bytes\n")

    recovered_app = create_app(tmp_path / "harness.sqlite3", tmp_path / "artifacts")
    try:
        with pytest.raises(WritebackError, match="changed|unknown|restore"):
            with TestClient(recovered_app):
                pass
    finally:
        recovered_app.state.harness.close()

    assert setup["first"].read_bytes() == b"user edit that is neither base nor applied bytes\n"
    assert setup["second"].read_bytes() != setup["original"]
    assert setup["transaction_path"].exists()


def test_writeback_committed_cleanup_failure_is_explicit_and_recoverable(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})
    run = Run(id="run-1", task_id=task.id)
    workspace_root = tmp_path / "writeback-workspaces"
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=workspace_root,
    )
    preview = service.preview(run=run, task=task, artifact=artifact)
    transaction_path = workspace_root / "_transactions" / preview["writeback_id"]

    from app.core import writeback as writeback_module

    monkeypatch.setattr(
        writeback_module,
        "_run_test_command",
        lambda *args, **kwargs: LocalTestCommandResult(command="python -m pytest -q", exit_code=0),
    )
    rmtree = writeback_module.shutil.rmtree

    def fail_committed_cleanup(path, *args, **kwargs):
        if Path(path) == transaction_path:
            raise OSError("injected cleanup failure")
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(writeback_module.shutil, "rmtree", fail_committed_cleanup)
    with pytest.raises(WritebackError, match="committed|cleanup"):
        service.approve(
            run=run,
            task=task,
            artifact=artifact,
            writeback_id=preview["writeback_id"],
            confirm_repository_path=str(repo),
            confirm_patch_hash=preview["patch_hash"],
            expected_base_hashes=preview["base_hashes"],
        )

    assert source.read_text(encoding="utf-8") == "def answer():\n    return 42\n"
    assert json.loads((transaction_path / "journal.json").read_text(encoding="utf-8"))["state"] == "committed"
    monkeypatch.setattr(writeback_module.shutil, "rmtree", rmtree)
    assert service.recover_pending_transactions() == []
    assert not transaction_path.exists()
    storage.close()


def test_writeback_recovery_rejects_forged_repository_path_before_write(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    victim_repo = tmp_path / "victim"
    victim_repo.mkdir()
    victim = victim_repo / "app.py"
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    task = _task(repo)
    run = Run(id="run-1", task_id=task.id)
    workspace_root = tmp_path / "writeback-workspaces"
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=workspace_root,
    )
    plan = service._build_plan(run=run, task=task, artifact=artifact)
    victim.write_bytes(plan.files[0].new_content.encode("utf-8"))
    victim_before = victim.read_bytes()
    transaction_path, _ = service._prepare_transaction(run=run, artifact=artifact, plan=plan)
    journal_path = transaction_path / "journal.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    journal["repository_path"] = str(victim_repo.resolve())
    journal_path.write_text(json.dumps(journal), encoding="utf-8", newline="")

    with pytest.raises(WritebackError, match="repository|task|facts"):
        service.recover_pending_transactions()

    assert victim.read_bytes() == victim_before
    assert source.read_text(encoding="utf-8") == "def answer():\n    return 41\n"
    assert transaction_path.exists()
    storage.close()


def test_writeback_recovery_of_rolled_back_state_needs_no_backup_or_sqlite_write(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    original = source.read_bytes()
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    task = _task(repo)
    run = Run(id="run-1", task_id=task.id)
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=tmp_path / "writeback-workspaces",
    )
    plan = service._build_plan(run=run, task=task, artifact=artifact)
    transaction_path, transaction = service._prepare_transaction(run=run, artifact=artifact, plan=plan)
    service._set_transaction_state(transaction_path, transaction, "rolled_back")
    (transaction_path / "base" / "app.py").unlink()
    trace_count = len(trace_logger.list_for_run(run.id))

    assert service.recover_pending_transactions() == [plan.writeback_id]
    assert source.read_bytes() == original
    assert not transaction_path.exists()
    assert len(trace_logger.list_for_run(run.id)) == trace_count
    storage.close()


def test_writeback_recovery_rejects_transaction_directory_without_journal(tmp_path) -> None:
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    workspace_root = tmp_path / "writeback-workspaces"
    transaction_path = workspace_root / "_transactions" / ("a" * 24)
    transaction_path.mkdir(parents=True)
    probe = transaction_path / "recovery-probe.txt"
    probe.write_text("preserve", encoding="utf-8")
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=workspace_root,
    )

    with pytest.raises(WritebackError, match="journal|transaction"):
        service.recover_pending_transactions()

    assert probe.read_text(encoding="utf-8") == "preserve"
    storage.close()


def test_writeback_concurrent_duplicate_approve_is_idempotent(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")

    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})
    run = Run(id="run-1", task_id=task.id)
    services = [
        WritebackService(
            artifact_store=artifact_store,
            trace_logger=trace_logger,
            workspace_root=tmp_path / f"writeback-workspaces-{index}",
        )
        for index in range(2)
    ]
    preview = services[0].preview(run=run, task=task, artifact=artifact)
    call_lock = Lock()
    start = Barrier(2)
    test_calls = 0

    from app.core import writeback as writeback_module

    def passing_test(*args, **kwargs) -> LocalTestCommandResult:
        nonlocal test_calls
        with call_lock:
            test_calls += 1
        sleep(0.1)
        return LocalTestCommandResult(command="python -m pytest -q", exit_code=0)

    monkeypatch.setattr(writeback_module, "_run_test_command", passing_test)

    def approve(service: WritebackService) -> dict[str, object]:
        start.wait()
        return service.approve(
            run=run,
            task=task,
            artifact=artifact,
            writeback_id=preview["writeback_id"],
            confirm_repository_path=str(repo),
            confirm_patch_hash=preview["patch_hash"],
            expected_base_hashes=preview["base_hashes"],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(approve, services))

    assert results[0] == results[1]
    assert test_calls == 1
    assert source.read_text(encoding="utf-8") == "def answer():\n    return 42\n"
    applied_events = [
        event
        for event in trace_logger.list_for_run(run.id)
        if event.payload.get("action") == "writeback_applied"
    ]
    assert len(applied_events) == 1
    storage.close()


def _leave_successful_writeback_journal(tmp_path, monkeypatch) -> dict[str, object]:
    repo = tmp_path / "repo"
    repo.mkdir()
    first = repo / "first.py"
    second = repo / "second.py"
    first.write_text("def answer():\n    return 41\n", encoding="utf-8")
    original = first.read_bytes()
    second.write_bytes(original)
    workspace_root = tmp_path / "output" / "writeback_workspaces"
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=_multi_patch_artifact(["first.py", "second.py"]),
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})
    run = Run(id="run-1", task_id=task.id)
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=workspace_root,
    )
    preview = service.preview(run=run, task=task, artifact=artifact)

    from app.core import writeback as writeback_module

    discard_transaction = writeback_module._discard_transaction

    def retain_completed_transaction(path: Path) -> None:
        if path.name == preview["writeback_id"] and (path / "journal.json").exists():
            return
        discard_transaction(path)

    monkeypatch.setattr(writeback_module, "_discard_transaction", retain_completed_transaction)
    monkeypatch.setattr(
        writeback_module,
        "_run_test_command",
        lambda *args, **kwargs: LocalTestCommandResult(command="python -m pytest -q", exit_code=0),
    )
    service.approve(
        run=run,
        task=task,
        artifact=artifact,
        writeback_id=preview["writeback_id"],
        confirm_repository_path=str(repo),
        confirm_patch_hash=preview["patch_hash"],
        expected_base_hashes=preview["base_hashes"],
    )
    monkeypatch.setattr(writeback_module, "_discard_transaction", discard_transaction)
    storage.close()
    return {
        "first": first,
        "second": second,
        "original": original,
        "transaction_path": workspace_root / "_transactions" / preview["writeback_id"],
    }


def _task(repo: Path, *, inputs: dict[str, object] | None = None) -> Task:
    return Task(
        id="task-1",
        title="Fix answer",
        goal="Change answer from 41 to 42.",
        workflow_pack="code_rd_institutional",
        inputs={"repository_path": str(repo), **(inputs or {})},
    )


def _step(name: str) -> WorkflowStep:
    return WorkflowStep(
        name=name,
        agent_role="ImplementationExecutor" if name == "prepare_patch" else "TestExecutor",
        produces_artifact_type=ArtifactType.PATCH.value if name == "prepare_patch" else ArtifactType.TEST_REPORT.value,
        return_contract=ReturnContract(
            required_artifact_types=[
                ArtifactType.PATCH.value if name == "prepare_patch" else ArtifactType.TEST_REPORT.value
            ],
            require_risk_notes=True,
        ),
        runtime="acp",
        session_policy=SessionPolicy(persistent=True, resume_strategy="latest_artifact_and_trace", requires_approval=True),
    )


def _agent() -> AgentDefinition:
    return AgentDefinition(
        id="code_rd_institutional-implementation_executor",
        pack_name="code_rd_institutional",
        role="ImplementationExecutor",
        system_prompt="Prepare local code output.",
        model_config={"provider": "mock", "model": "mock-local-code"},
    )


def _patch_artifact(path: str, old_line: str, new_line: str) -> str:
    return (
        "# Patch\n\n"
        "```diff\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        "@@ -1,2 +1,2 @@\n"
        " def answer():\n"
        f"-{old_line}\n"
        f"+{new_line}\n"
        "```\n"
    )


def _multi_patch_artifact(paths: list[str]) -> str:
    patches = []
    for path in paths:
        patches.extend(
            [
                f"--- a/{path}",
                f"+++ b/{path}",
                "@@ -1,2 +1,2 @@",
                " def answer():",
                "-    return 41",
                "+    return 42",
            ]
        )
    return "# Patch\n\n```diff\n" + "\n".join(patches) + "\n```\n"


def _artifact_store(tmp_path):
    from app.core.artifacts import ArtifactStore
    from app.core.models import AgentRun
    from app.core.storage import SQLiteStorage
    from app.core.trace import TraceLogger

    storage = SQLiteStorage(tmp_path / "harness.sqlite3", check_same_thread=False)
    storage.connect()
    storage.init_schema()
    repository = tmp_path / "repo"
    inputs = {"repository_path": str(repository.resolve())} if repository.is_dir() else {}
    storage.create_task(
        Task(id="task-1", title="Demo", goal="Demo", workflow_pack="code_rd_institutional", inputs=inputs)
    )
    storage.create_run(Run(id="run-1", task_id="task-1"))
    storage.create_agent_definition(
        AgentDefinition(
            id="agent-1",
            pack_name="code_rd_institutional",
            role="ImplementationExecutor",
            system_prompt="Prepare patch.",
        )
    )
    storage.create_agent_run(
        AgentRun(id="agent-run-1", run_id="run-1", agent_id="agent-1", step_name="prepare_patch")
    )
    trace_logger = TraceLogger(storage)
    return ArtifactStore(tmp_path / "artifacts", storage, trace_logger), trace_logger, storage
