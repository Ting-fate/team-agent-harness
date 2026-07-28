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
LAUNCHER = ROOT / "scripts" / "harness-launcher.ps1"
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
def test_launcher_recognizes_only_expected_project_service_commands() -> None:
    launcher_path = str(LAUNCHER).replace("'", "''")
    command = f"""
. '{launcher_path}' -FunctionsOnly
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
