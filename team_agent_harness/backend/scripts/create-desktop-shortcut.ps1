param(
    [string]$DesktopPath = ""
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$SetupScript = Join-Path $PSScriptRoot "setup-desktop.ps1"
$Desktop = if ($DesktopPath) { $DesktopPath } else { [Environment]::GetFolderPath("Desktop") }
$ShortcutPath = Join-Path $Desktop "Team Agent Harness Launcher.lnk"

if (-not (Test-Path -LiteralPath $SetupScript)) {
    throw "Setup script not found: $SetupScript"
}
if (-not (Test-Path -LiteralPath $Desktop)) {
    throw "Desktop directory not found: $Desktop"
}

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($ShortcutPath)
$shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcut.Arguments = "-NoProfile -STA -ExecutionPolicy Bypass -File `"$SetupScript`""
$shortcut.WorkingDirectory = $Root
$shortcut.IconLocation = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe,0"
$shortcut.Description = "Set up and launch Team Agent Harness"
$shortcut.Save()

Write-Host "Created desktop shortcut: $ShortcutPath"
