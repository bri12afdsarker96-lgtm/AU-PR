@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 环境快照 · 一键生成并上传
echo ==========================================
echo   环境快照：抓取本机目录/环境状态 → 推送到 GitHub
echo   （开发者直接读文件即可远程看到你的真实环境，无需截图）
echo ==========================================
echo.

python 环境快照.py
if errorlevel 1 ( echo [错误] 快照生成失败，请把上方输出截图发给开发。& pause & exit /b 1 )

echo.
echo [上传] 提交并推送 环境快照.txt ...
git add 环境快照.txt
git commit -m "环境快照：本机目录与环境状态" >nul 2>nul
git push
if errorlevel 1 (
  echo [警告] 推送失败（网络/权限）。快照文件已生成在本目录：环境快照.txt
  echo         可稍后重试 git push，或把该文件内容直接粘贴给开发。
) else (
  echo.
  echo ✅ 已上传。告诉 Claude：「快照已推送」即可。
)
pause
