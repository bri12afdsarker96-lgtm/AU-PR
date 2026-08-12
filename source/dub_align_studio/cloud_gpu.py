"""云 GPU 会话管理（优云智算 / 通用 SSH 主机）。

用户 2026-08 需求：把 SSH 登录指令/密码做到客户端里，避免每次手动上服务器；
成片跑完空闲 5 分钟自动关机；一键唤醒 / 一键关机。

设计取舍：
    - **凭据加密**：Windows DPAPI（CryptProtectData，用户级 Scope=CurrentUser）——
      只有当前 Windows 用户在本机能解，换机重设。缺 pywin32（如非 Win 或未装）
      降级明文并在 status 里打 warn，永远不静默丢失安全性。
    - **传输**：paramiko（成熟、纯 Python-ish）。缺依赖时 wake/sleep 返回可读错误，
      不阻塞其他模块加载 —— 用延迟 import。
    - **空闲判定**：以「最后一次任务活跃时刻」为基准（web_server 在 _run_job
      起始/完成时调 mark_active）。看门狗每 30s tick；idle_paused_until 允许用户
      临时暂停自动关（比如离席午休）。
    - **云 API**（优云智算 OpenAPI）：预留 provider 接口，本版先只做 SSH 通路，
      100% 通用不绑厂商；后续接 UCloud/CompShare 的 StopUHostInstance 可平替。
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from . import settings as studio_settings


# ────────────────────────────────────────────────────────────────── 常量
_SETTINGS_KEY = "cloud_gpu"
_DEFAULT_IDLE_MINUTES = 5.0    # 空闲多久自动关机（分钟）
_TICK_SECONDS = 30.0           # 看门狗心跳间隔
_PROBE_TIMEOUT = 4.0           # 探活 HTTP 请求超时（秒）
_SSH_TIMEOUT = 15.0            # SSH 连接超时（秒）
_SSH_EXEC_TIMEOUT = 60.0       # 单条命令执行超时（秒）
_WAKE_TIMEOUT_SECONDS = 5 * 60 # 唤醒等待就绪最长（秒）
_WAKE_POLL_INTERVAL = 5.0      # 唤醒等待就绪的探活轮询间隔（秒）
_UCLOUD_ENDPOINT = "https://api.ucloud.cn/"   # 默认 UCloud OpenAPI base；优云智算复用同一网关


# ────────────────────────────────────────────────────────────────── 凭据加密
_DPAPI_AVAILABLE: bool | None = None


def _dpapi_available() -> bool:
    """判定 win32crypt 是否可用；结果缓存一次。"""
    global _DPAPI_AVAILABLE
    if _DPAPI_AVAILABLE is None:
        try:
            import win32crypt  # noqa: F401
            _DPAPI_AVAILABLE = True
        except Exception:  # noqa: BLE001
            _DPAPI_AVAILABLE = False
    return _DPAPI_AVAILABLE


def encrypt_str(plain: str) -> str:
    """把明文加密为 `dpapi:<base64>` 字符串；无 DPAPI 时打 `plain:<base64>` 兜底。

    永远不返回赤裸明文，避免误把明文写进 settings.json 被人肉眼扫到；`plain:` 前缀
    显式暴露"未加密"事实，UI 可据此告警。"""
    if not plain:
        return ""
    if _dpapi_available():
        try:
            import win32crypt

            blob = win32crypt.CryptProtectData(plain.encode("utf-8"), "mercury-cloud-gpu",
                                                None, None, None, 0)
            return "dpapi:" + base64.b64encode(blob).decode("ascii")
        except Exception:  # noqa: BLE001
            pass
    return "plain:" + base64.b64encode(plain.encode("utf-8")).decode("ascii")


def decrypt_str(cipher: str) -> str:
    """把 encrypt_str 的产物还原成明文；无法还原返回空串（不抛，避免整块 config 崩）。"""
    if not cipher:
        return ""
    if cipher.startswith("dpapi:"):
        if not _dpapi_available():
            return ""     # 换到没 DPAPI 的机器上；换机需要重设，与设计一致
        try:
            import win32crypt

            blob = base64.b64decode(cipher[len("dpapi:"):].encode("ascii"))
            _desc, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
            return data.decode("utf-8")
        except Exception:  # noqa: BLE001
            return ""
    if cipher.startswith("plain:"):
        try:
            return base64.b64decode(cipher[len("plain:"):].encode("ascii")).decode("utf-8")
        except Exception:  # noqa: BLE001
            return ""
    # 兼容老配置里裸明文（升级路径）——一次性读到即刻改写成加密形式（由 save 触发）
    return cipher


def credential_mode() -> str:
    """当前凭据存储模式："dpapi" 或 "plain"——供 UI 打 warn。"""
    return "dpapi" if _dpapi_available() else "plain"


# ────────────────────────────────────────────────────────────────── 配置
@dataclass
class CloudConfig:
    enabled: bool = False
    provider: str = "ssh"          # 'ssh' | 'compshare'（compshare 走 UCloud OpenAPI 真正停机）
    host: str = ""
    port: int = 22
    user: str = "root"
    password_cipher: str = ""      # encrypt_str 的产物
    key_path: str = ""             # 私钥文件路径（优先于密码）
    wake_cmd: str = ""             # SSH 唤醒后要执行的命令（比如启动 dots.tts 服务）
    sleep_cmd: str = "sudo shutdown -h now"
    health_url: str = ""           # 探活 URL（一般=dots_remote_endpoint + /health 或 /ping）
    idle_minutes: float = _DEFAULT_IDLE_MINUTES
    auto_sleep: bool = True        # 关掉即彻底手动
    idle_paused_until: float = 0.0 # 用户临时暂停自动关（epoch 秒；<=now 视为未暂停）
    # CompShare / UCloud OpenAPI：填了 provider=compshare 就走 API 停机（不计费），
    # 空缺退回 SSH shutdown。key/secret 同样 DPAPI 加密存储。
    cs_endpoint: str = _UCLOUD_ENDPOINT
    cs_public_key: str = ""
    cs_private_key_cipher: str = ""
    cs_project_id: str = ""
    cs_region: str = ""            # 例："cn-bj2"
    cs_zone: str = ""              # 例："cn-bj2-04"（部分 Action 需要）
    cs_instance_id: str = ""       # 例："uhost-xxxxxxxx"


def load_cloud_config() -> CloudConfig:
    """从 settings.json 读回；缺字段用 dataclass 默认值补齐。"""
    raw = studio_settings.load_settings().get(_SETTINGS_KEY) or {}
    if not isinstance(raw, dict):
        raw = {}
    valid = {k: v for k, v in raw.items() if k in CloudConfig.__dataclass_fields__}
    try:
        valid["port"] = int(valid.get("port") or 22)
    except Exception:  # noqa: BLE001
        valid["port"] = 22
    try:
        valid["idle_minutes"] = float(valid.get("idle_minutes") or _DEFAULT_IDLE_MINUTES)
    except Exception:  # noqa: BLE001
        valid["idle_minutes"] = _DEFAULT_IDLE_MINUTES
    valid["auto_sleep"] = bool(valid.get("auto_sleep", True))
    valid["enabled"] = bool(valid.get("enabled", False))
    return CloudConfig(**valid)


def save_cloud_config(update: dict) -> CloudConfig:
    """合并保存云 GPU 配置。password / cs_private_key 明文来时自动加密；空串=清空。

    换机后 dpapi 密文变空还原——需重设密码；这是 DPAPI 用户级作用域的既有约束。"""
    current = asdict(load_cloud_config())
    if "password" in update:
        pw = str(update.pop("password") or "")
        current["password_cipher"] = encrypt_str(pw) if pw else ""
    if "cs_private_key" in update:
        sk = str(update.pop("cs_private_key") or "")
        current["cs_private_key_cipher"] = encrypt_str(sk) if sk else ""
    for key, val in update.items():
        if key not in CloudConfig.__dataclass_fields__:
            continue
        current[key] = val
    prov = str(current.get("provider") or "ssh").lower()
    current["provider"] = prov if prov in ("ssh", "compshare") else "ssh"
    # 归一化数字类型（前端可能传字符串）
    try:
        current["port"] = int(current.get("port") or 22)
    except Exception:  # noqa: BLE001
        current["port"] = 22
    try:
        current["idle_minutes"] = max(0.5, float(current.get("idle_minutes") or _DEFAULT_IDLE_MINUTES))
    except Exception:  # noqa: BLE001
        current["idle_minutes"] = _DEFAULT_IDLE_MINUTES

    settings = studio_settings.load_settings()
    settings[_SETTINGS_KEY] = current
    studio_settings.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    studio_settings.SETTINGS_FILE.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return CloudConfig(**current)


# ────────────────────────────────────────────────────────────────── SSH 通路
class SshUnavailable(RuntimeError):
    """paramiko 未安装或连接失败——UI 需给友好提示（"请先在软件目录 pip install paramiko"）。"""


def _load_paramiko():
    try:
        import paramiko  # 延迟 import：无依赖时不阻塞模块加载
    except ImportError as exc:
        raise SshUnavailable(
            "缺 paramiko 模块。云 GPU 一键唤醒需要 SSH 客户端库；请在软件目录运行 "
            "`python\\python.exe -m pip install paramiko` 后再试。"
        ) from exc
    return paramiko


def ssh_exec(config: CloudConfig, command: str, timeout: float = _SSH_EXEC_TIMEOUT) -> tuple[int, str, str]:
    """在配置的云主机上跑一条 shell 命令，返回 (rc, stdout, stderr)。

    优先用密钥，退回密码。返回 rc 由远端命令决定；SSH 通路本身出错抛 SshUnavailable。"""
    if not config.host:
        raise SshUnavailable("云 GPU 未配置 host，先到「设置 · 云 GPU」填地址。")
    paramiko = _load_paramiko()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs = dict(hostname=config.host, port=int(config.port or 22),
                   username=config.user or "root", timeout=_SSH_TIMEOUT,
                   look_for_keys=False, allow_agent=False)
    if config.key_path and Path(config.key_path).is_file():
        kwargs["key_filename"] = config.key_path
    else:
        password = decrypt_str(config.password_cipher)
        if not password:
            raise SshUnavailable("云 GPU 未设密码且未配密钥，请到「设置 · 云 GPU」补齐。")
        kwargs["password"] = password
    try:
        client.connect(**kwargs)
    except Exception as exc:  # noqa: BLE001
        raise SshUnavailable(f"SSH 连接失败：{exc}") from exc
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        stdin.close()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        rc = stdout.channel.recv_exit_status()
        return rc, out, err
    finally:
        client.close()


# ────────────────────────────────────────────────────────────────── UCloud/CompShare OpenAPI
def ucloud_signature(params: dict, private_key: str) -> str:
    """UCloud 官方签名算法：把参数按 key 字典序拼成 k1v1k2v2... 后追加 private_key，取 SHA1 hex。
    优云智算复用同一网关，签名规则一致；若你的账号走不同的端点，只需改 cs_endpoint。"""
    items = sorted((str(k), str(v)) for k, v in params.items() if v is not None and v != "")
    joined = "".join(f"{k}{v}" for k, v in items)
    return hashlib.sha1((joined + private_key).encode("utf-8")).hexdigest()


def ucloud_call(endpoint: str, action: str, public_key: str, private_key: str,
                extra: dict | None = None, timeout: float = 10.0) -> dict:
    """通用 UCloud OpenAPI 调用；返回解析后的 JSON（含 RetCode/Message/…）。
    RetCode != 0 时保留返回值给上层，让 UI 显示原文，不改字段避免与官方文档失配。"""
    params: dict = {"Action": action, "PublicKey": public_key}
    if extra:
        params.update({k: v for k, v in extra.items() if v is not None and v != ""})
    params["Signature"] = ucloud_signature(params, private_key)
    url = endpoint.rstrip("/") + "/?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return {"RetCode": -1, "Message": f"HTTP 请求失败：{exc}"}
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return {"RetCode": -1, "Message": f"响应非 JSON：{body[:400]}"}


def _compshare_creds(config: CloudConfig) -> tuple[str, str] | None:
    """返回 (public_key, private_key) 或 None（必填三项不齐：PublicKey + PrivateKey + InstanceId）。

    优云智算 API 密钥页只发一对 pub/private，实例卡里只保证有 `uhost-*` ID；
    Region/Zone/Project **不强求**——容器实例许多接口不需要。UI 也把这三项
    折进"高级"（默认不填），用户按需再填。"""
    pk = (config.cs_public_key or "").strip()
    sk = decrypt_str(config.cs_private_key_cipher)
    if not pk or not sk or not (config.cs_instance_id or "").strip():
        return None
    return pk, sk


def _cs_optional_extras(config: CloudConfig, include_zone: bool = False) -> dict:
    """把非空的可选字段拼进 API 参数；空的**不传**——避免签名把空串带进去、以及
    带上容器实例根本不认的 UHost 特有字段（Zone 只有 Start/Stop 才可能需要）。"""
    extras: dict = {}
    if (config.cs_region or "").strip():
        extras["Region"] = config.cs_region.strip()
    if include_zone and (config.cs_zone or "").strip():
        extras["Zone"] = config.cs_zone.strip()
    if (config.cs_project_id or "").strip():
        extras["ProjectId"] = config.cs_project_id.strip()
    return extras


def compshare_stop(config: CloudConfig) -> tuple[bool, str]:
    """走优云智算/UCloud OpenAPI 停机（等价"控制台点关机"，不再计费）。

    ⚠ 优云智算的容器实例 API 规格与官方 UHostInstance 未必完全一致——首次调用如
    RetCode≠0，把 Message 反馈给开发按真实文档调 Action 名（当前默认
    StopUHostInstance）。API 失败时 CloudManager 会自动回退 SSH shutdown 兜底。"""
    creds = _compshare_creds(config)
    if not creds:
        return False, "云 API 凭据/InstanceId 不齐（至少要 PublicKey + PrivateKey + InstanceId），回退 SSH 关机。"
    pk, sk = creds
    extras = _cs_optional_extras(config, include_zone=True)
    extras["UHostId"] = config.cs_instance_id
    j = ucloud_call(config.cs_endpoint or _UCLOUD_ENDPOINT, "StopUHostInstance", pk, sk, extra=extras)
    rc = j.get("RetCode")
    msg = j.get("Message") or ""
    if rc == 0:
        return True, f"云 API StopUHostInstance 已下发（{config.cs_instance_id}）。"
    return False, f"云 API StopUHostInstance 失败 RetCode={rc}：{msg}"


def compshare_start(config: CloudConfig) -> tuple[bool, str]:
    creds = _compshare_creds(config)
    if not creds:
        return False, "云 API 凭据/InstanceId 不齐（至少要 PublicKey + PrivateKey + InstanceId）。"
    pk, sk = creds
    extras = _cs_optional_extras(config, include_zone=True)
    extras["UHostId"] = config.cs_instance_id
    j = ucloud_call(config.cs_endpoint or _UCLOUD_ENDPOINT, "StartUHostInstance", pk, sk, extra=extras)
    rc = j.get("RetCode")
    msg = j.get("Message") or ""
    if rc == 0:
        return True, f"云 API StartUHostInstance 已下发（{config.cs_instance_id}）。"
    return False, f"云 API StartUHostInstance 失败 RetCode={rc}：{msg}"


def compshare_state(config: CloudConfig) -> str:
    """查询实例当前 State（Running / Stopped / Starting / Stopping / …）。空串=查询失败。"""
    creds = _compshare_creds(config)
    if not creds:
        return ""
    pk, sk = creds
    extras = _cs_optional_extras(config, include_zone=False)
    extras["UHostIds.0"] = config.cs_instance_id
    j = ucloud_call(config.cs_endpoint or _UCLOUD_ENDPOINT, "DescribeUHostInstance", pk, sk, extra=extras)
    if j.get("RetCode") != 0:
        return ""
    sets = j.get("UHostSet") or []
    if not sets:
        return ""
    return str(sets[0].get("State") or "")


def http_probe(url: str, timeout: float = _PROBE_TIMEOUT) -> bool:
    """探活：给个 URL，返回是否 200~399。URL 为空时视为"未配置探活"、跳过（返回 True）。"""
    if not url:
        return True
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as exc:
        return 200 <= exc.code < 400
    except Exception:  # noqa: BLE001
        return False


# ────────────────────────────────────────────────────────────────── 空闲看门狗
class IdleWatchdog:
    """后台线程按 tick_seconds 心跳；空闲超阈值就调 on_timeout（一次性，触发后自复位）。

    单元测试用 tick_seconds=0.05 + time_source 注入时钟即可加速验证。"""

    def __init__(self, get_config: Callable[[], CloudConfig], on_timeout: Callable[[], None],
                 tick_seconds: float = _TICK_SECONDS,
                 time_source: Callable[[], float] = time.time) -> None:
        self._get_config = get_config
        self._on_timeout = on_timeout
        self._tick = tick_seconds
        self._now = time_source
        self._last_active = self._now()
        self._triggered_at = 0.0     # 上一次触发关机的时刻，防重复触发
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def mark_active(self) -> None:
        with self._lock:
            self._last_active = self._now()
            self._triggered_at = 0.0

    def last_active(self) -> float:
        with self._lock:
            return self._last_active

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cloud-idle-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    def check_once(self) -> bool:
        """执行一次判定：若达到关机条件返回 True 并触发回调；否则返回 False。
        供测试直接驱动、也供 loop 内复用。"""
        config = self._get_config()
        if not config.enabled or not config.auto_sleep:
            return False
        idle_seconds = max(0.5, float(config.idle_minutes)) * 60.0
        now = self._now()
        # 用户手动暂停期内不触发
        if config.idle_paused_until and now < float(config.idle_paused_until):
            return False
        with self._lock:
            elapsed = now - self._last_active
            already_triggered = (self._triggered_at > self._last_active)
        if elapsed < idle_seconds or already_triggered:
            return False
        try:
            self._on_timeout()
        except Exception:  # noqa: BLE001
            pass
        with self._lock:
            self._triggered_at = self._now()
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self._tick):
            try:
                self.check_once()
            except Exception:  # noqa: BLE001
                pass  # 看门狗永不抛错——挂了下一轮继续


# ────────────────────────────────────────────────────────────────── 管理器（web_server 唯一入口）
@dataclass
class WakeState:
    """异步唤醒工作线程的进度快照——供前端顶栏灯 + 进度条读取。"""
    stage: str = "idle"        # idle / starting / probing / ready / failed
    started_at: float = 0.0
    updated_at: float = 0.0
    elapsed: float = 0.0
    attempts: int = 0          # 探活尝试次数
    message: str = ""


class CloudManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_status: dict = {"action": "", "ok": False, "message": "", "at": 0.0}
        self._wake_state = WakeState()
        self._wake_lock = threading.Lock()
        self._wake_thread: threading.Thread | None = None
        self._watchdog = IdleWatchdog(load_cloud_config, self._auto_sleep_now)
        self._watchdog.start()

    # ---- 状态 ----
    def status(self) -> dict:
        config = load_cloud_config()
        idle_seconds = max(0.0, self._watchdog._now() - self._watchdog.last_active())
        auto_paused = bool(config.idle_paused_until and self._watchdog._now() < float(config.idle_paused_until))
        with self._lock:
            snapshot = dict(self._last_status)
        with self._wake_lock:
            wake = asdict(self._wake_state)
        # 探活（可选）：health_url 有值则实时探
        alive = http_probe(config.health_url) if config.health_url else None
        cs_ready = bool(_compshare_creds(config))
        return {
            "enabled": config.enabled,
            "provider": config.provider,
            "host": config.host,
            "port": config.port,
            "user": config.user,
            "has_password": bool(config.password_cipher),
            "has_key": bool(config.key_path and Path(config.key_path).is_file()),
            "wake_cmd": config.wake_cmd,
            "sleep_cmd": config.sleep_cmd,
            "health_url": config.health_url,
            "idle_minutes": config.idle_minutes,
            "auto_sleep": config.auto_sleep,
            "auto_paused": auto_paused,
            "idle_paused_until": config.idle_paused_until,
            "credential_mode": credential_mode(),
            "idle_seconds": round(idle_seconds, 1),
            "alive": alive,
            "last_action": snapshot,
            "wake_state": wake,
            "cs_ready": cs_ready,
            "cs_endpoint": config.cs_endpoint,
            "cs_public_key": config.cs_public_key,
            "cs_project_id": config.cs_project_id,
            "cs_region": config.cs_region,
            "cs_zone": config.cs_zone,
            "cs_instance_id": config.cs_instance_id,
            "has_cs_secret": bool(config.cs_private_key_cipher),
        }

    def mark_active(self) -> None:
        self._watchdog.mark_active()

    # ---- 动作 ----
    def wake(self) -> dict:
        """异步唤醒：立即返回；后台线程执行 ssh + 轮询 health_url。
        再次点击时若上一轮在跑就复用当前进度，不并发多个 wake 工作线程。"""
        self.mark_active()
        with self._wake_lock:
            if self._wake_thread and self._wake_thread.is_alive():
                snap = asdict(self._wake_state)
                return {"started": False, "message": "唤醒已在进行中…", "wake_state": snap}
            now = self._watchdog._now()
            self._wake_state = WakeState(stage="starting", started_at=now, updated_at=now,
                                          message="正在通过 SSH 下发唤醒命令…")
            self._wake_thread = threading.Thread(target=self._wake_worker, name="cloud-wake",
                                                  daemon=True)
            self._wake_thread.start()
            snap = asdict(self._wake_state)
        return {"started": True, "wake_state": snap}

    def sleep(self, reason: str = "manual") -> dict:
        """按 provider 分派：compshare 走 UCloud API 停机（真正停机不计费）；
        ssh 走 sudo shutdown。CompShare 凭据不齐时自动回退 SSH，保证总有兜底。"""
        config = load_cloud_config()
        if config.provider == "compshare" and _compshare_creds(config):
            ok, msg = compshare_stop(config)
            if ok:
                return self._record("sleep", ok=True, message=f"[{reason}] {msg}")
            # 云 API 失败 → 回退 SSH，避免"忘关"计费
            fallback = self._ssh_sleep(config, reason=f"{reason} · cloud-api-fallback")
            fallback["message"] = f"云 API 失败：{msg}；已回退 SSH：{fallback.get('message', '')}"
            return fallback
        return self._ssh_sleep(config, reason)

    def start_via_api(self) -> dict:
        """用 UCloud API 开机（前提：实例是 Stopped 状态）。SSH 无法完成这一步——
        机器已关机时 SSH 连不上；对已释放/暂停计费的实例，只有云 API 能拉起来。"""
        config = load_cloud_config()
        if config.provider != "compshare" or not _compshare_creds(config):
            return self._record("start_api", ok=False, message="未启用 CompShare provider 或凭据不齐。")
        ok, msg = compshare_start(config)
        self.mark_active()
        return self._record("start_api", ok=ok, message=msg)

    def pause_auto(self, minutes: float) -> dict:
        """暂停自动关机 N 分钟；minutes<=0 表示取消暂停。"""
        until = 0.0 if minutes <= 0 else self._watchdog._now() + minutes * 60.0
        save_cloud_config({"idle_paused_until": until})
        self.mark_active()
        return self.status()

    # ---- 内部 ----
    def _ssh_sleep(self, config: CloudConfig, reason: str) -> dict:
        if not config.sleep_cmd.strip():
            return self._record("sleep", ok=False, message="未配置 SSH 关机命令。")
        try:
            rc, out, err = ssh_exec(config, config.sleep_cmd, timeout=10.0)
        except SshUnavailable as exc:
            return self._record("sleep", ok=False, message=f"[{reason}] {exc}")
        detail = (out or err or "").strip()[:2000]
        # shutdown 命令 rc 常见非 0（连接被服务器主动断），仍视为下发成功
        return self._record("sleep", ok=True,
                             message=f"[{reason}] 关机指令已下发（rc={rc}）。{detail}".strip())

    def _auto_sleep_now(self) -> None:
        self.sleep(reason="auto-idle")

    def _record(self, action: str, ok: bool, message: str) -> dict:
        entry = {"action": action, "ok": ok, "message": message, "at": self._watchdog._now()}
        with self._lock:
            self._last_status = entry
        return entry

    # ---- wake 工作线程 ----
    def _wake_update(self, **fields) -> None:
        with self._wake_lock:
            now = self._watchdog._now()
            for k, v in fields.items():
                setattr(self._wake_state, k, v)
            self._wake_state.updated_at = now
            if self._wake_state.started_at:
                self._wake_state.elapsed = round(now - self._wake_state.started_at, 1)

    def _wake_worker(self) -> None:
        config = load_cloud_config()
        # 阶段 1：SSH 下发唤醒（若配了；不配就跳到探活阶段）
        if config.wake_cmd.strip():
            self._wake_update(stage="starting", message="SSH 下发唤醒命令…")
            try:
                rc, out, err = ssh_exec(config, config.wake_cmd)
            except SshUnavailable as exc:
                self._wake_update(stage="failed", message=str(exc))
                self._record("wake", ok=False, message=str(exc))
                return
            if rc != 0:
                snippet = (out or err or "").strip()[:400]
                msg = f"唤醒命令 rc={rc}：{snippet or '无输出'}"
                self._wake_update(stage="failed", message=msg)
                self._record("wake", ok=False, message=msg)
                return
        # 阶段 2：无探活 URL → 直接就绪；有则轮询直至 200 或超时
        if not config.health_url.strip():
            self._wake_update(stage="ready", message="唤醒命令已下发（未配置探活 URL）。")
            self._record("wake", ok=True, message="唤醒指令已下发。")
            return
        self._wake_update(stage="probing", message="等待服务上线（每 5 秒探活一次）…")
        deadline = self._watchdog._now() + _WAKE_TIMEOUT_SECONDS
        attempts = 0
        while self._watchdog._now() < deadline:
            attempts += 1
            self._wake_update(attempts=attempts, message=f"探活第 {attempts} 次…")
            if http_probe(config.health_url):
                msg = f"服务已上线（探活 {attempts} 次后就绪）。"
                self._wake_update(stage="ready", message=msg)
                self._record("wake", ok=True, message=msg)
                return
            # 用 stop_event.wait 更快响应停止（沿用 watchdog 的机制）
            time.sleep(_WAKE_POLL_INTERVAL)
        msg = f"等待服务上线超时（{attempts} 次探活，{_WAKE_TIMEOUT_SECONDS}s）。"
        self._wake_update(stage="failed", message=msg)
        self._record("wake", ok=False, message=msg)


_MANAGER: CloudManager | None = None
_MANAGER_LOCK = threading.Lock()


def manager() -> CloudManager:
    """惰性单例：首次访问才启动看门狗（unittest 不 import 就不启线程）。"""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = CloudManager()
        return _MANAGER


def reset_manager_for_tests() -> None:
    """测试用：停旧看门狗、丢单例。"""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is not None:
            try:
                _MANAGER._watchdog.stop()
            except Exception:  # noqa: BLE001
                pass
            _MANAGER = None
