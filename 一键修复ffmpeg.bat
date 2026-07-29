@echo off
setlocal EnableDelayedExpansion
title 一键修复 ffmpeg（让软件找得到）
color 0B
echo ============================================================
echo      一键修复 ffmpeg —— 让「水星配音」渲染时找得到 ffmpeg
echo ============================================================
echo.
echo  原理：把 ffmpeg.exe/ffprobe.exe 放进"已在 PATH 上、用户可写"的目录，
echo        正在运行的软件也能立刻找到，不用改打包、不用手配环境变量。
echo.

set "DEST1=%LOCALAPPDATA%\Microsoft\WindowsApps"
set "DEST2=%LOCALAPPDATA%\MercuryFFmpeg\bin"
set "FFM="
set "FFP="

echo [1/3] 在常见位置查找现成的 ffmpeg.exe / ffprobe.exe ...
call :find "%~dp0."
call :find "%~dp0bin"
call :find "%~dp0ffmpeg\bin"
call :find "%USERPROFILE%\Downloads"
if not defined FFM for /f "delims=" %%f in ('where ffmpeg 2^>nul') do if not defined FFM set "FFM=%%f"
if not defined FFP for /f "delims=" %%f in ('where ffprobe 2^>nul') do if not defined FFP set "FFP=%%f"

if defined FFM if defined FFP goto INSTALL

echo [2/3] 本机没找到，正在从 gyan.dev 下载 ffmpeg（约 80MB，请稍候）...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile ($env:TEMP+'\ff.zip'); Remove-Item -Recurse -Force ($env:TEMP+'\ffx') -ErrorAction SilentlyContinue; Expand-Archive -Force ($env:TEMP+'\ff.zip') ($env:TEMP+'\ffx')"
if errorlevel 1 goto DLFAIL
for /r "%TEMP%\ffx" %%f in (ffmpeg.exe) do if not defined FFM set "FFM=%%f"
for /r "%TEMP%\ffx" %%f in (ffprobe.exe) do if not defined FFP set "FFP=%%f"
if not defined FFM goto DLFAIL
if not defined FFP goto DLFAIL

:INSTALL
echo.
echo [3/3] 安装到 PATH 目录 ...
echo   源 ffmpeg : !FFM!
echo   源 ffprobe: !FFP!
mkdir "%DEST2%" 2>nul
copy /y "!FFM!" "%DEST1%\ffmpeg.exe"  >nul 2>nul
copy /y "!FFP!" "%DEST1%\ffprobe.exe" >nul 2>nul
copy /y "!FFM!" "%DEST2%\ffmpeg.exe"  >nul
copy /y "!FFP!" "%DEST2%\ffprobe.exe" >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d='%DEST2%'; $p=[Environment]::GetEnvironmentVariable('Path','User'); if(-not $p){$p=''}; if($p -notlike ('*'+$d+'*')){[Environment]::SetEnvironmentVariable('Path',($p.TrimEnd(';')+';'+$d),'User')}"

echo.
echo [验证] where ffmpeg ：
where ffmpeg 2>nul || echo   （本窗口可能还没刷新，新开的软件会生效）
echo.
echo ============================================================
echo   完成！ffmpeg 已就位：
echo     %DEST1%
echo     %DEST2%  （已加入用户 PATH）
echo.
echo   下一步：回软件点失败任务的「重试」——多数情况立刻就好。
echo   若仍报"未找到"：把软件【完全关闭再重新打开】一次，再点重试。
echo ============================================================
echo.
pause
exit /b 0

:find
if defined FFM if defined FFP goto :eof
if not defined FFM if exist "%~1\ffmpeg.exe"  set "FFM=%~1\ffmpeg.exe"
if not defined FFP if exist "%~1\ffprobe.exe" set "FFP=%~1\ffprobe.exe"
goto :eof

:DLFAIL
echo.
echo [下载失败] 网络不通或 gyan.dev 访问受限。
echo 请手动下载 ffmpeg-release-essentials.zip，解压后把 bin 里的
echo ffmpeg.exe / ffprobe.exe 两个文件和本 bat 放到同一个文件夹，再双击本 bat。
echo 地址：https://www.gyan.dev/ffmpeg/builds/
echo.
pause
exit /b 1
