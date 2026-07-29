@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ==========================================
echo   水星配音对齐工作室  一键打包（项目打包）
echo ==========================================

rem 前置：必须在真源 v0.5 的完整代码上打包（含 source\）。缺 source 直接拦下。
if not exist "source\dub_align_studio\launcher.py" goto :NOSRC

rem [0] 关掉正在运行的旧程序（不关会导致清理产物「拒绝访问」）
taskkill /f /im "水星配音对齐工作室.exe" >nul 2>nul

rem [1] 数据保护：数据总目录若在 dist 内，先搬到仓库根（下一步会删掉整个产物目录）
if exist "dist\水星配音对齐工作室\水星配音数据" goto :SAVEDATA
goto :CLEAN
:SAVEDATA
echo [保护] 数据目录在 dist 内，先搬到仓库根，避免随产物一起被清除...
robocopy "dist\水星配音对齐工作室\水星配音数据" "水星配音数据" /e /move >nul
echo [保护] 已搬到 "%~dp0水星配音数据"。建议数据总目录设在仓库外：D:\GitHub\By\AU^&PR\水星配音数据

rem [2] 清除上一次打包的全部产物（每次打包都从干净状态开始）
:CLEAN
echo [清理] 删除上次打包产物与旧缓存：dist / build / spec / __pycache__ ...
rd /s /q "dist\水星配音对齐工作室" 2>nul
rd /s /q "build\水星配音对齐工作室" 2>nul
del /q "水星配音对齐工作室.spec" 2>nul
for /d %%D in ("dist_new_*") do rd /s /q "%%D" 2>nul
for /d /r "source" %%p in (__pycache__) do rd /s /q "%%p" 2>nul
if exist "dist\水星配音对齐工作室" goto :LOCKED
echo [清理] 旧产物与旧缓存已清除。

rem [3] 环境检查：找一个真正能执行的 Python（逐行试，避免 for/&& 解析歧义）
set "PYEXE="
py -3 -c "import sys" >nul 2>nul && set "PYEXE=py -3"
if not defined PYEXE python -c "import sys" >nul 2>nul && set "PYEXE=python"
if not defined PYEXE python3 -c "import sys" >nul 2>nul && set "PYEXE=python3"
if not defined PYEXE if exist "E:\环境依赖\Python311\python.exe" set "PYEXE=E:\环境依赖\Python311\python.exe"
if not defined PYEXE if exist "D:\Python\Python311\python.exe" set "PYEXE=D:\Python\Python311\python.exe"
if not defined PYEXE goto :NOPY
echo [Python] 使用解释器：%PYEXE%
%PYEXE% -m pip show pyinstaller >nul 2>nul || %PYEXE% -m pip install pyinstaller || goto :PIPFAIL

rem [4] 版本构建戳（界面右上角 / 工具箱版本行显示）
set "GITHASH=unknown"
for /f %%i in ('git rev-parse --short HEAD 2^>nul') do set "GITHASH=%%i"
> "source\dub_align_studio\_build_info.py" echo BUILD_STAMP = "%DATE% %TIME:~0,5% · %GITHASH%"
echo [版本] 本次构建戳：%DATE% %TIME:~0,5% · %GITHASH%

rem [5] 打包（入口必须是 launcher.py；--clean 同时清 PyInstaller 缓存）
set "PYTHONPATH=source"
%PYEXE% -m PyInstaller --noconfirm --clean --onedir --name "水星配音对齐工作室" ^
  --paths source ^
  --add-data "source\dub_align_studio\web;dub_align_studio\web" ^
  --collect-submodules dub_align_studio --collect-submodules integrated_workbench ^
  --console "source\dub_align_studio\launcher.py" || goto :BUILDFAIL

rem [6] 收尾：优先用仓库既有 打包收尾.py（写版本文件 + 自检 + 出带版本号 zip）；缺失则内联自检
if exist "打包收尾.py" (
  %PYEXE% "打包收尾.py" || goto :SELFFAIL
) else (
  echo [收尾] 未找到 打包收尾.py，改为内联自检（不生成带版本号 zip）。
  if not exist "dist\水星配音对齐工作室\水星配音对齐工作室.exe" goto :SELFFAIL
  echo [收尾] 已确认产物 exe 存在。
)

echo.
echo [完成] 产物目录：dist\水星配音对齐工作室\
echo    1. 把 ffmpeg.exe 和 ffprobe.exe 放进 dist\水星配音对齐工作室（exe 旁边），成品即自带 ffmpeg
echo    2. 双击 exe，右上角应显示版本号
echo    3. 数据总目录建议设在仓库外：D:\GitHub\By\AU^&PR\水星配音数据（勿放 dist 内，打包会清空）
pause
exit /b 0

:NOSRC
echo [错误] 未找到 source\dub_align_studio\launcher.py。
echo        打包必须在真源分支 release/v0.5-download-fix 的完整代码上进行。先对齐：
echo        git fetch origin ^&^& git checkout release/v0.5-download-fix ^&^& git pull
pause
exit /b 1

:SELFFAIL
echo [自检失败] 未在 dist\水星配音对齐工作室 找到 exe，打包可能未成功。
echo             请把上方 PyInstaller 输出整段发给开发。
pause
exit /b 1

:LOCKED
echo [错误] 旧产物 dist\水星配音对齐工作室 被占用，无法清除。请先：
echo        1. 关闭正在运行的软件窗口，含最小化的黑色控制台
echo        2. 关闭正打开 dist 文件夹的资源管理器窗口
echo        3. 暂停杀毒软件实时扫描
echo        然后重新双击本脚本。
pause
exit /b 1

:NOPY
echo [错误] 未找到可用的 Python。请先安装 Python 3.11+：
echo        官网 https://www.python.org/downloads/ ，安装时务必勾选
echo        "Add python.exe to PATH"（并建议勾 py launcher）。装完重开本窗口再双击。
echo        已装却仍报错：多半是 PATH 里的 python 是"应用商店占位符"——到
echo        设置 → 应用 → 高级应用设置 → 应用执行别名，把 python.exe / python3.exe 关掉。
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
