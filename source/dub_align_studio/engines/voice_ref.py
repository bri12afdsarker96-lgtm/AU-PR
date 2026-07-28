"""音色引用：两引擎共用的最小音色描述（音色库完整管理见 voice_library.py）。

独立小模块以避免 base ↔ voice_library 循环依赖。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VoiceRef:
    """一个音色 = 参考音频（+ 可选转写文本）。

    transcript：dots.tts 的 continuation clone 模式需要参考音频的转写文本才能达到
    最高说话人相似度；fish-speech 只需参考音频。留空则各引擎走各自的纯音色模式。
    """

    voice_id: str
    reference_wav: Path
    transcript: str = ""
    name: str = ""
