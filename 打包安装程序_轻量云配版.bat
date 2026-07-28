@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Mercury Dub Align - Lite Cloud Installer

echo ============================================================
echo   Mercury Dub Align - Lite Cloud Installer
echo ============================================================
echo.
echo This script turns the lite cloud package into a Setup.exe installer.
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found. Install Python 3.11+ and add it to PATH.
  pause
  exit /b 1
)

python "%~dp0tools\package_lite_cloud_installer.py"
set "CODE=%ERRORLEVEL%"
echo.
if not "%CODE%"=="0" (
  echo [FAILED] Installer packaging failed.
) else (
  echo [OK] Installer is ready.
)
pause
exit /b %CODE%
