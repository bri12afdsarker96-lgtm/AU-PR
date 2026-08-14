# -*- coding: utf-8 -*-
"""心跳守护线程 + action 处理器。

设计：
    * 一个 `HeartbeatDaemon` 全局单例
    * 后台线程按 `heartbeat_interval` 秒调 `client.heartbeat`
    * 心跳返回 action：
        - continue → 继续
        - expired / banned / force_update / session_invalid / device_invalid /
          app_invalid → 触发 `on_invalid(action, message)` 回调（gate 关门）
    * 网络暂时挂掉不立刻踢，允许 `soft_grace_seconds` 内网络恢复；超过才踢
    * 每一次心跳的结果都写回 SessionStore（last_action / server_time / expire_at）
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .client import (
    HeartbeatResult, LicensingClient, LicensingError, NetworkError,
)
from .session import SessionStore


# action == 这些视为"授权失效"
INVALID_ACTIONS = frozenset({
    "expired", "banned", "force_update",
    "session_invalid", "device_invalid", "app_invalid",
})


class HeartbeatDaemon:
    def __init__(self, client: LicensingClient, store: SessionStore, *,
                 device_name: str = "", system_version: str = "",
                 soft_grace_seconds: float = 300.0,
                 on_invalid: Callable[[str, str], None] | None = None,
                 on_ok: Callable[[HeartbeatResult], None] | None = None,
                 ) -> None:
        self.client = client
        self.store = store
        self.device_name = device_name
        self.system_version = system_version
        self.soft_grace_seconds = soft_grace_seconds
        self.on_invalid = on_invalid or (lambda _a, _m: None)
        self.on_ok = on_ok or (lambda _r: None)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # 当前任务信息（web_server 可 set 供心跳 body 上报）
        self._task_running = False
        self._task_id = ""
        self._task_name = ""

    def set_task(self, running: bool, task_id: str = "",
                  task_name: str = "") -> None:
        with self._lock:
            self._task_running = bool(running)
            self._task_id = task_id or ""
            self._task_name = task_name or ""

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="license-heartbeat", daemon=True,
            )
            self._thread.start()

    def stop(self, wait_seconds: float = 3.0) -> bool:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=wait_seconds)
        return not (t is not None and t.is_alive())

    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def _one_beat(self) -> None:
        st = self.store.get()
        if not st.session_token or not st.code:
            return   # 未激活/未 start 时不发心跳
        with self._lock:
            task_running = self._task_running
            task_id = self._task_id
            task_name = self._task_name
        try:
            result = self.client.heartbeat(
                code=st.code, machine_id=st.machine_id,
                session_token=st.session_token,
                device_name=self.device_name,
                system_version=self.system_version,
                task_running=task_running,
                current_task_id=task_id,
                current_task_name=task_name,
            )
        except NetworkError as exc:
            # 网络异常：宽限期内不踢；超过宽限期 → 视为 session_invalid 保护策略
            self.store.update(last_error=str(exc)[:200])
            now = time.time()
            last_ok = st.last_check_at or now
            if now - last_ok > self.soft_grace_seconds:
                self.store.update(
                    last_action="network_lost",
                    last_error=f"网络长时间不可达（>{int(self.soft_grace_seconds)}s）",
                )
                self.on_invalid(
                    "session_invalid",
                    f"无法连接授权服务器超过 "
                    f"{int(self.soft_grace_seconds)} 秒，暂停使用",
                )
            return
        except LicensingError as exc:
            # 服务器返回异常状态（协议/500）：记录但不立刻踢
            self.store.update(last_error=str(exc)[:200])
            return

        # OK：写回状态
        self.store.update(
            last_action=result.action,
            last_error="",
            expire_at=result.expire_at or st.expire_at,
            server_time=result.server_time or st.server_time,
        )
        if result.action in INVALID_ACTIONS:
            self.on_invalid(result.action, result.message)
        else:
            self.on_ok(result)

    def _loop(self) -> None:
        # 首次立即打一次；然后按 interval 循环
        while not self._stop.is_set():
            interval = 30
            try:
                self._one_beat()
                st = self.store.get()
                interval = max(5, int(st.heartbeat_interval or 30))
            except Exception:  # noqa: BLE001
                # 心跳内部若抛异常也不要杀线程
                pass
            self._stop.wait(interval)
