@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title 水星配音对齐工作室 · 轻量云配版 · 一键打包（目录版 + 安装程序）
echo ============================================================
echo   一键打包（轻量云配版）
echo   两步：① 出目录版（可直接跑） → ② 出 Inno 安装程序（可分发）
echo ============================================================
echo.
echo 前置：本机装 Python 3.11+（勾 Add to PATH）+ Inno Setup 6（第②步用）。
echo       无 Inno Setup 也不会中断——仅跳过第②步，目录版照常出。
echo       任何细节口径参见「打包_轻量云配版.bat / 打包安装程序_轻量云配版.bat」。
echo.
pause

rem ──── 定位 Python（拒 Store 版：不可靠）
set "PYHOME="
for /f "usebackq delims=" %%i in (`python -c "import sys;print(sys.base_prefix)" 2^>nul`) do set "PYHOME=%%i"
if not defined PYHOME ( echo [错误] 未找到 python，请确认已装并加 PATH。& pause & exit /b 1 )
echo %PYHOME% | findstr /i "WindowsApps" >nul && ( echo [错误] 检测到 Microsoft Store 版 Python（不可靠），请装 python.org 版。& pause & exit /b 1 )
echo [信息] 用于打包的 Python：%PYHOME%

set "VER=0.0.0"
for /f "usebackq delims=" %%v in (`python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import APP_VERSION;print(APP_VERSION)"`) do set "VER=%%v"
echo [信息] 版本：%VER%

set "PKGROOT=打包产物"
set "PKG=%PKGROOT%\水星配音对齐工作室_轻量云配版_v%VER%"

echo.
echo ============================================================
echo   ① 目录版打包
echo ============================================================
echo [重建] %PKG% ...
rd /s /q "%PKG%" 2>nul
mkdir "%PKG%" 2>nul

echo [1/6] 复制 Python 运行环境（预剔除 torch/CUDA/dots.tts 等本机大件，6~12GB 起→数百 MB）...
robocopy "%PYHOME%" "%PKG%\python" /e /nfl /ndl /njh /njs /nc /ns /xd torch torchaudio torchvision torchgen functorch triton nvidia dots_tts transformers tokenizers accelerate safetensors fish_speech xformers flash_attn cusparselt cudnn >nul
if errorlevel 8 ( echo [错误] 复制 Python 失败。& pause & exit /b 1 )

echo [2/6] 二次深清 site-packages（长路径 + 兜底把 torch/CUDA 彻底删掉）...
set "SP=%CD%\%PKG%\python\Lib\site-packages"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$sp='%SP%'; if(Test-Path -LiteralPath $sp){Get-ChildItem -LiteralPath $sp -Directory | Where-Object {$_.Name -match '^(torch|nvidia|triton|dots_tts|dots\.tts|transformers|tokenizers|accelerate|safetensors|functorch|torchgen|fish_speech|xformers|flash_attn|cusparselt|cudnn)'} | ForEach-Object {Remove-Item -LiteralPath ('\\?\'+$_.FullName) -Recurse -Force -ErrorAction SilentlyContinue}}"

echo [3/6] 复制项目源码 + 服务端启动脚本 ...
robocopy "source" "%PKG%\source" /e /nfl /ndl /njh /njs /nc /ns /xd __pycache__ >nul
if exist "server" robocopy "server" "%PKG%\server" /e /nfl /ndl /njh /njs /nc /ns >nul

echo [4/6] 复制数据总目录（保 whisper/音效；剔除 dots.tts/fish-speech/torch/字体/音色库 等本机专用）...
set "DATADIR="
for /f "usebackq delims=" %%d in (`python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.settings import data_root;print(data_root())"`) do set "DATADIR=%%d"
if defined DATADIR if exist "%DATADIR%" (
  robocopy "%DATADIR%" "%PKG%\水星配音数据" /e /nfl /ndl /njh /njs /nc /ns /xd "dots.tts" "fish-speech" "torch" "字体" "音色库" >nul
) else ( echo   （未找到数据总目录，跳过） )

echo [5/6] 打包 ffmpeg/ffprobe（渲染必需，本地 CPU）...
set "FFM=" & set "FFP="
for /f "usebackq delims=" %%f in (`where ffmpeg 2^>nul`) do if not defined FFM set "FFM=%%f"
for /f "usebackq delims=" %%f in (`where ffprobe 2^>nul`) do if not defined FFP set "FFP=%%f"
if not defined FFM if exist "ffmpeg.exe" set "FFM=ffmpeg.exe"
if not defined FFP if exist "ffprobe.exe" set "FFP=ffprobe.exe"
if defined FFM if defined FFP ( copy /y "!FFM!" "%PKG%\ffmpeg.exe">nul & copy /y "!FFP!" "%PKG%\ffprobe.exe">nul & echo   已带上 ffmpeg/ffprobe ) else ( echo   [注意] 没找到 ffmpeg，请把 ffmpeg.exe/ffprobe.exe 放进 %PKG% )

echo [6/6] 写入 启动.bat 与 首次使用说明 ...
> "%PKG%\启动.bat" echo @echo off
>> "%PKG%\启动.bat" echo cd /d "%%~dp0"
>> "%PKG%\启动.bat" echo title 水星配音对齐工作室（轻量云配版）
>> "%PKG%\启动.bat" echo set "PYTHONPATH=source"
>> "%PKG%\启动.bat" echo set "MERCURY_CLOUD_ONLY=1"
>> "%PKG%\启动.bat" echo set "PATH=%%~dp0;%%PATH%%"
>> "%PKG%\启动.bat" echo if not exist "%%~dp0ffmpeg.exe" echo [提示] 缺 ffmpeg.exe（渲染成片需要），把 ffmpeg.exe/ffprobe.exe 放到本文件夹后重开。
>> "%PKG%\启动.bat" echo if not exist "%%~dp0python\python.exe" goto NOPY
>> "%PKG%\启动.bat" echo echo 启动中（轻量云配版），浏览器会自动打开；关窗口即退出。第一次要在「设置-云配音」填服务器地址+APIKey。
>> "%PKG%\启动.bat" echo "%%~dp0python\python.exe" source\dub_align_studio\launcher.py
>> "%PKG%\启动.bat" echo goto END
>> "%PKG%\启动.bat" echo :NOPY
>> "%PKG%\启动.bat" echo echo [错误] 缺 python\python.exe，包不完整。请重新解压或重新打包。
>> "%PKG%\启动.bat" echo :END
>> "%PKG%\启动.bat" echo pause

> "%PKG%\首次使用说明.txt" echo 水星配音对齐工作室 · 轻量云配版（云配音本地渲染）
>> "%PKG%\首次使用说明.txt" echo.
>> "%PKG%\首次使用说明.txt" echo 1) 双击 启动.bat，浏览器会自动打开。
>> "%PKG%\首次使用说明.txt" echo 2) 首次到「设置 · 云配音」填服务器地址 + API Key，保存、测试连接（提示已启用即通）。
>> "%PKG%\首次使用说明.txt" echo 3) 配音引擎选「dots.tts（云 GPU · 远程）」。
>> "%PKG%\首次使用说明.txt" echo 4) 后续所有渲染、量时长、混音在本机 CPU 上跑，不需独立 GPU。
>> "%PKG%\首次使用说明.txt" echo 备注：包内不包含本地大模型（选 dots.tts 走远端），启用提示不要用，避免误用。

