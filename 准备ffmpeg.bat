@echo off
REM ============================================================
REM  准备打包用的 ffmpeg（必须带 h264_nvenc 才能启用 GPU 编码）
REM
REM  做什么：
REM    1. 找本机 ffmpeg（PATH 里的）
REM    2. 验证它带不带 h264_nvenc（NVIDIA 硬件编码）
REM    3. 带 → 拷贝 ffmpeg.exe + ffprobe.exe 到 tools\ffmpeg\
REM       （build_dist.py 打包时优先用这里的，不再靠 PATH 猜）
REM    4. 不带 → 提示去 gyan.dev 下 full 版
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   Step 1  找本机 ffmpeg
echo ============================================================
where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo   [X] PATH 里找不到 ffmpeg
    echo       请先装：winget install Gyan.FFmpeg   或去 https://www.gyan.dev/ffmpeg/builds/
    echo       下 ffmpeg-release-full 版，解压后把 bin 加进 PATH，再跑本脚本
    pause
    exit /b 1
)
for /f "delims=" %%i in ('where ffmpeg') do (
    set "FFMPEG_EXE=%%i"
    goto :got_ffmpeg
)
:got_ffmpeg
echo   [OK] 找到：!FFMPEG_EXE!
for %%i in ("!FFMPEG_EXE!") do set "FFMPEG_DIR=%%~dpi"
set "FFPROBE_EXE=!FFMPEG_DIR!ffprobe.exe"

echo.
echo ============================================================
echo   Step 2  验证是否带 h264_nvenc（NVIDIA 硬件编码）
echo ============================================================
"!FFMPEG_EXE!" -hide_banner -encoders 2>nul | findstr /C:"h264_nvenc" >nul
if errorlevel 1 (
    echo   [X] 这个 ffmpeg **不带** h264_nvenc（多半是 essentials 裁剪版）
    echo.
    echo       换 full 版：
    echo         1^) https://www.gyan.dev/ffmpeg/builds/
    echo         2^) 下 "ffmpeg-release-full.7z"（不是 essentials）
    echo         3^) 解压，把里面 bin\ffmpeg.exe + bin\ffprobe.exe
    echo            拷到本项目 tools\ffmpeg\ 下
    echo         4^) 再跑本脚本验证
    echo.
    echo       或用 winget 装 full 版：winget install Gyan.FFmpeg
    pause
    exit /b 2
)
echo   [OK] 带 h264_nvenc —— 支持 NVIDIA 硬件编码

echo.
echo ============================================================
echo   Step 3  拷贝到 tools\ffmpeg\
echo ============================================================
if not exist "tools\ffmpeg" mkdir "tools\ffmpeg"
copy /Y "!FFMPEG_EXE!" "tools\ffmpeg\ffmpeg.exe" >nul
if errorlevel 1 (
    echo   [X] 拷 ffmpeg.exe 失败
    pause
    exit /b 3
)
echo   [OK] ffmpeg.exe -^> tools\ffmpeg\

if exist "!FFPROBE_EXE!" (
    copy /Y "!FFPROBE_EXE!" "tools\ffmpeg\ffprobe.exe" >nul
    echo   [OK] ffprobe.exe -^> tools\ffmpeg\
) else (
    echo   [!] 同目录没 ffprobe.exe（!FFPROBE_EXE!）
    echo       full 版一般自带；若缺，从同一个 ffmpeg 包的 bin 里拷过来
)

echo.
echo ============================================================
echo   [OK] 完成！tools\ffmpeg\ 已备好带 nvenc 的 ffmpeg
echo ============================================================
echo   下一步：跑 打包_安装器_pyinstaller.bat 打包
echo   打出来的软件在「硬件编码」选 NVIDIA（或自动）即走显卡编码。
echo.
pause
exit /b 0
