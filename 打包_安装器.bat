@echo off
REM ============================================================
REM  一键打包 · 水星配音对齐工作室 · 轻量云配版包 -> exe 安装器
REM
REM  流程：
REM    Step 1  build_dist.py --with-ffmpeg
REM            -> dist\轻量云配版包\  （Nuitka + Cython 编译 + ffmpeg 内嵌 + 敏感清扫）
REM    Step 2  Inno Setup 6 ISCC.exe installer.iss
REM            -> dist\安装器\setup_水星配音对齐工作室_v0.7.71_lite.exe
REM
REM  产物：一个 setup_xxx.exe，双击就装，用户机器无需任何 python/ffmpeg 环境
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   Step 1/2  Nuitka + Cython + ffmpeg 打包（build_dist.py）
echo ============================================================
python build_dist.py --with-ffmpeg --require-ffmpeg
if errorlevel 1 (
    echo.
    echo [X] build_dist.py 失败（exit=%errorlevel%），停止打包
    echo    - 无 ffmpeg：请把 ffmpeg.exe/ffprobe.exe 放到 tools\ffmpeg\
    echo    - 无 nuitka/cython：pip install nuitka cython
    pause
    exit /b %errorlevel%
)

echo.
echo ============================================================
echo   Step 2/2  Inno Setup 6 编译（installer.iss）
echo ============================================================
REM ---- 中文向导预检：优先本地 installer\ChineseSimplified.isl ----
if not exist "installer\ChineseSimplified.isl" (
    echo.
    echo   [!] 未找到 installer\ChineseSimplified.isl
    echo       没有它，安装向导会降级到英文。
    set /p DL="    要现在下载吗？（Y=下载，N=继续用英文）: "
    if /i "!DL!"=="Y" call "installer\_下载中文语言包.bat"
)
set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
)
if not exist "%ISCC%" (
    echo [X] 未找到 Inno Setup 6 ISCC.exe
    echo     下载安装：https://jrsoftware.org/isdl.php
    pause
    exit /b 2
)

"%ISCC%" installer.iss
if errorlevel 1 (
    echo.
    echo [X] Inno Setup 编译失败（exit=%errorlevel%）
    pause
    exit /b %errorlevel%
)

echo.
echo ============================================================
echo   [OK] 打包完成
echo ============================================================
echo   产物：dist\安装器\setup_水星配音对齐工作室_v0.7.71_lite.exe
echo   把这一个 exe 发给用户即可，无需别的东西
echo.
pause
exit /b 0
