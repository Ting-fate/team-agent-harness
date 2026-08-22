import base64
import importlib.util
import json
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Iterator

import pytest


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = ROOT.parents[1]
LAUNCHER = ROOT / "scripts" / "harness-launcher.ps1"
SETUP = ROOT / "scripts" / "setup-desktop.ps1"
SHORTCUT = ROOT / "scripts" / "create-desktop-shortcut.ps1"
ROOT_ENTRY = REPOSITORY_ROOT / "Start-Team-Agent-Harness.cmd"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
HARNESS_REQUIRED_PATHS = (
    "/team-selections/validate",
    "/workflow-packs/{pack_name}/team-template",
    "/runs/{run_id}/team",
)
POWERSHELL_PROTECTED_ACL_HELPERS = r"""
function Set-ProtectedTestFileAcl([string]$Path) {
    $directory = Split-Path -Parent $Path
    $directoryAcl = [System.IO.Directory]::GetAccessControl($directory)
    $directoryAcl.SetAccessRuleProtection($false, $true)
    [System.IO.Directory]::SetAccessControl($directory, $directoryAcl)
    $fileAcl = [System.IO.File]::GetAccessControl($Path)
    $fileAcl.SetAccessRuleProtection($true, $true)
    [System.IO.File]::SetAccessControl($Path, $fileAcl)
}
function Get-TestAclSemantics([string]$Path) {
    $acl = [System.IO.File]::GetAccessControl($Path)
    $sections = [System.Security.AccessControl.AccessControlSections]::Owner `
        -bor [System.Security.AccessControl.AccessControlSections]::Group `
        -bor [System.Security.AccessControl.AccessControlSections]::Access
    return "$($acl.AreAccessRulesProtected)|$($acl.GetSecurityDescriptorSddlForm($sections))"
}
"""


def _service_handler(
    service: str,
    requests: list[str] | None = None,
) -> type[BaseHTTPRequestHandler]:
    class ServiceHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if requests is not None:
                requests.append(self.path)
            if service == "harness_slow":
                time.sleep(0.35)
            if service.startswith("harness") and self.path == "/":
                content_type = "text/plain" if service == "harness_plain_ui" else "text/html"
                marker = "" if service == "harness_wrong_ui" else 'id="mainWorkspace"'
                body = f"<!doctype html><main {marker}>Harness UI</main>".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            payload: object
            if service == "litellm" and self.path == "/health/liveliness":
                payload = "I'm alive!"
            elif service.startswith("harness") and self.path == "/health":
                worker = "stopped" if service == "harness_worker_stopped" else "running"
                payload = {"status": "ok", "worker": worker}
            elif service.startswith("harness") and self.path == "/openapi.json":
                if service == "harness_old":
                    payload = {
                        "info": {"title": "Team Agent Harness", "version": "0.0.9"},
                        "paths": {path: {} for path in HARNESS_REQUIRED_PATHS},
                    }
                elif service == "harness_missing_capability":
                    payload = {
                        "info": {"title": "Team Agent Harness", "version": "0.1.0"},
                        "paths": {path: {} for path in HARNESS_REQUIRED_PATHS[:-1]},
                    }
                else:
                    payload = {
                        "info": {"title": "Team Agent Harness", "version": "0.1.0"},
                        "paths": {path: {} for path in HARNESS_REQUIRED_PATHS},
                    }
            elif service.startswith("harness") and self.path == "/model-providers":
                payload = [
                    {
                        "name": "litellm_proxy",
                        "enabled": service.startswith("harness_proxy_enabled"),
                    }
                ]
            elif service.startswith("harness") and self.path.startswith("/runs?"):
                if service == "harness_proxy_enabled_active":
                    payload = [{"id": "active-run", "status": "running"}]
                elif service == "harness_active_job":
                    payload = [{"id": "job-run", "status": "completed"}]
                elif service == "harness_long_history":
                    payload = [
                        {"id": f"history-{index}", "status": "completed"}
                        for index in range(20)
                    ]
                else:
                    payload = []
            elif service == "harness_active_job" and self.path == "/runs/job-run/runtime-jobs":
                payload = [{"run_id": "job-run", "status": "approval_required"}]
            elif service == "harness_long_history" and self.path.endswith("/runtime-jobs"):
                payload = []
            elif (
                service == "chrome"
                and self.path == "/health"
                and self.headers.get("X-Team-Agent-Browser-Proxy") == "1"
            ):
                payload = {
                    "status": "ok",
                    "connected": True,
                    "proxy": f"http://127.0.0.1:{self.server.server_port}",
                    "capabilities": [
                        "atomic_navigate_eval_v2",
                        "pinned_public_egress_v1",
                        "isolated_browser_context_v1",
                    ],
                }
            else:
                self.send_error(404)
                return

            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    return ServiceHandler


@contextmanager
def _healthy_service(service: str, requests: list[str] | None = None) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _service_handler(service, requests))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _unused_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@contextmanager
def _running_project_harness(tmp_path: Path, *, proxy_enabled: bool = False) -> Iterator[int]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    port = _unused_loopback_port()
    environment = os.environ.copy()
    for name in (
        "LITELLM_API_KEY",
        "LITELLM_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "DEEPSEEK_API_KEY",
        "TEAM_AGENT_ALLOW_REAL_MODEL_CALLS",
        "TEAM_AGENT_MODEL_ROUTING_CONFIG",
        "TEAM_AGENT_ALLOW_REAL_WEB_SEARCH",
        "TEAM_AGENT_ALLOW_BROWSER_ACCESS",
        "TEAM_AGENT_BROWSER_CDP_URL",
    ):
        environment.pop(name, None)
    environment["PYTHONPATH"] = str(ROOT)
    if proxy_enabled:
        environment["TEAM_AGENT_ALLOW_REAL_MODEL_CALLS"] = "1"
        environment["TEAM_AGENT_MODEL_ROUTING_CONFIG"] = str(
            ROOT / "config" / "model-routing.litellm.example.json"
        )
        environment["LITELLM_API_KEY"] = "configured-for-test"

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            str(ROOT),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=tmp_path,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"Harness process exited early with code {process.returncode}.")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
                    if json.load(response) == {"status": "ok", "worker": "running"}:
                        break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("Harness process did not become ready.")
        yield port
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _start_script_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("LITELLM_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE", "DEEPSEEK_API_KEY"):
        environment.pop(name, None)
    environment["TEAM_AGENT_ALLOW_BROWSER_ACCESS"] = "1"
    environment["TEAM_AGENT_BROWSER_PROVIDER"] = "chrome"
    return environment


def _configured_start_script_environment() -> dict[str, str]:
    environment = _start_script_environment()
    environment.update(
        {
            "LITELLM_API_KEY": "sk-test-local-only",
            "OPENAI_API_KEY": "test-openai-key",
            "OPENAI_API_BASE": "https://relay.invalid/v1",
            "DEEPSEEK_API_KEY": "test-deepseek-key",
            "TEAM_AGENT_ALLOW_BROWSER_ACCESS": "0",
        }
    )
    return environment


def _reuse_start_script_environment(*, configured: bool = False) -> dict[str, str]:
    environment = _configured_start_script_environment() if configured else _start_script_environment()
    environment["TEAM_AGENT_ALLOW_BROWSER_ACCESS"] = "0"
    return environment


def test_start_script_prefers_dedicated_litellm_environment() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")

    dedicated_assignment = '$DefaultLiteLlmPython = Join-Path $Root ".venv-litellm\\Scripts\\python.exe"'
    dedicated_selection = "elseif (Test-Path $DefaultLiteLlmPython)"

    assert dedicated_assignment in script
    assert dedicated_selection in script
    selection = script[script.index("$LiteLlmPythonExe =") : script.index("$LiteLlmRunner =")]
    assert "$DefaultHarnessPython" in selection
    assert "$Python" not in selection


def test_start_script_keeps_harness_python_out_of_support_runtimes() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    chrome_arguments = script[
        script.index("function Test-ChromeProxyCommandArguments") : script.index("$HarnessBasePython =")
    ]
    chrome_identity = script[
        script.index("function Test-ChromeProxyProcessInfo") : script.index("function Test-ChromeProxyProcess {")
    ]
    chrome_start = script[
        script.index("$BrowserProxyReady = $false") : script.index("Write-Host \"LiteLLM Proxy:")
    ]

    assert "$DefaultHarnessPython" in chrome_arguments
    assert "$DefaultHarnessPython" in chrome_identity
    assert "$ChromeProxyBasePython" in script
    assert "$Python" not in chrome_arguments
    assert "$Python" not in chrome_identity
    assert "Start-Process -FilePath $DefaultHarnessPython" in chrome_start
    assert "Start-Process -FilePath $Python" not in chrome_start


def test_start_script_requires_atomic_browser_proxy_with_pinned_egress() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    harness_probe = script[
        script.index("function Test-HarnessEndpoint") : script.index("function Test-ChromeProxyEndpoint")
    ]
    chrome_probe = script[
        script.index("function Test-ChromeProxyEndpoint") : script.index("function Wait-ForExpectedService")
    ]

    assert '"X-Team-Agent-Browser-Proxy" = "1"' not in harness_probe
    assert '"X-Team-Agent-Browser-Proxy" = "1"' in chrome_probe
    assert '$health.proxy -eq "http://127.0.0.1:$Port"' in chrome_probe
    assert '$health.capabilities) -contains "atomic_navigate_eval_v2"' in chrome_probe
    assert '$health.capabilities) -contains "pinned_public_egress_v1"' in chrome_probe
    assert '$health.capabilities) -contains "isolated_browser_context_v1"' in chrome_probe


def test_start_script_local_health_probes_disable_proxy_and_redirects() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")

    assert "function Invoke-LocalHttpGet" in script
    assert "$handler.UseProxy = $false" in script
    assert "$handler.AllowAutoRedirect = $false" in script
    assert "$client.MaxResponseContentBufferSize = 1048576" in script
    assert "Invoke-RestMethod" not in script


def test_start_script_decodes_litellm_json_string_liveliness_response() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    probe = script[
        script.index("function Test-LiteLlmEndpoint") : script.index("function Test-HarnessEndpoint")
    ]

    assert "ConvertFrom-Json" in probe
    assert '[string]$response -eq "I\'m alive!"' in probe


