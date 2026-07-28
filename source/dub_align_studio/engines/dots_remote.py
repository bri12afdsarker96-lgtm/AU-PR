"""云 dots.tts 远程引擎（混合方案）：把**唯一需要 GPU 的那一步**（推理）甩给云服务器，
其余全部留在本地（本地路径、分镜/输出目录、FFmpeg 渲染、去重、存档都不变）。

设计要点：
  · 与 DotsLocalEngine 实现同一个 DubEngine 协议（synthesize_full），下游一行都不用改；
  · 客户端只需 stdlib（urllib + wave），**不依赖 torch/soundfile/numpy**——这正是「不用本地显卡」
    的意义所在；
  · 可选引子「嗯。」在**客户端**注入、起音/尾部裁切也在**客户端**做（复用 dots_local 里那套
    纯 Python 切点函数），云端只当一个「无脑 GPU worker」：收文本+参考音频 → 吐原始 PCM16 wav；
  · 鉴权走 HTTP 头 X-API-Key；地址/Key 从设置或环境变量读取（见 settings.dots_remote_config）。

配置（二选一，环境变量优先）：
  · 设置面板：dots_remote_endpoint / dots_remote_api_key（存 ~/.dub_align_studio/settings.json）；
  · 环境变量：DOTS_REMOTE_ENDPOINT / DOTS_REMOTE_API_KEY。
"""

from __future__ import annotations

import array as _array
import base64
import getpass
import hashlib
import io
import json
import socket
import urllib.error
import urllib.request
import wave
from pathlib import Path

from .. import settings as studio_settings
from .base import (
    EngineStatus,
    EngineUnavailable,
    MasterAudio,
    SynthesisOptions,
    wav_seconds,
    write_master_metadata,
)
from .dots_local import (
    EXPECTED_SAMPLE_RATE,
    _ONSET_LEAD_IN,
    _leading_trim_index,
    _onset_cut_index,
    _tail_cut_index,
)
from .voice_ref import VoiceRef

DEFAULT_TIMEOUT_S = 300   # 单行合成上限。正常一行几秒~几十秒；300s 仍很宽裕，但云端卡死时
                          # 最多 5 分钟即报错，不再干等 15 分钟冻住整条队列。首行含模型加载见下方重试。
_HEALTH_TIMEOUT_S = 30


def _stable_user_id() -> str:
    """每台安装稳定唯一的用户标识（主机名+用户名 → 短哈希）。供云端多用户公平队列区分不同用户，
    即便大家共用同一把 API Key 也能各自排队限流、人人平等轮流。"""
    try:
        raw = f"{socket.gethostname()}|{getpass.getuser()}"
    except Exception:  # noqa: BLE001
        raw = "unknown"
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


_USER_ID = _stable_user_id()


