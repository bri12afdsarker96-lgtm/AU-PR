from __future__ import annotations

import ctypes
import getpass
import hashlib
import hmac
import json
import os
import platform
import socket
from .proc import check_output_silent
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, ttk
import tkinter as tk

from .product_identity import software_app_id


APP_NAME = "水星剪辑"
APP_VERSION = "v2026.07.09.1"
FAILURE_LIMIT = 5
LOCK_MINUTES = 30
REQUEST_TIMEOUT_SECONDS = 8
HEARTBEAT_FAIL_LIMIT = 3
CLIENT_USER_AGENT = f"ShuiXingJianJi/{APP_VERSION}"
ATTEMPT_POLICY_VERSION = 2

_AUTH_ENDPOINT_BYTES = [50, 46, 46, 42, 96, 117, 117, 107, 106, 107, 116, 104, 106, 107, 116, 107, 106, 98, 116, 98, 96, 98, 106, 106, 107, 117]
_AUTH_ENDPOINT_KEY = 0x5A
_CACHE_NAME = "license.bin"
_ATTEMPT_NAME = "activation_attempts.bin"
_HASH_SUFFIX = ".sha256"
_MACHINE_PROTECT_MAGIC = b"MCHN2"
_MACHINE_PROTECT_SALT = b"shuixing-license-cache-machine-v1"
_MACHINE_PROTECT_ITERATIONS = 240_000
_MACHINE_PROTECT_NONCE_SIZE = 16
_MACHINE_PROTECT_TAG_SIZE = 32


class SecurityViolation(RuntimeError):
    pass


class LicenseError(RuntimeError):
    pass


class LicenseNetworkError(LicenseError):
    pass


