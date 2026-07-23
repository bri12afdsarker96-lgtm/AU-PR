@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   水星配音对齐工作室  一键打包
echo ==========================================

rem [0] 关掉正在运行的旧程序（不关会导致清理产物「拒绝访问」）
taskkill /f /im "水星配音对齐工作室.exe" >nul 2>nul

rem [1] 数据保护：数据总目录若在 dist 内，先搬到仓库根（下一步会删掉整个产物目录）
if exist "dist\水星配音对齐工作室\水星配音数据" goto :SAVEDATA
goto :CLEAN
:SAVEDATA
echo [保护] 数据目录在 dist 内，先搬到仓库根，避免随产物一起被清除...
robocopy "dist\水星配音对齐工作室\水星配音数据" "水星配音数据" /e /move >nul
echo [保护] 已搬到 "%~dp0水星配音数据"，打包后在软件「工具箱自检」把总目录指向它。

rem [2] 清除上一次打包的全部产物（每次打包都从干净状态开始）
:CLEAN
echo [清理] 删除上次打包产物：dist 输出 + build 中间件 + 历史回退目录 + spec ...
rd /s /q "dist\水星配音对齐工作室" 2>nul
rd /s /q "build\水星配音对齐工作室" 2>nul
del /q "水星配音对齐工作室.spec" 2>nul
for /d %%D in ("dist_new_*") do rd /s /q "%%D" 2>nul
rem 清不掉说明仍被占用，直接提示，不再堆新目录
if exist "dist\水星配音对齐工作室" goto :LOCKED
echo [清理] 旧产物已清除。

rem [3] 环境检查
where python >nul 2>nul || goto :NOPY
python -m pip show pyinstaller >nul 2>nul || python -m pip install pyinstaller || goto :PIPFAIL

rem [4] 版本构建戳（界面右上角 / 工具箱版本行显示）
set "GITHASH=unknown"
for /f %%i in ('git rev-parse --short HEAD 2^>nul') do set "GITHASH=%%i"
> "source\dub_align_studio\_build_info.py" echo BUILD_STAMP = "%DATE% %TIME:~0,5% · %GITHASH%"
echo [版本] 本次构建戳：%DATE% %TIME:~0,5% · %GITHASH%

rem [5] 打包（入口必须是 launcher.py；--clean 同时清 PyInstaller 缓存）
set "PYTHONPATH=source"
python -m PyInstaller --noconfirm --clean --onedir --name "水星配音对齐工作室" ^
  --paths source ^
  --add-data "source\dub_align_studio\web;dub_align_studio\web" ^
  --collect-submodules dub_align_studio --collect-submodules integrated_workbench ^
  --console "source\dub_align_studio\launcher.py" || goto :BUILDFAIL

rem [6] 落版本文件
python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import full_version;print(full_version())" > "dist\水星配音对齐工作室\版本.txt" 2>nul
echo.
echo [完成] 产物目录：dist\水星配音对齐工作室\
echo    1. 把 ffmpeg.exe 和 ffprobe.exe 放进该目录（exe 旁边）
echo    2. 双击其中的 水星配音对齐工作室.exe，右上角应显示版本号
echo    3. 数据总目录建议设在 dist 之外，例如 D:\水星配音数据
pause
exit /b 0

:LOCKED
echo [错误] 旧产物 dist\水星配音对齐工作室 被占用，无法清除。请先：
echo        1. 关闭正在运行的软件窗口，含最小化的黑色控制台
echo        2. 关闭正打开 dist 文件夹的资源管理器窗口
echo        3. 暂停杀毒软件实时扫描
echo        然后重新双击本脚本。
pause
exit /b 1

:NOPY
echo [错误] 未找到 python。请安装 Python 3.11+ 并勾选 Add python.exe to PATH。
pause
exit /b 1

:PIPFAIL
echo [错误] PyInstaller 安装失败，请检查网络后重试。
pause
exit /b 1

:BUILDFAIL
echo [错误] 打包失败。若上方是「拒绝访问 / PermissionError」：
echo        关闭正在运行的软件、关闭打开 dist 的资源管理器窗口、暂停杀毒后重试；
echo        其他报错请把上方整段发给开发。
pause
exit /b 1
