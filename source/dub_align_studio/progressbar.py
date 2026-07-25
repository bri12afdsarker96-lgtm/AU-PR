"""视频进度条（烧录式播放进度条）：短剧风格顶部/底部横条——一条随播放时间线性增长的
已播进度线 + 可自定义文字，进度在片尾正好走满（与视频播放严格同步）。纯逻辑，可单测。

需求（用户 2026-07-25，参考短剧顶部导流条截图；2026-07-25 修订原理）：
    先铺一层**底色条带**（track，整条半透明），再叠一层**已播进度图层**（fill，半透明、
    覆盖整条高度）随视频进度**从左往右扫**（等价于对宽度打关键帧），片尾正好扫满；
    **自定义文字在进度条内**居中显示（可含「 | 」分段）。
    位置（顶部/底部）、字号、文字色、底色/不透明度、进度色/不透明度、条高、边距均可自定义。

实现：进度图层宽用 ffmpeg drawbox 的宽度表达式 `iw*min(1,t/时长)`——按帧时间戳 t 增长、
与画面同轴（不依赖播放器，等价于逐帧关键帧）。track/fill 用 drawbox（fill 半透明整条高、
底色透出），文字用 drawtext 叠在最上层（textfile 走同一转义）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .overlays import normalize_color
from .subtitles import _filter_path


PROGRESSBAR_POSITIONS = ["顶部", "底部"]


@dataclass(frozen=True)
class ProgressBar:
    """烧录进度条样式。text 为条带上的自定义文字（空则只画条带+进度线）。"""

    text: str = ""
    position: str = "顶部"           # 顶部 / 底部
    font_size_px: int = 40
    text_color: str = "#FFFFFF"
    track_color: str = "#000000"     # 底色条带
    track_opacity: float = 0.45
    fill_color: str = "#FFC24B"      # 已播进度图层（暗夜金，半透明覆盖整条高）
    fill_opacity: float = 0.38
    strip_height_px: int = 0         # 条带高；0=按字号推算
    margin_px: int = 24              # 距顶/底边距
    border_width: int = 2            # 文字描边（保证任意画面可读）
    border_color: str = "black"


def _clamp01(value) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _strip_height(bar: ProgressBar) -> int:
    return int(bar.strip_height_px) if bar.strip_height_px and bar.strip_height_px > 0 \
        else int(bar.font_size_px) + 24


def _strip_y(bar: ProgressBar, canvas_height: int, strip_h: int) -> int:
    """顶部：距顶 margin；底部：距底 margin。"""
    if bar.position == "底部":
        return max(0, int(canvas_height) - int(bar.margin_px) - strip_h)
    return int(bar.margin_px)


def progressbar_filters(
    bar: ProgressBar,
    font: Path | None,
    textfile_dir: Path,
    canvas_width: int,
    canvas_height: int,
    total_seconds: float,
) -> list[str]:
    """生成进度条烧录滤镜（自下而上叠放）：
       [① 底色条带 drawbox, ② 已播进度图层 drawbox(半透明整条高、时间驱动), (可选)③ 文字 drawtext]。

    进度图层宽 = iw*min(1,t/时长)——随帧时间戳从左往右扫、片尾满宽（total_seconds<=0 兜底按 0.1s，
    避免除零；此时进度在 0.1s 处即扫满）。
    font 为 None（找不到中文字体）时仅画条带+进度图层、跳过文字（降级不报错）。
    """
    textfile_dir = Path(textfile_dir)
    textfile_dir.mkdir(parents=True, exist_ok=True)
    strip_h = _strip_height(bar)
    strip_y = _strip_y(bar, canvas_height, strip_h)
    dur = max(0.1, float(total_seconds or 0.0))
    track_op = _clamp01(bar.track_opacity)
    fill_op = _clamp01(bar.fill_opacity)

    filters: list[str] = []
    # ① 底色条带（半透明 banner，横贯画面，整条铺底）
    filters.append(
        f"drawbox=x=0:y={strip_y}:w=iw:h={strip_h}:"
        f"color={normalize_color(bar.track_color)}@{track_op:.2f}:t=fill"
    )
    # ② 已播进度图层（半透明、覆盖整条高度，宽度随时间从左往右扫、片尾满）
    #    ——底色透出形成「已播/未播」对比；min 里的逗号需转义，避免被当滤镜分隔符
    filters.append(
        f"drawbox=x=0:y={strip_y}:w=iw*min(1\\,t/{dur:.3f}):h={strip_h}:"
        f"color={normalize_color(bar.fill_color)}@{fill_op:.2f}:t=fill"
    )
    # ③ 自定义文字（条带内垂直居中、水平居中；单行不折；叠在最上层保证可读）
    text = " ".join(str(bar.text or "").split())
    if text and font is not None:
        text_path = textfile_dir / "progressbar.txt"
        text_path.write_text(text, encoding="utf-8")
        parts = [
            f"fontfile='{_filter_path(Path(font))}'",
            f"textfile='{_filter_path(text_path)}'",
            f"fontsize={int(bar.font_size_px)}",
            f"fontcolor={normalize_color(bar.text_color)}",
            "x=(w-text_w)/2",
            f"y={strip_y}+({strip_h}-text_h)/2",
        ]
        if bar.border_width and bar.border_width > 0:
            parts.append(f"borderw={int(bar.border_width)}")
            parts.append(f"bordercolor={normalize_color(bar.border_color)}")
        filters.append("drawtext=" + ":".join(parts))
    return filters


def progressbar_from_payload(payload: dict) -> ProgressBar | None:
    """从前端 payload 解析进度条配置；未启用返回 None。"""
    data = (payload or {}).get("progressbar") or {}
    if not isinstance(data, dict) or not data.get("enabled"):
        return None
    allowed = {k: data[k] for k in ProgressBar.__dataclass_fields__ if k in data}
    try:
        return ProgressBar(**allowed)
    except (TypeError, ValueError):
        return None
