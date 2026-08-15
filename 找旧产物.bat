@echo off
REM ============================================================
REM  扫描本机所有可能是旧发行产物的目录，报告：
REM    - 有没有主 exe（水星配音对齐工作室.exe）
REM    - 有没有 ffmpeg.exe/ffprobe.exe
REM    - 目录大小
REM
REM  如果找到完整的旧产物，可以直接拿来喂 installer.iss，
REM  不用重装 nuitka/cython 再编译。
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "EXE_NAME=水星配音对齐工作室.exe"
set "FOUND_ANY=0"

echo.
echo ============================================================
echo   扫描目录：项目根 + 常见旧产物位置
echo ============================================================
echo.

REM 可能的候选目录（相对项目根）
set "CANDIDATES=轻量云配版包 发布包 打包产物 dist\轻量云配版包 dist\发布包 dist\打包产物 build\轻量云配版包 installer"

for %%D in (%CANDIDATES%) do (
    if exist "%%D\" (
        echo ------------------------------------------------------------
        echo [DIR] %%D\
        echo ------------------------------------------------------------

        if exist "%%D\!EXE_NAME!" (
            for %%A in ("%%D\!EXE_NAME!") do (
                set /a SZ_MB=%%~zA / 1048576
                echo   [OK] !EXE_NAME!  ^(!SZ_MB! MB^)
                set "FOUND_ANY=1"
                set "LAST_HIT=%%D"
            )
        ) else (
            echo   [-]  无 !EXE_NAME!
        )

        if exist "%%D\ffmpeg.exe" (
            for %%A in ("%%D\ffmpeg.exe") do (
                set /a SZ_MB=%%~zA / 1048576
                echo   [OK] ffmpeg.exe    ^(!SZ_MB! MB^)
            )
        ) else (
            echo   [-]  无 ffmpeg.exe
        )

        if exist "%%D\ffprobe.exe" (
            for %%A in ("%%D\ffprobe.exe") do (
                set /a SZ_MB=%%~zA / 1048576
                echo   [OK] ffprobe.exe   ^(!SZ_MB! MB^)
            )
        )

        REM 目录总大小（Windows dir /s 汇总）
        for /f "tokens=3" %%s in ('dir "%%D" /s /-c ^| find "个文件"') do (
            echo   总大小：约 %%s 字节
        )
        echo.
    )
)

echo ============================================================
if "%FOUND_ANY%"=="1" (
    echo   [OK] 找到旧产物 -^> 建议用法：
    echo.
    echo   1^) 快速复用：不重新 build，直接把旧目录喂给 installer.iss
    echo      -^> 编辑 installer.iss 顶部：
    echo         #define SourceDir "%LAST_HIT%"
    echo      -^> 然后直接跑 ISCC.exe installer.iss
    echo.
    echo   2^) 或者用我给你写好的 快捷方式：
    echo      打包_安装器_用旧产物.bat  "%LAST_HIT%"
    echo.
) else (
    echo   [X] 没找到主 exe -^> 只能重新编译
    echo   1^) pip install nuitka cython
    echo   2^) 然后双击 打包_安装器.bat
)
echo ============================================================
echo.
pause
