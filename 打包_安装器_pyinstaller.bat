@echo off
REM ============================================================
REM  用 PyInstaller 打包（不用 nuitka） -> Inno Setup 出安装器
REM
REM  为什么走 PyInstaller：
REM    - 你机器 Python 3.14 装 nuitka 大概率失败（nuitka 未跟上 3.14）
REM    - 你已有 pyinstaller 6.22.0 + 老 spec，直接复用最省心
REM
REM  流程：
REM    Step 1  找 spec 文件（优先根目录，其次 _archive_旧打包_*）
REM    Step 2  pyinstaller <spec>  -> dist\水星配音对齐工作室_云配版\
REM    Step 3  敏感文件清扫（.py .pyc settings.json license.json 等）
REM    Step 4  ISCC.exe /DSourceDir=... /DAppExeName=... installer.iss
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "SPEC=水星配音对齐工作室_云配版.spec"
set "SPEC_FALLBACK=水星配音对齐工作室.spec"
set "PYI_DIST=dist\水星配音对齐工作室_云配版"
set "PYI_EXE=水星配音对齐工作室_云配版.exe"

REM ---------- Step 1  找 spec ----------
echo.
echo ============================================================
echo   Step 1/4  定位 pyinstaller spec 文件
echo ============================================================

if exist "%SPEC%" (
    echo   [OK] 找到 %SPEC%
    goto :have_spec
)

echo   根目录无 %SPEC%，去 _archive_旧打包_* 里找...
for /d %%A in ("_archive_旧打包_*") do (
    if exist "%%A\spec_pyinstaller\%SPEC%" (
        echo   [归档命中] %%A\spec_pyinstaller\%SPEC% -^> 复原到根目录
        copy /Y "%%A\spec_pyinstaller\%SPEC%" "%SPEC%" >nul
        copy /Y "%%A\spec_pyinstaller\%SPEC_FALLBACK%" "%SPEC_FALLBACK%" >nul 2>&1
        goto :have_spec
    )
)

echo.
echo   [X] 没找到 %SPEC%，无法用 pyinstaller 打包
echo       备选：直接用 发布包\ 里的 v0.7.71 exe（虽是老代码）
pause
exit /b 1

:have_spec

REM ---------- Step 2  pyinstaller ----------
echo.
echo ============================================================
echo   Step 2/4  PyInstaller 编译（可能 3-10 分钟）
echo ============================================================
REM 不依赖 PATH 里的 pyinstaller.exe（Scripts\ 常不在 PATH），
REM 直接用当前 python 的 -m PyInstaller，pyinstaller 只要 pip install 过就行。
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo   [X] 当前 python 没装 pyinstaller
    echo       修复：python -m pip install pyinstaller
    echo       当前 python:
    where python
    pause
    exit /b 2
)
for /f "delims=" %%v in ('python -m PyInstaller --version 2^>nul') do echo   [OK] pyinstaller %%v

REM 清 dist 里的老产物免得残留
if exist "%PYI_DIST%\" (
    echo   清理旧 %PYI_DIST%\
    rmdir /S /Q "%PYI_DIST%"
)

python -m PyInstaller --clean --noconfirm "%SPEC%"
if errorlevel 1 (
    echo   [X] pyinstaller 失败（exit=%errorlevel%）
    pause
    exit /b %errorlevel%
)

if not exist "%PYI_DIST%\%PYI_EXE%" (
    echo   [X] pyinstaller 没输出主 exe: %PYI_DIST%\%PYI_EXE%
    echo       检查 spec 文件是否指向正确入口 launcher.py
    pause
    exit /b 3
)

echo   [OK] %PYI_DIST%\%PYI_EXE%

REM ---------- Step 3  敏感文件清扫 ----------
echo.
echo ============================================================
echo   Step 3/4  敏感文件清扫（.py .pyc settings.json 等）
echo ============================================================
REM 用 Python 一句话复用 build_dist.py 的 scrub_sensitive
python -c "import sys; sys.path.insert(0, '.'); from build_dist import scrub_sensitive; from pathlib import Path; n = scrub_sensitive(Path(r'%PYI_DIST%')); print(f'  已清除 {n} 项敏感文件')"
if errorlevel 1 (
    echo   [!] 清扫脚本失败（不致命，continue）
)

REM ---------- Step 4  ISCC ----------
echo.
echo ============================================================
echo   Step 4/4  Inno Setup 编译 installer
echo ============================================================
REM 中文语言包预检
if not exist "installer\ChineseSimplified.isl" (
    echo   [!] 未找到 installer\ChineseSimplified.isl -^> 装向导会用英文
    set /p DL="       现在下载吗？（Y=下载 N=用英文）: "
    if /i "!DL!"=="Y" call "installer\_下载中文语言包.bat"
    echo.
)

set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo   [X] 未找到 ISCC.exe: https://jrsoftware.org/isdl.php
    pause
    exit /b 4
)

"%ISCC%" /DSourceDir="%PYI_DIST%" /DAppExeName="%PYI_EXE%" installer.iss
if errorlevel 1 (
    echo   [X] ISCC 失败（exit=%errorlevel%）
    pause
    exit /b %errorlevel%
)

echo.
echo ============================================================
echo   [OK] 打包完成
echo ============================================================
echo   产物：dist\安装器\setup_水星配音对齐工作室_v0.7.71_lite.exe
echo   把这一个 exe 发给用户
echo.
pause
exit /b 0
