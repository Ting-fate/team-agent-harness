from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier, Lock
from time import sleep
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.local_code_executor import LocalCodeExecutor, TestCommandResult as LocalTestCommandResult
from app.core.model_runtime import (
    MockModelAdapter,
    ModelGateway,
    ModelRuntimeError,
    OpenAICompatibleModelAdapter,
)
from app.core.models import AgentDefinition, AgentRun, AgentRunStatus, ArtifactType, EvalStatus, Handoff, Run, Task
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
    assert "Source repository write requested: `false`" in output.artifacts[0].content
    assert str(repo.resolve()) not in output.artifacts[0].content
    assert "app.py" in output.artifacts[0].content
    assert ".env" not in output.artifacts[0].content
    assert "sk-secret" not in output.artifacts[0].content
    assert output.model_response is not None
    assert output.model_response.mocked is True
    assert output.model_request is not None
    assert str(repo.resolve()) not in str(output.model_request)


def test_local_code_executor_binder_failure_prevents_prepare_patch_model_call(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    adapter = CountingMockAdapter()

    def fail_binder(run, request):
        raise RuntimeError("durable request binding unavailable")

    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": adapter}),
        model_request_binder=fail_binder,
        workspace_root=tmp_path / "workspaces",
    )

    with pytest.raises(RuntimeError, match="durable request binding unavailable"):
        executor.execute(
            task=_task(repo),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("prepare_patch"),
            agent=_agent(),
            context={},
        )

    assert adapter.calls == 0


def test_local_code_executor_recorder_failure_prevents_prepare_patch_http_call(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEAM_AGENT_ALLOW_REAL_MODEL_CALLS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    client_calls: list[dict[str, object]] = []
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=lambda **kwargs: client_calls.append(kwargs))
        )
    )

    def bind_failing_recorder(run, request):
        def fail_persistence(evidence):
            raise RuntimeError("durable trace unavailable")

        return replace(request, provider_attempt_recorder=fail_persistence)

    executor = LocalCodeExecutor(
        model_gateway=ModelGateway(
            {
                "openai": OpenAICompatibleModelAdapter(
                    provider="openai",
                    api_key_env="OPENAI_API_KEY",
                    client=client,
                    endpoint="chat_completions",
                    retry_delay_seconds=0,
                )
            }
        ),
        model_request_binder=bind_failing_recorder,
        workspace_root=tmp_path / "workspaces",
    )
    agent = _agent().model_copy(
        update={"model_settings": {"provider": "openai", "model": "gpt-5"}}
    )

    with pytest.raises(ModelRuntimeError, match="could not be persisted"):
        executor.execute(
            task=_task(repo),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("prepare_patch"),
            agent=agent,
            context={"run_id": "run-1", "real_model_access_confirmed": True},
        )

    assert client_calls == []


def test_local_code_executor_returns_bound_request_for_test_changes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    base_executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 41", "    return 42"),
    )
    adapter = CountingMockAdapter()
    bound_requests = []

    def bind_request(run, request):
        bound_request = replace(
            request,
            metadata={**request.metadata, "bound_run_id": run.id},
        )
        bound_requests.append(bound_request)
        return bound_request

    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": adapter}),
        artifact_store=base_executor.artifact_store,
        patch_workspace_preparer=base_executor.patch_workspace_preparer,
        model_request_binder=bind_request,
        workspace_root=tmp_path / "workspaces",
    )
    from app.core import local_code_executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "_run_test_command",
        lambda *args, **kwargs: LocalTestCommandResult(
            command="python -m pytest -q",
            exit_code=0,
            execution_verified=True,
            total_tests=1,
        ),
    )

    try:
        output = executor.execute(
            task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("test_changes"),
            agent=_agent(),
            context={**context, "agent_run_id": "agent-run-test"},
        )
    finally:
        storage.close()

    assert len(bound_requests) == 1
    assert output.model_request is bound_requests[0]
    assert output.model_request.metadata["bound_run_id"] == "run-1"
    assert output.model_request.metadata["agent_run_id"] == "agent-run-test"
    assert adapter.calls == 1
    assert adapter.requests[0].metadata["bound_run_id"] == "run-1"


