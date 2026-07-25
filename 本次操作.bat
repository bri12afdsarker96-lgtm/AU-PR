@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 本次操作 · 清理旧的两个冗余文件夹（保留 D:\Mercury）
setlocal
rem ════════════════════════════════════════════════════════════════
rem 不用合并：只保留 D:\Mercury（新代码 + 数据/模型），删掉旧的 D:\GitHub\By\AU&PR
rem （里面是「3060 拷贝」+「嵌套克隆」两个冗余，数据/模型早已搬到 D:\Mercury）。
rem 安全闸：先确认 D:\Mercury 的模型/音色/字体都在，才允许删；否则中止、不删任何东西。
rem ════════════════════════════════════════════════════════════════
set "KEEP=D:\Mercury"
set "KEEPDATA=%KEEP%\水星配音数据"
set "OLD=D:\GitHub\By\AU&PR"

echo ════════ 计划 ════════
echo  保留（正在用、已验证）：
echo    "%KEEP%\AU-PR"        代码
echo    "%KEEPDATA%"          数据+模型
echo  待删（都是冗余、数据是副本）：
echo    "%OLD%"               （含 3060 拷贝 + 嵌套克隆）
echo.

rem —— 安全闸：D:\Mercury 的关键模型/音色/字体必须齐，才允许删旧 ——
set "SAFE=1"
if not exist "%KEEPDATA%\组件\dots.tts\dots.tts-soar" set "SAFE="
if not exist "%KEEPDATA%\音色库" set "SAFE="
if not exist "%KEEPDATA%\字体" set "SAFE="
if not defined SAFE (
  echo [中止] 没在 "%KEEPDATA%" 里确认到 dots.tts 模型 / 音色库 / 字体，
  echo        为防误删，本次不删任何东西。请先确认 D:\Mercury 的数据完整再运行。
  pause
  exit /b 1
)
echo [安全闸通过] D:\Mercury 的 dots.tts 模型 / 音色库 / 字体 均在。

echo.
echo —— 旧目录里若还有「水星配音数据」旧副本，先列出来（都是副本，真数据在 D:\Mercury）——
if exist "%OLD%" for /f "delims=" %%d in ('dir /s /b /ad "%OLD%" 2^>nul ^| findstr /i /e "\\水星配音数据"') do echo    副本："%%d"

echo.
if not exist "%OLD%" ( echo 旧目录 "%OLD%" 已不存在，无需清理。& pause & exit /b 0 )
choice /c YN /m "确认删除旧目录（含两个冗余文件夹）？Y=删  N=不删"
if errorlevel 2 ( echo 未删除。稍后可手动执行： rmdir /s /q "%OLD%" & pause & exit /b 0 )

taskkill /f /im "水星配音对齐工作室.exe" >nul 2>nul
rmdir /s /q "%OLD%"
if exist "%OLD%" (
  echo [部分失败] "%OLD%" 仍在——可能有资源管理器窗口/程序占用。关掉后重试即可。
) else (
  echo [完成] 旧目录已删除。现在只剩干净的 D:\Mercury\AU-PR + D:\Mercury\水星配音数据。
)
pause
exit /b 0
