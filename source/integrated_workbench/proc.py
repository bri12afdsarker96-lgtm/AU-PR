"""子进程静默运行：Windows 上隐藏控制台窗口，杜绝 ffmpeg/ffprobe/whisper 等黑框弹窗。

问题：GUI 程序里用 subprocess 调命令行工具，Windows 会为每个子进程弹出一个控制台窗口，
量产/剪辑时“黑框一直跳”。修法：给子进程加 CREATE_NO_WINDOW + 隐藏 STARTUPINFO。
非 Windows 无影响。仅用于命令行工具；启动 GUI 程序不要用这里（会把窗口也藏了）。
"""

from __future__ import annotations

import subprocess
import sys
import threading

_IS_WINDOWS = sys.platform.startswith("win")
# CREATE_NO_WINDOW = 0x08000000（不新建控制台窗口）
_CREATE_NO_WINDOW = 0x08000000

# 活动子进程登记簿：UI 的“停止任务”通过 terminate_active() 立刻终止在跑的 ffmpeg 等。
_ACTIVE: set = set()
_ACTIVE_LOCK = threading.Lock()


def _register(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.add(proc)


def _unregister(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(proc)


def terminate_active() -> int:
    """终止所有登记在册且仍在运行的子进程，返回终止数。已结束的顺带清理。"""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    stopped = 0
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
                stopped += 1
        except Exception:
            pass
        _unregister(proc)
    return stopped


def _hidden_kwargs(kwargs: dict) -> dict:
    """把隐藏控制台的参数合并进 kwargs（仅 Windows 生效）。"""
    if not _IS_WINDOWS:
        return kwargs
    kwargs = dict(kwargs)
    kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | _CREATE_NO_WINDOW
    try:
        startupinfo = kwargs.get("startupinfo") or subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        kwargs["startupinfo"] = startupinfo
    except Exception:
        pass
    return kwargs


def run_silent(cmd, **kwargs) -> subprocess.CompletedProcess:
    """等价于 subprocess.run，但在 Windows 上不弹控制台窗口。

    进程会登记在册：UI 的“停止任务”可随时 terminate_active() 终止在跑的子进程
    （被终止后 returncode 非 0，走调用方既有的失败/降级路径）。
    """
    kwargs = dict(kwargs)
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    check = kwargs.pop("check", False)
    timeout = kwargs.pop("timeout", None)
    input_data = kwargs.pop("input", None)
    if input_data is not None:
        kwargs["stdin"] = subprocess.PIPE
    proc = subprocess.Popen(cmd, **_hidden_kwargs(kwargs))
    _register(proc)
    try:
        stdout, stderr = proc.communicate(input=input_data, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise
    finally:
        _unregister(proc)
    completed = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, stdout, stderr)
    return completed


def popen_silent(cmd, **kwargs) -> subprocess.Popen:
    """等价于 subprocess.Popen，但在 Windows 上不弹控制台窗口（仅命令行工具用）。

    同样登记在册；进程结束后由 terminate_active() 顺带清理登记。
    """
    proc = subprocess.Popen(cmd, **_hidden_kwargs(kwargs))
    _register(proc)
    return proc


def check_output_silent(cmd, **kwargs) -> str:
    """等价于 subprocess.check_output(text=True)，但在 Windows 上不弹控制台窗口。"""
    kwargs.setdefault("text", True)
    completed = run_silent(cmd, stdout=subprocess.PIPE, stderr=kwargs.pop("stderr", subprocess.PIPE), **kwargs)
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, cmd, completed.stdout, completed.stderr)
    return completed.stdout
