"""字幕层：自定义大小的烧录字幕 + SRT 导出。纯逻辑为主，可单元测试。

需求（用户 2026-07-22）：字幕可自定义大小；另配合剪映草稿导出（capcut_draft.py）。

设计（保持简单）：
    - 成片是一条连续时间轴，每行的 [start, end] 由计时表/帧窗口给出 →
      一遍 drawtext 链烧完全部字幕（每行一个 drawtext + enable=between(t,start,end)）；
    - 文本走 textfile（避免转义地狱），与内核 dub_sync 的字幕做法同风格；
    - 字体缺失时不烧、只记提示（与内核「字体缺失，未烧字幕」口径一致）；
    - 无论烧不烧，都导出 .srt——剪映可直接导入 SRT，作为草稿字幕的兜底路径。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


def normalize_color(value: str) -> str:
    """#RRGGBB → 0xRRGGBB（ffmpeg drawtext 记法）；颜色名（white/black…）原样放行。"""
    value = (value or "").strip() or "white"
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return "0x" + value[1:]
    return value


# 常见中文字体候选（Windows 优先，Linux 兜底；找不到则不烧字幕只出 SRT）。
DEFAULT_FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/msyh.ttc"),
    Path("C:/Windows/Fonts/msyhbd.ttc"),
    Path("C:/Windows/Fonts/simhei.ttf"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
]


# 字幕位置预设 → 垂直比例（底部为 None，走 bottom_margin 原逻辑）
SUBTITLE_POSITIONS: dict[str, float | None] = {
    "顶部": 0.06, "中上": 0.30, "中部": 0.48, "中下": 0.68, "底部": None,
}


@dataclass(frozen=True)
class SubtitleStyle:
    """字幕样式。font_size_px 为像素字号；position 为五档垂直位置（默认底部）。"""

    font_size_px: int = 64
    position: str = "底部"
    font_name: str = ""  # 字体库字体名/文件名；空 = 系统默认
    color: str = "white"
    border_width: int = 3
    border_color: str = "black"
    bottom_margin_px: int = 180
    max_chars_per_line: int = 18
    max_lines: int = 2


@dataclass(frozen=True)
class SubtitleEntry:
    """一条字幕：行文本 + 在成片时间轴上的窗口（来自帧量化后的行窗口）。"""

    index: int
    start: float
    end: float
    text: str


def entries_from_frame_windows(texts: list[str], frames: list[int], fps: float) -> list[SubtitleEntry]:
    """由每行帧数推出各行 [start, end) 窗口（与渲染帧窗口严格一致，天然音画同步）。"""
    if len(texts) != len(frames):
        raise ValueError(f"文本行数({len(texts)})与帧窗口数({len(frames)})不一致。")
    entries: list[SubtitleEntry] = []
    cursor = 0
    for i, (text, count) in enumerate(zip(texts, frames), start=1):
        start = cursor / fps
        cursor += count
        entries.append(SubtitleEntry(index=i, start=round(start, 3), end=round(cursor / fps, 3), text=text.strip()))
    return entries


# 标点逐句字幕（2026-07-25 用户定案）：一行文案按常用标点切成短句，每个短句在该行
# 时间窗内按字数占比拿到自己的显示窗——字幕按时间轴一句句出现，且单条不再过长。
_PHRASE_SPLIT = re.compile(r"[，。！？；：、,.!?;:…—]+")
PHRASE_MIN_SECONDS = 0.4     # 每个短句最短显示时长（太短一闪而过）；不够分则退回按占比


def split_line_phrases(text: str) -> list[str]:
    """按常用标点把一行切成短句并剥掉标点；空片丢弃；无标点则整行一句。"""
    phrases = [p.strip() for p in _PHRASE_SPLIT.split(str(text)) if p.strip()]
    return phrases or ([str(text).strip()] if str(text).strip() else [])


def entries_to_phrases(entries: list[SubtitleEntry]) -> list[SubtitleEntry]:
    """把逐行字幕条目展开为标点逐句条目：短句在行窗口内按字数占比排时，
    每句 ≥PHRASE_MIN_SECONDS（不够分则纯占比），末句对齐行尾（不破坏行边界=音画同步）。"""
    result: list[SubtitleEntry] = []
    counter = 0
    for entry in entries:
        phrases = split_line_phrases(entry.text)
        span = max(0.0, entry.end - entry.start)
        if not phrases or span <= 0:
            continue
        total_chars = sum(len(p) for p in phrases) or 1
        durations = [span * len(p) / total_chars for p in phrases]
        if len(phrases) * PHRASE_MIN_SECONDS <= span + 1e-9:  # 够分才抬底（+eps 防 6*0.4 浮点略大于 span）
            durations = [max(PHRASE_MIN_SECONDS, d) for d in durations]
            overflow = sum(durations) - span
            if overflow > 1e-9:
                # 抬底多出的时长，按各句「高于底线的富余」比例扣回：保证每句仍 ≥ 底线且总和 = span。
                # （旧版只从最长一句扣，扣不完时后续句会被 cursor 夹成过短甚至零时长——已修 2026-07-25）
                slack_total = sum(d - PHRASE_MIN_SECONDS for d in durations)
                if slack_total > 1e-9:  # guard 保证 slack_total ≥ overflow，故每句扣后仍 ≥ 底线
                    durations = [d - overflow * (d - PHRASE_MIN_SECONDS) / slack_total
                                 for d in durations]
        cursor = entry.start
        for i, (phrase, dur) in enumerate(zip(phrases, durations)):
            counter += 1
            end = entry.end if i == len(phrases) - 1 else min(entry.end, cursor + dur)
            result.append(SubtitleEntry(index=counter, start=round(cursor, 3),
                                        end=round(end, 3), text=phrase))
            cursor = end
    return result


