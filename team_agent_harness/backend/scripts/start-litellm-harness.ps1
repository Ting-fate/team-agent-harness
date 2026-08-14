param(
    [int]$LiteLlmPort = 4000,
    [int]$HarnessPort = 8014,
    [int]$BrowserProxyPort = 3456,
    [int]$ChromeDebugPort = 9223,
    [int]$ModelTimeoutSeconds = 180,
    [int]$LiteLlmMaxRetries = 0,
    [string]$HarnessPython = "",
    [string]$LiteLlmPython = "",
    [string]$EnvFile = "",
    [switch]$FunctionsOnly
)

$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Net.Http
if (-not ("TeamAgentHarness.ProcessCommandLine" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace TeamAgentHarness
{
    public static class ProcessCommandLine
    {
        [DllImport("shell32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern IntPtr CommandLineToArgvW(string commandLine, out int argumentCount);

        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr memory);

        public static string[] Split(string commandLine)
        {
            if (String.IsNullOrWhiteSpace(commandLine))
            {
                return new string[0];
            }

            int argumentCount;
            IntPtr arguments = CommandLineToArgvW(commandLine, out argumentCount);
            if (arguments == IntPtr.Zero)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }

            try
            {
                var result = new List<string>(argumentCount);
                for (int index = 0; index < argumentCount; index++)
                {
                    IntPtr argument = Marshal.ReadIntPtr(arguments, index * IntPtr.Size);
                    result.Add(Marshal.PtrToStringUni(argument) ?? String.Empty);
                }
                return result.ToArray();
            }
            finally
            {
                LocalFree(arguments);
            }
        }
    }
}
'@
}

$Root = Split-Path -Parent $PSScriptRoot
$ProjectRootArgument = "--team-agent-project-root"
$DefaultEnvFile = Join-Path $Root ".env.local"
$LocalEnvFile = if ($EnvFile) { $EnvFile } else { $DefaultEnvFile }
$ConfigDir = Join-Path $Root "config"
$LiteLlmConfig = Join-Path $ConfigDir "litellm.config.example.yaml"
$LocalRoutingConfig = Join-Path $ConfigDir "model-routing.local.json"
$ExampleRoutingConfig = Join-Path $ConfigDir "model-routing.litellm.example.json"
$RoutingConfig = if (Test-Path $LocalRoutingConfig) { $LocalRoutingConfig } else { $ExampleRoutingConfig }
$DefaultHarnessPython = Join-Path $Root ".venv\Scripts\python.exe"
$Python = if ($HarnessPython) { $HarnessPython } else { $DefaultHarnessPython }
$DefaultLiteLlmPython = Join-Path $Root ".venv-litellm\Scripts\python.exe"
$LiteLlmPythonExe = if ($LiteLlmPython) {
    $LiteLlmPython
} elseif (Test-Path $DefaultLiteLlmPython) {
    $DefaultLiteLlmPython
} else {
    $DefaultHarnessPython
}
$LiteLlmRunner = Join-Path $PSScriptRoot "run_litellm_proxy.py"
$ChromeCdpProxy = Join-Path $PSScriptRoot "chrome_cdp_proxy.py"
$HarnessUiMarker = 'id="mainWorkspace"'
$HarnessRequiredOpenApiPaths = @(
    "/team-selections/validate",
    "/workflow-packs/{pack_name}/team-template",
    "/runs/{run_id}/team"
)
$SupportWarnings = @()

function Add-SupportWarning {
    param([string]$Message)
    $script:SupportWarnings += $Message
    [Console]::Error.WriteLine("WARNING: $Message")
}

function Import-LocalEnvFile {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return
    }

    $values = @{}
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

        $values[$name] = $value
    }

    foreach ($name in $values.Keys) {
        [Environment]::SetEnvironmentVariable($name, $values[$name], "Process")
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

function Get-PythonBaseExecutable {
    param([string]$PythonPath)
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        return ""
    }
    try {
        $venvRoot = Split-Path -Parent (Split-Path -Parent $PythonPath)
        $venvConfig = Join-Path $venvRoot "pyvenv.cfg"
        foreach ($line in Get-Content -LiteralPath $venvConfig -Encoding UTF8 -ErrorAction Stop) {
            $match = [regex]::Match($line, "^\s*executable\s*=\s*(.+?)\s*$")
            if ($match.Success) {
                return $match.Groups[1].Value
            }
        }
    } catch {
        return ""
    }
    return ""
}

