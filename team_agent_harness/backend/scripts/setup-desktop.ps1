param(
    [switch]$FunctionsOnly,
    [switch]$SkipShortcut,
    [switch]$SkipLaunch,
    [switch]$Repair,
    [switch]$NonInteractive,
    [string]$PythonPath = "",
    [string]$DesktopPath = ""
)

$ErrorActionPreference = "Stop"

$BackendRoot = Split-Path -Parent $PSScriptRoot
$RepositoryRoot = Split-Path -Parent (Split-Path -Parent $BackendRoot)
$ProjectFile = Join-Path $BackendRoot "pyproject.toml"
$HarnessVenv = Join-Path $BackendRoot ".venv"
$LiteLlmVenv = Join-Path $BackendRoot ".venv-litellm"
$Launcher = Join-Path $PSScriptRoot "harness-launcher.ps1"
$ShortcutScript = Join-Path $PSScriptRoot "create-desktop-shortcut.ps1"
$SetupSchemaVersion = 1
$LiteLlmRequirement = "litellm[proxy]==1.89.2"
$MainProbe = "import fastapi, openai, pydantic, uvicorn, websockets"
$LiteLlmProbe = "import fastapi, litellm, uvicorn"

function Test-SupportedBootstrapPythonVersion {
    param(
        [int]$Major,
        [int]$Minor
    )
    return $Major -eq 3 -and $Minor -in @(12, 13)
}

function Get-PythonDetails {
    param(
        [string]$Executable,
        [string[]]$PrefixArguments = @()
    )
    try {
        $json = & $Executable @PrefixArguments -c "import json,sys; print(json.dumps({'major':sys.version_info.major,'minor':sys.version_info.minor,'micro':sys.version_info.micro,'executable':sys.executable}))" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $json) {
            return $null
        }
        $details = $json | Select-Object -Last 1 | ConvertFrom-Json
        if (-not (Test-SupportedBootstrapPythonVersion -Major $details.major -Minor $details.minor)) {
            return $null
        }
        return [PSCustomObject]@{
            Executable = $Executable
            PrefixArguments = @($PrefixArguments)
            Version = "$($details.major).$($details.minor).$($details.micro)"
            ResolvedExecutable = [string]$details.executable
        }
    } catch {
        return $null
    }
}

function Resolve-CompatiblePython {
    param([string]$RequestedPython = "")

    if ($RequestedPython) {
        $requested = Get-PythonDetails -Executable $RequestedPython
        if (-not $requested) {
            throw "-PythonPath must point to Python 3.12 or 3.13: $RequestedPython"
        }
        return $requested
    }

    $candidates = @()
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $candidates += ,@($pyLauncher.Source, @("-3.13"))
    }
    $python313 = Get-Command "python3.13.exe" -ErrorAction SilentlyContinue
    if ($python313) {
        $candidates += ,@($python313.Source, @())
    }
    if ($env:LOCALAPPDATA) {
        $candidates += ,@((Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"), @())
    }
    if ($pyLauncher) {
        $candidates += ,@($pyLauncher.Source, @("-3.12"))
    }
    $python312 = Get-Command "python3.12.exe" -ErrorAction SilentlyContinue
    if ($python312) {
        $candidates += ,@($python312.Source, @())
    }
    if ($env:LOCALAPPDATA) {
        $candidates += ,@((Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"), @())
    }
    $defaultPython = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($defaultPython) {
        $candidates += ,@($defaultPython.Source, @())
    }

    foreach ($candidate in $candidates) {
        $details = Get-PythonDetails -Executable $candidate[0] -PrefixArguments $candidate[1]
        if ($details) {
            return $details
        }
    }
    return $null
}

function Install-CompatiblePythonWithWinget {
    if ($NonInteractive) {
        throw "Python 3.12 or 3.13 is required. Install Python 3.13, then run setup again."
    }
    $winget = Get-Command "winget.exe" -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Python 3.12 or 3.13 is required. Install Python 3.13 from python.org, then run setup again."
    }

    Add-Type -AssemblyName System.Windows.Forms
    $message = @"
Team Agent Harness needs Python 3.12 or 3.13.

Install Python 3.13 for the current Windows user with winget now?
"@
    $choice = [System.Windows.Forms.MessageBox]::Show(
        $message,
        "Team Agent Harness setup",
        [System.Windows.Forms.MessageBoxButtons]::YesNo,
        [System.Windows.Forms.MessageBoxIcon]::Question
    )
    if ($choice -ne [System.Windows.Forms.DialogResult]::Yes) {
        throw "Python installation was cancelled. Install Python 3.13, then run setup again."
    }

    Write-Host "Installing Python 3.13 for the current Windows user..."
    $process = Start-Process -FilePath $winget.Source -ArgumentList @(
        "install",
        "--exact",
        "--id", "Python.Python.3.13",
        "--scope", "user",
        "--accept-package-agreements",
        "--accept-source-agreements"
    ) -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "winget could not install Python 3.13 (exit code $($process.ExitCode))."
    }

    $python = Resolve-CompatiblePython
    if (-not $python) {
        throw "Python 3.13 was installed but is not visible yet. Sign out of Windows or restart, then run setup again."
    }
    return $python
}

function Invoke-CheckedCommand {
    param(
        [string]$Executable,
        [string[]]$Arguments,
        [string]$FailureMessage
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)."
    }
}

