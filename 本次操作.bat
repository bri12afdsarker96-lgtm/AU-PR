@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 本次操作 · 迁到干净路径 D:\Mercury（A 方案）
setlocal
rem ════════════════════════════════════════════════════════════════
rem A 方案：把「数据总目录」搬到无 & 的纯英文路径 D:\Mercury，
rem         仓库改用 GitHub Desktop 重新克隆（干净、能同步）。
rem 本脚本【只安全搬数据】：同盘搬移=瞬间且可逆；不动仓库代码、不删任何东西。
rem 仓库克隆与删旧目录按下方提示手动做（都很简单）。
rem ════════════════════════════════════════════════════════════════
set "OLD=D:\GitHub\By\AU&PR"
set "OLDDATA=%OLD%\水星配音数据"
set "NEWROOT=D:\Mercury"
set "NEWDATA=%NEWROOT%\水星配音数据"

echo ════════════════════════════════════════════════
echo  即将把数据搬到无 ^& 的干净路径：
echo    从  "%OLDDATA%"
echo    到  "%NEWDATA%"
echo  （同盘搬移，瞬间完成、可逆；不动仓库、不删任何文件。）
echo ════════════════════════════════════════════════
echo.

rem 关掉可能占用数据的软件，避免搬移被锁
taskkill /f /im "水星配音对齐工作室.exe" >nul 2>nul

if not exist "%OLDDATA%" (
  echo [跳过搬移] 没找到 "%OLDDATA%" —— 可能已搬过或不在此处。
  goto GUIDE
)
if exist "%NEWDATA%" (
  echo [中止] 目标已存在 "%NEWDATA%"。为防覆盖，请先自行处理后再运行本脚本。
  pause
  exit /b 1
)

choice /c YN /m "确认搬移数据？Y=执行  N=取消"
if errorlevel 2 (
  echo 已取消，未做任何改动。
  pause
  exit /b 0
)

if not exist "%NEWROOT%" mkdir "%NEWROOT%"
move "%OLDDATA%" "%NEWDATA%"
if errorlevel 1 (
  echo [失败] 搬移未成功（可能有程序占用或权限不足）。数据仍在原处，未丢失。
  pause
  exit /b 1
)
echo [成功] 数据已搬到 "%NEWDATA%"

:GUIDE
echo.
echo ════════ 接下来（手动，一次到位）════════
echo  1) 安装 GitHub Desktop（自带 git、能登录拉取、图形界面 pull/push）。
echo  2) 用它 Clone 仓库到  "%NEWROOT%\AU-PR"（选 release/v0.5-download-fix 或 3050 分支）。
echo     新克隆在 D:\Mercury\AU-PR，其上一级 D:\Mercury 就是刚搬来的数据，软件会自动识别。
echo  3) 在 "%NEWROOT%\AU-PR" 里启动软件，确认能配音 / 出片 / 字幕正常（无 ^& 路径，字幕不再 □□□）。
echo  4) 全部确认无误后，再删掉旧目录（此前一直不动，确保可回退）：
echo        rmdir /s /q "%OLD%"
echo.
echo （渲染成片仍需把 ffmpeg.exe / ffprobe.exe 放到软件目录或系统 PATH。）
pause
exit /b 0
