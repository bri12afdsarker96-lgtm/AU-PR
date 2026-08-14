# -*- coding: utf-8 -*-
"""RASP + crypto_store 单元测试。

覆盖：
    1. rasp.RaspReport.suspicious 逻辑
    2. debugger_attached 兜底不抛
    3. suspect_processes 缓存 + 强制刷新
    4. integrity_check 无 baseline → None
    5. integrity_check 有 baseline 且匹配 → True
    6. xor_bytes / decrypt_str 双向
    7. crypto_store.xor 加解密可逆
    8. session.SessionStore 敏感字段落盘后**不含明文**
    9. session.SessionStore 老明文向后兼容读得回
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.licensing import rasp   # noqa: E402
from dub_align_studio.licensing import crypto_store  # noqa: E402
from dub_align_studio.licensing.session import SessionStore  # noqa: E402


# --------------------------------------------------------------------------
# RASP
# --------------------------------------------------------------------------


def test_1_rasp_report_suspicious():
    r = rasp.RaspReport()
    assert r.suspicious is False
    r.debugger_attached = True
    assert r.suspicious is True
    assert "debugger" in r.summary()

    r = rasp.RaspReport(frida_detected=True)
    assert r.suspicious is True
    assert "frida" in r.summary()

    r = rasp.RaspReport(integrity_ok=False)
    assert r.suspicious is True
    assert "tamper" in r.summary()

    r = rasp.RaspReport(integrity_ok=True)
    assert r.suspicious is False


def test_2_debugger_attached_no_throw():
    # 不管在哪个平台，都不能抛
    result = rasp.debugger_attached()
    assert isinstance(result, bool)


def test_3_suspect_processes_cache(monkeypatch):
    """process 扫描默认缓存；refresh_processes=True 会清缓存。"""
    calls = []
    def _fake_scan():
        calls.append(1)
        return ["fake-debugger"]
    monkeypatch.setattr(rasp, "suspect_processes", _fake_scan)
    rasp._process_scan_once.cache_clear()

    # 两次 full_scan（不 refresh）→ 只应扫一次
    rasp.full_scan()
    rasp.full_scan()
    assert len(calls) == 1

    # refresh=True 强制重扫
    rasp.full_scan(refresh_processes=True)
    assert len(calls) == 2


def test_4_integrity_no_baseline_returns_none(monkeypatch):
    monkeypatch.delenv("DUB_ALIGN_INTEGRITY_HASH", raising=False)
    # 让 side-file 也读不到
    monkeypatch.setattr(rasp, "_read_baseline_hash", lambda: "")
    ok, exp, act = rasp.integrity_check()
    assert ok is None
    assert exp == ""
    assert act == ""


def test_5_integrity_matches(monkeypatch, tmp_path):
    """伪造 baseline == 当前 sys.executable 的 hash → True。"""
    import hashlib
    exe = sys.executable
    with open(exe, "rb") as f:
        h = hashlib.sha256(f.read()).hexdigest()
    monkeypatch.setenv("DUB_ALIGN_INTEGRITY_HASH", h)
    ok, exp, act = rasp.integrity_check()
    assert ok is True
    assert exp == h and act == h


def test_5_integrity_mismatch(monkeypatch):
    monkeypatch.setenv("DUB_ALIGN_INTEGRITY_HASH", "0" * 64)
    ok, exp, act = rasp.integrity_check()
    assert ok is False


def test_6_xor_bytes_reversible():
    data = "配音对齐工作室".encode("utf-8")
    key = b"secret-key-16by"
    cipher = rasp.xor_bytes(data, key)
    assert cipher != data
    plain2 = rasp.xor_bytes(cipher, key)
    assert plain2 == data


def test_6_decrypt_str_roundtrip():
    key = b"x" * 16
    plain = "http://101.201.108.8:8001"
    cipher = rasp.xor_bytes(plain.encode("utf-8"), key).hex()
    got = rasp.decrypt_str(cipher, key)
    assert got == plain


def test_6_strict_mode_env(monkeypatch):
    monkeypatch.delenv("DUB_ALIGN_RASP_STRICT", raising=False)
    assert rasp.strict_mode_enabled() is False
    monkeypatch.setenv("DUB_ALIGN_RASP_STRICT", "1")
    assert rasp.strict_mode_enabled() is True
    monkeypatch.setenv("DUB_ALIGN_RASP_STRICT", "0")
    assert rasp.strict_mode_enabled() is False


# --------------------------------------------------------------------------
# crypto_store
# --------------------------------------------------------------------------


def test_7_xor_encrypt_decrypt_roundtrip():
    # xor 兜底路径在所有平台都能用
    plain = "SESSION-TOK-secret-2026-XXX"
    encoded = crypto_store._xor_encrypt(plain)
    assert encoded.startswith("xor:")
    got = crypto_store._xor_decrypt(encoded)
    assert got == plain


def test_7_encrypt_dispatches_to_available_backend():
    """在 Linux/macOS 上应走 xor 兜底；Windows 上应走 dpapi。"""
    plain = "hello"
    encoded = crypto_store.encrypt(plain)
    assert encoded  # 至少非空
    assert crypto_store.decrypt(encoded) == plain


def test_7_decrypt_legacy_plain_backward_compat():
    """老版本明文（无前缀）应能被 decrypt 识别原样返回。"""
    assert crypto_store.decrypt("legacy-plain-text") == "legacy-plain-text"
    assert crypto_store.decrypt("") == ""


def test_7_backend_name_string():
    import platform
    name = crypto_store.backend_name()
    if platform.system().lower().startswith("win"):
        assert name == "dpapi"
    else:
        assert name == "xor-fallback"


# --------------------------------------------------------------------------
# SessionStore 与加密的联动
# --------------------------------------------------------------------------


def test_8_session_sensitive_not_plain_on_disk(tmp_path):
    """写入后打开 license.json，敏感字段不应含原始明文。"""
    p = tmp_path / "license.json"
    s = SessionStore(path=p)
    secret_code = "TEST-CODE-SECRET-2026-ABCDEF"
    secret_token = "SESSION-TOKEN-DO-NOT-LEAK"
    s.update(activated=True, code=secret_code, session_token=secret_token,
             machine_id="hwid-x")
    assert p.exists()
    on_disk = p.read_text(encoding="utf-8")
    # 至少两个敏感字段的原文都不该出现在磁盘上
    assert secret_code not in on_disk, "code 明文泄露到磁盘"
    assert secret_token not in on_disk, "session_token 明文泄露到磁盘"


def test_9_session_reads_back_correctly(tmp_path):
    """写入 → 换新实例读回 → 拿到明文。"""
    p = tmp_path / "license.json"
    s1 = SessionStore(path=p)
    s1.update(activated=True, code="MY-CODE-XX", session_token="tok-abc",
              machine_id="hwid")
    s2 = SessionStore(path=p)
    st = s2.get()
    assert st.activated is True
    assert st.code == "MY-CODE-XX"
    assert st.session_token == "tok-abc"


def test_9_session_backward_compat_plain(tmp_path):
    """老版本明文 license.json 也应能读回（无前缀 = 明文）。"""
    p = tmp_path / "license.json"
    p.write_text(json.dumps({
        "schema": "dub_align_studio_license@v1",
        "activated": True,
        "code": "OLD-PLAIN-CODE",     # 老版本明文
        "session_token": "OLD-PLAIN-TOKEN",
        "machine_id": "hwid",
    }, ensure_ascii=False), encoding="utf-8")
    s = SessionStore(path=p)
    st = s.get()
    assert st.code == "OLD-PLAIN-CODE"
    assert st.session_token == "OLD-PLAIN-TOKEN"


# --------------------------------------------------------------------------
# Nonce / sig（防重放）
# --------------------------------------------------------------------------


def test_10_client_payload_includes_nonce_ts_sig():
    from dub_align_studio.licensing import LicensingClient, LicensingConfig
    c = LicensingClient(LicensingConfig(server="http://fake"))
    p1 = c._base_payload("CODE", "hwid")
    assert "nonce" in p1 and len(p1["nonce"]) == 32   # 16 bytes hex
    assert "ts" in p1 and isinstance(p1["ts"], int)
    assert "sig" in p1 and len(p1["sig"]) == 64        # sha256 hex
    # nonce 每次不同
    p2 = c._base_payload("CODE", "hwid")
    assert p1["nonce"] != p2["nonce"]


def test_10_sig_verifies_with_hmac_sha256():
    """服务端可用 HMAC-SHA256(code, nonce|ts|machine_id|app_id) 校验。"""
    import hmac
    import hashlib
    from dub_align_studio.licensing import LicensingClient, LicensingConfig
    c = LicensingClient(LicensingConfig(server="http://fake"))
    p = c._base_payload("SEC-CODE", "hwid-x")
    msg = f"{p['nonce']}|{p['ts']}|{p['machine_id']}|{p['app_id']}".encode("utf-8")
    expected = hmac.new(b"SEC-CODE", msg, hashlib.sha256).hexdigest()
    assert p["sig"] == expected