@dataclass
class LicenseSession:
    code: str
    machine_id: str
    app_id: str
    session_token: str
    heartbeat_interval: int
    expire_at: str = ""
    _stop: threading.Event = field(default_factory=threading.Event)

    def start_heartbeat(self, root: tk.Tk, on_invalid) -> None:
        self._stop = threading.Event()

        def worker() -> None:
            failures = 0
            interval = max(10, int(self.heartbeat_interval or 30))
            while not self._stop.wait(interval):
                # 运行期周期性复检：反调试与完整性从"仅启动一次"升级为持续校验，
                # 单点静态 patch 启动检查不足以绕过。
                try:
                    runtime_security_recheck()
                except SecurityViolation as exc:
                    root.after(0, lambda msg=str(exc): on_invalid(msg))
                    return
                try:
                    response = LicenseClient().heartbeat(self)
                    action = str(response.get("action") or "continue")
                    if action != "continue":
                        message = str(response.get("message") or f"授权状态异常：{action}")
                        root.after(0, lambda msg=message: on_invalid(msg))
                        return
                    failures = 0
                except Exception as exc:
                    failures += 1
                    if failures >= HEARTBEAT_FAIL_LIMIT:
                        root.after(0, lambda msg=str(exc): on_invalid(f"授权心跳失败：{msg}"))
                        return

        threading.Thread(target=worker, name="license-heartbeat", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def _decode_endpoint() -> str:
    return "".join(chr(value ^ _AUTH_ENDPOINT_KEY) for value in _AUTH_ENDPOINT_BYTES)


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    target = Path(base) / APP_NAME / "security"
    target.mkdir(parents=True, exist_ok=True)
    return target


def current_executable() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(__file__).resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def app_sha256() -> str:
    return software_app_id()


def verify_exe_integrity() -> None:
    exe = current_executable()
    expected_path = exe.with_suffix(exe.suffix + _HASH_SUFFIX)
    if not getattr(sys, "frozen", False) or not expected_path.exists():
        return
    expected = expected_path.read_text(encoding="utf-8").strip().lower().split()[0]
    actual = sha256_file(exe).lower()
    if expected != actual:
        raise SecurityViolation("程序完整性校验失败，请重新安装官方版本。")


def runtime_security_recheck() -> None:
    """运行期周期性复检：反调试 + 完整性。

    与启动检查复用同一实现，但由心跳线程按周期重复执行，使攻击者仅在启动处
    静态 patch 不足以持续绕过。两个子检查在非打包环境均直接返回，开发无副作用。
    """
    anti_debug_check()
    verify_exe_integrity()


def machine_id() -> str:
    parts = [platform.node(), getpass.getuser(), str(uuid.getnode())]
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
            parts.append(str(value))
    except Exception:
        pass
    try:
        parts.append(socket.gethostname())
    except Exception:
        pass
    raw = "|".join(part for part in parts if part)
    return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest().upper()


def anti_debug_check() -> None:
    if not getattr(sys, "frozen", False):
        return
    if sys.gettrace() is not None:
        raise SecurityViolation("检测到调试环境，程序已停止。")
    try:
        if bool(ctypes.windll.kernel32.IsDebuggerPresent()):
            raise SecurityViolation("检测到调试器，程序已停止。")
        is_debugger = ctypes.c_int(0)
        ctypes.windll.kernel32.CheckRemoteDebuggerPresent(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(is_debugger),
        )
        if is_debugger.value:
            raise SecurityViolation("检测到远程调试器，程序已停止。")
    except AttributeError:
        pass
    start = time.perf_counter()
    time.sleep(0.15)
    if time.perf_counter() - start > 2.0:
        raise SecurityViolation("运行环境异常，程序已停止。")
    suspicious = {
        "x64dbg.exe",
        "x32dbg.exe",
        "ida.exe",
        "ida64.exe",
        "idaq.exe",
        "idaq64.exe",
        "ghidra.exe",
        "dnspy.exe",
        "dnspy-x86.exe",
        "ilspy.exe",
        "ollydbg.exe",
        "processhacker.exe",
        "scylla.exe",
        "scylla_x64.exe",
        "scylla_x86.exe",
    }
    try:
        output = check_output_silent(["tasklist", "/fo", "csv", "/nh"], errors="ignore", timeout=3)
        lower = output.lower()
        if any(name in lower for name in suspicious):
            raise SecurityViolation("检测到逆向分析工具，程序已停止。")
    except SecurityViolation:
        raise
    except Exception:
        pass


def _entropy() -> bytes:
    value = f"{APP_NAME}|{machine_id()}|license-cache-v2"
    return hashlib.sha256(value.encode("utf-8")).digest()


def _xor_bytes(data: bytes) -> bytes:
    key = hashlib.sha256(_entropy() + b"fallback").digest()
    return bytes(byte ^ key[index % len(key)] for index, byte in enumerate(data))


def _machine_bound_keys() -> tuple[bytes, bytes]:
    material = hashlib.pbkdf2_hmac(
        "sha256",
        machine_id().encode("utf-8"),
        _MACHINE_PROTECT_SALT,
        _MACHINE_PROTECT_ITERATIONS,
        dklen=64,
    )
    return material[:32], material[32:]


def _hmac_stream(key: bytes, nonce: bytes, size: int) -> bytes:
    chunks = []
    counter = 0
    while sum(len(chunk) for chunk in chunks) < size:
        block = nonce + counter.to_bytes(8, "big")
        chunks.append(hmac.new(key, block, hashlib.sha256).digest())
        counter += 1
    return b"".join(chunks)[:size]


def _machine_bound_protect(data: bytes) -> bytes:
    enc_key, mac_key = _machine_bound_keys()
    nonce = os.urandom(_MACHINE_PROTECT_NONCE_SIZE)
    stream = _hmac_stream(enc_key, nonce, len(data))
    ciphertext = bytes(byte ^ stream[index] for index, byte in enumerate(data))
    body = _MACHINE_PROTECT_MAGIC + nonce + ciphertext
    tag = hmac.new(mac_key, body, hashlib.sha256).digest()
    return body + tag


def _machine_bound_unprotect(data: bytes) -> bytes:
    payload = data[len(_MACHINE_PROTECT_MAGIC) :]
    minimum_size = _MACHINE_PROTECT_NONCE_SIZE + _MACHINE_PROTECT_TAG_SIZE
    if len(payload) < minimum_size:
        raise LicenseError("本地授权缓存格式无效。")
    nonce = payload[:_MACHINE_PROTECT_NONCE_SIZE]
    ciphertext = payload[_MACHINE_PROTECT_NONCE_SIZE:-_MACHINE_PROTECT_TAG_SIZE]
    tag = payload[-_MACHINE_PROTECT_TAG_SIZE:]
    enc_key, mac_key = _machine_bound_keys()
    body = data[:-_MACHINE_PROTECT_TAG_SIZE]
    expected = hmac.new(mac_key, body, hashlib.sha256).digest()
    if not hmac.compare_digest(tag, expected):
        raise LicenseError("本地授权缓存无法解密，请重新激活。")
    stream = _hmac_stream(enc_key, nonce, len(ciphertext))
    return bytes(byte ^ stream[index] for index, byte in enumerate(ciphertext))


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        return _machine_bound_protect(data)
    try:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]

        data_buffer = ctypes.create_string_buffer(data)
        entropy_bytes = _entropy()
        entropy_buffer = ctypes.create_string_buffer(entropy_bytes)
        blob_in = DATA_BLOB(len(data), ctypes.cast(data_buffer, ctypes.POINTER(ctypes.c_char)))
        blob_entropy = DATA_BLOB(len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(blob_in), None, ctypes.byref(blob_entropy), None, None, 0, ctypes.byref(blob_out))
        if not ok:
            raise OSError("CryptProtectData failed")
        try:
            return b"DPAPI2" + ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return _machine_bound_protect(data)


