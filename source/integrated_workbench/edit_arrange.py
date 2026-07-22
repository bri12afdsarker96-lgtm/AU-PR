"""编排引擎：把"逐句/逐镜计划"编排成带精确时间位置的成片方案，并编译成 FFmpeg 实施指令。

回答三个问题：每一镜用什么转场/特效/音效、在什么位置用、怎么实施。

编排规则（可按赛道微调）：
  开场镜：加钩子特效 + 开场音效（0s），首帧强留存；
  镜间转场：相邻镜之间放转场——情绪强的镜前用强转场（闪黑/放大冲入），否则用赛道默认转场；
  特效位置：普通镜用赛道默认轻特效（作用整镜）；情绪峰值镜用语义强特效（作用镜头前 1s 强调）；
  音效位置：转场点放转场音效；情绪峰值镜起点放重音音效；开场放开场音效；
  收尾镜：加淡出/黑场收束。
时间位置按各镜时长累计计算。纯逻辑，可单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .edit_genre import get_preset
from .edit_library import TRANSITIONS


# 情绪强度（决定是否用强转场/强特效/重音）
_STRONG_EMOTIONS = {"反转高能", "惊讶震撼", "愤怒冲突", "热血励志", "悬念钩子"}


@dataclass
class ArrangeShot:
    index: int
    emotion: str
    effect: str
    transition: str
    sound_effect: str
    duration: float


@dataclass(frozen=True)
class SoundCue:
    at_seconds: float
    name: str
    reason: str  # 开场 / 转场点 / 情绪重音


@dataclass(frozen=True)
class ArrangedShot:
    index: int
    start_at: float          # 该镜在成片中的起始时间
    duration: float
    effect: str
    effect_scope: str        # full=整镜 / intro=前1秒强调
    transition_in: str       # 进入本镜的转场名（首镜为空）
    transition_duration: float
    transition_offset: float  # 转场在时间线上的起点（xfade offset）


@dataclass
class Arrangement:
    shots: list[ArrangedShot] = field(default_factory=list)
    sound_cues: list[SoundCue] = field(default_factory=list)
    total_duration: float = 0.0
    opening_effect: str = ""
    closing: str = "淡入淡出"


def _adapt_from_sentence_matches(matches, durations: list[float]) -> list[ArrangeShot]:
    out: list[ArrangeShot] = []
    for i, m in enumerate(matches):
        dur = durations[i] if i < len(durations) else getattr(m, "pace_seconds", 3.0)
        out.append(ArrangeShot(getattr(m, "index", i + 1), m.emotion, m.effect, m.transition, m.sound_effect, float(dur)))
    return out


def _adapt_from_aligned(aligned) -> list[ArrangeShot]:
    return [
        ArrangeShot(p.index, p.emotion, p.effect, p.transition, p.sound_effect, float(p.shot_duration or p.audio_duration))
        for p in aligned
    ]


def arrange(shots: list[ArrangeShot], genre: str) -> Arrangement:
    """核心编排：为每一镜决定转场/特效/音效及其时间位置。"""
    preset = get_preset(genre)
    arr = Arrangement(opening_effect="推进放大")
    cursor = 0.0
    for i, shot in enumerate(shots):
        is_first = i == 0
        strong = shot.emotion in _STRONG_EMOTIONS

        # —— 转场（进入本镜）——
        if is_first:
            transition, t_dur, t_off = "", 0.0, 0.0
        else:
            transition = shot.transition if strong else preset.transition
            if transition not in TRANSITIONS:
                transition = preset.transition
            t_dur = TRANSITIONS.get(transition, ("dissolve", 0.5))[1]
            t_off = round(max(0.0, cursor - t_dur), 3)  # xfade 在切点前 t_dur 开始重叠

        # —— 特效及作用范围 ——
        if is_first:
            effect, scope = arr.opening_effect, "intro"
        elif strong:
            effect, scope = shot.effect, "intro"   # 情绪峰值：镜头前 1s 强调
        else:
            effect, scope = (preset.effects[0] if preset.effects else "无"), "full"

        arr.shots.append(
            ArrangedShot(shot.index, round(cursor, 3), round(shot.duration, 3), effect, scope, transition, t_dur, t_off)
        )

        # —— 音效位置 ——
        if is_first:
            arr.sound_cues.append(SoundCue(0.0, _opening_sound(preset), "开场"))
        else:
            # 转场点音效（在切点）
            arr.sound_cues.append(SoundCue(round(cursor, 3), "转场嗖声", "转场点"))
        if strong and shot.sound_effect:
            # 情绪重音落在镜头起点
            arr.sound_cues.append(SoundCue(round(cursor, 3), shot.sound_effect, "情绪重音"))

        cursor += shot.duration

    arr.total_duration = round(cursor, 3)
    if arr.shots:
        arr.closing = "黑场过渡"
    return arr


def _opening_sound(preset) -> str:
    theme_default = {
        "综艺": "综艺开头-咚", "影视": "悬疑嗡声", "节奏": "爆点鼓点",
        "氛围": "舒缓钢琴音效", "古风": "空灵水滴", "提示": "叮",
    }
    return theme_default.get(preset.sound_theme, "叮")


def arrange_script(matches, genre: str, durations: list[float] | None = None) -> Arrangement:
    """从语义逐句计划（SentenceMatch）编排。durations 缺省时用各句节奏时长。"""
    shots = _adapt_from_sentence_matches(matches, durations or [])
    return arrange(shots, genre)


def arrange_aligned(aligned, genre: str) -> Arrangement:
    """从配音对齐计划（dub_bridge.SentencePlan）编排——画面时长=配音时长。"""
    return arrange(_adapt_from_aligned(aligned), genre)


# ---------------------------------------------------------------- 如何实施：编译为 FFmpeg 指令
def to_render_spec(arr: Arrangement) -> dict:
    """把编排方案编译成可执行的渲染规格（转场→xfade，特效→滤镜，音效→adelay/amix）。

    返回结构化 spec，渲染层据此拼 filter_complex；也便于单测校验位置与手段。
    """
    transitions = []
    for shot in arr.shots:
        if shot.transition_in:
            xfade = TRANSITIONS.get(shot.transition_in, ("dissolve", 0.5))[0]
            transitions.append({
                "shot": shot.index,
                "filter": f"xfade=transition={xfade}:duration={shot.transition_duration:.2f}:offset={shot.transition_offset:.2f}",
            })
    effects = [
        {"shot": s.index, "scope": s.effect_scope, "effect": s.effect}
        for s in arr.shots if s.effect and s.effect != "无"
    ]
    sounds = [
        {"at": f"{c.at_seconds:.2f}", "name": c.name, "reason": c.reason,
         "filter": f"adelay={int(c.at_seconds * 1000)}|{int(c.at_seconds * 1000)}"}
        for c in arr.sound_cues if c.name
    ]
    return {
        "total_duration": arr.total_duration,
        "transitions": transitions,   # 每处 xfade 及其 offset（在哪切）
        "effects": effects,           # 每镜特效及作用范围
        "sound_mix": sounds,          # 每个音效及其 adelay 落点（在哪响）
        "closing": arr.closing,
    }
