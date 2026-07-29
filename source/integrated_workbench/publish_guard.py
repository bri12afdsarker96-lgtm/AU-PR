"""发布前把关：判重预检 + 敏感词扫描 + 一键体检清单 + 文件洗重。

面向短剧矩阵量产的"防限流/防封号"护城河：
- 判重预检：复用画面/音频指纹，发布前自查这批成片彼此相似度，超阈值拦截；
- 敏感词扫描：标题/字幕/文案扫广告法极限词、导流词等，防封号；
- 一键体检：把时长/分辨率/敏感词/判重聚合成红绿灯报告；
- 文件洗重：产出去元数据、改文件哈希的 FFmpeg 命令（平台先查文件级重复）。
纯逻辑，可单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass

from .audio_fingerprint import AudioFingerprintItem, audio_similarity
from .video_dedup import _hash_similarity


# ---------------------------------------------------------------- 敏感词
SENSITIVE_LEXICON: dict[str, list[str]] = {
    "广告法极限词": [
        "国家级", "最高级", "最佳", "第一", "唯一", "顶级", "极致", "100%",
        "绝对", "永久", "万能", "首选", "最优", "史无前例", "独一无二",
    ],
    "导流引流": [
        "加微信", "加vx", "加v", "微信号", "扫码", "私信", "二维码",
        "公众号", "点击链接", "关注领取", "加QQ", "私聊",
    ],
    "夸大功效": ["根治", "治愈", "药到病除", "包治", "无副作用", "彻底解决"],
}


@dataclass(frozen=True)
class SensitiveHit:
    word: str
    category: str
    position: int


def scan_sensitive_words(text: str, extra_words: list[str] | None = None) -> list[SensitiveHit]:
    """扫描文本里的敏感/违禁/导流词，返回命中列表（含位置）。"""
    hits: list[SensitiveHit] = []
    lexicon = dict(SENSITIVE_LEXICON)
    if extra_words:
        lexicon = {**lexicon, "自定义": list(extra_words)}
    for category, words in lexicon.items():
        for word in words:
            start = text.find(word)
            while start != -1:
                hits.append(SensitiveHit(word, category, start))
                start = text.find(word, start + 1)
    return sorted(hits, key=lambda h: h.position)


# ---------------------------------------------------------------- 判重预检
@dataclass(frozen=True)
class DedupRiskPair:
    left: str
    right: str
    picture_similarity: float
    audio_similarity: float
    level: str  # high / medium / low


@dataclass
class GuardItem:
    name: str
    picture_hashes: list[str]
    audio: AudioFingerprintItem | None = None


def dedup_precheck(
    items: list[GuardItem],
    picture_threshold: float = 0.92,
    audio_threshold: float = 0.97,
) -> list[DedupRiskPair]:
    """两两比对画面+音频相似度；任一超阈值即高风险，发布前应拆开或重做。"""
    pairs: list[DedupRiskPair] = []
    for i, left in enumerate(items):
        for right in items[i + 1 :]:
            pic = _hash_similarity(left.picture_hashes, right.picture_hashes)
            aud = -1.0
            if left.audio is not None and right.audio is not None:
                aud = audio_similarity(left.audio, right.audio)
            high = pic >= picture_threshold or (aud >= 0 and aud >= audio_threshold)
            medium = not high and (pic >= picture_threshold - 0.08 or (aud >= 0 and aud >= audio_threshold - 0.05))
            level = "high" if high else ("medium" if medium else "low")
            pairs.append(DedupRiskPair(left.name, right.name, round(pic, 4), round(aud, 4), level))
    return sorted(pairs, key=lambda p: p.picture_similarity, reverse=True)


def dedup_risk_summary(pairs: list[DedupRiskPair]) -> dict[str, int]:
    summary = {"high": 0, "medium": 0, "low": 0}
    for pair in pairs:
        summary[pair.level] = summary.get(pair.level, 0) + 1
    return summary


# ---------------------------------------------------------------- 一键体检
@dataclass(frozen=True)
class GuardCheck:
    item: str
    level: str  # ok / warn / error
    detail: str


@dataclass
class PublishEntry:
    name: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    title: str = ""
    subtitle: str = ""
    copywriting: str = ""


def prepublish_checklist(
    entries: list[PublishEntry],
    min_duration: float = 5.0,
    max_duration: float = 600.0,
    min_short_side: int = 720,
    dedup_pairs: list[DedupRiskPair] | None = None,
) -> list[GuardCheck]:
    """把每条成片的时长/分辨率/敏感词 + 整批判重风险聚合成红绿灯清单。"""
    checks: list[GuardCheck] = []
    for entry in entries:
        if entry.duration and not (min_duration <= entry.duration <= max_duration):
            checks.append(GuardCheck(f"{entry.name}/时长", "warn", f"{entry.duration:.1f}s 不在 {min_duration:.0f}~{max_duration:.0f}s"))
        if entry.width and entry.height:
            short = min(entry.width, entry.height)
            if short < min_short_side:
                checks.append(GuardCheck(f"{entry.name}/分辨率", "warn", f"短边 {short} < {min_short_side}"))
        for label, text in (("标题", entry.title), ("字幕", entry.subtitle), ("文案", entry.copywriting)):
            hits = scan_sensitive_words(text)
            if hits:
                words = "、".join(sorted({h.word for h in hits}))
                checks.append(GuardCheck(f"{entry.name}/{label}敏感词", "error", f"命中：{words}"))

    if dedup_pairs:
        summary = dedup_risk_summary(dedup_pairs)
        if summary["high"]:
            top = [p for p in dedup_pairs if p.level == "high"][:5]
            detail = "；".join(f"{p.left}↔{p.right}(画{p.picture_similarity:.2f})" for p in top)
            checks.append(GuardCheck("判重预检", "error", f"高相似 {summary['high']} 组，勿同账号发：{detail}"))
        elif summary["medium"]:
            checks.append(GuardCheck("判重预检", "warn", f"中相似 {summary['medium']} 组，建议错峰或再差异化"))
        else:
            checks.append(GuardCheck("判重预检", "ok", "未发现高相似成片"))

    if not checks:
        checks.append(GuardCheck("总体", "ok", "未发现问题"))
    return checks


def overall_level(checks: list[GuardCheck]) -> str:
    levels = {c.level for c in checks}
    if "error" in levels:
        return "error"
    if "warn" in levels:
        return "warn"
    return "ok"


# ---------------------------------------------------------------- 文件洗重
def strip_metadata_command(ffmpeg: str, source: str, output: str) -> list[str]:
    """产出去元数据 + 改文件哈希的 FFmpeg 命令（流拷贝，秒级）。

    平台常先查文件级重复（MD5/元数据），此步在画面差异化之外再抹掉文件指纹。
    """
    return [
        ffmpeg or "ffmpeg",
        "-y",
        "-i",
        source,
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c",
        "copy",
        "-fflags",
        "+bitexact",
        "-metadata",
        "comment=",
        output,
    ]
