"""矩阵运营：账号画风隔离 / 错峰发布排期 / 账号健康度 / 成片命名规则。

面向"多账号批量分发、规避集中限流"的短剧矩阵场景。纯逻辑，可单元测试；
时间相关函数接收 base_time 以保持确定性。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .edit_genre import genre_names


# ---------------------------------------------------------------- 账号画风隔离
def assign_account_styles(accounts: list[str], genres: list[str] | None = None) -> dict[str, str]:
    """给每个账号分配不同赛道画风，避免一眼看出同团队批量搬运。

    账号数超过赛道数时循环复用，但相邻账号尽量错开。
    """
    pool = genres or genre_names()
    if not pool:
        return {account: "" for account in accounts}
    return {account: pool[i % len(pool)] for i, account in enumerate(accounts)}


# ---------------------------------------------------------------- 错峰发布排期
@dataclass(frozen=True)
class ScheduleSlot:
    account: str
    video: str
    publish_at: str  # ISO 时间


def build_publish_schedule(
    assignments: list[tuple[str, str]],
    base_time: datetime,
    interval_minutes: int = 30,
    per_account_gap_minutes: int = 180,
) -> list[ScheduleSlot]:
    """把 (账号, 成片) 排成错峰发布计划：全局按 interval 错开，同账号按更大间隔隔开。

    - interval_minutes：相邻任务的全局最小间隔（避免同一时刻大量相似内容）。
    - per_account_gap_minutes：同一账号两条之间的最小间隔（避免单号高频）。
    """
    slots: list[ScheduleSlot] = []
    last_account_time: dict[str, datetime] = {}
    cursor = base_time
    for index, (account, video) in enumerate(assignments):
        planned = cursor if index == 0 else cursor + timedelta(minutes=interval_minutes)
        cursor = planned
        earliest = last_account_time.get(account)
        if earliest is not None:
            gap_ok = earliest + timedelta(minutes=per_account_gap_minutes)
            if planned < gap_ok:
                planned = gap_ok
                cursor = planned
        last_account_time[account] = planned
        slots.append(ScheduleSlot(account, video, planned.isoformat(timespec="minutes")))
    return slots


# ---------------------------------------------------------------- 账号健康度
@dataclass(frozen=True)
class AccountHealth:
    account: str
    published: int
    zero_play: int
    avg_play: float
    level: str  # ok / warn / risk
    reason: str


def account_health(records: list[dict]) -> list[AccountHealth]:
    """按账号回流数据评估健康度。records: {account, play_count, published(bool)}。

    - risk：发布多但零播占比高（疑似限流）。
    - warn：均播偏低。
    - ok：正常。
    """
    grouped: dict[str, list[int]] = {}
    for row in records:
        account = str(row.get("account", "")).strip()
        if not account:
            continue
        try:
            play = int(row.get("play_count") or 0)
        except (TypeError, ValueError):
            play = 0
        grouped.setdefault(account, []).append(play)

    result: list[AccountHealth] = []
    for account, plays in grouped.items():
        published = len(plays)
        zero = sum(1 for p in plays if p == 0)
        avg = round(sum(plays) / published, 1) if published else 0.0
        if published >= 3 and zero / published >= 0.6:
            level, reason = "risk", f"零播 {zero}/{published}，疑似限流"
        elif avg < 100 and published:
            level, reason = "warn", f"均播 {avg} 偏低"
        else:
            level, reason = "ok", f"均播 {avg}"
        result.append(AccountHealth(account, published, zero, avg, level, reason))
    return sorted(result, key=lambda h: {"risk": 0, "warn": 1, "ok": 2}[h.level])


# ---------------------------------------------------------------- 成片命名规则
def build_output_name(
    template: str,
    *,
    account: str = "",
    genre: str = "",
    date: str = "",
    index: int = 1,
    title: str = "",
) -> str:
    """按命名模板生成成片文件名，避免手动重命名混乱。

    模板占位：{account} {genre} {date} {index} {index2} {index3} {title}
    """
    safe_title = _safe(title)
    values = {
        "account": _safe(account),
        "genre": _safe(genre),
        "date": date,
        "index": str(index),
        "index2": f"{index:02d}",
        "index3": f"{index:03d}",
        "title": safe_title,
    }
    try:
        name = template.format(**values)
    except (KeyError, IndexError, ValueError):
        name = f"{values['account']}_{values['genre']}_{values['date']}_{values['index3']}"
    return name.strip("_") or "成片"


def _safe(text: str) -> str:
    bad = '\\/:*?"<>|'
    return "".join(ch for ch in str(text) if ch not in bad).strip()