class DotsRemoteEngine:
    """云 dots.tts：把推理发到远程 GPU，本地只做轻量收尾。key = "dots_remote"。"""

    key = "dots_remote"

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        seed: int = 42,
    ) -> None:
        cfg_ep, cfg_key = studio_settings.dots_remote_config()
        self.endpoint = (endpoint if endpoint is not None else cfg_ep).strip().rstrip("/")
        self.api_key = (api_key if api_key is not None else cfg_key).strip()
        self.timeout = timeout
        self.seed = seed

    # ------------------------------------------------------------------ 探测
    def probe(self) -> EngineStatus:
        if not self.endpoint:
            return EngineStatus(
                key=self.key,
                available=False,
                detail="未配置云配音服务器。请在设置里填「云配音服务器地址 + API Key」，"
                "或设环境变量 DOTS_REMOTE_ENDPOINT / DOTS_REMOTE_API_KEY。",
            )
        try:
            data = self._get_json("/health", timeout=_HEALTH_TIMEOUT_S)
        except EngineUnavailable as exc:
            return EngineStatus(key=self.key, available=False, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            return EngineStatus(
                key=self.key,
                available=False,
                detail=f"云端不可达：{self.endpoint}（{exc}）。请确认服务器已启动、地址/API Key 正确、网络可通。",
            )
        gpu = (data or {}).get("gpu") if isinstance(data, dict) else None
        model_ok = bool((data or {}).get("model_loaded", True)) if isinstance(data, dict) else True
        detail = f"云 dots.tts 可用：{self.endpoint}"
        if gpu:
            detail += f"，GPU：{gpu}"
        if not model_ok:
            detail += "（模型尚未加载，首行会先加载 1–3 分钟）"
        return EngineStatus(key=self.key, available=True, detail=detail)

    # -------------------------------------------------------------- 整篇合成
    def synthesize_full(
        self,
        text: str,
        voice: VoiceRef | None,
        output: Path,
        options: SynthesisOptions | None = None,
    ) -> MasterAudio:
        if not self.endpoint:
            raise EngineUnavailable(self.probe().detail)
        if not text.strip():
            raise ValueError("整篇文案为空。")
        options = options or SynthesisOptions(seed=self.seed)
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        lead_in = _ONSET_LEAD_IN if getattr(options, "dots_lead_in", False) else ""
        payload: dict = {
            # 引子按需在客户端注入（与本地引擎同一口径），云端只管按收到的文本生成
            "text": (lead_in + text) if lead_in else text,
            "num_steps": int(options.num_steps),
            "guidance_scale": float(options.guidance_scale),
            "seed": int(options.seed),
            "normalize_text": bool(options.normalize_text),
        }
        ref_raw = str(voice.reference_wav).strip() if voice is not None else ""
        if ref_raw and ref_raw != ".":
            ref_path = Path(voice.reference_wav)
            if not ref_path.is_file():
                raise EngineUnavailable(f"参考音频不存在：{ref_path}")
            payload["prompt_audio_b64"] = base64.b64encode(ref_path.read_bytes()).decode("ascii")
            payload["prompt_audio_name"] = ref_path.name
            if voice.transcript.strip():
                payload["prompt_text"] = voice.transcript.strip()  # 带转写：克隆相似度最高

        wav_bytes = self._post_for_wav("/synthesize", payload)
        trimmed = _trim_wav_bytes(wav_bytes, use_onset_trim=bool(lead_in))   # 复用与本地一致的纯 Python 起音/尾部裁切
        output.write_bytes(trimmed)

        seconds = wav_seconds(output)
        master = MasterAudio(
            path=output,
            engine=self.key,
            voice_id=voice.voice_id if voice else "",
            model=f"remote:{self.endpoint}",
            seed=options.seed,
            sample_rate=EXPECTED_SAMPLE_RATE,
            seconds=round(seconds, 3),
            options=options.to_payload(),
        )
        write_master_metadata(master)
        return master

    # --------------------------------------------------------------- HTTP
    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "audio/wav, application/json",
                   "X-User-Id": _USER_ID}   # 供云端公平队列区分用户
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _get_json(self, path: str, timeout: float) -> dict:
        req = urllib.request.Request(self.endpoint + path, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise EngineUnavailable(_http_error_hint(exc)) from exc

    def _post_for_wav(self, path: str, payload: dict) -> bytes:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.endpoint + path, data=body, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                ctype = resp.headers.get("Content-Type", "") or ""
                data = resp.read()
        except urllib.error.HTTPError as exc:
            raise EngineUnavailable(_http_error_hint(exc)) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise EngineUnavailable(
                f"云端 {int(self.timeout)} 秒无响应（{self.endpoint}）。多为服务器卡死/崩溃/正忙——"
                "请用「查看云端日志.bat」检查，必要时重跑「一键部署到云GPU.bat」重启服务。") from exc
        except Exception as exc:  # noqa: BLE001
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                raise EngineUnavailable(
                    f"云端 {int(self.timeout)} 秒无响应（{self.endpoint}）。多为服务器卡死/崩溃/正忙——"
                    "请用「查看云端日志.bat」检查，必要时重跑「一键部署到云GPU.bat」重启。") from exc
            raise EngineUnavailable(f"连接云配音服务器失败：{self.endpoint}（{exc}）。") from exc
        if "application/json" in ctype:  # 服务端把错误当 JSON 返回
            try:
                j = json.loads(data.decode("utf-8", "ignore"))
                detail = j.get("detail") or j.get("error") or j
            except Exception:  # noqa: BLE001
                detail = data[:300]
            raise EngineUnavailable(f"云端合成失败：{detail}")
        if not data:
            raise EngineUnavailable("云端返回空音频。请检查服务器日志。")
        return data


def _http_error_hint(exc: urllib.error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8", "ignore")[:300]
    except Exception:  # noqa: BLE001
        body = ""
    if exc.code in (401, 403):
        return f"云端鉴权失败（HTTP {exc.code}）。请检查 API Key 是否与服务器一致。"
    if exc.code == 404:
        return f"云端地址无该接口（HTTP 404）。请确认地址填到服务根（不含 /synthesize）。{body}"
    if exc.code == 429:   # 单用户排队已满（公平队列限流）
        return "云端排队已满（你的在队请求过多）。多为同时提交太多，请等前面的配音完成后再试。"
    return f"云端返回错误（HTTP {exc.code}）：{body}"


def _trim_wav_bytes(wav_bytes: bytes, use_onset_trim: bool = True) -> bytes:
    """对云端返回的 PCM16 WAV 做起音/尾部裁切（复用 dots_local 的纯 Python 切点函数）。
    非 PCM16 或解析失败时原样返回（best-effort，绝不为了裁切把音频弄坏）。切口做 5ms 线性淡入淡出防爆响。"""
    try:
        reader = wave.open(io.BytesIO(wav_bytes), "rb")
    except Exception:  # noqa: BLE001
        return wav_bytes
    with reader:
        nch = reader.getnchannels()
        sampwidth = reader.getsampwidth()
        sr = reader.getframerate()
        nframes = reader.getnframes()
        raw = reader.readframes(nframes)
    if sampwidth != 2 or nch < 1 or sr <= 0:
        return wav_bytes   # 只处理 PCM16（服务端固定输出 PCM16）
    samples = _array.array("h")
    samples.frombytes(raw)
    total = len(samples) // nch
    if total <= 0:
        return wav_bytes
    if nch == 1:
        mono = [abs(x) for x in samples]
    else:
        mono = [max(abs(samples[i * nch + c]) for c in range(nch)) for i in range(total)]
    peak = max(mono) if mono else 0
    start = _onset_cut_index(mono, sr, peak) if use_onset_trim else _leading_trim_index(mono, sr, peak)
    end = _tail_cut_index(mono, sr, peak)
    if not (0 <= start < end <= total):
        start, end = 0, total
    sliced = samples[start * nch:end * nch]
    frames = len(sliced) // nch
    fade = min(int(sr * 0.005), frames // 2)
    for i in range(fade):
        g = i / fade
        j = frames - 1 - i
        for c in range(nch):
            sliced[i * nch + c] = int(sliced[i * nch + c] * g)
            sliced[j * nch + c] = int(sliced[j * nch + c] * g)
    out = io.BytesIO()
    writer = wave.open(out, "wb")
    writer.setnchannels(nch)
    writer.setsampwidth(2)
    writer.setframerate(sr)
    writer.writeframes(sliced.tobytes())
    writer.close()
    return out.getvalue()
