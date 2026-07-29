@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 生成环境快照 · 换机/新窗口对齐
echo ==========================================
echo   生成环境快照：深扒本地仓 + 诊断与真源 v0.5 的对齐关系
echo   （开发者/新对话窗口直接读 环境快照.txt 即可对齐颗粒度，无需截图）
echo ==========================================
echo.

rem ── 找一个真正能执行的 Python（逐行试，避免 for/&& 解析歧义）──
set "PYEXE="
py -3 -c "import sys" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE python -c "import sys" >nul 2>nul && set "PYEXE=python"
if not defined PYEXE python3 -c "import sys" >nul 2>nul && set "PYEXE=python3"
if not defined PYEXE if exist "E:\环境依赖\Python311\python.exe" set "PYEXE=E:\环境依赖\Python311\python.exe"
if not defined PYEXE if exist "D:\Python\Python311\python.exe" set "PYEXE=D:\Python\Python311\python.exe"
if not defined PYEXE goto :NOPY

echo [Python] 使用解释器：%PYEXE%
echo.

rem 先拉一次远程引用，保证"本地 vs 真源 v0.5 / main"的领先落后判断是最新的
where git >nul 2>nul
if not errorlevel 1 (
  echo [同步] git fetch origin （只取引用，不改工作区）...
  git fetch origin >nul 2>nul
)

%PYEXE% 生成环境快照.py
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
git commit -m "环境快照：本机对齐状态与仓库深扫" >nul 2>nul
git push
if errorlevel 1 (
  echo [警告] 推送失败（网络/权限，或需在 GitHub Desktop 里 Push）。
  echo        没关系——已为你打开 环境快照.txt，直接复制内容发给 Claude 即可。
  start "" notepad "环境快照.txt"
) else (
  echo.
  echo [OK] 已上传。告诉 Claude：「快照已推送」即可。
)

:done
echo.
echo （本窗口不会自动关闭；看完按任意键退出）
pause
exit /b 0

:NOPY
echo [错误] 未找到可用的 Python。请先安装 Python 3.11+：
echo        官网 https://www.python.org/downloads/ ，安装时务必勾选
echo        "Add python.exe to PATH"（并建议勾 py launcher）。装完重开本窗口再双击。
echo.
echo        已装却仍报错：多半是 PATH 里的 python 是"应用商店占位符"。
echo        到 设置 → 应用 → 高级应用设置 → 应用执行别名，把 python.exe / python3.exe 关掉。
pause
exit /b 1
