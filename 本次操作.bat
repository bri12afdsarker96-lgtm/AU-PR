@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 本次操作 · 修复 torchaudio（dots.tts 依赖）
rem ════════════════════════════════════════════════════════════════
rem 协作约定：Claude 更新本文件并推到 GitHub；你 git pull 后双击本文件即可。
rem 本次任务：确保 dots.tts 依赖的 torchaudio 与 torch 同版本(CUDA/cu126)。
rem 幂等：已匹配则跳过安装、只记录；不会动已经正常的环境。日志推到 3050 分支。
rem ════════════════════════════════════════════════════════════════
set "IDX=https://download.pytorch.org/whl/cu126"
set "LOG=本次操作日志.txt"

echo ════════ 检查 torch / torchaudio 是否已匹配 ════════
python -c "import torch,torchaudio,sys;t=torch.__version__.split('+')[0].split('.')[:2];a=torchaudio.__version__.split('+')[0].split('.')[:2];print('torch',torch.__version__,'| torchaudio',torchaudio.__version__);sys.exit(0 if t==a else 1)"
if %errorlevel%==0 (
  echo [跳过] torchaudio 已安装且与 torch 小版本匹配，无需修复。
  goto VERIFY
)

echo.
echo [1/2] 安装 torchaudio（cu126）…
python -m pip install --force-reinstall torchaudio --index-url %IDX% --timeout 60 --retries 3
if errorlevel 1 goto FAIL
set "TA="
for /f "delims=" %%v in ('python -c "import importlib.metadata as m;print(m.version('torchaudio'))"') do set "TA=%%v"
if not defined TA goto FAIL
echo.
echo [2/2] 把 torch 锁到与 torchaudio 相同的版本 %TA% …
python -m pip install --force-reinstall "torch==%TA%" --index-url %IDX% --timeout 60 --retries 3
if errorlevel 1 goto FAIL

:VERIFY
echo.
echo ════════ 验证并写日志 ════════
> "%LOG%" echo 本次操作日志 · %DATE% %TIME%
>> "%LOG%" echo 任务：为 dots.tts 补齐/校验 torchaudio（cu126，与 torch 同版本）
python -c "import torch,torchaudio;print('torch      :',torch.__version__);print('torchaudio :',torchaudio.__version__);print('cuda 可用  :',torch.cuda.is_available());print('GPU        :',torch.cuda.get_device_name(0) if torch.cuda.is_available() else '无')" >> "%LOG%" 2>&1
echo -- dots.tts probe -- >> "%LOG%"
set "PYTHONPATH=source"
python -c "import sys;sys.path.insert(0,'source');from dub_align_studio.engines.dots_local import DotsLocalEngine as E;s=E().probe();print('available=',s.available);print(s.detail)" >> "%LOG%" 2>&1
type "%LOG%"

echo.
echo ════════ 推送日志到 3050 分支（git 缺失/无权限则跳过，可手动同步）════════
git add "%LOG%" 2>nul && git commit -m "3050: torchaudio 校验/修复日志" 2>nul && git pull --rebase origin 3050 2>nul && git push origin HEAD:3050 && echo [OK] 日志已推到 3050 分支。 || echo [提示] git 推送未完成（未装 git / 非 clone / 无凭据）——把上面的日志内容发给 Claude 即可。
echo.
echo 全部完成。
pause
exit /b 0

:FAIL
echo.
echo [失败] torchaudio / torch 安装失败。请确认这台电脑能联网访问 download.pytorch.org，
echo        或把上方输出发给 Claude。也可到软件「工具箱自检」点 torch/dots.tts「安装」。
pause
exit /b 1