python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import full_version;print(full_version()+' 轻量云配版')" > "%PKG%\版本.txt" 2>nul

set "PKGSIZE=?"
for /f "usebackq delims=" %%s in (`powershell -NoProfile -Command "[int]((Get-ChildItem -LiteralPath '%CD%\%PKG%' -Recurse -File -ErrorAction SilentlyContinue ^| Measure-Object Length -Sum).Sum/1MB)" 2^>nul`) do set "PKGSIZE=%%s"
echo.
echo [完成①] 目录版：%PKG%\  （约 !PKGSIZE! MB；预期数百 MB，若几千 MB 说明大件没删干净，已被丢在分卷根）

echo.
echo ============================================================
echo   ② 安装程序打包（Inno Setup 6）
echo ============================================================

if not exist "%PKG%\启动.bat" (
  echo [错误] 目录版未生成完成，跳过安装程序打包。
  pause & exit /b 1
)

if not exist "installer\app.ico" (
  echo [信息] 生成图标 ...
  python "installer\make_icon.py" >nul 2>nul
)

set "ISCC="
for %%p in (
  "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
  "%ProgramFiles%\Inno Setup 6\ISCC.exe"
  "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
) do if not defined ISCC if exist "%%~p" set "ISCC=%%~p"
where iscc >nul 2>nul && if not defined ISCC set "ISCC=iscc"
if not defined ISCC (
  echo [警告] 没找到 Inno Setup 6（ISCC.exe），跳过第②步。
  echo        目录版已可用；需要安装程序请到 https://jrsoftware.org/isdl.php 装 Inno Setup 6 后再跑本脚本。
  goto DONE
)
echo [信息] ISCC：%ISCC%

echo [编译] Inno Setup 编译安装程序 ...
"%ISCC%" /DLite=1 /DMyVer=%VER% /DRepoDir="%CD%" /DSrcDir="%CD%\%PKG%" "installer\水星配音对齐工作室.iss"
if errorlevel 1 ( echo [失败] Inno 编译失败。看上方日志排查。& pause & exit /b 1 )

echo.
echo [完成②] 安装程序：发布包\水星配音对齐工作室_云配版安装程序_v%VER%.exe

:DONE
echo.
echo ============================================================
echo   全部完成
echo   ① 目录版：%PKG%\
echo   ② 安装程序：发布包\水星配音对齐工作室_云配版安装程序_v%VER%.exe（若装了 Inno Setup 6）
echo ============================================================
pause
exit /b 0
