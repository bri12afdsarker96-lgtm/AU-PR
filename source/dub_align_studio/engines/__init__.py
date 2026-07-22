"""L2 配音层：封闭双引擎（dots.tts / fish-speech）+ mock。

接口只有一个：DubEngine.synthesize_full(text, voice, output) → MasterAudio。
整篇文案一次克隆出连贯 master.wav（篇级粒度；引擎层不感知行/帧）。
"""

from __future__ import annotations

from .base import (
    DubEngine,
    EngineStatus,
    EngineUnavailable,
    MasterAudio,
    SynthesisOptions,
    write_master_metadata,
)
from .mock_engine import MockEngine
from .dots_local import DotsLocalEngine
from .fish_local import FishLocalEngine

__all__ = [
    "DubEngine",
    "EngineStatus",
    "EngineUnavailable",
    "MasterAudio",
    "SynthesisOptions",
    "MockEngine",
    "DotsLocalEngine",
    "FishLocalEngine",
    "write_master_metadata",
]
