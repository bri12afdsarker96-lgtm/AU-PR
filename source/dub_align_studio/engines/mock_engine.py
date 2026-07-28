"""Mock 引擎：确定性假 TTS，单测/验收专用（真 TTS 非确定，确定性断言只对 mock 做）。

行为：整篇文案按行数生成连贯正弦 WAV——第 i 行对应 durations[i] 秒、频率随行号变化
（人耳可辨行边界，便于人工抽检成片）。纯标准库（wave + math），不依赖 ffmpeg/GPU。
每行时长可显式给定（与 MockAligner 配对验证渲染几何），缺省每行 5.0s（业务下限）。
"""

from __future__ import annotations

import math
import struct
import wave
from dataclasses import dataclass, field
from pathlib import Path

from integrated_workbench.semantic_match import parse_script

from ..timing import LINE_DURATION_FLOOR
from .base import EngineStatus, MasterAudio, SynthesisOptions, write_master_metadata
from .voice_ref import VoiceRef


_SAMPLE_RATE = 44100
_BASE_FREQ = 220.0


@dataclass
class MockEngine:
    """确定性假引擎。durations 与文案行一一对应；缺省每行 5.0s。"""

    durations: list[float] = field(default_factory=list)
    sample_rate: int = _SAMPLE_RATE
    max_chars: int = 1_000_000   # mock 不分块（确定性测试口径不变）

    key: str = "mock"

    def probe(self) -> EngineStatus:
        return EngineStatus(key=self.key, available=True, detail="mock 引擎恒可用（确定性，测试专用）。")

    def synthesize_full(
        self,
        text: str,
        voice: VoiceRef | None,
        output: Path,
        options: SynthesisOptions | None = None,
    ) -> MasterAudio:
        lines = parse_script(text)
        if not lines:
            raise ValueError("整篇文案为空（没有有效脚本行）。")
        durations = self.durations or [LINE_DURATION_FLOOR] * len(lines)
        if len(durations) != len(lines):
            raise ValueError(f"MockEngine 时长数({len(durations)})与脚本行数({len(lines)})不一致。")

        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        # 让 mock 预览随「参数 + 音色」变化：不同 seed/步数/引导/语速/音色 → 不同基频，
        # 无 GPU 也能听出参数与音色是否生效（真引擎则由引擎自身按参数出声）。
        self._write_wav(output, durations, self._timbre_shift(voice, options))

        master = MasterAudio(
            path=output,
            engine=self.key,
            voice_id=voice.voice_id if voice else "",
            model="mock-sine",
            seed=0,
            sample_rate=self.sample_rate,
            seconds=round(sum(durations), 3),
            options=options.to_payload() if options else None,
        )
        write_master_metadata(master)
        return master

    @staticmethod
    def _timbre_shift(voice: VoiceRef | None, options: SynthesisOptions | None) -> float:
        """由 音色 + 合成参数 派生一个基频倍率（0.6~1.7），使不同设定的 mock 预览可辨。"""
        import hashlib

        vid = voice.voice_id if voice else "默认声线"
        opt = options.to_payload() if options else {}
        key = f"{vid}|{opt.get('seed')}|{opt.get('num_steps')}|{opt.get('guidance_scale')}|{opt.get('speed')}"
        h = int(hashlib.md5(key.encode("utf-8")).hexdigest()[:6], 16)  # noqa: S324 仅做可辨性映射
        return 0.6 + (h % 1100) / 1000.0  # 0.60 ~ 1.70

    def _write_wav(self, path: Path, durations: list[float], timbre: float = 1.0) -> None:
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(self.sample_rate)
            for index, seconds in enumerate(durations):
                freq = _BASE_FREQ * timbre * (1.0 + 0.25 * index)
                count = round(max(0.0, float(seconds)) * self.sample_rate)
                samples = bytearray()
                for n in range(count):
                    value = int(12000 * math.sin(2 * math.pi * freq * n / self.sample_rate))
                    samples += struct.pack("<h", value)
                handle.writeframes(bytes(samples))
