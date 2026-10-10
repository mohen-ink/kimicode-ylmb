@echo off
rem ============================================================
rem  Kimi Code usage panel - the only startup entry (called by plugin hooks)
rem  cwd = plugin root; %KIMI_PLUGIN_ROOT% is used as a fallback
rem  Python found -> service.py --tick (spawn daemon / trigger refresh)
rem  No Python    -> inject a static "Python required" placeholder so the
rem                 sidebar card shows an install hint instead of nothing
rem  NOTE: keep this file ASCII-only with CRLF line endings. cmd.exe reads it
rem        with the ANSI code page (GBK on zh-CN Windows); UTF-8 Chinese or
rem        LF-only endings make it mis-parse and the daemon silently never starts.
rem  NOTE: never redirect the tick's output to the shared service.log from here.
rem        cmd opens ">>file" with deny-write sharing, so when several hooks fire
rem        at once (and a tick is still running) every later call dies in the
rem        redirect before python starts -- with errorlevel 0, i.e. silently.
rem        service.py logs to that file itself with a shared append, so use nul.
rem ============================================================
setlocal
set "ROOT=%~dp0.."
if defined KIMI_PLUGIN_ROOT set "ROOT=%KIMI_PLUGIN_ROOT%"
set "SVC=%ROOT%\scripts\service.py"
set "UNINSTALL_MARKER=%USERPROFILE%\.kimi-code\usage-dashboard\plugin-uninstall.json"
if exist "%UNINSTALL_MARKER%" exit /b 0

rem --- locate Python: py launcher > PATH > common install dirs ---
set "PYEXE="
set "PYARG="
where py >nul 2>&1 && (set "PYEXE=py" & set "PYARG=-3" & goto :run)
where python >nul 2>&1 && (set "PYEXE=python" & goto :run)
for %%P in ("C:\Program Files\python\python.exe" "C:\Program Files\Python313\python.exe" "C:\Program Files\Python312\python.exe" "C:\Program Files\Python311\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe") do (
    if not defined PYEXE if exist "%%~P" set "PYEXE=%%~P"
)
if not defined PYEXE goto :nopython

:run
rem Output goes to nul: service.py writes service.log itself (shared append) and
rem cmd's >> rewrite would make concurrent hooks collide on the same file.
"%PYEXE%" %PYARG% "%SVC%" --tick >nul 2>&1
exit /b 0

:nopython
rem --- No Python found: inject a static placeholder so the sidebar at least
rem     shows an "install Python" hint instead of staying blank. Output to nul
rem     for the same reason as :run (no safe shared-file redirect from cmd).
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\need-python.ps1" >nul 2>&1
exit /b 0
