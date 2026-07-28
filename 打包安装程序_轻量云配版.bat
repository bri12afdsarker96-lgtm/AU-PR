@echo off
setlocal
cd /d "%~dp0"
title 打包安装程序 · 轻量云配版 → 单文件 Setup.exe
echo 开始生成云配版安装程序…（本窗口若一闪而过：多为①没装 Inno Setup 6 ②还没跑 打包_轻量云配版.bat）
rem ============================================================
rem 把「轻量云配版包」封成带图标/快捷方式/卸载项的单文件安装程序(Inno Setup 6)。
rem 发给别人(集显低配电脑也行)双击即可安装。
rem 前置：① 先跑「打包_轻量云配版.bat」生成轻量包；② 本机装好 Inno Setup 6(含 ISCC.exe)。
rem 产物：发布包\水星配音对齐工作室_云配版安装程序_v<版本>.exe
rem ============================================================

set "VER=0.0.0"
for /f "usebackq delims=" %%v in (`python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import APP_VERSION;print(APP_VERSION)" 2^>nul`) do set "VER=%%v"
echo [信息] 版本：%VER%

set "SRC=%CD%\轻量云配版包\水星配音对齐工作室_轻量版_v%VER%"
if not exist "%SRC%\启动.bat" (
  echo [错误] 没找到轻量包：%SRC%
  echo        请先双击「打包_轻量云配版.bat」生成轻量包，再运行本脚本。
  pause & exit /b 1
)
echo [信息] 轻量包：%SRC%

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
  echo [错误] 未找到 Inno Setup 6(ISCC.exe)。
  echo        请到 https://jrsoftware.org/isdl.php 下载安装 Inno Setup 6，再运行本脚本。
  pause & exit /b 1
)
echo [信息] 编译器：%ISCC%

echo [编译] 正在生成云配版安装程序 ...
"%ISCC%" /DLite=1 /DMyVer=%VER% /DRepoDir="%CD%" /DSrcDir="%SRC%" "installer\水星配音对齐工作室.iss"
if errorlevel 1 ( echo [失败] 编译失败，请把上方输出发给开发。& pause & exit /b 1 )

echo.
echo [完成] 安装程序在：发布包\水星配音对齐工作室_云配版安装程序_v%VER%.exe
echo        直接发给别人，双击安装(带图标、桌面/开始菜单快捷方式、卸载项)。
echo        目标机：无需独立显卡，集显即可；装好后在「设置-云配音」填云地址+API Key。
pause
exit /b 0
