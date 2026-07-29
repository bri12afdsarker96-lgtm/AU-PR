@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 水星配音对齐工作室 · 启动
echo ==========================================
echo   启动水星配音对齐工作室（浏览器界面会自动打开）
echo   关闭软件：回到本黑窗口按 Ctrl+C，或直接关掉本窗口
echo ==========================================
echo.

rem ── 找一个真正能执行的 Python（逐行试，避免 for/&& 解析歧义）──
set "PYEXE="
py -3 -c "import sys" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE python -c "import sys" >nul 2>nul && set "PYEXE=python"
if not defined PYEXE python3 -c "import sys" >nul 2>nul && set "PYEXE=python3"
if not defined PYEXE if exist "E:\环境依赖\Python311\python.exe" set "PYEXE=E:\环境依赖\Python311\python.exe"
if not defined PYEXE if exist "D:\Python\Python311\python.exe" set "PYEXE=D:\Python\Python311\python.exe"
if not defined PYEXE goto :NOPY

if not exist "source\dub_align_studio\launcher.py" goto :NOSRC

echo [Python] 使用解释器：%PYEXE%
echo [启动] 正在拉起软件……首次打开稍等几秒，浏览器会自动弹出界面。
echo.
set "PYTHONPATH=source"
%PYEXE% -m dub_align_studio

echo.
echo [已退出] 软件已停止。若是异常退出，请把上方最后几行发给 Claude。
pause
exit /b 0

:NOSRC
echo [错误] 未找到 source\dub_align_studio\launcher.py。
echo        说明本地代码不完整，请先在 GitHub Desktop 里 Pull 到最新。
pause
exit /b 1

:NOPY
echo [错误] 未找到可用的 Python。请先安装 Python 3.11+：
echo        官网 https://www.python.org/downloads/ ，安装时务必勾选 "Add python.exe to PATH"。
echo        已装却仍报错：到 设置 → 应用 → 高级应用设置 → 应用执行别名，
echo        把 python.exe / python3.exe 两个开关关掉。
pause
exit /b 1