def test_start_script_requires_worker_ui_and_current_capabilities() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    probe = script[
        script.index("function Test-HarnessEndpoint") : script.index("function Test-ChromeProxyEndpoint")
    ]

    assert '$health.worker -eq "running"' in probe
    assert 'Invoke-LocalHttpGet -Uri "http://127.0.0.1:$Port/" -IncludeMetadata' in probe
    assert '$page.ContentType -eq "text/html"' in probe
    assert "$page.Content.Contains($HarnessUiMarker)" in probe
    assert "/openapi.json" in probe
    assert '$openApi.info.version -eq "0.1.0"' in probe
    for path in HARNESS_REQUIRED_PATHS:
        assert path in script


def test_start_script_gates_litellm_before_starting_control_plane() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")

    litellm_gate = script.index("$LiteLlmReady = $false")
    harness_start = script.index("$HarnessService = Start-HarnessService")
    harness_gate = script.index(
        'if ($EffectiveRouteMode -eq "litellm" -and $LiteLlmReady) {',
        litellm_gate,
    )

    assert litellm_gate < script.index("$LiteLlmVersion =") < harness_start
    assert litellm_gate < script.index("$LiteLlmProcess = Start-Process") < harness_start
    assert harness_gate < harness_start
    assert 'Remove-Item Env:LITELLM_API_KEY -ErrorAction SilentlyContinue' in script[
        harness_gate:harness_start
    ]
    assert '$env:LITELLM_BASE_URL = "http://127.0.0.1:$LiteLlmPort/v1"' in script[
        harness_gate:harness_start
    ]
    assert harness_start < script.index("$BrowserProxyProcess = Start-Process")


def test_start_script_preflights_harness_before_side_effects_and_revalidates_later() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    executable_body = script[script.index("$StartedServices =") :]

    preflight_listener = executable_body.index("$InitialHarnessPid = Get-ListenerProcessId")
    preflight_identity = executable_body.index("Test-HarnessProcessInfo", preflight_listener)
    harness_python_guard = "Test-Path -LiteralPath $Python -PathType Leaf"
    preflight_python = executable_body.index(f"elseif (-not ({harness_python_guard}))")
    output_creation = executable_body.index("New-Item -ItemType Directory -Force $OutputDir")
    litellm_probe = executable_body.index("$LiteLlmPid = if")
    later_listener = executable_body.index("$HarnessListener = Get-RevalidatedHarnessListener")
    restart_block = executable_body[
        executable_body.index('if ($reuseAction -eq "restart_idle")') : executable_body.index(
            '} elseif ($reuseAction -eq "reuse_active")'
        )
    ]
    assert "[string]$HarnessPython" in script
    assert preflight_listener < preflight_identity < preflight_python
    assert preflight_python < output_creation < litellm_probe < later_listener
    assert later_listener < executable_body.rindex(f"if (-not ({harness_python_guard}))")
    assert restart_block.index(harness_python_guard) < restart_block.index(
        "Stop-ExpectedHarnessProcess"
    )
    assert executable_body.count(harness_python_guard) == 3


def test_download_entry_resolves_setup_relative_to_the_extracted_repository() -> None:
    entry = ROOT_ENTRY.read_text(encoding="utf-8")
    setup = SETUP.read_text(encoding="utf-8")

    assert 'set "REPOSITORY_ROOT=%~dp0"' in entry
    assert "%REPOSITORY_ROOT%team_agent_harness\\backend\\scripts\\setup-desktop.ps1" in entry
    assert "$BackendRoot = Split-Path -Parent $PSScriptRoot" in setup
    assert "$RepositoryRoot = Split-Path -Parent (Split-Path -Parent $BackendRoot)" in setup
    assert "Set-Location" not in setup


def test_setup_prefers_python_313_then_312_and_rejects_314_for_litellm() -> None:
    setup = SETUP.read_text(encoding="utf-8")

    assert setup.index('@("-3.13")') < setup.index('@("-3.12")')
    assert "$Minor -in @(12, 13)" in setup
    assert "$version.Minor -ge 14" in setup
    assert '$LiteLlmRequirement = "litellm[proxy]==1.89.2"' in setup


def test_runtime_startup_never_installs_dependencies() -> None:
    setup = SETUP.read_text(encoding="utf-8")
    start = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")

    assert '$LiteLlmRequirement = "litellm[proxy]==1.89.2"' in setup
    assert "pip install" not in start


def test_start_script_falls_back_to_mock_routes_when_credentials_are_incomplete() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    block = script[
        script.index("if ($CredentialProblems.Count -gt 0) {") : script.index(
            "$LiteLlmReady = $false"
        )
    ]

    assert "Remove-Item Env:TEAM_AGENT_MODEL_ROUTING_CONFIG" in block
    assert "Harness will use mock Pack routes" in block


def test_setup_never_passes_provider_credentials_to_child_commands() -> None:
    distribution_scripts = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT_ENTRY, SETUP, SHORTCUT)
    )

    for credential_name in (
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "LITELLM_API_KEY",
        "OPENAI_API_BASE",
    ):
        assert credential_name not in distribution_scripts


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for setup behavior testing")
def test_setup_version_gate_and_ready_state_are_idempotent(tmp_path: Path) -> None:
    setup_path = str(SETUP).replace("'", "''")
    state_path = str(tmp_path / "ready state.json").replace("'", "''")
    fake_python = tmp_path / "fake-python.cmd"
    fake_python.write_bytes(
        b"@echo off\r\n"
        b'if "%~1"=="-c" goto python_code\r\n'
        b'if "%~1"=="-m" goto python_module\r\n'
        b"exit /b 1\r\n"
        b":python_code\r\n"
        b'if "%~2"=="import json" goto python_probe\r\n'
        b'if not "%~2"=="import sys; print(f\'{sys.version_info.major}.{sys.version_info.minor}\')" exit /b 1\r\n'
        b'if not "%~3"=="" exit /b 1\r\n'
        b'>>"%~dp0python-calls.txt" echo version\r\n'
        b"echo 3.13\r\n"
        b"exit /b 0\r\n"
        b":python_probe\r\n"
        b'if not "%~3"=="" exit /b 1\r\n'
        b'>>"%~dp0python-calls.txt" echo probe\r\n'
        b"exit /b 0\r\n"
        b":python_module\r\n"
        b'if not "%~2"=="pip" exit /b 1\r\n'
        b'if not "%~3"=="check" exit /b 1\r\n'
        b'if not "%~4"=="" exit /b 1\r\n'
        b'>>"%~dp0python-calls.txt" echo pip-check\r\n'
        b"exit /b 0\r\n"
    )
    python_path = str(fake_python).replace("'", "''")
    command = f"""
. '{setup_path}' -FunctionsOnly
$before = Test-EnvironmentReady -PythonExe '{python_path}' -StatePath '{state_path}' -DependencyHash 'test-hash' -ProbeCode \"import json\"
Write-SetupState -StatePath '{state_path}' -DependencyHash 'test-hash' -PythonExe '{python_path}'
$first = Test-EnvironmentReady -PythonExe '{python_path}' -StatePath '{state_path}' -DependencyHash 'test-hash' -ProbeCode \"import json\"
$second = Test-EnvironmentReady -PythonExe '{python_path}' -StatePath '{state_path}' -DependencyHash 'test-hash' -ProbeCode \"import json\"
[PSCustomObject]@{{
    Python312 = Test-SupportedBootstrapPythonVersion -Major 3 -Minor 12
    Python313 = Test-SupportedBootstrapPythonVersion -Major 3 -Minor 13
    Python314 = Test-SupportedBootstrapPythonVersion -Major 3 -Minor 14
    Before = $before
    First = $first
    Second = $second
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not result.stderr.strip(), result.stderr
    assert json.loads(result.stdout.strip()) == {
        "Python312": True,
        "Python313": True,
        "Python314": False,
        "Before": False,
        "First": True,
        "Second": True,
    }
    assert (tmp_path / "python-calls.txt").read_text(encoding="ascii").splitlines() == [
        "version",
        "version",
        "probe",
        "pip-check",
        "version",
        "probe",
        "pip-check",
    ]
    assert json.loads((tmp_path / "ready state.json").read_text(encoding="utf-8-sig")) == {
        "schema_version": 2,
        "dependency_hash": "test-hash",
        "python": "3.13",
    }


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for shortcut behavior testing")
def test_desktop_shortcut_runs_setup_before_the_launcher(tmp_path: Path) -> None:
    copied_scripts = (
        tmp_path
        / "Extracted Team Agent Harness"
        / "team_agent_harness"
        / "backend"
        / "scripts"
    )
    copied_scripts.mkdir(parents=True)
    copied_shortcut_script = copied_scripts / SHORTCUT.name
    shutil.copy2(SHORTCUT, copied_shortcut_script)
    shutil.copy2(SETUP, copied_scripts / SETUP.name)
    desktop_path = tmp_path / "Desktop Folder"
    desktop_path.mkdir()

    result = subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(copied_shortcut_script),
            "-DesktopPath",
            str(desktop_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    shortcut_path = desktop_path / "Team Agent Harness Launcher.lnk"
    assert shortcut_path.exists()
    escaped_shortcut = str(shortcut_path).replace("'", "''")
    inspect_command = f"""
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut('{escaped_shortcut}')
[PSCustomObject]@{{
    TargetPath = $shortcut.TargetPath
    Arguments = $shortcut.Arguments
    WorkingDirectory = $shortcut.WorkingDirectory
}} | ConvertTo-Json -Compress
"""
    inspected = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", inspect_command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert inspected.returncode == 0, inspected.stderr
    payload = json.loads(inspected.stdout.strip())
    assert payload["TargetPath"].lower().endswith("powershell.exe")
    assert "setup-desktop.ps1" in payload["Arguments"]
    assert "harness-launcher.ps1" not in payload["Arguments"]
    assert '"' in payload["Arguments"]
    assert Path(payload["WorkingDirectory"]) == copied_scripts.parent


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_start_script_keeps_healthy_services_when_env_is_invalid(tmp_path: Path) -> None:
    invalid_env = tmp_path / "must-not-be-loaded.env"
    invalid_env.write_text("this is not a valid env assignment", encoding="utf-8")
    missing_litellm_python = tmp_path / "missing-litellm-python.exe"

    with ExitStack() as stack:
        litellm_port = stack.enter_context(_healthy_service("litellm"))
        harness_port = stack.enter_context(_running_project_harness(tmp_path / "harness"))
        browser_port = stack.enter_context(_healthy_service("chrome"))
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "start-litellm-harness.ps1"),
                "-LiteLlmPort",
                str(litellm_port),
                "-RouteMode",
                "litellm",
                "-HarnessPort",
                str(harness_port),
                "-HarnessPython",
                sys.executable,
                "-BrowserProxyPort",
                str(browser_port),
                "-LiteLlmPython",
                str(missing_litellm_python),
                "-EnvFile",
                str(invalid_env),
            ],
            cwd=ROOT,
            env=_reuse_start_script_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    assert result.returncode == 2, result.stderr
    assert "Harness UI:" in result.stdout
    assert "Invalid env line" in result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_start_script_litellm_port_conflict_is_only_a_support_warning(tmp_path: Path) -> None:
    litellm_requests: list[str] = []
    with ExitStack() as stack:
        litellm_port = stack.enter_context(_healthy_service("unrelated", litellm_requests))
        harness_port = stack.enter_context(_running_project_harness(tmp_path / "harness"))
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "start-litellm-harness.ps1"),
                "-LiteLlmPort",
                str(litellm_port),
                "-RouteMode",
                "litellm",
                "-HarnessPort",
                str(harness_port),
                "-HarnessPython",
                sys.executable,
                "-LiteLlmPython",
                str(tmp_path / "missing-litellm-python.exe"),
                "-EnvFile",
                str(tmp_path / "missing.env"),
            ],
            cwd=ROOT,
            env=_reuse_start_script_environment(configured=True),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

        assert result.returncode == 2
        assert (
            f"Port {litellm_port} is occupied by PID " in result.stderr
            and "but it is not the expected LiteLLM service." in result.stderr
        )
        assert "Harness UI:" in result.stdout
        with urllib.request.urlopen(f"http://127.0.0.1:{harness_port}/health", timeout=2) as response:
            assert json.load(response) == {"status": "ok", "worker": "running"}

    assert litellm_requests == []


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_start_script_direct_mode_does_not_probe_or_start_litellm(tmp_path: Path) -> None:
    litellm_requests: list[str] = []
    with ExitStack() as stack:
        litellm_port = stack.enter_context(_healthy_service("unrelated", litellm_requests))
        harness_port = stack.enter_context(_running_project_harness(tmp_path / "harness"))
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "start-harness.ps1"),
                "-LiteLlmPort",
                str(litellm_port),
                "-HarnessPort",
                str(harness_port),
                "-HarnessPython",
                sys.executable,
                "-LiteLlmPython",
                str(tmp_path / "missing-litellm-python.exe"),
                "-EnvFile",
                str(tmp_path / "missing.env"),
            ],
            cwd=ROOT,
            env=_reuse_start_script_environment(configured=True),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

        assert result.returncode in {0, 2}, result.stderr
        assert "not the expected LiteLLM service" not in result.stderr
        assert "LiteLLM Proxy: disabled by direct route mode" in result.stdout

    assert litellm_requests == []


def test_start_script_checks_litellm_process_identity_before_http_probe() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    occupied_path = script[
        script.index("$LiteLlmPid = if") : script.index(
            "elseif ($CredentialProblems.Count -gt 0)"
        )
    ]

    assert "Test-LiteLlmProcess" in occupied_path
    assert occupied_path.index("Test-LiteLlmProcess") < occupied_path.index("Test-LiteLlmEndpoint")
    identity = script[
        script.index("function Test-LiteLlmCommandArguments") : script.index("function Test-ChromeProxyCommandArguments")
    ]
    assert "$LiteLlmConfig" in identity
    assert "--config" in identity
    assert '"127.0.0.1"' in identity
    assert "Get-ProcessCommandArguments" in script


def test_start_script_checks_harness_and_chrome_process_identity_before_http() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    harness_path = script[
        script.index("function Get-RevalidatedHarnessListener") : script.index(
            "function Get-HarnessLiteLlmProxyEnabled"
        )
    ]
    chrome_path = script[script.index("$BrowserProxyPid = Get-ListenerProcessId") : script.index("} elseif (-not (Test-Path $ChromeCdpProxy))")]

    assert harness_path.index("Test-HarnessProcessInfo") < harness_path.index("Test-SameProcessInstance")
    assert harness_path.index("Test-SameProcessInstance") < harness_path.index("Test-HarnessEndpoint")
    assert chrome_path.index("Test-ChromeProxyProcess") < chrome_path.index("Test-ChromeProxyEndpoint")


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_start_script_revalidation_rejects_replaced_instance_before_http() -> None:
    start_script = str(ROOT / "scripts" / "start-litellm-harness.ps1").replace("'", "''")
    command = f"""
