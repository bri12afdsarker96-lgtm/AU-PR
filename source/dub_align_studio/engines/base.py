"""引擎接口与产物契约。纯逻辑，可单元测试。

边界（口径基准：docs/配音对齐工作室架构与路线_20260722.md）：
    - 引擎只做「篇级」：整篇文案 → 单条连贯 master.wav。不切行、不知道帧。
    - 产物契约：master.wav + master.json 元数据
      （engine / voice_id / model / seed / sample_rate / seconds）。
    - 非确定组件纪律：真引擎（dots/fish）只进 capability-check 探测；
      单元测试一律走 MockEngine。
"""

from __future__ import annotations

import json
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .voice_ref import VoiceRef


MASTER_METADATA_SUFFIX = ".json"  # master.wav → master.json


class EngineUnavailable(RuntimeError):
    """引擎不可用（未安装/无 GPU/服务未启动）。信息面向用户，须给出下一步指引。"""


@dataclass(frozen=True)
class SynthesisOptions:
    """整篇合成参数（对照 dots.tts 整合包 Settings 面板；fish 侧取交集，多余项忽略）。

    - num_steps / guidance_scale：扩散步数与引导强度（质量/速度权衡）；
    - speed：语速（1.0=原速；整合包为保音调无损变速）；
    - max_pause_seconds：停顿上限（秒）——标点处停顿超过此秒数就压缩，让整篇更连贯；
      0 = 不压缩。对「整篇克隆」尤其重要：停顿失控会拉长行时长、抬高 freeze 比例；
    - seed：固定以尽量可复现；
    - normalize_text：引擎侧文本规范化开关。
    """

    num_steps: int = 10
    guidance_scale: float = 1.2
    speed: float = 1.0
    max_pause_seconds: float = 0.0
    seed: int = 42
    normalize_text: bool = False
    # Edge TTS 独占参数（其他引擎收到时忽略，保持向后兼容）：
    # · edge_voice：预设声线 id，如 "zh-CN-XiaoxiaoNeural"；
    # · edge_pitch：音调 -50~+50；
    # · edge_style：语气风格（general / cheerful / newscast …），默认 general。
    edge_voice: str = ""
    edge_pitch: int = 0
    edge_style: str = "general"

    def to_payload(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EngineStatus:
    """探测结果：capability-check 风格，available + 人类可读细节。"""

    key: str
    available: bool
    detail: str


@dataclass(frozen=True)
class EngineCapabilities:
    """引擎能力矩阵——**统一声明**每个 SynthesisOptions 字段是否被引擎原生消费。

    调度层（longform.synthesize_long / rechunk / voice_try）根据这里决定：
      · native_speed=False → 后处理用 ffmpeg `atempo` 真改语速（不再"设置了没生效"）；
      · native_speed=True 且 speed!=1 → **不做**后处理（防 Edge TTS 被二次变速）；
      · supports_seed=False → 前端展示灰色提示"seed 不改变声音"（素材选择仍生效）；
      · 其它字段用于前端 tooltip 明确"哪些原生 / 哪些软件后处理"。

    max_pause_seconds 不进本表——它是**通用后处理**（所有引擎统一压缩长静音，
    与引擎无关），因此不用能力开关。"""

    native_speed: bool = False        # 是否在引擎请求里直接传 speed 并生效
    supports_seed: bool = False       # seed 是否真的影响声音（Edge/云端多数不支持）
    supports_num_steps: bool = False  # num_steps 是否被引擎消费
    supports_guidance: bool = False   # guidance_scale 是否被引擎消费
    supports_edge_pitch: bool = False # Edge 专属：音调
    supports_edge_style: bool = False # Edge 专属：语气风格
    supports_voice_ref: bool = True   # 是否使用参考音频（音色克隆）
    detail: str = ""                  # 人类可读描述，前端 tooltip 用


@dataclass(frozen=True)
class MasterAudio:
    """整篇克隆产物：连贯 master 音频 + 元数据（写入 master.json）。"""

    path: Path
    engine: str
    voice_id: str
    model: str
    seed: int | None
    sample_rate: int
    seconds: float
    options: dict | None = None  # SynthesisOptions.to_payload()，随元数据落盘

    def metadata_path(self) -> Path:
        return self.path.with_suffix(MASTER_METADATA_SUFFIX)


@runtime_checkable
class DubEngine(Protocol):
    """配音引擎接口：整篇合成 + 可用性探测。"""

    key: str

    def probe(self) -> EngineStatus:
        ...

    def synthesize_full(
        self,
        text: str,
        voice: VoiceRef | None,
        output: Path,
        options: SynthesisOptions | None = None,
    ) -> MasterAudio:
        ...


def write_master_metadata(master: MasterAudio) -> Path:
    """把 master 元数据落盘为 master.json（UTF-8，路径字段序列化为字符串）。"""
    payload = asdict(master)
    payload["path"] = str(master.path)
    target = master.metadata_path()
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def wav_seconds(path: Path) -> float:
    """读 WAV 实际时长（标准库，不依赖 ffprobe；引擎层自检用）。"""
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
    if rate <= 0:
        raise RuntimeError(f"WAV 采样率异常：{path}")
    return frames / rate
