"""视频文本框（overlay）：成片画面上的自定义文字层。纯逻辑，可单元测试。

需求（用户 2026-07-22，参考小说推文成片）：在视频上加若干文本框——
顶部书名、中部旁白句、底部引导语等；**文本、位置、字号、颜色均可自定义**，
可选描边/半透明背景框/显示时间窗（缺省全程显示）。

与字幕（subtitles.py）的关系：字幕跟随行窗口自动排布；文本框是用户手摆的
固定文字层，二者叠加互不影响（文本框绘制在字幕之上）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .subtitles import SubtitleStyle, _filter_path, wrap_subtitle_text


# 位置预设 → 垂直位置比例（0=顶 1=底；y = (h-text_h)*ratio）
POSITION_PRESETS: dict[str, float] = {
    "顶部": 0.06,
    "中上": 0.30,
    "中部": 0.48,
    "中下": 0.68,
    "底部": 0.86,
}


@dataclass(frozen=True)
class OverlayText:
    """一个文本框。start/end 为显示窗口（秒）；end=0 表示直到片尾。"""

    text: str
    position: str = "中部"          # POSITION_PRESETS 键，或留空用 y_ratio
    y_ratio: float = -1.0            # 自定义垂直比例(0~1)；<0 表示用 position 预设
    font_size_px: int = 72
    color: str = "#FFFFFF"
    border_width: int = 3
    border_color: str = "black"
    boxed: bool = False              # 半透明背景框（参考图底部引导语样式）
    box_color: str = "black"
    box_opacity: float = 0.5
    start: float = 0.0
    end: float = 0.0
    font_name: str = ""  # 字体库字体；空 = 跟随全局/系统
    max_chars_per_line: int = 12
    max_lines: int = 3

    def resolved_ratio(self) -> float:
        if 0.0 <= self.y_ratio <= 1.0:
            return self.y_ratio
        return POSITION_PRESETS.get(self.position, POSITION_PRESETS["中部"])


def normalize_color(value: str) -> str:
    """#RRGGBB → 0xRRGGBB（ffmpeg 记法）；颜色名原样放行。"""
    value = (value or "").strip() or "white"
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return "0x" + value[1:]
    return value


def overlay_filters(
    overlays: list[OverlayText],
    font: Path,
    textfile_dir: Path,
    canvas_width: int,
    total_seconds: float,
) -> list[str]:
    """为每个文本框生成 drawtext 滤镜。文本走 textfile；折行按画布宽自适应。"""
    textfile_dir = Path(textfile_dir)
    textfile_dir.mkdir(parents=True, exist_ok=True)
    filters: list[str] = []
    for order, overlay in enumerate(overlays, start=1):
        if not overlay.text.strip():
            continue
        wrap_style = SubtitleStyle(
            font_size_px=overlay.font_size_px,
            max_chars_per_line=overlay.max_chars_per_line,
            max_lines=overlay.max_lines,
        )
        text_path = textfile_dir / f"overlay_{order:02d}.txt"
        text_path.write_text(wrap_subtitle_text(overlay.text, wrap_style, canvas_width), encoding="utf-8")

        chosen = font
        if overlay.font_name:
            from .subtitles import pick_font

            chosen = pick_font(overlay.font_name) or font
        parts = [
            f"fontfile='{_filter_path(chosen)}'",
            f"textfile='{_filter_path(text_path)}'",
            f"fontsize={int(overlay.font_size_px)}",
            f"fontcolor={normalize_color(overlay.color)}",
            "x=(w-text_w)/2",
            f"y=(h-text_h)*{overlay.resolved_ratio():.3f}",
        ]
        if overlay.border_width > 0:
            parts.append(f"borderw={int(overlay.border_width)}")
            parts.append(f"bordercolor={normalize_color(overlay.border_color)}")
        if overlay.boxed:
            opacity = min(1.0, max(0.0, overlay.box_opacity))
            parts.append("box=1")
            parts.append(f"boxcolor={normalize_color(overlay.box_color)}@{opacity:.2f}")
            parts.append(f"boxborderw={max(8, int(overlay.font_size_px * 0.25))}")
        start = max(0.0, float(overlay.start))
        end = float(overlay.end) if overlay.end and overlay.end > start else total_seconds
        if start > 0.0 or end < total_seconds - 1e-6:
            parts.append(f"enable='between(t,{start:.3f},{end:.3f})'")
        filters.append("drawtext=" + ":".join(parts))
    return filters


# ------------------------------------------------------------------ 存取（GUI 用）
def overlays_to_dicts(overlays: list[OverlayText]) -> list[dict]:
    from dataclasses import asdict

    return [asdict(item) for item in overlays]


def overlays_from_dicts(payload: list[dict]) -> list[OverlayText]:
    result: list[OverlayText] = []
    for row in payload or []:
        if not isinstance(row, dict) or not str(row.get("text", "")).strip():
            continue
        allowed = {key: row[key] for key in OverlayText.__dataclass_fields__ if key in row}
        result.append(OverlayText(**allowed))
    return result