function Get-NormalizedFullyQualifiedPath {
    param([string]$Path)
    if (-not $Path) {
        return ""
    }
    try {
        $pathRoot = [System.IO.Path]::GetPathRoot($Path)
        if (
            -not $pathRoot `
            -or $pathRoot -match '^[A-Za-z]:$' `
            -or $pathRoot -in @("\", "/")
        ) {
            return ""
        }
        return [System.IO.Path]::GetFullPath($Path)
    } catch {
        return ""
    }
}

function Test-SameExecutablePath {
    param(
        [string]$ActualPath,
        [string]$ExpectedPath
    )
    if (-not $ActualPath -or -not $ExpectedPath) {
        return $false
    }
    $actual = Get-NormalizedFullyQualifiedPath $ActualPath
    $expected = Get-NormalizedFullyQualifiedPath $ExpectedPath
    if (-not $actual -or -not $expected) {
        return $false
    }
    return [string]::Equals($actual, $expected, [System.StringComparison]::OrdinalIgnoreCase)
}

function Get-ProcessCommandArguments {
    param([string]$CommandLine)
    try {
        return [TeamAgentHarness.ProcessCommandLine]::Split($CommandLine)
    } catch {
        return @()
    }
}

function Test-HarnessCommandArguments {
    param(
        [string[]]$Arguments,
        [string]$ExpectedPython,
        [int]$Port
    )
    return $Arguments.Count -eq 10 `
        -and (Test-SameExecutablePath $Arguments[0] $ExpectedPython) `
        -and $Arguments[1] -ceq "-m" `
        -and $Arguments[2] -ceq "uvicorn" `
        -and $Arguments[3] -ceq "app.main:app" `
        -and $Arguments[4] -ceq "--app-dir" `
        -and (Test-SameExecutablePath $Arguments[5] $Root) `
        -and $Arguments[6] -ceq "--host" `
        -and $Arguments[7] -ceq "127.0.0.1" `
        -and $Arguments[8] -ceq "--port" `
        -and $Arguments[9] -ceq "$Port"
}

function Test-LiteLlmCommandArguments {
    param(
        [string[]]$Arguments,
        [int]$Port
    )
    if ($Arguments.Count -notin @(10, 12)) {
        return $false
    }
    if (
        -not (Test-SameExecutablePath $Arguments[0] $LiteLlmPythonExe) `
        -or -not (Test-SameExecutablePath $Arguments[1] $LiteLlmRunner) `
        -or $Arguments[2] -cne $ProjectRootArgument `
        -or -not (Test-SameExecutablePath $Arguments[3] $Root) `
        -or $Arguments[4] -cne "--config" `
        -or -not (Test-SameExecutablePath $Arguments[5] $LiteLlmConfig) `
        -or $Arguments[6] -cne "--host" `
        -or $Arguments[7] -cne "127.0.0.1" `
        -or $Arguments[8] -cne "--port" `
        -or $Arguments[9] -cne "$Port"
    ) {
        return $false
    }
    if ($Arguments.Count -eq 10) {
        return $true
    }
    $timeout = 0.0
    return $Arguments[10] -ceq "--request_timeout" `
        -and [double]::TryParse(
            $Arguments[11],
            [System.Globalization.NumberStyles]::Float,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [ref]$timeout
        ) `
        -and $timeout -gt 0
}

function Test-ChromeProxyCommandArguments {
    param(
        [string[]]$Arguments,
        [int]$Port
    )
    return $Arguments.Count -eq 8 `
        -and (Test-SameExecutablePath $Arguments[0] $DefaultHarnessPython) `
        -and (Test-SameExecutablePath $Arguments[1] $ChromeCdpProxy) `
        -and $Arguments[2] -ceq "--host" `
        -and $Arguments[3] -ceq "127.0.0.1" `
        -and $Arguments[4] -ceq "--port" `
        -and $Arguments[5] -ceq "$Port" `
        -and $Arguments[6] -ceq "--chrome-debug-port" `
        -and $Arguments[7] -ceq "$ChromeDebugPort"
}

$HarnessBasePython = Get-PythonBaseExecutable $Python
$LiteLlmBasePython = Get-PythonBaseExecutable $LiteLlmPythonExe
$ChromeProxyBasePython = Get-PythonBaseExecutable $DefaultHarnessPython

function Get-ProcessCreationTicks {
    param([object]$ProcessInfo)
    if (-not $ProcessInfo -or -not $ProcessInfo.CreationDate) {
        return $null
    }
    try {
        return ([DateTime]$ProcessInfo.CreationDate).ToUniversalTime().Ticks
    } catch {
        return $null
    }
}

function Get-ProcessInstanceIdentity {
    param([System.Diagnostics.Process]$Process)
    if (-not $Process) {
        return $null
    }
    try {
        $Process.Refresh()
        if ($Process.HasExited) {
            return $null
        }
        $processId = [int]$Process.Id
        $processStartTicks = [long]$Process.StartTime.ToUniversalTime().Ticks
    } catch {
        return $null
    }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    $creationTicks = Get-ProcessCreationTicks $processInfo
    if (
        -not $processInfo `
        -or $null -eq $creationTicks `
        -or [Math]::Abs($processStartTicks - [long]$creationTicks) -gt [TimeSpan]::TicksPerMillisecond
    ) {
        return $null
    }
    return [PSCustomObject]@{
        ProcessId = [int]$processInfo.ProcessId
        CreationTicks = [long]$creationTicks
    }
}

function Get-ProcessInfoIdentity {
    param([object]$ProcessInfo)
    $creationTicks = Get-ProcessCreationTicks $ProcessInfo
    if (-not $ProcessInfo -or $null -eq $creationTicks) {
        return $null
    }
    return [PSCustomObject]@{
        ProcessId = [int]$ProcessInfo.ProcessId
        CreationTicks = [long]$creationTicks
    }
}

function Test-SameProcessInstance {
    param(
        [object]$ProcessInfo,
        [object]$ExpectedIdentity
    )
    if (-not $ProcessInfo -or -not $ExpectedIdentity) {
        return $false
    }
    $creationTicks = Get-ProcessCreationTicks $ProcessInfo
    return $null -ne $creationTicks `
        -and [int]$ProcessInfo.ProcessId -eq [int]$ExpectedIdentity.ProcessId `
        -and [long]$creationTicks -eq [long]$ExpectedIdentity.CreationTicks
}

function Test-ProcessObjectMatchesIdentity {
    param(
        [System.Diagnostics.Process]$Process,
        [object]$ExpectedIdentity
    )
    if (-not $Process -or -not $ExpectedIdentity) {
        return $false
    }
    try {
        $Process.Refresh()
        return -not $Process.HasExited `
            -and [int]$Process.Id -eq [int]$ExpectedIdentity.ProcessId `
            -and [Math]::Abs(
                [long]$Process.StartTime.ToUniversalTime().Ticks - [long]$ExpectedIdentity.CreationTicks
            ) -le [TimeSpan]::TicksPerMillisecond
    } catch {
        return $false
    }
}

function Get-ControlledProcessChain {
    param(
        [object]$ProcessInfo,
        [object]$ExpectedIdentity
    )
    if (-not $ProcessInfo -or -not $ExpectedIdentity) {
        return $null
    }
    $visited = [System.Collections.Generic.HashSet[int]]::new()
    $chain = [System.Collections.Generic.List[object]]::new()
    $current = $ProcessInfo
    for ($depth = 0; $depth -lt 16; $depth++) {
        $currentId = [int]$current.ProcessId
        if (-not $visited.Add($currentId)) {
            return $null
        }
        $currentIdentity = Get-ProcessInfoIdentity $current
        if (-not $currentIdentity) {
            return $null
        }
        $chain.Add($currentIdentity)
        if ($currentId -eq [int]$ExpectedIdentity.ProcessId) {
            if (Test-SameProcessInstance $current $ExpectedIdentity) {
                return $chain.ToArray()
            }
            return $null
        }
        $parentId = [int]$current.ParentProcessId
        if ($parentId -le 0) {
            return $null
        }
        $current = Get-CimInstance Win32_Process -Filter "ProcessId = $parentId" -ErrorAction SilentlyContinue
        if (-not $current) {
            return $null
        }
    }
    return $null
}

function Test-ProcessInstanceOrDescendant {
    param(
        [object]$ProcessInfo,
        [object]$ExpectedIdentity
    )
    return @(Get-ControlledProcessChain $ProcessInfo $ExpectedIdentity).Count -gt 0
}

function Get-UniqueListenerProcessInfo {
    param([int]$Port)
    $listenerPids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { [int]$_.OwningProcess } |
        Where-Object { $_ -gt 0 } |
        Sort-Object -Unique)
    if ($listenerPids.Count -ne 1) {
        return $null
    }
    return Get-CimInstance Win32_Process -Filter "ProcessId = $($listenerPids[0])" -ErrorAction SilentlyContinue
}

function Test-LiteLlmProcessInfo {
    param(
        [object]$ProcessInfo,
        [int]$Port
    )
    try {
        if (-not $processInfo -or -not $processInfo.CommandLine) {
            return $false
        }
        $expectedExecutables = @($LiteLlmPythonExe, $LiteLlmBasePython)
        $executableMatches = @($expectedExecutables | Where-Object {
            $_ -and (Test-SameExecutablePath $processInfo.ExecutablePath $_)
        }).Count -gt 0
        if (-not $executableMatches) {
            return $false
        }

        $arguments = @(Get-ProcessCommandArguments ([string]$processInfo.CommandLine))
        return Test-LiteLlmCommandArguments -Arguments $arguments -Port $Port
    } catch {
        return $false
    }
}

function Test-LiteLlmProcess {
    param(
        [int]$ProcessId,
        [int]$Port
    )
    try {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        return Test-LiteLlmProcessInfo -ProcessInfo $processInfo -Port $Port
    } catch {
        return $false
    }
}

function Test-HarnessProcessInfo {
    param(
        [object]$ProcessInfo,
        [int]$Port
    )
    try {
        if (-not $processInfo -or -not $processInfo.CommandLine) {
            return $false
        }
        $expectedExecutables = @($Python, $HarnessBasePython)
        $executableMatches = @($expectedExecutables | Where-Object {
            $_ -and (Test-SameExecutablePath $processInfo.ExecutablePath $_)
        }).Count -gt 0
        if (-not $executableMatches) {
            return $false
        }

        $arguments = @(Get-ProcessCommandArguments ([string]$processInfo.CommandLine))
        return Test-HarnessCommandArguments -Arguments $arguments -ExpectedPython $Python -Port $Port
    } catch {
        return $false
    }
}

function Test-HarnessProcess {
    param(
        [int]$ProcessId,
        [int]$Port
    )
    try {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        return Test-HarnessProcessInfo -ProcessInfo $processInfo -Port $Port
    } catch {
        return $false
    }
}

function Test-ChromeProxyProcessInfo {
    param(
        [object]$ProcessInfo,
        [int]$Port
    )
    try {
        if (-not $processInfo -or -not $processInfo.CommandLine) {
            return $false
        }
        $expectedExecutables = @($DefaultHarnessPython, $ChromeProxyBasePython)
        $executableMatches = @($expectedExecutables | Where-Object {
            $_ -and (Test-SameExecutablePath $processInfo.ExecutablePath $_)
        }).Count -gt 0
        if (-not $executableMatches) {
            return $false
        }

        $arguments = @(Get-ProcessCommandArguments ([string]$processInfo.CommandLine))
        return Test-ChromeProxyCommandArguments -Arguments $arguments -Port $Port
    } catch {
        return $false
    }
}

function Test-ChromeProxyProcess {
    param(
        [int]$ProcessId,
        [int]$Port
    )
    try {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        return Test-ChromeProxyProcessInfo -ProcessInfo $processInfo -Port $Port
    } catch {
        return $false
    }
}

function Get-SpawnedServiceInstance {
    param(
        [ValidateSet("LiteLLM", "Harness", "ChromeProxy")]
        [string]$ServiceRole,
        [int]$Port,
        [object]$SpawnedIdentity
    )
    $firstInfo = Get-UniqueListenerProcessInfo -Port $Port
    $controlledChain = @(Get-ControlledProcessChain $firstInfo $SpawnedIdentity)
    if (
        -not $firstInfo `
        -or -not $controlledChain.Count `
        -or @($controlledChain | Where-Object { -not $_ }).Count
    ) {
        return $null
    }
    $commandMatches = switch ($ServiceRole) {
        "LiteLLM" { Test-LiteLlmProcessInfo -ProcessInfo $firstInfo -Port $Port }
        "Harness" { Test-HarnessProcessInfo -ProcessInfo $firstInfo -Port $Port }
        "ChromeProxy" { Test-ChromeProxyProcessInfo -ProcessInfo $firstInfo -Port $Port }
    }
    if (-not $commandMatches) {
        return $null
    }

    $firstIdentity = Get-ProcessInfoIdentity $firstInfo
    $currentInfo = Get-UniqueListenerProcessInfo -Port $Port
    $currentChain = @(Get-ControlledProcessChain $currentInfo $SpawnedIdentity)
    if (
        -not (Test-SameProcessInstance $currentInfo $firstIdentity) `
        -or -not $currentChain.Count `
        -or @($currentChain | Where-Object { -not $_ }).Count
    ) {
        return $null
    }
    return [PSCustomObject]@{
        ListenerIdentity = $firstIdentity
        ControlledChain = @($currentChain)
    }
}

function Test-SpawnedServiceOwnership {
    param(
        [ValidateSet("LiteLLM", "Harness", "ChromeProxy")]
        [string]$ServiceRole,
        [int]$Port,
        [object]$SpawnedIdentity
    )
    return $null -ne (Get-SpawnedServiceInstance `
        -ServiceRole $ServiceRole `
        -Port $Port `
        -SpawnedIdentity $SpawnedIdentity)
}

function Get-VerifiedServiceProcess {
    param(
        [ValidateSet("LiteLLM", "Harness", "ChromeProxy")]
        [string]$ServiceRole,
        [int]$Port,
        [object]$ExpectedIdentity
    )
    $currentInfo = Get-UniqueListenerProcessInfo -Port $Port
    if (-not (Test-SameProcessInstance $currentInfo $ExpectedIdentity)) {
        return $null
    }
    $commandMatches = switch ($ServiceRole) {
        "LiteLLM" { Test-LiteLlmProcessInfo -ProcessInfo $currentInfo -Port $Port }
        "Harness" { Test-HarnessProcessInfo -ProcessInfo $currentInfo -Port $Port }
        "ChromeProxy" { Test-ChromeProxyProcessInfo -ProcessInfo $currentInfo -Port $Port }
    }
    if (-not $commandMatches) {
        return $null
    }
    $confirmedInfo = Get-UniqueListenerProcessInfo -Port $Port
    if (-not (Test-SameProcessInstance $confirmedInfo $ExpectedIdentity)) {
        return $null
    }
    return $confirmedInfo
}

function Stop-VerifiedProcessIdentity {
    param([object]$ExpectedIdentity)
    if (-not $ExpectedIdentity) {
        return
    }
    $currentInfo = Get-CimInstance Win32_Process `
        -Filter "ProcessId = $($ExpectedIdentity.ProcessId)" `
        -ErrorAction SilentlyContinue
    if (-not (Test-SameProcessInstance $currentInfo $ExpectedIdentity)) {
        return
    }
    $currentProcess = Get-Process -Id $ExpectedIdentity.ProcessId -ErrorAction SilentlyContinue
    if (Test-ProcessObjectMatchesIdentity $currentProcess $ExpectedIdentity) {
        Stop-Process -InputObject $currentProcess -Force -ErrorAction SilentlyContinue
    }
}

function Stop-SpawnedProcessInstance {
    param(
        [System.Diagnostics.Process]$Process,
        [object]$SpawnedIdentity,
        [object]$ServiceInstance,
        [ValidateSet("LiteLLM", "Harness", "ChromeProxy")]
        [string]$ServiceRole,
        [int]$Port
    )
    if ($ServiceInstance -and $ServiceInstance.ListenerIdentity) {
        $controlledIdentities = @($ServiceInstance.ControlledChain)
        $listenerWasControlled = @($controlledIdentities | Where-Object {
            [int]$_.ProcessId -eq [int]$ServiceInstance.ListenerIdentity.ProcessId `
                -and [long]$_.CreationTicks -eq [long]$ServiceInstance.ListenerIdentity.CreationTicks
        }).Count -eq 1
        if ($listenerWasControlled) {
            $verifiedListener = Get-VerifiedServiceProcess `
                -ServiceRole $ServiceRole `
                -Port $Port `
                -ExpectedIdentity $ServiceInstance.ListenerIdentity
            if ($verifiedListener) {
                Stop-VerifiedProcessIdentity $ServiceInstance.ListenerIdentity
            }
        }
        foreach ($identity in $controlledIdentities) {
            Stop-VerifiedProcessIdentity $identity
        }
    }
    if (-not $Process -or -not $SpawnedIdentity) {
        return
    }
    try {
        $currentInfo = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $($SpawnedIdentity.ProcessId)" `
            -ErrorAction SilentlyContinue
        if (
            (Test-SameProcessInstance $currentInfo $SpawnedIdentity) `
            -and (Test-ProcessObjectMatchesIdentity $Process $SpawnedIdentity)
        ) {
            Stop-Process -InputObject $Process -Force -ErrorAction SilentlyContinue
        }
    } catch {
    }
}

function Invoke-StartupRollback {
    param([object[]]$StartedServices)

    for ($index = $StartedServices.Count - 1; $index -ge 0; $index--) {
        $service = $StartedServices[$index]
        if (-not $service.Process) {
            continue
        }
        Stop-SpawnedProcessInstance `
            -Process $service.Process `
            -SpawnedIdentity $service.SpawnedIdentity `
            -ServiceInstance $service.ServiceInstance `
            -ServiceRole $service.ServiceRole `
            -Port $service.Port
    }

    foreach ($service in $StartedServices) {
        if ($service.RestoreAction) {
            [void](& $service.RestoreAction)
        }
    }
}

function Invoke-LocalHttpGet {
    param(
        [string]$Uri,
        [hashtable]$Headers = @{},
        [switch]$IncludeMetadata
    )
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.MaxResponseContentBufferSize = 1048576
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
        $content = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        if ($IncludeMetadata) {
            return [PSCustomObject]@{
                Content = $content
                ContentType = [string]$response.Content.Headers.ContentType.MediaType
            }
        }
        return $content
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
        $page = Invoke-LocalHttpGet -Uri "http://127.0.0.1:$Port/" -IncludeMetadata
        $openApi = Invoke-LocalHttpGet -Uri "http://127.0.0.1:$Port/openapi.json" | ConvertFrom-Json
        $openApiPaths = @($openApi.paths.PSObject.Properties.Name)
        $missingPaths = @($HarnessRequiredOpenApiPaths | Where-Object { $openApiPaths -notcontains $_ })
        return $health.status -eq "ok" `
            -and $health.worker -eq "running" `
            -and $page.ContentType -eq "text/html" `
            -and $page.Content.Contains($HarnessUiMarker) `
            -and $openApi.info.title -eq "Team Agent Harness" `
            -and $openApi.info.version -eq "0.1.0" `
            -and $missingPaths.Count -eq 0
    } catch {
        return $false
    }
}

function Get-RevalidatedHarnessListener {
    param(
        [int]$Port,
        [object]$InitialIdentity
    )
    $processId = Get-ListenerProcessId -Port $Port
    if (-not $processId) {
        if ($InitialIdentity) {
            throw "The Team Agent Harness process on port $Port changed during startup; the verified instance disappeared."
        }
        return $null
    }
    $processInfo = Get-UniqueListenerProcessInfo -Port $Port
    $identity = Get-ProcessInfoIdentity $processInfo
    if (
        -not $identity `
        -or [int]$identity.ProcessId -ne [int]$processId `
        -or -not (Test-HarnessProcessInfo -ProcessInfo $processInfo -Port $Port)
    ) {
        throw "Port $Port is occupied by PID $processId, but it is not the expected Team Agent Harness service."
    }
    if ($InitialIdentity -and -not (Test-SameProcessInstance $processInfo $InitialIdentity)) {
        throw "The Team Agent Harness process on port $Port changed during startup; refusing to probe a replacement instance."
    }
    if (-not (Test-HarnessEndpoint -Port $Port)) {
        throw "The expected Team Agent Harness process on port $Port did not pass its readiness checks."
    }
    return [PSCustomObject]@{
        ProcessId = [int]$processId
        ProcessInfo = $processInfo
        Identity = $identity
    }
}

function Get-HarnessLiteLlmProxyEnabled {
    param([int]$Port)
    $providerResponse = Invoke-LocalHttpGet -Uri "http://127.0.0.1:$Port/model-providers" |
        ConvertFrom-Json
    $providers = @($providerResponse)
    $proxyProviders = @($providers | Where-Object { $_.name -eq "litellm_proxy" })
    if ($proxyProviders.Count -ne 1 -or $proxyProviders[0].enabled -isnot [bool]) {
        throw "Existing Harness returned an invalid LiteLLM provider state."
    }
    return [bool]$proxyProviders[0].enabled
}

function Get-HarnessWorkState {
    param([int]$Port)
    try {
        $pageSize = 20
        $seenRunIds = [System.Collections.Generic.HashSet[string]]::new(
            [System.StringComparer]::Ordinal
        )
        $runResponse = Invoke-LocalHttpGet `
            -Uri "http://127.0.0.1:$Port/runs?limit=$pageSize&offset=0" |
            ConvertFrom-Json
        $runs = @($runResponse)
        if ($runs.Count -gt $pageSize) {
            return "unknown"
        }
        foreach ($run in $runs) {
            $runId = [string]$run.id
            $runStatus = [string]$run.status
            if (-not $runId -or -not $seenRunIds.Add($runId)) {
                return "unknown"
            }
            if ($runStatus -in @("queued", "running", "waiting")) {
                return "active"
            }
            if ($runStatus -notin @("completed", "failed", "cancelled")) {
                return "unknown"
            }

            $encodedRunId = [System.Uri]::EscapeDataString($runId)
            $jobResponse = Invoke-LocalHttpGet `
                -Uri "http://127.0.0.1:$Port/runs/$encodedRunId/runtime-jobs" |
                ConvertFrom-Json
            $jobs = @($jobResponse)
            foreach ($job in $jobs) {
                $jobRunId = [string]$job.run_id
                $jobStatus = [string]$job.status
                if ($jobRunId -cne $runId) {
                    return "unknown"
                }
                if ($jobStatus -in @("recorded", "approval_required", "approved")) {
                    return "active"
                }
                if ($jobStatus -notin @("completed", "failed", "rejected", "cancelled")) {
                    return "unknown"
                }
            }
        }
        if ($runs.Count -eq $pageSize) {
            return "unknown"
        }
        return "idle"
    } catch {
        return "unknown"
    }
}

function Get-HarnessReuseAction {
    param(
        [bool]$ExistingProxyEnabled,
        [bool]$CurrentProxyReady,
        [ValidateSet("idle", "active", "unknown")]
        [string]$WorkState
    )
    if ($ExistingProxyEnabled -eq $CurrentProxyReady) {
        return "reuse_current"
    }
    if ($WorkState -eq "idle") {
        if (-not $ExistingProxyEnabled -and $CurrentProxyReady) {
            return "restart_idle"
        }
        return "reuse_unrestorable"
    }
    if ($WorkState -eq "active") {
        return "reuse_active"
    }
    return "reuse_unknown"
}

function Stop-ExpectedHarnessProcess {
    param(
        [int]$ProcessId,
        [int]$Port,
        [object]$ExpectedIdentity
    )
    if (-not $ExpectedIdentity -or [int]$ExpectedIdentity.ProcessId -ne $ProcessId) {
        throw "The Team Agent Harness process identity is unavailable for restart."
    }
    if ((Get-HarnessWorkState -Port $Port) -ne "idle") {
        return $false
    }
    $currentInfo = Get-VerifiedServiceProcess `
        -ServiceRole "Harness" `
        -Port $Port `
        -ExpectedIdentity $ExpectedIdentity
    if (-not $currentInfo -or -not (Test-HarnessProcessInfo -ProcessInfo $currentInfo -Port $Port)) {
        throw "The Team Agent Harness process identity changed before restart."
    }
    $process = Get-Process -Id $ProcessId -ErrorAction Stop
    if (-not (Test-ProcessObjectMatchesIdentity $process $ExpectedIdentity)) {
        throw "The Team Agent Harness process identity changed before restart."
    }
    Stop-Process -InputObject $process -Force -ErrorAction Stop
    try {
        Wait-Process -Id $ProcessId -Timeout 10 -ErrorAction Stop
    } catch {
        if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
            throw "Team Agent Harness did not stop within 10 seconds."
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ((Get-ListenerProcessId -Port $Port) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 100
    }
    if (Get-ListenerProcessId -Port $Port) {
        throw "Team Agent Harness port $Port was not released after restart preparation."
    }
    return $true
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

function Wait-ForExpectedService {
    param(
        [string]$Name,
        [scriptblock]$Probe,
        [System.Diagnostics.Process]$Process,
        [object]$SpawnedIdentity,
        [ref]$ObservedServiceInstance,
        [ValidateSet("LiteLLM", "Harness", "ChromeProxy")]
        [string]$ServiceRole,
        [int]$Port,
        [int]$TimeoutSeconds = 30
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process -and $Process.HasExited) {
            throw "$Name exited before becoming healthy (exit code $($Process.ExitCode))."
        }
        $serviceInstance = Get-SpawnedServiceInstance `
            -ServiceRole $ServiceRole `
            -Port $Port `
            -SpawnedIdentity $SpawnedIdentity
        if ($serviceInstance) {
            if ($ObservedServiceInstance) {
                $ObservedServiceInstance.Value = $serviceInstance
            }
            $probeSucceeded = [bool](& $Probe)
            if (
                $probeSucceeded `
                -and (Get-VerifiedServiceProcess `
                    -ServiceRole $ServiceRole `
                    -Port $Port `
                    -ExpectedIdentity $serviceInstance.ListenerIdentity)
            ) {
                return
            }
        }
        Start-Sleep -Milliseconds 250
    }
    throw "$Name did not become healthy within $TimeoutSeconds seconds."
}

