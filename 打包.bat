@echo off
chcp 65001 >nul
cd /d %~dp0
echo ══════════════════════════════════════════
echo   水星配音对齐工作室 · 一键打包
echo ══════════════════════════════════════════
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
set PYTHONPATH=source
python -m PyInstaller --noconfirm --clean --onedir --name "水星配音对齐工作室" ^
  --paths source ^
  --add-data "source\dub_align_studio\web;dub_align_studio\web" ^
  --collect-submodules dub_align_studio --collect-submodules integrated_workbench ^
  --console source\dub_align_studio\launcher.py
if errorlevel 1 (
  echo [错误] 打包失败，请把上方报错整段发给开发。
  pause & exit /b 1
)
echo.
echo ✅ 打包完成：dist\水星配音对齐工作室\
echo    下一步：把 ffmpeg.exe / ffprobe.exe 放进该目录（exe 旁边），双击 exe 启动。
echo    首次启动请在「工具箱自检」页设置数据总目录并点「环境自检」。
pause
