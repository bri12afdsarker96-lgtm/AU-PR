# -*- coding: utf-8 -*-
"""本地敏感字段加密（session_token / code）持久化。

策略：
    * Windows：优先 **DPAPI**（CryptProtectData / CryptUnprotectData），
      密钥绑到当前用户账户，只有同用户能解，跨机复制文件无用。
    * 其他平台：优先 `keyring`（若已装）；否则 xor(machine_id_hex) 兜底
      （**弱加密**——只挡新手，能挡不到专业攻击）。

对上层的 API：
    encrypt(plain) -> str  （返回带前缀的 encoded 字符串，可直接落 JSON）
    decrypt(encoded) -> str

**注意**：本模块不引入新依赖；keyring 缺失时静默降级。
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import platform
from ctypes import wintypes


_PREFIX_DPAPI = "dpapi:"
_PREFIX_KEYRING = "keyring:"
_PREFIX_XOR = "xor:"
_PREFIX_PLAIN = "plain:"


# --------------------------------------------------------------------------
# Windows DPAPI
# --------------------------------------------------------------------------


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                 ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_available() -> bool:
    return platform.system().lower().startswith("win")


def _dpapi_encrypt(plain: str) -> str:
    """DPAPI CryptProtectData → hex encoded；失败抛。"""
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    data = plain.encode("utf-8")
    in_blob = _DATA_BLOB(len(data),
                          ctypes.cast(ctypes.c_char_p(data),
                                       ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, None, None, None,
        0, ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(f"CryptProtectData failed: {ctypes.get_last_error()}")
    try:
        raw = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        try:
            kernel32.LocalFree(out_blob.pbData)
        except Exception:  # noqa: BLE001
            pass
    return _PREFIX_DPAPI + base64.b64encode(raw).decode("ascii")


def _dpapi_decrypt(encoded: str) -> str:
    if not encoded.startswith(_PREFIX_DPAPI):
        raise ValueError("not a DPAPI-encoded value")
    raw = base64.b64decode(encoded[len(_PREFIX_DPAPI):].encode("ascii"))
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    in_blob = _DATA_BLOB(len(raw),
                          ctypes.cast(ctypes.c_char_p(raw),
                                       ctypes.POINTER(ctypes.c_char)))
    out_blob = _DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        0, ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError(f"CryptUnprotectData failed: {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData).decode(
            "utf-8", errors="replace",
        )
    finally:
        try:
            kernel32.LocalFree(out_blob.pbData)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# keyring 兜底
# --------------------------------------------------------------------------


def _keyring_available() -> bool:
    try:
        import keyring  # noqa: PLC0415, F401
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------
# xor 弱加密兜底（只挡文本 grep）
# --------------------------------------------------------------------------


def _xor_key() -> bytes:
    """派生：SHA-256("dub_align_studio_local_v1" + hostname).digest()[:32]。
    不用 machine_id 避免循环依赖（licensing 里也用 machine_id）。"""
    import socket
    h = hashlib.sha256()
    h.update(b"dub_align_studio_local_v1|")
    h.update((socket.gethostname() or "unknown").encode("utf-8", "replace"))
    return h.digest()[:32]


def _xor_encrypt(plain: str) -> str:
    key = _xor_key()
    data = plain.encode("utf-8")
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return _PREFIX_XOR + base64.b64encode(out).decode("ascii")


def _xor_decrypt(encoded: str) -> str:
    if not encoded.startswith(_PREFIX_XOR):
        raise ValueError("not xor-encoded")
    raw = base64.b64decode(encoded[len(_PREFIX_XOR):].encode("ascii"))
    key = _xor_key()
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
    return out.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------
# 对外 API
# --------------------------------------------------------------------------


def encrypt(plain: str) -> str:
    """把 plain 加密为带前缀的字符串（可直接落 JSON）。"""
    if not plain:
        return ""
    if _dpapi_available():
        try:
            return _dpapi_encrypt(plain)
        except Exception:  # noqa: BLE001
            pass
    if _keyring_available():
        # keyring 不是加密"字符串"，而是"存"。为保持相同 API 契约（拿到字符串
        # 直接落文件），我们仍走 xor（内部）。keyring 真正用法是把 token 存进
        # 系统钥匙串而不是文件，超出本 MVP 范围。
        pass
    # 兜底：xor（弱加密 + 明确前缀，让排查一眼看出用了哪一档）
    return _xor_encrypt(plain)


def decrypt(encoded: str) -> str:
    if not encoded:
        return ""
    if encoded.startswith(_PREFIX_DPAPI):
        try:
            return _dpapi_decrypt(encoded)
        except Exception:  # noqa: BLE001
            return ""
    if encoded.startswith(_PREFIX_XOR):
        try:
            return _xor_decrypt(encoded)
        except Exception:  # noqa: BLE001
            return ""
    if encoded.startswith(_PREFIX_PLAIN):
        return encoded[len(_PREFIX_PLAIN):]
    # 老版本明文（无前缀）向后兼容
    return encoded


def backend_name() -> str:
    """当前实际使用的后端 —— 供 UI/诊断展示。"""
    if _dpapi_available():
        return "dpapi"
    return "xor-fallback"