function Start-HarnessService {
    param(
        [string]$Name = "Team Agent Harness",
        [int]$Port = $HarnessPort,
        [switch]$DisableLiteLlmProxy
    )
    $savedApiKey = [Environment]::GetEnvironmentVariable("LITELLM_API_KEY", "Process")
    $savedBaseUrl = [Environment]::GetEnvironmentVariable("LITELLM_BASE_URL", "Process")
    try {
        if ($DisableLiteLlmProxy) {
            Remove-Item Env:LITELLM_API_KEY -ErrorAction SilentlyContinue
            Remove-Item Env:LITELLM_BASE_URL -ErrorAction SilentlyContinue
        }
        $process = Start-Process -FilePath $Python `
            -ArgumentList @(
                "-m",
                "uvicorn",
                "app.main:app",
                "--app-dir",
                ('"' + $Root + '"'),
                "--host",
                "127.0.0.1",
                "--port",
                "$Port"
            ) `
            -WorkingDirectory $Root `
            -RedirectStandardOutput $HarnessLog `
            -RedirectStandardError $HarnessErr `
            -WindowStyle Hidden `
            -PassThru
    } finally {
        [Environment]::SetEnvironmentVariable("LITELLM_API_KEY", $savedApiKey, "Process")
        [Environment]::SetEnvironmentVariable("LITELLM_BASE_URL", $savedBaseUrl, "Process")
    }
    $spawnedIdentity = Get-ProcessInstanceIdentity -Process $process
    if (-not $spawnedIdentity) {
        Stop-SpawnedProcessInstance -Process $process -SpawnedIdentity $spawnedIdentity
        throw "$Name spawned process identity could not be established."
    }
    $serviceInstance = $null
    try {
        Wait-ForExpectedService `
            -Name $Name `
            -Process $process `
            -SpawnedIdentity $spawnedIdentity `
            -ObservedServiceInstance ([ref]$serviceInstance) `
            -ServiceRole "Harness" `
            -Port $Port `
            -Probe { Test-HarnessEndpoint -Port $Port }
        return [PSCustomObject]@{
            Process = $process
            SpawnedIdentity = $spawnedIdentity
            ServiceInstance = $serviceInstance
            ServiceRole = "Harness"
            Port = $Port
        }
    } catch {
        Stop-SpawnedProcessInstance `
            -Process $process `
            -SpawnedIdentity $spawnedIdentity `
            -ServiceInstance $serviceInstance `
            -ServiceRole "Harness" `
            -Port $Port
        throw
    }
}

