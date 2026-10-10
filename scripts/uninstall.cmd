@echo off
rem ============================================================
rem  Kimi Code usage panel - one-shot uninstaller
rem  Clicked from the sidebar "Uninstall" button. Copies the worker
rem  script to %TEMP% (so the plugin directory can be deleted while
rem  the script is still running), then runs it in this window.
rem  NOTE: keep this file ASCII-only with CRLF line endings.
rem ============================================================
setlocal
set "ROOT=%~dp0.."
if defined KIMI_PLUGIN_ROOT set "ROOT=%KIMI_PLUGIN_ROOT%"
set "STAGED=%TEMP%\kimi-code-usage-uninstall.ps1"

copy /y "%ROOT%\scripts\uninstall-core.ps1" "%STAGED%" >nul 2>&1
if not exist "%STAGED%" (
    echo Failed to stage the uninstall script in %%TEMP%%.
    pause
    exit /b 1
)

title Kimi Code Usage Panel - Uninstall
powershell -NoProfile -ExecutionPolicy Bypass -File "%STAGED%" -Root "%ROOT%"
exit /b 0
