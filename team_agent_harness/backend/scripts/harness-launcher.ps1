param(
    [switch]$FunctionsOnly
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
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

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$ProjectRootArgument = "--team-agent-project-root"
$EnvFile = Join-Path $Root ".env.local"
$EnvExampleFile = Join-Path $Root ".env.local.example"
$StartScript = Join-Path $PSScriptRoot "start-litellm-harness.ps1"
$HarnessPython = Join-Path $Root ".venv\Scripts\python.exe"
$LiteLlmPython = Join-Path $Root ".venv-litellm\Scripts\python.exe"
$LiteLlmRunner = Join-Path $PSScriptRoot "run_litellm_proxy.py"
$LiteLlmConfig = Join-Path $Root "config\litellm.config.example.yaml"
$ChromeCdpProxy = Join-Path $PSScriptRoot "chrome_cdp_proxy.py"
$ChromeProxyPort = 3456
$ChromeDebugPort = 9223
$OutputDir = Join-Path $Root "output"
$HarnessUrl = "http://127.0.0.1:8014/"
$LiteLlmUrl = "http://127.0.0.1:4000/"
$StartupTimeoutSeconds = 180
$HarnessReadinessProbe = {
    param(
        [int]$Port,
        [string]$ExpectedPython,
        [string]$ExpectedBasePython,
        [string]$ExpectedRoot
    )

    Add-Type -AssemblyName System.Net.Http
    $uiMarker = 'id="mainWorkspace"'
    $requiredPaths = @(
        "/team-selections/validate",
        "/workflow-packs/{pack_name}/team-template",
        "/runs/{run_id}/team"
    )

    function Invoke-ProbeHttpGet {
        param(
            [string]$Uri,
            [switch]$IncludeMetadata
        )
        $handler = [System.Net.Http.HttpClientHandler]::new()
        $handler.UseProxy = $false
        $handler.AllowAutoRedirect = $false
        $client = [System.Net.Http.HttpClient]::new($handler)
        $client.MaxResponseContentBufferSize = 1048576
        $client.Timeout = [TimeSpan]::FromSeconds(1)
        $request = $null
        $response = $null
        try {
            $request = [System.Net.Http.HttpRequestMessage]::new(
                [System.Net.Http.HttpMethod]::Get,
                $Uri
            )
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

    function Get-ProbeFullyQualifiedPath {
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

    try {
        $listenerPids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            ForEach-Object { [int]$_.OwningProcess } |
            Where-Object { $_ -gt 0 } |
            Sort-Object -Unique)
        if ($listenerPids.Count -ne 1) {
            return $false
        }
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($listenerPids[0])" -ErrorAction Stop
        if (-not $processInfo -or -not $processInfo.CommandLine -or -not $processInfo.ExecutablePath) {
            return $false
        }
        $actualExecutable = Get-ProbeFullyQualifiedPath ([string]$processInfo.ExecutablePath)
        if (-not $actualExecutable) {
            return $false
        }
        $expectedExecutables = @($ExpectedPython, $ExpectedBasePython) | Where-Object { $_ }
        $executableMatches = @($expectedExecutables | Where-Object {
            $expectedExecutable = Get-ProbeFullyQualifiedPath ([string]$_)
            $expectedExecutable -and [string]::Equals(
                $actualExecutable,
                $expectedExecutable,
                [System.StringComparison]::OrdinalIgnoreCase
            )
        }).Count -gt 0
        if (-not $executableMatches) {
            return $false
        }
        $arguments = @([TeamAgentHarness.ProcessCommandLine]::Split([string]$processInfo.CommandLine))
        if ($arguments.Count -ne 10) {
            return $false
        }
        $commandExecutable = Get-ProbeFullyQualifiedPath $arguments[0]
        $expectedCommandExecutable = Get-ProbeFullyQualifiedPath $ExpectedPython
        $commandRoot = Get-ProbeFullyQualifiedPath $arguments[5]
        $expectedCommandRoot = Get-ProbeFullyQualifiedPath $ExpectedRoot
        $commandExecutableMatches = $commandExecutable -and $expectedCommandExecutable -and [string]::Equals(
            $commandExecutable,
            $expectedCommandExecutable,
            [System.StringComparison]::OrdinalIgnoreCase
        )
        if (
            -not $commandExecutableMatches `
            -or $arguments[1] -cne "-m" `
            -or $arguments[2] -cne "uvicorn" `
            -or $arguments[3] -cne "app.main:app" `
            -or $arguments[4] -cne "--app-dir" `
            -or -not $commandRoot `
            -or -not $expectedCommandRoot `
            -or -not [string]::Equals(
                $commandRoot,
                $expectedCommandRoot,
                [System.StringComparison]::OrdinalIgnoreCase
            ) `
            -or $arguments[6] -cne "--host" `
            -or $arguments[7] -cne "127.0.0.1" `
            -or $arguments[8] -cne "--port" `
            -or $arguments[9] -cne "$Port"
        ) {
            return $false
        }

        $health = Invoke-ProbeHttpGet -Uri "http://127.0.0.1:$Port/health" | ConvertFrom-Json
        $page = Invoke-ProbeHttpGet -Uri "http://127.0.0.1:$Port/" -IncludeMetadata
        $openApi = Invoke-ProbeHttpGet -Uri "http://127.0.0.1:$Port/openapi.json" | ConvertFrom-Json
        $openApiPaths = @($openApi.paths.PSObject.Properties.Name)
        $missingPaths = @($requiredPaths | Where-Object { $openApiPaths -notcontains $_ })
        return $health.status -eq "ok" `
            -and $health.worker -eq "running" `
            -and $page.ContentType -eq "text/html" `
            -and $page.Content.Contains($uiMarker) `
            -and $openApi.info.title -eq "Team Agent Harness" `
            -and $openApi.info.version -eq "0.1.0" `
            -and $missingPaths.Count -eq 0
    } catch {
        return $false
    }
}
$script:ReadinessProbePowerShell = $null
$script:ReadinessProbeAsyncResult = $null
$script:LastUiReady = $false

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

$HarnessBasePython = Get-PythonBaseExecutable $HarnessPython
$LiteLlmBasePython = Get-PythonBaseExecutable $LiteLlmPython

function ZH {
    param([int[]]$CodePoints)
    return -join ($CodePoints | ForEach-Object { [char]$_ })
}

$T = @{
    Title = ZH 22242,38431,26234,33021,20307,21551,21160,22120
    LiteLlmKey = ZH 26412,22320,32,76,105,116,101,76,76,77,32,21475,20196
    OpenAiKey = ZH 79,112,101,110,65,73,32,20013,36716,31449,32,75,101,121
    OpenAiBase = ZH 79,112,101,110,65,73,32,20013,36716,31449,22320,22336
    DeepSeekKey = ZH 68,101,101,112,83,101,101,107,32,75,101,121
    ShowKeys = ZH 26174,31034,32,75,101,121
    SaveConfig = ZH 20445,23384,37197,32622
    StartServices = ZH 21551,21160,26381,21153
    StopServices = ZH 20572,27490,26381,21153
    Refresh = ZH 21047,26032,29366,24577
    OpenUi = ZH 25171,24320,39029,38754
    OpenProject = ZH 25171,24320,39033,30446
    OpenConfig = ZH 25171,24320,37197,32622
    OpenLogs = ZH 25171,24320,26085,24535
    Hint = ZH 25552,31034,65306,26412,22320,32,76,105,116,101,76,76,77,32,21475,20196,21487,22635,32,115,107,45,100,101,118,45,108,111,99,97,108,45,107,101,121,65307,20854,20182,32,75,101,121,32,24517,39035,22635,30495,23454,20540
    Running = ZH 36816,34892,20013
    Stopped = ZH 26410,36816,34892
    PortOccupied = ZH 20854,20182,36827,31243,21344,29992
    LoadedConfig = ZH 24050,21152,36733,37197,32622
    SavedConfig = ZH 24050,20445,23384,32,46,101,110,118,46,108,111,99,97,108
    SaveFailed = ZH 20445,23384,22833,36133
    StartFailed = ZH 21551,21160,22833,36133
    StartSent = ZH 24050,21457,36865,21551,21160,21629,20196,65292,27491,22312,31561,24453,25511,21046,21488,23601,32490
    StoppedLog = ZH 24050,20572,27490,26412,39033,30446,26381,21153,65307,20854,20182,31471,21475,36827,31243,26410,22788,29702
    StopIncomplete = ZH 37096,20998,26381,21153,26410,33021,23433,20840,20572,27490
    StatusRefreshed = ZH 29366,24577,24050,21047,26032
    Starting = ZH 27491,22312,21551,21160,65292,35831,31245,20505
    Ready = ZH 25511,21046,21488,24050,23601,32490
    ReadyOpened = ZH 25511,21046,21488,24050,23601,32490,65292,24050,25171,24320,39029,38754
    ReadyWarnings = ZH 25511,21046,21488,24050,23601,32490,65292,20294,37096,20998,36741,21161,26381,21153,21551,21160,22833,36133
    StartupFailed = ZH 21551,21160,36827,31243,36864,20986,65292,25511,21046,21488,26410,23601,32490
    StartupTimedOut = ZH 21551,21160,31561,24453,36229,26102,65292,20173,22312,30417,27979,65307,35831,26597,30475,26085,24535
    UiUnavailable = ZH 25511,21046,21488,23578,26410,23601,32490,65292,26242,19981,33021,25171,24320,39029,38754
    StartAlreadyRunning = ZH 21551,21160,20219,21153,27491,22312,36827,34892,65292,35831,21247,37325,22797,28857,20987
    SupervisorLogs = ZH 21551,21160,32,115,117,112,101,114,118,105,115,111,114,32,26085,24535
    OpenFailed = ZH 25171,24320,39029,38754,22833,36133
}

function Read-EnvFile {
    param([string]$Path)
    $values = @{}
    if (-not (Test-Path $Path)) {
        return $values
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $match = [regex]::Match($trimmed, "^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
        if (-not $match.Success) {
            continue
        }
        $name = $match.Groups[1].Value
        $value = $match.Groups[2].Value.Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$name] = $value
    }
    return $values
}

function Commit-AtomicEnvFile {
    param(
        [string]$TemporaryPath,
        [string]$DestinationPath
    )
    if (Test-Path -LiteralPath $DestinationPath) {
        $destinationAcl = [System.IO.File]::GetAccessControl($DestinationPath)
        [System.IO.File]::SetAccessControl($TemporaryPath, $destinationAcl)
        $directory = [System.IO.Path]::GetDirectoryName($DestinationPath)
        $fileName = [System.IO.Path]::GetFileName($DestinationPath)
        $backupPath = Join-Path $directory ".$fileName.$([Guid]::NewGuid().ToString('N')).bak"
        try {
            [System.IO.File]::Replace($TemporaryPath, $DestinationPath, $backupPath, $true)
        } finally {
            if (Test-Path -LiteralPath $backupPath) {
                Remove-Item -LiteralPath $backupPath -Force
            }
        }
    } else {
        [System.IO.File]::Move($TemporaryPath, $DestinationPath)
    }
}

function Write-EnvFileAtomically {
    param(
        [string]$Path,
        [string[]]$Lines
    )
    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $directory = [System.IO.Path]::GetDirectoryName($fullPath)
    $fileName = [System.IO.Path]::GetFileName($fullPath)
    $temporaryPath = Join-Path $directory ".$fileName.$([Guid]::NewGuid().ToString('N')).tmp"
    $encoding = [System.Text.UTF8Encoding]::new($true)
    $text = [string]::Join([Environment]::NewLine, $Lines) + [Environment]::NewLine
    $preamble = $encoding.GetPreamble()
    $payload = $encoding.GetBytes($text)
    $stream = $null
    try {
        $stream = [System.IO.FileStream]::new(
            $temporaryPath,
            [System.IO.FileMode]::CreateNew,
            [System.IO.FileAccess]::Write,
            [System.IO.FileShare]::None,
            4096,
            [System.IO.FileOptions]::WriteThrough
        )
        if ($preamble.Length -gt 0) {
            $stream.Write($preamble, 0, $preamble.Length)
        }
        $stream.Write($payload, 0, $payload.Length)
        $stream.Flush($true)
        $stream.Dispose()
        $stream = $null
        Commit-AtomicEnvFile -TemporaryPath $temporaryPath -DestinationPath $fullPath
    } finally {
        if ($stream) {
            $stream.Dispose()
        }
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

function Save-EnvFile {
    param(
        [string]$LiteLlmApiKey,
        [string]$OpenAiApiKey,
        [string]$OpenAiApiBase,
        [string]$DeepSeekApiKey
    )
    $managedValues = [ordered]@{
        LITELLM_API_KEY = $LiteLlmApiKey
        OPENAI_API_KEY = $OpenAiApiKey
        OPENAI_API_BASE = $OpenAiApiBase
        DEEPSEEK_API_KEY = $DeepSeekApiKey
    }
    foreach ($value in $managedValues.Values) {
        if ([string]$value -match "[\x00\r\n]") {
            throw "Environment values must be single-line text."
        }
    }

    $existingLines = if (Test-Path -LiteralPath $EnvFile) {
        @(Get-Content -LiteralPath $EnvFile)
    } else {
        @("# Local Team Agent Harness credentials. Do not commit this file.")
    }
    $updatedKeys = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::OrdinalIgnoreCase
    )
    $content = [System.Collections.Generic.List[string]]::new()
    foreach ($line in $existingLines) {
        $match = [regex]::Match(
            [string]$line,
            '^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=.*$'
        )
        $key = if ($match.Success) { $match.Groups[1].Value } else { "" }
        if ($key -and $managedValues.Contains($key)) {
            if ($updatedKeys.Add($key)) {
                $content.Add("$key=$($managedValues[$key])")
            }
            continue
        }
        $content.Add([string]$line)
    }
    foreach ($key in $managedValues.Keys) {
        if ($updatedKeys.Add($key)) {
            $content.Add("$key=$($managedValues[$key])")
        }
    }
    Write-EnvFileAtomically -Path $EnvFile -Lines $content.ToArray()
}

function Test-HarnessUiReady {
    param([int]$Port = 8014)
    return [bool](& $HarnessReadinessProbe $Port $HarnessPython $HarnessBasePython $Root)
}

function Start-HarnessUiProbe {
    param([int]$Port = 8014)
    if ($script:ReadinessProbeAsyncResult) {
        return $false
    }

    $powerShell = [System.Management.Automation.PowerShell]::Create()
    try {
        [void]$powerShell.AddScript($HarnessReadinessProbe.ToString())
        [void]$powerShell.AddArgument($Port)
        [void]$powerShell.AddArgument($HarnessPython)
        [void]$powerShell.AddArgument($HarnessBasePython)
        [void]$powerShell.AddArgument($Root)
        $script:ReadinessProbePowerShell = $powerShell
        $script:ReadinessProbeAsyncResult = $powerShell.BeginInvoke()
        return $true
    } catch {
        $powerShell.Dispose()
        $script:ReadinessProbePowerShell = $null
        $script:ReadinessProbeAsyncResult = $null
        $script:LastUiReady = $false
        return $false
    }
}

function Complete-HarnessUiProbe {
    if (-not $script:ReadinessProbeAsyncResult -or -not $script:ReadinessProbeAsyncResult.IsCompleted) {
        return $false
    }

    try {
        $results = $script:ReadinessProbePowerShell.EndInvoke($script:ReadinessProbeAsyncResult)
        if ($results.Count -gt 0) {
            $lastResult = @($results)[$results.Count - 1]
            $script:LastUiReady = [System.Management.Automation.LanguagePrimitives]::ConvertTo(
                $lastResult,
                [bool]
            )
        } else {
            $script:LastUiReady = $false
        }
    } catch {
        $script:LastUiReady = $false
    } finally {
        $script:ReadinessProbePowerShell.Dispose()
        $script:ReadinessProbePowerShell = $null
        $script:ReadinessProbeAsyncResult = $null
    }
    return $true
}

function Stop-HarnessUiProbe {
    if (-not $script:ReadinessProbePowerShell) {
        return
    }
    try {
        if ($script:ReadinessProbeAsyncResult -and -not $script:ReadinessProbeAsyncResult.IsCompleted) {
            $script:ReadinessProbePowerShell.Stop()
        } elseif ($script:ReadinessProbeAsyncResult) {
            [void]$script:ReadinessProbePowerShell.EndInvoke($script:ReadinessProbeAsyncResult)
        }
    } catch {
    } finally {
        $script:ReadinessProbePowerShell.Dispose()
        $script:ReadinessProbePowerShell = $null
        $script:ReadinessProbeAsyncResult = $null
    }
}

function Get-StartupObservation {
    param(
        [bool]$UiReady,
        [bool]$ProcessExited,
        [int]$ExitCode,
        [DateTime]$DeadlineUtc,
        [DateTime]$NowUtc = [DateTime]::UtcNow
    )
    if ($ProcessExited) {
        if ($UiReady) {
            if ($ExitCode -ne 0) {
                return "ReadyWithWarnings"
            }
            return "Ready"
        }
        return "Failed"
    }
    if ($NowUtc -ge $DeadlineUtc) {
        if ($UiReady) {
            return "ReadyWithWarnings"
        }
        return "TimedOut"
    }
    if ($UiReady) {
        return "Ready"
    }
    return "Starting"
}

function Protect-LauncherText {
    param(
        [string]$Text,
        [string[]]$SensitiveValues = @(),
        [string]$HomePath = $env:USERPROFILE
    )
    if (-not $Text) {
        return ""
    }

    $redacted = [string]$Text
    foreach ($sensitiveValue in @($SensitiveValues | Where-Object { $_ } | Sort-Object Length -Descending -Unique)) {
        $redacted = [regex]::Replace(
            $redacted,
            [regex]::Escape([string]$sensitiveValue),
            "[REDACTED]",
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
    }
    $redacted = [regex]::Replace(
        $redacted,
        '(?i)(Authorization\s*:\s*Bearer\s+)[^\s]+',
        '${1}[REDACTED]'
    )
    $redacted = [regex]::Replace(
        $redacted,
        '(?i)((?:OPENAI|DEEPSEEK|LITELLM)_(?:API_KEY|TOKEN|PASSWORD)\s*=\s*)[^\s]+',
        '${1}[REDACTED]'
    )
    $redacted = [regex]::Replace(
        $redacted,
        '(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s]+',
        '${1}[REDACTED]'
    )
    $redacted = [regex]::Replace(
        $redacted,
        '(?i)(https?://)[^/\s:@]+:[^/\s@]+@',
        '${1}[REDACTED]@'
    )
    if ($HomePath) {
        $redacted = [regex]::Replace(
            $redacted,
            [regex]::Escape($HomePath),
            "%USERPROFILE%",
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
    }
    return $redacted -replace '[\x00-\x08\x0B\x0C\x0E-\x1F]', ''
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
    $currentIdentity = Get-ProcessInfoIdentity $ProcessInfo
    return $null -ne $currentIdentity `
        -and $null -ne $ExpectedIdentity `
        -and [int]$currentIdentity.ProcessId -eq [int]$ExpectedIdentity.ProcessId `
        -and [long]$currentIdentity.CreationTicks -eq [long]$ExpectedIdentity.CreationTicks
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

function Test-HarnessCommandArguments {
    param(
        [string[]]$Arguments,
        [int]$Port
    )
    return $Arguments.Count -eq 10 `
        -and (Test-SameExecutablePath $Arguments[0] $HarnessPython) `
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
        -not (Test-SameExecutablePath $Arguments[0] $LiteLlmPython) `
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
        -and (Test-SameExecutablePath $Arguments[0] $HarnessPython) `
        -and (Test-SameExecutablePath $Arguments[1] $ChromeCdpProxy) `
        -and $Arguments[2] -ceq "--host" `
        -and $Arguments[3] -ceq "127.0.0.1" `
        -and $Arguments[4] -ceq "--port" `
        -and $Arguments[5] -ceq "$Port" `
        -and $Arguments[6] -ceq "--chrome-debug-port" `
        -and $Arguments[7] -ceq "$ChromeDebugPort"
}

function Test-ProjectServiceProcess {
    param(
        [object]$ProcessInfo,
        [ValidateSet("Harness", "LiteLLM", "ChromeProxy")]
        [string]$ServiceRole,
        [int]$Port
    )
    if (-not $ProcessInfo -or -not $ProcessInfo.CommandLine) {
        return $false
    }

    $expectedProcessExecutables = if ($ServiceRole -eq "LiteLLM") {
        @($LiteLlmPython, $LiteLlmBasePython)
    } else {
        @($HarnessPython, $HarnessBasePython)
    }
    $actualExecutableMatches = @($expectedProcessExecutables | Where-Object {
        $_ -and (Test-SameExecutablePath $ProcessInfo.ExecutablePath $_)
    }).Count -gt 0
    if (-not $actualExecutableMatches) {
        return $false
    }

    $arguments = @(Get-ProcessCommandArguments ([string]$ProcessInfo.CommandLine))
    if ($ServiceRole -eq "Harness") {
        return Test-HarnessCommandArguments -Arguments $arguments -Port $Port
    }
    if ($ServiceRole -eq "ChromeProxy") {
        return Test-ChromeProxyCommandArguments -Arguments $arguments -Port $Port
    }
    return Test-LiteLlmCommandArguments -Arguments $arguments -Port $Port
}

function Get-VerifiedProjectServiceProcess {
    param(
        [int]$Port,
        [ValidateSet("Harness", "LiteLLM", "ChromeProxy")]
        [string]$ServiceRole,
        [object]$ExpectedIdentity
    )
    if (-not $ExpectedIdentity) {
        return $null
    }
    $listenerPids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { [int]$_.OwningProcess } |
        Where-Object { $_ -gt 0 } |
        Sort-Object -Unique)
    if ($listenerPids.Count -ne 1 -or $listenerPids[0] -ne [int]$ExpectedIdentity.ProcessId) {
        return $null
    }
    $currentInfo = Get-CimInstance -ClassName Win32_Process `
        -Filter "ProcessId = $($ExpectedIdentity.ProcessId)" `
        -ErrorAction SilentlyContinue
    if (
        -not (Test-SameProcessInstance $currentInfo $ExpectedIdentity) `
        -or -not (Test-ProjectServiceProcess $currentInfo $ServiceRole $Port)
    ) {
        return $null
    }
    $confirmedPids = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { [int]$_.OwningProcess } |
        Where-Object { $_ -gt 0 } |
        Sort-Object -Unique)
    if ($confirmedPids.Count -ne 1 -or $confirmedPids[0] -ne [int]$ExpectedIdentity.ProcessId) {
        return $null
    }
    return $currentInfo
}

function Invoke-ChromeProxyGracefulShutdown {
    param(
        [int]$Port,
        [object]$ExpectedIdentity
    )
    if (-not (Get-VerifiedProjectServiceProcess `
        -Port $Port `
        -ServiceRole "ChromeProxy" `
        -ExpectedIdentity $ExpectedIdentity)) {
        return $false
    }

    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.MaxResponseContentBufferSize = 65536
    $client.Timeout = [TimeSpan]::FromSeconds(12)
    $request = $null
    $response = $null
    try {
        if (-not (Get-VerifiedProjectServiceProcess `
            -Port $Port `
            -ServiceRole "ChromeProxy" `
            -ExpectedIdentity $ExpectedIdentity)) {
            return $false
        }
        $request = [System.Net.Http.HttpRequestMessage]::new(
            [System.Net.Http.HttpMethod]::Post,
            "http://127.0.0.1:$Port/shutdown"
        )
        [void]$request.Headers.TryAddWithoutValidation("X-Team-Agent-Browser-Proxy", "1")
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        if (-not $response.IsSuccessStatusCode) {
            return $false
        }
        $payload = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
        if ($payload.status -cne "stopping") {
            return $false
        }
    } catch {
        return $false
    } finally {
        if ($response) {
            $response.Dispose()
        }
        if ($request) {
            $request.Dispose()
        }
        $client.Dispose()
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while ([DateTime]::UtcNow -lt $deadline) {
        $currentInfo = Get-CimInstance Win32_Process `
            -Filter "ProcessId = $($ExpectedIdentity.ProcessId)" `
            -ErrorAction SilentlyContinue
        if (-not (Test-SameProcessInstance $currentInfo $ExpectedIdentity)) {
            return $true
        }
        Start-Sleep -Milliseconds 100
    }
    return $false
}

function Get-PortServiceState {
    param(
        [int]$Port,
        [ValidateSet("Harness", "LiteLLM", "ChromeProxy")]
        [string]$ServiceRole
    )

    $connections = @(Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" })
    $listenerPids = @($connections |
        ForEach-Object { [int]$_.OwningProcess } |
        Where-Object { $_ -gt 0 } |
        Sort-Object -Unique)
    $targetProcesses = @()
    $otherPids = @()

    foreach ($processId in $listenerPids) {
        $processInfo = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
        if (Test-ProjectServiceProcess $processInfo $ServiceRole $Port) {
            $targetProcesses += $processInfo
        } else {
            $otherPids += $processId
        }
    }

    $state = if (-not $listenerPids.Count) {
        "Stopped"
    } elseif ($targetProcesses.Count -and $otherPids.Count) {
        "PortConflict"
    } elseif ($targetProcesses.Count) {
        "TargetRunning"
    } else {
        "PortOccupied"
    }
    return [PSCustomObject]@{
        State = $state
        Port = $Port
        ServiceRole = $ServiceRole
        TargetProcesses = @($targetProcesses)
        TargetPids = @($targetProcesses | ForEach-Object { [int]$_.ProcessId })
        OtherPids = @($otherPids)
    }
}

function Port-Status {
    param(
        [int]$Port,
        [ValidateSet("Harness", "LiteLLM", "ChromeProxy")]
        [string]$ServiceRole
    )
    $status = Get-PortServiceState $Port $ServiceRole
    $targetPidText = ($status.TargetPids -join ",")
    $otherPidText = ($status.OtherPids -join ",")
    switch ($status.State) {
        "TargetRunning" { return "$($T.Running) (PID $targetPidText)" }
        "PortOccupied" { return "$($T.PortOccupied) (PID $otherPidText)" }
        "PortConflict" { return "$($T.Running) (PID $targetPidText); $($T.PortOccupied) (PID $otherPidText)" }
        default { return $T.Stopped }
    }
}

function Stop-ProjectService {
    param(
        [int]$Port,
        [ValidateSet("Harness", "LiteLLM", "ChromeProxy")]
        [string]$ServiceRole
    )
    $status = Get-PortServiceState $Port $ServiceRole
    $stoppedPids = @()
    $failedPids = @()
    foreach ($processInfo in $status.TargetProcesses) {
        $expectedIdentity = Get-ProcessInfoIdentity $processInfo
        $currentInfo = Get-VerifiedProjectServiceProcess `
            -Port $Port `
            -ServiceRole $ServiceRole `
            -ExpectedIdentity $expectedIdentity
        if (-not $currentInfo) {
            $failedPids += [int]$processInfo.ProcessId
            continue
        }
        if ($ServiceRole -eq "ChromeProxy") {
            if (Invoke-ChromeProxyGracefulShutdown -Port $Port -ExpectedIdentity $expectedIdentity) {
                $stoppedPids += [int]$currentInfo.ProcessId
            } else {
                $failedPids += [int]$currentInfo.ProcessId
            }
            continue
        }
        $process = Get-Process -Id $currentInfo.ProcessId -ErrorAction SilentlyContinue
        if (-not (Test-ProcessObjectMatchesIdentity $process $expectedIdentity)) {
            $failedPids += [int]$currentInfo.ProcessId
            continue
        }
        Stop-Process -InputObject $process -Force -ErrorAction Stop
        $stoppedPids += [int]$currentInfo.ProcessId
    }
    return [PSCustomObject]@{
        State = $status.State
        StoppedPids = @($stoppedPids)
        FailedPids = @($failedPids)
        OtherPids = @($status.OtherPids)
    }
}

function Complete-StartupMonitoring {
    param([switch]$TerminateSupervisor)

    if ($script:StartupProcess) {
        $supervisor = $script:StartupProcess
        try {
            $supervisorExited = $false
            try {
                $supervisor.Refresh()
                $supervisorExited = $supervisor.HasExited
            } catch {
                $supervisorExited = $false
            }
            if ($TerminateSupervisor -and -not $supervisorExited) {
                Stop-Process -InputObject $supervisor -Force -ErrorAction SilentlyContinue
                try {
                    [void]$supervisor.WaitForExit(2000)
                } catch {
                }
            }
        } finally {
            $supervisor.Dispose()
            $script:StartupProcess = $null
        }
    }
    if ($script:StartButton) {
        $script:StartButton.Enabled = $true
    }
    if (-not $script:ReadinessProbeAsyncResult -and $script:StartupTimer) {
        $script:StartupTimer.Stop()
    }
}

function Get-NormalizedProjectRoot {
    param([string]$ProjectRoot)
    if (-not $ProjectRoot) {
        throw "Project root is required for the launcher lease."
    }
    $normalized = [System.IO.Path]::GetFullPath($ProjectRoot).TrimEnd([char[]]"\/")
    if (-not $normalized) {
        $normalized = [System.IO.Path]::GetFullPath($ProjectRoot)
    }
    return $normalized.ToUpperInvariant()
}

function Get-LauncherMutexName {
    param([string]$ProjectRoot)
    $normalized = Get-NormalizedProjectRoot -ProjectRoot $ProjectRoot
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($normalized)
        $hash = $sha256.ComputeHash($bytes)
        $hex = -join ($hash | ForEach-Object { $_.ToString("x2") })
        return "Local\TeamAgentHarnessLauncher-$hex"
    } finally {
        $sha256.Dispose()
    }
}

function Acquire-LauncherLease {
    param([string]$ProjectRoot = $Root)
    $name = Get-LauncherMutexName -ProjectRoot $ProjectRoot
    $lease = [System.Threading.Mutex]::new($false, $name)
    $acquired = $false
    try {
        try {
            $acquired = $lease.WaitOne(0)
        } catch [System.Threading.AbandonedMutexException] {
            $acquired = $true
        }
        if ($acquired) {
            return $lease
        }
    } catch {
        $lease.Dispose()
        throw
    }
    $lease.Dispose()
    return $null
}

function Release-LauncherLease {
    param([System.Threading.Mutex]$Lease)
    if (-not $Lease) {
        return
    }
    try {
        $Lease.ReleaseMutex()
    } catch [System.ApplicationException] {
    } finally {
        $Lease.Dispose()
    }
}

function Open-HarnessUi {
    param(
        [switch]$Automatic,
        [switch]$ReadinessVerified
    )
    $script:LastOpenUiError = ""
    $script:LastUiReady = if ($ReadinessVerified) {
        $true
    } else {
        Test-HarnessUiReady
    }
    if (-not $script:LastUiReady) {
        if ($script:OpenUiButton) {
            $script:OpenUiButton.Enabled = $false
        }
        return $false
    }
    if ($script:OpenUiButton) {
        $script:OpenUiButton.Enabled = $true
    }
    if ($Automatic -and $script:StartupUiOpened) {
        return $true
    }
    try {
        Start-Process -FilePath $HarnessUrl | Out-Null
    } catch {
        $script:LastOpenUiError = Protect-LauncherText `
            -Text $_.Exception.Message `
            -SensitiveValues (Get-SensitiveConfigValues)
        return $false
    }
    if ($Automatic) {
        $script:StartupUiOpened = $true
    }
    return $true
}

if ($FunctionsOnly) {
    return
}

$script:LauncherLease = Acquire-LauncherLease -ProjectRoot $Root
if (-not $script:LauncherLease) {
    return
}

function Ensure-EnvFile {
    if (Test-Path $EnvFile) {
        return
    }
    if (Test-Path $EnvExampleFile) {
        Copy-Item -LiteralPath $EnvExampleFile -Destination $EnvFile
        return
    }
    Save-EnvFile -LiteLlmApiKey "sk-dev-local-key" -OpenAiApiKey "" -OpenAiApiBase "" -DeepSeekApiKey ""
}

$font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$form = New-Object System.Windows.Forms.Form
$form.Text = $T.Title
$form.Size = New-Object System.Drawing.Size -ArgumentList 760, 600
$form.StartPosition = "CenterScreen"
$form.Font = $font
$form.MinimumSize = New-Object System.Drawing.Size -ArgumentList 720, 540

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Location = New-Object System.Drawing.Point -ArgumentList 18, 18
$statusLabel.Size = New-Object System.Drawing.Size -ArgumentList 700, 24
$form.Controls.Add($statusLabel)

$labels = @(
    @("LiteLLM API Key", $T.LiteLlmKey, 62),
    @("OpenAI Relay Key", $T.OpenAiKey, 112),
    @("OpenAI Base URL", $T.OpenAiBase, 162),
    @("DeepSeek API Key", $T.DeepSeekKey, 212)
)

$textBoxes = @{}
foreach ($entry in $labels) {
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $entry[1]
    $label.Location = New-Object System.Drawing.Point -ArgumentList 20, ([int]$entry[2])
    $label.Size = New-Object System.Drawing.Size -ArgumentList 150, 24
    $form.Controls.Add($label)

    $box = New-Object System.Windows.Forms.TextBox
    $box.Location = New-Object System.Drawing.Point -ArgumentList 180, ([int]$entry[2] - 4)
    $box.Size = New-Object System.Drawing.Size -ArgumentList 520, 28
    $box.Anchor = "Top,Left,Right"
    if ($entry[0] -like "*Key") {
        $box.UseSystemPasswordChar = $true
    }
    $form.Controls.Add($box)
    $textBoxes[$entry[0]] = $box
}

$showKeys = New-Object System.Windows.Forms.CheckBox
$showKeys.Text = $T.ShowKeys
$showKeys.Location = New-Object System.Drawing.Point -ArgumentList 180, 250
$showKeys.Size = New-Object System.Drawing.Size -ArgumentList 120, 24
$showKeys.Add_CheckedChanged({
    $visible = -not $showKeys.Checked
    $textBoxes["LiteLLM API Key"].UseSystemPasswordChar = $visible
    $textBoxes["OpenAI Relay Key"].UseSystemPasswordChar = $visible
    $textBoxes["DeepSeek API Key"].UseSystemPasswordChar = $visible
})
$form.Controls.Add($showKeys)

$hintLabel = New-Object System.Windows.Forms.Label
$hintLabel.Text = $T.Hint
$hintLabel.Location = New-Object System.Drawing.Point -ArgumentList 310, 250
$hintLabel.Size = New-Object System.Drawing.Size -ArgumentList 390, 42
$hintLabel.Anchor = "Top,Left,Right"
$form.Controls.Add($hintLabel)

$logBox = New-Object System.Windows.Forms.TextBox
$logBox.Location = New-Object System.Drawing.Point -ArgumentList 20, 380
$logBox.Size = New-Object System.Drawing.Size -ArgumentList 700, 150
$logBox.Anchor = "Left,Right,Top,Bottom"
$logBox.Multiline = $true
$logBox.ScrollBars = "Vertical"
$logBox.ReadOnly = $true
$form.Controls.Add($logBox)

function Append-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "HH:mm:ss"
    $logBox.AppendText("[$timestamp] $Message`r`n")
}

function Refresh-Status {
    if ($script:OpenUiButton) {
        $script:OpenUiButton.Enabled = $script:LastUiReady
    }
    $statusLabel.Text = "LiteLLM 4000: $(Port-Status 4000 LiteLLM)    Harness 8014: $(Port-Status 8014 Harness)"
    if (Start-HarnessUiProbe) {
        if ($script:StartupTimer -and -not $script:StartupTimer.Enabled) {
            $script:StartupTimer.Start()
        }
    }
}

function Get-SensitiveConfigValues {
    return @(
        $textBoxes["LiteLLM API Key"].Text,
        $textBoxes["OpenAI Relay Key"].Text,
        $textBoxes["OpenAI Base URL"].Text,
        $textBoxes["DeepSeek API Key"].Text
    ) | Where-Object { $_ }
}

function Read-LauncherLogTail {
    param(
        [string]$Path,
        [int]$MaxBytes = 16384
    )
    $stream = [System.IO.File]::Open(
        $Path,
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::ReadWrite
    )
    try {
        $start = [Math]::Max(0, $stream.Length - $MaxBytes)
        [void]$stream.Seek($start, [System.IO.SeekOrigin]::Begin)
        $length = [int]($stream.Length - $start)
        $buffer = New-Object byte[] $length
        $read = 0
        while ($read -lt $length) {
            $count = $stream.Read($buffer, $read, $length - $read)
            if ($count -le 0) {
                break
            }
            $read += $count
        }
        $text = [System.Text.Encoding]::UTF8.GetString($buffer, 0, $read)
        if ($start -gt 0) {
            $firstLineBreak = $text.IndexOf("`n")
            if ($firstLineBreak -ge 0) {
                $text = $text.Substring($firstLineBreak + 1)
            }
        }
        return $text
    } finally {
        $stream.Dispose()
    }
}

function Get-StartupDiagnostics {
    $parts = @()
    foreach ($path in @($script:StartupErrorLogPath, $script:StartupLogPath)) {
        if (-not $path -or -not (Test-Path -LiteralPath $path)) {
            continue
        }
        try {
            $content = Read-LauncherLogTail -Path $path
            if ($content) {
                $parts += $content
            }
        } catch {
            continue
        }
    }
    if (-not $parts.Count) {
        return "No startup diagnostics were written."
    }

    $diagnostics = $parts -join "`r`n"
    if ($diagnostics.Length -gt 5000) {
        $diagnostics = $diagnostics.Substring($diagnostics.Length - 5000)
    }
    $diagnostics = Protect-LauncherText `
        -Text $diagnostics `
        -SensitiveValues (Get-SensitiveConfigValues)
    if ($Root) {
        $diagnostics = [regex]::Replace(
            $diagnostics,
            [regex]::Escape($Root),
            "%PROJECT_ROOT%",
            [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
    }
    return $diagnostics.Trim()
}

function Update-StartupState {
    $probeCompleted = Complete-HarnessUiProbe
    if ($probeCompleted -and $script:OpenUiButton) {
        $script:OpenUiButton.Enabled = $script:LastUiReady
    }
    if (-not $script:StartupProcess) {
        if (-not $script:ReadinessProbeAsyncResult -and $script:StartupTimer) {
            $script:StartupTimer.Stop()
        }
        return
    }

    $processExited = $false
    $exitCode = 0
    try {
        $script:StartupProcess.Refresh()
        $processExited = $script:StartupProcess.HasExited
        if ($processExited) {
            $exitCode = $script:StartupProcess.ExitCode
        }
    } catch {
        $processExited = $true
        $exitCode = -1
    }

    $nowUtc = [DateTime]::UtcNow
    if (
        $processExited `
        -and -not $script:LastUiReady `
        -and $nowUtc -lt $script:StartupDeadlineUtc
    ) {
        if ($script:ReadinessProbeAsyncResult) {
            return
        }
        if (-not $script:StartupFinalProbeRequested) {
            $script:StartupFinalProbeRequested = $true
            [void](Start-HarnessUiProbe)
            return
        }
    }

    $observation = Get-StartupObservation `
        -UiReady $script:LastUiReady `
        -ProcessExited $processExited `
        -ExitCode $exitCode `
        -DeadlineUtc $script:StartupDeadlineUtc `
        -NowUtc $nowUtc

    switch ($observation) {
        "Starting" {
            $statusLabel.Text = $T.Starting
            [void](Start-HarnessUiProbe)
        }
        "Ready" {
            if (-not $script:StartupReadyReported) {
                if (Open-HarnessUi -Automatic -ReadinessVerified) {
                    Append-Log $T.ReadyOpened
                    $script:StartupReadyReported = $true
                } else {
                    Append-Log "$($T.OpenFailed): $script:LastOpenUiError"
                }
            }
            $statusLabel.Text = $T.Ready
            if ($processExited) {
                Complete-StartupMonitoring
            } else {
                [void](Start-HarnessUiProbe)
            }
        }
        "ReadyWithWarnings" {
            if (-not (Open-HarnessUi -Automatic -ReadinessVerified) -and $script:LastOpenUiError) {
                Append-Log "$($T.OpenFailed): $script:LastOpenUiError"
            }
            $statusLabel.Text = $T.ReadyWarnings
            if ($processExited) {
                Append-Log "$($T.ReadyWarnings) (exit code $exitCode)."
            } else {
                Append-Log "$($T.ReadyWarnings) (startup support deadline expired)."
            }
            Append-Log (Get-StartupDiagnostics)
            Complete-StartupMonitoring -TerminateSupervisor:(-not $processExited)
        }
        "Failed" {
            if ($script:OpenUiButton) {
                $script:OpenUiButton.Enabled = $false
            }
            $statusLabel.Text = $T.StartupFailed
            Append-Log "$($T.StartupFailed) (exit code $exitCode)."
            Append-Log (Get-StartupDiagnostics)
            Complete-StartupMonitoring
        }
        "TimedOut" {
            $statusLabel.Text = $T.StartupTimedOut
            Append-Log $T.StartupTimedOut
            Append-Log (Get-StartupDiagnostics)
            Complete-StartupMonitoring -TerminateSupervisor
        }
    }
}

function Stop-StartupMonitoring {
    $script:LastUiReady = $false
    Complete-StartupMonitoring -TerminateSupervisor
}

function Load-Config {
    Ensure-EnvFile
    $values = Read-EnvFile $EnvFile
    $textBoxes["LiteLLM API Key"].Text = [string]$values["LITELLM_API_KEY"]
    $textBoxes["OpenAI Relay Key"].Text = [string]$values["OPENAI_API_KEY"]
    $textBoxes["OpenAI Base URL"].Text = [string]$values["OPENAI_API_BASE"]
    $textBoxes["DeepSeek API Key"].Text = [string]$values["DEEPSEEK_API_KEY"]
    Append-Log "$($T.LoadedConfig): $EnvFile"
}

function Button {
    param(
        [string]$Text,
        [int]$X,
        [int]$Y,
        [scriptblock]$OnClick
    )
    $button = New-Object System.Windows.Forms.Button
    $button.Text = $Text
    $button.Location = New-Object System.Drawing.Point -ArgumentList $X, $Y
    $button.Size = New-Object System.Drawing.Size -ArgumentList 150, 34
    $button.Add_Click($OnClick)
    $form.Controls.Add($button)
    return $button
}

$script:StartupProcess = $null
$script:StartupDeadlineUtc = [DateTime]::MinValue
$script:StartupLogPath = ""
$script:StartupErrorLogPath = ""
$script:StartupUiOpened = $false
$script:StartupReadyReported = $false
$script:StartupFinalProbeRequested = $false
$script:LastOpenUiError = ""
$script:StartButton = $null
$script:OpenUiButton = $null
$script:StartupTimer = New-Object System.Windows.Forms.Timer
$script:StartupTimer.Interval = 500
$script:StartupTimer.Add_Tick({ Update-StartupState })

Button $T.SaveConfig 20 292 {
    try {
        Save-EnvFile `
            -LiteLlmApiKey $textBoxes["LiteLLM API Key"].Text.Trim() `
            -OpenAiApiKey $textBoxes["OpenAI Relay Key"].Text.Trim() `
            -OpenAiApiBase $textBoxes["OpenAI Base URL"].Text.Trim() `
            -DeepSeekApiKey $textBoxes["DeepSeek API Key"].Text.Trim()
        Append-Log $T.SavedConfig
    } catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, $T.SaveFailed, "OK", "Error") | Out-Null
    }
} | Out-Null

$script:StartButton = Button $T.StartServices 180 292 {
    try {
        if ($script:StartupProcess) {
            $script:StartupProcess.Refresh()
            if (-not $script:StartupProcess.HasExited) {
                Append-Log $T.StartAlreadyRunning
                return
            }
            Complete-StartupMonitoring
        }
        Stop-HarnessUiProbe
        $script:LastUiReady = $false
        if ($script:OpenUiButton) {
            $script:OpenUiButton.Enabled = $false
        }
        Save-EnvFile `
            -LiteLlmApiKey $textBoxes["LiteLLM API Key"].Text.Trim() `
            -OpenAiApiKey $textBoxes["OpenAI Relay Key"].Text.Trim() `
            -OpenAiApiBase $textBoxes["OpenAI Base URL"].Text.Trim() `
            -DeepSeekApiKey $textBoxes["DeepSeek API Key"].Text.Trim()

        New-Item -ItemType Directory -Force $OutputDir | Out-Null
        $startupId = Get-Date -Format "yyyyMMdd-HHmmss-fff"
        $script:StartupLogPath = Join-Path $OutputDir "startup-supervisor-$startupId.log"
        $script:StartupErrorLogPath = Join-Path $OutputDir "startup-supervisor-$startupId.err.log"
        $argumentLine = '-NoProfile -ExecutionPolicy Bypass -File "{0}" -LiteLlmPython "{1}"' -f `
            $StartScript, $LiteLlmPython
        $script:StartupProcess = Start-Process `
            -FilePath "powershell.exe" `
            -ArgumentList $argumentLine `
            -WorkingDirectory $Root `
            -WindowStyle Hidden `
            -RedirectStandardOutput $script:StartupLogPath `
            -RedirectStandardError $script:StartupErrorLogPath `
            -PassThru
        $script:StartupDeadlineUtc = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
        $script:StartupUiOpened = $false
        $script:StartupReadyReported = $false
        $script:StartupFinalProbeRequested = $false
        $script:LastUiReady = $false
        $script:StartButton.Enabled = $false
        $script:OpenUiButton.Enabled = $false
        $statusLabel.Text = $T.Starting
        Append-Log $T.StartSent
        Append-Log "$($T.SupervisorLogs): %PROJECT_ROOT%\output\$(Split-Path -Leaf $script:StartupLogPath)"
        $script:StartupTimer.Start()
        Update-StartupState
    } catch {
        $safeMessage = Protect-LauncherText `
            -Text $_.Exception.Message `
            -SensitiveValues (Get-SensitiveConfigValues)
        Append-Log "$($T.StartFailed): $safeMessage"
        if ($script:StartButton) {
            $script:StartButton.Enabled = $true
        }
        [System.Windows.Forms.MessageBox]::Show($safeMessage, $T.StartFailed, "OK", "Error") | Out-Null
    }
}

Button $T.StopServices 340 292 {
    Stop-StartupMonitoring
    $stopResults = @(
        Stop-ProjectService 8014 Harness
        Stop-ProjectService $ChromeProxyPort ChromeProxy
        Stop-ProjectService 4000 LiteLLM
    )
    $failedPids = @($stopResults | ForEach-Object { $_.FailedPids } | Where-Object { $_ } | Sort-Object -Unique)
    if ($failedPids.Count) {
        Append-Log "$($T.StopIncomplete) (PID $($failedPids -join ','))"
    } else {
        Append-Log $T.StoppedLog
    }
    Refresh-Status
} | Out-Null

Button $T.Refresh 500 292 {
    Refresh-Status
    Append-Log $T.StatusRefreshed
} | Out-Null

$script:OpenUiButton = Button $T.OpenUi 20 336 {
    if (-not (Open-HarnessUi)) {
        if ($script:LastOpenUiError) {
            Append-Log "$($T.OpenFailed): $script:LastOpenUiError"
            [System.Windows.Forms.MessageBox]::Show(
                $script:LastOpenUiError,
                $T.OpenFailed,
                "OK",
                "Error"
            ) | Out-Null
        } else {
            Append-Log $T.UiUnavailable
            $statusLabel.Text = $T.UiUnavailable
        }
    }
}
$script:OpenUiButton.Enabled = $false

Button $T.OpenProject 180 336 {
    Start-Process explorer.exe $Root
} | Out-Null

Button $T.OpenConfig 340 336 {
    Ensure-EnvFile
    Start-Process notepad.exe $EnvFile
} | Out-Null

Button $T.OpenLogs 500 336 {
    New-Item -ItemType Directory -Force $OutputDir | Out-Null
    Start-Process explorer.exe $OutputDir
} | Out-Null

$form.Add_Shown({
    Load-Config
    Refresh-Status
})

$form.Add_FormClosed({
    Complete-StartupMonitoring -TerminateSupervisor
    Stop-HarnessUiProbe
})

try {
    [void]$form.ShowDialog()
} finally {
    if ($script:StartupTimer) {
        $script:StartupTimer.Stop()
        $script:StartupTimer.Dispose()
    }
    Stop-HarnessUiProbe
    Release-LauncherLease -Lease $script:LauncherLease
    $script:LauncherLease = $null
}
