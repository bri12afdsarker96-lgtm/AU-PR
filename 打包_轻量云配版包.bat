@echo off
REM ============================================================
REM  打包 · 轻量云配版包（发行版）
REM  强制启用：激活码 gate + RASP strict + Cython + Nuitka
REM  产物目录：dist\轻量云配版包\
REM ============================================================

setlocal ENABLEDELAYEDEXPANSION
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo ============================================================
echo   打包 · 轻量云配版包
echo ============================================================
echo.
echo cwd: %CD%
echo.

REM ---- Python 定位 ----
set "PY="
if exist ".\python\python.exe"                     set "PY=.\python\python.exe"
if not defined PY if exist ".\venv\Scripts\python.exe"  set "PY=.\venv\Scripts\python.exe"
if not defined PY if exist ".\.venv\Scripts\python.exe" set "PY=.\.venv\Scripts\python.exe"
if not defined PY (
    where python >nul 2>&1
    if !errorlevel! equ 0 set "PY=python"
)
if not defined PY (
    where py >nul 2>&1
    if !errorlevel! equ 0 set "PY=py -3"
)
if not defined PY (
    echo [ERROR] 未找到 Python 3。
    pause
    exit /b 1
)

echo Python: %PY%
echo.

REM ---- 装依赖（第一次跑会装 cython/nuitka）----
echo 检查/安装打包依赖 cython + nuitka…
%PY% -m pip install --disable-pip-version-check --quiet cython nuitka setuptools
if !errorlevel! neq 0 (
    echo [WARN] 依赖安装失败；将尝试跳过 Cython/Nuitka
)

REM ---- 跑构建 ----
%PY% "build_dist.py" --out "dist\轻量云配版包"
set RC=%errorlevel%

echo.
echo ============================================================
if %RC% equ 0 (
    echo ✅ 打包完成 · 产物在 dist\轻量云配版包\
    echo    用户复制该整个目录即可运行「启动软件.bat」
) else (
    echo ❌ 打包失败 exit=%RC%
)
echo ============================================================
echo.
pause
exit /b %RC%
