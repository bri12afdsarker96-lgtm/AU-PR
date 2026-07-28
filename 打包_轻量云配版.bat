@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Mercury Dub Align - Lite Cloud Pack

echo ============================================================
echo   Mercury Dub Align - Lite Cloud Pack
echo ============================================================
echo.
echo This pack is for cloud TTS: no local torch/dots/fish models.
echo It keeps the local web UI, ffmpeg/ffprobe, whisper timing, and pyCapCut support.
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found. Install Python 3.11+ and add it to PATH.
  pause
  exit /b 1
)

python "%~dp0tools\package_lite_cloud.py"
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" (
  echo [FAILED] Lite cloud packaging failed.
) else (
  echo [OK] Lite cloud package is ready.
)
pause
exit /b %CODE%
