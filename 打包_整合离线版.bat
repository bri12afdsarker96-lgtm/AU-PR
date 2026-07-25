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

rem [0.1] 拒绝 Microsoft Store 版 Python：它在 WindowsApps 受系统保护，无法整目录拷贝/跨机运行，
rem       打出来的整合包在别的电脑上双击「启动.bat」会一闪而过、无任何报错（正是本次现象）。
echo %PYHOME% | findstr /i "WindowsApps" >nul && goto :STOREPY

rem [0.5] 打包前自检：用「将被拷进包的这个 Python」实测能否加载 dots.tts 运行时。
rem 目的：避免把装错的环境（最常见 venv：依赖在 venv 里、base 里没有）打成一个用不了的大包。
echo [自检] 用引擎 probe 校验打包 Python 能否真正跑 dots.tts（含 tn 桩/版本匹配/CUDA）...
"%PYHOME%\python.exe" -c "import sys;sys.path.insert(0,'source');from dub_align_studio.engines.dots_local import DotsLocalEngine as E;s=E().probe();print('[自检]',s.detail);sys.exit(0 if s.available else 1)"
if errorlevel 1 goto :PREFAIL

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

rem [1.5] 校验「包内 Python」能独立运行——用它自己（脱离 PATH）跑一句，跑不起来说明源 Python
rem       不可重定位/复制不全，此时打出来的包到别的机器必然起不来，直接拦下并说明原因。
"%PKG%\python\python.exe" -c "import sys;print(sys.version)" >nul 2>&1
if errorlevel 1 goto :PYCOPYBAD
echo [1/5] 包内 Python 复制校验通过（可独立运行）。

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
>> "%PKG%\启动.bat" echo if not exist "%%~dp0python\python.exe" goto NOPY
>> "%PKG%\启动.bat" echo echo 正在启动 水星配音对齐工作室（整合离线版）...浏览器将自动打开，勿关本窗口。
>> "%PKG%\启动.bat" echo "%%~dp0python\python.exe" source\dub_align_studio\launcher.py
>> "%PKG%\启动.bat" echo if errorlevel 1 goto FAIL
>> "%PKG%\启动.bat" echo goto END
>> "%PKG%\启动.bat" echo :NOPY
>> "%PKG%\启动.bat" echo echo [错误] 缺少 python\python.exe —— 整合包不完整，请把整个文件夹重新完整拷贝。
>> "%PKG%\启动.bat" echo goto END
>> "%PKG%\启动.bat" echo :FAIL
>> "%PKG%\启动.bat" echo echo.
>> "%PKG%\启动.bat" echo echo [启动失败] Python 异常退出，详情如下：
>> "%PKG%\启动.bat" echo if exist "source\dub_align_studio\启动错误.log" type "source\dub_align_studio\启动错误.log"
>> "%PKG%\启动.bat" echo echo 若窗口一闪、上面没有 Python 报错：多为「包内 Python 无法在本机运行」——
>> "%PKG%\启动.bat" echo echo 源机若用的是 Microsoft Store 版 Python（不可跨机），请改用 python.org 版重装依赖后重新打包。
>> "%PKG%\启动.bat" echo echo 另一可能：本机缺 Visual C++ 运行库 —— 装一个「Microsoft Visual C++ 2015-2022 Redistributable (x64)」再试。
>> "%PKG%\启动.bat" echo :END
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
exit /b 0

:PREFAIL
echo.
echo [自检失败] 将被打进包的 Python 是：%PYHOME%
echo   它无法加载 dots.tts 运行时，或 transformers 不是 4.57.0 —— 打出来的包在目标机上照样报 Qwen2。
echo   最常见原因：依赖装进了 venv/conda，而本脚本打包的是它的 base Python（base 里没有这些包）。
echo   解决其一：
echo     - 用「非 venv 的系统 Python」重新 pip 安装依赖后再运行本脚本；
echo     - 或把 transformers==4.57.0 + CUDA 版 torch + dots.tts 直接装进 %PYHOME%。
echo   校验命令（能打印 available=True 即可打包）：
echo     "%PYHOME%\python.exe" -c "import sys;sys.path.insert(0,'source');from dub_align_studio.engines.dots_local import DotsLocalEngine as E;print(E().probe())"
pause
exit /b 1

:STOREPY
echo.
echo [错误] 检测到打包用的是 Microsoft Store 版 Python：
echo        %PYHOME%
echo   Store 版 Python 位于受保护的 WindowsApps，无法整目录拷贝、也不能在别的电脑运行，
echo   打出来的整合包到别的机器上双击「启动.bat」会一闪而过、且没有任何报错（正是你遇到的现象）。
echo   解决：到 python.org 下载安装 Python 3.11（安装时勾 Add python.exe to PATH，
echo        不要用 Microsoft Store 那个），在它里面装好 CUDA 版 torch + dots.tts + whisper，
echo        确认能正常配音后，再运行本脚本打包。
pause
exit /b 1

:PYCOPYBAD
echo.
echo [错误] 已把源 Python 拷进包内，但「包内 Python」无法独立运行：
echo        %PKG%\python\python.exe
echo   说明源 Python 不可重定位或复制不完整（常见于 Store 版 / 精简版 / 依赖系统级运行库的环境）。
echo   请改用 python.org 安装的独立 Python 重新打包；若仍不行，把本窗口内容发给开发。
pause
exit /b 1
