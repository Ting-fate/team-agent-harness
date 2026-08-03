from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from app.core import local_code_executor as executor_module
from app.core.local_code_executor import LocalCodeExecutor
from app.core.model_runtime import MockModelAdapter, ModelGateway
from app.core.models import AgentDefinition, ArtifactType, Run, Task
from app.core.runner import WorkflowRunnerError
from app.packs.base import ReturnContract, SessionPolicy, WorkflowStep


@pytest.mark.parametrize(
    "target",
    [
        r"C:\outside\evil_test.py",
        r"..\evil_test.py",
        "..",
        "--rootdir=..",
        "--basetemp=..",
        "@../pytest.args",
    ],
)
def test_local_code_executor_rejects_pytest_targets_outside_workspace(tmp_path, target) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    executor = _executor(tmp_path)

    with pytest.raises(WorkflowRunnerError, match="inside the isolated workspace"):
        executor.execute(
            task=_task(repo, test_command=f'python -m pytest "{target}"'),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("test_changes"),
            agent=_agent(),
            context={},
        )


def test_allowed_test_command_uses_current_interpreter() -> None:
    args = executor_module._parse_allowed_test_command("pytest -q")

    assert args[:3] == [sys.executable, "-m", "pytest"]


@pytest.mark.parametrize(
    "option",
    ["--collect-only", "--setup-plan", "--setup-only", "--version", "--fixtures", "--markers", "-V", "-VV"],
)
def test_allowed_test_command_rejects_non_executing_pytest_modes(option: str) -> None:
    with pytest.raises(WorkflowRunnerError, match="must execute tests"):
        executor_module._parse_allowed_test_command(f"python -m pytest {option}")


def test_allowed_test_command_rejects_pyargs_package_execution() -> None:
    with pytest.raises(WorkflowRunnerError, match="not allowed"):
        executor_module._parse_allowed_test_command("python -m pytest --pyargs installed_package")


