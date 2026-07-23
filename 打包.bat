@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AU-PR Build

echo ==========================================
echo   AU-PR build
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 goto :NOPY

python build_app.py
set "ERR=%ERRORLEVEL%"
if not "%ERR%"=="0" goto :BUILDFAIL

echo.
echo [OK] dist package created.
echo [OK] release zip is in the parent release folder.
pause
exit /b 0

:NOPY
echo [ERROR] Python was not found. Install Python 3.11+ and enable PATH.
pause
exit /b 1

:BUILDFAIL
echo.
echo [ERROR] Build failed, exit code: %ERR%
echo Check the first error message above.
pause
exit /b %ERR%
