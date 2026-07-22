@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   水星配音对齐工作室 · 一键打包
echo ==========================================

rem [0] 先关掉正在运行的旧程序（不关的话清理 dist 会「拒绝访问」）
taskkill /f /im "水星配音对齐工作室.exe" >nul 2>nul

rem [1] 数据保护：数据总目录若放在 dist 里，先搬到仓库根——dist 每次打包都会被整个清空！
if exist "dist\水星配音对齐工作室\水星配音数据" (
  echo [保护] 检测到数据总目录在 dist 内，先搬出，避免打包时被清空 ...
  robocopy "dist\水星配音对齐工作室\水星配音数据" "水星配音数据" /e /move >nul
  echo [保护] 已搬到 "%~dp0水星配音数据"
  echo         打包完成后，在软件「工具箱自检」页把数据总目录指向上面这个路径即可（选择 - 保存 - 环境自检）。
  echo.
)

rem [2] 预清理旧 dist；删不掉说明仍被占用，直接给出处理办法
if exist "dist\水星配音对齐工作室" rd /s /q "dist\水星配音对齐工作室" 2>nul
if exist "dist\水星配音对齐工作室" (
  echo [错误] dist\水星配音对齐工作室 被占用，无法清理。请依次检查后重试：
  echo        1. 关闭正在运行的 水星配音对齐工作室 窗口（含最小化的黑色控制台）
  echo        2. 关闭打开着该文件夹的资源管理器窗口
  echo        3. 暂停杀毒软件的实时扫描
  pause & exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 未找到 python。请安装 Python 3.11+ 并勾选 "Add python.exe to PATH"。
  pause & exit /b 1
)
python -m pip show pyinstaller >nul 2>nul
if errorlevel 1 (
  echo 首次使用：安装 PyInstaller ...
  python -m pip install pyinstaller
  if errorlevel 1 ( echo [错误] PyInstaller 安装失败，请检查网络。 & pause & exit /b 1 )
)
set "PYTHONPATH=source"
python -m PyInstaller --noconfirm --clean --onedir --name "水星配音对齐工作室" ^
  --paths source ^
  --add-data "source\dub_align_studio\web;dub_align_studio\web" ^
  --collect-submodules dub_align_studio --collect-submodules integrated_workbench ^
  --console "source\dub_align_studio\launcher.py"
if errorlevel 1 (
  echo [错误] 打包失败。若上方是「拒绝访问 PermissionError」：关闭正在运行的软件/资源管理器/杀毒后重试；
  echo        其他报错请把上方整段发给开发。
  pause & exit /b 1
)
echo.
echo [完成] 打包产物：dist\水星配音对齐工作室\
echo    1. 把 ffmpeg.exe / ffprobe.exe 放进该目录（exe 旁边）
echo    2. 双击 exe 启动；数据总目录建议设在 dist 之外（如 D:\水星配音数据），
echo       否则下次打包前会被自动搬到仓库根目录保护起来。
pause
