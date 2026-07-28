@echo off
setlocal

set "REPOSITORY_ROOT=%~dp0"
set "SETUP_SCRIPT=%REPOSITORY_ROOT%team_agent_harness\backend\scripts\setup-desktop.ps1"

if not exist "%SETUP_SCRIPT%" (
    echo Team Agent Harness setup script was not found:
    echo %SETUP_SCRIPT%
    pause
    exit /b 1
)

powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%SETUP_SCRIPT%"
set "SETUP_EXIT_CODE=%ERRORLEVEL%"

if not "%SETUP_EXIT_CODE%"=="0" (
    echo.
    echo Setup did not complete. Review the error above, then run this file again.
    pause
)

exit /b %SETUP_EXIT_CODE%
