@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title 水星配音对齐工作室 · 打包轻量云配版（PyInstaller，与正式版同标准）
echo ============================================================
echo    水星配音对齐工作室 · 轻量云配版打包（不含 torch/CUDA/dots 本地模型）
echo ============================================================
echo.
echo 本版与「正式打包版」同用 PyInstaller，只是：
echo   · 排除 torch/torchaudio/dots.tts/transformers 等重依赖（配音走云 GPU，本机不需要）
echo   · 包内置「云配版标记」，直接双击 exe 即云配模式（隐藏本地模型 UI，不做本地环境自检）
echo   · 自带 ffmpeg/ffprobe（渲染用 CPU）；字体/音色/模型一律不随包（要用自己放）
echo.
pause

set "NAME=水星配音对齐工作室_云配版"

rem [0] 关掉旧程序（不关会导致清理产物「拒绝访问」）
taskkill /f /im "%NAME%.exe" >nul 2>nul

rem [1] 环境检查
where python >nul 2>nul || goto :NOPY
python -m pip show pyinstaller >nul 2>nul || python -m pip install pyinstaller || goto :PIPFAIL

rem [2] 版本 + 构建戳
set "VER=0.0.0"
for /f "usebackq delims=" %%v in (`python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import APP_VERSION;print(APP_VERSION)"`) do set "VER=%%v"
set "GITHASH=unknown"
for /f %%i in ('git rev-parse --short HEAD 2^>nul') do set "GITHASH=%%i"
> "source\dub_align_studio\_build_info.py" echo BUILD_STAMP = "%DATE% %TIME:~0,5% · %GITHASH% · 云配版"
echo [版本] v%VER%  构建戳：%DATE% %TIME:~0,5% · %GITHASH%

rem [3] 清理上次云配版产物（每次从干净状态开始）
echo [1/6] 清理上次产物 dist\%NAME% / build\%NAME% / spec / __pycache__ ...
rd /s /q "dist\%NAME%" 2>nul
rd /s /q "build\%NAME%" 2>nul
del /q "%NAME%.spec" 2>nul
for /d /r "source" %%p in (__pycache__) do rd /s /q "%%p" 2>nul
if exist "dist\%NAME%" ( echo [错误] 旧产物 dist\%NAME% 被占用，无法清除。请关掉软件窗口/资源管理器/杀毒后重试。& pause & exit /b 1 )

rem [4] 生成「云配版标记」（--add-data 进 _internal；双击 exe 就是云配模式）
> "installer\cloud_edition.flag" echo lite-cloud-edition

rem [5] PyInstaller 打包（排除全部本地模型重依赖 → 产物天然不含 torch/CUDA/dots）
echo [2/6] PyInstaller 构建（排除 torch/dots/transformers 等，只打进真正 import 到的轻依赖）...
set "PYTHONPATH=source"
python -m PyInstaller --noconfirm --clean --onedir --name "%NAME%" ^
  --paths source ^
  --add-data "source\dub_align_studio\web;dub_align_studio\web" ^
  --add-data "installer\cloud_edition.flag;." ^
  --collect-submodules dub_align_studio --collect-submodules integrated_workbench ^
  --exclude-module torch --exclude-module torchaudio --exclude-module torchvision ^
  --exclude-module torchgen --exclude-module functorch --exclude-module triton ^
  --exclude-module dots_tts --exclude-module fish_speech ^
  --exclude-module transformers --exclude-module tokenizers ^
  --exclude-module accelerate --exclude-module safetensors ^
  --exclude-module xformers --exclude-module flash_attn --exclude-module bitsandbytes ^
  --exclude-module scipy --exclude-module pandas --exclude-module matplotlib ^
  --console "source\dub_align_studio\launcher.py" || goto :BUILDFAIL

if not exist "dist\%NAME%\%NAME%.exe" goto :BUILDFAIL

rem [6] 内置 ffmpeg/ffprobe（渲染用，纯 CPU），放 exe 旁
echo [3/6] 内置 ffmpeg/ffprobe（渲染用 CPU）...
set "FFM=" & set "FFP="
for /f "usebackq delims=" %%f in (`where ffmpeg 2^>nul`) do if not defined FFM set "FFM=%%f"
for /f "usebackq delims=" %%f in (`where ffprobe 2^>nul`) do if not defined FFP set "FFP=%%f"
if not defined FFM if exist "ffmpeg.exe" set "FFM=ffmpeg.exe"
if not defined FFP if exist "ffprobe.exe" set "FFP=ffprobe.exe"
if defined FFM if defined FFP ( copy /y "!FFM!" "dist\%NAME%\ffmpeg.exe">nul & copy /y "!FFP!" "dist\%NAME%\ffprobe.exe">nul & echo   已内置 ffmpeg/ffprobe ) else ( echo   [注意] 没找到 ffmpeg，请把 ffmpeg.exe/ffprobe.exe 放进 dist\%NAME% 后再打安装包 )

