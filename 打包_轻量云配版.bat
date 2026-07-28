@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title 水星配音对齐工作室 · 轻量云配版打包（无需独显）
echo ============================================================
echo    水星配音对齐工作室 · 轻量云配版 打包（集显低配电脑可用）
echo ============================================================
echo.
echo 本版剔除「本地配音大模型 torch/CUDA/dots.tts/HF权重」，体积从 6~12GB 降到几百 MB：
echo   · 配音克隆 走【云 GPU】（软件里设云地址+APIKey，引擎选 dots.tts 云GPU 远程）
echo   · 量时长(whisper) 与 渲染(ffmpeg) 走本机 CPU，集显即可，无需独立显卡
echo.
echo 请在【已能正常云配音的这台】上运行本脚本打包。
echo.
pause

rem [0] 定位 Python（拒绝 Store 版：不可跨机）
set "PYHOME="
for /f "usebackq delims=" %%i in (`python -c "import sys;print(sys.base_prefix)"`) do set "PYHOME=%%i"
if not defined PYHOME ( echo [错误] 未找到 python，请确认已装并在 PATH。& pause & exit /b 1 )
echo %PYHOME% | findstr /i "WindowsApps" >nul && ( echo [错误] 打包不能用 Microsoft Store 版 Python^(不可跨机^)，请用 python.org 版。& pause & exit /b 1 )
echo [信息] 打包所用 Python：%PYHOME%

set "VER=0.0.0"
for /f "usebackq delims=" %%v in (`python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import APP_VERSION;print(APP_VERSION)"`) do set "VER=%%v"

set "PKGROOT=轻量云配版包"
set "PKG=%PKGROOT%\水星配音对齐工作室_轻量版_v%VER%"
echo [清理] 重建 %PKG% ...
rd /s /q "%PKG%" 2>nul
mkdir "%PKG%" 2>nul

echo [1/6] 拷贝 Python 环境(拷贝时即跳过 torch/CUDA/dots 等重依赖，避免 6GB 进包) ...
robocopy "%PYHOME%" "%PKG%\python" /e /nfl /ndl /njh /njs /nc /ns /xd torch torchaudio torchvision torchgen functorch triton nvidia dots_tts transformers tokenizers accelerate safetensors fish_speech xformers flash_attn cusparselt cudnn >nul
if errorlevel 8 ( echo [错误] 拷贝 Python 失败。& pause & exit /b 1 )

