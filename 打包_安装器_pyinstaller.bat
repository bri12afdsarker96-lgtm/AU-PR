@echo off
REM ============================================================
REM  用 PyInstaller 打包（含激活码 + RASP + 完整性 + 图标）
REM  跳过 nuitka（Python 3.14 装不上），走已装的 pyinstaller。
REM
REM  流程：
REM    Step 1  spec 定位（优先 installer\dub_align_studio_lite.spec）
REM    Step 2  pyinstaller <spec>  -> dist\水星配音对齐工作室_云配版\
REM    Step 3  产物防护补齐：
REM              - 敏感文件清扫（.py .pyc settings.json license.json ...）
REM              - 内嵌 ffmpeg.exe/ffprobe.exe（./tools/ffmpeg 或 PATH）
REM              - 生成 启动软件.bat（DUB_ALIGN_LICENSE_REQUIRED=1 + RASP_STRICT=1）
REM              - 生成 integrity.hash（供 RASP 完整性自检）
REM              - 验证 licensing/ 模块已被打进 exe
REM    Step 4  ISCC.exe 编译 installer
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

REM 新 spec（含激活码 + RASP + 完整数据 + 图标）优先
set "SPEC_NEW=installer\dub_align_studio_lite.spec"
REM 老 spec（无激活码，仅兼容旧流程）兜底
set "SPEC_OLD=水星配音对齐工作室_云配版.spec"

set "PYI_DIST=dist\水星配音对齐工作室_云配版"
set "PYI_EXE=水星配音对齐工作室_云配版.exe"

REM ---------- Step 1  spec 定位 ----------
echo.
echo ============================================================
echo   Step 1/4  定位 pyinstaller spec
echo ============================================================
set "SPEC="
if exist "%SPEC_NEW%" (
    set "SPEC=%SPEC_NEW%"
    echo   [OK] 用新 spec ^(含激活码/RASP/图标^): %SPEC_NEW%
    goto :have_spec
)
if exist "%SPEC_OLD%" (
    set "SPEC=%SPEC_OLD%"
    echo   [!] 用老 spec ^(可能不含新加的 licensing/RASP^): %SPEC_OLD%
    echo       -^> 强烈建议 git pull 拿最新的 installer\dub_align_studio_lite.spec
    goto :have_spec
)
echo   [X] 都找不到 spec 文件
echo       期望：%SPEC_NEW%  或  %SPEC_OLD%
pause
exit /b 1

:have_spec

REM ---------- 老 spec 的 marker 文件预检 ----------
if not exist "installer\cloud_edition.flag" (
    echo   [补] installer\cloud_edition.flag ^(老 spec marker，非致命^)
    echo. > "installer\cloud_edition.flag"
)

REM ---------- Step 1.5  写 _build_info.py（PACKAGED + 随机密钥；PYZ 会打进去） ----------
echo.
echo ============================================================
echo   Step 1.5  写 source/dub_align_studio/_build_info.py
echo   （必须在 pyinstaller 之前，才能被 PYZ 归档 -^> 运行时 import 得到）
echo ============================================================
python -c "import sys; sys.path.insert(0, '.'); from build_dist import prepare_build_info; prepare_build_info()"
if errorlevel 1 (
    echo   [X] prepare_build_info 失败 -^> 防护会被绕过，中止打包
    pause
    exit /b 7
)

