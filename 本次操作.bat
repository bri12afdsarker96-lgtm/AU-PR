@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 本次操作 · 一键拉取最新代码
rem ════════════════════════════════════════════════════════════════
rem 把最新软件代码（source/）拉到本机。不动数据、不动本文件。
rem 找 git 的顺序：PATH 里的 git → GitHub Desktop 自带的 git（装了 GHD 即可，无需单独装 git）。
rem 拉的是权威代码分支 release/v0.5-download-fix（含全部最新修复）。
rem 放到 D:\Mercury\AU-PR（GitHub Desktop 克隆的那个目录）里双击。
rem ════════════════════════════════════════════════════════════════
set "BRANCH=release/v0.5-download-fix"

if not exist ".git" (
  echo [错误] 这里不是 git 仓库：%~dp0
  echo        请把本文件放到 D:\Mercury\AU-PR（GitHub Desktop 克隆出来的目录）里再双击。
  pause
  exit /b 1
)

rem —— 找一个可用的 git ——
set "GIT="
where git >nul 2>nul && set "GIT=git"
if not defined GIT for /d %%v in ("%LocalAppData%\GitHubDesktop\app-*") do if exist "%%v\resources\app\git\cmd\git.exe" set "GIT=%%v\resources\app\git\cmd\git.exe"
if not defined GIT (
  echo [没找到 git] 最简单：打开 GitHub Desktop，把当前分支切到 release/v0.5-download-fix，
  echo             点右上「Fetch origin / Pull origin」即可拉最新——和本脚本效果一样。
  echo 或安装 Git for Windows / GitHub Desktop 后再双击本脚本。
  pause
  exit /b 1
)
echo 使用 git："%GIT%"

echo.
echo ════════ 拉取最新代码（分支 %BRANCH%）════════
"%GIT%" fetch origin
if errorlevel 1 (
  echo [失败] fetch 失败（网络或未登录）。请先在 GitHub Desktop 里登录账号，或点它的 Fetch/Pull origin。
  pause
  exit /b 1
)
"%GIT%" checkout origin/%BRANCH% -- source
if errorlevel 1 (
  echo [失败] 更新 source 失败。请把上面的输出发给 Claude。
  pause
  exit /b 1
)
echo [完成] 软件代码 source/ 已更新到最新。

echo.
echo ════════ 当前版本 ════════
set "PYTHONPATH=source"
python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import APP_VERSION;print('APP_VERSION =',APP_VERSION)" 2>nul

echo.
echo 若软件正开着：刷新浏览器页面（或重开一次）即可看到最新界面。
echo （纯前端/代码改动无需重装依赖；只有代码更新，数据和模型不动。）
pause
exit /b 0