def test_local_code_executor_redacts_source_paths_from_task_context_and_artifact(tmp_path) -> None:
    repo = tmp_path / "private" / "repo"
    repo.mkdir(parents=True)
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    source_path = str(repo.resolve())
    source_path_forward = source_path.replace("\\", "/")
    task = _task(repo).model_copy(
        update={
            "title": f"Fix {source_path}",
            "goal": f"Edit {source_path_forward}/app.py without exposing the source path.",
            "constraints": [f"Keep {source_path} private."],
            "acceptance_criteria": [f"No output contains {source_path}."],
        }
    )
    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        workspace_root=tmp_path / "workspaces",
    )

    output = executor.execute(
        task=task,
        run=Run(id="run-1", task_id=task.id),
        step=_step("prepare_patch"),
        agent=_agent(),
        context={"operator_note": f"Repository is {source_path}."},
    )

    assert source_path not in str(output.model_request)
    assert source_path_forward not in str(output.model_request)
    assert source_path not in output.artifacts[0].content
    assert "[LOCAL_PATH]" in str(output.model_request)
    assert "[LOCAL_PATH]" in output.artifacts[0].content


def test_local_code_executor_redacts_credentials_from_normal_source_files(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    github_token = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
    aws_access_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    jwt_token = "eyJ" + "abcdefghijklmno.eyJpqrstuvwxyz012.abcdefghijklmnopqr"
    credentials = {
        "database_password": "db-password-should-not-leak",
        "github_token": github_token,
        "aws_access_key": aws_access_key,
        "jwt": jwt_token,
    }
    (repo / "settings.py").write_text(
        "DATABASE_URL = \"postgresql://service:db-password-should-not-leak@db.invalid/app\"\n"
        f"GITHUB_TOKEN = \"{github_token}\"\n"
        f"AWS_ACCESS_KEY_ID = \"{aws_access_key}\"\n"
        f"JWT_VALUE = \"{jwt_token}\"\n",
        encoding="utf-8",
    )
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

    request_dump = str(output.model_request)
    artifact_dump = output.artifacts[0].content
    for secret in credentials.values():
        assert secret not in request_dump
        assert secret not in artifact_dump
    assert "[REDACTED]" in request_dump


def test_local_code_executor_tests_applied_patch_without_modifying_source(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import answer\n\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 41", "    return 42"),
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q"})

    try:
        output = executor.execute(
            task=task,
            run=Run(id="run-1", task_id="task-1"),
            step=_step("test_changes"),
            agent=_agent(),
            context=context,
        )
    finally:
        storage.close()

    assert source.read_text(encoding="utf-8") == "def answer():\n    return 41\n"
    assert (tmp_path / "workspaces" / "run-1" / "test_changes" / "repo" / "app.py").read_text(
        encoding="utf-8"
    ) == "def answer():\n    return 42\n"
    assert output.artifacts[0].type == ArtifactType.TEST_REPORT
    assert "Test command passed" in output.summary
    assert "1 passed" in output.artifacts[0].content
    assert context["previous_handoff"]["artifact_refs"][0] in output.artifacts[0].content
    assert "Patch applied to isolated workspace: `true`" in output.artifacts[0].content
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
    executor, context, storage = _executor_with_patch(
        tmp_path,
        "# Patch\n\n"
        "```diff\n"
        "--- a/test_sample.py\n"
        "+++ b/test_sample.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def test_leaky_output():\n"
        "     print('api_key=abc123 token=tok123 secret=sauce Authorization: Bearer sk-other')\n"
        "-    assert False\n"
        "+    assert 0\n"
        "```\n",
    )

    try:
        output = executor.execute(
            task=_task(repo, inputs={"test_command": "python -m pytest -q -s"}),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("test_changes"),
            agent=_agent(),
            context=context,
        )
    finally:
        storage.close()

    content = output.artifacts[0].content
    assert "api_key=[REDACTED]" in content
    assert "token=[REDACTED]" in content
    assert "secret=[REDACTED]" in content
    assert "Authorization: Bearer [REDACTED]" in content
    assert "abc123" not in content
    assert "tok123" not in content
    assert "sauce" not in content
    assert "sk-other" not in content
    assert output.eval_results[0].status.value == "fail"


def test_local_code_executor_redacts_source_path_from_test_output_and_review(tmp_path) -> None:
    repo = tmp_path / "private" / "repo"
    repo.mkdir(parents=True)
    source_path = str(repo.resolve())
    (repo / "test_sample.py").write_text(
        "def test_path_output():\n"
        f"    print({source_path!r})\n"
        "    assert False\n",
        encoding="utf-8",
    )
    executor, context, storage = _executor_with_patch(
        tmp_path,
        "# Patch\n\n"
        "```diff\n"
        "--- a/test_sample.py\n"
        "+++ b/test_sample.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def test_path_output():\n"
        f"     print({source_path!r})\n"
        "-    assert False\n"
        "+    assert True\n"
        "```\n",
    )
    task = _task(repo, inputs={"test_command": "python -m pytest -q -s"}).model_copy(
        update={"title": f"Test {source_path}", "goal": f"Verify {source_path} privately."}
    )

    try:
        output = executor.execute(
            task=task,
            run=Run(id="run-1", task_id=task.id),
            step=_step("test_changes"),
            agent=_agent(),
            context=context,
        )
    finally:
        storage.close()

    assert source_path not in str(output.model_request)
    assert source_path not in output.artifacts[0].content
    assert "[LOCAL_PATH]" in output.artifacts[0].content


def test_local_code_executor_rejects_source_changed_after_workspace_preparation(
    tmp_path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 41\n", encoding="utf-8")
    (repo / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 41", "    return 42"),
    )
    preparer = executor.patch_workspace_preparer
    assert preparer is not None
    original_prepare = preparer.prepare_patched_workspace

    def prepare_then_change_source(**kwargs):
        details = original_prepare(**kwargs)
        source.write_text("def answer():\n    return 43\n", encoding="utf-8")
        return details

    monkeypatch.setattr(preparer, "prepare_patched_workspace", prepare_then_change_source)
    try:
        with pytest.raises(WorkflowRunnerError, match="no longer match the patch base"):
            executor.execute(
                task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
                run=Run(id="run-1", task_id="task-1"),
                step=_step("test_changes"),
                agent=_agent(),
                context=context,
            )
    finally:
        storage.close()


def test_local_code_executor_isolates_workspace_by_step(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import answer\n\n\ndef test_answer():\n    assert answer() == 43\n",
        encoding="utf-8",
    )
    executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 42", "    return 43"),
    )
    run = Run(id="run-1", task_id="task-1")

    patch_output = executor.execute(
        task=_task(repo),
        run=run,
        step=_step("prepare_patch"),
        agent=_agent(),
        context={},
    )
    try:
        test_output = executor.execute(
            task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
            run=run,
            step=_step("test_changes"),
            agent=_agent(),
            context=context,
        )
    finally:
        storage.close()

    assert str((tmp_path / "workspaces").resolve()) not in patch_output.artifacts[0].content
    assert str((tmp_path / "workspaces").resolve()) not in test_output.artifacts[0].content
    assert (tmp_path / "workspaces" / run.id / "prepare_patch" / "repo").is_dir()
    assert (tmp_path / "workspaces" / run.id / "test_changes" / "repo").is_dir()
    assert source.read_text(encoding="utf-8") == "def answer():\n    return 42\n"
    assert (tmp_path / "workspaces" / run.id / "test_changes" / "repo" / "app.py").read_text(
        encoding="utf-8"
    ) == "def answer():\n    return 43\n"


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


def test_local_code_executor_rejects_missing_test_command_before_model_call(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        workspace_root=tmp_path / "workspaces",
    )

    with pytest.raises(WorkflowRunnerError, match="test_command is required"):
        executor.execute(
            task=_task(repo),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("test_changes"),
            agent=_agent(),
            context={},
        )


def test_local_code_executor_requires_explicit_host_execution_opt_in(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 41", "    return 42"),
    )

    try:
        with pytest.raises(WorkflowRunnerError, match="allow_host_test_execution=true"):
            executor.execute(
                task=_task(
                    repo,
                    inputs={
                        "test_command": "python -m pytest -q",
                        "allow_host_test_execution": False,
                    },
                ),
                run=Run(id="run-1", task_id="task-1"),
                step=_step("test_changes"),
                agent=_agent(),
                context=context,
            )
    finally:
        storage.close()


def test_local_code_executor_marks_breaking_patch_test_failure(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("def answer():\n    return 42\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import answer\n\n\ndef test_answer():\n    assert answer() == 42\n",
        encoding="utf-8",
    )
    executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 42", "    return 43"),
    )
    adapter = CountingMockAdapter()
    executor.model_gateway = ModelGateway({"mock": adapter})

    try:
        output = executor.execute(
            task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("test_changes"),
            agent=_agent(),
            context=context,
        )
    finally:
        storage.close()

    assert output.eval_results[0].status.value == "fail"
    assert "failed with exit code" in output.summary
    assert adapter.calls == 0
    assert source.read_text(encoding="utf-8") == "def answer():\n    return 42\n"


def test_local_code_executor_rejects_all_skipped_test_run_without_model_review(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "import pytest\n\n"
        "def test_answer():\n"
        "    pytest.skip('no executable assertion')\n",
        encoding="utf-8",
    )
    executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 41", "    return 42"),
    )
    adapter = CountingMockAdapter()
    executor.model_gateway = ModelGateway({"mock": adapter})

    try:
        output = executor.execute(
            task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("test_changes"),
            agent=_agent(),
            context=context,
        )
    finally:
        storage.close()

    assert output.eval_results[0].status == EvalStatus.FAIL
    assert "no non-skipped tests" in output.summary.lower()
    assert adapter.calls == 0


def test_local_code_executor_rejects_invalid_patch_before_model_call(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    executor, context, storage = _executor_with_patch(tmp_path, "# Patch\n\nNo unified diff.\n")
    adapter = CountingMockAdapter()
    executor.model_gateway = ModelGateway({"mock": adapter})

    from app.core import local_code_executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "_run_test_command",
        lambda *args, **kwargs: pytest.fail("test command must not run for an invalid patch"),
    )

    try:
        with pytest.raises(WritebackError, match="fenced unified diff"):
            executor.execute(
                task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
                run=Run(id="run-1", task_id="task-1"),
                step=_step("test_changes"),
                agent=_agent(),
                context=context,
            )
    finally:
        storage.close()
    assert adapter.calls == 0


def test_local_code_executor_rejects_tampered_patch_artifact(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 41", "    return 42"),
    )
    artifact_id = context["previous_handoff"]["artifact_refs"][0]
    artifact = storage.get_artifact(artifact_id)
    assert artifact is not None
    assert executor.artifact_store is not None
    (executor.artifact_store.root_dir / artifact.path).write_text("tampered", encoding="utf-8")

    try:
        with pytest.raises(WorkflowRunnerError, match="content hash"):
            executor.execute(
                task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
                run=Run(id="run-1", task_id="task-1"),
                step=_step("test_changes"),
                agent=_agent(),
                context=context,
            )
    finally:
        storage.close()


def test_local_code_executor_rechecks_patch_after_test_before_model_review(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 41", "    return 42"),
    )
    adapter = CountingMockAdapter()
    executor.model_gateway = ModelGateway({"mock": adapter})
    artifact = storage.get_artifact(context["previous_handoff"]["artifact_refs"][0])
    assert artifact is not None
    assert executor.artifact_store is not None

    def passing_test_that_tampers_artifact(*args, **kwargs):
        (executor.artifact_store.root_dir / artifact.path).write_text("tampered", encoding="utf-8")
        return LocalTestCommandResult(command="python -m pytest -q", exit_code=0)

    from app.core import local_code_executor as executor_module

    monkeypatch.setattr(executor_module, "_run_test_command", passing_test_that_tampers_artifact)
    try:
        with pytest.raises(WorkflowRunnerError, match="content hash"):
            executor.execute(
                task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
                run=Run(id="run-1", task_id="task-1"),
                step=_step("test_changes"),
                agent=_agent(),
                context=context,
            )
    finally:
        storage.close()

    assert adapter.calls == 0


def test_patch_application_preserves_no_newline_marker(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "app.py"
    source.write_text("value = 1", encoding="utf-8", newline="")
    executor, context, storage = _executor_with_patch(
        tmp_path,
        "# Patch\n\n"
        "```diff\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "\\ No newline at end of file\n"
        "+value = 2\n"
        "\\ No newline at end of file\n"
        "```\n",
    )
    try:
        output = executor.execute(
            task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("test_changes"),
            agent=_agent(),
            context=context,
        )
    finally:
        storage.close()

    applied = tmp_path / "workspaces" / "run-1" / "test_changes" / "repo" / "app.py"
    assert applied.read_bytes() == b"value = 2"
    assert output.eval_results[0].status == EvalStatus.FAIL


def test_patch_application_rejects_inconsistent_new_hunk_start(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("value = 1\n", encoding="utf-8")
    executor, context, storage = _executor_with_patch(
        tmp_path,
        "# Patch\n\n"
        "```diff\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +9 @@\n"
        "-value = 1\n"
        "+value = 2\n"
        "```\n",
    )
    try:
        with pytest.raises(WritebackConflict, match="does not apply cleanly"):
            executor.execute(
                task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
                run=Run(id="run-1", task_id="task-1"),
                step=_step("test_changes"),
                agent=_agent(),
                context=context,
            )
    finally:
        storage.close()


def test_local_code_executor_rejects_patch_from_incomplete_attempt(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 41", "    return 42"),
    )
    patch_attempt = storage.get_agent_run("agent-run-1")
    assert patch_attempt is not None
    storage.update_agent_run(patch_attempt.model_copy(update={"status": AgentRunStatus.FAILED}))

    try:
        with pytest.raises(WorkflowRunnerError, match="completed prepare_patch attempt"):
            executor.execute(
                task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
                run=Run(id="run-1", task_id="task-1"),
                step=_step("test_changes"),
                agent=_agent(),
                context=context,
            )
    finally:
        storage.close()


def test_local_code_executor_rejects_patch_from_superseded_completed_attempt(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 41", "    return 42"),
    )
    storage.create_agent_run(
        AgentRun(
            id="agent-run-2",
            run_id="run-1",
            agent_id="agent-1",
            step_name="prepare_patch",
            status=AgentRunStatus.COMPLETED,
        )
    )

    try:
        with pytest.raises(WorkflowRunnerError, match="current prepare_patch attempt"):
            executor.execute(
                task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
                run=Run(id="run-1", task_id="task-1"),
                step=_step("test_changes"),
                agent=_agent(),
                context=context,
            )
    finally:
        storage.close()


def test_local_code_executor_marks_test_timeout_as_failure_without_model_call(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    executor, context, storage = _executor_with_patch(
        tmp_path,
        _patch_artifact("app.py", "    return 41", "    return 42"),
    )
    adapter = CountingMockAdapter()
    executor.model_gateway = ModelGateway({"mock": adapter})

    from app.core import local_code_executor as executor_module

    monkeypatch.setattr(
        executor_module,
        "_run_test_command",
        lambda *args, **kwargs: LocalTestCommandResult(
            command="python -m pytest -q",
            exit_code=None,
            timed_out=True,
        ),
    )
    try:
        output = executor.execute(
            task=_task(repo, inputs={"test_command": "python -m pytest -q"}),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("test_changes"),
            agent=_agent(),
            context=context,
        )
    finally:
        storage.close()

    assert output.eval_results[0].status == EvalStatus.FAIL
    assert adapter.calls == 0


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


def test_local_code_executor_rejects_dynamic_plan_step_with_reserved_name(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        workspace_root=tmp_path / "workspaces",
    )
    task = _task(repo)
    dynamic_step = _step("prepare_patch").model_copy(update={"execution_source": "operator"})

    assert executor.supports(task, dynamic_step) is False


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


def test_writeback_rejects_patch_from_superseded_prepare_attempt(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    stale_artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="stale-patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    storage.create_agent_run(
        AgentRun(
            id="agent-run-2",
            run_id="run-1",
            agent_id="agent-1",
            step_name="prepare_patch",
            status=AgentRunStatus.COMPLETED,
        )
    )
    current_artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-2",
        artifact_type=ArtifactType.PATCH,
        filename="current-patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 43"),
    )
    service = WritebackService(artifact_store=artifact_store, trace_logger=trace_logger)
    run = Run(id="run-1", task_id="task-1")
    task = _task(repo)

    with pytest.raises(WritebackError, match="current completed prepare_patch"):
        service.preview(run=run, task=task, artifact=stale_artifact)

    preview = service.preview(run=run, task=task, artifact=current_artifact)
    assert preview["patch_artifact_id"] == current_artifact.id
    storage.close()


def test_writeback_revalidates_patch_ownership_after_long_running_tests(tmp_path, monkeypatch) -> None:
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
    service = WritebackService(
        artifact_store=artifact_store,
        trace_logger=trace_logger,
        workspace_root=tmp_path / "writeback-workspaces",
    )
    preview = service.preview(run=run, task=task, artifact=artifact)

    from app.core import writeback as writeback_module

    def supersede_during_test(*args, **kwargs):
        storage.create_agent_run(
            AgentRun(
                id="agent-run-2",
                run_id=run.id,
                agent_id="agent-1",
                step_name="prepare_patch",
                status=AgentRunStatus.COMPLETED,
            )
        )
        artifact_store.write_text(
            run_id=run.id,
            agent_run_id="agent-run-2",
            artifact_type=ArtifactType.PATCH,
            filename="newer-patch.md",
            content=_patch_artifact("app.py", "    return 41", "    return 43"),
        )
        return LocalTestCommandResult(command="python -m pytest -q", exit_code=0)

    monkeypatch.setattr(writeback_module, "_run_test_command", supersede_during_test)

    with pytest.raises(WritebackError, match="current completed prepare_patch"):
        service.approve(
            run=run,
            task=task,
            artifact=artifact,
            writeback_id=preview["writeback_id"],
            confirm_repository_path=str(repo),
            confirm_patch_hash=preview["patch_hash"],
            expected_base_hashes=preview["base_hashes"],
        )
    assert source.read_text(encoding="utf-8") == "def answer():\n    return 41\n"
    storage.close()


def test_writeback_preserves_trailing_spaces_on_final_diff_line(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    source = repo / "note.md"
    source.write_text("line\n", encoding="utf-8")
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=(
            "# Patch\n\n"
            "```diff\n"
            "--- a/note.md\n"
            "+++ b/note.md\n"
            "@@ -1 +1 @@\n"
            "-line\n"
            "+Markdown hard break  \n"
            "```\n"
        ),
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
    service.approve(
        run=run,
        task=task,
        artifact=artifact,
        writeback_id=preview["writeback_id"],
        confirm_repository_path=str(repo),
        confirm_patch_hash=preview["patch_hash"],
        expected_base_hashes=preview["base_hashes"],
    )

    assert source.read_bytes() == b"Markdown hard break  \n"
    storage.close()


def test_writeback_rejects_patch_from_failed_prepare_attempt(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 41\n", encoding="utf-8")
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    failed_attempt = storage.create_agent_run(
        AgentRun(
            id="failed-agent-run",
            run_id="run-1",
            agent_id="agent-1",
            step_name="prepare_patch",
            status=AgentRunStatus.FAILED,
        )
    )
    failed_artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id=failed_attempt.id,
        artifact_type=ArtifactType.PATCH,
        filename="failed-patch.md",
        content=_patch_artifact("app.py", "    return 41", "    return 42"),
    )
    service = WritebackService(artifact_store=artifact_store, trace_logger=trace_logger)

    with pytest.raises(WritebackError, match="completed prepare_patch"):
        service.preview(
            run=Run(id="run-1", task_id="task-1"),
            task=_task(repo),
            artifact=failed_artifact,
        )
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
    inputs={
        "repository_path": repo,
        "test_command": "python -m pytest -q",
        "allow_host_test_execution": True,
    },
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


class CountingMockAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    def complete(self, request):
        self.calls += 1
        self.requests.append(request)
        return MockModelAdapter().complete(request)


def _executor_with_patch(tmp_path: Path, patch_content: str):
    artifact_store, trace_logger, storage = _artifact_store(tmp_path)
    artifact = artifact_store.write_text(
        run_id="run-1",
        agent_run_id="agent-run-1",
        artifact_type=ArtifactType.PATCH,
        filename="patch.md",
        content=patch_content,
    )
    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        artifact_store=artifact_store,
        patch_workspace_preparer=WritebackService(
            artifact_store=artifact_store,
            trace_logger=trace_logger,
            workspace_root=tmp_path / "workspaces",
        ),
        workspace_root=tmp_path / "workspaces",
    )
    handoff = storage.create_handoff(Handoff(
        id="handoff-1",
        run_id="run-1",
        from_agent_run_id="agent-run-1",
        to_agent_id=_agent().id,
        summary="Prepared patch.",
        artifact_refs=[artifact.id],
        next_objective="test_changes",
    ))
    return executor, {
        "previous_handoff": handoff.model_dump(mode="json"),
        "dependency_lineage": {
            "prepare_patch": {
                "handoff_id": handoff.id,
                "from_agent_run_id": "agent-run-1",
            }
        },
    }, storage


def _task(repo: Path, *, inputs: dict[str, object] | None = None) -> Task:
    return Task(
        id="task-1",
        title="Fix answer",
        goal="Change answer from 41 to 42.",
        workflow_pack="code_rd_institutional",
        inputs={
            "repository_path": str(repo),
            "allow_host_test_execution": True,
            **(inputs or {}),
        },
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
        id="agent-1",
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
        AgentRun(
            id="agent-run-1",
            run_id="run-1",
            agent_id="agent-1",
            step_name="prepare_patch",
            status=AgentRunStatus.COMPLETED,
        )
    )
    trace_logger = TraceLogger(storage)
    return ArtifactStore(tmp_path / "artifacts", storage, trace_logger), trace_logger, storage
