@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 本次操作 · 摸清 3050 文件夹现状（只查看，不改动）
rem ════════════════════════════════════════════════════════════════
rem 只【读取】现状写进日志，绝不删除/移动任何东西。
rem 路径里的 & 会截断 echo，所有含路径的输出一律加引号包住（&-safe）。
rem 用法：放到 D:\GitHub\By\AU&PR 下双击；把生成的「文件夹现状.txt」发给 Claude。
rem ════════════════════════════════════════════════════════════════
set "ROOT=D:\GitHub\By\AU&PR"
set "LOG=%~dp0文件夹现状.txt"

> "%LOG%" echo 3050 文件夹现状 · %DATE% %TIME%
>> "%LOG%" echo 扫描根："%ROOT%"
>> "%LOG%" echo 本脚本所在："%~dp0"
>> "%LOG%" echo.

>> "%LOG%" echo ════════ 1. git 是否安装 ════════
where git >nul 2>&1
if errorlevel 1 (
  >> "%LOG%" echo   git 未安装或不在 PATH —— 这就是快照里 git 报 WinError 2、无法 pull/push 的原因。
  set "HASGIT="
) else (
  set "HASGIT=1"
  for /f "delims=" %%g in ('where git') do >> "%LOG%" echo   git: "%%g"
)

>> "%LOG%" echo.
>> "%LOG%" echo ════════ 2. 数据总目录（务必保留）════════
if exist "%ROOT%\水星配音数据" (>> "%LOG%" echo   存在："%ROOT%\水星配音数据") else (>> "%LOG%" echo   未在扫描根下找到 水星配音数据)

>> "%LOG%" echo.
>> "%LOG%" echo ════════ 3. 所有 AU-PR 仓库文件夹 ════════
for /f "delims=" %%D in ('dir /s /b /ad "%ROOT%" 2^>nul ^| findstr /i /e "\\AU-PR"') do call :INSPECT "%%~D"

echo 完成：请把 "%~dp0文件夹现状.txt" 发给 Claude（本脚本没有删除/移动任何文件）。
type "%LOG%"
pause
exit /b 0

:INSPECT
set "D=%~1"
>> "%LOG%" echo.
>> "%LOG%" echo -- 仓库："%D%"
if exist "%D%\.git" (>> "%LOG%" echo    类型：git 克隆（有 .git）) else (>> "%LOG%" echo    类型：复制/无 .git)
if exist "%D%\source\dub_align_studio" (>> "%LOG%" echo    含源码：是) else (>> "%LOG%" echo    含源码：否)
if exist "%D%\dist" (>> "%LOG%" echo    含 dist 构建：是) else (>> "%LOG%" echo    含 dist 构建：否)
if exist "%D%\source\dub_align_studio\version.py" for /f "tokens=2 delims== " %%v in ('findstr /b /c:"APP_VERSION" "%D%\source\dub_align_studio\version.py"') do >> "%LOG%" echo    代码版本：%%v
if defined HASGIT if exist "%D%\.git" (
  pushd "%D%"
  for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD 2^>nul') do >> "%LOG%" echo    分支：%%b
  for /f "delims=" %%h in ('git rev-parse --short HEAD 2^>nul') do >> "%LOG%" echo    HEAD：%%h
  for /f "delims=" %%r in ('git remote get-url origin 2^>nul') do >> "%LOG%" echo    远端："%%r"
  popd
)
exit /b 0
