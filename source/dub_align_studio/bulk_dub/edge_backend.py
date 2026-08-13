"""把 Edge TTS 引擎适配成 Scheduler 需要的 TtsBackend。"""

from __future__ import annotations

import time
from pathlib import Path

from ..engines import SynthesisOptions
from ..engines.edge_tts import EdgeTtsEngine
from ..engines.base import EngineUnavailable

from .scheduler import TtsBackend, TtsHttpError


class EdgeTtsBackend(TtsBackend):
    def __init__(self, engine: EdgeTtsEngine | None = None) -> None:
        self.engine = engine or EdgeTtsEngine()

    def synthesize(self, *, text: str, voice_id: str, speed: float,
                   pitch: int, style: str, output_wav: Path) -> float:
        options = SynthesisOptions(
            speed=float(speed),
            edge_voice=voice_id,
            edge_pitch=int(pitch),
            edge_style=style or "general",
        )
        try:
            master = self.engine.synthesize_full(
                text=text, voice=None, output=output_wav, options=options,
            )
        except EngineUnavailable as exc:
            retryable = getattr(exc, "retryable", False)
            status = 503 if retryable else 502
            if not retryable and ("未配置" in str(exc) or "地址" in str(exc)):
                raise TtsHttpError(400, str(exc)) from exc
            raise TtsHttpError(status, str(exc), retry_after=None) from exc
        return float(master.seconds)


class MockTtsBackend(TtsBackend):
    """确定性 mock：生成指定时长的静音 WAV，用于压测和单元测试。"""

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
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * frame_count)
