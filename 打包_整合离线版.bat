@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 水星配音对齐工作室 · 整合离线包
echo ==========================================
echo   水星配音对齐工作室 · 整合离线包打包
echo   （Python 环境 + 依赖 + 插件 + 模型 全打包，目标机零安装）
echo ==========================================
echo.
echo 说明：本脚本把「当前这个 Python 环境」整体拷进包里，
echo       所以请务必在【已装好 torch(CUDA)+dots.tts+whisper 且能正常配音】的 Python 上运行本脚本。
echo       成品很大（含 torch/CUDA/模型，通常 6~12GB），请预留磁盘空间与时间。
echo.
pause

rem [0] 定位当前 Python 安装目录（整体拷贝以保证可离线自包含）
set "PYHOME="
for /f "usebackq delims=" %%i in (`python -c "import sys;print(sys.base_prefix)"`) do set "PYHOME=%%i"
if not defined PYHOME ( echo [错误] 未找到 python，请确认已装并在 PATH。& pause & exit /b 1 )
echo [信息] 打包所用 Python：%PYHOME%

rem 读版本号用于命名
set "VER=0.0.0"
for /f "usebackq delims=" %%v in (`python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import APP_VERSION;print(APP_VERSION)"`) do set "VER=%%v"

set "PKGROOT=整合离线包"
set "PKG=%PKGROOT%\水星配音对齐工作室_整合版_v%VER%"
echo [清理] 重建打包目录 %PKG% ...
rd /s /q "%PKG%" 2>nul
mkdir "%PKG%" 2>nul

rem [1] 拷贝整个 Python 环境（含 site-packages 里的 torch/dots.tts/transformers 等全部依赖）
echo [1/5] 拷贝 Python 环境（含全部依赖/插件，体量大请耐心）...
robocopy "%PYHOME%" "%PKG%\python" /e /nfl /ndl /njh /njs /nc /ns >nul
if errorlevel 8 ( echo [错误] 拷贝 Python 失败。& pause & exit /b 1 )

rem [2] 拷贝软件源码
echo [2/5] 拷贝软件源码 ...
robocopy "source" "%PKG%\source" /e /nfl /ndl /njh /njs /nc /ns /xd __pycache__ >nul

rem [3] 拷贝数据总目录（whisper-cli / ggml 模型 / 字体 / 音色库 等，一并离线）
set "DATADIR="
for /f "usebackq delims=" %%d in (`python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.settings import data_root;print(data_root())"`) do set "DATADIR=%%d"
if defined DATADIR if exist "%DATADIR%" (
  echo [3/5] 拷贝数据总目录：%DATADIR%
  robocopy "%DATADIR%" "%PKG%\水星配音数据" /e /nfl /ndl /njh /njs /nc /ns >nul
) else (
  echo [3/5] 未找到数据总目录，跳过（可打包后把「水星配音数据」文件夹放进包内同级）。
)

rem [4] 拷贝 HuggingFace 缓存（dots.tts 模型权重在此，拷了才能离线用）
if exist "%USERPROFILE%\.cache\huggingface" (
  echo [4/5] 拷贝 HuggingFace 模型缓存（dots.tts 权重）...
  robocopy "%USERPROFILE%\.cache\huggingface" "%PKG%\hf_cache" /e /nfl /ndl /njh /njs /nc /ns >nul
) else (
  echo [4/5] 未找到 HF 缓存，跳过（dots.tts 首次配音会联网下权重；如需完全离线，先在本机成功配音一次再打包）。
)

rem [5] 写启动脚本（用包内 Python，指向包内数据与模型缓存，全离线）
echo [5/5] 生成启动脚本 启动.bat ...
> "%PKG%\启动.bat" echo @echo off
>> "%PKG%\启动.bat" echo chcp 65001 ^>nul
>> "%PKG%\启动.bat" echo cd /d "%%~dp0"
>> "%PKG%\启动.bat" echo title 水星配音对齐工作室
>> "%PKG%\启动.bat" echo set "PYTHONPATH=source"
>> "%PKG%\启动.bat" echo if exist "hf_cache" set "HF_HOME=%%~dp0hf_cache"
>> "%PKG%\启动.bat" echo echo 正在启动 水星配音对齐工作室（整合离线版）...浏览器将自动打开，勿关本窗口。
>> "%PKG%\启动.bat" echo "%%~dp0python\python.exe" source\dub_align_studio\launcher.py
>> "%PKG%\启动.bat" echo pause

rem 版本文件
python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import full_version;print(full_version())" > "%PKG%\版本.txt" 2>nul

echo.
echo [完成] 整合离线包目录：%PKG%\
echo        目标电脑：整个文件夹拷过去，双击里面的「启动.bat」即可，无需装 Python/依赖/模型。
echo        （渲染成片仍需 ffmpeg：把 ffmpeg.exe/ffprobe.exe 放进该文件夹或系统 PATH。）
echo.
echo 是否压缩成 zip？（大文件压缩较慢；直接拷文件夹也可用）
choice /c YN /m "压缩成 zip 请按 Y，跳过按 N"
if errorlevel 2 goto :DONE
echo 正在压缩（大包较慢，请耐心）...
python -c "import shutil,os; p=shutil.make_archive(os.path.join('发布包','水星配音对齐工作室_整合版_v%VER%'),'zip','%PKGROOT%','水星配音对齐工作室_整合版_v%VER%'); print('ZIP:',p)"
:DONE
echo.
echo 全部完成。
pause
