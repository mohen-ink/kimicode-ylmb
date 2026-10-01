@echo off
rem ============================================================
rem  Kimi Code 用量面板 · 唯一启动入口（由插件 hook 调用）
rem  cwd = 插件根目录；也可用 %KIMI_PLUGIN_ROOT% 兜底
rem  有 Python -> service.py --tick（拉起常驻服务/触发刷新）
rem  无 Python -> 记日志提示（模型管理/配额需要常驻服务）
rem ============================================================
setlocal
set "ROOT=%~dp0.."
if defined KIMI_PLUGIN_ROOT set "ROOT=%KIMI_PLUGIN_ROOT%"
set "SVC=%ROOT%\scripts\service.py"
set "LOG=%ROOT%\scripts\service.log"

rem --- 找 Python：py 启动器 > PATH > 常见安装路径 ---
set "PYEXE="
where py >nul 2>&1 && (set "PYEXE=py -3" & goto :run)
where python >nul 2>&1 && (set "PYEXE=python" & goto :run)
for %%P in ("C:\Program Files\python\python.exe" "C:\Program Files\Python313\python.exe" "C:\Program Files\Python312\python.exe" "C:\Program Files\Python311\python.exe" "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%LOCALAPPDATA%\Programs\Python\Python311\python.exe") do (
    if not defined PYEXE if exist %%~P set "PYEXE=%%~P"
)
if not defined PYEXE goto :psfallback

:run
%PYEXE% "%SVC%" --tick >>"%LOG%" 2>&1
exit /b 0

:psfallback
echo [%date% %time%] Python not found on PATH; service.py --tick skipped >>"%LOG%"
exit /b 0