def _dpapi_unprotect(data: bytes) -> bytes:
    if data.startswith(b"XOR2"):
        return _xor_bytes(data[4:])
    if data.startswith(_MACHINE_PROTECT_MAGIC):
        return _machine_bound_unprotect(data)
    if not data.startswith(b"DPAPI2"):
        raise LicenseError("本地授权缓存格式无效。")
    payload = data[6:]
    if os.name != "nt":
        raise LicenseError("当前系统不支持读取此授权缓存。")
    try:
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]

        payload_buffer = ctypes.create_string_buffer(payload)
        entropy_bytes = _entropy()
        entropy_buffer = ctypes.create_string_buffer(entropy_bytes)
        blob_in = DATA_BLOB(len(payload), ctypes.cast(payload_buffer, ctypes.POINTER(ctypes.c_char)))
        blob_entropy = DATA_BLOB(len(entropy_bytes), ctypes.cast(entropy_buffer, ctypes.POINTER(ctypes.c_char)))
        blob_out = DATA_BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, ctypes.byref(blob_entropy), None, None, 0, ctypes.byref(blob_out))
        if not ok:
            raise OSError("CryptUnprotectData failed")
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception as exc:
        raise LicenseError("本地授权缓存无法解密，请重新激活。") from exc


def _read_protected_json(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = path.read_bytes()
    text = _dpapi_unprotect(raw).decode("utf-8")
    return json.loads(text)


def _write_protected_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path.write_bytes(_dpapi_protect(raw))


class ActivationAttemptGuard:
    def __init__(self) -> None:
        self.path = app_data_dir() / _ATTEMPT_NAME

    def state(self) -> dict:
        try:
            data = _read_protected_json(self.path)
        except Exception:
            return {}
        if data.get("app_id") != app_sha256() or data.get("policy_version") != ATTEMPT_POLICY_VERSION:
            return {}
        return data

    def locked_message(self) -> str:
        data = self.state()
        until_text = data.get("locked_until")
        if not until_text:
            return ""
        try:
            until = datetime.fromisoformat(until_text)
        except Exception:
            return ""
        if datetime.now(timezone.utc) < until:
            minutes = max(1, round((until - datetime.now(timezone.utc)).total_seconds() / 60))
            return f"激活失败次数过多，请 {minutes} 分钟后再试。"
        return ""

    def record_success(self) -> None:
        _write_protected_json(self.path, {"app_id": app_sha256(), "policy_version": ATTEMPT_POLICY_VERSION, "failures": 0, "locked_until": ""})

    def record_failure(self) -> str:
        data = self.state()
        failures = int(data.get("failures") or 0) + 1
        payload = {"app_id": app_sha256(), "policy_version": ATTEMPT_POLICY_VERSION, "failures": failures, "locked_until": ""}
        message = f"激活失败 {failures}/{FAILURE_LIMIT} 次。"
        if failures >= FAILURE_LIMIT:
            until = datetime.now(timezone.utc) + timedelta(minutes=LOCK_MINUTES)
            payload = {"app_id": app_sha256(), "policy_version": ATTEMPT_POLICY_VERSION, "failures": 0, "locked_until": until.isoformat()}
            message = f"连续失败已锁定 {LOCK_MINUTES} 分钟。"
        _write_protected_json(self.path, payload)
        return message


def format_license_countdown(expire_at: str, now: datetime) -> tuple[str, str]:
    """把激活到期时间格式化成倒计时展示文案，返回 (文案, 级别)。级别∈ success/warning/danger/muted。

    纯函数（now 由调用方传入），便于单测；供 UI 左下角激活倒计时使用。
    """
    if not expire_at:
        return ("激活状态：未激活", "muted")
    try:
        exp = datetime.fromisoformat(str(expire_at))
    except (ValueError, TypeError):
        return ("激活有效期：未知", "muted")
    # 统一到无时区比较（缓存里可能带/不带时区）
    if exp.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=exp.tzinfo)
    elif exp.tzinfo is None and now.tzinfo is not None:
        exp = exp.replace(tzinfo=now.tzinfo)
    secs = (exp - now).total_seconds()
    if secs <= 0:
        return ("激活已过期", "danger")
    days = int(secs // 86400)
    hours = int((secs % 86400) // 3600)
    minutes = int((secs % 3600) // 60)
    if days >= 1:
        text = f"激活有效期剩 {days} 天 {hours} 小时"
    elif hours >= 1:
        text = f"激活有效期剩 {hours} 小时 {minutes} 分"
    else:
        text = f"激活有效期剩 {minutes} 分"
    level = "danger" if days < 1 else ("warning" if days < 7 else "success")
    return (text, level)


class LicenseCache:
    def __init__(self) -> None:
        self.path = app_data_dir() / _CACHE_NAME

    def load(self) -> dict:
        try:
            data = _read_protected_json(self.path)
        except Exception:
            return {}
        if data.get("machine_id") != machine_id():
            return {}
        if data.get("app_id") != app_sha256():
            return {}
        return data

    def save(self, code: str, response: dict) -> None:
        _write_protected_json(
            self.path,
            {
                "code": code,
                "machine_id": machine_id(),
                "app_id": app_sha256(),
                "expire_at": response.get("expire_at") or "",
                "saved_at": datetime.now(timezone.utc).isoformat(),
            },
        )


class LicenseClient:
    def __init__(self) -> None:
        self.base_url = _decode_endpoint().rstrip("/")
        self.mid = machine_id()
        self.app_id = app_sha256()

    def base_payload(self, code: str) -> dict:
        return {
            "code": code,
            "machine_id": self.mid,
            "app_id": self.app_id,
            "device_name": platform.node() or socket.gethostname(),
            "system_version": f"{platform.system()} {platform.release()} {platform.version()}",
            "app_version": APP_VERSION,
        }

    def activate(self, code: str) -> dict:
        return self._post("/api/client/activate", self.base_payload(code))

    def start(self, code: str) -> LicenseSession:
        response = self._post("/api/client/start", self.base_payload(code))
        token = str(response.get("session_token") or "")
        if not token:
            raise LicenseError("授权服务未返回会话 token。")
        return LicenseSession(
            code=code,
            machine_id=self.mid,
            app_id=self.app_id,
            session_token=token,
            heartbeat_interval=int(response.get("heartbeat_interval") or 30),
            expire_at=str(response.get("expire_at") or ""),
        )

    def activate_and_start(self, code: str) -> LicenseSession:
        self.activate(code)
        session = self.start(code)
        LicenseCache().save(code, {"expire_at": session.expire_at})
        return session

    def heartbeat(self, session: LicenseSession) -> dict:
        payload = self.base_payload(session.code)
        payload.update(
            {
                "session_token": session.session_token,
                "task_running": True,
                "current_task_id": "desktop-client",
                "current_task_name": "水星剪辑桌面端",
            }
        )
        return self._post("/api/client/heartbeat", payload)

    def logout(self, session: LicenseSession) -> None:
        payload = self.base_payload(session.code)
        payload.update(
            {
                "session_token": session.session_token,
                "task_running": False,
                "current_task_id": "",
                "current_task_name": "",
            }
        )
        try:
            self._post("/api/client/logout", payload)
        except Exception:
            pass

    def _post(self, path: str, payload: dict) -> dict:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": CLIENT_USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise LicenseError(_extract_error(raw) or f"授权请求失败：HTTP {exc.code}") from exc
        except Exception as exc:
            raise LicenseNetworkError("无法连接授权服务，请检查网络后重试。") from exc
        try:
            result = json.loads(raw or "{}")
        except Exception as exc:
            raise LicenseError("授权服务返回格式异常。") from exc
        if result.get("success") is False:
            raise LicenseError(str(result.get("message") or result.get("detail") or "授权失败。"))
        return result


def _extract_error(raw: str) -> str:
    try:
        payload = json.loads(raw or "{}")
    except Exception:
        return raw[:160]
    return str(payload.get("detail") or payload.get("message") or "").strip()


class ActivationDialog(tk.Tk):
    def __init__(self, initial_message: str = "") -> None:
        super().__init__()
        self.title(f"{APP_NAME} 激活")
        self.geometry("520x360")
        self.resizable(False, False)
        self.configure(bg="#f6f8fb")
        self.session: LicenseSession | None = None
        self.code_var = tk.StringVar()
        self.status_var = tk.StringVar(value=initial_message or "请输入激活码，验证成功后进入软件。")
        self.guard = ActivationAttemptGuard()
        self._build()

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=22)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=APP_NAME, font=("", 20, "bold")).pack(anchor="w")
        ttk.Label(frame, text="授权激活", font=("", 11)).pack(anchor="w", pady=(4, 16))

        machine_box = ttk.LabelFrame(frame, text="本机机器码（用于绑定设备，不是软件 APP ID）")
        machine_box.pack(fill=tk.X, pady=(0, 12))
        machine_text = machine_id()
        ttk.Label(machine_box, text=machine_text, wraplength=450).pack(anchor="w", padx=10, pady=8)

        code_box = ttk.LabelFrame(frame, text="激活码")
        code_box.pack(fill=tk.X, pady=(0, 12))
        entry = ttk.Entry(code_box, textvariable=self.code_var, show="*", width=42)
        entry.pack(side=tk.LEFT, padx=10, pady=10, fill=tk.X, expand=True)
        entry.focus_set()
        ttk.Button(code_box, text="激活并进入", command=self.activate).pack(side=tk.LEFT, padx=(0, 10))

        ttk.Label(frame, textvariable=self.status_var, foreground="#344054", wraplength=455).pack(anchor="w", pady=(2, 12))
        ttk.Label(frame, text="授权服务地址已内置，客户端页面不会展示。", foreground="#667085").pack(anchor="w")
        self.bind("<Return>", lambda _event: self.activate())

    def activate(self) -> None:
        locked = self.guard.locked_message()
        if locked:
            self.status_var.set(locked)
            return
        code = self.code_var.get().strip()
        if not code:
            self.status_var.set("请先输入激活码。")
            return
        self.status_var.set("正在连接授权服务...")
        self.update_idletasks()
        try:
            self.session = LicenseClient().activate_and_start(code)
            self.guard.record_success()
            self.destroy()
        except LicenseNetworkError as exc:
            self.status_var.set(str(exc))
        except Exception as exc:
            lock_message = self.guard.record_failure()
            self.status_var.set(f"{exc} {lock_message}")


def show_security_error(message: str) -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(APP_NAME, message)
    root.destroy()


def load_or_activate_session() -> LicenseSession | None:
    client = LicenseClient()
    cache = LicenseCache().load()
    if cache.get("code"):
        try:
            return client.start(str(cache["code"]))
        except Exception as exc:
            dialog = ActivationDialog(f"本地授权需要重新验证：{exc}")
            dialog.mainloop()
            return dialog.session
    dialog = ActivationDialog()
    dialog.mainloop()
    return dialog.session


def start_local_runtime_services() -> None:
    # 当前本地能力以随调随用的 CLI/模型为主，这里保留授权通过后的统一启动钩子。
    runtime_state = app_data_dir() / "runtime_services.json"
    runtime_state.write_text(
        json.dumps({"started_at": datetime.now().isoformat(timespec="seconds"), "app_id": app_sha256()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def guarded_main(app_factory) -> None:
    try:
        anti_debug_check()
        verify_exe_integrity()
    except SecurityViolation as exc:
        show_security_error(str(exc))
        return

    session: LicenseSession | None = None
    if getattr(sys, "frozen", False) or os.environ.get("MERCURY_ENFORCE_LICENSE") == "1":
        session = load_or_activate_session()
        if not session:
            return
        start_local_runtime_services()

    app = app_factory()
    if session:
        client = LicenseClient()

        def close_app() -> None:
            session.stop()
            client.logout(session)
            app.destroy()

        def invalid(message: str) -> None:
            messagebox.showerror(APP_NAME, message)
            close_app()

        app.protocol("WM_DELETE_WINDOW", close_app)
        session.start_heartbeat(app, invalid)
    app.mainloop()
