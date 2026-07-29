"""打包 exe ↔ 系统 Python 桥接：轻量包不带 torch/dots.tts 等重依赖（轻量安装纪律），
但本机系统 Python 里往往已装好——frozen 启动时把「同 major.minor 版本」的系统 Python
的 site-packages 追加进 sys.path（打包内模块仍优先），dots.tts/fish 依赖即刻可用；
工具箱的 pip 安装/版本检测也统一路由到该系统 Python，装的东西两边都认。

找不到同版本系统 Python 时不报错——保持轻量包原行为（mock/外接引擎可用），
探测文案会指引用户装 Python 3.11 或改用整合离线版。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_cached: Path | None | bool = None      # None=未探测；False=确认没有；Path=找到
_cached_prefix: Path | None = None


def _probe(exe: Path) -> tuple[tuple[int, int], Path] | None:
    try:
        out = subprocess.check_output(
            [str(exe), "-c",
             "import sys,json;print(json.dumps([list(sys.version_info[:2]),sys.base_prefix]))"],
            text=True, timeout=15, stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        ver, prefix = json.loads(out.strip().splitlines()[-1])
        return (int(ver[0]), int(ver[1])), Path(prefix)
    except Exception:
        return None


def system_python() -> Path | None:
    """与当前进程同 major.minor 的系统 Python（frozen 桥接与 pip 安装用）。"""
    global _cached, _cached_prefix
    if _cached is not None:
        return _cached if isinstance(_cached, Path) else None
    want = sys.version_info[:2]
    candidates: list[Path] = []
    which = shutil.which("python")
    if which:
        candidates.append(Path(which))
    for root in ("C:/", "D:/", "E:/"):
        for pattern in ("Python/Python3*/python.exe", "Python3*/python.exe",
                        "Program Files/Python3*/python.exe"):
            try:
                candidates.extend(Path(root).glob(pattern))
            except OSError:
                pass
    seen = set()
    for exe in candidates:
        exe = exe.resolve()
        if exe in seen or not exe.is_file():
            continue
        seen.add(exe)
        probed = _probe(exe)
        if probed and probed[0] == tuple(want):
            _cached, _cached_prefix = exe, probed[1]
            return exe
    _cached = False
    return None


def bridge_site_packages() -> str:
    """frozen 时把系统 Python 的 Lib/site-packages 等目录追加进 sys.path。返回给用户看的一句话。"""
    if not getattr(sys, "frozen", False):
        return ""
    exe = system_python()
    if exe is None:
        return ("提示：未找到本机 Python %d.%d —— dots.tts/fish 需要它（或改用整合离线版）；"
                "mock 测试与外接引擎不受影响。" % sys.version_info[:2])
    prefix = _cached_prefix or exe.parent
    added = []
    for sub in ("Lib/site-packages", "Lib", "DLLs"):
        path = prefix / sub
        if path.is_dir() and str(path) not in sys.path:
            sys.path.append(str(path))     # 追加在尾部：打包内自带模块优先，绝不被系统包顶掉
            added.append(sub)
    for dll_dir in (prefix, prefix / "DLLs"):
        try:
            if dll_dir.is_dir():
                os.add_dll_directory(str(dll_dir))
        except (OSError, AttributeError):
            pass
    if added:
        return f"已桥接系统 Python：{prefix}（dots.tts/torch 等直接用它 site-packages 里已装的依赖）。"
    return ""