echo [2/6] 兜底清理残留重依赖(绝对路径 + 长路径删除，防 torch/CUDA 删不掉) ...
set "SP=%CD%\%PKG%\python\Lib\site-packages"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$sp='%SP%'; if(Test-Path -LiteralPath $sp){Get-ChildItem -LiteralPath $sp -Directory | Where-Object {$_.Name -match '^(torch|nvidia|triton|dots_tts|dots\.tts|transformers|tokenizers|accelerate|safetensors|functorch|torchgen|fish_speech|xformers|flash_attn|cusparselt|cudnn)'} | ForEach-Object {Remove-Item -LiteralPath ('\\?\'+$_.FullName) -Recurse -Force -ErrorAction SilentlyContinue}}"

echo [3/6] 拷贝软件源码 + 云配音部署脚本 ...
robocopy "source" "%PKG%\source" /e /nfl /ndl /njh /njs /nc /ns /xd __pycache__ >nul
if exist "server" robocopy "server" "%PKG%\server" /e /nfl /ndl /njh /njs /nc /ns >nul

echo [4/6] 拷贝数据总目录(仅 whisper/音效；剔除 dots.tts/fish-speech/torch/字体/音色 —— 字体音色请自行导入) ...
set "DATADIR="
for /f "usebackq delims=" %%d in (`python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.settings import data_root;print(data_root())"`) do set "DATADIR=%%d"
if defined DATADIR if exist "%DATADIR%" (
  robocopy "%DATADIR%" "%PKG%\水星配音数据" /e /nfl /ndl /njh /njs /nc /ns /xd "dots.tts" "fish-speech" "torch" "字体" "音色库" >nul
) else ( echo   （未找到数据总目录，跳过） )

echo [5/6] 打包 ffmpeg/ffprobe(渲染必需，本机 CPU) ...
set "FFM=" & set "FFP="
for /f "usebackq delims=" %%f in (`where ffmpeg 2^>nul`) do if not defined FFM set "FFM=%%f"
for /f "usebackq delims=" %%f in (`where ffprobe 2^>nul`) do if not defined FFP set "FFP=%%f"
if not defined FFM if exist "ffmpeg.exe" set "FFM=ffmpeg.exe"
if not defined FFP if exist "ffprobe.exe" set "FFP=ffprobe.exe"
if defined FFM if defined FFP ( copy /y "!FFM!" "%PKG%\ffmpeg.exe">nul & copy /y "!FFP!" "%PKG%\ffprobe.exe">nul & echo   已打包 ffmpeg/ffprobe ) else ( echo   [注意] 没找到 ffmpeg，请把 ffmpeg.exe/ffprobe.exe 放进 %PKG% )

echo [6/6] 生成 启动.bat 与 首次使用说明 ...
> "%PKG%\启动.bat" echo @echo off
>> "%PKG%\启动.bat" echo cd /d "%%~dp0"
>> "%PKG%\启动.bat" echo title 水星配音对齐工作室(轻量云配版)
>> "%PKG%\启动.bat" echo set "PYTHONPATH=source"
>> "%PKG%\启动.bat" echo set "MERCURY_CLOUD_ONLY=1"
>> "%PKG%\启动.bat" echo set "PATH=%%~dp0;%%PATH%%"
>> "%PKG%\启动.bat" echo if not exist "%%~dp0ffmpeg.exe" echo [提示] 未发现 ffmpeg.exe(渲染成片需要)，请把 ffmpeg.exe/ffprobe.exe 放到本文件夹后重开。
>> "%PKG%\启动.bat" echo if not exist "%%~dp0python\python.exe" goto NOPY
>> "%PKG%\启动.bat" echo echo 启动中(轻量云配版)…浏览器会自动打开，勿关本窗口。配音请在设置里填云地址+APIKey、引擎选 dots.tts 云GPU 远程。
>> "%PKG%\启动.bat" echo "%%~dp0python\python.exe" source\dub_align_studio\launcher.py
>> "%PKG%\启动.bat" echo goto END
>> "%PKG%\启动.bat" echo :NOPY
>> "%PKG%\启动.bat" echo echo [错误] 缺 python\python.exe，整包不完整，请重新完整拷贝整个文件夹。
>> "%PKG%\启动.bat" echo :END
>> "%PKG%\启动.bat" echo pause

> "%PKG%\首次使用说明.txt" echo 水星配音对齐工作室 · 轻量云配版（无需独立显卡）
>> "%PKG%\首次使用说明.txt" echo.
>> "%PKG%\首次使用说明.txt" echo 1) 双击 启动.bat，浏览器自动打开。
>> "%PKG%\首次使用说明.txt" echo 2) 设置 - 云配音：填 云服务器地址 + API Key，保存、测试连接(显示可用即通)。
>> "%PKG%\首次使用说明.txt" echo 3) 配音引擎 下拉选：dots.tts（云 GPU · 远程）。
>> "%PKG%\首次使用说明.txt" echo 4) 配音在云端跑；量时长与渲染在本机 CPU，集显即可，无需独立显卡。
>> "%PKG%\首次使用说明.txt" echo 注：本版不含本地大模型，选 dots.tts（本地）会提示不可用，属正常。

python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import full_version;print(full_version()+' 轻量云配版')" > "%PKG%\版本.txt" 2>nul

echo.
set "PKGSIZE=?"
for /f "delims=" %%s in ('powershell -NoProfile -Command "[int]((Get-ChildItem -LiteralPath '%CD%\%PKG%' -Recurse -File -ErrorAction SilentlyContinue ^| Measure-Object Length -Sum).Sum/1MB)" 2^>nul') do set "PKGSIZE=%%s"
echo.
echo [体积] 轻量包大小约 !PKGSIZE! MB（正常应为几百 MB；若仍上千 MB 说明重依赖没删净，请把本窗口发我）
echo [完成] 轻量云配版：%PKG%\
echo   拷到低配电脑(集显即可)，双击 启动.bat，按 首次使用说明.txt 配好云端即用。
echo.
choice /c YN /m "压缩成 zip 请按 Y，跳过按 N"
if errorlevel 2 goto DONE
echo 正在压缩…
python -c "import shutil,os;print('ZIP:',shutil.make_archive(os.path.join('发布包','水星配音对齐工作室_轻量版_v%VER%'),'zip','%PKGROOT%','水星配音对齐工作室_轻量版_v%VER%'))"
:DONE
echo 全部完成。
pause
exit /b 0
