$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Launcher = Join-Path $PSScriptRoot "harness-launcher.ps1"
$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "Team Agent Harness Launcher.lnk"

if (-not (Test-Path $Launcher)) {
    throw "Launcher not found: $Launcher"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcut.Arguments = "-NoProfile -STA -ExecutionPolicy Bypass -File `"$Launcher`""
$shortcut.WorkingDirectory = $Root
$shortcut.IconLocation = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe,0"
$shortcut.Description = "Team Agent Harness local launcher"
$shortcut.Save()

Write-Host "Created desktop shortcut: $ShortcutPath"
