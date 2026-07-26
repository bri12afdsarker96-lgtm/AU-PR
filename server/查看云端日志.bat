@echo off
setlocal EnableDelayedExpansion
title 查看云端配音服务 日志/状态
color 0B
echo ============================================================
echo            查看云端「配音服务」日志 / 状态
echo ============================================================
echo.
where ssh >nul 2>nul || goto NOSSH
echo  粘贴 AutoDL/优云 控制台的「SSH 登录指令」整行
echo  形如:  ssh -p 12345 root@117.50.173.135
echo.
set "SSHCMD="
set /p "SSHCMD=在此粘贴 SSH 登录指令 后回车: "
if "!SSHCMD!"=="" goto EMPTY
set "PORT=22"
set "USERHOST="
for /f "tokens=1-4" %%a in ("!SSHCMD!") do (
    if /I "%%b"=="-p" (
        set "PORT=%%c"
        set "USERHOST=%%d"
    ) else (
        set "USERHOST=%%b"
    )
)
if "!USERHOST!"=="" goto PARSEFAIL
echo.
echo   目标: !USERHOST!   端口: !PORT!
echo   出现 password 时输入密码(右键粘贴, 不显示是正常的)
echo.
ssh -p !PORT! -o StrictHostKeyChecking=accept-new !USERHOST! "echo '=== 服务进程 ==='; ps -ef | grep dots_tts_server | grep -v grep || echo '(服务进程未运行 - 可能已崩溃)'; echo; echo '=== 8000 端口是否监听 ==='; (ss -ltnp 2>/dev/null | grep ':8000') || echo '(8000 未监听 - 还在加载模型 或 已崩溃)'; echo; echo '=== 模型缓存大小(下载进度) ==='; du -sh ~/.cache/huggingface 2>/dev/null || echo '(暂无缓存)'; echo; echo '=== 日志尾部 150 行 ==='; tail -n 150 /root/server/server.log 2>/dev/null || echo '(没有日志文件)'"
echo.
echo ============================================================
echo   把上面内容(尤其 8000 是否监听 + 日志尾部)截图发我
echo ============================================================
pause
goto END
:NOSSH
echo 未找到 ssh, 请用 Windows 10/11.
pause
goto END
:EMPTY
echo 没有输入, 已退出.
pause
goto END
:PARSEFAIL
echo 没能解析出 user@host, 请确认粘贴的是完整 SSH 指令.
pause
goto END
:END
endlocal
