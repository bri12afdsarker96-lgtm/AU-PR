"""fish-speech（Fish Audio S2 Pro）本地服务适配（M2b）：整篇文案 → 连贯 master.wav。

上游事实（https://github.com/fishaudio/fish-speech，2026-07 核实）：
    - 本地部署为 HTTP 服务（server 推理）；克隆用参考音频 10–30s，零微调；
    - 产品定位非商用，Fish Audio Research License 无碍。

保持简单：纯标准库 urllib 调本地服务，不引第三方 SDK。
    - probe：GET {base_url}/v1/health（失败再试 GET /，只判「服务在不在」）；
    - synthesize_full：POST {base_url}/v1/tts，JSON（text + 参考音频 base64 + 参数），
      期望返回 audio/wav 字节流，落盘为 master.wav。
    - 端点/字段与 dots 一样标注「本地验证轮收口」；不匹配时给明确报错与指引。
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

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


DEFAULT_BASE_URL = "http://127.0.0.1:8080"
EXPECTED_SAMPLE_RATE = 44100
_TIMEOUT_PROBE = 5
_TIMEOUT_SYNTH = 1800  # 整篇克隆可能很长，给足半小时


@dataclass
class FishLocalEngine:
    """fish-speech 本地服务引擎。服务由用户按官方文档启动，本软件只连不管。"""

    base_url: str = DEFAULT_BASE_URL
    max_chars: int = 160   # 单次合成字数上限；超长整篇由 longform 分块拼接，避免截断/漂移

    key: str = "fish_local"

    #: 能力矩阵：fish-speech 只发 seed / 参考音频，不发 speed / max_pause——两项由通用后处理生效。
    capabilities: "EngineCapabilities" = field(default_factory=lambda: EngineCapabilities(
        native_speed=False,
        supports_seed=True,
        supports_num_steps=False,
        supports_guidance=False,
        supports_voice_ref=True,
        detail="fish-speech：seed/参考音频原生；num_steps/guidance/speed/max_pause 由软件后处理",
    ))

    def probe(self) -> EngineStatus:
        for path in ("/v1/health", "/"):
            try:
                request = urllib.request.Request(self.base_url.rstrip("/") + path, method="GET")
                with urllib.request.urlopen(request, timeout=_TIMEOUT_PROBE) as response:
                    if 200 <= response.status < 500:
                        return EngineStatus(
                            key=self.key,
                            available=True,
                            detail=f"fish-speech 服务在线：{self.base_url}",
                        )
            except (urllib.error.URLError, OSError, ValueError):
                continue
        return EngineStatus(
            key=self.key,
            available=False,
            detail=f"fish-speech 服务未启动（{self.base_url}）。请按官方文档启动本地 server 后重试。",
        )

    def synthesize_full(
        self,
        text: str,
        voice: VoiceRef | None,
        output: Path,
        options: SynthesisOptions | None = None,
    ) -> MasterAudio:
        status = self.probe()
        if not status.available:
            raise EngineUnavailable(status.detail)
        if not text.strip():
            raise ValueError("整篇文案为空。")
        options = options or SynthesisOptions()
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        payload: dict = {"text": text, "format": "wav", "seed": options.seed}
        if voice is not None:
            reference: dict = {
                "audio": base64.b64encode(voice.reference_wav.read_bytes()).decode("ascii"),
            }
            if voice.transcript.strip():
                reference["text"] = voice.transcript.strip()
            payload["references"] = [reference]

        audio = self._post_tts(payload)
        output.write_bytes(audio)

        seconds = wav_seconds(output)
        master = MasterAudio(
            path=output,
            engine=self.key,
            voice_id=voice.voice_id if voice else "",
            model="fish-speech-s2-pro",
            seed=options.seed,
            sample_rate=EXPECTED_SAMPLE_RATE,
            seconds=round(seconds, 3),
            options=options.to_payload(),
        )
        write_master_metadata(master)
        return master

    def _post_tts(self, payload: dict) -> bytes:
        url = self.base_url.rstrip("/") + "/v1/tts"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SYNTH) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:500].decode("utf-8", errors="replace")
            raise EngineUnavailable(
                f"fish-speech /v1/tts 返回 {exc.code}：{detail}。"
                "请核对 fish-speech 版本与请求格式（本地验证轮收口）。"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise EngineUnavailable(f"fish-speech 请求失败：{exc}") from exc
        if not body or len(body) < 44:  # WAV 头都不够
            raise EngineUnavailable("fish-speech 返回空音频。请核对服务日志。")
        return body
