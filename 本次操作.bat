@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 本次操作 · 迁移收尾：修 CUDA torch + 重指数据目录
setlocal
rem ════════════════════════════════════════════════════════════════
rem 放到 D:\Mercury\AU-PR 里双击（迁移后的新仓库根目录）。做两件事：
rem  1) 把 torch 换回 CUDA 版 2.11.0+cu126（fish-speech 把它降成了 2.8.0 CPU，dots.tts 用不了）；
rem  2) 把软件「数据总目录」重新指向 D:\Mercury\水星配音数据（旧设置还指着已删的旧路径）。
rem 然后验证 cuda / dots.tts / data_root。不删任何东西。
rem ════════════════════════════════════════════════════════════════
set "IDX=https://download.pytorch.org/whl/cu126"
set "PYTHONPATH=source"
for %%i in ("%~dp0..") do set "PARENT=%%~fi"
set "NEWDATA=%PARENT%\水星配音数据"
set "LOG=%~dp0本次操作日志.txt"

echo ════════ 1/3 换回 CUDA 版 torch（2.11.0+cu126）════════
echo （fish-speech 依赖把 torch 降成了 2.8.0 CPU，导致 dots.tts 检测不到 CUDA）
python -m pip install --force-reinstall torch==2.11.0 torchaudio==2.11.0 --index-url %IDX% --timeout 60 --retries 3
if errorlevel 1 goto FAIL

echo.
echo ════════ 2/3 数据总目录重指向 "%NEWDATA%" ════════
if exist "%NEWDATA%" (
  python -c "import sys;sys.path.insert(0,'source');from dub_align_studio import settings;print('已设为:',settings.set_data_root(r'%NEWDATA%'))"
) else (
  echo [警告] 没找到 "%NEWDATA%" —— 请确认数据已在此处；跳过设置。
)

echo.
echo ════════ 3/3 验证 ════════
> "%LOG%" echo 本次操作日志 · %DATE% %TIME%
>> "%LOG%" echo 任务：迁移收尾（CUDA torch + 数据目录重指向）
python -c "import torch,torchaudio;print('torch      :',torch.__version__);print('torchaudio :',torchaudio.__version__);print('cuda 可用  :',torch.cuda.is_available());print('GPU        :',torch.cuda.get_device_name(0) if torch.cuda.is_available() else '无')" >> "%LOG%" 2>&1
python -c "import sys;sys.path.insert(0,'source');from dub_align_studio import settings;print('data_root  :',settings.data_root())" >> "%LOG%" 2>&1
python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.engines.dots_local import DotsLocalEngine as E;s=E().probe();print('dots.tts   :','available=',s.available)" >> "%LOG%" 2>&1
type "%LOG%"

echo.
echo ════════ 完成 ════════
echo 若上面 cuda 可用=True 且 dots.tts available=True，就成功了——把「本次操作日志.txt」发我。
echo 确认新目录能配音/出片后，可删旧目录：  rmdir /s /q "D:\GitHub\By\AU&PR"
echo ⚠ 别再点 fish-speech「安装」——它会把 torch 再次降级成 2.8.0 CPU，弄坏 dots.tts。
pause
exit /b 0

:FAIL
echo.
echo [失败] torch 安装失败。请确认能联网访问 download.pytorch.org（或校园/公司网代理），
echo        或到软件「工具箱自检」点 PyTorch GPU「安装」。把上方输出发我也行。
pause
exit /b 1
