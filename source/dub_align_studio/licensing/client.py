# -*- coding: utf-8 -*-
"""授权服务端 HTTP 客户端（CLIENT_API_V2）。

服务器地址：默认 http://101.201.108.8:8001/（可在 settings.json 覆盖）。
接口路径：
    POST /api/client/activate
    POST /api/client/start
    POST /api/client/heartbeat
    POST /api/client/logout

所有请求都带 app_id / machine_id / device_name / system_version / app_version；
请求包裹 JSON；所有异常都封装为 `LicensingError` 便于上层统一处理。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


DEFAULT_SERVER = "http://101.201.108.8:8001"
DEFAULT_TIMEOUT = 15.0

# 与 CLIENT_API_V2 对齐——所有请求都会带上这些字段
# 软件唯一标识；服务器会校验正式码是否属于本 app_id
APP_ID = "dub_align_studio"
APP_VERSION = "1.0.0"

# 常见 CF/边缘会拦 Python-urllib 默认 UA（见 edge_tts 的 R14-FIX-4c）；
# 加浏览器 UA 是防御性动作。
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class LicensingError(Exception):
    """所有 licensing 相关错误的基类。"""

    def __init__(self, message: str, *, http_status: int = 0,
                 detail: str = "", raw: dict | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.detail = detail
        self.raw = raw or {}


class NetworkError(LicensingError):
    """网络层无法连通服务器（DNS/连接/超时）。"""


class ServerError(LicensingError):
    """服务器返回 5xx / 格式异常。"""


class LicenseDenied(LicensingError):
    """403/404 —— 激活码非法、被封、到期、不属于本软件等。"""


@dataclass
class LicensingConfig:
    server: str = DEFAULT_SERVER
    timeout: float = DEFAULT_TIMEOUT
    app_id: str = APP_ID
    app_version: str = APP_VERSION


@dataclass
class ActivateResult:
    success: bool
    message: str = ""
    expire_at: str = ""  # UTC "YYYY-MM-DD HH:MM:SS"
    raw: dict = field(default_factory=dict)


@dataclass
class StartResult:
    success: bool
    message: str = ""
    session_token: str = ""
    expire_at: str = ""
    server_time: str = ""
    heartbeat_interval: int = 30
    raw: dict = field(default_factory=dict)


@dataclass
class HeartbeatResult:
    success: bool
    action: str = "continue"   # continue/expired/banned/force_update/session_invalid/device_invalid/app_invalid
    message: str = ""
    expire_at: str = ""
    server_time: str = ""
    raw: dict = field(default_factory=dict)


@dataclass
class LogoutResult:
    success: bool
    message: str = ""
    raw: dict = field(default_factory=dict)


class LicensingClient:
    """封装 4 个 HTTP 端点；只做协议层，不管持久化 / heartbeat 调度。"""

    def __init__(self, config: LicensingConfig | None = None) -> None:
        self.config = config or LicensingConfig()

    # -------------------------------------------------------------- helpers
    def _post(self, path: str, payload: dict) -> tuple[int, dict]:
        url = self.config.server.rstrip("/") + path
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": _BROWSER_UA,
        }
        req = urllib.request.Request(url, data=body, headers=headers,
                                      method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout) as r:
                data = r.read()
                status = int(r.status or 200)
        except urllib.error.HTTPError as exc:
            data = b""
            try:
                data = exc.read() or b""
            except Exception:  # noqa: BLE001
                pass
            status = int(exc.code or 500)
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise NetworkError(
                f"无法连接授权服务器（{self.config.server}）：{exc}"
            ) from exc

        raw: dict = {}
        if data:
            try:
                raw = json.loads(data.decode("utf-8", "replace"))
                if not isinstance(raw, dict):
                    raw = {"_raw": raw}
            except json.JSONDecodeError:
                raw = {"_raw_text": data.decode("utf-8", "replace")[:500]}
        return status, raw

    def _base_payload(self, code: str, machine_id: str, *,
                       device_name: str = "", system_version: str = "",
                       app_id: str | None = None,
                       app_version: str | None = None,
                       extras: dict | None = None) -> dict:
        """基础字段。新版扩展：
        * `nonce` —— 16 字节随机十六进制串（防重放；服务端拒重复 nonce）
        * `ts` —— 客户端 unix ts（服务端可用于窗口限时校验）
        * `sig` —— HMAC-SHA256(code, f"{nonce}|{ts}|{machine_id}|{app_id}")
                    简单会话签名（服务端拿 code 也能验；不用非对称是为了
                    协议轻量。若服务端将来给客户端下发私钥/公钥体系，可换）
        服务端当前 CLIENT_API_V2 不校验也不用；服务端升级后可开始校验。
        """
        base = {
            "code": code,
            "machine_id": machine_id,
            "app_id": app_id or self.config.app_id,
            "device_name": device_name or "",
            "system_version": system_version or "",
            "app_version": app_version or self.config.app_version,
        }
        # 防重放三件套（服务端**忽略**这三个字段是完全 OK 的，向后兼容）
        nonce = secrets.token_hex(16)
        ts = int(time.time())
        msg = f"{nonce}|{ts}|{machine_id}|{base['app_id']}".encode("utf-8")
        sig = hmac.new(code.encode("utf-8"), msg, hashlib.sha256).hexdigest()
        base["nonce"] = nonce
        base["ts"] = ts
        base["sig"] = sig
        if extras:
            base.update(extras)
        return base

    @staticmethod
    def _err_from_response(status: int, raw: dict, default_msg: str) -> LicensingError:
        detail = str(raw.get("detail") or raw.get("message") or default_msg)
        if 500 <= status < 600:
            return ServerError(
                f"服务器错误（HTTP {status}）：{detail}",
                http_status=status, detail=detail, raw=raw,
            )
        if status in (401, 403, 404, 409):
            return LicenseDenied(
                detail, http_status=status, detail=detail, raw=raw,
            )
        return LicensingError(
            f"HTTP {status}：{detail}", http_status=status,
            detail=detail, raw=raw,
        )

    # -------------------------------------------------------------- endpoints
    def activate(self, *, code: str, machine_id: str,
                  device_name: str = "", system_version: str = "",
                  app_version: str | None = None) -> ActivateResult:
        payload = self._base_payload(
            code, machine_id, device_name=device_name,
            system_version=system_version, app_version=app_version,
        )
        status, raw = self._post("/api/client/activate", payload)
        if status == 200 and raw.get("success"):
            return ActivateResult(
                success=True,
                message=str(raw.get("message") or "激活成功"),
                expire_at=str(raw.get("expire_at") or ""),
                raw=raw,
            )
        raise self._err_from_response(status, raw, "激活失败")

    def start(self, *, code: str, machine_id: str,
              device_name: str = "", system_version: str = "",
              app_version: str | None = None) -> StartResult:
        payload = self._base_payload(
            code, machine_id, device_name=device_name,
            system_version=system_version, app_version=app_version,
        )
        status, raw = self._post("/api/client/start", payload)
        if status == 200 and raw.get("success"):
            try:
                hb = int(raw.get("heartbeat_interval") or 30)
            except (TypeError, ValueError):
                hb = 30
            return StartResult(
                success=True,
                message=str(raw.get("message") or "授权验证成功"),
                session_token=str(raw.get("session_token") or ""),
                expire_at=str(raw.get("expire_at") or ""),
                server_time=str(raw.get("server_time") or ""),
                heartbeat_interval=max(5, hb),
                raw=raw,
            )
        raise self._err_from_response(status, raw, "启动验证失败")

    def heartbeat(self, *, code: str, machine_id: str, session_token: str,
                   device_name: str = "", system_version: str = "",
                   app_version: str | None = None,
                   task_running: bool = False,
                   current_task_id: str = "",
                   current_task_name: str = "") -> HeartbeatResult:
        """心跳失败按契约返回 200 + action != continue；网络异常抛 NetworkError。
        对上层：任何非 continue 都应视为授权停止。"""
        payload = self._base_payload(
            code, machine_id, device_name=device_name,
            system_version=system_version, app_version=app_version,
        )
        payload.update({
            "session_token": session_token,
            "task_running": bool(task_running),
            "current_task_id": current_task_id or "",
            "current_task_name": current_task_name or "",
        })
        status, raw = self._post("/api/client/heartbeat", payload)
        if status == 200 and (raw.get("success") or raw.get("action")):
            return HeartbeatResult(
                success=bool(raw.get("success", True)),
                action=str(raw.get("action") or "continue"),
                message=str(raw.get("message") or ""),
                expire_at=str(raw.get("expire_at") or ""),
                server_time=str(raw.get("server_time") or ""),
                raw=raw,
            )
        # 非 200 或结构异常也视为异常
        raise self._err_from_response(status, raw, "心跳失败")

    def logout(self, *, code: str, machine_id: str, session_token: str,
                device_name: str = "", system_version: str = "",
                app_version: str | None = None) -> LogoutResult:
        payload = self._base_payload(
            code, machine_id, device_name=device_name,
            system_version=system_version, app_version=app_version,
        )
        payload.update({
            "session_token": session_token,
            "task_running": False,
            "current_task_id": "",
            "current_task_name": "",
        })
        status, raw = self._post("/api/client/logout", payload)
        return LogoutResult(
            success=(status == 200 and bool(raw.get("success", False))),
            message=str(raw.get("message") or ""),
            raw=raw,
        )
