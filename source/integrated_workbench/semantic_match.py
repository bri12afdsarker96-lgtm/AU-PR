"""语义匹配（半智能）：上传文本逐句识别语义，自动匹配画面剪辑方式与音效。

离线客户端不内置大模型，采用"关键词词库 + 句式模式 + 可扩展语义库 + 置信度 + 人工纠偏"
的半智能方案（确定性、可测试、可离线）：
  · 词库覆盖常见情绪用词；
  · 句式模式识别常见句型（疑问/感叹/钩子/转折/条件/强调/列举）；
  · 用户可通过 load_extra_lexicon 扩充语义库，或对单句做人工覆盖；
  · 每句返回主情绪 + 置信度 + 命中依据，供 UI 展示与纠偏。
接口稳定，日后可接 LLM/ASR 升级为语义级理解而不改调用方。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .edit_genre import get_preset


# —— 情绪词库（大幅扩充，覆盖常见用词）——
EMOTION_LEXICON: dict[str, list[str]] = {
    "反转高能": [
        "反转", "没想到", "居然", "竟然", "原来", "结果", "打脸", "逆袭", "高能", "炸裂",
        "万万没想到", "谁知", "不料", "岂料", "殊不知", "翻盘", "反杀", "神转折", "意想不到",
        "谁能想到", "出乎意料", "大跌眼镜", "剧情反转",
    ],
    "紧张悬疑": [
        "秘密", "真相", "证据", "线索", "危险", "警告", "阴谋", "背后", "隐藏", "悬疑", "诡异",
        "可疑", "蹊跷", "谜团", "内幕", "真凶", "幕后", "暗中", "诡计", "疑点", "调查", "追查",
        "潜伏", "卧底", "陷阱", "危机", "步步紧逼", "暗流",
    ],
    "温情感动": [
        "感动", "温暖", "陪伴", "守护", "眼泪", "母亲", "父亲", "妈妈", "爸爸", "家", "拥抱",
        "谢谢", "感恩", "牵挂", "思念", "亲情", "温情", "暖心", "泪目", "破防", "治愈", "陪着",
        "一直都在", "默默", "无私", "付出",
    ],
    "甜蜜浪漫": [
        "喜欢", "爱你", "心动", "告白", "表白", "浪漫", "甜", "宠", "撒糖", "牵手", "亲吻",
        "怦然心动", "脸红", "心跳", "情侣", "恋爱", "暗恋", "心里只有你", "偏爱", "独宠", "宠溺",
    ],
    "热血励志": [
        "加油", "努力", "坚持", "拼", "梦想", "奋斗", "热血", "不放弃", "逆袭", "翻身", "崛起",
        "王者归来", "扬眉吐气", "证明自己", "永不言弃", "拼搏", "燃", "斗志", "逆风翻盘",
    ],
    "搞笑欢乐": [
        "哈哈", "笑", "搞笑", "沙雕", "离谱", "尴尬", "皮", "梗", "整活", "笑死", "太逗", "憨",
        "社死", "翻车", "无语", "绝了", "笑不活了", "崩溃大笑", "名场面",
    ],
    "悲伤低落": [
        "难过", "伤心", "痛苦", "失去", "离开", "遗憾", "哭", "绝望", "孤独", "心碎", "崩溃",
        "泪流", "无助", "失望", "落寞", "凄凉", "痛哭", "撕心裂肺", "再也回不去",
    ],
    "愤怒冲突": [
        "愤怒", "生气", "吵架", "冲突", "对峙", "质问", "背叛", "报复", "撕破", "翻脸", "怒斥",
        "忍无可忍", "恨", "怒", "打脸", "对骂", "决裂", "反击", "算账", "讨回",
    ],
    "惊讶震撼": [
        "震撼", "惊呆", "不敢相信", "天啊", "史诗", "壮观", "第一次", "惊人", "震惊", "目瞪口呆",
        "颠覆", "炸了", "开挂", "逆天", "神操作", "太强了", "封神",
    ],
    "悬念钩子": [
        "千万别", "一定要", "记住", "注意", "别错过", "接下来", "更可怕的是", "重点来了",
        "最关键", "看到最后", "结局", "揭晓", "揭秘", "内含", "隐藏福利",
    ],
    "疑问互动": ["为什么", "怎么", "是不是", "有没有", "凭什么", "难道", "到底", "你知道吗", "你觉得", "评论区"],
    "强调金句": ["记住一句话", "人生", "成年人", "越", "从来没有", "真正", "最重要的是", "本质", "格局", "认知"],
}

# —— 句式模式（半智能：不止关键词，识别句型）——
# 模式 -> (情绪, 命中说明)
PATTERN_RULES: list[tuple[str, str, str]] = [
    (r"[?？]$", "疑问互动", "疑问句"),
    (r"^(为什么|凭什么|难道|到底|怎么)", "疑问互动", "疑问开头"),
    (r"(千万别|一定要|记住|别错过|看到最后)", "悬念钩子", "钩子句式"),
    (r"(如果|要是|假如).{0,12}(就|才|会|便)", "紧张悬疑", "条件假设句"),
    (r"(但是|然而|结果|没想到|谁知).", "反转高能", "转折句"),
    (r"(第一|第二|第三|首先|其次|最后)", "强调金句", "列举句式"),
    (r"(一定|务必|切记|绝对不能).", "悬念钩子", "强调祈使"),
    (r"[!！]{1}.{0,6}[!！]?$", "惊讶震撼", "感叹句"),
    (r"(越.{1,6}越)", "强调金句", "递进句式"),
]

# 情绪 -> 画面剪辑与音效建议（名称取自 edit_library）
EMOTION_STYLE: dict[str, dict[str, str]] = {
    "反转高能": {"effect": "冲出屏幕", "transition": "闪黑", "sound": "综艺开头-咚"},
    "紧张悬疑": {"effect": "暗角", "transition": "雾化", "sound": "悬疑嗡声"},
    "温情感动": {"effect": "精致嫩光", "transition": "叠化", "sound": "舒缓钢琴音效"},
    "甜蜜浪漫": {"effect": "精致嫩光", "transition": "圆形展开", "sound": "综艺灵光一闪"},
    "热血励志": {"effect": "推进放大", "transition": "放大冲入", "sound": "爆点鼓点"},
    "搞笑欢乐": {"effect": "轻微抖动", "transition": "像素化", "sound": "综艺灵光一闪"},
    "悲伤低落": {"effect": "电影感画幅", "transition": "黑场过渡", "sound": "影视悲伤节奏"},
    "愤怒冲突": {"effect": "色差故障", "transition": "闪黑", "sound": "咚 撞击轻响"},
    "惊讶震撼": {"effect": "推进放大", "transition": "放大冲入", "sound": "综艺惊讶"},
    "悬念钩子": {"effect": "推进放大", "transition": "闪黑", "sound": "叮"},
    "疑问互动": {"effect": "无", "transition": "叠化", "sound": "叮"},
    "强调金句": {"effect": "电影感画幅", "transition": "叠化", "sound": "叮"},
    "中性": {"effect": "无", "transition": "叠化", "sound": ""},
}

# 运行期可扩展词库（用户语义库）与人工覆盖
_EXTRA_LEXICON: dict[str, list[str]] = {}
_OVERRIDES: dict[str, str] = {}


@dataclass(frozen=True)
class SentenceMatch:
    index: int
    text: str
    emotion: str
    effect: str
    transition: str
    sound_effect: str
    pace_seconds: float
    confidence: float = 0.0
    basis: str = ""


def load_extra_lexicon(path: str | Path) -> dict[str, list[str]]:
    """加载用户语义库（JSON：{情绪: [词, ...]}），并入内存词库。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    added: dict[str, list[str]] = {}
    for emotion, words in (data or {}).items():
        if isinstance(words, list):
            cur = _EXTRA_LEXICON.setdefault(emotion, [])
            for w in words:
                if isinstance(w, str) and w.strip() and w not in cur:
                    cur.append(w.strip())
                    added.setdefault(emotion, []).append(w.strip())
    return added