@pytest.mark.parametrize("configured_addopts", ["environment", "pytest.ini"])
def test_test_command_overrides_collect_only_configuration(
    tmp_path,
    monkeypatch,
    configured_addopts: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "executed.txt"
    (workspace / "test_execution.py").write_text(
        "from pathlib import Path\n\n"
        "def test_executes():\n"
        "    Path('executed.txt').write_text('yes', encoding='utf-8')\n",
        encoding="utf-8",
    )
    if configured_addopts == "environment":
        monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")
    else:
        (workspace / "pytest.ini").write_text("[pytest]\naddopts = --collect-only\n", encoding="utf-8")

    result = executor_module._run_test_command("python -m pytest -q", workspace, 30)

    assert result.exit_code == 0
    assert marker.read_text(encoding="utf-8") == "yes"


def test_test_command_rejects_pytest_cmdline_hook_spoofed_success(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "conftest.py").write_text(
        "def pytest_cmdline_main(config):\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (workspace / "test_never_runs.py").write_text(
        "def test_never_runs():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    result = executor_module._run_test_command("python -m pytest -q", workspace, 30)

    assert result.exit_code == 0
    assert result.passed is False
    assert result.execution_verified is False


def test_test_command_rejects_sessionfinish_hook_spoofed_success(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "conftest.py").write_text(
        "def pytest_sessionfinish(session, exitstatus):\n"
        "    session.exitstatus = 0\n",
        encoding="utf-8",
    )
    (workspace / "test_failure.py").write_text(
        "def test_failure():\n"
        "    assert False\n",
        encoding="utf-8",
    )

    result = executor_module._run_test_command("python -m pytest -q", workspace, 30)

    assert result.exit_code == 0
    assert result.passed is False
    assert result.failed_tests == 1


def test_test_command_redacts_raw_provider_credentials_and_connection_passwords(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    github_token = "ghp_" + "abcdefghijklmnopqrstuvwxyz123456"
    aws_access_key = "AKIA" + "ABCDEFGHIJKLMNOP"
    secrets = [
        "db-password-should-not-leak",
        github_token,
        aws_access_key,
        "eyJabcdefghijklmno.eyJpqrstuvwxyz012.abcdefghijklmnopqr",
    ]
    (workspace / "test_output.py").write_text(
        "def test_output():\n"
        "    print('postgresql://service:db-password-should-not-leak@db.invalid/app')\n"
        f"    print({github_token!r})\n"
        f"    print({aws_access_key!r})\n"
        "    print('eyJabcdefghijklmno.eyJpqrstuvwxyz012.abcdefghijklmnopqr')\n"
        "    assert True\n",
        encoding="utf-8",
    )

    result = executor_module._run_test_command("python -m pytest -q -s", workspace, 30)

    assert result.passed is True
    for secret in secrets:
        assert secret not in result.stdout
    assert "[REDACTED]" in result.stdout


def test_sanitized_test_environment_uses_allowlist_and_private_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///private")
    monkeypatch.setenv("SENTRY_DSN", "https://private.invalid/1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--collect-only")

    environment = executor_module._sanitized_environment(tmp_path / "workspace")

    assert "DATABASE_URL" not in environment
    assert "SENTRY_DSN" not in environment
    assert "PYTEST_ADDOPTS" not in environment
    assert Path(environment["USERPROFILE"]).is_relative_to(tmp_path)


def test_local_code_executor_rejects_workspace_inside_source_repository(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    executor = LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        workspace_root=repo / "output" / "workspaces",
    )

    with pytest.raises(WorkflowRunnerError, match="must be disjoint"):
        executor.execute(
            task=_task(repo),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("prepare_patch"),
            agent=_agent(),
            context={},
        )

    assert not (repo / "output").exists()


def test_local_code_executor_rejects_oversized_copy_before_creating_workspace(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "large.txt").write_text("x" * 64, encoding="utf-8")
    monkeypatch.setattr(executor_module, "_MAX_WORKSPACE_COPY_BYTES", 32)
    executor = _executor(tmp_path)

    with pytest.raises(WorkflowRunnerError, match="isolated-copy byte limit"):
        executor.execute(
            task=_task(repo),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("prepare_patch"),
            agent=_agent(),
            context={},
        )

    assert (tmp_path / "workspaces").exists() is False


def test_local_code_executor_does_not_persist_absolute_repository_path_in_errors(
    tmp_path,
    monkeypatch,
) -> None:
    repo = tmp_path / "private-parent" / "repo"
    repo.mkdir(parents=True)
    executor = _executor(tmp_path)

    def fail_copy(source_path, workspace_path):
        del workspace_path
        raise WorkflowRunnerError(f"repository file could not be copied safely: {source_path / 'app.py'}")

    monkeypatch.setattr(executor_module, "_prepare_workspace", fail_copy)

    with pytest.raises(WorkflowRunnerError) as exc_info:
        executor.execute(
            task=_task(repo),
            run=Run(id="run-1", task_id="task-1"),
            step=_step("prepare_patch"),
            agent=_agent(),
            context={},
        )

    message = str(exc_info.value)
    assert str(repo.resolve()) not in message
    assert "[REPOSITORY_PATH]" in message


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_workspace_copy_does_not_follow_directory_junctions(tmp_path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (outside / "outside-secret.txt").write_text("must stay outside", encoding="utf-8")
    junction = repo / "linked-outside"
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        pytest.skip(f"Could not create a Windows junction: {result.stderr or result.stdout}")

    try:
        assert junction.is_junction()
        workspace = tmp_path / "workspace" / "repo"
        executor_module._prepare_workspace(repo, workspace)

        assert (workspace / "linked-outside").exists() is False
        assert list(workspace.rglob("outside-secret.txt")) == []
    finally:
        if junction.is_junction():
            os.rmdir(junction)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction race regression")
def test_workspace_copy_revalidates_directory_after_ignore_scan(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    replaceable = repo / "replaceable"
    outside = tmp_path / "outside"
    workspace = tmp_path / "workspace" / "repo"
    replaceable.mkdir(parents=True)
    outside.mkdir()
    (replaceable / "inside.txt").write_text("inside", encoding="utf-8")
    (outside / "outside-secret.txt").write_text("must stay outside", encoding="utf-8")
    original_copy_ignore = executor_module._copy_ignore
    replaced = False

    def replace_directory_after_scan(directory: str, names: list[str]) -> set[str]:
        nonlocal replaced
        ignored = original_copy_ignore(directory, names)
        if Path(directory) == repo and workspace.parent.exists() and not replaced:
            shutil.rmtree(replaceable)
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(replaceable), str(outside)],
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            )
            if result.returncode != 0:
                pytest.skip(f"Could not create a Windows junction: {result.stderr or result.stdout}")
            replaced = True
        return ignored

    monkeypatch.setattr(executor_module, "_copy_ignore", replace_directory_after_scan)
    try:
        executor_module._prepare_workspace(repo, workspace)

        assert replaced is True
        assert (workspace / "replaceable").exists() is False
        assert list(workspace.rglob("outside-secret.txt")) == []
    finally:
        if replaceable.is_junction():
            os.rmdir(replaceable)


def test_workspace_copy_does_not_include_hardlinked_files(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside-notes.txt"
    outside.write_text("must stay outside", encoding="utf-8")
    hardlink = repo / "shared-notes.txt"
    try:
        os.link(outside, hardlink)
    except OSError as exc:
        pytest.skip(f"Could not create a hard link: {exc}")

    workspace = tmp_path / "workspace" / "repo"
    executor_module._prepare_workspace(repo, workspace)

    assert (workspace / "shared-notes.txt").exists() is False
    assert list(workspace.rglob("outside-notes.txt")) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable-mode regression")
def test_workspace_copy_preserves_executable_mode_without_special_bits(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    executable = repo / "run-tests.sh"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    os.chmod(executable, 0o6755)

    workspace = tmp_path / "workspace" / "repo"
    executor_module._prepare_workspace(repo, workspace)

    copied_mode = stat.S_IMODE((workspace / executable.name).stat().st_mode)
    assert copied_mode & 0o777 == 0o755
    assert copied_mode & 0o7000 == 0


def test_sensitive_filename_detection_keeps_normal_source_names() -> None:
    assert executor_module._is_sensitive_name("tokenizer.py") is False
    assert executor_module._is_sensitive_name("passwordless.py") is False
    assert executor_module._is_sensitive_name("secretary.py") is False
    assert executor_module._is_sensitive_name(".npmrc") is True
    assert executor_module._is_sensitive_name("private.pem") is True
    assert executor_module._is_sensitive_name("api_key.local.json") is True


def _executor(tmp_path: Path) -> LocalCodeExecutor:
    return LocalCodeExecutor(
        model_gateway=ModelGateway({"mock": MockModelAdapter()}),
        workspace_root=tmp_path / "workspaces",
    )


def _task(repo: Path, *, test_command: str | None = None) -> Task:
    inputs: dict[str, object] = {
        "repository_path": str(repo),
        "allow_host_test_execution": True,
    }
    if test_command is not None:
        inputs["test_command"] = test_command
    return Task(
        id="task-1",
        title="Security test",
        goal="Verify local execution boundaries.",
        workflow_pack="code_rd_institutional",
        inputs=inputs,
    )


def _step(name: str) -> WorkflowStep:
    artifact_type = ArtifactType.PATCH.value if name == "prepare_patch" else ArtifactType.TEST_REPORT.value
    return WorkflowStep(
        name=name,
        agent_role="ImplementationExecutor" if name == "prepare_patch" else "TestExecutor",
        produces_artifact_type=artifact_type,
        return_contract=ReturnContract(required_artifact_types=[artifact_type], require_risk_notes=True),
        runtime="acp",
        session_policy=SessionPolicy(persistent=True, resume_strategy="latest_artifact_and_trace", requires_approval=True),
    )


def _agent() -> AgentDefinition:
    return AgentDefinition(
        id="code-rd-security-test",
        pack_name="code_rd_institutional",
        role="TestExecutor",
        system_prompt="Review local code output.",
        model_config={"provider": "mock", "model": "mock-local-code"},
    )
