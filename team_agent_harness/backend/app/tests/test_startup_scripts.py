import json
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Iterator

import pytest


ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = ROOT.parents[1]
LAUNCHER = ROOT / "scripts" / "harness-launcher.ps1"
SETUP = ROOT / "scripts" / "setup-desktop.ps1"
SHORTCUT = ROOT / "scripts" / "create-desktop-shortcut.ps1"
ROOT_ENTRY = REPOSITORY_ROOT / "Start-Team-Agent-Harness.cmd"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def _service_handler(service: str) -> type[BaseHTTPRequestHandler]:
    class ServiceHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload: object
            if service == "litellm" and self.path == "/health/liveliness":
                payload = "I'm alive!"
            elif service == "harness" and self.path == "/health":
                payload = {"status": "ok"}
            elif service == "harness" and self.path == "/openapi.json":
                payload = {"info": {"title": "Team Agent Harness"}}
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
def _healthy_service(service: str) -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _service_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _start_script_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("LITELLM_API_KEY", "OPENAI_API_KEY", "OPENAI_API_BASE", "DEEPSEEK_API_KEY"):
        environment.pop(name, None)
    environment["TEAM_AGENT_ALLOW_BROWSER_ACCESS"] = "1"
    environment["TEAM_AGENT_BROWSER_PROVIDER"] = "chrome"
    return environment


def test_start_script_prefers_dedicated_litellm_environment() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")

    dedicated_assignment = '$DefaultLiteLlmPython = Join-Path $Root ".venv-litellm\\Scripts\\python.exe"'
    dedicated_selection = "elseif (Test-Path $DefaultLiteLlmPython)"

    assert dedicated_assignment in script
    assert dedicated_selection in script
    assert script.index(dedicated_selection) < script.index("$Python\n}")


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
    assert "Invoke-RestMethod" not in script


def test_start_script_decodes_litellm_json_string_liveliness_response() -> None:
    script = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")
    probe = script[
        script.index("function Test-LiteLlmEndpoint") : script.index("function Test-HarnessEndpoint")
    ]

    assert "ConvertFrom-Json" in probe
    assert '[string]$response -eq "I\'m alive!"' in probe


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


def test_start_and_setup_scripts_pin_the_same_litellm_release() -> None:
    setup = SETUP.read_text(encoding="utf-8")
    start = (ROOT / "scripts" / "start-litellm-harness.ps1").read_text(encoding="utf-8")

    requirement = '$LiteLlmRequirement = "litellm[proxy]==1.89.2"'
    assert requirement in setup
    assert requirement in start
    assert 'pip install $LiteLlmRequirement' in start


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
    python_path = str(Path(sys.executable)).replace("'", "''")
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
def test_start_script_reuses_all_healthy_services_before_loading_env_or_dependencies(tmp_path: Path) -> None:
    invalid_env = tmp_path / "must-not-be-loaded.env"
    invalid_env.write_text("this is not a valid env assignment", encoding="utf-8")
    missing_litellm_python = tmp_path / "missing-litellm-python.exe"

    with ExitStack() as stack:
        litellm_port = stack.enter_context(_healthy_service("litellm"))
        harness_port = stack.enter_context(_healthy_service("harness"))
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
                "-HarnessPort",
                str(harness_port),
                "-BrowserProxyPort",
                str(browser_port),
                "-LiteLlmPython",
                str(missing_litellm_python),
                "-EnvFile",
                str(invalid_env),
            ],
            cwd=ROOT,
            env=_start_script_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    assert result.returncode == 0, result.stderr
    assert "Reusing healthy local services." in result.stdout
    assert "Loaded local env file" not in result.stdout


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is required for startup behavior testing")
def test_start_script_reuse_preflight_rejects_wrong_service_identity(tmp_path: Path) -> None:
    invalid_env = tmp_path / "must-not-be-loaded.env"
    invalid_env.write_text("this is not a valid env assignment", encoding="utf-8")

    with ExitStack() as stack:
        litellm_port = stack.enter_context(_healthy_service("unrelated"))
        harness_port = stack.enter_context(_healthy_service("harness"))
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
                "-HarnessPort",
                str(harness_port),
                "-BrowserProxyPort",
                str(browser_port),
                "-LiteLlmPython",
                str(tmp_path / "missing-litellm-python.exe"),
                "-EnvFile",
                str(invalid_env),
            ],
            cwd=ROOT,
            env=_start_script_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )

    assert result.returncode != 0
    assert (
        f"Port {litellm_port} is occupied by PID " in result.stderr
        and "but it is not the expected LiteLLM service." in result.stderr
    )
    assert "Invalid env line" not in result.stderr


def test_launcher_stops_only_revalidated_project_service_processes() -> None:
    script = LAUNCHER.read_text(encoding="utf-8")

    assert "function Test-ProjectServiceProcess" in script
    assert "function Get-PortServiceState" in script
    assert "function Stop-ProjectService" in script
    assert "Test-ProjectServiceProcess $currentInfo $ServiceRole $Port" in script
    assert "Stop-Process -InputObject $process" in script
    assert "Stop-PortProcess" not in script
    assert "Stop-Process -Id $connection.OwningProcess" not in script


def test_launcher_status_distinguishes_project_service_from_other_port_owner() -> None:
    script = LAUNCHER.read_text(encoding="utf-8")

    assert '"TargetRunning"' in script
    assert '"PortOccupied"' in script
    assert '"PortConflict"' in script
    assert "Port-Status 4000 LiteLLM" in script
    assert "Port-Status 8014 Harness" in script


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
$harness = [PSCustomObject]@{{
    ExecutablePath = $HarnessPython
    CommandLine = ('"{{0}}" -m uvicorn app.main:app --host 127.0.0.1 --port 8014' -f $HarnessPython)
}}
$litellm = [PSCustomObject]@{{
    ExecutablePath = $LiteLlmPython
    CommandLine = ('"{{0}}" "{{1}}" --host 127.0.0.1 --port 4000' -f $LiteLlmPython, $LiteLlmRunner)
}}
$harnessBaseProcess = [PSCustomObject]@{{
    ExecutablePath = $HarnessBasePython
    CommandLine = ('"{{0}}" -m uvicorn app.main:app --host 127.0.0.1 --port 8014' -f $HarnessPython)
}}
$spoofedBaseProcess = [PSCustomObject]@{{
    ExecutablePath = $HarnessBasePython
    CommandLine = ('"{{0}}" -m uvicorn app.main:app --host 127.0.0.1 --port 8014' -f $HarnessBasePython)
}}
[PSCustomObject]@{{
    Harness = Test-ProjectServiceProcess $harness Harness 8014
    HarnessBaseProcess = Test-ProjectServiceProcess $harnessBaseProcess Harness 8014
    LiteLLM = Test-ProjectServiceProcess $litellm LiteLLM 4000
    SpoofedBaseProcess = Test-ProjectServiceProcess $spoofedBaseProcess Harness 8014
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
        "SpoofedBaseProcess": False,
        "WrongPort": False,
        "WrongRunner": False,
    }


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
