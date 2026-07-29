@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 环境快照 · 一键生成
echo ==========================================
echo   环境快照：抓取本机目录/环境/dots.tts真实API/参考音频体检
echo   （开发者直接读文件即可远程定位是哪一环报错，无需截图）
echo ==========================================
echo.

rem 找一个真正能执行的 Python：优先 py 启动器，其次 python / python3
set "PYEXE="
for %%P in ("py -3" "python" "python3") do if not defined PYEXE %%~P -c "import sys" >nul 2>nul && set "PYEXE=%%~P"
if not defined PYEXE goto :NOPY

%PYEXE% 环境快照.py
if errorlevel 1 ( echo [错误] 快照生成失败，请把上方输出截图发给开发。& pause & exit /b 1 )

echo.
where git >nul 2>nul
if errorlevel 1 (
  echo [提示] 本机未检测到 git（不在 PATH）——跳过自动上传。
  echo        已为你打开 环境快照.txt，请全选复制其内容直接发给 Claude。
  start "" notepad "环境快照.txt"
  goto done
)

echo [上传] 提交并推送 环境快照.txt ...
git add 环境快照.txt
git commit -m "环境快照：本机目录与环境状态" >nul 2>nul
git push
if errorlevel 1 (
  echo [警告] 推送失败（网络/权限）。已为你打开 环境快照.txt，请复制内容发给 Claude。
  start "" notepad "环境快照.txt"
) else (
  echo.
  echo [OK] 已上传。告诉 Claude：「快照已推送」即可。
)

:done
echo.
pause
exit /b 0

:NOPY
echo [错误] 未找到可用的 Python。请先安装 Python 3.11+：
echo        官网 https://www.python.org/downloads/ ，安装时务必勾选 "Add python.exe to PATH"。
echo        已装却仍报错：多半是 PATH 里的 python 是"应用商店占位符"，到
echo        设置 → 应用 → 高级应用设置 → 应用执行别名，把 python.exe / python3.exe 关掉。
pause
exit /b 1
