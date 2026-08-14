@echo off
REM ============================================================
REM  批量带货配音 - 失败原因诊断
REM  双击即可运行，输出到 诊断_批量带货失败原因_日志.txt
REM ============================================================

setlocal ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

REM ---- 让控制台能正常显示中文 ----
chcp 65001 >nul

echo.
echo ============================================================
echo   批量带货配音 - 失败原因诊断
echo ============================================================
echo.
echo 目录：%CD%
echo.

REM ---- 找 Python：优先项目自带的（若有），其次系统 python ----
set "PY="
if exist ".\python\python.exe"           set "PY=.\python\python.exe"
if not defined PY if exist ".\venv\Scripts\python.exe"   set "PY=.\venv\Scripts\python.exe"
if not defined PY if exist ".\.venv\Scripts\python.exe"  set "PY=.\.venv\Scripts\python.exe"
if not defined PY (
    where python >nul 2>&1
    if !errorlevel! equ 0 set "PY=python"
)
if not defined PY (
    where py >nul 2>&1
    if !errorlevel! equ 0 set "PY=py -3"
)

if not defined PY (
    echo [错误] 找不到 python 解释器。
    echo 请确认软件目录里有 python\ 或 venv\，或系统已装 Python 3。
    echo.
    pause
    exit /b 1
)

echo 使用 Python：%PY%
echo.

REM ---- 跑诊断脚本 ----
%PY% "诊断_批量带货失败原因.py"
set RC=%errorlevel%

echo.
echo ============================================================
if %RC% equ 0 (
    echo   诊断完成。日志：诊断_批量带货失败原因_日志.txt
) else (
    echo   诊断脚本返回错误码 %RC%。
    echo   请把上方输出截图或把 诊断_批量带货失败原因_日志.txt 发给协助方。
)
echo ============================================================
echo.
pause
exit /b %RC%