def find_cjk_font(candidates: list[Path] | None = None) -> Path | None:
    for candidate in candidates if candidates is not None else DEFAULT_FONT_CANDIDATES:
        if candidate.exists():
            return candidate
    return None


def pick_font(font_name: str | None) -> Path | None:
    """样式指定字体优先（字体库解析），否则系统中文字体兜底。"""
    if font_name:
        from .fonts import resolve_font

        chosen = resolve_font(font_name)
        if chosen:
            return chosen
    return find_cjk_font()


def effective_chars_per_line(style: SubtitleStyle, canvas_width: int | None) -> int:
    """每行实际字数 = min(样式上限, 画布能放下的字数)。

    中文字符宽≈字号，行宽必须 ≤ 画布宽的 92%，否则 drawtext 居中后两侧溢出被裁。
    """
    if not canvas_width or canvas_width <= 0:
        return style.max_chars_per_line
    fits = max(4, int(canvas_width * 0.92 // max(1, style.font_size_px)))
    return min(style.max_chars_per_line, fits)


def wrap_subtitle_text(text: str, style: SubtitleStyle, canvas_width: int | None = None) -> str:
    """折行：**手动换行优先**（用户在文本框里回车的位置即分行位置，2026-07-24 需求），
    每个手动行超宽时再按画布自适应字数续折；无手动换行则整段自动折。
    总行数封顶 max_lines（超出截断，末行加省略号）。"""
    per_line = effective_chars_per_line(style, canvas_width)
    manual = [ln for ln in (seg.strip() for seg in str(text).splitlines()) if ln]
    if len(manual) > 1:
        lines: list[str] = []
        for segment in manual:
            compact = " ".join(segment.split())
            lines.extend(compact[o: o + per_line] for o in range(0, len(compact), per_line))
        if len(lines) > style.max_lines:
            lines = lines[: style.max_lines]
            lines[-1] = lines[-1][: max(1, per_line - 1)] + "…"
        return "\n".join(lines)
    compact = " ".join(str(text).strip().split())
    limit = per_line * style.max_lines
    if len(compact) > limit:
        compact = compact[: limit - 1] + "…"
    lines = [compact[offset : offset + per_line] for offset in range(0, len(compact), per_line)]
    return "\n".join(lines[: style.max_lines])


def drawtext_filters(
    entries: list[SubtitleEntry],
    style: SubtitleStyle,
    font: Path,
    textfile_dir: Path,
    canvas_width: int | None = None,
) -> list[str]:
    """为每条字幕生成一个 drawtext 滤镜（textfile + enable 窗口），按序返回。

    canvas_width 用于折行自适应（防止行宽超画布被裁）。
    """
    textfile_dir = Path(textfile_dir)
    textfile_dir.mkdir(parents=True, exist_ok=True)
    filters: list[str] = []
    for entry in entries:
        if not entry.text:
            continue
        text_path = textfile_dir / f"subtitle_{entry.index:03d}.txt"
        # 单条字幕永不折行：长度只由标点分句控制（2026-07-25 用户定案）。
        # 整句压成一行（清掉任何换行/多空白），wrap_subtitle_text 仅留给文本框 overlay。
        text_path.write_text(" ".join(str(entry.text).split()), encoding="utf-8")
        filters.append(
            "drawtext="
            f"fontfile='{_filter_path(font)}':"
            f"textfile='{_filter_path(text_path)}':"
            f"fontsize={int(style.font_size_px)}:"
            f"fontcolor={normalize_color(style.color)}:"
            f"borderw={int(style.border_width)}:bordercolor={normalize_color(style.border_color)}:"
            "x=(w-text_w)/2:"
            f"y={_subtitle_y(style)}:"
            f"enable='between(t,{entry.start:.3f},{entry.end:.3f})'"
        )
    return filters


def _subtitle_y(style: SubtitleStyle) -> str:
    ratio = SUBTITLE_POSITIONS.get(style.position, None)
    if ratio is None:
        return f"h-text_h-{int(style.bottom_margin_px)}"
    return f"(h-text_h)*{ratio:.3f}"


# ------------------------------------------------------------------ SRT
def write_srt(path: Path, entries: list[SubtitleEntry]) -> Path:
    """导出 SRT（UTF-8，带 BOM 以兼容剪映/播放器中文识别）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks: list[str] = []
    counter = 0
    for entry in entries:
        if not entry.text:
            continue
        counter += 1
        blocks.append(
            f"{counter}\n{srt_timestamp(entry.start)} --> {srt_timestamp(entry.end)}\n{entry.text}\n"
        )
    path.write_text("﻿" + "\n".join(blocks), encoding="utf-8")
    return path


def srt_timestamp(seconds: float) -> str:
    total_ms = round(max(0.0, seconds) * 1000)
    ms = total_ms % 1000
    total = total_ms // 1000
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d},{ms:03d}"


def _filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