. '{start_script}' -FunctionsOnly
$script:HttpCalls = 0
$script:ListenerPid = 4242
$initialCreation = [DateTime]::UtcNow.AddMinutes(-5)
$script:CurrentCreation = $initialCreation.AddSeconds(1)
$initialIdentity = [PSCustomObject]@{{
    ProcessId = 4242
    CreationTicks = $initialCreation.Ticks
}}
function Get-ListenerProcessId {{ return $script:ListenerPid }}
function Get-UniqueListenerProcessInfo {{
    return [PSCustomObject]@{{
        ProcessId = 4242
        CreationDate = $script:CurrentCreation
        ExecutablePath = $Python
        CommandLine = ''
    }}
}}
function Test-HarnessProcessInfo {{ return $true }}
function Test-HarnessEndpoint {{
    $script:HttpCalls++
    return $true
}}
$replacedRejected = $false
try {{
    [void](Get-RevalidatedHarnessListener -Port 8014 -InitialIdentity $initialIdentity)
}} catch {{
    $replacedRejected = $_.Exception.Message -like '*changed during startup*'
}}
$callsAfterReplacement = $script:HttpCalls
$script:ListenerPid = $null
$disappearedRejected = $false
try {{
    [void](Get-RevalidatedHarnessListener -Port 8014 -InitialIdentity $initialIdentity)
}} catch {{
    $disappearedRejected = $_.Exception.Message -like '*verified instance disappeared*'
}}
$callsAfterDisappearance = $script:HttpCalls
$freePortResult = Get-RevalidatedHarnessListener -Port 8014 -InitialIdentity $null
$callsAfterInitialFreePort = $script:HttpCalls
$script:ListenerPid = 4242
$script:CurrentCreation = $initialCreation
$sameInstance = Get-RevalidatedHarnessListener -Port 8014 -InitialIdentity $initialIdentity
[PSCustomObject]@{{
    ReplacedRejected = $replacedRejected
    CallsAfterReplacement = $callsAfterReplacement
    DisappearedRejected = $disappearedRejected
    CallsAfterDisappearance = $callsAfterDisappearance
    InitialFreePortReturnedNull = $null -eq $freePortResult
    CallsAfterInitialFreePort = $callsAfterInitialFreePort
    SameInstancePid = $sameInstance.ProcessId
    FinalHttpCalls = $script:HttpCalls
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "ReplacedRejected": True,
        "CallsAfterReplacement": 0,
        "DisappearedRejected": True,
        "CallsAfterDisappearance": 0,
        "InitialFreePortReturnedNull": True,
        "CallsAfterInitialFreePort": 0,
        "SameInstancePid": 4242,
        "FinalHttpCalls": 1,
    }


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_spawned_service_wait_never_probes_an_unrelated_port_owner() -> None:
    requests: list[str] = []
    with _healthy_service("harness", requests) as harness_port:
        start_script = str(ROOT / "scripts" / "start-litellm-harness.ps1").replace("'", "''")
        powershell_path = str(POWERSHELL).replace("'", "''")
        command = f"""
. '{start_script}' -FunctionsOnly
$spawned = Start-Process -FilePath '{powershell_path}' -ArgumentList @('-NoProfile', '-Command', 'Start-Sleep -Seconds 30') -WindowStyle Hidden -PassThru
try {{
    $identity = Get-ProcessInstanceIdentity -Process $spawned
    $serviceInstance = $null
    $outcome = 'ready'
    try {{
        Wait-ForExpectedService `
            -Name 'Team Agent Harness' `
            -Process $spawned `
            -SpawnedIdentity $identity `
            -ObservedServiceInstance ([ref]$serviceInstance) `
            -ServiceRole 'Harness' `
            -Port {harness_port} `
            -TimeoutSeconds 1 `
            -Probe {{ Test-HarnessEndpoint -Port {harness_port} }}
    }} catch {{
        $outcome = 'rejected'
    }}
    [PSCustomObject]@{{ Outcome = $outcome; Identity = [bool]$identity }} | ConvertTo-Json -Compress
}} finally {{
    Stop-SpawnedProcessInstance -Process $spawned -SpawnedIdentity $identity
}}
"""
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {"Outcome": "rejected", "Identity": True}
    assert requests == []


def test_start_script_spawn_cleanup_uses_the_original_process_instance() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    cleanup = script[
        script.index("function Stop-SpawnedProcessInstance") : script.index("function Invoke-LocalHttpGet")
    ]

    assert "ServiceInstance" in cleanup
    assert "ListenerIdentity" in cleanup
    assert "ControlledChain" in cleanup
    assert "Test-SameProcessInstance" in cleanup
    assert "Stop-Process -InputObject $Process" in cleanup
    assert "Stop-Process -Id $LiteLlmProcess.Id" not in script
    assert "Stop-Process -Id $HarnessProcess.Id" not in script
    assert "Stop-Process -Id $BrowserProxyProcess.Id" not in script


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_startup_rollback_reverses_created_services_and_skips_reused_services() -> None:
    start_script = str(ROOT / "scripts" / "start-litellm-harness.ps1").replace("'", "''")
    command = f"""
. '{start_script}' -FunctionsOnly
$script:rollbackCalls = [System.Collections.Generic.List[string]]::new()
function Stop-SpawnedProcessInstance {{
    param($Process, $SpawnedIdentity, $ServiceInstance, $ServiceRole, $Port)
    [void]$script:rollbackCalls.Add("$ServiceRole`:$Port")
}}
$currentProcess = Get-Process -Id $PID
$startedServices = @(
    [PSCustomObject]@{{ Process = $currentProcess; SpawnedIdentity = $null; ServiceInstance = $null; ServiceRole = 'LiteLLM'; Port = 4000 }},
    [PSCustomObject]@{{ Process = $null; SpawnedIdentity = $null; ServiceInstance = $null; ServiceRole = 'Harness'; Port = 8014 }},
    [PSCustomObject]@{{ Process = $currentProcess; SpawnedIdentity = $null; ServiceInstance = $null; ServiceRole = 'ChromeProxy'; Port = 3456 }}
)
Invoke-StartupRollback -StartedServices $startedServices
[PSCustomObject]@{{
    Calls = @($script:rollbackCalls)
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {"Calls": ["ChromeProxy:3456", "LiteLLM:4000"]}


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_startup_rollback_stops_new_services_before_restoring_replaced_harness() -> None:
    start_script = str(ROOT / "scripts" / "start-litellm-harness.ps1").replace("'", "''")
    command = f"""
. '{start_script}' -FunctionsOnly
$script:rollbackCalls = [System.Collections.Generic.List[string]]::new()
function Stop-SpawnedProcessInstance {{
    param($Process, $SpawnedIdentity, $ServiceInstance, $ServiceRole, $Port)
    [void]$script:rollbackCalls.Add("stop:$ServiceRole`:$Port")
}}
$currentProcess = Get-Process -Id $PID
$startedServices = @(
    [PSCustomObject]@{{ Process = $currentProcess; SpawnedIdentity = $null; ServiceInstance = $null; ServiceRole = 'LiteLLM'; Port = 4000 }},
    [PSCustomObject]@{{ Process = $null; RestoreAction = {{ [void]$script:rollbackCalls.Add('restore:Harness:8014') }} }},
    [PSCustomObject]@{{ Process = $currentProcess; SpawnedIdentity = $null; ServiceInstance = $null; ServiceRole = 'Harness'; Port = 8014 }}
)
Invoke-StartupRollback -StartedServices $startedServices
[PSCustomObject]@{{
    Calls = @($script:rollbackCalls)
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "Calls": ["stop:Harness:8014", "stop:LiteLLM:4000", "restore:Harness:8014"]
    }


def test_startup_body_registers_only_new_services_for_fatal_rollback() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    executable_body = script[script.index("$StartedServices =") :]

    assert executable_body.count("$StartedServices.Add") == 4
    assert "Invoke-StartupRollback -StartedServices @($StartedServices)" in executable_body
    restore_registration = executable_body.index("RestoreAction =")
    assert executable_body.index("Stop-ExpectedHarnessProcess") < restore_registration
    assert restore_registration < executable_body.index("$HarnessPid = $null")
    assert executable_body.index("exit 2") < executable_body.index("Invoke-StartupRollback")


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_start_script_cleans_verified_descendant_listener_after_parent_exits(tmp_path: Path) -> None:
    port = _unused_loopback_port()
    start_script = str(ROOT / "scripts" / "start-litellm-harness.ps1").replace("'", "''")
    powershell_path = str(POWERSHELL).replace("'", "''")
    python_path = str(Path(sys.executable)).replace("'", "''")
    root_path = str(ROOT).replace("'", "''")
    child_command = f"""
$child = Start-Process -FilePath '{python_path}' `
    -ArgumentList @('-m', 'uvicorn', 'app.main:app', '--app-dir', '"{root_path}"', '--host', '127.0.0.1', '--port', '{port}') `
    -WorkingDirectory '{root_path}' `
    -WindowStyle Hidden `
    -PassThru
Start-Sleep -Seconds 4
"""
    encoded_child_command = base64.b64encode(child_command.encode("utf-16-le")).decode("ascii")
    command = f"""
. '{start_script}' -FunctionsOnly -HarnessPython '{python_path}'
$spawned = Start-Process -FilePath '{powershell_path}' `
    -ArgumentList @('-NoProfile', '-EncodedCommand', '{encoded_child_command}') `
    -WindowStyle Hidden `
    -PassThru
$spawnedIdentity = Get-ProcessInstanceIdentity -Process $spawned
$serviceInstance = $null
$outcome = 'ready'
try {{
    Wait-ForExpectedService `
        -Name 'Team Agent Harness' `
        -Process $spawned `
        -SpawnedIdentity $spawnedIdentity `
        -ServiceRole 'Harness' `
        -Port {port} `
        -ObservedServiceInstance ([ref]$serviceInstance) `
        -TimeoutSeconds 3 `
        -Probe {{ $false }}
}} catch {{
    $outcome = 'failed'
    Stop-SpawnedProcessInstance `
        -Process $spawned `
        -SpawnedIdentity $spawnedIdentity `
        -ServiceInstance $serviceInstance `
        -ServiceRole 'Harness' `
        -Port {port}
}}
$deadline = [DateTime]::UtcNow.AddSeconds(5)
while ((Get-ListenerProcessId -Port {port}) -and [DateTime]::UtcNow -lt $deadline) {{
    Start-Sleep -Milliseconds 100
}}
[PSCustomObject]@{{
    Outcome = $outcome
    Captured = [bool]$serviceInstance
    ParentExited = $spawned.HasExited
    ListenerPid = Get-ListenerProcessId -Port {port}
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "Outcome": "failed",
        "Captured": True,
        "ParentExited": True,
        "ListenerPid": None,
    }


def test_start_script_freezes_browser_proxy_url_before_harness_spawn() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    assignment = '$env:TEAM_AGENT_BROWSER_CDP_URL = "http://127.0.0.1:$BrowserProxyPort"'
    assert script.index(assignment) < script.index("$HarnessService = Start-HarnessService")
    assert "Remove-Item Env:TEAM_AGENT_BROWSER_CDP_URL" in script


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_start_script_restarts_only_for_proxy_mismatch_proven_idle() -> None:
    start_script = str(ROOT / "scripts" / "start-litellm-harness.ps1").replace("'", "''")
    command = f"""
. '{start_script}' -FunctionsOnly
[PSCustomObject]@{{
    SameState = Get-HarnessReuseAction -ExistingProxyEnabled $true -CurrentProxyReady $true -WorkState idle
    EnableProxy = Get-HarnessReuseAction -ExistingProxyEnabled $false -CurrentProxyReady $true -WorkState idle
    DisableProxy = Get-HarnessReuseAction -ExistingProxyEnabled $true -CurrentProxyReady $false -WorkState idle
    ActiveMismatch = Get-HarnessReuseAction -ExistingProxyEnabled $true -CurrentProxyReady $false -WorkState active
    UnknownMismatch = Get-HarnessReuseAction -ExistingProxyEnabled $true -CurrentProxyReady $false -WorkState unknown
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "SameState": "reuse_current",
        "EnableProxy": "restart_idle",
        "DisableProxy": "reuse_unrestorable",
        "ActiveMismatch": "reuse_active",
        "UnknownMismatch": "reuse_unknown",
    }

    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    mismatch_path = script[
        script.index("$workState = if ($existingProxyEnabled -ne $LiteLlmReady)") : script.index(
            "if (-not $HarnessPid)"
        )
    ]
    assert "Get-HarnessWorkState" in mismatch_path
    assert "Stop-ExpectedHarnessProcess" in mismatch_path
    assert "-DisableLiteLlmProxy" in mismatch_path


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_start_script_work_state_protects_active_runs_jobs_and_unknown_state() -> None:
    with ExitStack() as stack:
        idle_port = stack.enter_context(_healthy_service("harness"))
        active_run_port = stack.enter_context(_healthy_service("harness_proxy_enabled_active"))
        active_job_port = stack.enter_context(_healthy_service("harness_active_job"))
        unknown_port = stack.enter_context(_healthy_service("unrelated"))
        start_script = str(ROOT / "scripts" / "start-litellm-harness.ps1").replace("'", "''")
        command = f"""
. '{start_script}' -FunctionsOnly
[PSCustomObject]@{{
    Idle = Get-HarnessWorkState -Port {idle_port}
    ActiveRun = Get-HarnessWorkState -Port {active_run_port}
    ActiveJob = Get-HarnessWorkState -Port {active_job_port}
    Unknown = Get-HarnessWorkState -Port {unknown_port}
}} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "Idle": "idle",
        "ActiveRun": "active",
        "ActiveJob": "active",
        "Unknown": "unknown",
    }, result.stderr + result.stdout


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_start_script_work_state_is_bounded_and_full_page_fails_closed() -> None:
    requests: list[str] = []
    with _healthy_service("harness_long_history", requests) as harness_port:
        start_script = str(ROOT / "scripts" / "start-litellm-harness.ps1").replace("'", "''")
        command = f"""
. '{start_script}' -FunctionsOnly
Get-HarnessWorkState -Port {harness_port}
"""
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unknown"
    assert requests[0] == "/runs?limit=20&offset=0"
    assert sum(path.startswith("/runs?") for path in requests) == 1
    assert sum(path.endswith("/runtime-jobs") for path in requests) == 20


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_start_script_rejects_unrelated_harness_shape_without_sending_http(tmp_path: Path) -> None:
    requests: list[str] = []
    with _healthy_service("harness_missing_capability", requests) as harness_port:
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "start-litellm-harness.ps1"),
                "-HarnessPort",
                str(harness_port),
                "-HarnessPython",
                str(tmp_path / "missing-harness-python.exe"),
                "-LiteLlmPython",
                str(tmp_path / "missing-litellm-python.exe"),
                "-EnvFile",
                str(tmp_path / "missing.env"),
            ],
            cwd=ROOT,
            env=_reuse_start_script_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    assert result.returncode != 0
    assert "not the expected Team Agent Harness service" in result.stderr
    assert requests == []


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
@pytest.mark.parametrize("harness_python_kind", ["missing", "directory"])
def test_start_script_invalid_harness_python_fails_before_support_side_effects(
    tmp_path: Path,
    harness_python_kind: str,
) -> None:
    project_root = tmp_path / "isolated-project"
    copied_scripts = project_root / "scripts"
    copied_scripts.mkdir(parents=True)
    copied_start_script = copied_scripts / "start-litellm-harness.ps1"
    shutil.copy2(ROOT / "scripts" / "start-litellm-harness.ps1", copied_start_script)
    harness_port = _unused_loopback_port()
    browser_port = _unused_loopback_port()
    requests: list[str] = []
    harness_python = tmp_path / f"{harness_python_kind}-harness-python"
    if harness_python_kind == "directory":
        harness_python.mkdir()

    with _healthy_service("litellm", requests) as litellm_port:
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(copied_start_script),
                "-HarnessPort",
                str(harness_port),
                "-HarnessPython",
                str(harness_python),
                "-LiteLlmPort",
                str(litellm_port),
                "-LiteLlmPython",
                sys.executable,
                "-BrowserProxyPort",
                str(browser_port),
                "-EnvFile",
                str(tmp_path / "missing.env"),
            ],
            cwd=project_root,
            env=_configured_start_script_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    assert result.returncode != 0
    assert "Python venv not found" in result.stderr
    assert requests == []
    assert not (project_root / "output").exists()
    for port in (harness_port, browser_port):
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.2)


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_start_script_missing_credentials_do_not_block_existing_harness(tmp_path: Path) -> None:
    with ExitStack() as stack:
        harness_port = stack.enter_context(_running_project_harness(tmp_path / "harness"))
        unused_support_port = stack.enter_context(_healthy_service("unrelated"))
        result = subprocess.run(
            [
                POWERSHELL,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "start-litellm-harness.ps1"),
                "-HarnessPort",
                str(harness_port),
                "-HarnessPython",
                sys.executable,
                "-LiteLlmPort",
                str(unused_support_port),
                "-LiteLlmPython",
                str(tmp_path / "missing-litellm-python.exe"),
                "-EnvFile",
                str(tmp_path / "missing.env"),
            ],
            cwd=ROOT,
            env=_reuse_start_script_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    assert result.returncode == 2, result.stderr
    assert "Missing or invalid model credentials" in result.stderr
    assert "Harness UI:" in result.stdout


def test_launcher_stops_only_revalidated_project_service_processes() -> None:
    script = LAUNCHER.read_text(encoding="utf-8")

    assert "function Test-ProjectServiceProcess" in script
    assert "function Test-ChromeProxyCommandArguments" in script
    assert "function Get-PortServiceState" in script
    assert "function Stop-ProjectService" in script
    assert "function Get-ProcessCreationTicks" in script
    assert "function Test-SameProcessInstance" in script
    assert "function Get-VerifiedProjectServiceProcess" in script
    assert "CreationTicks" in script
    assert "Get-NetTCPConnection -LocalPort $Port" in script
    assert "Test-ProjectServiceProcess $currentInfo $ServiceRole $Port" in script
    assert "Stop-Process -InputObject $process" in script
    assert "function Invoke-ChromeProxyGracefulShutdown" in script
    assert 'if ($ServiceRole -eq "ChromeProxy")' in script
    graceful_stop = script[
        script.index("function Invoke-ChromeProxyGracefulShutdown") : script.index("function Get-PortServiceState")
    ]
    assert graceful_stop.count("Get-VerifiedProjectServiceProcess") == 2
    assert '"http://127.0.0.1:$Port/shutdown"' in graceful_stop
    assert '"X-Team-Agent-Browser-Proxy", "1"' in graceful_stop
    assert "FailedPids = @($failedPids)" in script
    stop_service = script[
        script.index("function Stop-ProjectService") : script.index("function Complete-StartupMonitoring")
    ]
    process_revalidation = 'if (-not (Test-ProcessObjectMatchesIdentity $process $expectedIdentity)) {'
    assert process_revalidation in stop_service
    assert stop_service.index(process_revalidation) < stop_service.index("Stop-Process -InputObject $process")
    assert "$failedPids += [int]$currentInfo.ProcessId" in stop_service
    assert "Stop-PortProcess" not in script
    assert "Stop-Process -Id $connection.OwningProcess" not in script
    stop_path = script[script.index("Button $T.StopServices") : script.index("Button $T.Refresh")]
    assert "Stop-ProjectService $ChromeProxyPort ChromeProxy" in stop_path
    assert "$T.StopIncomplete" in stop_path


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_chrome_proxy_identity_requires_exact_project_command() -> None:
    launcher_path = str(LAUNCHER).replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
$valid = @(
    $HarnessPython,
    $ChromeCdpProxy,
    '--host',
    '127.0.0.1',
    '--port',
    "$ChromeProxyPort",
    '--chrome-debug-port',
    "$ChromeDebugPort"
)
$wrongEntryPoint = @($valid)
$wrongEntryPoint[1] = Join-Path (Split-Path -Parent $ChromeCdpProxy) 'other_proxy.py'
$wrongDebugPort = @($valid)
$wrongDebugPort[7] = '9999'
[PSCustomObject]@{{
    Valid = Test-ChromeProxyCommandArguments -Arguments $valid -Port $ChromeProxyPort
    WrongEntryPoint = Test-ChromeProxyCommandArguments -Arguments $wrongEntryPoint -Port $ChromeProxyPort
    WrongDebugPort = Test-ChromeProxyCommandArguments -Arguments $wrongDebugPort -Port $ChromeProxyPort
}} | ConvertTo-Json -Compress
"""

    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "Valid": True,
        "WrongEntryPoint": False,
        "WrongDebugPort": False,
    }


def test_start_script_restart_stop_revalidates_process_instance_and_listener() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    stop_path = script[
        script.index("function Stop-ExpectedHarnessProcess") : script.index("function Test-ChromeProxyEndpoint")
    ]

    assert "ExpectedIdentity" in stop_path
    assert "Get-VerifiedServiceProcess" in stop_path
    assert "Test-HarnessProcessInfo" in stop_path

    verifier = script[
        script.index("function Get-VerifiedServiceProcess") : script.index("function Stop-VerifiedProcessIdentity")
    ]
    assert "Test-SameProcessInstance" in verifier
    assert "Get-UniqueListenerProcessInfo -Port $Port" in verifier


def test_launcher_status_distinguishes_project_service_from_other_port_owner() -> None:
    script = LAUNCHER.read_text(encoding="utf-8")

    assert '"TargetRunning"' in script
    assert '"PortOccupied"' in script
    assert '"PortConflict"' in script
    assert "Port-Status 4000 LiteLLM" in script
    assert "Port-Status 8014 Harness" in script


def test_launcher_uses_async_readiness_state_machine_and_supervisor_logs() -> None:
    script = LAUNCHER.read_text(encoding="utf-8")

    assert "Start-Sleep -Milliseconds 800" not in script
    assert "System.Windows.Forms.Timer" in script
    assert "function Get-StartupObservation" in script
    assert "function Test-HarnessUiReady" in script
    assert "function Start-HarnessUiProbe" in script
    assert "function Complete-HarnessUiProbe" in script
    assert "$script:StartupProcess" in script
    assert "-RedirectStandardOutput $script:StartupLogPath" in script
    assert "-RedirectStandardError $script:StartupErrorLogPath" in script
    assert "-PassThru" in script
    assert "$script:StartButton.Enabled = $false" in script
    assert "$client.MaxResponseContentBufferSize = 1048576" in script
    assert "$script:LastOpenUiError" in script
    assert "Start-Process -FilePath $HarnessUrl" in script

    timer_path = script[
        script.index("function Update-StartupState") : script.index("function Stop-StartupMonitoring")
    ]
    assert "Test-HarnessUiReady" not in timer_path
    assert "Invoke-LocalHttpGet" not in timer_path
    assert "Start-HarnessUiProbe" in timer_path

    refresh_path = script[script.index("function Refresh-Status") : script.index("function Get-SensitiveConfigValues")]
    open_path = script[script.index("function Open-HarnessUi") : script.index("function Update-StartupState")]
    assert "Test-HarnessUiReady" not in refresh_path
    assert "Test-HarnessUiReady" in open_path

    start_path = script[script.index("$script:StartButton = Button") : script.index("Button $T.StopServices")]
    assert start_path.index("Stop-HarnessUiProbe") < start_path.index("Start-Process")
    assert start_path.index("$script:LastUiReady = $false") < start_path.index("Start-Process")


def test_launcher_start_path_does_not_gate_harness_on_provider_credentials() -> None:
    script = LAUNCHER.read_text(encoding="utf-8")
    start_path = script[
        script.index("$script:StartButton = Button") : script.index("Button $T.StopServices")
    ]

    assert "Validate-Config" not in script
    assert "Save-EnvFile" in start_path
    assert "Start-Process" in start_path


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_save_preserves_unmanaged_env_lines_without_printing_values(tmp_path: Path) -> None:
    env_file = tmp_path / "launcher.env"
    original_lines = [
        "# keep this comment",
        "LITELLM_API_KEY=old-local",
        "",
        "TEAM_AGENT_ALLOW_BROWSER_ACCESS=1",
        "TEAM_AGENT_BROWSER_PROVIDER=chrome",
        "TAVILY_API_KEY=keep-unmanaged-value",
        "export OPENAI_API_KEY=old-openai",
        "OPENAI_API_KEY=duplicate-openai",
        "OPENAI_API_BASE=https://old.invalid/v1",
        "DEEPSEEK_API_KEY=old-deepseek",
        "# trailing comment",
    ]
    env_file.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
    launcher_path = str(LAUNCHER).replace("'", "''")
    env_path = str(env_file).replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
$script:EnvFile = '{env_path}'
Save-EnvFile `
    -LiteLlmApiKey 'new-local-value' `
    -OpenAiApiKey 'new-openai-value' `
    -OpenAiApiBase 'https://new.invalid/v1' `
    -DeepSeekApiKey 'new-deepseek-value'
"""

    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not result.stdout.strip()
    assert "new-local-value" not in result.stderr
    assert "new-openai-value" not in result.stderr
    assert env_file.read_text(encoding="utf-8-sig").splitlines() == [
        "# keep this comment",
        "LITELLM_API_KEY=new-local-value",
        "",
        "TEAM_AGENT_ALLOW_BROWSER_ACCESS=1",
        "TEAM_AGENT_BROWSER_PROVIDER=chrome",
        "TAVILY_API_KEY=keep-unmanaged-value",
        "OPENAI_API_KEY=new-openai-value",
        "OPENAI_API_BASE=https://new.invalid/v1",
        "DEEPSEEK_API_KEY=new-deepseek-value",
        "# trailing comment",
        "TEAM_AGENT_GPT_ROUTE_MODE=direct",
        "TEAM_AGENT_GPT_RELAY_PROTOCOL=chat_completions",
    ]


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_save_env_atomically_preserves_acl_and_leaves_no_temp_file(tmp_path: Path) -> None:
    env_file = tmp_path / "launcher.env"
    env_file.write_text("# original\nOPENAI_API_KEY=old\n", encoding="utf-8")
    launcher_path = str(LAUNCHER).replace("'", "''")
    env_path = str(env_file).replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
$script:EnvFile = '{env_path}'
{POWERSHELL_PROTECTED_ACL_HELPERS}
Set-ProtectedTestFileAcl $script:EnvFile
$directoryProtected = [System.IO.Directory]::GetAccessControl((Split-Path -Parent $script:EnvFile)).AreAccessRulesProtected
$beforeProtected = [System.IO.File]::GetAccessControl($script:EnvFile).AreAccessRulesProtected
$beforeAcl = Get-TestAclSemantics $script:EnvFile
Save-EnvFile `
    -LiteLlmApiKey 'local-value' `
    -OpenAiApiKey 'openai-value' `
    -OpenAiApiBase 'https://relay.invalid/v1' `
    -DeepSeekApiKey 'deepseek-value'
$afterProtected = [System.IO.File]::GetAccessControl($script:EnvFile).AreAccessRulesProtected
$afterAcl = Get-TestAclSemantics $script:EnvFile
$tempPattern = ".$(Split-Path -Leaf $script:EnvFile).*.tmp"
$backupPattern = ".$(Split-Path -Leaf $script:EnvFile).*.bak"
[PSCustomObject]@{{
    DirectoryProtected = $directoryProtected
    BeforeProtected = $beforeProtected
    AfterProtected = $afterProtected
    SameAcl = $beforeAcl -ceq $afterAcl
    TempCount = @(Get-ChildItem -LiteralPath (Split-Path -Parent $script:EnvFile) -Filter $tempPattern).Count
    BackupCount = @(Get-ChildItem -LiteralPath (Split-Path -Parent $script:EnvFile) -Filter $backupPattern).Count
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip())
    assert payload["DirectoryProtected"] is False, payload
    assert payload["BeforeProtected"] is True, payload
    assert payload["AfterProtected"] is True, payload
    assert payload["SameAcl"] is True, payload
    assert payload["TempCount"] == 0
    assert payload["BackupCount"] == 0


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_atomic_env_commit_failure_preserves_original_file(tmp_path: Path) -> None:
    env_file = tmp_path / "launcher.env"
    original = "# original\nOPENAI_API_KEY=old\n"
    env_file.write_text(original, encoding="utf-8")
    original_on_disk = env_file.read_bytes().decode("utf-8")
    launcher_path = str(LAUNCHER).replace("'", "''")
    env_path = str(env_file).replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
$script:EnvFile = '{env_path}'
function Commit-AtomicEnvFile {{ throw 'forced commit failure' }}
$failed = $false
try {{
    Save-EnvFile `
        -LiteLlmApiKey 'local-value' `
        -OpenAiApiKey 'openai-value' `
        -OpenAiApiBase 'https://relay.invalid/v1' `
        -DeepSeekApiKey 'deepseek-value'
}} catch {{
    $failed = $_.Exception.Message -eq 'forced commit failure'
}}
$tempPattern = ".$(Split-Path -Leaf $script:EnvFile).*.tmp"
[PSCustomObject]@{{
    Failed = $failed
    Content = [System.IO.File]::ReadAllText($script:EnvFile)
    TempCount = @(Get-ChildItem -LiteralPath (Split-Path -Parent $script:EnvFile) -Filter $tempPattern).Count
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "Failed": True,
        "Content": original_on_disk,
        "TempCount": 0,
    }


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_acl_commit_failure_rolls_back_original_file(tmp_path: Path) -> None:
    env_file = tmp_path / "launcher.env"
    original = "# original\nOPENAI_API_KEY=old\n"
    env_file.write_text(original, encoding="utf-8")
    original_on_disk = env_file.read_bytes().decode("utf-8")
    launcher_path = str(LAUNCHER).replace("'", "''")
    env_path = str(env_file).replace("'", "''")
    temporary_path = str(tmp_path / "launcher.tmp").replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
{POWERSHELL_PROTECTED_ACL_HELPERS}
Set-ProtectedTestFileAcl '{env_path}'
$beforeAcl = Get-TestAclSemantics '{env_path}'
$beforeProtected = [System.IO.File]::GetAccessControl('{env_path}').AreAccessRulesProtected
$directoryProtected = [System.IO.Directory]::GetAccessControl((Split-Path -Parent '{env_path}')).AreAccessRulesProtected
$script:AclCallCount = 0
function Set-LauncherFileAccessControl([string]$Path, [object]$AccessControl) {{
    $script:AclCallCount++
    if ($script:AclCallCount -eq 2) {{ throw 'forced destination ACL failure' }}
    [System.IO.File]::SetAccessControl($Path, $AccessControl)
}}
[System.IO.File]::WriteAllText('{temporary_path}', 'replacement')
$failed = $false
try {{
    Commit-AtomicEnvFile -TemporaryPath '{temporary_path}' -DestinationPath '{env_path}'
}} catch {{
    $failed = $_.Exception.Message -eq 'forced destination ACL failure'
}}
$backupPattern = ".$(Split-Path -Leaf '{env_path}').*.bak"
$afterAcl = Get-TestAclSemantics '{env_path}'
$afterProtected = [System.IO.File]::GetAccessControl('{env_path}').AreAccessRulesProtected
[PSCustomObject]@{{
    Failed = $failed
    AclCallCount = $script:AclCallCount
    DirectoryProtected = $directoryProtected
    BeforeProtected = $beforeProtected
    AfterProtected = $afterProtected
    SameAcl = $beforeAcl -ceq $afterAcl
    Content = [System.IO.File]::ReadAllText('{env_path}')
    TempExists = Test-Path -LiteralPath '{temporary_path}'
    BackupCount = @(Get-ChildItem -LiteralPath (Split-Path -Parent '{env_path}') -Filter $backupPattern).Count
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "Failed": True,
        "AclCallCount": 3,
        "DirectoryProtected": False,
        "BeforeProtected": True,
        "AfterProtected": True,
        "SameAcl": True,
        "Content": original_on_disk,
        "TempExists": False,
        "BackupCount": 0,
    }


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_incomplete_acl_rollback_retains_original_backup(tmp_path: Path) -> None:
    env_file = tmp_path / "launcher.env"
    original = "# original\nOPENAI_API_KEY=old\n"
    env_file.write_text(original, encoding="utf-8")
    original_on_disk = env_file.read_bytes().decode("utf-8")
    launcher_path = str(LAUNCHER).replace("'", "''")
    env_path = str(env_file).replace("'", "''")
    temporary_path = str(tmp_path / "launcher.tmp").replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
{POWERSHELL_PROTECTED_ACL_HELPERS}
Set-ProtectedTestFileAcl '{env_path}'
$beforeAcl = Get-TestAclSemantics '{env_path}'
$beforeProtected = [System.IO.File]::GetAccessControl('{env_path}').AreAccessRulesProtected
$directoryProtected = [System.IO.Directory]::GetAccessControl((Split-Path -Parent '{env_path}')).AreAccessRulesProtected
$script:AclCallCount = 0
function Set-LauncherFileAccessControl([string]$Path, [object]$AccessControl) {{
    $script:AclCallCount++
    if ($script:AclCallCount -gt 1) {{ throw 'forced ACL failure' }}
    [System.IO.File]::SetAccessControl($Path, $AccessControl)
}}
[System.IO.File]::WriteAllText('{temporary_path}', 'replacement')
$failureMessage = ''
try {{
    Commit-AtomicEnvFile -TemporaryPath '{temporary_path}' -DestinationPath '{env_path}'
}} catch {{
    $failureMessage = $_.Exception.Message
}}
$backupPattern = ".$(Split-Path -Leaf '{env_path}').*.bak"
$backups = @(Get-ChildItem -LiteralPath (Split-Path -Parent '{env_path}') -Filter $backupPattern)
[PSCustomObject]@{{
    FailureMentionsRetainedBackup = $failureMessage -like '*rollback was incomplete*Original backup retained*'
    AclCallCount = $script:AclCallCount
    Content = [System.IO.File]::ReadAllText('{env_path}')
    TempExists = Test-Path -LiteralPath '{temporary_path}'
    BackupCount = $backups.Count
    BackupContent = if ($backups.Count -eq 1) {{ [System.IO.File]::ReadAllText($backups[0].FullName) }} else {{ '' }}
    BackupProtected = if ($backups.Count -eq 1) {{ [System.IO.File]::GetAccessControl($backups[0].FullName).AreAccessRulesProtected }} else {{ $false }}
    BackupSameAcl = if ($backups.Count -eq 1) {{ (Get-TestAclSemantics $backups[0].FullName) -ceq $beforeAcl }} else {{ $false }}
    BeforeProtected = $beforeProtected
    DirectoryProtected = $directoryProtected
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "FailureMentionsRetainedBackup": True,
        "AclCallCount": 3,
        "Content": original_on_disk,
        "TempExists": False,
        "BackupCount": 1,
        "BackupContent": original_on_disk,
        "BackupProtected": True,
        "BackupSameAcl": True,
        "BeforeProtected": True,
        "DirectoryProtected": False,
    }


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_readiness_requires_verified_project_process_before_http(tmp_path: Path) -> None:
    unrelated_requests: list[str] = []
    with ExitStack() as stack:
        ready_port = stack.enter_context(_running_project_harness(tmp_path / "harness"))
        unrelated_port = stack.enter_context(_healthy_service("harness", unrelated_requests))
        launcher_path = str(LAUNCHER).replace("'", "''")
        python_path = str(Path(sys.executable)).replace("'", "''")
        base_python_path = str(Path(getattr(sys, "_base_executable", sys.executable))).replace("'", "''")
        command = f"""
. '{launcher_path}' -FunctionsOnly
$script:HarnessPython = '{python_path}'
$script:HarnessBasePython = '{base_python_path}'
[PSCustomObject]@{{
    Ready = Test-HarnessUiReady -Port {ready_port}
    Unrelated = Test-HarnessUiReady -Port {unrelated_port}
}} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert not result.stderr.strip(), result.stderr
    assert json.loads(result.stdout.strip()) == {
        "Ready": True,
        "Unrelated": False,
    }
    assert unrelated_requests == []


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_startup_observation_covers_ready_exit_and_timeout_states() -> None:
    launcher_path = str(LAUNCHER).replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
$now = [DateTime]::UtcNow
[PSCustomObject]@{{
    Starting = Get-StartupObservation -UiReady $false -ProcessExited $false -ExitCode 0 -DeadlineUtc $now.AddSeconds(10) -NowUtc $now
    Ready = Get-StartupObservation -UiReady $true -ProcessExited $false -ExitCode 0 -DeadlineUtc $now.AddSeconds(10) -NowUtc $now
    ReadyWithWarnings = Get-StartupObservation -UiReady $true -ProcessExited $true -ExitCode 7 -DeadlineUtc $now.AddSeconds(10) -NowUtc $now
    Failed = Get-StartupObservation -UiReady $false -ProcessExited $true -ExitCode 7 -DeadlineUtc $now.AddSeconds(10) -NowUtc $now
    TimedOut = Get-StartupObservation -UiReady $false -ProcessExited $false -ExitCode 0 -DeadlineUtc $now.AddSeconds(-1) -NowUtc $now
    DeadlineWithReady = Get-StartupObservation -UiReady $true -ProcessExited $false -ExitCode 0 -DeadlineUtc $now.AddSeconds(-1) -NowUtc $now
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not result.stderr.strip(), result.stderr
    assert json.loads(result.stdout.strip()) == {
        "Starting": "Starting",
        "Ready": "Ready",
        "ReadyWithWarnings": "ReadyWithWarnings",
        "Failed": "Failed",
        "TimedOut": "TimedOut",
        "DeadlineWithReady": "ReadyWithWarnings",
    }


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_async_readiness_probe_is_single_flight(tmp_path: Path) -> None:
    with _running_project_harness(tmp_path / "harness") as harness_port:
        launcher_path = str(LAUNCHER).replace("'", "''")
        python_path = str(Path(sys.executable)).replace("'", "''")
        base_python_path = str(Path(getattr(sys, "_base_executable", sys.executable))).replace("'", "''")
        command = f"""
. '{launcher_path}' -FunctionsOnly
$script:HarnessPython = '{python_path}'
$script:HarnessBasePython = '{base_python_path}'
$first = Start-HarnessUiProbe -Port {harness_port}
$second = Start-HarnessUiProbe -Port {harness_port}
$deadline = [DateTime]::UtcNow.AddSeconds(10)
while ($script:ReadinessProbeAsyncResult -and -not $script:ReadinessProbeAsyncResult.IsCompleted -and [DateTime]::UtcNow -lt $deadline) {{
    Start-Sleep -Milliseconds 50
}}
$completed = Complete-HarnessUiProbe
[PSCustomObject]@{{
    First = $first
    Second = $second
    Completed = $completed
    Ready = $script:LastUiReady
    Cleared = $null -eq $script:ReadinessProbeAsyncResult
}} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "First": True,
        "Second": False,
        "Completed": True,
        "Ready": True,
        "Cleared": True,
    }


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_automatic_open_trusts_completed_probe_and_opens_only_once() -> None:
    launcher_path = str(LAUNCHER).replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
$script:openCalls = 0
$script:readinessCalls = 0
$script:StartupUiOpened = $false
$script:OpenUiButton = [PSCustomObject]@{{ Enabled = $false }}
function Test-HarnessUiReady {{
    $script:readinessCalls += 1
    return $false
}}
function Start-Process {{
    param([string]$FilePath)
    $script:openCalls += 1
}}
$first = Open-HarnessUi -Automatic -ReadinessVerified
$second = Open-HarnessUi -Automatic -ReadinessVerified
[PSCustomObject]@{{
    First = $first
    Second = $second
    OpenCalls = $script:openCalls
    ReadinessCalls = $script:readinessCalls
    UiOpened = $script:StartupUiOpened
    ButtonEnabled = $script:OpenUiButton.Enabled
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "First": True,
        "Second": True,
        "OpenCalls": 1,
        "ReadinessCalls": 0,
        "UiOpened": True,
        "ButtonEnabled": True,
    }


def test_launcher_marks_ready_reported_only_after_browser_open_succeeds() -> None:
    script = LAUNCHER.read_text(encoding="utf-8")
    ready_path = script[
        script.index('"Ready" {', script.index("function Update-StartupState")) : script.index(
            '"ReadyWithWarnings" {', script.index("function Update-StartupState")
        )
    ]

    assert "Open-HarnessUi -Automatic -ReadinessVerified" in ready_path
    assert ready_path.index("if (Open-HarnessUi") < ready_path.index(
        "$script:StartupReadyReported = $true"
    )


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_timeout_cleanup_reclaims_supervisor_and_restores_start_button() -> None:
    launcher_path = str(LAUNCHER).replace("'", "''")
    powershell_path = str(POWERSHELL).replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
$script:StartupProcess = Start-Process -FilePath '{powershell_path}' -ArgumentList @('-NoProfile', '-Command', 'Start-Sleep -Seconds 30') -WindowStyle Hidden -PassThru
$supervisorPid = $script:StartupProcess.Id
$script:StartButton = [PSCustomObject]@{{ Enabled = $false }}
$script:StartupTimer = $null
$script:ReadinessProbeAsyncResult = $null
Complete-StartupMonitoring -TerminateSupervisor
Start-Sleep -Milliseconds 200
[PSCustomObject]@{{
    Alive = [bool](Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue)
    ProcessCleared = $null -eq $script:StartupProcess
    StartEnabled = $script:StartButton.Enabled
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {
        "Alive": False,
        "ProcessCleared": True,
        "StartEnabled": True,
    }


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_named_lease_allows_only_one_process_per_normalized_root(tmp_path: Path) -> None:
    project_root = tmp_path / "Lease Root"
    project_root.mkdir()
    launcher_path = str(LAUNCHER).replace("'", "''")
    first_root = str(project_root).replace("'", "''")
    second_root = (str(project_root).upper() + "\\.").replace("'", "''")
    first_command = f"""
. '{launcher_path}' -FunctionsOnly
$lease = Acquire-LauncherLease -ProjectRoot '{first_root}'
if (-not $lease) {{ Write-Output 'blocked'; exit 3 }}
Write-Output 'acquired'
[Console]::Out.Flush()
Start-Sleep -Seconds 30
"""
    second_command = f"""
. '{launcher_path}' -FunctionsOnly
$lease = Acquire-LauncherLease -ProjectRoot '{second_root}'
if ($lease) {{
    Write-Output 'acquired'
    Release-LauncherLease -Lease $lease
}} else {{
    Write-Output 'blocked'
}}
"""
    first = subprocess.Popen(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", first_command],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        assert first.stdout is not None
        assert first.stdout.readline().strip() == "acquired"
        second = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", second_command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        assert second.returncode == 0, second.stderr
        assert second.stdout.strip() == "blocked"
    finally:
        first.terminate()
        first.wait(timeout=5)

    recovered = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", second_command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout.strip() == "acquired"


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_diagnostics_redact_credentials_and_personal_paths() -> None:
    launcher_path = str(LAUNCHER).replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
Protect-LauncherText `
    -Text 'Authorization: Bearer bearer-secret OPENAI_API_KEY=env-secret known-secret https://user:pass@example.com C:\\Users\\Private\\project' `
    -SensitiveValues @('known-secret') `
    -HomePath 'C:\\Users\\Private'
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "bearer-secret" not in result.stdout
    assert "env-secret" not in result.stdout
    assert "known-secret" not in result.stdout
    assert "user:pass" not in result.stdout
    assert "C:\\Users\\Private" not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert "%USERPROFILE%" in result.stdout


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_reads_base_python_from_project_venv_metadata(tmp_path: Path) -> None:
    unicode_root = tmp_path / "\u8def\u5f84\u9a8c\u8bc1"
    venv_root = unicode_root / ".venv"
    venv_python = venv_root / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.touch()
    base_python = unicode_root / "\u57fa\u7840-python.exe"
    (venv_root / "pyvenv.cfg").write_text(
        f"home = {tmp_path}\nexecutable = {base_python}\n",
        encoding="utf-8",
    )
    launcher_path = str(LAUNCHER).replace("'", "''")
    venv_python_path = str(venv_python).replace("'", "''")
    base_python_path = str(base_python).replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
[string]::Equals(
    (Get-PythonBaseExecutable '{venv_python_path}'),
    '{base_python_path}',
    [System.StringComparison]::OrdinalIgnoreCase
) | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not result.stderr.strip(), result.stderr
    assert json.loads(result.stdout.strip()) is True


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_recognizes_only_expected_project_service_commands() -> None:
    launcher_path = str(LAUNCHER).replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
$HarnessBasePython = Join-Path $Root "test-base-python.exe"
$LiteLlmBasePython = Join-Path $Root "test-litellm-base-python.exe"
$WrongRoot = Join-Path $Root "alternate-root"
$harness = [PSCustomObject]@{{
    ExecutablePath = $HarnessPython
    CommandLine = ('"{{0}}" -m uvicorn app.main:app --app-dir "{{1}}" --host 127.0.0.1 --port 8014' -f $HarnessPython, $Root)
}}
$litellm = [PSCustomObject]@{{
    ExecutablePath = $LiteLlmPython
    CommandLine = ('"{{0}}" "{{1}}" {{2}} "{{3}}" --config "{{4}}" --host 127.0.0.1 --port 4000' -f $LiteLlmPython, $LiteLlmRunner, $ProjectRootArgument, $Root, $LiteLlmConfig)
}}
$litellmBaseProcess = [PSCustomObject]@{{
    ExecutablePath = $LiteLlmBasePython
    CommandLine = ('"{{0}}" "{{1}}" {{2}} "{{3}}" --config "{{4}}" --host 127.0.0.1 --port 4000' -f $LiteLlmPython, $LiteLlmRunner, $ProjectRootArgument, $Root, $LiteLlmConfig)
}}
$litellmWrongConfig = [PSCustomObject]@{{
    ExecutablePath = $LiteLlmPython
    CommandLine = ('"{{0}}" "{{1}}" {{2}} "{{3}}" --config alternate.yaml --host 127.0.0.1 --port 4000' -f $LiteLlmPython, $LiteLlmRunner, $ProjectRootArgument, $Root)
}}
$litellmWrongRoot = [PSCustomObject]@{{
    ExecutablePath = $LiteLlmPython
    CommandLine = ('"{{0}}" "{{1}}" {{2}} "{{3}}" --config "{{4}}" --host 127.0.0.1 --port 4000' -f $LiteLlmPython, $LiteLlmRunner, $ProjectRootArgument, $WrongRoot, $LiteLlmConfig)
}}
$litellmPublicHost = [PSCustomObject]@{{
    ExecutablePath = $LiteLlmPython
    CommandLine = ('"{{0}}" "{{1}}" {{2}} "{{3}}" --config "{{4}}" --host 0.0.0.0 --port 4000' -f $LiteLlmPython, $LiteLlmRunner, $ProjectRootArgument, $Root, $LiteLlmConfig)
}}
$harnessPublicHost = [PSCustomObject]@{{
    ExecutablePath = $HarnessPython
    CommandLine = ('"{{0}}" -m uvicorn app.main:app --app-dir "{{1}}" --host 0.0.0.0 --port 8014' -f $HarnessPython, $Root)
}}
$harnessWrongRoot = [PSCustomObject]@{{
    ExecutablePath = $HarnessPython
    CommandLine = ('"{{0}}" -m uvicorn app.main:app --app-dir "{{1}}" --host 127.0.0.1 --port 8014' -f $HarnessPython, $WrongRoot)
}}
$harnessBaseProcess = [PSCustomObject]@{{
    ExecutablePath = $HarnessBasePython
    CommandLine = ('"{{0}}" -m uvicorn app.main:app --app-dir "{{1}}" --host 127.0.0.1 --port 8014' -f $HarnessPython, $Root)
}}
$spoofedBaseProcess = [PSCustomObject]@{{
    ExecutablePath = $HarnessBasePython
    CommandLine = ('"{{0}}" -m uvicorn app.main:app --app-dir "{{1}}" --host 127.0.0.1 --port 8014' -f $HarnessBasePython, $Root)
}}
$harnessInlineSpoof = [PSCustomObject]@{{
    ExecutablePath = $HarnessPython
    CommandLine = ('"{{0}}" -c "marker = ''-m uvicorn app.main:app --app-dir {{1}} --host 127.0.0.1 --port 8014''"' -f $HarnessPython, $Root)
}}
$litellmInlineSpoof = [PSCustomObject]@{{
    ExecutablePath = $LiteLlmPython
    CommandLine = ('"{{0}}" -c "marker = ''{{1}} {{2}} {{3}} --config {{4}} --host 127.0.0.1 --port 4000''"' -f $LiteLlmPython, $LiteLlmRunner, $ProjectRootArgument, $Root, $LiteLlmConfig)
}}
[PSCustomObject]@{{
    Harness = Test-ProjectServiceProcess $harness Harness 8014
    HarnessBaseProcess = Test-ProjectServiceProcess $harnessBaseProcess Harness 8014
    LiteLLM = Test-ProjectServiceProcess $litellm LiteLLM 4000
    LiteLLMBaseProcess = Test-ProjectServiceProcess $litellmBaseProcess LiteLLM 4000
    LiteLLMWrongConfig = Test-ProjectServiceProcess $litellmWrongConfig LiteLLM 4000
    LiteLLMWrongRoot = Test-ProjectServiceProcess $litellmWrongRoot LiteLLM 4000
    LiteLLMPublicHost = Test-ProjectServiceProcess $litellmPublicHost LiteLLM 4000
    HarnessPublicHost = Test-ProjectServiceProcess $harnessPublicHost Harness 8014
    HarnessWrongRoot = Test-ProjectServiceProcess $harnessWrongRoot Harness 8014
    SpoofedBaseProcess = Test-ProjectServiceProcess $spoofedBaseProcess Harness 8014
    HarnessInlineSpoof = Test-ProjectServiceProcess $harnessInlineSpoof Harness 8014
    LiteLLMInlineSpoof = Test-ProjectServiceProcess $litellmInlineSpoof LiteLLM 4000
    WrongPort = Test-ProjectServiceProcess $harness Harness 4000
    WrongRunner = Test-ProjectServiceProcess $harness LiteLLM 8014
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not result.stderr.strip(), result.stderr
    assert json.loads(result.stdout.strip()) == {
        "Harness": True,
        "HarnessBaseProcess": True,
        "LiteLLM": True,
        "LiteLLMBaseProcess": True,
        "LiteLLMWrongConfig": False,
        "LiteLLMWrongRoot": False,
        "LiteLLMPublicHost": False,
        "HarnessPublicHost": False,
        "HarnessWrongRoot": False,
        "SpoofedBaseProcess": False,
        "HarnessInlineSpoof": False,
        "LiteLLMInlineSpoof": False,
        "WrongPort": False,
        "WrongRunner": False,
    }


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_start_and_launcher_require_fully_qualified_identity_paths() -> None:
    launcher_path = str(LAUNCHER).replace("'", "''")
    start_script = str(ROOT / "scripts" / "start-litellm-harness.ps1").replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
$driveRelativeRoot = "$($Root.Substring(0, 2))."
$rootRelativeRoot = $Root.Substring(2)
$launcherHarnessArgs = @($HarnessPython, '-m', 'uvicorn', 'app.main:app', '--app-dir', '.', '--host', '127.0.0.1', '--port', '8014')
$launcherLiteLlmArgs = @($LiteLlmPython, 'scripts\\run_litellm_proxy.py', $ProjectRootArgument, '.', '--config', 'config\\litellm.config.example.yaml', '--host', '127.0.0.1', '--port', '4000')
$launcherResults = [PSCustomObject]@{{
    Relative = Test-SameExecutablePath '.' $Root
    DriveRelative = Test-SameExecutablePath $driveRelativeRoot $Root
    RootRelative = Test-SameExecutablePath $rootRelativeRoot $Root
    Harness = Test-HarnessCommandArguments -Arguments $launcherHarnessArgs -Port 8014
    LiteLLM = Test-LiteLlmCommandArguments -Arguments $launcherLiteLlmArgs -Port 4000
}}

. '{start_script}' -FunctionsOnly
$startHarnessArgs = @($Python, '-m', 'uvicorn', 'app.main:app', '--app-dir', '.', '--host', '127.0.0.1', '--port', '8014')
$startLiteLlmArgs = @($LiteLlmPythonExe, 'scripts\\run_litellm_proxy.py', $ProjectRootArgument, '.', '--config', 'config\\litellm.config.example.yaml', '--host', '127.0.0.1', '--port', '4000')
$startResults = [PSCustomObject]@{{
    Relative = Test-SameExecutablePath '.' $Root
    DriveRelative = Test-SameExecutablePath $driveRelativeRoot $Root
    RootRelative = Test-SameExecutablePath $rootRelativeRoot $Root
    Harness = Test-HarnessCommandArguments -Arguments $startHarnessArgs -ExpectedPython $Python -Port 8014
    LiteLLM = Test-LiteLlmCommandArguments -Arguments $startLiteLlmArgs -Port 4000
}}
[PSCustomObject]@{{ Launcher = $launcherResults; Start = $startResults }} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not result.stderr.strip(), result.stderr
    expected = {
        "Relative": False,
        "DriveRelative": False,
        "RootRelative": False,
        "Harness": False,
        "LiteLLM": False,
    }
    assert json.loads(result.stdout.strip()) == {"Launcher": expected, "Start": expected}


def test_litellm_runner_consumes_only_matching_project_root_marker() -> None:
    runner_path = ROOT / "scripts" / "run_litellm_proxy.py"
    spec = importlib.util.spec_from_file_location("test_run_litellm_proxy", runner_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    forwarded = ["--config", "config.yaml", "--host", "127.0.0.1"]
    assert module._validated_litellm_args(
        [module.PROJECT_ROOT_ARGUMENT, str(ROOT), *forwarded]
    ) == forwarded
    with pytest.raises(SystemExit, match="identity is missing"):
        module._validated_litellm_args(forwarded)
    with pytest.raises(SystemExit, match="does not match"):
        module._validated_litellm_args(
            [module.PROJECT_ROOT_ARGUMENT, str(ROOT.parent), *forwarded]
        )


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_start_and_launcher_reject_live_project_python_inline_spoof() -> None:
    marker = "-m uvicorn app.main:app --host 127.0.0.1 --port 8014"
    process = subprocess.Popen(
        [sys.executable, "-c", f"import time; marker={marker!r}; time.sleep(30)"],
        cwd=ROOT,
    )
    try:
        start_script = str(ROOT / "scripts" / "start-litellm-harness.ps1").replace("'", "''")
        launcher_path = str(LAUNCHER).replace("'", "''")
        command = f"""
. '{start_script}' -FunctionsOnly
$startResult = Test-HarnessProcess -ProcessId {process.pid} -Port 8014
. '{launcher_path}' -FunctionsOnly
$processInfo = Get-CimInstance Win32_Process -Filter 'ProcessId = {process.pid}' -ErrorAction Stop
[PSCustomObject]@{{
    StartScript = $startResult
    Launcher = Test-ProjectServiceProcess $processInfo Harness 8014
}} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == {"StartScript": False, "Launcher": False}


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_start_and_launcher_do_not_claim_or_stop_shadow_harness(tmp_path: Path) -> None:
    shadow_root = tmp_path / "shadow project"
    shadow_package = shadow_root / "app"
    shadow_package.mkdir(parents=True)
    (shadow_package / "__init__.py").write_text("", encoding="utf-8")
    (shadow_package / "main.py").write_text(
        "from fastapi import FastAPI\n\napp = FastAPI()\n",
        encoding="utf-8",
    )
    port = _unused_loopback_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--app-dir",
            ".",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        executable=getattr(sys, "_base_executable", sys.executable),
        cwd=shadow_root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"Shadow Harness exited early with code {process.returncode}.")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("Shadow Harness did not begin listening.")

        start_script = str(ROOT / "scripts" / "start-litellm-harness.ps1").replace("'", "''")
        launcher_path = str(LAUNCHER).replace("'", "''")
        command = f"""
$listenerPid = [int](Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction Stop |
    Select-Object -First 1 -ExpandProperty OwningProcess)
. '{start_script}' -FunctionsOnly
$startResult = Test-HarnessProcess -ProcessId $listenerPid -Port {port}
. '{launcher_path}' -FunctionsOnly
$before = Get-PortServiceState {port} Harness
$stopResult = Stop-ProjectService {port} Harness
[PSCustomObject]@{{
    ListenerPid = $listenerPid
    StartScript = $startResult
    State = $before.State
    TargetPids = @($before.TargetPids)
    OtherPids = @($before.OtherPids)
    StoppedPids = @($stopResult.StoppedPids)
    Alive = [bool](Get-Process -Id $listenerPid -ErrorAction SilentlyContinue)
}} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout.strip())
        assert payload == {
            "ListenerPid": process.pid,
            "StartScript": False,
            "State": "PortOccupied",
            "TargetPids": [],
            "OtherPids": [process.pid],
            "StoppedPids": [],
            "Alive": True,
        }
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for launcher behavior testing")
def test_launcher_does_not_stop_unrelated_temporary_listener() -> None:
    listener_code = """
import socket
import time

listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(1)
print(listener.getsockname()[1], flush=True)
time.sleep(30)
"""
    listener = subprocess.Popen(
        [sys.executable, "-c", listener_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert listener.stdout is not None
        port_line = listener.stdout.readline().strip()
        assert port_line, listener.stderr.read() if listener.stderr is not None else "listener failed"
        port = int(port_line)
        launcher_path = str(LAUNCHER).replace("'", "''")
        command = f"""
. '{launcher_path}' -FunctionsOnly
$before = Get-PortServiceState {port} Harness
$stopResult = Stop-ProjectService {port} Harness
$alive = [bool](Get-Process -Id {listener.pid} -ErrorAction SilentlyContinue)
[PSCustomObject]@{{
    State = $before.State
    TargetPids = @($before.TargetPids)
    OtherPids = @($before.OtherPids)
    StoppedPids = @($stopResult.StoppedPids)
    Alive = $alive
}} | ConvertTo-Json -Compress
"""
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert not result.stderr.strip(), result.stderr
        payload = json.loads(result.stdout.strip())
        assert payload["State"] == "PortOccupied"
        assert payload["TargetPids"] == []
        assert payload["OtherPids"]
        assert payload["StoppedPids"] == []
        assert payload["Alive"] is True
        assert listener.poll() is None
    finally:
        listener.terminate()
        listener.wait(timeout=5)
