"""待编辑队列（⑧）：一键成片烧完字幕的成片进队列，等待「文本框」二次精修。

需求（用户 2026-07-26）：
    · 生成成片（烧完字幕）后自动进队列；
    · **12 小时无后续操作**自动移除（按「最后活动时间」起算，不是生成时间）；
    · 点队列里任一「已烧字幕成片」可载入文本框继续编辑（select→touch 刷新时效）；
    · 持久化到本地（关软件重开仍在）。

纯逻辑 + JSON 持久化，可单测：store 路径与 now 均可注入。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

TTL_SECONDS = 12 * 3600   # 12 小时无操作自动清（用户 2026-07-26 定：3h→12h）

# ThreadingHTTPServer 下多请求并发（UI 轮询 GET 与成片完成 add 同时到）会 load→改→save 打架，
# 导致丢条目甚至整列被清空。用一把可重入锁把「读-改-写」串起来，_save 走临时文件+原子替换。
_LOCK = threading.RLock()

_STORE_OVERRIDE: Path | None = None   # 测试可覆盖，避免污染 ~/.dub_align_studio


def _default_store() -> Path:
    if _STORE_OVERRIDE is not None:
        return _STORE_OVERRIDE
    from . import settings as studio_settings

    return studio_settings.STUDIO_HOME / "edit_queue.json"


def _store_path(store: Path | None) -> Path:
    return Path(store) if store is not None else _default_store()


def _load(store: Path) -> list[dict]:
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else data
    return [x for x in (items or []) if isinstance(x, dict)]


def _save(store: Path, items: list[dict]) -> None:
    store.parent.mkdir(parents=True, exist_ok=True)
    # 原子写：先写同目录临时文件再 os.replace，避免并发读到被截断的半个文件而当成空队列
    tmp = store.with_suffix(store.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, store)


def _prune(items: list[dict], now: float, ttl: float) -> list[dict]:
    """丢弃：① 距最后活动超过 ttl 的；② 成片文件已不存在的（可再生中间产物被清理/删档）。"""
    kept: list[dict] = []
    for it in items:
        last = float(it.get("last_active") or it.get("created") or 0.0)
        if now - last > ttl:
            continue
        film = str(it.get("film_path") or "")
        if film and not Path(film).exists():
            continue
        kept.append(it)
    return kept


def add(title: str, film_path: str, output_dir: str,
        canvas: tuple[int, int] | list[int] | None = None,
        settings: dict | None = None,
        *, store: Path | None = None, now: float | None = None) -> dict:
    """把一条已烧字幕的成片加入待编辑队列（按 film_path 去重：已存在则刷新时效/标题）。

    settings：生成该成片时的烧录设置（字幕/进度条/水印/文本框/音频/比例），供文本框选中它时
    还原，保证「按当前样式重烧」与首次一致（用户 2026-07-26 B）。"""
    store = _store_path(store)
    now = time.time() if now is None else float(now)
    with _LOCK:
        items = _prune(_load(store), now, TTL_SECONDS)
        film_path = str(film_path)
        row = next((x for x in items if str(x.get("film_path")) == film_path), None)
        if row is None:
            row = {"id": uuid.uuid4().hex[:8], "film_path": film_path, "created": now}
            items.append(row)
        row["title"] = str(title or Path(output_dir).name or "成片")
        row["output_dir"] = str(output_dir)
        row["canvas"] = list(canvas) if canvas else row.get("canvas") or []
        if settings is not None:
            row["settings"] = settings
        row["last_active"] = now
        _save(store, items)
        return row


def list_active(*, store: Path | None = None, now: float | None = None,
                ttl: float = TTL_SECONDS) -> list[dict]:
    """返回未过期且成片仍在的队列项，最新活动在前。仅当清理确实删掉了东西才落盘，
    避免把只读的 GET 变成每次写盘（既省磨损，也缩小并发写窗口）。"""
    store = _store_path(store)
    now = time.time() if now is None else float(now)
    with _LOCK:
        raw = _load(store)
        items = _prune(raw, now, ttl)
        if len(items) != len(raw):
            _save(store, items)
        return sorted(items, key=lambda x: float(x.get("last_active") or 0.0), reverse=True)


def touch(item_id: str, *, store: Path | None = None, now: float | None = None) -> bool:
    """刷新某项的最后活动时间（用户点开它编辑时调用 → 重新计 12h）。顺带清过期，
    避免把已过期项写回复活。"""
    store = _store_path(store)
    now = time.time() if now is None else float(now)
    with _LOCK:
        items = _prune(_load(store), now, TTL_SECONDS)
        hit = False
        for it in items:
            if str(it.get("id")) == str(item_id):
                it["last_active"] = now
                hit = True
        _save(store, items)   # prune 后总要落盘（清掉过期），无论是否命中
        return hit


def remove(item_id: str, *, store: Path | None = None) -> bool:
    """从队列移除某项（不删成片文件本身，只出队）。"""
    store = _store_path(store)
    with _LOCK:
        items = _load(store)
        kept = [it for it in items if str(it.get("id")) != str(item_id)]
        if len(kept) != len(items):
            _save(store, kept)
            return True
        return False