if ($FunctionsOnly) {
    return
}

$StartedServices = [System.Collections.Generic.List[object]]::new()
try {
$InitialHarnessIdentity = $null
$InitialHarnessPid = Get-ListenerProcessId -Port $HarnessPort
if ($InitialHarnessPid) {
    $InitialHarnessInfo = Get-UniqueListenerProcessInfo -Port $HarnessPort
    $InitialHarnessIdentity = Get-ProcessInfoIdentity $InitialHarnessInfo
    if (
        -not $InitialHarnessIdentity `
        -or [int]$InitialHarnessIdentity.ProcessId -ne [int]$InitialHarnessPid `
        -or -not (Test-HarnessProcessInfo -ProcessInfo $InitialHarnessInfo -Port $HarnessPort)
    ) {
        throw "Port $HarnessPort is occupied by PID $InitialHarnessPid, but it is not the expected Team Agent Harness service."
    }
} elseif (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python venv not found: $Python"
}

try {
    Import-LocalEnvFile $LocalEnvFile
} catch {
    Add-SupportWarning "Local environment file could not be loaded: $($_.Exception.Message)"
}

$env:TEAM_AGENT_ALLOW_REAL_MODEL_CALLS = "1"
$env:TEAM_AGENT_MODEL_ROUTING_CONFIG = $RoutingConfig
Remove-Item Env:LITELLM_BASE_URL -ErrorAction SilentlyContinue
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
if ($env:TEAM_AGENT_ALLOW_BROWSER_ACCESS -eq "1" -and $env:TEAM_AGENT_BROWSER_PROVIDER -eq "chrome") {
    $env:TEAM_AGENT_BROWSER_CDP_URL = "http://127.0.0.1:$BrowserProxyPort"
} else {
    Remove-Item Env:TEAM_AGENT_BROWSER_CDP_URL -ErrorAction SilentlyContinue
}

$OutputDir = Join-Path $Root "output"
New-Item -ItemType Directory -Force $OutputDir | Out-Null

$LiteLlmLog = Join-Path $OutputDir "litellm-proxy.log"
$LiteLlmErr = Join-Path $OutputDir "litellm-proxy.err.log"
$HarnessLog = Join-Path $OutputDir "harness-litellm.log"
$HarnessErr = Join-Path $OutputDir "harness-litellm.err.log"
$ChromeProxyLog = Join-Path $OutputDir "chrome-cdp-proxy.log"
$ChromeProxyErr = Join-Path $OutputDir "chrome-cdp-proxy.err.log"

$CredentialProblems = @()
if (-not $env:LITELLM_API_KEY -or -not $env:LITELLM_API_KEY.StartsWith("sk-")) {
    $CredentialProblems += "LITELLM_API_KEY"
}
if (-not $env:OPENAI_API_KEY) {
    $CredentialProblems += "OPENAI_API_KEY"
}
if (-not $env:OPENAI_API_BASE -or -not $env:OPENAI_API_BASE.StartsWith("http")) {
    $CredentialProblems += "OPENAI_API_BASE"
}
if (-not $env:DEEPSEEK_API_KEY) {
    $CredentialProblems += "DEEPSEEK_API_KEY"
}
if ($CredentialProblems.Count -gt 0) {
    Add-SupportWarning "Missing or invalid model credentials: $($CredentialProblems -join ', '). Harness remains available; real model routing may be unavailable."
}

$LiteLlmReady = $false
$LiteLlmPid = Get-ListenerProcessId -Port $LiteLlmPort
if ($LiteLlmPid) {
    if (-not (Test-LiteLlmProcess -ProcessId $LiteLlmPid -Port $LiteLlmPort)) {
        Add-SupportWarning "Port $LiteLlmPort is occupied by PID $LiteLlmPid, but it is not the expected LiteLLM service. Harness remains available."
    } elseif (Test-LiteLlmEndpoint -Port $LiteLlmPort) {
        $LiteLlmReady = $true
    } else {
        Add-SupportWarning "The expected LiteLLM process on port $LiteLlmPort did not pass its health check. Harness remains available."
    }
} elseif ($CredentialProblems.Count -gt 0) {
    Add-SupportWarning "LiteLLM was not started because its model credentials are incomplete."
} elseif (-not (Test-Path $LiteLlmPythonExe)) {
    Add-SupportWarning "LiteLLM Python is unavailable. Run the desktop setup to restore optional model routing support."
} elseif (-not (Test-Path $LiteLlmRunner)) {
    Add-SupportWarning "LiteLLM runner is unavailable. Harness remains available without the proxy."
} elseif (-not (Test-Path $LiteLlmConfig)) {
    Add-SupportWarning "LiteLLM configuration is unavailable. Harness remains available without the proxy."
} else {
    $LiteLlmVersion = ""
    try {
        $LiteLlmVersion = [string](& $LiteLlmPythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $LiteLlmVersion.Trim()) {
            throw "Unable to inspect the LiteLLM Python runtime."
        }
        if ([version]$LiteLlmVersion.Trim() -ge [version]"3.14") {
            throw "LiteLLM requires the project-local Python 3.12 or 3.13 environment."
        }

        & $LiteLlmPythonExe -m pip show litellm *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "LiteLLM is not installed. Run the desktop setup; runtime startup never installs dependencies."
        }

        $LiteLlmProcess = Start-Process -FilePath $LiteLlmPythonExe `
            -ArgumentList @(
                ('"' + $LiteLlmRunner + '"'),
                $ProjectRootArgument,
                ('"' + $Root + '"'),
                "--config",
                ('"' + $LiteLlmConfig + '"'),
                "--host",
                "127.0.0.1",
                "--port",
                "$LiteLlmPort",
                "--request_timeout",
                "$env:REQUEST_TIMEOUT"
            ) `
            -WorkingDirectory $Root `
            -RedirectStandardOutput $LiteLlmLog `
            -RedirectStandardError $LiteLlmErr `
            -WindowStyle Hidden `
            -PassThru
        $LiteLlmProcessIdentity = Get-ProcessInstanceIdentity -Process $LiteLlmProcess
        if (-not $LiteLlmProcessIdentity) {
            Stop-SpawnedProcessInstance -Process $LiteLlmProcess -SpawnedIdentity $LiteLlmProcessIdentity
            throw "LiteLLM spawned process identity could not be established."
        }
        $LiteLlmServiceInstance = $null
        try {
            Wait-ForExpectedService `
                -Name "LiteLLM" `
                -Process $LiteLlmProcess `
                -SpawnedIdentity $LiteLlmProcessIdentity `
                -ObservedServiceInstance ([ref]$LiteLlmServiceInstance) `
                -ServiceRole "LiteLLM" `
                -Port $LiteLlmPort `
                -Probe { Test-LiteLlmEndpoint -Port $LiteLlmPort }
            $LiteLlmReady = $true
            [void]$StartedServices.Add([PSCustomObject]@{
                Process = $LiteLlmProcess
                SpawnedIdentity = $LiteLlmProcessIdentity
                ServiceInstance = $LiteLlmServiceInstance
                ServiceRole = "LiteLLM"
                Port = $LiteLlmPort
            })
        } catch {
            Stop-SpawnedProcessInstance `
                -Process $LiteLlmProcess `
                -SpawnedIdentity $LiteLlmProcessIdentity `
                -ServiceInstance $LiteLlmServiceInstance `
                -ServiceRole "LiteLLM" `
                -Port $LiteLlmPort
            throw
        }
    } catch {
        Add-SupportWarning "LiteLLM support is unavailable: $($_.Exception.Message)"
    }
}

