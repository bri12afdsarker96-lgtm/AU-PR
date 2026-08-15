"""Edge TTS 免费云端预设音色引擎（对接 wangwangit/tts 的 Cloudflare Worker）。

定位：
    - 与 dots_remote 并列的**独立**引擎；不能替代 dots.tts（不做音色克隆）。
    - 仅调用第三方公开 Worker（如自建 Cloudflare Worker）— 背后是微软 Edge Read Aloud。
    - **预设合成音色**：晓晓/云希…；不读取任何参考音频，也不会污染 音色库/ 目录。

请求形态（Worker 固定接受）：
    POST {endpoint}/v1/audio/speech
    JSON: {input, voice, speed, pitch, style, volume?}     # 不发 model / response_format
    响应：audio/mpeg（MP3 字节流）

拼接流水线（复用现有）：
    · engines/longform.synthesize_long(per_line=True) 逐行调 synthesize_full；
    · 本引擎的 synthesize_full 拿到 MP3 后立刻用项目现有 ffmpeg（settings.ffmpeg_tool）
      转成**固定格式 PCM16 WAV**（44100Hz / stereo），保证 _concat_wavs 能直接拼接；
    · MP3 临时文件写在同目录 .part 里，成功即删；失败清理 .part / 半成品输出，不留脏。

错误分类（一律给中文可读信息）：
    · HTTP 4xx（用户可修复）：直接抛错；
    · HTTP 429 / 网络瞬断 / 可恢复 5xx：有限次数指数退避重试；
    · Worker 把上游 429 包装为 HTTP 500 JSON（error.message/code）— 也走重试；
    · 非音频 Content-Type / 空响应：抛"服务器未返回音频"错误。

MIT 归属：Worker 源码 https://github.com/wangwangit/tts （MIT）。本文件只做协议对接，
未复制其代码；接口字段名称与语义为兼容而对齐。微软 Edge 内部朗读接口不承诺 SLA、不承诺
商用授权，请自建 Cloudflare 免费账户部署 Worker 后自负配额与合规责任。
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from integrated_workbench.proc import run_silent

from .. import settings as studio_settings
from .base import (
    EngineCapabilities,
    EngineStatus,
    EngineUnavailable,
    MasterAudio,
    SynthesisOptions,
    wav_seconds,
    write_master_metadata,
)
from .voice_ref import VoiceRef


DEFAULT_TIMEOUT_S = 90.0        # 单行 TTS 上限；Worker 一般数秒返回
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 1.5         # 指数退避基数：1.5s, 3.0s, 6.0s
_WAV_SAMPLE_RATE = 44100
_WAV_CHANNELS = 2               # 与 mock/dots 输出一致；_concat_wavs 只要每段格式一致即可
_PROBE_SAMPLE_TEXT = "配音对齐工作室。"

# R14-FIX-4c：Cloudflare 边缘对无浏览器指纹的 UA（如 "Python-urllib/3.x"）
# 直接返回 HTTP 403 Error 1010，触发路径包括"测试连接"按钮与生产合成。
# 用常见 Chrome UA 伪装规避（不涉及 CF 账户端 Bot Fight Mode 设置）。
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _ssl_context():
    """HTTPS 用的 SSL context。

    冻结 exe（PyInstaller）里 Python 默认的 CA 证书路径往往找不到，导致对
    Cloudflare Worker（workers.dev）等 HTTPS 端点连接失败——而 license 服务器是
    HTTP 所以不受影响，正好解释「license 通、Edge TTS 挂」。
    这里显式用 certifi 的 cacert.pem 建 context（certifi 已在 spec 里打进包）。
    """
    import ssl
    try:
        import certifi  # type: ignore[import-not-found]
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        try:
            return ssl.create_default_context()
        except Exception:  # noqa: BLE001
            return None


_SSL_CTX = _ssl_context()

# 预设声线（README 列出的中文声线；风格在请求里另传）
EDGE_VOICES: list[dict] = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓（女·活泼）"},
    {"id": "zh-CN-XiaoyiNeural",   "name": "晓伊（女·柔和）"},
    {"id": "zh-CN-XiaochenNeural", "name": "晓辰（女·活泼）"},
    {"id": "zh-CN-XiaohanNeural",  "name": "晓涵（女·温暖）"},
    {"id": "zh-CN-XiaomengNeural", "name": "晓梦（女·可爱）"},
    {"id": "zh-CN-XiaomoNeural",   "name": "晓墨（女·成熟）"},
    {"id": "zh-CN-XiaoqiuNeural",  "name": "晓秋（女·温柔）"},
    {"id": "zh-CN-XiaoruiNeural",  "name": "晓睿（女·睿智）"},
    {"id": "zh-CN-XiaoshuangNeural", "name": "晓双（女·青春）"},
    {"id": "zh-CN-XiaoxuanNeural", "name": "晓萱（女·随和）"},
    {"id": "zh-CN-XiaoyanNeural",  "name": "晓颜（女·亲切）"},
    {"id": "zh-CN-XiaoyouNeural",  "name": "晓悠（女·童声）"},
    {"id": "zh-CN-XiaozhenNeural", "name": "晓甄（女·干练）"},
    {"id": "zh-CN-YunxiNeural",    "name": "云希（男·阳光）"},
    {"id": "zh-CN-YunyangNeural",  "name": "云扬（男·专业）"},
    {"id": "zh-CN-YunjianNeural",  "name": "云健（男·浑厚）"},
    {"id": "zh-CN-YunfengNeural",  "name": "云枫（男·成熟）"},
    {"id": "zh-CN-YunhaoNeural",   "name": "云皓（男·清朗）"},
    {"id": "zh-CN-YunxiaNeural",   "name": "云夏（男·清亮）"},
    {"id": "zh-CN-YunyeNeural",    "name": "云野（男·沉稳）"},
    {"id": "zh-CN-YunzeNeural",    "name": "云泽（男·磁性）"},
]

EDGE_STYLES: list[str] = [
    "general", "assistant", "chat", "customerservice", "newscast",
    "affectionate", "calm", "cheerful", "gentle", "lyrical", "serious",
]

DEFAULT_STYLE = "general"


def edge_tts_endpoint() -> str:
    """从 settings 读取用户配置的服务根地址；**默认空**，不预置任何公共 Worker 地址。

    环境变量优先（EDGE_TTS_ENDPOINT），其次 settings.json 的 edge_tts_endpoint。
    """
    import os

    env = (os.environ.get("EDGE_TTS_ENDPOINT") or "").strip()
    if env:
        return _normalize_endpoint_root(env)
    raw = str(studio_settings.load_settings().get("edge_tts_endpoint") or "").strip()
    return _normalize_endpoint_root(raw) if raw else ""


def _normalize_endpoint_root(raw: str) -> str:
    """把用户可能填的多种形态归一化为**服务根地址**（不含 /v1/audio/speech）。

    兼容：
        · 用户只填了服务根：'https://x.workers.dev' → 原样（去尾斜杠）
        · 用户误填完整接口：'https://x.workers.dev/v1/audio/speech/' → 去掉尾部
        · 大小写不敏感的匹配；避免拼接时产生 //v1 或重复 /v1/audio/speech
    """
    url = raw.strip().rstrip("/")
    # 去掉尾部 /v1/audio/speech 或 /v1/audio/speech/ 的重复段（如误填）
    url = re.sub(r"/+v1/+audio/+speech/*$", "", url, flags=re.IGNORECASE)
    return url


def _endpoint_url(endpoint_root: str) -> str:
    """把根地址安全拼成 {root}/v1/audio/speech，绝不产生 // 或双 /v1/…"""
    root = _normalize_endpoint_root(endpoint_root)
    if not root:
        return ""
    return root + "/v1/audio/speech"


