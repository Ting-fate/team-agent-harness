param(
    [int]$LiteLlmPort = 4000,
    [int]$HarnessPort = 8014,
    [int]$BrowserProxyPort = 3456,
    [int]$ChromeDebugPort = 9223,
    [int]$ModelTimeoutSeconds = 180,
    [int]$LiteLlmMaxRetries = 0,
    [string]$LiteLlmPython = "",
    [string]$EnvFile = ""
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Net.Http

$Root = Split-Path -Parent $PSScriptRoot
$DefaultEnvFile = Join-Path $Root ".env.local"
$LocalEnvFile = if ($EnvFile) { $EnvFile } else { $DefaultEnvFile }
$ConfigDir = Join-Path $Root "config"
$LiteLlmConfig = Join-Path $ConfigDir "litellm.config.example.yaml"
$LocalRoutingConfig = Join-Path $ConfigDir "model-routing.local.json"
$ExampleRoutingConfig = Join-Path $ConfigDir "model-routing.litellm.example.json"
$RoutingConfig = if (Test-Path $LocalRoutingConfig) { $LocalRoutingConfig } else { $ExampleRoutingConfig }
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$DefaultLiteLlmPython = Join-Path $Root ".venv-litellm\Scripts\python.exe"
$LiteLlmRequirement = "litellm[proxy]==1.89.2"
$LiteLlmPythonExe = if ($LiteLlmPython) {
    $LiteLlmPython
} elseif (Test-Path $DefaultLiteLlmPython) {
    $DefaultLiteLlmPython
} else {
    $Python
}
$LiteLlmRunner = Join-Path $PSScriptRoot "run_litellm_proxy.py"
$ChromeCdpProxy = Join-Path $PSScriptRoot "chrome_cdp_proxy.py"

function Import-LocalEnvFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }

        $match = [regex]::Match($trimmed, "^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
        if (-not $match.Success) {
            throw "Invalid env line in $Path. Use NAME=value format."
        }

        $name = $match.Groups[1].Value
        $value = $match.Groups[2].Value.Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }

    Write-Host "Loaded local env file: $Path"
}

function Get-ListenerProcessId {
    param([int]$Port)
    $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($connection) {
        return [int]$connection.OwningProcess
    }
    return $null
}

function Invoke-LocalHttpGet {
    param(
        [string]$Uri,
        [hashtable]$Headers = @{}
    )
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(2)
    $request = $null
    $response = $null
    try {
        $request = [System.Net.Http.HttpRequestMessage]::new(
            [System.Net.Http.HttpMethod]::Get,
            $Uri
        )
        foreach ($name in $Headers.Keys) {
            [void]$request.Headers.TryAddWithoutValidation([string]$name, [string]$Headers[$name])
        }
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            throw "Local endpoint returned HTTP $([int]$response.StatusCode)."
        }
        return $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
    } finally {
        if ($response) {
            $response.Dispose()
        }
        if ($request) {
            $request.Dispose()
        }
        $client.Dispose()
    }
}

function Test-LiteLlmEndpoint {
    param([int]$Port)
    try {
        $response = Invoke-LocalHttpGet -Uri "http://127.0.0.1:$Port/health/liveliness" |
            ConvertFrom-Json
        return [string]$response -eq "I'm alive!"
    } catch {
        return $false
    }
}

function Test-HarnessEndpoint {
    param([int]$Port)
    try {
        $health = Invoke-LocalHttpGet -Uri "http://127.0.0.1:$Port/health" | ConvertFrom-Json
        $openApi = Invoke-LocalHttpGet -Uri "http://127.0.0.1:$Port/openapi.json" | ConvertFrom-Json
        return $health.status -eq "ok" -and $openApi.info.title -eq "Team Agent Harness"
    } catch {
        return $false
    }
}