REM ---------- Step 1.9  Cython 编译 licensing（强化反破译） ----------
echo.
echo ============================================================
echo   Step 1.9  Cython 编译 licensing/*.py -^> .pyd
echo   （攻击者拿到 .pyd 无源码可看，比 .pyc 反编译难得多）
echo ============================================================
python -c "import Cython" 2>nul
if errorlevel 1 (
    echo   [!] Cython 未装 -^> licensing/*.py 会以 .pyc 打包（可反编译）
    set /p CY="       现在装吗？（Y=装 Cython N=跳过 弱化防护）: "
    if /i "!CY!"=="Y" (
        python -m pip install cython
    )
)
python -c "import Cython" 2>nul
if not errorlevel 1 (
    python -c "import sys; sys.path.insert(0, '.'); from build_dist import cython_compile_licensing; cython_compile_licensing()"
    if errorlevel 1 (
        echo   [!] Cython 编译失败——继续但 licensing 只有 .pyc 弱保护
    ) else (
        echo   [OK] licensing/*.pyd 已生成
    )
) else (
    echo   [!] 无 Cython，跳过（licensing 只有 .pyc；可用但反编译难度低）
)

REM ---------- Step 2  pyinstaller ----------
echo.
echo ============================================================
echo   Step 2/4  PyInstaller 编译（可能 3-10 分钟）
echo ============================================================
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo   [X] 当前 python 没装 pyinstaller: python -m pip install pyinstaller
    where python
    pause
    exit /b 2
)
for /f "delims=" %%v in ('python -m PyInstaller --version 2^>nul') do echo   [OK] pyinstaller %%v

REM pywebview 自检（原生窗口模式必需；缺就问一下装不装）
python -c "import webview" 2>nul
if errorlevel 1 (
    echo.
    echo   [!] 未装 pywebview -^> 打出的 exe 只能浏览器模式（不能原生窗口）
    set /p WV="       现在装吗？（Y=装 pywebview N=跳过）: "
    if /i "!WV!"=="Y" (
        python -m pip install pywebview
        python -c "import webview" 2>nul
        if errorlevel 1 (
            echo   [!] pywebview 装完仍 import 失败，继续但只能浏览器
        ) else (
            echo   [OK] pywebview 装好
        )
    )
) else (
    for /f "delims=" %%v in ('python -c "import webview; print(webview.__version__)" 2^>nul') do echo   [OK] pywebview %%v
)

if exist "%PYI_DIST%\" (
    echo   清理旧 %PYI_DIST%\
    rmdir /S /Q "%PYI_DIST%"
)
python -m PyInstaller --clean --noconfirm "%SPEC%"
if errorlevel 1 (
    echo   [X] pyinstaller 失败 exit=%errorlevel%
    pause
    exit /b %errorlevel%
)
if not exist "%PYI_DIST%\%PYI_EXE%" (
    echo   [X] 主 exe 未生成: %PYI_DIST%\%PYI_EXE%
    pause
    exit /b 3
)
echo   [OK] %PYI_DIST%\%PYI_EXE%

REM ---------- Step 3  产物防护补齐 ----------
echo.
echo ============================================================
echo   Step 3/4  产物防护补齐（清扫 + ffmpeg + 启动脚本 + hash + 验证）
echo ============================================================

REM 3.1  敏感文件清扫
python -c "import sys; sys.path.insert(0, '.'); from build_dist import scrub_sensitive; from pathlib import Path; n = scrub_sensitive(Path(r'%PYI_DIST%')); print(f'  [OK] 敏感文件清扫 {n} 项')"

REM 3.2  内嵌 ffmpeg
python -c "import sys; sys.path.insert(0, '.'); from build_dist import bundle_ffmpeg; from pathlib import Path; n = bundle_ffmpeg(Path(r'%PYI_DIST%')); print(f'  [OK] ffmpeg 内嵌 {n} 个')"

REM 3.3  验证 licensing/ 已被打进 _internal
if exist "%PYI_DIST%\_internal\dub_align_studio\licensing\" (
    echo   [OK] licensing/ 模块已进入 _internal
    dir /B "%PYI_DIST%\_internal\dub_align_studio\licensing\" | findstr /R "\." >nul && (
        for /f %%c in ('dir /B "%PYI_DIST%\_internal\dub_align_studio\licensing\" ^| find /C /V ""') do (
            echo        找到 %%c 个 licensing 子模块文件
    dir /B "%PYI_DIST%\_internal\dub_align_studio\licensing\" 2>nul | findstr /R "\.pyd$ \.so$" >nul && (
        echo   [OK] 包含 Cython 编译产物 ^(.pyd/.so^) —— 反编译难度显著提升
    ) || (
        echo   [!] 未发现 .pyd/.so —— licensing 只有 .pyc，反编译难度低
    )
        )
    )
) else (
    echo   [X] licensing/ 模块未打进 _internal —— 激活码 gate 不会生效！
    echo       spec 里 hiddenimports 应含 dub_align_studio.licensing.*
    echo       用 installer\dub_align_studio_lite.spec 就自带完整列表
    pause
    exit /b 5
)

REM 3.4  生成 启动软件.bat（含发行必需的 env）
python -c "import sys; sys.path.insert(0, '.'); from build_dist import write_launcher_bat; from pathlib import Path; write_launcher_bat(Path(r'%PYI_DIST%'), r'%PYI_EXE%'); print('  [OK] 启动软件.bat')"

REM 3.4b  个人数据泄漏保底扫描（settings.json / workers.dev / 服务器 IP）
python -c "import sys; sys.path.insert(0, '.'); from build_dist import verify_no_personal_data; from pathlib import Path; v = verify_no_personal_data(Path(r'%PYI_DIST%'));  print('  [OK] 无个人数据泄漏') if not v else (print('  [X] 发现个人数据泄漏：'), [print('     - '+x) for x in v], exit(1))"
if errorlevel 1 (
    echo   [X] 个人数据检查未通过 —— 中止打包（防止把你的 Edge TTS URL / 激活码泄漏给下游用户）
    pause
    exit /b 8
)

REM 3.5  算 exe HMAC baseline 写到 sidecar dist/_build_hmac.dat
REM      key 藏在 Step 1.5 生成的 _build_info.py（打进 PYZ），
REM      sidecar 只放 HMAC 值本身，攻击者伪造需先解 PYZ 拿 key
python -c "import sys; sys.path.insert(0, '.'); from build_dist import finalize_build_info_hmac; from pathlib import Path; finalize_build_info_hmac(Path(r'%PYI_DIST%'), r'%PYI_EXE%')"
if errorlevel 1 (
    echo   [!] finalize_build_info_hmac 失败 —— integrity_check 会跳过
    echo       其他防护层（gate/RASP/Cython）仍生效，可继续
)

REM ---------- Step 4  ISCC ----------
echo.
echo ============================================================
echo   Step 4/4  Inno Setup 编译
echo ============================================================
if not exist "installer\ChineseSimplified.isl" (
    echo   [!] installer\ChineseSimplified.isl 缺失 -^> 中文向导降级英文
)

set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo   [X] 未找到 ISCC.exe: https://jrsoftware.org/isdl.php
    pause
    exit /b 4
)

"%ISCC%" /DSourceDir="%PYI_DIST%" /DAppExeName="%PYI_EXE%" installer.iss
if errorlevel 1 (
    echo   [X] ISCC 失败 exit=%errorlevel%
    pause
    exit /b %errorlevel%
)

echo.
echo ============================================================
echo   [OK] 打包完成
echo ============================================================
REM 清理 source/_build_info.py（保持仓库干净，密钥不入库）
python -c "import sys; sys.path.insert(0, '.'); from build_dist import cleanup_build_info; cleanup_build_info()"

echo   产物：dist\安装器\setup_水星配音对齐工作室_v0.7.71_lite.exe
echo.
echo   已启用的防护：
echo     - 激活码 gate  ^(DUB_ALIGN_LICENSE_REQUIRED=1，启动软件.bat 里^)
echo     - RASP 反调试  ^(DUB_ALIGN_RASP_STRICT=1，启动软件.bat 里^)
echo     - 完整性自检   ^(integrity.hash 已写入^)
echo     - 敏感文件清扫 ^(无 .py/settings.json/license.json^)
echo     - HWID + Nonce + HMAC + DPAPI  ^(客户端代码已含^)
echo     - 原生窗口 ^(pywebview + WebView2，无浏览器 chrome^)
echo.
echo   云端后台请录入：
echo     app_id = dub_align_studio
echo     server = http://101.201.108.8:8001
echo.
pause
exit /b 0