class EdgeTtsEngine:
    """免费云端 Edge TTS 引擎（走 Cloudflare Worker）。key = "edge_tts"。

    参数由 SynthesisOptions.edge_* 字段传入（向后兼容：dots/fish/mock 收到时忽略）。
    """

    key = "edge_tts"

    #: 能力矩阵：**speed 由 Worker 原生消费**（native_speed=True）→ 后处理层必须
    #: 跳过 atempo，防双倍变速；pitch/style/voice 也是 Edge 独享；无 seed 概念、
    #: 无参考音频。max_pause 由通用后处理生效。
    capabilities: "EngineCapabilities" = EngineCapabilities(
        native_speed=True,
        supports_seed=False,
        supports_num_steps=False,
        supports_guidance=False,
        supports_edge_pitch=True,
        supports_edge_style=True,
        supports_voice_ref=False,
        detail="Edge TTS：speed/pitch/style/voice 原生；max_pause 由软件后处理",
    )

    def __init__(
        self,
        endpoint: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        default_voice: str = "zh-CN-XiaoxiaoNeural",
    ) -> None:
        self.endpoint_root = _normalize_endpoint_root(endpoint) if endpoint is not None else edge_tts_endpoint()
        self.timeout = timeout
        self.default_voice = default_voice

    # ------------------------------------------------------------------ 探测
    def probe(self, live_check: bool = False) -> EngineStatus:
        """默认只做**配置检查**——不做真实合成（避免 /api/state 频繁刷新时无故计费/耗流量）。
        live_check=True 时才发一次极短的合成请求，供"测试连接"按钮 / 环境自检明确调用。"""
        if not self.endpoint_root:
            return EngineStatus(
                key=self.key, available=False,
                detail="未配置 Edge TTS 服务地址。到「工具箱 · 免费 Edge TTS」填入你自建的 "
                "Cloudflare Worker 根地址（如 https://xxx.workers.dev），保存后再试。",
            )
        if not live_check:
            return EngineStatus(
                key=self.key, available=True,
                detail=f"Edge TTS 已配置：{self.endpoint_root}（未即时探活，点「测试连接」验证）",
            )
        try:
            self._request_mp3(_PROBE_SAMPLE_TEXT, self.default_voice,
                               speed=1.0, pitch=0, style=DEFAULT_STYLE)
        except EngineUnavailable as exc:
            return EngineStatus(key=self.key, available=False, detail=str(exc))
        return EngineStatus(key=self.key, available=True,
                             detail=f"Edge TTS 联通正常：{self.endpoint_root}")

    # -------------------------------------------------------------- 整篇合成
    def synthesize_full(
        self,
        text: str,
        voice: VoiceRef | None,      # 忽略（Edge 不支持参考音频）
        output: Path,
        options: SynthesisOptions | None = None,
    ) -> MasterAudio:
        if not self.endpoint_root:
            raise EngineUnavailable(self.probe().detail)
        if not text.strip():
            raise ValueError("整篇文案为空。")

        options = options or SynthesisOptions()
        voice_id = str(getattr(options, "edge_voice", "") or self.default_voice)
        pitch = int(getattr(options, "edge_pitch", 0) or 0)
        style = str(getattr(options, "edge_style", DEFAULT_STYLE) or DEFAULT_STYLE)
        speed = float(options.speed or 1.0)

        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        # 双临时策略：先写 .part.mp3 → ffmpeg 转到 .part.wav → 原子 replace 覆盖 output。
        # 这样任何失败都不会误删用户旧 output（比如重跑同一 chunk / 手动覆盖 master 的
        # 半成品保护），因为旧 output 只在 tmp_wav 完全生成后才被替换（Path.replace 原子）。
        tmp_mp3 = output.with_suffix(output.suffix + ".part.mp3")
        tmp_wav = output.with_suffix(output.suffix + ".part.wav")

        def _cleanup_temp() -> None:
            for p in (tmp_mp3, tmp_wav):
                try:
                    if p.exists():
                        p.unlink()
                except Exception:  # noqa: BLE001
                    pass

        try:
            mp3_bytes = self._request_mp3(text, voice_id, speed=speed, pitch=pitch, style=style)
            tmp_mp3.write_bytes(mp3_bytes)
            _convert_mp3_to_wav(tmp_mp3, tmp_wav,
                                 sample_rate=_WAV_SAMPLE_RATE,
                                 channels=_WAV_CHANNELS)
            # 原子替换 — 只有 tmp_wav 完整可用时才动 output；旧 output 若失败**不动**。
            tmp_wav.replace(output)
        except Exception:
            _cleanup_temp()
            raise
        finally:
            _cleanup_temp()

        seconds = wav_seconds(output)
        master = MasterAudio(
            path=output,
            engine=self.key,
            voice_id=voice_id,
            model=f"edge-worker:{self.endpoint_root}",
            seed=None,     # Edge TTS 无 seed 概念
            sample_rate=_WAV_SAMPLE_RATE,
            seconds=round(seconds, 3),
            options=options.to_payload(),
        )
        write_master_metadata(master)
        return master

    # --------------------------------------------------------------- HTTP
    def _request_mp3(self, text: str, voice: str, speed: float, pitch: int, style: str) -> bytes:
        """带指数退避的一次合成请求，返回 MP3 bytes。"""
        url = _endpoint_url(self.endpoint_root)
        # 只发 Worker 支持的字段（Anti-pattern：绝不发 model / response_format）
        payload: dict = {
            "input": text,
            "voice": voice,
            "speed": float(speed),
            "pitch": int(pitch),
            "style": style or DEFAULT_STYLE,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # R14-FIX-4c：**必须带浏览器 User-Agent**——否则 Cloudflare 边缘
        # 会把 Python-urllib/x.x 的默认 UA 识别为机器人并返回 HTTP 403
        # Error 1010（"Access denied. The site owner has blocked ..."）。
        # 与"测试连接"共用同一路径，测试也会因此失败。
        headers = {
            "Content-Type": "application/json",
            "Accept": "audio/mpeg, application/json",
            "User-Agent": _BROWSER_UA,
        }

        last_err: str = "未知错误"
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                _ctx = _SSL_CTX if url.lower().startswith("https") else None
                with urllib.request.urlopen(req, timeout=self.timeout, context=_ctx) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    data = resp.read()
                return _validate_audio_response(ctype, data)
            except EngineUnavailable as exc:
                # 校验层判定"应重试"的场景（如上游 429 被 Worker 包成 500 JSON）
                if getattr(exc, "retryable", False) and attempt < _MAX_RETRIES:
                    last_err = str(exc)
                    _sleep_backoff(attempt)
                    continue
                raise
            except urllib.error.HTTPError as exc:
                text_body = _peek_body(exc)
                # 上游/Worker 显式限流（真 429）或可恢复 5xx → 退避重试
                if exc.code == 429 or 500 <= exc.code < 600:
                    if attempt < _MAX_RETRIES:
                        last_err = f"HTTP {exc.code}：{text_body[:200]}"
                        _sleep_backoff(attempt)
                        continue
                raise EngineUnavailable(_http_error_hint(exc, text_body)) from exc
            except (TimeoutError, socket.timeout) as exc:
                last_err = f"请求超时（{int(self.timeout)}s）"
                if attempt < _MAX_RETRIES:
                    _sleep_backoff(attempt)
                    continue
                raise EngineUnavailable(
                    f"Edge TTS 服务 {int(self.timeout)} 秒无响应（{self.endpoint_root}）。"
                    f"可能服务器排队/网络阻塞——请稍后重试或换自建 Worker。") from exc
            except urllib.error.URLError as exc:
                # 网络瞬断（DNS/连接被拒/超时等）
                reason = getattr(exc, "reason", exc)
                last_err = f"网络异常：{reason}"
                if attempt < _MAX_RETRIES:
                    _sleep_backoff(attempt)
                    continue
                raise EngineUnavailable(
                    f"Edge TTS 连接失败：{self.endpoint_root}（{reason}）。"
                    f"请检查网络与 Worker 地址是否可达。") from exc
        raise EngineUnavailable(f"Edge TTS 连续 {_MAX_RETRIES} 次失败：{last_err}")


# ────────────────────────────────────────────────────────────── 校验/错误分类
def _validate_audio_response(content_type: str, data: bytes) -> bytes:
    """检查 HTTP 200 的响应：必须是 audio/* 且非空；若 Content-Type 是 application/json，
    多半是 Worker 把上游错误包装成 JSON —— 解析 error.code/message 分类为可重试/致命。"""
    if not data:
        raise EngineUnavailable("Edge TTS 返回空响应（0 字节）。Worker 侧可能异常，请稍后重试。")
    if "application/json" in content_type:
        try:
            parsed = json.loads(data.decode("utf-8", "ignore"))
        except Exception:
            parsed = {}
        err = parsed.get("error") if isinstance(parsed, dict) else None
        message = ""
        code = ""
        if isinstance(err, dict):
            message = str(err.get("message") or err.get("Message") or "").strip()
            code = str(err.get("code") or err.get("Code") or "").strip()
        elif isinstance(err, str):
            message = err
        text = f"[{code}] {message}" if code else message
        # Worker 常把上游 429 / 5xx 包成 HTTP 500 JSON，让上层重试
        retry_flags = ("429", "rate", "limit", "busy", "overload", "quota", "too many", "temporarily")
        if any(k in (text or "").lower() for k in retry_flags):
            exc = EngineUnavailable(f"Edge TTS 服务繁忙：{text or '未知'}")
            exc.retryable = True   # type: ignore[attr-defined]
            raise exc
        raise EngineUnavailable(f"Edge TTS 合成失败：{text or '未知（Worker JSON 无 error 字段）'}")
    if not (content_type.startswith("audio/") or content_type == "application/octet-stream"):
        raise EngineUnavailable(
            f"Edge TTS 返回非音频响应（Content-Type={content_type or '空'}，前 120 字节："
            f"{data[:120]!r}）。请确认地址填的是 Worker 服务根，而不是网页地址。")
    return data


def _http_error_hint(exc: urllib.error.HTTPError, body: str) -> str:
    code = exc.code
    if code == 401 or code == 403:
        return f"Edge TTS 鉴权失败（HTTP {code}）：{body[:200]}"
    if code == 404:
        return (f"Edge TTS 地址无该接口（HTTP 404，{body[:120]}）。"
                f"请确认服务根地址正确（软件会自动拼 /v1/audio/speech）。")
    if code == 429:
        return "Edge TTS 请求过多被限流（HTTP 429）。建议自建 Cloudflare Worker 独占配额。"
    if 500 <= code < 600:
        return f"Edge TTS 服务器错误（HTTP {code}）：{body[:200]}"
    return f"Edge TTS 请求失败（HTTP {code}）：{body[:200]}"


def _peek_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return ""


def _sleep_backoff(attempt: int) -> None:
    """attempt 从 1 起计；退避 1.5s / 3.0s / 6.0s ..."""
    time.sleep(_RETRY_BASE_DELAY * (2 ** (attempt - 1)))


# ────────────────────────────────────────────────────────────── MP3 → WAV
def _convert_mp3_to_wav(mp3: Path, wav: Path, sample_rate: int, channels: int) -> None:
    """用项目现有 ffmpeg 把 MP3 解码为**格式固定**的 PCM16 WAV，供 _concat_wavs 直接拼接。

    强制参数：`-ar {sample_rate} -ac {channels} -acodec pcm_s16le`；这样每段的
    params (nchannels/sampwidth/framerate) 必然一致，longform._concat_wavs 才不抛
    "音频段格式不一致，无法拼接"。

    走 integrated_workbench.proc.run_silent（Windows 不弹黑框，与 dots_local/whisper 一致）。"""
    ffmpeg = studio_settings.ffmpeg_tool("ffmpeg")
    if not ffmpeg or ffmpeg == "ffmpeg":
        # 尝试 which 兜底（settings.ffmpeg_tool 未解析到绝对路径时返回原名）
        import shutil
        which = shutil.which("ffmpeg")
        if which:
            ffmpeg = which
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(mp3),
        "-vn",
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-acodec", "pcm_s16le",
        str(wav),
    ]
    try:
        completed = run_silent(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise EngineUnavailable(
            f"未找到 ffmpeg（用来把 Edge TTS 的 MP3 转 WAV）。请把 ffmpeg.exe 放到软件目录或加入 PATH。"
        ) from exc
    if getattr(completed, "returncode", 1) != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-500:]
        raise EngineUnavailable(f"MP3 转 WAV 失败：{detail or '未知错误'}")
    if not wav.is_file() or wav.stat().st_size < 44:  # WAV 头至少 44 字节
        raise EngineUnavailable("MP3 转 WAV 输出为空/异常。")


# ────────────────────────────────────────────────────────────── 便捷查询
def voice_choices() -> list[dict]:
    """给前端下拉用的音色清单快照（{id,name}）。"""
    return [dict(v) for v in EDGE_VOICES]


def style_choices() -> list[str]:
    return list(EDGE_STYLES)
