@echo off
setlocal EnableDelayedExpansion
title 一键部署云配音服务器
color 0B
echo ============================================================
echo            一键部署「云配音」到 AutoDL GPU
echo ============================================================
echo.
echo  会自动: 上传服务端 - 装环境 - 启动服务
echo  你只需: 1) 粘贴 SSH 登录指令   2) 按提示输入密码
echo ------------------------------------------------------------

where ssh >nul 2>nul || goto NOSSH
where scp >nul 2>nul || goto NOSSH
if not exist "%~dp0dots_tts_server.py" goto NOFILE
if not exist "%~dp0cloud_bootstrap.sh" goto NOFILE

echo.
echo  打开 AutoDL 控制台, 复制「SSH 登录指令」整行
echo  形如:  ssh -p 12345 root@region-x.autodl.com
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
echo   目标主机: !USERHOST!
echo   SSH 端口: !PORT!
echo.
echo  下面开始, 出现 password 时请输入 AutoDL 面板的「密码」(输入时不显示是正常的)
pause

set "SSHOPT=-p !PORT! -o StrictHostKeyChecking=accept-new"
set "SCPOPT=-P !PORT! -o StrictHostKeyChecking=accept-new"

echo.
echo [1/3] 云端建目录 ...
ssh !SSHOPT! !USERHOST! "mkdir -p /root/server"
if errorlevel 1 goto FAIL

echo.
echo [2/3] 上传服务端 (按提示输入密码) ...
scp !SCPOPT! "%~dp0dots_tts_server.py" "%~dp0cloud_bootstrap.sh" "%~dp0requirements.txt" !USERHOST!:/root/server/
if errorlevel 1 goto FAIL

echo.
echo [3/3] 云端装环境并启动 (首次装包+下模型数分钟, 请耐心等) ...
ssh !SSHOPT! !USERHOST! "cd /root/server && sed -i 's/\r$//' cloud_bootstrap.sh && bash cloud_bootstrap.sh"
if errorlevel 1 goto FAIL

echo.
echo ============================================================
echo   部署完成! 接下来在软件里:
echo   1) 记下上面打印的 API Key
echo   2) AutoDL 控制台 - 本实例 - 自定义服务  拿公网 https 地址
echo   3) 软件「设置 - 云配音」填 地址 + API Key, 保存, 测试连接
echo      再到「配音引擎」下拉选: dots.tts 云 GPU 远程
echo ============================================================
echo.
pause
goto END

:NOSSH
echo.
echo  未找到 ssh/scp. 请用 Windows 10/11, 或在
echo  设置 - 应用 - 可选功能  里安装「OpenSSH 客户端」后重试.
pause
goto END

:NOFILE
echo.
echo  没找到 dots_tts_server.py / cloud_bootstrap.sh.
echo  请把本 bat 和这两个文件放在同一个 server 文件夹里.
pause
goto END

:EMPTY
echo 没有输入, 已退出.
pause
goto END

:PARSEFAIL
echo.
echo  没能解析出 user@host, 请确认粘贴的是完整 SSH 指令.
pause
goto END

:FAIL
echo.
echo  执行出错. 常见原因: 密码输错 / 网络不通 / 主机未开机.
echo  核对后重试; 反复失败请把窗口红字截图发我.
pause
goto END

:END
endlocal