rem [7] 标记再拷一份到 exe 旁（双保险：_internal 有、exe 旁也有）
copy /y "installer\cloud_edition.flag" "dist\%NAME%\cloud_edition.flag">nul

rem [8] 生成 启动.bat（设 MERCURY_CLOUD_ONLY=1 + PATH 兜底；其实直接双击 exe 也已是云配）
echo [4/6] 生成 启动.bat + 首次使用说明 ...
> "dist\%NAME%\启动.bat" echo @echo off
>> "dist\%NAME%\启动.bat" echo cd /d "%%~dp0"
>> "dist\%NAME%\启动.bat" echo title 水星配音对齐工作室（云配版）
>> "dist\%NAME%\启动.bat" echo set "MERCURY_CLOUD_ONLY=1"
>> "dist\%NAME%\启动.bat" echo set "PATH=%%~dp0;%%~dp0_internal;%%PATH%%"
>> "dist\%NAME%\启动.bat" echo if not exist "%%~dp0ffmpeg.exe" echo [提示] 未内置 ffmpeg.exe（渲染成片需要），把 ffmpeg.exe/ffprobe.exe 放到本文件夹后重开
>> "dist\%NAME%\启动.bat" echo echo 启动中（云配版）…浏览器稍后自动打开，别关此窗口。设置-云配音 填服务器地址+API Key，引擎选 dots.tts 云GPU 远程。
>> "dist\%NAME%\启动.bat" echo start "" "%%~dp0%NAME%.exe"

> "dist\%NAME%\首次使用说明.txt" echo 水星配音对齐工作室 · 轻量云配版（本机无需独立显卡）
>> "dist\%NAME%\首次使用说明.txt" echo.
>> "dist\%NAME%\首次使用说明.txt" echo 1) 双击 %NAME%.exe（或 启动.bat），浏览器自动打开。
>> "dist\%NAME%\首次使用说明.txt" echo 2) 设置 - 云配音：填云服务器地址 + API Key，保存、测试连接。
>> "dist\%NAME%\首次使用说明.txt" echo 3) 配音引擎选「dots.tts 云GPU 远程」。
>> "dist\%NAME%\首次使用说明.txt" echo 4) 字体/音色不随包：要用的字体放进「数据目录\字体」，音色在软件里导入。
>> "dist\%NAME%\首次使用说明.txt" echo 注：本版不带任何本地大模型（torch/dots/fish 全无），配音一律走云 GPU；渲染用自带 ffmpeg（CPU）。

rem [9] 版本文件
python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import full_version;print(full_version()+' 云配版')" > "dist\%NAME%\版本.txt" 2>nul

rem [10] 体积自检（相对路径 + argv 传给 python → 规避仓库路径里的 & 拆命令）
echo [5/6] 体积自检 ...
set "PKGSIZE=?"
for /f "usebackq delims=" %%s in (`python "build_scripts\dirsize.py" "dist\%NAME%"`) do set "PKGSIZE=%%s"
echo.
echo [完成] 云配版产物：dist\%NAME%\   大小约 !PKGSIZE! MB
echo        （应为数百 MB；若仍上 G 说明重依赖没排掉，把上方 PyInstaller 输出整段发我）
echo [完成] 直接把整个 dist\%NAME% 文件夹发给对方：双击 %NAME%.exe（或 启动.bat）即用。
echo        要做成 Setup.exe 安装包 → 双击「打包安装程序_轻量云配版.bat」。
echo.
choice /c YN /m "压缩成 zip 请按 Y，跳过按 N"
if errorlevel 2 goto :DONE
echo [6/6] 压缩中…
if not exist "发布包" mkdir "发布包"
python -c "import shutil;print('ZIP:',shutil.make_archive(r'发布包\%NAME%_v%VER%','zip',r'dist','%NAME%'))"
:DONE
echo 全部完成。
pause
exit /b 0

:NOPY
echo [错误] 未找到 python。请安装 Python 3.11+ 并勾选 Add python.exe to PATH。
pause & exit /b 1
:PIPFAIL
echo [错误] PyInstaller 安装失败，请检查网络后重试。
pause & exit /b 1
:BUILDFAIL
echo [错误] PyInstaller 打包失败。若是「拒绝访问/PermissionError」：关软件、关打开 dist 的资源管理器、暂停杀毒后重试；其他报错把上方整段发我。
pause & exit /b 1
