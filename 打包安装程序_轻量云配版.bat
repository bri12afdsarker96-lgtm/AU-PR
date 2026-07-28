@echo off
setlocal
cd /d "%~dp0"
title 打包安装程序 · 轻量云配版 · 生成单文件 Setup.exe
echo 开始制作云配版安装程序…（若无反应多半是没装 Inno Setup 6，或还没先跑 打包_轻量云配版.bat）
rem ============================================================
rem 把「打包_轻量云配版.bat」产出的 dist 文件夹，做成带图标/快捷方式/卸载的
rem 单文件安装程序 Setup.exe（Inno Setup 6）。别人电脑双击即可安装。
rem 前置：① 先跑「打包_轻量云配版.bat」② 本机装好 Inno Setup 6（含 ISCC.exe）。
rem 产物：发布包\水星配音对齐工作室_云配版安装程序_v<版本>.exe
rem ============================================================

set "NAME=水星配音对齐工作室_云配版"

set "VER=0.0.0"
for /f "usebackq delims=" %%v in (`python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import APP_VERSION;print(APP_VERSION)" 2^>nul`) do set "VER=%%v"
echo [信息] 版本：%VER%

set "SRC=%CD%\dist\%NAME%"
if not exist "%SRC%\%NAME%.exe" (
  echo [错误] 没找到云配版产物：%SRC%\%NAME%.exe
  echo        请先双击「打包_轻量云配版.bat」，等它构建完成再来。
  pause & exit /b 1
)
echo [信息] 源目录：%SRC%

if not exist "installer\app.ico" (
  echo [信息] 生成图标 ...
  python "installer\make_icon.py" >nul 2>nul
)

set "ISCC="
for %%p in (
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
  "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
) do if not defined ISCC if exist "%%~p" set "ISCC=%%~p"
where iscc >nul 2>nul && if not defined ISCC set "ISCC=iscc"
if not defined ISCC (
  echo [错误] 未找到 Inno Setup 6（ISCC.exe）。
  echo        请到 https://jrsoftware.org/isdl.php 下载安装 Inno Setup 6 后再跑本脚本。
  pause & exit /b 1
)
echo [信息] 编译器：%ISCC%

echo [编译] 生成云配版安装程序 ...
"%ISCC%" /DLite=1 /DMyVer=%VER% /DRepoDir="%CD%" /DSrcDir="%SRC%" "installer\水星配音对齐工作室.iss"
if errorlevel 1 ( echo [失败] 编译失败，请把上方红字整段发我。& pause & exit /b 1 )

echo.
echo [完成] 安装程序在：发布包\水星配音对齐工作室_云配版安装程序_v%VER%.exe
echo        直接发给别人：双击安装（含图标、桌面/开始菜单快捷方式、卸载）。
echo        目标电脑无需独立显卡，集成显卡即可；装好后在「设置-云配音」填服务器地址+API Key。
pause
exit /b 0
