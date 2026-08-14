@echo off
setlocal EnableDelayedExpansion
title 一键启动云配音服务（不重装，秒起）
color 0B
echo ============================================================
echo        一键启动「云配音」服务（关机重开后用，不重装）
echo ============================================================
echo.
echo  依赖和模型都还在云主机硬盘上，这个只把服务拉起来，很快。
echo  你只需: 1) 粘贴 SSH 登录指令   2) 按提示输入密码
echo ------------------------------------------------------------

where ssh >nul 2>nul || goto NOSSH
echo.
echo  粘贴 优云 控制台的「SSH 登录指令」整行，形如:
echo    ssh -p 12345 root@117.50.173.135
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
echo   出现 password 时输入密码（右键粘贴，不显示是正常的）
echo.

rem 只启动、不重装：SKIP_INSTALL=1
ssh -p !PORT! -o StrictHostKeyChecking=accept-new !USERHOST! "cd /root/server && sed -i 's/\r$//' cloud_bootstrap.sh && SKIP_INSTALL=1 bash cloud_bootstrap.sh"
if errorlevel 1 goto FAIL

echo.
echo ============================================================
echo  服务已拉起。等 1-3 分钟模型加载完，回软件点「测试连接」。
echo  （模型已缓存，不会重新下载；这次只是重新加载进显存）
echo ============================================================
echo.
pause
goto END

:NOSSH
echo 未找到 ssh，请用 Windows 10/11.
pause
goto END
:EMPTY
echo 没有输入，已退出.
pause
goto END
:PARSEFAIL
echo 没能解析出 user@host，请确认粘贴的是完整 SSH 指令.
pause
goto END
:FAIL
echo.
echo  启动出错。常见: 密码错 / 主机没开机 / 首次未部署过。
echo  若从没部署过，请先跑「一键部署到云GPU.bat」。
pause
goto END
:END
endlocal
