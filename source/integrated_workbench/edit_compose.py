"""合成层：画面比例、音画同步（按配音长度裁剪/变速）、多轨工程模型。

面向"每句配音单独一个音频文件 → 逐句音画同步"的短剧场景：
- 画面比例可自定义；
- 视频按对应配音音频的时长，裁剪多余画面或变速匹配（用户可选）；
- 多轨模型不限制视频/音频/字幕轨的层数。
纯逻辑，可单元测试；FFmpeg 片段以字符串形式产出，交由渲染层执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


# ------------------------------------------------------------------ 画面比例
# 名称 -> (宽比, 高比)；None 表示保持原始
ASPECT_RATIOS: dict[str, tuple[int, int] | None] = {
    "原始": None,
    "9:16 竖屏": (9, 16),
    "16:9 横屏": (16, 9),
    "1:1 方形": (1, 1),
    "4:3": (4, 3),
    "3:4": (3, 4),
    "2.35:1 电影": (235, 100),
    "21:9 宽银幕": (21, 9),
}


def aspect_canvas(name: str, base: int = 1080) -> tuple[int, int]:
    """按比例名返回画布像素尺寸（偶数）。竖屏以宽为 base，横屏以高为 base。"""
    ratio = ASPECT_RATIOS.get(name)
    if not ratio:
        return (base, base)
    w, h = ratio
    if w <= h:  # 竖屏/方形：固定宽
        width = base
        height = round(base * h / w)
    else:  # 横屏：固定高
        height = base
        width = round(base * w / h)
    return (_even(width), _even(height))


def aspect_fit_filter(target_w: int, target_h: int, mode: str = "contain") -> str:
    """把任意画面适配到目标画布：
    contain=完整显示+黑边（scale 到内接 + pad）；cover=铺满+裁切（scale 到外接 + crop）。
    """
    if mode == "cover":
        return (
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h}"
        )
    return (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
        f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def _even(value: int) -> int:
    value = int(round(value))
    return value if value % 2 == 0 else value + 1


def distinct_intro_offsets(count: int, step: float = 0.08, max_offset: float = 0.6) -> list[float]:
    """为一批成片分配互不相同的开场微裁量，保证首帧/封面帧各不相同（平台常抽首帧判重）。"""
    if count <= 0:
        return []
    return [round(min(max_offset, i * step), 3) for i in range(count)]


def enforce_distinct_intro(current_trim: float, index: int, count: int,
                           step: float = 0.08, max_offset: float = 0.6) -> float:
    """opt-in 首帧差异化：把第 index 份的开场裁量抬到该份专属下限，保证互不相同。

    只增不减（取 max），已有较大 intro_trim 的变体不受影响；index 越界返回原值。
    纯函数，便于单测；默认不调用即与冻结基线一致。
    """
    offsets = distinct_intro_offsets(count, step, max_offset)
    floor = offsets[index] if 0 <= index < len(offsets) else 0.0
    return round(max(float(current_trim or 0.0), floor), 3)


# ------------------------------------------------------------------ 音画同步
AUDIO_MATCH_MODES = ("裁剪多余画面", "变速匹配", "不处理")


@dataclass(frozen=True)
class SyncPlan:
    mode: str
    audio_duration: float
    video_duration: float
    target_duration: float
    video_speed: float  # 作用于视频 setpts 的倍率；1.0 表示不变速
    trim_to: float | None  # 需要裁剪时的目标时长；None 表示不裁剪
    note: str

    def video_filter(self) -> str:
        """返回作用于视频流的时间处理片段（变速/裁剪由渲染层配合 -t 使用）。"""
        parts = []
        if abs(self.video_speed - 1.0) > 1e-3:
            parts.append(f"setpts=PTS/{self.video_speed:.4f}")
        return ",".join(parts)


def match_video_to_audio(video_duration: float, audio_duration: float, mode: str) -> SyncPlan:
    """按配音音频时长对齐视频。音频保持自然速度，只调整视频，保证音画同步。

    - 裁剪多余画面：视频比音频长→裁到音频时长；视频比音频短→退化为放慢视频补足。
    - 变速匹配：调整视频速度使其时长等于音频时长（speed = 视频/音频）。
    - 不处理：原样。
    """
    audio_duration = max(0.01, float(audio_duration))
    video_duration = max(0.01, float(video_duration))

    if mode == "不处理":
        return SyncPlan(mode, audio_duration, video_duration, video_duration, 1.0, None, "保持原样")

    if mode == "变速匹配":
        speed = video_duration / audio_duration  # >1 视频加速，<1 放慢
        return SyncPlan(
            mode, audio_duration, video_duration, audio_duration, round(speed, 4), None,
            f"视频变速 {speed:.2f}× 对齐配音 {audio_duration:.2f}s",
        )

    # 裁剪多余画面
    if video_duration >= audio_duration:
        return SyncPlan(
            mode, audio_duration, video_duration, audio_duration, 1.0, round(audio_duration, 4),
            f"裁掉多余 {video_duration - audio_duration:.2f}s 对齐配音",
        )
    # 视频短于配音：无法裁长，退化为放慢补足
    speed = video_duration / audio_duration
    return SyncPlan(
        mode, audio_duration, video_duration, audio_duration, round(speed, 4), None,
        f"画面短于配音，放慢 {speed:.2f}× 补足到 {audio_duration:.2f}s",
    )


def atempo_chain(speed: float) -> str:
    """把任意变速拆成合法 atempo 链（单个 atempo 仅支持 0.5~2.0）。仅音频用；音画同步默认不用。"""
    speed = max(0.25, min(4.0, float(speed)))
    factors: list[float] = []
    remaining = speed
    while remaining > 2.0:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        factors.append(0.5)
        remaining /= 0.5
    factors.append(round(remaining, 4))
    return ",".join(f"atempo={f}" for f in factors)


# ------------------------------------------------------------------ 多轨工程模型
@dataclass
class Clip:
    source: str
    start: float = 0.0
    duration: float = 0.0
    filter_name: str = "原片"
    effects: list[str] = field(default_factory=list)
    transition_in: str = ""
    text: str = ""


@dataclass
class Track:
    kind: str  # video / audio / subtitle / effect / sticker
    clips: list[Clip] = field(default_factory=list)
    name: str = ""


@dataclass
class EditProject:
    """多轨工程：每种轨道可有任意条（不限层数）。"""
    width: int = 1080
    height: int = 1920
    fps: float = 30.0
    aspect_name: str = "9:16 竖屏"
    audio_match_mode: str = "裁剪多余画面"
    tracks: list[Track] = field(default_factory=list)

    def add_track(self, kind: str, name: str = "") -> Track:
        track = Track(kind=kind, name=name or f"{kind}{self.track_count(kind) + 1}")
        self.tracks.append(track)
        return track

    def track_count(self, kind: str) -> int:
        return sum(1 for t in self.tracks if t.kind == kind)

    def to_dict(self) -> dict:
        return asdict(self)
