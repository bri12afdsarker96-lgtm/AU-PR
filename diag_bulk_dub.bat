@echo off
REM ============================================================
REM  Bulk Dub failure diagnostic
REM  Double-click to run. Log: diag_bulk_dub_log.txt
REM  All content ASCII to avoid GBK/UTF-8 codepage traps.
REM ============================================================

setlocal ENABLEDELAYEDEXPANSION
cd /d "%~dp0"

chcp 65001 >nul

echo.
echo ============================================================
echo   Bulk Dub failure diagnostic
echo ============================================================
echo.
echo cwd: %CD%
echo.

REM ---- Locate Python: bundled first, then system ----
set "PY="
if exist ".\python\python.exe"                     set "PY=.\python\python.exe"
if not defined PY if exist ".\venv\Scripts\python.exe"  set "PY=.\venv\Scripts\python.exe"
if not defined PY if exist ".\.venv\Scripts\python.exe" set "PY=.\.venv\Scripts\python.exe"
if not defined PY if exist ".\python311\python.exe"     set "PY=.\python311\python.exe"
if not defined PY (
    where python >nul 2>&1
    if !errorlevel! equ 0 set "PY=python"
)
if not defined PY (
    where py >nul 2>&1
    if !errorlevel! equ 0 set "PY=py -3"
)

if not defined PY (
    echo [ERROR] No Python interpreter found.
    echo Please install Python 3 from https://www.python.org/downloads/
    echo or ensure .\python\python.exe exists in this folder.
    echo.
    pause
    exit /b 1
)

echo Python: %PY%
echo.

REM ---- Run the diagnostic script (ASCII filename, safe under any codepage) ----
%PY% "diag_bulk_dub.py"
set RC=%errorlevel%

echo.
echo ============================================================
if %RC% equ 0 (
    echo   Done. Log: diag_bulk_dub_log.txt
) else (
    echo   Diagnostic exited with code %RC%.
    echo   Please send the log file or a screenshot for triage.
)
echo ============================================================
echo.
pause
exit /b %RC%