if ($LiteLlmReady) {
    $env:LITELLM_BASE_URL = "http://127.0.0.1:$LiteLlmPort/v1"
} else {
    Remove-Item Env:LITELLM_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:LITELLM_BASE_URL -ErrorAction SilentlyContinue
}

$HarnessListener = Get-RevalidatedHarnessListener `
    -Port $HarnessPort `
    -InitialIdentity $InitialHarnessIdentity
$HarnessPid = if ($HarnessListener) { [int]$HarnessListener.ProcessId } else { $null }
if ($HarnessListener) {
    $HarnessInfo = $HarnessListener.ProcessInfo
    $HarnessIdentity = $HarnessListener.Identity
    $existingProxyEnabled = Get-HarnessLiteLlmProxyEnabled -Port $HarnessPort
    $workState = if ($existingProxyEnabled -ne $LiteLlmReady) {
        Get-HarnessWorkState -Port $HarnessPort
    } else {
        "unknown"
    }
    $reuseAction = Get-HarnessReuseAction `
        -ExistingProxyEnabled $existingProxyEnabled `
        -CurrentProxyReady $LiteLlmReady `
        -WorkState $workState
    if ($reuseAction -eq "restart_idle") {
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
            throw "Python venv not found: $Python"
        }
        if (Stop-ExpectedHarnessProcess `
            -ProcessId $HarnessPid `
            -Port $HarnessPort `
            -ExpectedIdentity $HarnessIdentity) {
            Write-Host "Restarting idle Team Agent Harness to load the verified LiteLLM provider state."
            $restoreAction = {
                [void](Start-HarnessService `
                    -Name "Previous Team Agent Harness" `
                    -Port $HarnessPort `
                    -DisableLiteLlmProxy)
            }.GetNewClosure()
            [void]$StartedServices.Add([PSCustomObject]@{
                Process = $null
                RestoreAction = $restoreAction
            })
            $HarnessPid = $null
        } else {
            Add-SupportWarning "Existing Harness became active while provider state was being checked, so it was safely reused without reconfiguration."
        }
    } elseif ($reuseAction -eq "reuse_active") {
        Add-SupportWarning "Existing Harness has active run or runtime-job work and was safely reused; its LiteLLM provider state will refresh after the work finishes and the launcher is run again."
    } elseif ($reuseAction -eq "reuse_unknown") {
        Add-SupportWarning "Existing Harness work state could not be proven idle and was safely reused; its LiteLLM provider state was not changed."
    } elseif ($reuseAction -eq "reuse_unrestorable") {
        Add-SupportWarning "Existing Harness was safely reused because its enabled LiteLLM provider state cannot be reconstructed after shutdown."
    } else {
        Add-SupportWarning "Existing Harness was safely reused. Saved model or browser configuration changes are not loaded until an explicit stop and restart."
    }
}
if (-not $HarnessPid) {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Python venv not found: $Python"
    }
    $HarnessService = Start-HarnessService -Port $HarnessPort
    [void]$StartedServices.Add($HarnessService)
}

Write-Host "Harness UI:     http://127.0.0.1:$HarnessPort/"

$BrowserProxyReady = $false
if ($env:TEAM_AGENT_ALLOW_BROWSER_ACCESS -eq "1" -and $env:TEAM_AGENT_BROWSER_PROVIDER -eq "chrome") {
    $BrowserProxyPid = Get-ListenerProcessId -Port $BrowserProxyPort
    if ($BrowserProxyPid) {
        if (-not (Test-ChromeProxyProcess -ProcessId $BrowserProxyPid -Port $BrowserProxyPort)) {
            Add-SupportWarning "Port $BrowserProxyPort is occupied by PID $BrowserProxyPid, but it is not the expected Chrome CDP proxy. Harness remains available."
        } elseif (Test-ChromeProxyEndpoint -Port $BrowserProxyPort) {
            $BrowserProxyReady = $true
        } else {
            Add-SupportWarning "The expected Chrome CDP proxy process on port $BrowserProxyPort did not pass its health check. Harness remains available."
        }
    } elseif (-not (Test-Path $ChromeCdpProxy)) {
        Add-SupportWarning "Chrome CDP proxy is unavailable. Harness remains available without browser access."
    } else {
        try {
            $BrowserProxyProcess = Start-Process -FilePath $DefaultHarnessPython `
                -ArgumentList @($ChromeCdpProxy, "--host", "127.0.0.1", "--port", "$BrowserProxyPort", "--chrome-debug-port", "$ChromeDebugPort") `
                -WorkingDirectory $Root `
                -RedirectStandardOutput $ChromeProxyLog `
                -RedirectStandardError $ChromeProxyErr `
                -WindowStyle Hidden `
                -PassThru
            $BrowserProxyProcessIdentity = Get-ProcessInstanceIdentity -Process $BrowserProxyProcess
            if (-not $BrowserProxyProcessIdentity) {
                Stop-SpawnedProcessInstance `
                    -Process $BrowserProxyProcess `
                    -SpawnedIdentity $BrowserProxyProcessIdentity
                throw "Chrome CDP proxy spawned process identity could not be established."
            }
            $BrowserProxyServiceInstance = $null
            try {
                Wait-ForExpectedService `
                    -Name "Chrome CDP proxy" `
                    -Process $BrowserProxyProcess `
                    -SpawnedIdentity $BrowserProxyProcessIdentity `
                    -ObservedServiceInstance ([ref]$BrowserProxyServiceInstance) `
                    -ServiceRole "ChromeProxy" `
                    -Port $BrowserProxyPort `
                    -Probe { Test-ChromeProxyEndpoint -Port $BrowserProxyPort }
                $BrowserProxyReady = $true
                [void]$StartedServices.Add([PSCustomObject]@{
                    Process = $BrowserProxyProcess
                    SpawnedIdentity = $BrowserProxyProcessIdentity
                    ServiceInstance = $BrowserProxyServiceInstance
                    ServiceRole = "ChromeProxy"
                    Port = $BrowserProxyPort
                })
            } catch {
                Stop-SpawnedProcessInstance `
                    -Process $BrowserProxyProcess `
                    -SpawnedIdentity $BrowserProxyProcessIdentity `
                    -ServiceInstance $BrowserProxyServiceInstance `
                    -ServiceRole "ChromeProxy" `
                    -Port $BrowserProxyPort
                throw
            }
        } catch {
            Add-SupportWarning "Chrome browser support is unavailable: $($_.Exception.Message)"
        }
    }
}

if ($LiteLlmReady) {
    Write-Host "LiteLLM Proxy: http://127.0.0.1:$LiteLlmPort"
} else {
    Write-Host "LiteLLM Proxy: unavailable (Harness is still usable)"
}
Write-Host "Routing config: $RoutingConfig"
Write-Host "Model timeout:  $env:TEAM_AGENT_MODEL_TIMEOUT_SECONDS seconds"
Write-Host "LiteLLM retries: $env:DEFAULT_MAX_RETRIES"
if ($BrowserProxyReady) {
    Write-Host "Chrome bridge:  http://127.0.0.1:$BrowserProxyPort"
    Write-Host "Chrome debug:   http://127.0.0.1:$ChromeDebugPort"
}

if ($SupportWarnings.Count -gt 0) {
    Write-Host "Harness is ready with $($SupportWarnings.Count) support warning(s)."
    exit 2
}
} catch {
    $startupError = $_
    try {
        Invoke-StartupRollback -StartedServices @($StartedServices)
    } catch {
        [Console]::Error.WriteLine("WARNING: Startup rollback encountered an error.")
    }
    throw $startupError
}
