@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 本次命令执行 · 换机对齐收尾自检
rem ════════════════════════════════════════════════════════════════
rem 协作约定：Claude 需要你在本地执行什么，就更新本文件并推到 GitHub。
rem 你只需：GitHub Desktop 里 Pull → 双击本文件 → 完成后告诉 Claude「操作完成」。
rem 全程无需手敲命令。
rem
rem 当前任务：换机 + 分支统一已完成，做一次收尾自检并出环境快照（只读，不改数据）。
rem ════════════════════════════════════════════════════════════════
echo ══════════════════════════════════════════
echo   本次命令执行：换机对齐收尾自检（只读诊断，不改数据）
echo ══════════════════════════════════════════
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

where git >nul 2>nul
if errorlevel 1 (
  echo [跳过] 未检测到 git（不在 PATH）。装好 Git 或把它加入 PATH 后重跑。
  goto snapshot
)

echo [1/4] 拉取远程引用 ...
git fetch origin

echo.
echo [2/4] 当前分支与对齐关系（应在统一分支，含全套源码）：
git rev-parse --abbrev-ref HEAD
git log --oneline -1
echo   —— 本地相对远端同名分支的领先/落后（左=落后 右=领先，0/0 才算齐）：
for /f "delims=" %%B in ('git rev-parse --abbrev-ref HEAD') do git rev-list --left-right --count origin/%%B...HEAD 2>nul
echo.

if exist "source\dub_align_studio\launcher.py" goto hascode
echo [!] 本地缺 source\ —— 项目不完整。请在 GitHub Desktop 里把分支切到
echo     claude/project-code-alignment-snapshot-jkeog1 并 Pull，再重跑本脚本。
goto snapshot

:hascode
echo [3/4] 编译自检 + 单元测试（应 331 项 OK）...
set "PYTHONPATH=source"
%PYEXE% -m compileall -q source && echo   编译全绿 || echo   [警告] 编译有错，见上方输出
%PYEXE% -m unittest discover -s tests -p "test_*.py"

:snapshot
echo.
echo [4/4] 生成环境快照（深扒本地仓 + 数据总目录体检）...
call "生成环境快照.bat"

echo.
echo ══════════════════════════════════════════
echo   收尾自检完成。请把 环境快照.txt 内容（或"快照已推送"）告诉 Claude。
echo   数据总目录记得在软件「工具箱自检」页指向：D:\GitHub\By\AU^&PR\水星配音数据
echo ══════════════════════════════════════════
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
