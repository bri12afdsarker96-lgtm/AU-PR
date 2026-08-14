@echo off
REM ============================================================
REM  一键清理老打包脚本 · 归档而非删除（可回滚）
REM
REM  行为：
REM    把所有老/重复的打包脚本、老产物、老 spec 移到
REM      _archive_旧打包_YYYYMMDD_HHMMSS\
REM    根目录，什么都不删。确认没事之后手工删这个 _archive_ 目录即可。
REM
REM  保留（最终打包方案）：
REM    build_dist.py     ← 打包核心
REM    installer.iss     ← Inno Setup 6 脚本
REM    打包_安装器.bat   ← 一键入口（双击这个就够）
REM    启动软件.bat      ← 产物启动脚本
REM    diag_bulk_dub.*   ← 诊断脚本（不是打包）
REM
REM  绝对不动：
REM    source\ tests\ docs\ server\ 水星配音数据\
REM    .git* README.md .gitattributes .gitignore
REM ============================================================

setlocal enabledelayedexpansion
chcp 65001 >nul
cd /d "%~dp0"

REM 时间戳目录名（YYYYMMDD_HHMMSS）
for /f "tokens=1-3 delims=/- " %%a in ("%date%") do (
    set "YY=%%a"
    set "MM=%%b"
    set "DD=%%c"
)
for /f "tokens=1-2 delims=:. " %%a in ("%time%") do (
    set "HH=%%a"
    set "MI=%%b"
)
set "HH=!HH: =0!"
set "ARCHIVE=_archive_旧打包_!YY!!MM!!DD!_!HH!!MI!"

echo.
echo ============================================================
echo   归档目录：%CD%\!ARCHIVE!\
echo   （只 move，不 delete；确认没事后手工删该目录即可）
echo ============================================================
echo.

if exist "!ARCHIVE!" (
    echo [X] 归档目录已存在，为避免覆盖请先手工处理：!ARCHIVE!
    pause
    exit /b 1
)
mkdir "!ARCHIVE!"
mkdir "!ARCHIVE!\bat脚本" 2>nul
mkdir "!ARCHIVE!\py脚本" 2>nul
mkdir "!ARCHIVE!\spec_pyinstaller" 2>nul
mkdir "!ARCHIVE!\日志和快照" 2>nul
mkdir "!ARCHIVE!\旧目录" 2>nul

REM ---------- 老 bat 脚本 ----------
call :MV_FILE "打包_轻量云配版.bat"           "!ARCHIVE!\bat脚本\"
call :MV_FILE "打包_轻量云配版_一键.bat"      "!ARCHIVE!\bat脚本\"
call :MV_FILE "打包_轻量云配版包.bat"         "!ARCHIVE!\bat脚本\"
call :MV_FILE "打包_整合离线版.bat"           "!ARCHIVE!\bat脚本\"
call :MV_FILE "打包安装程序.bat"              "!ARCHIVE!\bat脚本\"
call :MV_FILE "打包安装程序_轻量云配版.bat"   "!ARCHIVE!\bat脚本\"
call :MV_FILE "项目打包.bat"                  "!ARCHIVE!\bat脚本\"
call :MV_FILE "一键修复ffmpeg.bat"            "!ARCHIVE!\bat脚本\"
call :MV_FILE "生成环境快照.bat"              "!ARCHIVE!\bat脚本\"
call :MV_FILE "本次命令执行.bat"              "!ARCHIVE!\bat脚本\"

REM ---------- 老 py 脚本 ----------
call :MV_FILE "打包收尾.py"                   "!ARCHIVE!\py脚本\"
call :MV_FILE "生成环境快照.py"               "!ARCHIVE!\py脚本\"
call :MV_FILE "环境快照.py"                   "!ARCHIVE!\py脚本\"

REM ---------- PyInstaller 时代的 spec ----------
call :MV_FILE "水星配音对齐工作室.spec"       "!ARCHIVE!\spec_pyinstaller\"
call :MV_FILE "水星配音对齐工作室_云配版.spec" "!ARCHIVE!\spec_pyinstaller\"

REM ---------- 日志和快照文件 ----------
call :MV_FILE "环境快照.txt"                  "!ARCHIVE!\日志和快照\"
call :MV_FILE "本次操作日志.txt"              "!ARCHIVE!\日志和快照\"
call :MV_FILE "打包说明.md"                   "!ARCHIVE!\日志和快照\"

REM ---------- 老产物目录 ----------
call :MV_DIR  "build"                         "!ARCHIVE!\旧目录\"
call :MV_DIR  "dist"                          "!ARCHIVE!\旧目录\"
call :MV_DIR  "打包产物"                      "!ARCHIVE!\旧目录\"
call :MV_DIR  "发布包"                        "!ARCHIVE!\旧目录\"
call :MV_DIR  "轻量云配版包"                  "!ARCHIVE!\旧目录\"
call :MV_DIR  "installer"                     "!ARCHIVE!\旧目录\"

echo.
echo ============================================================
echo   ✅ 归档完成
echo ============================================================
echo.
echo   保留的最终打包方案：
if exist "build_dist.py"        echo     [OK] build_dist.py
if exist "installer.iss"        echo     [OK] installer.iss
if exist "打包_安装器.bat"      echo     [OK] 打包_安装器.bat
if exist "启动软件.bat"         echo     [OK] 启动软件.bat
if exist "diag_bulk_dub.bat"    echo     [OK] diag_bulk_dub.bat  ^(诊断，不是打包^)
if exist "diag_bulk_dub.py"     echo     [OK] diag_bulk_dub.py

if not exist "installer.iss" (
    echo.
    echo   [!] 未检测到 installer.iss —— 请先拉最新分支：
    echo         git pull origin claude/installer-exe
)
if not exist "打包_安装器.bat" (
    echo   [!] 未检测到 打包_安装器.bat —— 同上：
    echo         git pull origin claude/installer-exe
)

echo.
echo   归档目录：!ARCHIVE!\
echo   → 确认没问题后，右键该目录 → 删除即可
echo   → 需要回滚？把里面文件剪回根目录
echo.
pause
exit /b 0


REM ============================================================
REM  子过程：MV_FILE  源文件  目标目录
REM ============================================================
:MV_FILE
if exist "%~1" (
    move /Y "%~1" "%~2" >nul
    if !errorlevel!==0 (
        echo   [归档] %~1
    ) else (
        echo   [失败] %~1  ^(errorlevel=!errorlevel!^)
    )
) else (
    echo   [跳过] %~1  ^(不存在^)
)
exit /b 0

REM ============================================================
REM  子过程：MV_DIR  源目录  目标父目录
REM ============================================================
:MV_DIR
if exist "%~1\" (
    move /Y "%~1" "%~2" >nul
    if !errorlevel!==0 (
        echo   [归档] %~1\
    ) else (
        echo   [失败] %~1\  ^(errorlevel=!errorlevel!^)
    )
) else (
    echo   [跳过] %~1\  ^(不存在^)
)
exit /b 0