function Get-PythonVersion {
    param([string]$PythonExe)
    try {
        $version = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        return [version]($version | Select-Object -Last 1)
    } catch {
        return $null
    }
}

function Get-DependencyHash {
    param([string]$DependencyLabel)
    $projectHash = (Get-FileHash -LiteralPath $ProjectFile -Algorithm SHA256).Hash
    $bytes = [System.Text.Encoding]::UTF8.GetBytes("$SetupSchemaVersion|$projectHash|$DependencyLabel")
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "")
    } finally {
        $sha.Dispose()
    }
}

function Write-SetupState {
    param(
        [string]$StatePath,
        [string]$DependencyHash,
        [string]$PythonExe
    )
    $state = [ordered]@{
        schema_version = $SetupSchemaVersion
        dependency_hash = $DependencyHash
        python = [string](Get-PythonVersion -PythonExe $PythonExe)
    }
    $temporaryPath = "$StatePath.tmp"
    $state | ConvertTo-Json | Set-Content -LiteralPath $temporaryPath -Encoding UTF8
    Move-Item -LiteralPath $temporaryPath -Destination $StatePath -Force
}

function Test-EnvironmentReady {
    param(
        [string]$PythonExe,
        [string]$StatePath,
        [string]$DependencyHash,
        [string]$ProbeCode,
        [switch]$RequireLiteLlmCompatibleVersion
    )
    if (-not (Test-Path -LiteralPath $PythonExe) -or -not (Test-Path -LiteralPath $StatePath)) {
        return $false
    }
    try {
        $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
        if ($state.schema_version -ne $SetupSchemaVersion -or $state.dependency_hash -ne $DependencyHash) {
            return $false
        }
        $version = Get-PythonVersion -PythonExe $PythonExe
        if (-not $version -or $version.Major -ne 3 -or $version.Minor -lt 12) {
            return $false
        }
        if ($RequireLiteLlmCompatibleVersion -and $version.Minor -ge 14) {
            return $false
        }
        & $PythonExe -c $ProbeCode 2>$null
        if ($LASTEXITCODE -ne 0) {
            return $false
        }
        & $PythonExe -m pip check 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Assert-RepairPath {
    param([string]$Path)
    $resolvedBackend = [System.IO.Path]::GetFullPath($BackendRoot).TrimEnd('\')
    $resolvedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    if (-not $resolvedPath.StartsWith("$resolvedBackend\", [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to repair a path outside the backend directory: $resolvedPath"
    }
    if ((Split-Path -Leaf $resolvedPath) -notin @(".venv", ".venv-litellm")) {
        throw "Refusing to repair an unexpected directory: $resolvedPath"
    }
    $item = Get-Item -LiteralPath $resolvedPath -Force -ErrorAction Stop
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to repair a reparse point: $resolvedPath"
    }
}

function Ensure-VirtualEnvironment {
    param(
        [PSCustomObject]$BootstrapPython,
        [string]$VenvPath,
        [string]$DependencyHash,
        [string]$ProbeCode,
        [ValidateSet("main", "litellm")]
        [string]$Role
    )
    $venvPython = Join-Path $VenvPath "Scripts\python.exe"
    $statePath = Join-Path $VenvPath ".team-agent-harness-ready.json"
    $requireLiteLlmVersion = $Role -eq "litellm"

    if ($Repair -and (Test-Path -LiteralPath $VenvPath)) {
        Assert-RepairPath -Path $VenvPath
        Write-Host "Rebuilding $Role environment: $VenvPath"
        Remove-Item -LiteralPath $VenvPath -Recurse -Force
    }

    if (-not (Test-Path -LiteralPath $venvPython)) {
        if ((Test-Path -LiteralPath $VenvPath) -and -not $Repair) {
            throw "The $Role environment is incomplete: $VenvPath. Run setup-desktop.ps1 -Repair to rebuild it."
        }
        Write-Host "Creating $Role environment with Python $($BootstrapPython.Version)..."
        $venvArguments = @($BootstrapPython.PrefixArguments) + @("-m", "venv", $VenvPath)
        Invoke-CheckedCommand -Executable $BootstrapPython.Executable -Arguments $venvArguments -FailureMessage "Could not create the $Role environment"
    }

    $version = Get-PythonVersion -PythonExe $venvPython
    if (-not $version -or $version.Major -ne 3 -or $version.Minor -lt 12) {
        throw "The $Role environment must use Python 3.12 or newer: $VenvPath"
    }
    if ($requireLiteLlmVersion -and $version.Minor -ge 14) {
        throw "The LiteLLM environment must use Python 3.12 or 3.13. Run setup-desktop.ps1 -Repair to rebuild it."
    }

    $readyArguments = @{
        PythonExe = $venvPython
        StatePath = $statePath
        DependencyHash = $DependencyHash
        ProbeCode = $ProbeCode
        RequireLiteLlmCompatibleVersion = $requireLiteLlmVersion
    }
    if (Test-EnvironmentReady @readyArguments) {
        Write-Host "$Role environment is ready."
        return $venvPython
    }

    if ($Role -eq "main") {
        Write-Host "Installing Team Agent Harness runtime dependencies..."
        Invoke-CheckedCommand -Executable $venvPython -Arguments @(
            "-m", "pip", "install", "--disable-pip-version-check", "-e", $BackendRoot
        ) -FailureMessage "Could not install Team Agent Harness dependencies"
    } else {
        Write-Host "Installing LiteLLM Proxy dependencies..."
        Invoke-CheckedCommand -Executable $venvPython -Arguments @(
            "-m", "pip", "install", "--disable-pip-version-check", $LiteLlmRequirement
        ) -FailureMessage "Could not install LiteLLM Proxy dependencies"
    }

    & $venvPython -c $ProbeCode
    if ($LASTEXITCODE -ne 0) {
        throw "$Role environment dependency verification failed."
    }
    Invoke-CheckedCommand -Executable $venvPython -Arguments @(
        "-m", "pip", "check"
    ) -FailureMessage "$Role environment has incompatible dependencies"
    Write-SetupState -StatePath $statePath -DependencyHash $DependencyHash -PythonExe $venvPython
    Write-Host "$Role environment is ready."
    return $venvPython
}

function Open-HarnessLauncher {
    if (-not (Test-Path -LiteralPath $Launcher)) {
        throw "Launcher not found: $Launcher"
    }
    $powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $powershell)) {
        $powershell = "powershell.exe"
    }
    Start-Process -FilePath $powershell -ArgumentList @(
        "-NoProfile",
        "-STA",
        "-ExecutionPolicy", "Bypass",
        "-File", "`"$Launcher`""
    ) -WorkingDirectory $BackendRoot | Out-Null
}

if ($FunctionsOnly) {
    return
}

try {
    foreach ($requiredPath in @($ProjectFile, $Launcher, $ShortcutScript)) {
        if (-not (Test-Path -LiteralPath $requiredPath)) {
            throw "Required project file not found: $requiredPath"
        }
    }

    Write-Host "Preparing Team Agent Harness in: $RepositoryRoot"
    $mainHash = Get-DependencyHash -DependencyLabel "team-agent-harness-runtime"
    $liteLlmHash = Get-DependencyHash -DependencyLabel $LiteLlmRequirement
    $mainReady = -not $Repair -and (Test-EnvironmentReady `
        -PythonExe (Join-Path $HarnessVenv "Scripts\python.exe") `
        -StatePath (Join-Path $HarnessVenv ".team-agent-harness-ready.json") `
        -DependencyHash $mainHash `
        -ProbeCode $MainProbe)
    $liteLlmReady = -not $Repair -and (Test-EnvironmentReady `
        -PythonExe (Join-Path $LiteLlmVenv "Scripts\python.exe") `
        -StatePath (Join-Path $LiteLlmVenv ".team-agent-harness-ready.json") `
        -DependencyHash $liteLlmHash `
        -ProbeCode $LiteLlmProbe `
        -RequireLiteLlmCompatibleVersion)

    if ($mainReady -and $liteLlmReady) {
        Write-Host "Project-local environments are ready."
    } else {
        $bootstrapPython = Resolve-CompatiblePython -RequestedPython $PythonPath
        if (-not $bootstrapPython) {
            $bootstrapPython = Install-CompatiblePythonWithWinget
        }
        Write-Host "Using Python $($bootstrapPython.Version): $($bootstrapPython.ResolvedExecutable)"
        Ensure-VirtualEnvironment -BootstrapPython $bootstrapPython -VenvPath $HarnessVenv -DependencyHash $mainHash -ProbeCode $MainProbe -Role main | Out-Null
        Ensure-VirtualEnvironment -BootstrapPython $bootstrapPython -VenvPath $LiteLlmVenv -DependencyHash $liteLlmHash -ProbeCode $LiteLlmProbe -Role litellm | Out-Null
    }

    if (-not $SkipShortcut) {
        $shortcutArguments = @()
        if ($DesktopPath) {
            $shortcutArguments += @("-DesktopPath", $DesktopPath)
        }
        & $ShortcutScript @shortcutArguments
    }

    Write-Host "Team Agent Harness setup is complete."
    if (-not $SkipLaunch) {
        Open-HarnessLauncher
    }
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
