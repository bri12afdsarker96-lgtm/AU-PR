@echo off
REM ============================================================
REM  快速通道：用你**已有**的旧发行目录直接打 installer.exe，
REM  跳过 build_dist.py（不需要 nuitka/cython/ffmpeg 环境）。
REM
REM  用法：
REM    打包_安装器_用旧产物.bat  "旧产物目录"
REM
REM  示例：
REM    打包_安装器_用旧产物.bat  "轻量云配版包"
REM    打包_安装器_用旧产物.bat  "dist\轻量云配版包"
REM    打包_安装器_用旧产物.bat  "发布包"
REM
REM  条件：该目录里必须已有 水星配音对齐工作室.exe（其他文件按 excludes 过滤）
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

if "%~1"=="" (
    echo.
    echo 用法：%~nx0  "旧产物目录"
    echo.
    echo 建议先跑 找旧产物.bat 看哪些目录可用
    pause
    exit /b 1
)

set "SRC=%~1"
set "EXE_NAME=水星配音对齐工作室.exe"

if not exist "%SRC%\%EXE_NAME%" (
    echo.
    echo [X] %SRC%\%EXE_NAME% 不存在
    echo     该目录不含主 exe，无法打包。先跑 找旧产物.bat 确认。
    pause
    exit /b 2
)

echo.
echo ============================================================
echo   源目录：%SRC%\
echo   将复用它直接打 installer.exe（跳过 nuitka 编译）
echo ============================================================
echo.

REM 中文语言包预检
if not exist "installer\ChineseSimplified.isl" (
    echo [!] 未找到 installer\ChineseSimplified.isl —— 安装向导会用英文
    set /p DL="    现在下载吗？（Y=下载 N=用英文）: "
    if /i "!DL!"=="Y" call "installer\_下载中文语言包.bat"
    echo.
)

REM 找 ISCC
set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo [X] 未找到 Inno Setup 6 ISCC.exe
    echo     下载安装：https://jrsoftware.org/isdl.php
    pause
    exit /b 3
)

REM 用 /D 覆盖 installer.iss 里的 SourceDir 常量，指向本次给的目录
"%ISCC%" /DSourceDir="%SRC%" installer.iss
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
echo.
pause
exit /b 0
