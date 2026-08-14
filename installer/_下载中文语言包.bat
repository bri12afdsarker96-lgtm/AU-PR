@echo off
REM ============================================================
REM  下载 Inno Setup Chinese Simplified 语言文件
REM  下载到 installer\ChineseSimplified.isl（installer.iss 优先用它）
REM
REM  只需在打包机上跑一次。运行完后 打包_安装器.bat 就能出中文向导。
REM ============================================================

setlocal
cd /d "%~dp0"

set "URL=https://raw.githubusercontent.com/jrsoftware/issrc/main/Files/Languages/ChineseSimplified.isl"
set "DEST=%~dp0ChineseSimplified.isl"

echo.
echo   下载：%URL%
echo   到：  %DEST%
echo.

REM PowerShell 兜底（Win10+ 都自带）
where curl >nul 2>&1
if %errorlevel%==0 (
    curl -fL --retry 3 -o "%DEST%" "%URL%"
    if errorlevel 1 goto :err_dl
) else (
    powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%URL%' -OutFile '%DEST%' -UseBasicParsing } catch { exit 1 }"
    if errorlevel 1 goto :err_dl
)

if not exist "%DEST%" goto :err_dl

for %%A in ("%DEST%") do set "SZ=%%~zA"
echo.
echo   [OK] 下载完成（%SZ% 字节）
echo   之后跑 打包_安装器.bat，安装向导自动变中文。
echo.
pause
exit /b 0

:err_dl
echo.
echo   [X] 下载失败。可能：
echo       1) 网络不通 raw.githubusercontent.com  -^> 挂 VPN 再试
echo       2) 浏览器手工下载：
echo          %URL%
echo          保存为：%DEST%
echo.
pause
exit /b 1
