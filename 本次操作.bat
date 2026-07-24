@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 本次操作 · 恢复 torch 2.11 并验证 dots.tts
rem ════════════════════════════════════════════════════════════════
rem 协作约定：Claude 需要你在本地执行什么，就更新本文件并推到 GitHub。
rem 你只需：git pull → 双击本文件 → 完成后告诉 Claude「操作完成」。
rem 执行过程自动写入 本次操作日志.txt 并推回 GitHub，Claude 远程读结果。
rem ════════════════════════════════════════════════════════════════
set "LOG=本次操作日志.txt"
echo ══════════════════════════════════════════
echo   本次操作：恢复 torch 2.11.0+cu126 匹配对 → 验证 dots.tts
echo   （过程写入 %LOG% 并自动上传，无需截图）
echo ══════════════════════════════════════════
echo.

> "%LOG%" echo ══ 本次操作日志  %DATE% %TIME:~0,8% ══
>> "%LOG%" echo 操作：恢复 torch 2.11.0+cu126 匹配对 → 验证 dots.tts probe

echo [1/2] 重装 torch==2.11.0 + torchaudio==2.11.0（cu126 官方源，约 2.5GB，请耐心）...
>> "%LOG%" echo.
>> "%LOG%" echo [1/2] pip 重装 torch 匹配对：
python -m pip install --force-reinstall torch==2.11.0 torchaudio==2.11.0 --index-url https://download.pytorch.org/whl/cu126 >> "%LOG%" 2>&1
if errorlevel 1 ( echo   [失败] 安装未成功，详情已入日志。 ) else ( echo   [完成] 安装成功。 )

echo [2/2] 验证 dots.tts 引擎探测 ...
>> "%LOG%" echo.
>> "%LOG%" echo [2/2] dots.tts probe：
python -c "import sys,io;sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding='utf-8');sys.path.insert(0,'source');from dub_align_studio.engines.dots_local import DotsLocalEngine as E;s=E().probe();print('available=',s.available);print(s.detail)" >> "%LOG%" 2>&1

echo.
echo ──── 关键结果 ────
findstr /c:"available=" /c:"Successfully installed" /c:"ERROR" "%LOG%"
echo ──────────────────
echo.
echo [上传] 推送日志到 GitHub ...
git add "%LOG%"
git commit -m "本次操作日志" >nul 2>nul
git pull --rebase >nul 2>nul
git push >nul 2>nul
if errorlevel 1 (
  echo [警告] 推送失败（网络/权限）。日志已生成在本目录，可稍后重试或直接把内容粘给 Claude。
) else (
  echo ✅ 已上传。告诉 Claude「操作完成」即可。
)
pause
