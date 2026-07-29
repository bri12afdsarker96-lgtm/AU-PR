@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 本次命令执行 · 换机对齐首跑
rem ════════════════════════════════════════════════════════════════
rem 协作约定：Claude 需要你在本地执行什么，就更新本文件并推到 GitHub。
rem 你只需：git pull → 双击本文件 → 完成后告诉 Claude「操作完成」。
rem 当前预置任务：换新电脑后的首次对齐自检（只读诊断，不动你的数据）。
rem ════════════════════════════════════════════════════════════════
echo ══════════════════════════════════════════
echo   本次命令执行：换机对齐首跑（只做诊断，不改数据）
echo ══════════════════════════════════════════
echo.

where git >nul 2>nul
if errorlevel 1 (
  echo [跳过] 未检测到 git（不在 PATH）。装好 Git 或把它加入 PATH 后重跑。
  goto snapshot
)

echo [1/4] 拉取远程引用 ...
git fetch origin

echo.
echo [2/4] 当前分支与对齐关系（真源应为 release/v0.5-download-fix，不是 main）：
git rev-parse --abbrev-ref HEAD
git log --oneline -1
echo   —— 本地相对真源 v0.5 的领先/落后（左=落后 右=领先）：
git rev-list --left-right --count origin/release/v0.5-download-fix...HEAD 2>nul
echo.

if exist "source\dub_align_studio\launcher.py" goto hascode
echo [!] 本地缺 source\ —— 极可能停在 main（只有文档）。对齐命令：
echo       git checkout release/v0.5-download-fix ^&^& git pull
echo     确认无未提交改动后可直接切换。切换后重跑本脚本。
goto snapshot

:hascode
echo [3/4] 编译自检 + 单元测试（真源应 129 项全绿）...
set "PYTHONPATH=source"
python -m compileall -q source && echo   编译全绿 || echo   [警告] 编译有错，见上方输出
python -m unittest discover -s tests -p "test_*.py"

:snapshot
echo.
echo [4/4] 生成环境快照（深扒本地仓 + 数据总目录体检）...
call "生成环境快照.bat"

echo.
echo ══════════════════════════════════════════
echo   对齐首跑完成。请把 环境快照.txt 内容（或"快照已推送"）告诉 Claude。
echo   数据总目录记得在软件「工具箱自检」页指向：D:\GitHub\By\AU^&PR\水星配音数据
echo ══════════════════════════════════════════
pause
