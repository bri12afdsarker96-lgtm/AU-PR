@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 环境快照 · 一键生成
echo ==========================================
echo   环境快照：抓取本机目录/环境/dots.tts真实API/参考音频体检
echo   （开发者直接读文件即可远程定位是哪一环报错，无需截图）
echo ==========================================
echo.

python 环境快照.py
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
