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
rem ============================================================
setlocal
set "ROOT=%~dp0.."
if defined KIMI_PLUGIN_ROOT set "ROOT=%KIMI_PLUGIN_ROOT%"
set "SVC=%ROOT%\scripts\service.py"
set "LOG=%ROOT%\scripts\service.log"

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
"%PYEXE%" %PYARG% "%SVC%" --tick >>"%LOG%" 2>&1
exit /b 0

:nopython
rem --- No Python found: log it, then try to inject a static placeholder so the
rem     sidebar at least shows an "install Python" hint instead of staying blank.
echo [%date% %time%] Python not found on PATH; service.py --tick skipped >>"%LOG%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\scripts\need-python.ps1" >>"%LOG%" 2>&1
exit /b 0
