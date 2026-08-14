"""把 Edge TTS 引擎适配成 Scheduler 需要的 TtsBackend。

R5 修复：
    - 单次请求版：**不再**走 EdgeTtsEngine 内部的 3 次重试；本层每 attempt 只发一次请求。
      避免与 scheduler 5 次重试形成 3×5=15 的双重乘法重试。
    - 保留真实 HTTP 状态码（429/5xx/4xx）和 Retry-After。
    - 通用网络异常（DNS/连接被拒/超时）被归为可重试的 5xx 语义抛出，让 scheduler 决定退避。

MockTtsBackend 保留不变（测试用）。
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

from ..engines.base import EngineUnavailable
from ..engines.edge_tts import (
    DEFAULT_STYLE, _endpoint_url, _normalize_endpoint_root,
    _convert_mp3_to_wav, edge_tts_endpoint,
)

from .scheduler import TtsBackend, TtsHttpError


_DEFAULT_TIMEOUT_S = 90.0


class EdgeTtsBackend(TtsBackend):
    """单次请求 Edge TTS 适配层——不做内部重试，交给 scheduler。

    R11-6 Endpoint 热更新：
        - `endpoint=None`（默认）→ **每次 synthesize** 都从 settings 读一遍
          `edge_tts_endpoint()`；工具箱保存新地址后立即生效，无需重启软件。
        - `endpoint="..."`（显式传入）→ 固定用该地址（测试用）。

    R13-P1-7 backend capability：`requires_endpoint = True` —— 生产 backend
    必须先配置 Cloudflare Worker 地址；service 层从 backend 读该属性，
    不再暴露"生产参数跳过 endpoint 校验"的入口。
    """
    requires_endpoint = True

    def __init__(self, endpoint: str | None = None,
                 timeout: float = _DEFAULT_TIMEOUT_S,
                 default_voice: str = "zh-CN-XiaoshuangNeural") -> None:
        self._explicit_endpoint = (
            _normalize_endpoint_root(endpoint) if endpoint is not None else None
        )
        self.timeout = timeout
        self.default_voice = default_voice

    @property
    def endpoint_root(self) -> str:
        """兼容：老代码 / 测试仍读 backend.endpoint_root。"""
        if self._explicit_endpoint is not None:
            return self._explicit_endpoint
        return edge_tts_endpoint()

    def _current_endpoint(self) -> str:
        return self.endpoint_root

    def synthesize(self, *, text: str, voice_id: str, speed: float,
                   pitch: int, style: str, output_wav: Path) -> float:
        endpoint_root = self._current_endpoint()
        if not endpoint_root:
            # 未配置：客户端错（4xx 语义）——scheduler 不重试
            raise TtsHttpError(400, "Edge TTS 未配置服务地址。到工具箱填 Worker 根地址后再试。")
        if not text.strip():
            raise TtsHttpError(400, "TTS 文案为空")

        voice = voice_id or self.default_voice
        payload = {
            "input": text,
            "voice": voice,
            "speed": float(speed),
            "pitch": int(pitch),
            "style": style or DEFAULT_STYLE,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json",
                   "Accept": "audio/mpeg, application/json"}

        # 单次请求（不重试）
        try:
            req = urllib.request.Request(
                _endpoint_url(endpoint_root), data=body,
                headers=headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                data = resp.read()
                # Worker 常把上游 429/5xx 包装为 HTTP 200 + application/json
                self._maybe_raise_from_json_body(ctype, data, resp)
                # 校验音频响应
                if not data:
                    raise TtsHttpError(502, "Edge TTS 返回空响应（0 字节）")
                if not (ctype.startswith("audio/") or ctype == "application/octet-stream"):
                    raise TtsHttpError(
                        502,
                        f"Edge TTS 返回非音频（Content-Type={ctype or '空'}）",
                    )
                mp3_bytes = data
        except TtsHttpError:
            raise
        except urllib.error.HTTPError as exc:
            # 真 HTTP 错误：保留原状态和 Retry-After
            text_body = _peek_body(exc)
            retry_after = _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)
            raise TtsHttpError(
                int(exc.code),
                _http_error_hint(exc.code, text_body),
                retry_after=retry_after,
            ) from exc
        except (TimeoutError, socket.timeout) as exc:
            # 网络超时 → 计入熔断（服务器侧不可达）
            raise TtsHttpError(504, f"Edge TTS 请求超时（{int(self.timeout)}s）") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            raise TtsHttpError(503, f"Edge TTS 连接失败：{reason}") from exc

        # MP3 → WAV
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        tmp_mp3 = output_wav.with_suffix(output_wav.suffix + ".part.mp3")
        try:
            tmp_mp3.write_bytes(mp3_bytes)
            _convert_mp3_to_wav(tmp_mp3, output_wav,
                                 sample_rate=44100, channels=2)
        except EngineUnavailable as exc:
            # ffmpeg 缺失属于本地环境错——客户端 4xx 语义，让上层报明确
            raise TtsHttpError(400, str(exc)) from exc
        finally:
            if tmp_mp3.exists():
                try:
                    tmp_mp3.unlink()
                except OSError:
                    pass

        try:
            return _wav_seconds(output_wav)
        except Exception:  # noqa: BLE001
            return 0.0

    @staticmethod
    def _maybe_raise_from_json_body(ctype: str, data: bytes, resp) -> None:
        """Worker 若把上游 429/5xx 包装成 JSON+200 → 转成对应 TtsHttpError。"""
        if "application/json" not in ctype:
            return
        try:
            parsed = json.loads(data.decode("utf-8", "ignore"))
        except Exception:
            parsed = {}
        err = parsed.get("error") if isinstance(parsed, dict) else None
        message = ""
        code_hint = ""
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("Message") or "").strip()
            code_hint = str(err.get("code") or err.get("Code") or "").strip()
        elif isinstance(err, str):
            message = err
        combined = f"[{code_hint}] {message}" if code_hint else message
        low = (combined or "").lower()
        retry_flags = ("429", "rate", "limit", "busy", "overload", "quota",
                        "too many", "temporarily")
        retry_after = _parse_retry_after(
            resp.headers.get("Retry-After") if resp and resp.headers else None
        )
        if any(k in low for k in retry_flags):
            raise TtsHttpError(429, f"Edge TTS 服务繁忙：{combined or '未知'}",
                                retry_after=retry_after)
        raise TtsHttpError(502, f"Edge TTS 合成失败：{combined or '未知'}",
                            retry_after=retry_after)


class MockTtsBackend(TtsBackend):
    """确定性 mock：生成指定时长的静音 WAV，用于压测和单元测试。

    R13-P1-7：`requires_endpoint = False` —— mock backend 内部生成，无需
    Cloudflare Worker 地址；service 依 capability 判断自动跳过端点校验，
    生产 backend 无绕过入口。
    """
    requires_endpoint = False

    def __init__(self, *, base_seconds: float = 3.0,
                 http_error_every: int = 0,
                 delay: float = 0.0) -> None:
        self.base_seconds = base_seconds
        self.http_error_every = http_error_every
        self.delay = delay
        self._counter = 0
        import threading
        self._lock = threading.Lock()

    def synthesize(self, *, text: str, voice_id: str, speed: float,
                   pitch: int, style: str, output_wav: Path) -> float:
        with self._lock:
            self._counter += 1
            n = self._counter
        if self.http_error_every and (n % self.http_error_every == 0):
            raise TtsHttpError(429, "mock 限流", retry_after=1.0)
        if self.delay > 0:
            time.sleep(self.delay)
        seconds = max(0.2, self.base_seconds / max(0.1, speed))
        _write_silence_wav(output_wav, seconds, sample_rate=44100)
        return seconds


def _write_silence_wav(path: Path, seconds: float, *, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * frame_count)


def _wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        frames = w.getnframes()
        rate = w.getframerate() or 44100
        return frames / rate


def _peek_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""


def _parse_retry_after(value: str | None) -> float | None:
    """支持 "整数秒" 和 HTTP-date 两种格式（HTTP-date 转秒数近似）。"""
    if not value:
        return None
    v = value.strip()
    if v.isdigit():
        try:
            return float(v)
        except ValueError:
            return None
    try:
        # HTTP-date
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(v)
        return max(0.0, dt.timestamp() - time.time())
    except Exception:  # noqa: BLE001
        return None


def _http_error_hint(code: int, body: str) -> str:
    if code in (401, 403):
        return f"Edge TTS 鉴权失败（HTTP {code}）：{body[:200]}"
    if code == 404:
        return f"Edge TTS 地址无该接口（HTTP 404）：{body[:120]}"
    if code == 429:
        return "Edge TTS 请求过多被限流（HTTP 429）"
    if 500 <= code < 600:
        return f"Edge TTS 服务器错误（HTTP {code}）：{body[:200]}"
    return f"Edge TTS 请求失败（HTTP {code}）：{body[:200]}"
