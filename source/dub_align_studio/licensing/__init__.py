# -*- coding: utf-8 -*-
"""授权（激活码）子系统 —— 与 CLIENT_API_V2 对接。

对外 API：
    LicenseManager   —— 单例；封装激活/启动/心跳/退出/状态查询
    LicensingError / NetworkError / ServerError / LicenseDenied
    APP_ID / APP_VERSION / DEFAULT_SERVER
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .client import (
    APP_ID, APP_VERSION, DEFAULT_SERVER,
    LicenseDenied, LicensingClient, LicensingConfig, LicensingError,
    NetworkError, ServerError,
    ActivateResult, HeartbeatResult, LogoutResult, StartResult,
)
from .heartbeat import HeartbeatDaemon, INVALID_ACTIONS
from .machine_id import device_name as _device_name
from .machine_id import machine_id as _machine_id
from .machine_id import system_version as _system_version
from .session import LicenseState, SessionStore


__all__ = [
    "LicenseManager", "LicenseState",
    "LicensingError", "NetworkError", "ServerError", "LicenseDenied",
    "APP_ID", "APP_VERSION", "DEFAULT_SERVER",
    "INVALID_ACTIONS",
]


class LicenseManager:
    """业务外观层：activate / start / heartbeat / logout / is_active 全在这。

    web_server 只需拿到一个实例，剩下的：
      * `activate(code)`：首次绑定；成功后自动 `start()`
      * `start_from_saved()`：软件启动时如果本地已有 code+机器一致，自动 start
      * `is_active()`：路由 gate 判断（True 才让请求进）
      * `on_state_change(callback)`：授权失效/恢复时的钩子
    """

    def __init__(self, *, config: LicensingConfig | None = None,
                 store: SessionStore | None = None) -> None:
        self.config = config or LicensingConfig()
        self.client = LicensingClient(self.config)
        self.store = store or SessionStore()
        self._device = _device_name()
        self._sysver = _system_version()
        self._callbacks: list[Callable[[LicenseState], None]] = []
        self._cb_lock = threading.Lock()

        self.daemon = HeartbeatDaemon(
            client=self.client, store=self.store,
            device_name=self._device, system_version=self._sysver,
            on_invalid=self._on_daemon_invalid,
            on_ok=self._on_daemon_ok,
        )

    # ---------------------------------------------------------------- 状态
    def state(self) -> LicenseState:
        return self.store.get()

    def is_active(self) -> bool:
        """当前是否有效授权（本地判定，快速）——供路由 gate 用。"""
        st = self.store.get()
        if not st.activated or not st.session_token:
            return False
        if st.last_action in INVALID_ACTIONS:
            return False
        return True

    def on_state_change(self,
                         callback: Callable[[LicenseState], None]) -> None:
        with self._cb_lock:
            self._callbacks.append(callback)

    def _notify(self) -> None:
        st = self.state()
        with self._cb_lock:
            cbs = list(self._callbacks)
        for cb in cbs:
            try:
                cb(st)
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- daemon 回调
    def _on_daemon_invalid(self, action: str, message: str) -> None:
        self.store.update(last_action=action, last_error=message)
        # 停心跳线程本身不阻塞：让 heartbeat_loop 下一轮自然退出
        self._notify()

    def _on_daemon_ok(self, result: HeartbeatResult) -> None:
        self._notify()

    # -------------------------------------------------------------- 操作
    def activate(self, code: str) -> ActivateResult:
        """输入激活码 → 激活 → 自动 start；返回 activate 的原始结果。"""
        code = (code or "").strip()
        if not code:
            raise LicensingError("激活码不能为空")
        mid = _machine_id()
        res = self.client.activate(
            code=code, machine_id=mid,
            device_name=self._device, system_version=self._sysver,
        )
        self.store.update(
            activated=True, code=code, machine_id=mid,
            expire_at=res.expire_at, activated_at=time.time(),
            server=self.config.server, last_error="", last_action="",
        )
        # 立刻 start 拿 session_token
        try:
            self.start_from_saved()
        except LicensingError as exc:
            # activate 成功但 start 失败 → 记录但不清激活；用户重试即可
            self.store.update(last_error=str(exc)[:200])
        self._notify()
        return res

    def start_from_saved(self) -> StartResult | None:
        """启动时用本地保存的 code + machine_id 发 start；成功后启守护线程。"""
        st = self.store.get()
        if not st.code or not st.activated:
            return None
        mid = _machine_id()
        if st.machine_id and st.machine_id != mid:
            # 换机 → 直接失败（激活码绑定的是旧 machine_id）
            raise LicenseDenied(
                "当前电脑与激活码绑定设备不匹配（HWID 变化）",
                http_status=403, detail="HWID mismatch",
            )
        res = self.client.start(
            code=st.code, machine_id=mid,
            device_name=self._device, system_version=self._sysver,
        )
        self.store.update(
            session_token=res.session_token,
            heartbeat_interval=res.heartbeat_interval,
            expire_at=res.expire_at, server_time=res.server_time,
            machine_id=mid, last_action="continue", last_error="",
        )
        self.daemon.start()
        self._notify()
        return res

    def logout(self) -> LogoutResult | None:
        """主动退出：通知服务端 + 停心跳 + 清 session_token（保留 code 便于下次 start）。"""
        st = self.store.get()
        if not st.session_token or not st.code:
            self.daemon.stop()
            return None
        try:
            res = self.client.logout(
                code=st.code, machine_id=st.machine_id,
                session_token=st.session_token,
                device_name=self._device, system_version=self._sysver,
            )
        except LicensingError:
            res = LogoutResult(success=False)
        self.daemon.stop()
        self.store.update(session_token="", last_action="")
        self._notify()
        return res

    def deactivate(self) -> None:
        """完全清除本地激活状态（回到未激活）。"""
        self.daemon.stop()
        try:
            self.logout()
        except Exception:  # noqa: BLE001
            pass
        self.store.clear()
        self._notify()

    def set_task(self, running: bool, task_id: str = "",
                  task_name: str = "") -> None:
        """web_server 层：任务开始/结束时调，让心跳带上 task_running。"""
        self.daemon.set_task(running, task_id, task_name)


# ----------------------------------------------------------------- 单例
_MANAGER: LicenseManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> LicenseManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = LicenseManager()
        return _MANAGER


def reset_manager_for_tests(mgr: LicenseManager | None) -> None:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            try:
                _MANAGER.daemon.stop(1.0)
            except Exception:  # noqa: BLE001
                pass
        _MANAGER = mgr
