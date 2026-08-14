"""数据迭代：爆款配方反推 / 黄金3秒诊断 / A-B 测试。

把发布回流数据变成下一轮生产的依据。纯逻辑，可单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------- 爆款配方反推
@dataclass(frozen=True)
class RecipeStat:
    recipe: str  # 赛道/滤镜/标题模板等组合标识
    count: int
    avg_play: float
    avg_finish_rate: float


def top_recipes(records: list[dict], top_n: int = 5, min_samples: int = 1) -> list[RecipeStat]:
    """按配方聚合表现，找出高完播/高播放的组合，沉淀为可复用配方。

    records: {recipe, play_count, finish_rate}。recipe 可为 "赛道|滤镜|标题模板"。
    """
    buckets: dict[str, list[tuple[float, float]]] = {}
    for row in records:
        recipe = str(row.get("recipe", "")).strip()
        if not recipe:
            continue
        play = _num(row.get("play_count"))
        finish = _num(row.get("finish_rate"))
        buckets.setdefault(recipe, []).append((play, finish))

    stats: list[RecipeStat] = []
    for recipe, rows in buckets.items():
        if len(rows) < min_samples:
            continue
        n = len(rows)
        avg_play = round(sum(p for p, _ in rows) / n, 1)
        avg_finish = round(sum(f for _, f in rows) / n, 4)
        stats.append(RecipeStat(recipe, n, avg_play, avg_finish))
    # 先按完播率再按播放量排序
    stats.sort(key=lambda s: (s.avg_finish_rate, s.avg_play), reverse=True)
    return stats[:top_n]


# ---------------------------------------------------------------- 黄金3秒诊断
@dataclass(frozen=True)
class OpeningDiagnosis:
    level: str  # ok / warn / risk
    hooks: list[str]
    advice: str


_HOOK_WORDS = ["反转", "没想到", "居然", "秘密", "真相", "结果", "第一", "居然", "教你", "千万别", "警告"]


def diagnose_opening(first_line: str, first_seconds: float) -> OpeningDiagnosis:
    """黄金3秒诊断：开头是否有钩子、是否过长铺垫。启发式（可后续接真实留存数据）。"""
    hooks = [w for w in _HOOK_WORDS if w in first_line]
    if first_seconds > 4.0 and not hooks:
        return OpeningDiagnosis("risk", hooks, "开头超 4 秒且无钩子，建议前置冲突或悬念句")
    if not hooks:
        return OpeningDiagnosis("warn", hooks, "开头缺钩子词，建议加反转/悬念")
    return OpeningDiagnosis("ok", hooks, "开头有钩子")


# ---------------------------------------------------------------- A-B 测试
@dataclass(frozen=True)
class ABVariant:
    label: str
    title: str
    opening: str


@dataclass(frozen=True)
class ABResult:
    winner: str
    detail: str


def build_ab_pair(base_title: str, alt_title: str, base_open: str, alt_open: str) -> list[ABVariant]:
    return [ABVariant("A", base_title, base_open), ABVariant("B", alt_title, alt_open)]


def compare_ab(a_play: float, b_play: float, a_finish: float, b_finish: float) -> ABResult:
    """按完播率优先、播放量次之判定 A/B 胜者。"""
    a_score = (a_finish, a_play)
    b_score = (b_finish, b_play)
    if a_score == b_score:
        return ABResult("平局", "两版表现一致，可继续测")
    winner = "A" if a_score > b_score else "B"
    return ABResult(winner, f"A(完播{a_finish},播放{a_play:.0f}) vs B(完播{b_finish},播放{b_play:.0f})")


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