function Test-ChromeProxyEndpoint {
    param([int]$Port)
    try {
        $health = Invoke-LocalHttpGet `
            -Uri "http://127.0.0.1:$Port/health" `
            -Headers @{ "X-Team-Agent-Browser-Proxy" = "1" } |
            ConvertFrom-Json
        return $health.status -eq "ok" `
            -and $health.connected -eq $true `
            -and $health.proxy -eq "http://127.0.0.1:$Port" `
            -and @($health.capabilities) -contains "atomic_navigate_eval_v2" `
            -and @($health.capabilities) -contains "pinned_public_egress_v1" `
            -and @($health.capabilities) -contains "isolated_browser_context_v1"
    } catch {
        return $false
    }
}

$PreflightLiteLlmPid = Get-ListenerProcessId -Port $LiteLlmPort
$PreflightHarnessPid = Get-ListenerProcessId -Port $HarnessPort
$PreflightBrowserProxyPid = Get-ListenerProcessId -Port $BrowserProxyPort

if ($PreflightLiteLlmPid -and -not (Test-LiteLlmEndpoint -Port $LiteLlmPort)) {
    throw "Port $LiteLlmPort is occupied by PID $PreflightLiteLlmPid, but it is not the expected LiteLLM service."
}
if ($PreflightHarnessPid -and -not (Test-HarnessEndpoint -Port $HarnessPort)) {
    throw "Port $HarnessPort is occupied by PID $PreflightHarnessPid, but it is not the expected Team Agent Harness service."
}

$PreflightBrowserProxyHealthy = $false
if ($PreflightBrowserProxyPid) {
    $PreflightBrowserProxyHealthy = Test-ChromeProxyEndpoint -Port $BrowserProxyPort
    if (
        -not $PreflightBrowserProxyHealthy `
        -and $env:TEAM_AGENT_ALLOW_BROWSER_ACCESS -eq "1" `
        -and $env:TEAM_AGENT_BROWSER_PROVIDER -eq "chrome"
    ) {
        throw "Port $BrowserProxyPort is occupied by PID $PreflightBrowserProxyPid, but it is not the expected Chrome CDP proxy."
    }
}

if ($PreflightLiteLlmPid -and $PreflightHarnessPid -and $PreflightBrowserProxyHealthy) {
    Write-Host "Reusing healthy local services."
    Write-Host "LiteLLM Proxy: http://127.0.0.1:$LiteLlmPort"
    Write-Host "Harness UI:     http://127.0.0.1:$HarnessPort/"
    Write-Host "Chrome bridge:  http://127.0.0.1:$BrowserProxyPort"
    return
}

Import-LocalEnvFile $LocalEnvFile

if (-not (Test-Path $Python)) {
    throw "Python venv not found: $Python"
}

if (-not (Test-Path $LiteLlmPythonExe)) {
    throw "LiteLLM Python not found: $LiteLlmPythonExe"
}

if (-not $env:LITELLM_API_KEY) {
    throw "Set LITELLM_API_KEY first, or add it to .env.local. Example: LITELLM_API_KEY=sk-dev-local-key"
}

if (-not $env:LITELLM_API_KEY.StartsWith("sk-")) {
    throw "LITELLM_API_KEY must start with sk-. Example: LITELLM_API_KEY=sk-dev-local-key"
}

if (-not $env:OPENAI_API_KEY) {
    throw "Set OPENAI_API_KEY first, or add it to .env.local."
}

if (-not $env:OPENAI_API_BASE) {
    throw "Set OPENAI_API_BASE first, or add it to .env.local. Example: OPENAI_API_BASE=https://your-relay.example.com/v1"
}

if (-not $env:DEEPSEEK_API_KEY) {
    throw "Set DEEPSEEK_API_KEY first, or add it to .env.local."
}

$env:TEAM_AGENT_ALLOW_REAL_MODEL_CALLS = "1"
$env:TEAM_AGENT_MODEL_ROUTING_CONFIG = $RoutingConfig
$env:LITELLM_BASE_URL = "http://127.0.0.1:$LiteLlmPort/v1"
if (-not $env:TEAM_AGENT_MODEL_TIMEOUT_SECONDS) {
    $env:TEAM_AGENT_MODEL_TIMEOUT_SECONDS = "$ModelTimeoutSeconds"
}
if (-not $env:TEAM_AGENT_LITELLM_PROXY_MAX_ATTEMPTS) {
    $env:TEAM_AGENT_LITELLM_PROXY_MAX_ATTEMPTS = "1"
}
if (-not $env:REQUEST_TIMEOUT) {
    $env:REQUEST_TIMEOUT = "$ModelTimeoutSeconds"
}
if (-not $env:DEFAULT_MAX_RETRIES) {
    $env:DEFAULT_MAX_RETRIES = "$LiteLlmMaxRetries"
}

$LiteLlmVersion = & $LiteLlmPythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ([version]$LiteLlmVersion -ge [version]"3.14") {
    throw "LiteLLM dependencies do not currently install cleanly on Python $LiteLlmVersion in this environment. Install Python 3.12 or 3.13, create a LiteLLM venv, then run this script with -LiteLlmPython C:\path\to\litellm-venv\Scripts\python.exe"
}

& $LiteLlmPythonExe -m pip show litellm *> $null
if ($LASTEXITCODE -ne 0) {
    & $LiteLlmPythonExe -m pip install $LiteLlmRequirement
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install $LiteLlmRequirement. Check the pip error above."
    }
}

if (-not (Test-Path $LiteLlmRunner)) {
    throw "LiteLLM runner not found: $LiteLlmRunner"
}

$OutputDir = Join-Path $Root "output"
New-Item -ItemType Directory -Force $OutputDir | Out-Null

$LiteLlmLog = Join-Path $OutputDir "litellm-proxy.log"
$LiteLlmErr = Join-Path $OutputDir "litellm-proxy.err.log"
$HarnessLog = Join-Path $OutputDir "harness-litellm.log"
$HarnessErr = Join-Path $OutputDir "harness-litellm.err.log"
$ChromeProxyLog = Join-Path $OutputDir "chrome-cdp-proxy.log"
$ChromeProxyErr = Join-Path $OutputDir "chrome-cdp-proxy.err.log"

function Wait-ForExpectedService {
    param(
        [string]$Name,
        [scriptblock]$Probe,
        [System.Diagnostics.Process]$Process,
        [int]$TimeoutSeconds = 30
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process -and $Process.HasExited) {
            throw "$Name exited before becoming healthy (exit code $($Process.ExitCode))."
        }
        if (& $Probe) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "$Name did not become healthy within $TimeoutSeconds seconds."
}

$LiteLlmPid = Get-ListenerProcessId -Port $LiteLlmPort
if ($LiteLlmPid) {
    if (-not (Test-LiteLlmEndpoint -Port $LiteLlmPort)) {
        throw "Port $LiteLlmPort is occupied by PID $LiteLlmPid, but it is not the expected LiteLLM service."
    }
} else {
    $LiteLlmProcess = Start-Process -FilePath $LiteLlmPythonExe `
        -ArgumentList @($LiteLlmRunner, "--config", $LiteLlmConfig, "--host", "127.0.0.1", "--port", "$LiteLlmPort", "--request_timeout", "$env:REQUEST_TIMEOUT") `
        -WorkingDirectory $Root `
        -RedirectStandardOutput $LiteLlmLog `
        -RedirectStandardError $LiteLlmErr `
        -WindowStyle Hidden `
        -PassThru
    try {
        Wait-ForExpectedService -Name "LiteLLM" -Process $LiteLlmProcess -Probe { Test-LiteLlmEndpoint -Port $LiteLlmPort }
    } catch {
        if (-not $LiteLlmProcess.HasExited) {
            Stop-Process -Id $LiteLlmProcess.Id -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}

if ($env:TEAM_AGENT_ALLOW_BROWSER_ACCESS -eq "1" -and $env:TEAM_AGENT_BROWSER_PROVIDER -eq "chrome") {
    if (-not (Test-Path $ChromeCdpProxy)) {
        throw "Chrome CDP proxy not found: $ChromeCdpProxy"
    }
    $env:TEAM_AGENT_BROWSER_CDP_URL = "http://127.0.0.1:$BrowserProxyPort"
    $BrowserProxyPid = Get-ListenerProcessId -Port $BrowserProxyPort
    if ($BrowserProxyPid) {
        if (-not (Test-ChromeProxyEndpoint -Port $BrowserProxyPort)) {
            throw "Port $BrowserProxyPort is occupied by PID $BrowserProxyPid, but it is not the expected Chrome CDP proxy."
        }
    } else {
        $BrowserProxyProcess = Start-Process -FilePath $Python `
            -ArgumentList @($ChromeCdpProxy, "--host", "127.0.0.1", "--port", "$BrowserProxyPort", "--chrome-debug-port", "$ChromeDebugPort") `
            -WorkingDirectory $Root `
            -RedirectStandardOutput $ChromeProxyLog `
            -RedirectStandardError $ChromeProxyErr `
            -WindowStyle Hidden `
            -PassThru
        try {
            Wait-ForExpectedService -Name "Chrome CDP proxy" -Process $BrowserProxyProcess -Probe { Test-ChromeProxyEndpoint -Port $BrowserProxyPort }
        } catch {
            if (-not $BrowserProxyProcess.HasExited) {
                Stop-Process -Id $BrowserProxyProcess.Id -Force -ErrorAction SilentlyContinue
            }
            throw
        }
    }
}

$HarnessPid = Get-ListenerProcessId -Port $HarnessPort
if ($HarnessPid) {
    if (-not (Test-HarnessEndpoint -Port $HarnessPort)) {
        throw "Port $HarnessPort is occupied by PID $HarnessPid, but it is not the expected Team Agent Harness service."
    }
} else {
    $HarnessProcess = Start-Process -FilePath $Python `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$HarnessPort") `
        -WorkingDirectory $Root `
        -RedirectStandardOutput $HarnessLog `
        -RedirectStandardError $HarnessErr `
        -WindowStyle Hidden `
        -PassThru
    try {
        Wait-ForExpectedService -Name "Team Agent Harness" -Process $HarnessProcess -Probe { Test-HarnessEndpoint -Port $HarnessPort }
    } catch {
        if (-not $HarnessProcess.HasExited) {
            Stop-Process -Id $HarnessProcess.Id -Force -ErrorAction SilentlyContinue
        }
        throw
    }
}

Write-Host "LiteLLM Proxy: http://127.0.0.1:$LiteLlmPort"
Write-Host "Harness UI:     http://127.0.0.1:$HarnessPort/"
Write-Host "Routing config: $RoutingConfig"
Write-Host "Model timeout:  $env:TEAM_AGENT_MODEL_TIMEOUT_SECONDS seconds"
Write-Host "LiteLLM retries: $env:DEFAULT_MAX_RETRIES"
if ($env:TEAM_AGENT_ALLOW_BROWSER_ACCESS -eq "1" -and $env:TEAM_AGENT_BROWSER_PROVIDER -eq "chrome") {
    Write-Host "Chrome bridge:  http://127.0.0.1:$BrowserProxyPort"
    Write-Host "Chrome debug:   http://127.0.0.1:$ChromeDebugPort"
}
