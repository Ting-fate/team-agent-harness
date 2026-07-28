param(
    [switch]$FunctionsOnly
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env.local"
$EnvExampleFile = Join-Path $Root ".env.local.example"
$StartScript = Join-Path $PSScriptRoot "start-litellm-harness.ps1"
$HarnessPython = Join-Path $Root ".venv\Scripts\python.exe"
$LiteLlmPython = Join-Path $Root ".venv-litellm\Scripts\python.exe"
$LiteLlmRunner = Join-Path $PSScriptRoot "run_litellm_proxy.py"
$OutputDir = Join-Path $Root "output"
$HarnessUrl = "http://127.0.0.1:8014/"
$LiteLlmUrl = "http://127.0.0.1:4000/"

function Get-PythonBaseExecutable {
    param([string]$PythonPath)
    if (-not (Test-Path -LiteralPath $PythonPath)) {
        return ""
    }
    try {
        $venvRoot = Split-Path -Parent (Split-Path -Parent $PythonPath)
        $venvConfig = Join-Path $venvRoot "pyvenv.cfg"
        foreach ($line in Get-Content -LiteralPath $venvConfig -ErrorAction Stop) {
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
    StartSent = ZH 24050,21457,36865,21551,21160,21629,20196,65292,20960,31186,21518,28857,21047,26032,29366,24577
    StoppedLog = ZH 24050,20572,27490,26412,39033,30446,26381,21153,65307,20854,20182,31471,21475,36827,31243,26410,22788,29702
    StatusRefreshed = ZH 29366,24577,24050,21047,26032
    LiteLlmInvalid = ZH 76,105,116,101,76,76,77,32,21475,20196,38656,35201,20197,32,115,107,45,32,24320,22836,65292,20363,22914,32,115,107,45,100,101,118,45,108,111,99,97,108,45,107,101,121
    OpenAiKeyRequired = ZH 79,112,101,110,65,73,32,20013,36716,31449,32,75,101,121,32,19981,33021,20026,31354
    OpenAiBaseInvalid = ZH 79,112,101,110,65,73,32,20013,36716,31449,22320,22336,38656,35201,26159,32,104,116,116,112,40,115,41,32,22320,22336,65292,36890,24120,20197,32,47,118,49,32,32467,23614
    DeepSeekKeyRequired = ZH 68,101,101,112,83,101,101,107,32,75,101,121,32,19981,33021,20026,31354
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

function Save-EnvFile {
    param(
        [string]$LiteLlmApiKey,
        [string]$OpenAiApiKey,
        [string]$OpenAiApiBase,
        [string]$DeepSeekApiKey
    )
    $content = @(
        "# Local Team Agent Harness credentials. Do not commit this file.",
        "LITELLM_API_KEY=$LiteLlmApiKey",
        "OPENAI_API_KEY=$OpenAiApiKey",
        "OPENAI_API_BASE=$OpenAiApiBase",
        "DEEPSEEK_API_KEY=$DeepSeekApiKey"
    )
    Set-Content -LiteralPath $EnvFile -Value $content -Encoding UTF8
}

function Test-SameExecutablePath {
    param(
        [string]$ActualPath,
        [string]$ExpectedPath
    )
    if (-not $ActualPath -or -not $ExpectedPath) {
        return $false
    }
    try {
        $actual = [System.IO.Path]::GetFullPath($ActualPath)
        $expected = [System.IO.Path]::GetFullPath($ExpectedPath)
    } catch {
        return $false
    }
    return [string]::Equals($actual, $expected, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-ProjectServiceProcess {
    param(
        [object]$ProcessInfo,
        [ValidateSet("Harness", "LiteLLM")]
        [string]$ServiceRole,
        [int]$Port
    )
    if (-not $ProcessInfo -or -not $ProcessInfo.CommandLine) {
        return $false
    }

    $expectedCommandExecutable = if ($ServiceRole -eq "Harness") { $HarnessPython } else { $LiteLlmPython }
    $expectedProcessExecutables = if ($ServiceRole -eq "Harness") {
        @($HarnessPython, $HarnessBasePython)
    } else {
        @($LiteLlmPython, $LiteLlmBasePython)
    }
    $actualExecutableMatches = @($expectedProcessExecutables | Where-Object {
        $_ -and (Test-SameExecutablePath $ProcessInfo.ExecutablePath $_)
    }).Count -gt 0
    if (-not $actualExecutableMatches) {
        return $false
    }

    $commandLine = [string]$ProcessInfo.CommandLine
    $commandExecutablePattern = '^(?i)\s*"?{0}"?(?:\s|$)' -f [regex]::Escape(
        [System.IO.Path]::GetFullPath($expectedCommandExecutable)
    )
    if ($commandLine -notmatch $commandExecutablePattern) {
        return $false
    }
    $portPattern = "(?i)(?:^|\s)--port(?:\s+|=)$Port(?:\s|$)"
    if ($commandLine -notmatch $portPattern) {
        return $false
    }
    if ($ServiceRole -eq "Harness") {
        return $commandLine -match "(?i)(?:^|\s)-m\s+uvicorn\s+app\.main:app(?:\s|$)"
    }

    $runnerPattern = [regex]::Escape([System.IO.Path]::GetFullPath($LiteLlmRunner))
    $commandPattern = '(?i)(?:^|\s|")"?{0}"?(?:\s|$)' -f $runnerPattern
    return $commandLine -match $commandPattern
}

function Get-PortServiceState {
    param(
        [int]$Port,
        [ValidateSet("Harness", "LiteLLM")]
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
        [ValidateSet("Harness", "LiteLLM")]
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
        [ValidateSet("Harness", "LiteLLM")]
        [string]$ServiceRole
    )
    $status = Get-PortServiceState $Port $ServiceRole
    $stoppedPids = @()
    foreach ($processInfo in $status.TargetProcesses) {
        $currentInfo = Get-CimInstance -ClassName Win32_Process -Filter "ProcessId = $($processInfo.ProcessId)" -ErrorAction SilentlyContinue
        if (-not (Test-ProjectServiceProcess $currentInfo $ServiceRole $Port)) {
            continue
        }
        $process = Get-Process -Id $currentInfo.ProcessId -ErrorAction SilentlyContinue
        if ($process) {
            Stop-Process -InputObject $process -Force -ErrorAction Stop
            $stoppedPids += [int]$currentInfo.ProcessId
        }
    }
    return [PSCustomObject]@{
        State = $status.State
        StoppedPids = @($stoppedPids)
        OtherPids = @($status.OtherPids)
    }
}

if ($FunctionsOnly) {
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
    $statusLabel.Text = "LiteLLM 4000: $(Port-Status 4000 LiteLLM)    Harness 8014: $(Port-Status 8014 Harness)"
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

function Validate-Config {
    if (-not $textBoxes["LiteLLM API Key"].Text.Trim().StartsWith("sk-")) {
        throw $T.LiteLlmInvalid
    }
    if (-not $textBoxes["OpenAI Relay Key"].Text.Trim()) {
        throw $T.OpenAiKeyRequired
    }
    if (-not $textBoxes["OpenAI Base URL"].Text.Trim().StartsWith("http")) {
        throw $T.OpenAiBaseInvalid
    }
    if (-not $textBoxes["DeepSeek API Key"].Text.Trim()) {
        throw $T.DeepSeekKeyRequired
    }
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

Button $T.SaveConfig 20 292 {
    try {
        Validate-Config
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

Button $T.StartServices 180 292 {
    try {
        Validate-Config
        Save-EnvFile `
            -LiteLlmApiKey $textBoxes["LiteLLM API Key"].Text.Trim() `
            -OpenAiApiKey $textBoxes["OpenAI Relay Key"].Text.Trim() `
            -OpenAiApiBase $textBoxes["OpenAI Base URL"].Text.Trim() `
            -DeepSeekApiKey $textBoxes["DeepSeek API Key"].Text.Trim()
        $args = @(
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", $StartScript,
            "-LiteLlmPython", $LiteLlmPython
        )
        Start-Process -FilePath "powershell.exe" -ArgumentList $args -WorkingDirectory $Root -WindowStyle Hidden
        Append-Log $T.StartSent
        Start-Sleep -Milliseconds 800
        Refresh-Status
    } catch {
        [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, $T.StartFailed, "OK", "Error") | Out-Null
    }
} | Out-Null

Button $T.StopServices 340 292 {
    Stop-ProjectService 8014 Harness | Out-Null
    Stop-ProjectService 4000 LiteLLM | Out-Null
    Append-Log $T.StoppedLog
    Refresh-Status
} | Out-Null

Button $T.Refresh 500 292 {
    Refresh-Status
    Append-Log $T.StatusRefreshed
} | Out-Null

Button $T.OpenUi 20 336 {
    Start-Process $HarnessUrl
} | Out-Null

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

[void]$form.ShowDialog()
