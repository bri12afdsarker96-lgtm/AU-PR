# -*- coding: utf-8 -*-
"""稳定设备指纹（HWID / machine_id）。

生成规则（Windows 主）：
    1) 取 Windows machineguid（HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid）
    2) 取 C: 卷序列号（vol）
    3) 取 CPU 型号名（platform.processor）
    4) SHA-256(拼接) 前 32 位 hex

Linux 兜底：/etc/machine-id + MAC + CPU；macOS：ioreg + MAC。

**稳定性契约**：
    - 同一设备重装 OS 后指纹会变（可接受，等同新设备）
    - 换硬盘/换主板 → 变（等同新设备）
    - 网卡变化不参与（避免虚拟网卡/VPN 干扰）
    - Docker/WSL 里跑 → 会有稳定但不同于宿主的指纹
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
from functools import lru_cache

# 冻结 exe（PyInstaller windowed）里 subprocess 默认会弹黑色 cmd 窗，
# 必须传 CREATE_NO_WINDOW 抑制（否则启动算 HWID 时闪黑窗）。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run_ok(cmd: list[str], timeout: float = 3.0) -> str:
    try:
        r = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW,
        )
        if r.returncode == 0:
            return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _windows_machine_guid() -> str:
    """HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid —— Windows 内建的
    机器级唯一 ID，装系统时生成，重装才变。"""
    try:
        import winreg  # noqa: PLC0415
        for hive in (winreg.HKEY_LOCAL_MACHINE,):
            for path in (r"SOFTWARE\Microsoft\Cryptography",):
                for view in (winreg.KEY_WOW64_64KEY, 0):
                    try:
                        with winreg.OpenKey(
                            hive, path, 0, winreg.KEY_READ | view,
                        ) as k:
                            v, _ = winreg.QueryValueEx(k, "MachineGuid")
                            if v:
                                return str(v)
                    except OSError:
                        continue
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _windows_c_drive_serial() -> str:
    """C: 卷序列号——重新格式化系统盘才变。"""
    raw = _run_ok(["cmd", "/c", "vol", "C:"])
    # 输出形如 "驱动器 C 中的卷序列号是 XXXX-XXXX"
    for token in raw.replace("\r", " ").split():
        if "-" in token and len(token) in (9,) and all(
            c in "0123456789ABCDEFabcdef-" for c in token
        ):
            return token.upper()
    return ""


def _linux_machine_id() -> str:
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            v = open(p, "r").read().strip()
            if v:
                return v
        except OSError:
            continue
    return ""


def _macos_hardware_uuid() -> str:
    raw = _run_ok(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
    for line in raw.splitlines():
        if "IOPlatformUUID" in line:
            parts = line.split("=", 1)
            if len(parts) == 2:
                return parts[1].strip().strip('"')
    return ""


def _cpu_signature() -> str:
    return (platform.processor() or platform.machine() or "").strip()


@lru_cache(maxsize=1)
def machine_id() -> str:
    """返回本机稳定指纹（32 位 hex 字符串，至少 8 位——满足 API 契约）。"""
    parts: list[str] = []
    system = platform.system().lower()
    if "windows" in system:
        parts.append(_windows_machine_guid())
        parts.append(_windows_c_drive_serial())
    elif "linux" in system:
        parts.append(_linux_machine_id())
    elif "darwin" in system:
        parts.append(_macos_hardware_uuid())
    parts.append(_cpu_signature())
    parts.append(platform.node() or "")  # hostname
    parts.append(system)

    material = "|".join(x for x in parts if x)
    if not material:
        # 极端兜底：随机 UUID 落到用户目录，重启也保持（但**不再稳定跨用户目录**）
        try:
            fallback_path = os.path.expanduser(
                "~/.dub_align_studio/machine_id_fallback"
            )
            if os.path.exists(fallback_path):
                material = open(fallback_path).read().strip() or "unknown"
            else:
                import uuid as _uuid
                material = _uuid.uuid4().hex
                os.makedirs(os.path.dirname(fallback_path), exist_ok=True)
                with open(fallback_path, "w") as f:
                    f.write(material)
        except Exception:  # noqa: BLE001
            material = "unknown-machine"
    return hashlib.sha256(material.encode("utf-8", "replace")).hexdigest()[:32]


def device_name() -> str:
    """人类可读的设备名，供后台展示。"""
    return (platform.node() or "unknown-device")[:80]


def system_version() -> str:
    """OS 版本字符串。"""
    return f"{platform.system()} {platform.release()}"[:80]
