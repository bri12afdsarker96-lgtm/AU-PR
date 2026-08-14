# -*- coding: utf-8 -*-
"""授权状态持久化（本地文件）。

存储位置：`~/.dub_align_studio/license.json`（settings 同级）。
包含：code、session_token、machine_id、expire_at、last_server_time、
      heartbeat_interval、activated_at、last_action、last_check_at。

**敏感字段加密**（`code` / `session_token`）：
    * Windows → DPAPI（CryptProtectData，绑用户账户）
    * 其他 → xor 弱加密兜底（挡文本 grep，非专业防护）
    * 详见 `crypto_store.py`。
    * **前向兼容**：老版本明文写入的字段读时自动识别（无前缀即视为明文）。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import crypto_store


def default_session_file() -> Path:
    """`~/.dub_align_studio/license.json`。"""
    d = Path.home() / ".dub_align_studio"
    d.mkdir(parents=True, exist_ok=True)
    return d / "license.json"


@dataclass
class LicenseState:
    activated: bool = False           # 本地是否已通过至少一次 activate
    code: str = ""                    # 激活码（明文）
    machine_id: str = ""              # 首次激活时的 machine_id（校验一致）
    session_token: str = ""           # 最近一次 start 返回
    heartbeat_interval: int = 30
    expire_at: str = ""               # UTC 到期时间字符串
    server_time: str = ""             # 最近一次 server_time
    activated_at: float = 0.0         # 本地 unix ts
    last_check_at: float = 0.0        # 最近一次 heartbeat/start unix ts
    last_action: str = ""             # 最近一次 heartbeat 的 action
    last_error: str = ""              # 最近一次错误摘要（供 UI 展示）
    server: str = ""                  # 最近一次成功握手的服务器地址
    schema: str = "dub_align_studio_license@v1"

    def to_dict(self) -> dict:
        return asdict(self)


class SessionStore:
    """线程安全的 LicenseState 内存 + 落盘。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_session_file()
        self._lock = threading.Lock()
        self._state = LicenseState()
        self._load()

    # 敏感字段——落盘前加密；读回时解密
    _SENSITIVE_FIELDS = ("code", "session_token")

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return
        if not isinstance(data, dict):
            return
        if data.get("schema") != LicenseState.schema:
            return
        # 敏感字段解密
        for f in self._SENSITIVE_FIELDS:
            v = data.get(f)
            if isinstance(v, str) and v:
                data[f] = crypto_store.decrypt(v)
        allowed = set(LicenseState.__annotations__.keys())
        clean = {k: v for k, v in data.items() if k in allowed}
        try:
            self._state = LicenseState(**clean)
        except TypeError:
            self._state = LicenseState()

    def _save_locked(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = self._state.to_dict()
            # 敏感字段加密（保持 schema 不变；只是字符串内容被替换成密文）
            for f in self._SENSITIVE_FIELDS:
                v = payload.get(f)
                if isinstance(v, str) and v:
                    payload[f] = crypto_store.encrypt(v)
            tmp = self.path.parent / f"{self.path.name}.tmp"
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(str(tmp), str(self.path))
        except Exception:  # noqa: BLE001
            pass

    def get(self) -> LicenseState:
        with self._lock:
            # 返回浅拷贝，避免外部修改污染内部状态
            return LicenseState(**self._state.to_dict())

    def update(self, **changes) -> LicenseState:
        with self._lock:
            allowed = set(LicenseState.__annotations__.keys())
            for k, v in changes.items():
                if k in allowed:
                    setattr(self._state, k, v)
            self._state.last_check_at = time.time()
            self._save_locked()
            return LicenseState(**self._state.to_dict())

    def clear(self) -> None:
        """完全清除本地激活状态（激活码 + token + 到期都清）。"""
        with self._lock:
            self._state = LicenseState()
            try:
                if self.path.exists():
                    self.path.unlink()
            except OSError:
                pass
