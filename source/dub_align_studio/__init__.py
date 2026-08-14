"""水星配音对齐工作室（Dub Align Studio）——独立新软件，复用水星剪辑对齐内核。

设计锁定版（构思阶段结论）：
    整篇文案 → fish-speech 整篇克隆出连贯 master.wav（音色/韵律不漂移）
    → whisper 逐行量出各自真实时长 dur_i（变长，业务约束 ≥5s；whisper 只当尺子，不切音频）
    → 每句画面按 dur_i 裁剪/变速填满（复用 integrated_workbench.edit_compose 五档策略）
    → 一句一画面顺序拼接成整条无声视频
    → 整条 master.wav 直接叠加为唯一音轨（B 方案：整轨叠加，零拼接、零漂移）
    → 末段吸收帧舍入残差，收口保证「画面总长 = master 总长」。

本包当前落地 M1.5 骨架：
    - timing.MockAligner：假尺子（直接给定每行时长），用于验证 B 渲染几何；
      真实实现（whisper 强制对齐、fish-speech 整篇合成）在后续里程碑替换。
    - frames.quantize_to_frames：帧栅格收口（纯逻辑，可单测）。
    - render_b：真实 B 渲染路径（逐行裁/变速拼画面 + 叠整轨 + 帧收口断言）。
"""

from __future__ import annotations

from .timing import Aligner, LineTiming, MockAligner, LINE_DURATION_FLOOR, total_duration
from .frames import quantize_to_frames

__all__ = [
    "Aligner",
    "LineTiming",
    "MockAligner",
    "LINE_DURATION_FLOOR",
    "total_duration",
    "quantize_to_frames",
]
