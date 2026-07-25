@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 本次操作 · 摸清 3050 文件夹现状（只查看，不改动）
rem ════════════════════════════════════════════════════════════════
rem 目的：3050 上有「从 3060 复制来的文件夹」和「新克隆的 GitHub 文件夹」，
rem       本脚本只【读取】现状写进日志，绝不删除/移动任何东西。
rem 用法：把本文件放到 D:\GitHub\By\AU&PR 下任意位置，双击运行；
rem       完成后把生成的「文件夹现状.txt」发给 Claude，Claude 再给出安全的合并步骤。
rem ════════════════════════════════════════════════════════════════
set "ROOT=D:\GitHub\By\AU&PR"
set "LOG=%~dp0文件夹现状.txt"

> "%LOG%" echo 3050 文件夹现状 · %DATE% %TIME%
>> "%LOG%" echo 扫描根：%ROOT%
>> "%LOG%" echo 本脚本所在：%~dp0
>> "%LOG%" echo.

call :LINE "1. git 是否安装"
where git >nul 2>&1
if errorlevel 1 (
  call :OUT "git 未安装或不在 PATH —— 这就是环境快照里 git 报 WinError 2 的原因。"
  set "HASGIT="
) else (
  set "HASGIT=1"
  for /f "delims=" %%g in ('where git') do call :OUT "git: %%g"
)

call :LINE "2. 数据总目录（真正的素材/模型/音色，务必保留）"
if exist "%ROOT%\水星配音数据" (
  call :OUT "存在：%ROOT%\水星配音数据"
) else (
  call :OUT "未在 %ROOT% 下找到 水星配音数据（可能在别处，请留意）。"
)

call :LINE "3. 扫描所有仓库文件夹（名为 AU-PR 的目录）"
for /f "delims=" %%D in ('dir /s /b /ad "%ROOT%" 2^>nul ^| findstr /i /e "\\AU-PR"') do call :INSPECT "%%D"

echo. & echo ════════ 完成：请把「%~dp0文件夹现状.txt」发给 Claude ════════
type "%LOG%"
echo.
echo （本脚本没有删除/移动任何文件。合并步骤等 Claude 看过现状后再给。）
pause
exit /b 0

:INSPECT
set "D=%~1"
>> "%LOG%" echo -- 仓库：%D%
if exist "%D%\.git" (>> "%LOG%" echo    类型：git 克隆（有 .git）) else (>> "%LOG%" echo    类型：普通文件夹/复制（无 .git）)
if exist "%D%\source\dub_align_studio" (>> "%LOG%" echo    含源码：是) else (>> "%LOG%" echo    含源码：否)
if exist "%D%\dist" (>> "%LOG%" echo    含 dist 构建：是) else (>> "%LOG%" echo    含 dist 构建：否)
if exist "%D%\source\dub_align_studio\version.py" (
  for /f "tokens=2 delims== " %%v in ('findstr /b /c:"APP_VERSION" "%D%\source\dub_align_studio\version.py"') do >> "%LOG%" echo    代码版本：%%v
)
if defined HASGIT if exist "%D%\.git" (
  pushd "%D%"
  for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do >> "%LOG%" echo    分支：%%b
  for /f "delims=" %%h in ('git rev-parse --short HEAD 2^>nul') do >> "%LOG%" echo    HEAD：%%h
  for /f "delims=" %%r in ('git remote get-url origin 2^>nul') do >> "%LOG%" echo    远端：%%r
  popd
)
>> "%LOG%" echo.
exit /b 0

:LINE
echo. & echo ════════ %~1 ════════
>> "%LOG%" echo.
>> "%LOG%" echo ════════ %~1 ════════
exit /b 0

:OUT
echo   %~1
>> "%LOG%" echo   %~1
exit /b 0
