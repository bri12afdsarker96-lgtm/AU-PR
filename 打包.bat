@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   水星配音对齐工作室  一键打包
echo ==========================================

rem [0] 关掉正在运行的旧程序（不关会导致 dist 清理「拒绝访问」）
taskkill /f /im "水星配音对齐工作室.exe" >nul 2>nul

rem [1] 数据保护：数据总目录若在 dist 内，先搬到仓库根（dist 每次打包会被清空）
if exist "dist\水星配音对齐工作室\水星配音数据" goto :SAVEDATA
goto :PICKOUT
:SAVEDATA
echo [保护] 数据目录在 dist 内，先搬到仓库根，避免打包时被清空...
robocopy "dist\水星配音对齐工作室\水星配音数据" "水星配音数据" /e /move >nul
echo [保护] 已搬到 "%~dp0水星配音数据"，打包后在软件「工具箱自检」把总目录指向它。

rem [2] 选择输出目录：优先清理旧 dist；清不掉（被占用）就输出到新目录，保证打包必成
:PICKOUT
set "OUTDIR=dist"
if exist "dist\水星配音对齐工作室" rd /s /q "dist\水星配音对齐工作室" 2>nul
if not exist "dist\水星配音对齐工作室" goto :OUTOK
set "OUTDIR=dist_new_%RANDOM%"
echo [提示] 旧 dist 被占用无法清理（多为资源管理器开着该文件夹 / 杀毒扫描）。
echo        本次改为输出到新目录：%OUTDIR%
echo        空闲后可手动删除旧的 dist\水星配音对齐工作室 文件夹。
:OUTOK

rem [3] 环境检查
where python >nul 2>nul || goto :NOPY
python -m pip show pyinstaller >nul 2>nul || python -m pip install pyinstaller || goto :PIPFAIL

rem [4] 版本构建戳（界面右上角 / 工具箱版本行显示）
set "GITHASH=unknown"
for /f %%i in ('git rev-parse --short HEAD 2^>nul') do set "GITHASH=%%i"
> "source\dub_align_studio\_build_info.py" echo BUILD_STAMP = "%DATE% %TIME:~0,5% · %GITHASH%"
echo [版本] 本次构建戳：%DATE% %TIME:~0,5% · %GITHASH%

rem [5] 打包（入口必须是 launcher.py）
set "PYTHONPATH=source"
python -m PyInstaller --noconfirm --clean --onedir --name "水星配音对齐工作室" --distpath "%OUTDIR%" ^
  --paths source ^
  --add-data "source\dub_align_studio\web;dub_align_studio\web" ^
  --collect-submodules dub_align_studio --collect-submodules integrated_workbench ^
  --console "source\dub_align_studio\launcher.py" || goto :BUILDFAIL

rem [6] 落版本文件
python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.version import full_version;print(full_version())" > "%OUTDIR%\水星配音对齐工作室\版本.txt" 2>nul
echo.
echo [完成] 产物目录：%OUTDIR%\水星配音对齐工作室\
echo    1. 把 ffmpeg.exe 和 ffprobe.exe 放进该目录（exe 旁边）
echo    2. 双击其中的 水星配音对齐工作室.exe，右上角应显示版本号
echo    3. 数据总目录建议设在 dist 之外，例如 D:\水星配音数据
pause
exit /b 0

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
echo        1. 关闭正在运行的软件窗口，含最小化的黑色控制台
echo        2. 关闭正打开 dist 文件夹的资源管理器窗口
echo        3. 暂停杀毒软件实时扫描
echo        然后重新运行本脚本；其他报错请把上方整段发给开发。
pause
exit /b 1
