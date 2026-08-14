# -*- coding: utf-8 -*-
"""Licensing 子系统单元测试。

不打真实授权服务器 —— mock LicensingClient 的 4 个端点。
覆盖：
    1. LicenseState 空/loaded 状态
    2. SessionStore load/save/clear
    3. LicenseManager.activate 成功 → 自动 start
    4. activate 失败（LicenseDenied）
    5. start 网络失败 → 保留激活状态
    6. is_active gate 判定
    7. deactivate 清所有本地
    8. heartbeat action=expired → on_state_change 收到通知；is_active=False
    9. web_server 路由：/api/license/status（activated=False 时的字段）
   10. web_server gate：默认 disabled → 不拦；启用 env 后拦截 /api/state
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.licensing import (  # noqa: E402
    ActivateResult, HeartbeatResult, LicenseDenied, LicenseManager,
    LicenseState, LicensingClient, LicensingConfig, LicensingError,
    LogoutResult, NetworkError, ServerError, StartResult, SessionStore,
    reset_manager_for_tests,
)
from dub_align_studio.licensing.session import default_session_file  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """把 license.json 路径重定向到 tmp。"""
    session_path = tmp_path / "license.json"
    return SessionStore(path=session_path)


@pytest.fixture
def mgr(tmp_store):
    """一个 LicenseManager，用 mock client。"""
    m = LicenseManager(config=LicensingConfig(server="http://fake.local"),
                        store=tmp_store)
    yield m
    try:
        m.daemon.stop(1.0)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 1. LicenseState + SessionStore
# --------------------------------------------------------------------------


def test_1_license_state_default_empty():
    st = LicenseState()
    assert st.activated is False
    assert st.code == ""
    assert st.session_token == ""
    assert st.heartbeat_interval == 30


def test_2_session_store_persistence(tmp_path):
    p = tmp_path / "lic.json"
    s1 = SessionStore(path=p)
    s1.update(activated=True, code="TEST-CODE", machine_id="hwid123",
              session_token="tok", expire_at="2027-01-01 00:00:00")
    assert p.exists()
    # 新实例应能读回
    s2 = SessionStore(path=p)
    st = s2.get()
    assert st.activated is True
    assert st.code == "TEST-CODE"
    assert st.session_token == "tok"


def test_2_session_store_clear(tmp_path):
    p = tmp_path / "lic.json"
    s = SessionStore(path=p)
    s.update(activated=True, code="X", session_token="y")
    s.clear()
    assert not p.exists()
    assert s.get().activated is False
    assert s.get().code == ""


def test_2_session_store_corrupted_json_recovers(tmp_path):
    p = tmp_path / "lic.json"
    p.write_text("this is not json", encoding="utf-8")
    s = SessionStore(path=p)
    assert s.get().activated is False


# --------------------------------------------------------------------------
# 3-5. LicenseManager.activate / start
# --------------------------------------------------------------------------


def test_3_activate_success_triggers_start(mgr, monkeypatch):
    """activate 成功 → 自动调 start → session_token 落库 + daemon 启动。"""
    monkeypatch.setattr(mgr.client, "activate",
                         lambda **kw: ActivateResult(success=True,
                                                       message="OK",
                                                       expire_at="2027-01-01 00:00:00"))
    monkeypatch.setattr(mgr.client, "start",
                         lambda **kw: StartResult(success=True,
                                                    message="OK",
                                                    session_token="tok-abc",
                                                    expire_at="2027-01-01 00:00:00",
                                                    server_time="2026-08-14 12:00:00",
                                                    heartbeat_interval=30))
    # 阻止真实心跳
    monkeypatch.setattr(mgr.daemon, "start", lambda: None)
    r = mgr.activate("MY-CODE")
    assert r.success is True
    st = mgr.state()
    assert st.activated is True
    assert st.code == "MY-CODE"
    assert st.session_token == "tok-abc"
    assert mgr.is_active() is True


def test_4_activate_denied_raises(mgr, monkeypatch):
    def _boom(**kw):
        raise LicenseDenied("激活码已被封禁", http_status=403)

    monkeypatch.setattr(mgr.client, "activate", _boom)
    with pytest.raises(LicenseDenied, match="封禁"):
        mgr.activate("BAD-CODE")
    # 本地不应留下激活状态
    assert mgr.state().activated is False
    assert mgr.is_active() is False


def test_5_start_network_error_keeps_activated(mgr, monkeypatch):
    """activate 成功但 start 因网络失败 → 保留 activated=True，last_error 记录。"""
    monkeypatch.setattr(mgr.client, "activate",
                         lambda **kw: ActivateResult(success=True, message="OK",
                                                       expire_at="2027-01-01 00:00:00"))
    def _net_fail(**kw):
        raise NetworkError("connect refused")
    monkeypatch.setattr(mgr.client, "start", _net_fail)
    monkeypatch.setattr(mgr.daemon, "start", lambda: None)
    mgr.activate("MY-CODE")
    st = mgr.state()
    assert st.activated is True
    assert st.code == "MY-CODE"
    assert st.session_token == ""      # start 没成功
    assert "connect refused" in st.last_error


# --------------------------------------------------------------------------
# 6-7. is_active + deactivate
# --------------------------------------------------------------------------


def test_6_is_active_requires_activated_and_token(mgr, monkeypatch):
    assert mgr.is_active() is False   # 全空
    mgr.store.update(activated=True, code="X")
    assert mgr.is_active() is False   # 无 session_token
    mgr.store.update(session_token="tok")
    assert mgr.is_active() is True
    mgr.store.update(last_action="expired")
    assert mgr.is_active() is False   # action=expired


def test_7_deactivate_clears_everything(mgr, monkeypatch):
    mgr.store.update(activated=True, code="X", session_token="tok",
                      machine_id="hwid")
    monkeypatch.setattr(mgr.client, "logout",
                         lambda **kw: LogoutResult(success=True))
    mgr.deactivate()
    st = mgr.state()
    assert st.activated is False
    assert st.code == ""
    assert st.session_token == ""
    assert mgr.is_active() is False


# --------------------------------------------------------------------------
# 8. heartbeat action=expired → gate closes
# --------------------------------------------------------------------------


def test_8_heartbeat_expired_marks_inactive(mgr, monkeypatch):
    mgr.store.update(activated=True, code="X", machine_id="hwid",
                      session_token="tok", heartbeat_interval=5)
    monkeypatch.setattr(mgr.client, "heartbeat",
                         lambda **kw: HeartbeatResult(success=True,
                                                        action="expired",
                                                        message="授权已到期"))
    # 手工触发一次心跳
    mgr.daemon._one_beat()
    st = mgr.state()
    assert st.last_action == "expired"
    assert mgr.is_active() is False


def test_8_heartbeat_continue_stays_active(mgr, monkeypatch):
    mgr.store.update(activated=True, code="X", machine_id="hwid",
                      session_token="tok")
    monkeypatch.setattr(mgr.client, "heartbeat",
                         lambda **kw: HeartbeatResult(success=True,
                                                        action="continue"))
    mgr.daemon._one_beat()
    assert mgr.is_active() is True
    assert mgr.state().last_action == "continue"


def test_8_state_change_callback_fires(mgr, monkeypatch):
    mgr.store.update(activated=True, code="X", machine_id="hwid",
                      session_token="tok")
    seen: list = []
    mgr.on_state_change(lambda s: seen.append(s.last_action))
    monkeypatch.setattr(mgr.client, "heartbeat",
                         lambda **kw: HeartbeatResult(success=True,
                                                        action="banned"))
    mgr.daemon._one_beat()
    assert "banned" in seen


# --------------------------------------------------------------------------
# 9-10. web_server /api/license/* + gate
# --------------------------------------------------------------------------


def test_9_web_license_gate_default_disabled(monkeypatch):
    """默认不设 env → gate 未启用 → /api/state 不拦。"""
    monkeypatch.delenv("DUB_ALIGN_LICENSE_REQUIRED", raising=False)
    monkeypatch.delenv("DUB_ALIGN_LICENSE_DISABLE", raising=False)
    from dub_align_studio.web_server import (
        _license_gate_enabled, _license_should_block,
    )
    assert _license_gate_enabled() is False
    assert _license_should_block("/api/state") is False


def test_10_web_license_gate_enabled_blocks(monkeypatch, tmp_path):
    """DUB_ALIGN_LICENSE_REQUIRED=1 + 未激活 → gate 拦 /api/state。"""
    monkeypatch.setenv("DUB_ALIGN_LICENSE_REQUIRED", "1")
    # 重置 manager，让它用空 store
    from dub_align_studio import licensing as lp
    empty_store = SessionStore(path=tmp_path / "empty_lic.json")
    empty_mgr = LicenseManager(store=empty_store)
    reset_manager_for_tests(empty_mgr)
    try:
        from dub_align_studio.web_server import (
            _license_gate_enabled, _license_should_block,
        )
        assert _license_gate_enabled() is True
        # 主 API 应被拦
        assert _license_should_block("/api/state") is True
        assert _license_should_block("/api/bulk_dub/start") is True
        # 白名单不拦
        assert _license_should_block("/api/license/status") is False
        assert _license_should_block("/api/license/activate") is False
        assert _license_should_block("/") is False
        assert _license_should_block("/index.html") is False
    finally:
        reset_manager_for_tests(None)


def test_10_web_license_gate_disable_env_beats_required(monkeypatch, tmp_path):
    """DISABLE 环境变量优先级高于 REQUIRED，方便调试。"""
    monkeypatch.setenv("DUB_ALIGN_LICENSE_REQUIRED", "1")
    monkeypatch.setenv("DUB_ALIGN_LICENSE_DISABLE", "1")
    from dub_align_studio.web_server import _license_gate_enabled
    assert _license_gate_enabled() is False


# --------------------------------------------------------------------------
# 11. Client HTTP 层：mock urllib
# --------------------------------------------------------------------------


def test_11_client_activate_403_denied():
    """服务器 403 + detail → 抛 LicenseDenied。"""
    c = LicensingClient(LicensingConfig(server="http://fake"))
    detail_bytes = '{"detail":"授权已到期"}'.encode("utf-8")

    import io as _io
    import urllib.error as _ue

    def _raise(*args, **kwargs):
        raise _ue.HTTPError(
            "http://fake", 403, "Forbidden", {},
            _io.BytesIO(detail_bytes),
        )

    with mock.patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(LicenseDenied) as ei:
            c.activate(code="X", machine_id="hwid")
        assert ei.value.http_status == 403
        assert "到期" in str(ei.value)


def test_11_client_activate_network_error():
    c = LicensingClient(LicensingConfig(server="http://fake"))
    import urllib.error
    def _raise(*args, **kwargs):
        raise urllib.error.URLError("connection refused")
    with mock.patch("urllib.request.urlopen", side_effect=_raise):
        with pytest.raises(NetworkError):
            c.activate(code="X", machine_id="hwid")


def test_11_client_heartbeat_action_expired():
    c = LicensingClient(LicensingConfig(server="http://fake"))
    fake_resp = mock.MagicMock()
    fake_resp.status = 200
    fake_resp.read.return_value = (
        b'{"success":true,"action":"expired","message":"expired",'
        b'"expire_at":"2020-01-01 00:00:00"}'
    )
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False
    with mock.patch("urllib.request.urlopen", return_value=fake_resp):
        r = c.heartbeat(code="X", machine_id="hwid", session_token="tok")
    assert r.action == "expired"
    assert r.success is True
