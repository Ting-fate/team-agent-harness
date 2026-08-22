param(
    [int]$LiteLlmPort = 4000,
    [int]$HarnessPort = 8014,
    [int]$BrowserProxyPort = 3456,
    [int]$ChromeDebugPort = 9223,
    [int]$ModelTimeoutSeconds = 180,
    [int]$LiteLlmMaxRetries = 0,
    [ValidateSet("direct", "litellm")]
    [string]$RouteMode = "",
    [string]$HarnessPython = "",
    [string]$LiteLlmPython = "",
    [string]$EnvFile = "",
    [switch]$FunctionsOnly
)

$ErrorActionPreference = "Stop"
$legacyEntry = Join-Path $PSScriptRoot "start-litellm-harness.ps1"
if (-not (Test-Path -LiteralPath $legacyEntry -PathType Leaf)) {
    throw "Harness startup implementation is missing: $legacyEntry"
}

& $legacyEntry @PSBoundParameters
exit $LASTEXITCODE
