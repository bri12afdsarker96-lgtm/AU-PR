"""视频进度条（烧录式播放进度条）：短剧风格顶部/底部横条——一条随播放时间线性增长的
已播进度图层 + 可自定义文字，进度在片尾正好走满（与视频播放严格同步）。纯逻辑，可单测。

需求（用户 2026-07-25，参考短剧顶部导流条截图；2026-07-26 修订为丝滑）：
    先铺一层**底色条带**（track，整条半透明），再叠一层**已播进度图层**（fill，半透明、
    覆盖整条高度）随视频进度**从左往右连续扫过**，片尾正好扫满；
    **自定义文字在进度条内**居中显示（可含「 | 」分段）。
    位置（顶部/底部）、字号、文字色、底色/不透明度、进度色/不透明度、条高、边距均可自定义。

实现（2026-07-26 由分段跳格改为**逐帧丝滑**）：
    · 底色条带 track：静态 drawbox（半透明整条高，铺在填充下方）。
    · 已播进度 fill：一张与画面同宽的半透明色块作为 filter_complex 源，用 **overlay 的 x
      随 t 从 -W 平移到 0**——可见填充区随播放**逐帧连续增长**，片尾满宽。
      为什么不用 drawbox 的 w= 表达式：部分 ffmpeg 版本 drawbox 几何里不认 t（实测进度条
      静止）；而 overlay 的 x/y 随 t 动是通用能力，各版本可靠、且逐帧平滑（丝滑）。
    · 文字 drawtext：叠在填充之上（最上层），保证任意画面可读。
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
    """顶部：**贴满上沿**（y=0，去掉上方留白，2026-07-26 用户定案）；底部：距底 margin。"""
    if bar.position == "底部":
        return max(0, int(canvas_height) - int(bar.margin_px) - strip_h)
    return 0


@dataclass(frozen=True)
class FillOverlay:
    """已播进度图层：一张与画面同宽的半透明色块，用 overlay 随 t 从左往右连续推进。

    在 filter_complex 里先声明为一个 color 源（source），再 overlay 到画面上（overlay_step）。
    x 从 -width 平移到 0：可见的填充区就是 [0, width*t/duration]，随播放逐帧丝滑增长，片尾满。
    """

    color: str        # 已规范化（0xRRGGBB 或颜色名）
    opacity: float
    width: int
    height: int
    y: int
    duration: float   # 进度扫动分母（=master 时长，片尾 t/duration 到 1）
    fps: int = 25     # 色块源帧率，须与成片一致，避免 overlay 跨帧率错位
    src_seconds: float = 0.0   # 色块源时长：须 ≥ 成片时长，否则 shortest=1 会砍掉末帧

    def source(self, out_label: str) -> str:
        """声明色块源（含 alpha），输出到 out_label（如 [pbfill]）。

        源做得比成片略长且同帧率：shortest=1 便按「成片」长度收口（成片才是较短的一路），
        绝不会因色块源恰好少一帧而把成片末帧砍掉（帧收口断言随即失败）。"""
        d = max(self.duration, self.src_seconds) + 1.0   # 比成片多 1s 富余
        return (f"color=c={self.color}@{self.opacity:.2f}:s={self.width}x{self.height}:"
                f"r={int(self.fps)}:d={d:.3f},format=rgba{out_label}")

    def overlay_step(self, base_label: str, fill_label: str, out_label: str) -> str:
        """把色块 overlay 到画面：x 随 t 从 -width→0；min(0\\,…) 防 t 略超时越界（片尾锁满）。"""
        expr = f"min(0\\, -{self.width}+{self.width}*t/{self.duration:.3f})"
        return f"{base_label}{fill_label}overlay=x='{expr}':y={self.y}:shortest=1{out_label}"


def progressbar_layers(
    bar: ProgressBar,
    font: Path | None,
    textfile_dir: Path,
    canvas_width: int,
    canvas_height: int,
    total_seconds: float,
    fps: int = 25,
    video_seconds: float = 0.0,
) -> tuple[list[str], "FillOverlay", list[str]]:
    """生成进度条三层（自下而上）：

        (below, fill, above)
        · below：铺在填充下方的线性滤镜 —— ① 底色条带 drawbox。
        · fill ：② 已播进度图层（overlay 随 t 丝滑推进，由 render_b 拼进 filter_complex）。
        · above：叠在填充上方的线性滤镜 —— ③ 自定义文字 drawtext（font 为 None 则为空）。

    total_seconds<=0 兜底按 0.1s 避免除零。font 为 None（找不到中文字体）时仅出条带+进度、
    跳过文字（降级不报错）。
    """
    textfile_dir = Path(textfile_dir)
    textfile_dir.mkdir(parents=True, exist_ok=True)
    strip_h = _strip_height(bar)
    strip_y = _strip_y(bar, canvas_height, strip_h)
    dur = max(0.1, float(total_seconds or 0.0))
    track_op = _clamp01(bar.track_opacity)
    fill_op = _clamp01(bar.fill_opacity)

    # ① 底色条带（半透明 banner，横贯画面，整条铺底；铺在填充下方）
    below = [
        f"drawbox=x=0:y={strip_y}:w=iw:h={strip_h}:"
        f"color={normalize_color(bar.track_color)}@{track_op:.2f}:t=fill"
    ]

    # ② 已播进度图层（半透明、覆盖整条高度，overlay 随 t 从左往右连续扫、片尾满）
    fill = FillOverlay(
        color=normalize_color(bar.fill_color), opacity=fill_op,
        width=int(canvas_width), height=strip_h, y=strip_y, duration=dur,
        fps=int(fps) or 25, src_seconds=max(dur, float(video_seconds or 0.0)),
    )

    # ③ 自定义文字（条带内垂直+水平居中；单行不折；叠在填充之上保证可读）
    above: list[str] = []
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
        above.append("drawtext=" + ":".join(parts))

    return below, fill, above


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
