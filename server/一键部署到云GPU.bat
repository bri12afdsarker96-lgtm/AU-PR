@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
title 一键部署云配音服务器（AutoDL）
color 0B

echo ============================================================
echo            一键部署「云配音」到你的 AutoDL GPU
echo ============================================================
echo.
echo  这个脚本会自动把服务端传到云主机、装好环境并启动。
echo  你只需要做两件事：
echo    1) 粘贴 AutoDL 面板上的「SSH 登录指令」
echo    2) 在提示时输入 AutoDL 面板上的「密码」（可能要输入 1-2 次）
echo.
echo ------------------------------------------------------------

rem —— 检查 Windows 自带 ssh/scp ——
where ssh >nul 2>nul || goto :NOSSH
where scp >nul 2>nul || goto :NOSSH

rem —— 检查服务端文件是否在本文件夹旁 ——
if not exist "%~dp0dots_tts_server.py" goto :NOFILE
if not exist "%~dp0cloud_bootstrap.sh" goto :NOFILE

echo.
echo  请打开 AutoDL 控制台，复制「SSH 登录指令」整行，
echo  形如：  ssh -p 12345 root@region-x.autodl.com
echo.
set "SSHCMD="
set /p SSHCMD=在此粘贴 SSH 登录指令，然后回车：
if "!SSHCMD!"=="" (echo 没有输入，已退出。& pause & exit /b 1)

rem —— 从 SSH 指令里解析端口和 user@host ——
set "PORT=22"
set "USERHOST="
set "NEXTPORT="
for %%A in (%SSHCMD%) do (
    if defined NEXTPORT (
        set "PORT=%%A"
        set "NEXTPORT="
    ) else (
        if /I "%%A"=="-p" (
            set "NEXTPORT=1"
        ) else (
            echo %%A | find "@" >nul && set "USERHOST=%%A"
        )
    )
)

if "!USERHOST!"=="" (
    echo.
    echo  没能从指令里解析出 user@host，请确认粘贴的是完整 SSH 指令。
    pause & exit /b 1
)

echo.
echo ------------------------------------------------------------
echo   目标主机 : !USERHOST!
echo   SSH 端口 : !PORT!
echo ------------------------------------------------------------
echo.
echo  接下来会：① 在云端建目录 ② 上传服务端 ③ 装环境并启动
echo  提示 password 时，请输入 AutoDL 面板上的「密码」。
echo.
pause

set "SSHOPT=-p !PORT! -o StrictHostKeyChecking=accept-new"
set "SCPOPT=-P !PORT! -o StrictHostKeyChecking=accept-new"

echo.
echo [1/3] 在云端创建目录 /root/server ...
ssh %SSHOPT% !USERHOST! "mkdir -p /root/server"
if errorlevel 1 goto :FAIL

echo.
echo [2/3] 上传服务端文件（请按提示输入密码）...
scp %SCPOPT% "%~dp0dots_tts_server.py" "%~dp0cloud_bootstrap.sh" "%~dp0requirements.txt" !USERHOST!:/root/server/
if errorlevel 1 goto :FAIL

echo.
echo [3/3] 云端装环境并启动服务（首次装包+下模型需几分钟，请耐心等待）...
ssh %SSHOPT% !USERHOST! "cd /root/server && sed -i 's/\r$//' cloud_bootstrap.sh && bash cloud_bootstrap.sh"
if errorlevel 1 goto :FAIL

echo.
echo ============================================================
echo   部署完成！最后两步（在软件里做）：
echo   1) 记下上面打印的「API Key」
echo   2) AutoDL 控制台 - 本实例 -「自定义服务」拿到公网 https 地址
echo   然后打开软件：设置 - 云配音，填「地址 + API Key」-保存-测试连接，
echo   再到「配音引擎」下拉选：dots.tts（云 GPU · 远程）。
echo ============================================================
echo.
pause
exit /b 0

:NOSSH
echo.
echo  未找到 ssh/scp。请升级到 Windows 10/11（自带 OpenSSH），
echo  或在「设置-应用-可选功能」里安装「OpenSSH 客户端」后重试。
echo.
pause & exit /b 1

:NOFILE
echo.
echo  没找到 dots_tts_server.py / cloud_bootstrap.sh。
echo  请把本 bat 和这两个文件放在同一个 server 文件夹里再运行。
echo.
pause & exit /b 1

:FAIL
echo.
echo  执行出错。常见原因：密码输错、网络不通、或云主机未开机。
echo  请核对后重试；如反复失败，把窗口里的红字截图发我。
echo.
pause & exit /b 1
