"""L2 配音层：封闭双引擎（dots.tts / fish-speech）+ mock + edge_tts（免费云端预设音色）。

接口只有一个：DubEngine.synthesize_full(text, voice, output) → MasterAudio。
整篇文案一次克隆出连贯 master.wav（篇级粒度；引擎层不感知行/帧）。

edge_tts 不做克隆，仅走公开 Cloudflare Worker 请求预设音色；与 dots_remote 并列。
"""

from __future__ import annotations

from .base import (
    DubEngine,
    EngineCapabilities,
    EngineStatus,
    EngineUnavailable,
    MasterAudio,
    SynthesisOptions,
    write_master_metadata,
)
from .mock_engine import MockEngine
from .dots_local import DotsLocalEngine
from .dots_remote import DotsRemoteEngine
from .fish_local import FishLocalEngine
from .edge_tts import EdgeTtsEngine

__all__ = [
    "DubEngine",
    "EngineCapabilities",
    "EngineStatus",
    "EngineUnavailable",
    "MasterAudio",
    "SynthesisOptions",
    "MockEngine",
    "DotsLocalEngine",
    "DotsRemoteEngine",
    "FishLocalEngine",
    "EdgeTtsEngine",
    "write_master_metadata",
]
