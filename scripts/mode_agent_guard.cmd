@echo off
setlocal
set "ROOT=%~dp0.."
if defined KIMI_PLUGIN_ROOT set "ROOT=%KIMI_PLUGIN_ROOT%"
set "SCRIPT=mode_agent_guard.py"
if /i "%~1"=="prompt" set "SCRIPT=mode_prompt_hint.py"
where py >nul 2>&1 && (py -3 "%ROOT%\scripts\%SCRIPT%" & exit /b)
for %%P in ("C:\Program Files\Python313\python.exe" "C:\Program Files\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe") do (
    if exist "%%~P" ("%%~P" "%ROOT%\scripts\%SCRIPT%" & exit /b)
)
if /i "%~1"=="prompt" exit /b 0
echo Python unavailable; cannot enforce session agent policy 1>&2
exit /b 2
