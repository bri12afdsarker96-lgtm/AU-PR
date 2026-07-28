@echo off
chcp 65001 >nul
setlocal EnableExtensions DisableDelayedExpansion

set "APPDIR=%~dp0"

for /f %%I in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set "STAMP=%%I"
set "OUTDIR=%USERPROFILE%\Desktop"
if not exist "%OUTDIR%" set "OUTDIR=%TEMP%"
set "LOG=%OUTDIR%\Mercury_Lite_Diag_%STAMP%.txt"
set "PS1=%TEMP%\mercury_lite_diag_%STAMP%.ps1"

set "MERCURY_DIAG_APPDIR=%APPDIR%"
set "MERCURY_DIAG_LOG=%LOG%"
set "MERCURY_DIAG_BAT=%~f0"
set "MERCURY_DIAG_PS1=%PS1%"
set "MERCURY_DIAG_TARGET=%~1"

echo Generating diagnostic log, please wait...
echo Log path: %LOG%

powershell -NoProfile -ExecutionPolicy Bypass -Command "$src=Get-Content -LiteralPath $env:MERCURY_DIAG_BAT -Raw -Encoding UTF8; $m=':__POWERSHELL__'; $i=$src.LastIndexOf($m); if($i -lt 0){throw 'payload marker not found'}; $nl=$src.IndexOf([Environment]::NewLine,$i); if($nl -lt 0){throw 'payload body not found'}; $code=$src.Substring($nl + [Environment]::NewLine.Length); Set-Content -LiteralPath $env:MERCURY_DIAG_PS1 -Value $code -Encoding UTF8"
if errorlevel 1 (
  echo Failed to create temporary diagnostic script.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set "ERR=%ERRORLEVEL%"

echo.
if "%ERR%"=="0" (
  echo Diagnostic completed.
) else (
  echo Diagnostic completed with recorded command errors.
)
echo Please send this log file back:
echo %LOG%
echo.
pause
exit /b %ERR%

:__POWERSHELL__
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

$root = [IO.Path]::GetFullPath($env:MERCURY_DIAG_APPDIR)
$log = $env:MERCURY_DIAG_LOG
$manualTarget = $env:MERCURY_DIAG_TARGET

$env:MERCURY_CLOUD_ONLY = "1"
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONHOME = Join-Path $root "python"
$env:PYTHONPATH = Join-Path $root "source"
$env:PATH = $root + ";" + $env:PYTHONHOME + ";" + (Join-Path $env:PYTHONHOME "DLLs") + ";" + $env:PATH

function Write-Log([object]$Value = "") {
    Add-Content -LiteralPath $log -Value ([string]$Value) -Encoding UTF8
}

function Section([string]$Title) {
    Write-Log ""
    Write-Log ("===== " + $Title + " =====")
}

function Run-Section([string]$Title, [scriptblock]$Block) {
    Section $Title
    try {
        & $Block 2>&1 | ForEach-Object { Write-Log $_ }
    } catch {
        Write-Log ("ERROR: " + $_.Exception.ToString())
    }
}

function Show-DirSummary([string]$Label, [string]$Path) {
    try {
        $p = [IO.Path]::GetFullPath($Path)
        if (Test-Path -LiteralPath $p) {
            $item = Get-Item -LiteralPath $p -Force
            Write-Log ("DIR " + $Label + " exists=True path=" + $p)
            if ($item.PSIsContainer) {
                Get-ChildItem -LiteralPath $p -Force |
                    Select-Object Mode, Length, LastWriteTime, Name |
                    Format-Table -AutoSize |
                    Out-String -Width 240 |
                    ForEach-Object { Write-Log $_ }
            } else {
                Write-Log ("FILE " + $Label + " size=" + $item.Length + " modified=" + $item.LastWriteTime)
            }
        } else {
            Write-Log ("DIR " + $Label + " exists=False path=" + $p)
        }
    } catch {
        Write-Log ("DIR " + $Label + " error=" + $_.Exception.Message)
    }
}

Set-Content -LiteralPath $log -Encoding UTF8 -Value "水星配音对齐工作室 - 轻量云配版诊断日志"
Write-Log ("生成时间: " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss zzz"))
Write-Log ("安装目录: " + $root)
Write-Log ("日志文件: " + $log)
if ($manualTarget) {
    Write-Log ("手动传入路径: " + $manualTarget)
}

Run-Section "系统信息" {
    [Environment]::OSVersion.VersionString
    "User=" + [Environment]::UserName
    "Computer=" + [Environment]::MachineName
    "Is64BitOS=" + [Environment]::Is64BitOperatingSystem
    Get-CimInstance Win32_OperatingSystem |
        Select-Object Caption, Version, OSArchitecture, BuildNumber |
        Format-List |
        Out-String -Width 240
}

Run-Section "关键环境变量" {
    "MERCURY_CLOUD_ONLY=" + $env:MERCURY_CLOUD_ONLY
    "PYTHONHOME=" + $env:PYTHONHOME
    "PYTHONPATH=" + $env:PYTHONPATH
    "PATH=" + $env:PATH
}

Run-Section "安装目录文件清单" {
    Get-ChildItem -LiteralPath $root -Force |
        Select-Object Mode, Length, LastWriteTime, Name |
        Format-Table -AutoSize |
        Out-String -Width 240
}

Run-Section "关键文件是否存在" {
    $names = @(
        "启动.bat",
        "诊断_轻量云配版.bat",
        "版本.txt",
        "ffmpeg.exe",
        "ffprobe.exe",
        "python\python.exe",
        "python\python311.dll",
        "python\Lib\site-packages\numpy",
        "python\Lib\site-packages\PIL",
        "python\Lib\site-packages\pycapcut",
        "source\dub_align_studio\launcher.py",
        "source\dub_align_studio\web_server.py",
        "source\dub_align_studio\render_b.py",
        "水星配音数据\组件\whisper.cpp\whisper-cli.exe",
        "水星配音数据\组件\whisper.cpp\models\ggml-tiny.bin",
        "水星配音数据\组件\whisper.cpp\models\ggml-base.bin"
    )
    foreach ($name in $names) {
        $p = Join-Path $root $name
        if (Test-Path -LiteralPath $p) {
            $i = Get-Item -LiteralPath $p -Force
            $len = if ($i.PSIsContainer) { "<DIR>" } else { $i.Length }
            $hash = ""
            if (-not $i.PSIsContainer -and $i.Length -lt 250MB) {
                try { $hash = (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash } catch { $hash = "hash_error" }
            }
            $name + "`tOK`t" + $len + "`t" + $i.LastWriteTime + "`t" + $hash
        } else {
            $name + "`tMISSING"
        }
    }
}

Run-Section "ffmpeg 路径解析" {
    cmd /d /c where ffmpeg
    cmd /d /c where ffprobe
}

Run-Section "安装目录 ffmpeg 版本" {
    $ff = Join-Path $root "ffmpeg.exe"
    $fp = Join-Path $root "ffprobe.exe"
    if (Test-Path -LiteralPath $ff) {
        & $ff -hide_banner -version | Select-Object -First 8
    } else {
        "MISSING " + $ff
    }
    if (Test-Path -LiteralPath $fp) {
        & $fp -hide_banner -version | Select-Object -First 8
    } else {
        "MISSING " + $fp
    }
}

Run-Section "进程与端口" {
    Get-Process python, pythonw, ffmpeg, ffprobe -ErrorAction SilentlyContinue |
        Select-Object Id, ProcessName, CPU, WorkingSet, Path |
        Format-Table -AutoSize |
        Out-String -Width 240
    cmd /d /c "netstat -ano | findstr :8760"
}

$py = Join-Path $root "python\python.exe"
$pythonCode = @'
import importlib
import json
import os
import platform
import sys
import traceback
from pathlib import Path

root = Path(os.environ.get("MERCURY_DIAG_APPDIR", ".")).resolve()
print("python_executable=" + sys.executable)
print("python_version=" + sys.version.replace(chr(10), " "))
print("python_platform=" + platform.platform())
print("python_cwd=" + os.getcwd())
print("root=" + str(root))

mods = [
    "dub_align_studio",
    "dub_align_studio.version",
    "dub_align_studio.settings",
    "dub_align_studio.web_server",
    "dub_align_studio.render_b",
    "numpy",
    "PIL",
    "openpyxl",
    "pycapcut",
    "imageio",
    "uiautomation",
]
for mod in mods:
    try:
        importlib.import_module(mod)
        print("IMPORT_OK " + mod)
    except Exception as exc:
        print("IMPORT_FAIL " + mod + " " + repr(exc))
        traceback.print_exc()

try:
    from dub_align_studio import settings

    print("settings_file=" + str(settings.SETTINGS_FILE))
    print("settings_file_exists=" + str(settings.SETTINGS_FILE.exists()))
    try:
        payload = settings.load_settings()
        if payload.get("dots_remote_api_key"):
            payload["dots_remote_api_key"] = "***masked***"
        print("settings_json=" + json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception as exc:
        print("settings_json_error=" + repr(exc))

    print("cloud_only=" + str(settings.cloud_only()))
    print("default_data_root=" + str(settings.default_data_root()))
    print("data_root=" + str(settings.data_root()))
    print("components_root=" + str(settings.components_root()))
    print("whisper_cli_path=" + str(settings.whisper_cli_path()))
    print("ffmpeg_tool=" + str(settings.ffmpeg_tool("ffmpeg")))
    print("ffprobe_tool=" + str(settings.ffmpeg_tool("ffprobe")))

    paths = [
        ("data_root", settings.data_root()),
        ("components", settings.components_root()),
        ("fonts", settings.fonts_dir()),
        ("voices_root", settings.data_root() / settings.DIR_VOICES),
        ("clones", settings.clones_dir()),
        ("audio", settings.audio_assets_dir()),
    ]
    for label, path in paths:
        try:
            p = Path(path)
            count = len(list(p.iterdir())) if p.exists() and p.is_dir() else -1
            print("DIR " + label + " exists=" + str(p.exists()) + " count=" + str(count) + " path=" + str(p))
        except Exception as exc:
            print("DIR_ERROR " + label + " " + repr(exc))

    from dub_align_studio.render_b import RenderConfig, _require_binaries

    cfg = RenderConfig(ffmpeg=settings.ffmpeg_tool("ffmpeg"), ffprobe=settings.ffmpeg_tool("ffprobe"))
    _require_binaries(cfg)
    print("render_preflight=OK")
except Exception as exc:
    print("APP_SETTINGS_FAIL " + repr(exc))
    traceback.print_exc()
'@

Run-Section "内置 Python 与应用导入检查" {
    if (Test-Path -LiteralPath $py) {
        $tmpPy = Join-Path $env:TEMP ("mercury_lite_diag_py_" + [Guid]::NewGuid().ToString("N") + ".py")
        Set-Content -LiteralPath $tmpPy -Value $pythonCode -Encoding UTF8
        try {
            & $py -X utf8 $tmpPy
        } finally {
            Remove-Item -LiteralPath $tmpPy -ErrorAction SilentlyContinue
        }
    } else {
        "MISSING " + $py
    }
}

Run-Section "本机服务接口" {
    foreach ($url in @(
        "http://127.0.0.1:8760/api/state",
        "http://127.0.0.1:8760/api/components"
    )) {
        try {
            "--- " + $url
            (Invoke-WebRequest -UseBasicParsing -Uri $url -TimeoutSec 5).Content
        } catch {
            "--- " + $url
            "REQUEST_FAIL " + $_.Exception.Message
        }
    }
}

Run-Section "用户配置目录" {
    $cfgHome = Join-Path $env:USERPROFILE ".dub_align_studio"
    if (Test-Path -LiteralPath $cfgHome) {
        Get-ChildItem -LiteralPath $cfgHome -Force |
            Select-Object Mode, Length, LastWriteTime, Name |
            Format-Table -AutoSize |
            Out-String -Width 240

        $settings = Join-Path $cfgHome "settings.json"
        if (Test-Path -LiteralPath $settings) {
            "--- " + $settings + " (masked)"
            try {
                $obj = Get-Content -LiteralPath $settings -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($obj.PSObject.Properties.Name -contains "dots_remote_api_key") {
                    $obj.dots_remote_api_key = "***masked***"
                }
                $obj | ConvertTo-Json -Depth 8
            } catch {
                "settings_parse_error=" + $_.Exception.Message
            }
        }

        $edit = Join-Path $cfgHome "edit_queue.json"
        if (Test-Path -LiteralPath $edit) {
            "--- " + $edit
            Get-Content -LiteralPath $edit -Raw -Encoding UTF8
        }
    } else {
        "MISSING " + $cfgHome
    }
}

Run-Section "最近输出目录线索" {
    if ($manualTarget) {
        Show-DirSummary "manual_target" $manualTarget
    }

    $cfgHome = Join-Path $env:USERPROFILE ".dub_align_studio"
    $q = Join-Path $cfgHome "edit_queue.json"
    if (Test-Path -LiteralPath $q) {
        try {
            $j = Get-Content -LiteralPath $q -Raw -Encoding UTF8 | ConvertFrom-Json
            foreach ($it in @($j.items)) {
                if ($it.output_dir) {
                    "OUTPUT_DIR=" + $it.output_dir
                    if (Test-Path -LiteralPath $it.output_dir) {
                        Get-ChildItem -LiteralPath $it.output_dir -Force |
                            Select-Object Mode, Length, LastWriteTime, Name |
                            Format-Table -AutoSize |
                            Out-String -Width 240
                        $seg = Join-Path $it.output_dir "成片_segments"
                        if (Test-Path -LiteralPath $seg) {
                            "SEGMENTS=" + $seg
                            Get-ChildItem -LiteralPath $seg -Force |
                                Select-Object -First 40 Mode, Length, LastWriteTime, Name |
                                Format-Table -AutoSize |
                                Out-String -Width 240
                        }
                    }
                }
            }
        } catch {
            "edit_queue_parse_error=" + $_.Exception.Message
        }
    } else {
        "NO edit_queue.json"
    }
}

Write-Log ""
Write-Log "===== 诊断结束 ====="
exit 0
