# -*- coding: utf-8 -*-
"""Runtime Application Self-Protection（RASP）—— 反调试 / 环境检测 / 完整性自检。

**定位**：本模块**只做检测**，不做具体动作（退出/lock/上报）；调用方（LicenseManager
或 launcher）根据 `RaspReport.suspicious` 决定策略。

原则：
    1. 所有检测**尽力 best-effort**，异常绝不抛（避免正常用户误退出）
    2. **软报警**优先：`suspicious=True` 但不立刻退，先给服务端一次心跳带上
       风险标记，服务端可决定是否 kick（避免误伤）
    3. 打包发行版可通过 `DUB_ALIGN_RASP_STRICT=1` 让 `LicenseManager` 立刻退

## 检测项

| 类别 | Windows | Linux/macOS |
| --- | --- | --- |
| 调试器附加 | IsDebuggerPresent + CheckRemoteDebuggerPresent + NtQueryInformationProcess | /proc/self/status TracerPid |
| 已知调试/hook 工具 | 进程枚举（frida-server/x64dbg/OllyDbg/IDA/CheatEngine） | 同 + ps aux 关键字 |
| Frida 客户端注入 | 扫描 loaded modules 有无 frida-agent | dladdr / /proc/self/maps 检 frida |
| 虚拟机/沙箱 | WMI Model 关键字 (VirtualBox/VMware/QEMU) | dmi/product_name |
| exe 完整性 | 自 SHA-256 校验（有 baseline 时） | 同 |
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import platform
import subprocess
import sys
from dataclasses import dataclass, field
from functools import lru_cache


# 已知调试/hook/逆向工具的**进程名关键字**（不区分大小写）
_SUSPECT_PROCESSES = (
    "frida-server", "frida-agent", "frida-helper",
    "x32dbg", "x64dbg", "ollydbg", "ollyice", "windbg",
    "idaq", "idaq64", "ida.exe", "ida64.exe", "ida64",
    "cheatengine", "processhacker", "procexp", "procmon",
    "hookinjector", "scylla", "resourcehacker",
    "de4dot", "reflector", "dnspy", "ilspy",
)

# 已知模拟器/虚拟机指纹（WMI Manufacturer / Model / dmi 关键字）
_VM_KEYWORDS = (
    "vmware", "virtualbox", "vbox", "qemu", "kvm",
    "xen", "parallels", "hyper-v", "hyper-v virtual",
    "bhyve", "microsoft corporation virtual",
)


@dataclass
class RaspReport:
    debugger_attached: bool = False
    suspect_processes: list[str] = field(default_factory=list)
    frida_detected: bool = False
    vm_detected: bool = False
    integrity_ok: bool | None = None   # None = 未校验/无 baseline
    integrity_expected: str = ""
    integrity_actual: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        return bool(
            self.debugger_attached or self.suspect_processes
            or self.frida_detected or self.vm_detected
            or self.integrity_ok is False
        )

    def summary(self) -> str:
        if not self.suspicious:
            return "clean"
        bits = []
        if self.debugger_attached: bits.append("debugger")
        if self.suspect_processes:
            bits.append(f"proc({','.join(self.suspect_processes[:3])})")
        if self.frida_detected: bits.append("frida")
        if self.vm_detected: bits.append("vm")
        if self.integrity_ok is False: bits.append("tamper")
        return "|".join(bits)


# --------------------------------------------------------------------------
# 反调试
# --------------------------------------------------------------------------


def _win_is_debugger_present() -> bool:
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        return bool(k32.IsDebuggerPresent())
    except Exception:  # noqa: BLE001
        return False


def _win_check_remote_debugger() -> bool:
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        h = k32.GetCurrentProcess()
        flag = ctypes.c_int(0)
        ok = k32.CheckRemoteDebuggerPresent(h, ctypes.byref(flag))
        return bool(ok and flag.value)
    except Exception:  # noqa: BLE001
        return False


def _linux_tracer_pid() -> int:
    try:
        with open("/proc/self/status", "r") as f:
            for line in f:
                if line.startswith("TracerPid:"):
                    try:
                        return int(line.split(":", 1)[1].strip())
                    except ValueError:
                        return 0
    except OSError:
        pass
    return 0


def debugger_attached() -> bool:
    system = platform.system().lower()
    if "windows" in system:
        return _win_is_debugger_present() or _win_check_remote_debugger()
    if "linux" in system or "darwin" in system:
        return _linux_tracer_pid() > 0
    return False


# --------------------------------------------------------------------------
# 可疑进程枚举
# --------------------------------------------------------------------------


def _list_processes_windows() -> list[str]:
    """`tasklist /fo csv` 输出全部进程名（小写）。best-effort。"""
    try:
        out = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5.0, text=True, encoding="utf-8", errors="replace",
        )
        if out.returncode != 0:
            return []
        names: list[str] = []
        for line in (out.stdout or "").splitlines():
            # "process.exe","1234","Session","0","1,234 K"
            parts = line.split(",", 1)
            if not parts:
                continue
            names.append(parts[0].strip('"').strip().lower())
        return names
    except Exception:  # noqa: BLE001
        return []


def _list_processes_posix() -> list[str]:
    try:
        out = subprocess.run(
            ["ps", "axo", "comm"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=5.0, text=True, encoding="utf-8", errors="replace",
        )
        if out.returncode != 0:
            return []
        return [line.strip().lower() for line in (out.stdout or "").splitlines()]
    except Exception:  # noqa: BLE001
        return []


def suspect_processes() -> list[str]:
    system = platform.system().lower()
    names = _list_processes_windows() if "windows" in system \
        else _list_processes_posix()
    hits: list[str] = []
    for n in names:
        for key in _SUSPECT_PROCESSES:
            if key in n and n not in hits:
                hits.append(n)
                break
    return hits


# --------------------------------------------------------------------------
# Frida 注入检测
# --------------------------------------------------------------------------


def frida_detected() -> bool:
    """Frida 注入的典型痕迹：
    - Windows：进程内加载了 frida-agent-* 模块
    - Linux：/proc/self/maps 里出现 frida 相关 .so
    - 端口 27042 有 frida-server 监听（副信号）
    """
    system = platform.system().lower()
    if "linux" in system:
        try:
            with open("/proc/self/maps", "r") as f:
                for line in f:
                    if "frida" in line.lower():
                        return True
        except OSError:
            pass
    if "windows" in system:
        try:
            # ctypes 枚举模块名（简化：查 kernel32 的 GetModuleHandleW）
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            for mod in ("frida-agent-32.dll", "frida-agent-64.dll",
                        "frida-agent.dll", "frida-gadget.dll"):
                if k32.GetModuleHandleW(ctypes.c_wchar_p(mod)):
                    return True
        except Exception:  # noqa: BLE001
            pass
    # 副信号（不作强判：仅当上面命中时才归入 frida_detected）
    return False


# --------------------------------------------------------------------------
# 虚拟机 / 沙箱
# --------------------------------------------------------------------------


def vm_detected() -> bool:
    system = platform.system().lower()
    if "linux" in system:
        for path in ("/sys/class/dmi/id/product_name",
                      "/sys/class/dmi/id/sys_vendor",
                      "/sys/class/dmi/id/board_vendor"):
            try:
                text = open(path).read().strip().lower()
                for k in _VM_KEYWORDS:
                    if k in text:
                        return True
            except OSError:
                continue
    if "windows" in system:
        try:
            out = subprocess.run(
                ["wmic", "computersystem", "get", "manufacturer,model", "/format:csv"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=5.0, text=True, encoding="utf-8", errors="replace",
            )
            text = (out.stdout or "").lower()
            for k in _VM_KEYWORDS:
                if k in text:
                    return True
        except Exception:  # noqa: BLE001
            pass
    return False


# --------------------------------------------------------------------------
# 完整性自检（exe SHA-256 vs baseline）
# --------------------------------------------------------------------------


def _read_baseline_hash() -> str:
    """从 sys._MEIPASS（PyInstaller）或环境变量读期望 hash；无则返回空。

    发行版打包时应该：
      1) 编译产物 sha256 计算
      2) 把 hash 写入 `<dist>/integrity.hash`（或环境变量 DUB_ALIGN_INTEGRITY_HASH）
      3) 运行时 rasp.integrity_check() 拉出对比
    """
    env = os.environ.get("DUB_ALIGN_INTEGRITY_HASH", "").strip().lower()
    if env:
        return env
    # 尝试从 sys.executable 同级目录读 integrity.hash
    try:
        exe = sys.executable
        if exe:
            side = os.path.join(os.path.dirname(exe), "integrity.hash")
            if os.path.exists(side):
                return open(side).read().strip().lower()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def integrity_check() -> tuple[bool | None, str, str]:
    """校验 sys.executable 的 SHA-256。
    返回 (ok_or_None, expected, actual)。
    - None：无 baseline，跳过（不算失败）
    - True：一致
    - False：不一致 → 视为被篡改
    """
    expected = _read_baseline_hash()
    if not expected:
        return None, "", ""
    try:
        actual = _hash_file(sys.executable)
    except Exception:  # noqa: BLE001
        return False, expected, ""
    return (actual == expected), expected, actual


# --------------------------------------------------------------------------
# 敏感字符串加密（编译期填入密文；运行时用 xor 解）
# --------------------------------------------------------------------------


def xor_bytes(data: bytes, key: bytes) -> bytes:
    if not key:
        return data
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def decrypt_str(cipher_hex: str, key: bytes) -> str:
    """把打包期埋入的 xor 密文（hex）解出来。key 建议来自
    机器特征 + 编译常量的拼接，不写在源码里。"""
    try:
        raw = bytes.fromhex(cipher_hex)
    except (ValueError, TypeError):
        return ""
    try:
        return xor_bytes(raw, key).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------
# 组合报告 · 供 LicenseManager 一次性调用
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _process_scan_once() -> list[str]:
    """一次性扫描；避免每次 heartbeat 都跑 ps/tasklist。"""
    return suspect_processes()


def full_scan(*, refresh_processes: bool = False) -> RaspReport:
    """一次性做完全部检测。process 扫描默认缓存（进程列表变化频率低）。
    调用方可传 `refresh_processes=True` 强制重扫。"""
    if refresh_processes:
        _process_scan_once.cache_clear()
    rep = RaspReport()
    try:
        rep.debugger_attached = debugger_attached()
    except Exception:  # noqa: BLE001
        rep.notes.append("debugger check failed")
    try:
        rep.suspect_processes = _process_scan_once()
    except Exception:  # noqa: BLE001
        rep.notes.append("process scan failed")
    try:
        rep.frida_detected = frida_detected()
    except Exception:  # noqa: BLE001
        rep.notes.append("frida check failed")
    try:
        rep.vm_detected = vm_detected()
    except Exception:  # noqa: BLE001
        rep.notes.append("vm check failed")
    ok, exp, act = integrity_check()
    rep.integrity_ok = ok
    rep.integrity_expected = exp
    rep.integrity_actual = act
    return rep


def strict_mode_enabled() -> bool:
    """`DUB_ALIGN_RASP_STRICT=1` → RASP 触发即退。默认软报警。"""
    v = os.environ.get("DUB_ALIGN_RASP_STRICT", "").strip().lower()
    return v in ("1", "true", "yes", "on")