def set_override(text: str, emotion: str) -> None:
    """人工纠偏：把某句固定判为某情绪（半智能的“人工”那一半）。"""
    if emotion not in EMOTION_STYLE:
        raise ValueError(f"未知情绪：{emotion}")
    _OVERRIDES[text.strip()] = emotion


def clear_runtime_lexicon() -> None:
    _EXTRA_LEXICON.clear()
    _OVERRIDES.clear()


def parse_script(text: str) -> list[str]:
    """一行一句（对应一个配音音频）。空行忽略，前后空白去除。"""
    return [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]


def analyze_sentence_detail(text: str) -> tuple[str, float, str]:
    """返回 (主情绪, 置信度 0~1, 命中依据)。半智能核心。"""
    stripped = text.strip()
    if stripped in _OVERRIDES:
        return _OVERRIDES[stripped], 1.0, "人工指定"

    scores: dict[str, float] = {}
    basis: dict[str, list[str]] = {}
    # 1) 关键词
    for lexicon in (EMOTION_LEXICON, _EXTRA_LEXICON):
        for emotion, words in lexicon.items():
            for word in words:
                if word and word in text:
                    scores[emotion] = scores.get(emotion, 0.0) + 1.0
                    basis.setdefault(emotion, []).append(word)
    # 2) 句式模式（半智能）
    for pattern, emotion, label in PATTERN_RULES:
        if re.search(pattern, stripped):
            scores[emotion] = scores.get(emotion, 0.0) + 0.8
            basis.setdefault(emotion, []).append(label)

    if not scores:
        return "中性", 0.0, "无命中"
    best = max(scores.items(), key=lambda kv: kv[1])
    total = sum(scores.values())
    confidence = round(best[1] / total, 2) if total else 0.0
    return best[0], confidence, "、".join(basis.get(best[0], [])[:4])


def analyze_sentence(text: str) -> str:
    """主情绪标签（向后兼容）。"""
    return analyze_sentence_detail(text)[0]


def match_sentence(index: int, text: str, genre: str) -> SentenceMatch:
    preset = get_preset(genre)
    emotion, confidence, basis = analyze_sentence_detail(text)
    style = EMOTION_STYLE.get(emotion, EMOTION_STYLE["中性"])
    effect = style["effect"] if style["effect"] and style["effect"] != "无" else (preset.effects[0] if preset.effects else "无")
    transition = style["transition"] or preset.transition
    sound = style["sound"]
    return SentenceMatch(
        index=index,
        text=text,
        emotion=emotion,
        effect=effect,
        transition=transition,
        sound_effect=sound,
        pace_seconds=preset.pace_seconds,
        confidence=confidence,
        basis=basis,
    )


def build_script_plan(text: str, genre: str) -> list[SentenceMatch]:
    return [match_sentence(i, line, genre) for i, line in enumerate(parse_script(text), start=1)]


def plan_summary(plan: list[SentenceMatch]) -> dict[str, int]:
    dist: dict[str, int] = {}
    for item in plan:
        dist[item.emotion] = dist.get(item.emotion, 0) + 1
    return dist


def low_confidence_sentences(plan: list[SentenceMatch], threshold: float = 0.34) -> list[SentenceMatch]:
    """置信度低或中性的句子，提示用户人工确认（半智能的人工纠偏入口）。"""
    return [m for m in plan if m.emotion == "中性" or m.confidence < threshold]
