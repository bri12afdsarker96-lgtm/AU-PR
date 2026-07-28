@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包安装程序 · 整合离线版 → 单文件 Setup.exe
rem ════════════════════════════════════════════════════════════════
rem 把「整合离线包」封成带图标/快捷方式/卸载项的单文件安装程序（Inno Setup 6）。
rem 前置：① 先跑 打包_整合离线版.bat 生成整合包；② 本机装好 Inno Setup 6（含 ISCC.exe）。
rem 产物：发布包\水星配音对齐工作室_安装程序_v<版本>.exe
rem ════════════════════════════════════════════════════════════════

rem [1] 读版本号
set "VER=0.0.0"
for /f "usebackq delims=" %%v in (`python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import APP_VERSION;print(APP_VERSION)" 2^>nul`) do set "VER=%%v"
echo [信息] 版本：%VER%

rem [2] 定位整合包
set "SRC=%CD%\整合离线包\水星配音对齐工作室_整合版_v%VER%"
if not exist "%SRC%\启动.bat" (
  echo [错误] 没找到整合包：%SRC%
  echo        请先双击「打包_整合离线版.bat」生成整合包，再运行本脚本。
  pause & exit /b 1
)
echo [信息] 整合包：%SRC%

rem [3] 图标（缺失则用 Python 现生成）
if not exist "installer\app.ico" (
  echo [信息] 生成图标 installer\app.ico ...
  python "installer\make_icon.py" >nul 2>nul
)

rem [4] 找 Inno Setup 编译器 ISCC.exe
set "ISCC="
for %%p in (
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
  "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
) do if not defined ISCC if exist "%%~p" set "ISCC=%%~p"
where iscc >nul 2>nul && if not defined ISCC set "ISCC=iscc"
if not defined ISCC (
  echo [错误] 未找到 Inno Setup 6（ISCC.exe）。
  echo        请到 https://jrsoftware.org/isdl.php 下载安装 Inno Setup 6，再运行本脚本。
  pause & exit /b 1
)
echo [信息] 编译器：%ISCC%

rem [5] 编译
echo [编译] 正在生成安装程序（大包压缩较慢，请耐心）...
"%ISCC%" /DMyVer=%VER% /DRepoDir="%CD%" /DSrcDir="%SRC%" "installer\水星配音对齐工作室.iss"
if errorlevel 1 ( echo [失败] 编译失败，请把上方输出发给开发。& pause & exit /b 1 )

echo.
echo [完成] 安装程序在：发布包\水星配音对齐工作室_安装程序_v%VER%.exe
echo        双击它即可安装（带图标、桌面/开始菜单快捷方式、卸载项）。
echo        提示：目标机需 NVIDIA 显卡；渲染成片需 ffmpeg.exe/ffprobe.exe。
pause
exit /b 0
