"""动态水印（烧录式）：画面上一行可自定义文字，随时间在**四角之间随机跳动**。

需求（用户 2026-07-26）：
    · 水印文字可自定义编辑；
    · 「动态」= 在画面中浮动——随机在 左上/右上/左下/右下 四角切换显示；
    · 不影响字幕与进度条（水印画在**下层**，字幕/进度条盖在其上，不被水印遮挡）。

实现：用 drawtext 的 x/y 表达式随 t 切角——把时间轴按 interval 切片，第 k 片用一个
**按 seed 洗好牌**的角序列里的角（确定可复现，观感随机）。角序列编码成 (top_bit,right_bit)，
再用嵌套 if(eq(k,…)) 选出该片的左右/上下。表达式整体用单引号包住 → 里面的逗号是字面量、
不会被当滤镜分隔符（与字幕 enable='between(t,a,b)' 同机制）。纯逻辑，可单测。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from .overlays import normalize_color
from .subtitles import _filter_path


@dataclass(frozen=True)
class Watermark:
    """动态水印样式。text 为空则不烧。"""

    text: str = ""
    font_size_px: int = 36
    color: str = "#FFFFFF"
    opacity: float = 0.5
    interval_seconds: float = 3.0   # 每隔多少秒换一个角
    margin_px: int = 40             # 距画面边缘留白
    border_width: int = 2
    border_color: str = "black"
    font_name: str = ""
    seed: int = 42                  # 决定四角出现顺序（确定可复现）


# 四角编码：(top_bit, right_bit)。top=1 贴上、right=1 贴右。
_CORNERS = [(1, 0), (1, 1), (0, 0), (0, 1)]   # 左上 右上 左下 右下


def _clamp01(v) -> float:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


def corner_sequence(seed: int, cycles: int = 2) -> list[tuple[int, int]]:
    """按 seed 洗牌四角，重复 cycles 轮拼成一个更长的循环序列（减少相邻重复、观感更随机）。"""
    rng = random.Random(seed)
    seq: list[tuple[int, int]] = []
    for _ in range(max(1, cycles)):
        block = _CORNERS[:]
        rng.shuffle(block)
        seq.extend(block)
    return seq


def _pick(k_expr: str, bits: list[int]) -> str:
    """构造 bits[k] 的嵌套 if：if(eq(K,0),b0,if(eq(K,1),b1,…,blast))。"""
    expr = str(bits[-1])
    for k in range(len(bits) - 2, -1, -1):
        expr = f"if(eq({k_expr},{k}),{bits[k]},{expr})"
    return expr


def watermark_filters(
    wm: Watermark,
    font: Path | None,
    textfile_dir: Path,
    canvas_width: int,
    canvas_height: int,
    total_seconds: float,
) -> list[str]:
    """生成动态水印 drawtext（随 t 在四角跳动）。text 为空或字体缺失时返回 []（降级不报错）。"""
    text = " ".join(str(wm.text or "").split())
    if not text or font is None:
        return []
    textfile_dir = Path(textfile_dir)
    textfile_dir.mkdir(parents=True, exist_ok=True)
    text_path = textfile_dir / "watermark.txt"
    text_path.write_text(text, encoding="utf-8")

    interval = max(0.3, float(wm.interval_seconds or 3.0))
    m = int(wm.margin_px)
    seq = corner_sequence(int(wm.seed))
    n = len(seq)
    k_expr = f"mod(floor(t/{interval:.3f}),{n})"
    right_expr = _pick(k_expr, [c[1] for c in seq])
    top_expr = _pick(k_expr, [c[0] for c in seq])
    # top=1 → y=margin（贴上）；否则 h-text_h-margin（贴下）。right 同理。整体单引号保护逗号。
    x = f"'if({right_expr},w-text_w-{m},{m})'"
    y = f"'if({top_expr},{m},h-text_h-{m})'"

    parts = [
        f"fontfile='{_filter_path(Path(font))}'",
        f"textfile='{_filter_path(text_path)}'",
        f"fontsize={int(wm.font_size_px)}",
        f"fontcolor={normalize_color(wm.color)}@{_clamp01(wm.opacity):.2f}",
        f"x={x}",
        f"y={y}",
    ]
    if wm.border_width and wm.border_width > 0:
        parts.append(f"borderw={int(wm.border_width)}")
        parts.append(f"bordercolor={normalize_color(wm.border_color)}")
    return ["drawtext=" + ":".join(parts)]


def watermark_from_payload(payload: dict) -> Watermark | None:
    """从前端 payload 解析水印配置；未启用返回 None。"""
    data = (payload or {}).get("watermark") or {}
    if not isinstance(data, dict) or not data.get("enabled"):
        return None
    if not str(data.get("text") or "").strip():
        return None
    allowed = {k: data[k] for k in Watermark.__dataclass_fields__ if k in data}
    try:
        return Watermark(**allowed)
    except (TypeError, ValueError):
        return None
