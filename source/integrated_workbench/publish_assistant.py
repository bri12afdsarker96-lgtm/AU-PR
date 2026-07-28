from __future__ import annotations

"""Guided publishing session logic.

Compliance boundary:
① 软件永不存储、读取、传输任何平台账号密码、Cookie、登录态；
② 软件永不对平台页面做任何自动化操作（不模拟点击、不注入脚本、不控制浏览器）——只做 `webbrowser.open` 打开页面，之后一切操作由人完成；
③ 上述两条写入专题文档"边界"节与模块头部注释，作为永久约束。
"""

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    DIR_CONFIG,
    DIR_COPYWRITING,
    DIR_READY,
    DIR_REVIEW,
    DIR_TITLE_TABLE,
    ProjectConfig,
    VIDEO_EXTENSIONS,
)
from .project import atomic_write_text, iter_media
from .publish_feedback import (
    ORIGIN_ASSISTANT,
    build_feedback_review,
    feedback_records_path,
    _append_feedback_records,
)


STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_MISSING = "missing"
ASSISTANT_STATUSES = {
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_MISSING,
}

PLATFORMS = ["抖音", "快手", "视频号", "小红书", "B站", "其他"]
DEFAULT_PLATFORM_ENTRANCES = {
    "抖音": "https://creator.douyin.com/creator-micro/content/upload",
    "快手": "https://cp.kuaishou.com/article/publish/video",
    "视频号": "https://channels.weixin.qq.com/platform/post/create",
    "小红书": "https://creator.xiaohongshu.com/publish/publish",
    "B站": "https://member.bilibili.com/platform/upload/video/frame",
}

SESSION_PREFIX = "发布会话_"
SESSION_SUFFIX = ".json"
ENTRANCE_OVERRIDE_NAME = "发布入口.json"
ACCOUNT_SLOT_COLUMN = "account_slot"
PLATFORM_COLUMN = "platform"


@dataclass
class AssistantItem:
    index: int
    account_slot: str
    platform: str
    file: Path
    caption: str
    status: str
    fail_reason: str = ""
    marked_at: str = ""
    source: str = ""
    batch_id: str = ""
    dedup_level: str = ""
    similarity_status: str = ""
    original_file: str = ""


@dataclass
class AssistantSession:
    session_id: str
    ready_dir: Path
    items: list[AssistantItem]
    cursor: int
    created_at: str
    updated_at: str


@dataclass
class AssistantFinishSummary:
    total: int
    published: int
    failed: int
    skipped: int
    missing: int
    pending: int
    review_json: Path
    review_md: Path


def create_session(config: ProjectConfig, ready_dir: str | Path | None = None) -> AssistantSession:
    selected_dir = _resolve_ready_dir(config, ready_dir)
    production_csv = selected_dir / "生产清单.csv"
    if not production_csv.exists():
        raise FileNotFoundError(f"产线目录缺少生产清单.csv：{selected_dir}")

    production_rows = _read_csv_rows(production_csv)
    platform_map = load_account_platform_map(config)
    caption_map = _load_caption_map(config, selected_dir, production_rows)
    items: list[AssistantItem] = []

    for index, row in enumerate(production_rows):
        raw_file = _first_value(row, ["file", "文件", "视频", "视频文件"])
        resolved_file, missing_reason = _resolve_video_file(config, selected_dir, raw_file)
        account_slot = _first_value(row, ["account_slot", "账号槽位", "账号位", "账号"])
        status = STATUS_PENDING if resolved_file.exists() else STATUS_MISSING
        fail_reason = "" if status == STATUS_PENDING else missing_reason
        caption = _caption_for(caption_map, resolved_file, raw_file)
        items.append(
            AssistantItem(
                index=index,
                account_slot=account_slot,
                platform=platform_map.get(account_slot, ""),
                file=resolved_file,
                caption=caption,
                status=status,
                fail_reason=fail_reason,
                source=str(row.get("source", "") or ""),
                batch_id=str(row.get("batch_id", "") or ""),
                dedup_level=str(row.get("dedup_level", "") or ""),
                similarity_status=str(row.get("similarity_status", "") or ""),
                original_file=str(row.get("original_file", "") or raw_file),
            )
        )

    now = _now_iso()
    session = AssistantSession(
        session_id=_new_session_id(selected_dir),
        ready_dir=selected_dir,
        items=items,
        cursor=_next_actionable_cursor(items),
        created_at=now,
        updated_at=now,
    )
    save_session(session)
    return session


def resume_session(config: ProjectConfig, ready_dir: str | Path) -> AssistantSession:
    selected_dir = _resolve_ready_dir(config, ready_dir)
    session = find_unfinished_session(config, selected_dir)
    if session is None:
        raise FileNotFoundError(f"没有未完成的发布会话：{selected_dir}")
    return session


def find_unfinished_session(config: ProjectConfig, ready_dir: str | Path | None = None) -> AssistantSession | None:
    selected_dir = _resolve_ready_dir(config, ready_dir)
    sessions = sorted(
        selected_dir.glob(f"{SESSION_PREFIX}*{SESSION_SUFFIX}"),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    for path in sessions:
        session = load_session(path)
        if any(_item_needs_attention(item) for item in session.items):
            session.cursor = _next_actionable_cursor(session.items)
            return session
    return None


def load_session(path: str | Path) -> AssistantSession:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    items = [_item_from_dict(item) for item in payload.get("items", [])]
    return AssistantSession(
        session_id=str(payload.get("session_id", "")),
        ready_dir=Path(str(payload.get("ready_dir", ""))),
        items=items,
        cursor=int(payload.get("cursor", _next_actionable_cursor(items)) or 0),
        created_at=str(payload.get("created_at", "")),
        updated_at=str(payload.get("updated_at", "")),
    )


def save_session(session: AssistantSession) -> Path:
    session.updated_at = _now_iso()
    path = session_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session.session_id,
        "ready_dir": str(session.ready_dir),
        "items": [_item_to_dict(item) for item in session.items],
        "cursor": session.cursor,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return path


def session_path(session: AssistantSession) -> Path:
    return session.ready_dir / f"{SESSION_PREFIX}{session.session_id}{SESSION_SUFFIX}"


def current_item(session: AssistantSession) -> AssistantItem | None:
    cursor = session.cursor
    if 0 <= cursor < len(session.items) and _item_needs_attention(session.items[cursor]):
        return session.items[cursor]
    cursor = _next_actionable_cursor(session.items)
    session.cursor = cursor
    if 0 <= cursor < len(session.items):
        return session.items[cursor]
    return None


def mark_item(
    config: ProjectConfig,
    session: AssistantSession,
    index: int,
    status: str,
    fail_reason: str = "",
) -> AssistantSession:
    if status not in ASSISTANT_STATUSES:
        raise ValueError(f"不支持的发布助手状态：{status}")
    item = _find_item(session, index)
    item.status = status
    item.fail_reason = fail_reason or item.fail_reason
    item.marked_at = _now_iso()
    if status in {STATUS_PUBLISHED, STATUS_FAILED}:
        _append_assistant_feedback_record(config, item, status, fail_reason)
    session.cursor = _next_actionable_cursor(session.items, start=index + 1)
    save_session(session)
    return session


def update_item(
    config: ProjectConfig,
    session: AssistantSession,
    index: int,
    platform: str | None = None,
    caption: str | None = None,
    remember_platform: bool = False,
) -> AssistantSession:
    item = _find_item(session, index)
    if platform is not None:
        item.platform = platform.strip()
    if caption is not None:
        item.caption = caption.strip()
    if remember_platform and item.platform and item.account_slot:
        remember_platform_mapping(config, item.account_slot, item.platform)
    save_session(session)
    return session


def finish_session(config: ProjectConfig, session: AssistantSession) -> AssistantFinishSummary:
    session.cursor = _next_actionable_cursor(session.items)
    save_session(session)
    review_json, review_md = build_feedback_review(config)
    counts = session_counts(session)
    return AssistantFinishSummary(
        total=counts["total"],
        published=counts[STATUS_PUBLISHED],
        failed=counts[STATUS_FAILED],
        skipped=counts[STATUS_SKIPPED],
        missing=counts[STATUS_MISSING],
        pending=counts[STATUS_PENDING],
        review_json=review_json,
        review_md=review_md,
    )


def session_counts(session: AssistantSession) -> dict[str, int]:
    counts = {status: 0 for status in ASSISTANT_STATUSES}
    for item in session.items:
        counts[item.status] = counts.get(item.status, 0) + 1
    counts["total"] = len(session.items)
    counts["actionable"] = sum(1 for item in session.items if _item_needs_attention(item))
    return counts


def load_platform_entrances(config: ProjectConfig) -> dict[str, str]:
    entrances = dict(DEFAULT_PLATFORM_ENTRANCES)
    override = config.root / DIR_CONFIG / ENTRANCE_OVERRIDE_NAME
    if override.exists():
        try:
            payload = json.loads(override.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            for key, value in payload.items():
                platform = str(key).strip()
                url = str(value or "").strip()
                if platform and url:
                    entrances[platform] = url
    return entrances


def platform_url(config: ProjectConfig, platform: str) -> str:
    text = str(platform or "").strip()
    if text == "其他":
        return ""
    return load_platform_entrances(config).get(text, "")


def load_account_platform_map(config: ProjectConfig) -> dict[str, str]:
    path = _accounts_path(config)
    if not path.exists():
        return {}
    rows, fieldnames = _read_csv_with_fields(path)
    platform_col = _pick_column(fieldnames, ["platform", "发布平台", "平台", "publish_platform"])
    slot_col = _pick_column(fieldnames, ["account_slot", "账号槽位", "账号位", "账号槽", "账号", "account_id", "nickname"])
    if not platform_col or not slot_col:
        return {}
    result: dict[str, str] = {}
    for row in rows:
        slot = str(row.get(slot_col, "") or "").strip()
        platform = str(row.get(platform_col, "") or "").strip()
        if slot and platform and slot not in result:
            result[slot] = platform
    return result


def remember_platform_mapping(config: ProjectConfig, account_slot: str, platform: str) -> Path:
    slot = str(account_slot or "").strip()
    platform_value = str(platform or "").strip()
    if not slot or not platform_value:
        raise ValueError("账号槽位和平台不能为空。")

    path = _accounts_path(config)
    rows, fieldnames = _read_csv_with_fields(path) if path.exists() else ([], [])
    original_fields = list(fieldnames)
    slot_col = _pick_column(fieldnames, [ACCOUNT_SLOT_COLUMN, "账号槽位", "账号位", "账号槽"]) or ACCOUNT_SLOT_COLUMN
    platform_col = _pick_column(fieldnames, [PLATFORM_COLUMN, "发布平台", "平台", "publish_platform"]) or PLATFORM_COLUMN
    for field in [slot_col, platform_col]:
        if field not in fieldnames:
            fieldnames.append(field)

    matched = next((row for row in rows if str(row.get(slot_col, "") or "").strip() == slot), None)
    exact = next(
        (
            row
            for row in rows
            if str(row.get(slot_col, "") or "").strip() == slot
            and str(row.get(platform_col, "") or "").strip() == platform_value
        ),
        None,
    )
    platform_column_is_new = platform_col not in original_fields
    if exact:
        pass
    elif matched and platform_column_is_new:
        matched[platform_col] = platform_value
    elif matched and str(matched.get(platform_col, "") or "").strip() == platform_value:
        pass
    else:
        row = {field: "" for field in fieldnames}
        row[slot_col] = slot
        row[platform_col] = platform_value
        rows.append(row)

    _write_csv_rows(path, rows, fieldnames)
    return path


def _append_assistant_feedback_record(
    config: ProjectConfig,
    item: AssistantItem,
    status: str,
    fail_reason: str,
) -> None:
    publish_status = STATUS_PUBLISHED if status == STATUS_PUBLISHED else STATUS_FAILED
    reason = fail_reason or item.fail_reason
    row = {
        "account_slot": item.account_slot,
        "file": item.file.name,
        "source": item.source,
        "batch_id": item.batch_id,
        "dedup_level": item.dedup_level,
        "similarity_status": item.similarity_status,
        "platform": item.platform,
        "publish_status": publish_status,
        "publish_time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "publish_url": "",
        "play_count": "",
        "duplicate_flag": "",
        "fail_reason": reason if status == STATUS_FAILED else "",
        "note": reason if status == STATUS_FAILED else "",
        "origin": ORIGIN_ASSISTANT,
        "import_id": datetime.now().strftime("%Y%m%d_%H%M%S"),
        "matched_path": str(item.file),
    }
    _append_feedback_records(feedback_records_path(config), [row])


def _resolve_ready_dir(config: ProjectConfig, ready_dir: str | Path | None) -> Path:
    if ready_dir:
        candidate = _resolve_path(config, ready_dir)
        if candidate.exists() and candidate.is_dir():
            return candidate
        raise FileNotFoundError(f"合格产线目录不存在：{ready_dir}")

    root = config.root / DIR_READY
    if not root.exists():
        raise FileNotFoundError("还没有产线目录，请先完成一键产线生成。")
    candidates = sorted(
        [path for path in root.iterdir() if path.is_dir() and path.name.startswith("产线_") and (path / "生产清单.csv").exists()],
        key=lambda path: (path.stat().st_mtime, path.name),
    )
    if not candidates:
        raise FileNotFoundError("还没有可引导发布的产线目录，请先完成一键产线生成。")
    return candidates[-1]


def _resolve_video_file(config: ProjectConfig, ready_dir: Path, file_value: str) -> tuple[Path, str]:
    text = str(file_value or "").strip()
    if not text:
        placeholder = ready_dir / "未填写成片文件.mp4"
        return placeholder, "生产清单未填写成片文件"
    candidate = Path(text)
    if candidate.is_absolute() and candidate.exists():
        return candidate, ""
    for direct in [ready_dir / candidate.name, config.root / DIR_READY / candidate.name]:
        if direct.exists():
            return direct, ""
    target_name = candidate.name.lower()
    for root in [ready_dir, config.root / DIR_READY]:
        for video in iter_media(root, VIDEO_EXTENSIONS):
            if video.name.lower() == target_name:
                return video, ""
    fallback = ready_dir / candidate.name
    return fallback, f"找不到成片文件：{file_value}"


def _load_caption_map(
    config: ProjectConfig,
    ready_dir: Path,
    production_rows: list[dict[str, str]],
) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in _caption_table_candidates(config, ready_dir, production_rows):
        for row in _read_csv_rows(path):
            caption = _caption_from_row(row)
            if not caption:
                continue
            for key in _caption_keys_from_row(row):
                result.setdefault(key, caption)
    return result


def _caption_table_candidates(
    config: ProjectConfig,
    ready_dir: Path,
    production_rows: list[dict[str, str]],
) -> list[Path]:
    roots: list[Path] = [ready_dir]
    for row in production_rows:
        batch_id = str(row.get("batch_id", "") or "").strip()
        if not batch_id:
            continue
        candidate = Path(batch_id)
        if candidate.exists() and candidate.is_dir():
            roots.append(candidate)
        else:
            roots.append(config.root / DIR_REVIEW / batch_id)
    roots.extend([config.root / DIR_COPYWRITING, config.root / DIR_TITLE_TABLE])

    keywords = ("标题", "文案", "title", "titles", "caption", "copywriting")
    seen: set[str] = set()
    result: list[Path] = []
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in sorted(root.glob("*.csv"), key=lambda item: item.name.lower()):
            name = path.name.lower()
            if not any(keyword.lower() in name for keyword in keywords):
                continue
            key = str(path.resolve())
            if key not in seen:
                seen.add(key)
                result.append(path)
    return result


def _caption_from_row(row: dict[str, str]) -> str:
    title = _first_value(row, ["title", "标题", "发布标题"])
    description = _first_value(row, ["caption", "文案", "发布文案", "description", "简介", "说明"])
    tags = _first_value(row, ["tags", "话题", "标签"])
    parts = []
    if title:
        parts.append(title)
    if description and description != title:
        parts.append(description)
    if tags:
        parts.append(tags)
    return "\n".join(parts).strip()


def _caption_keys_from_row(row: dict[str, str]) -> list[str]:
    values = [
        _first_value(row, ["video_path", "视频路径", "path"]),
        _first_value(row, ["file", "文件", "视频", "视频文件", "video"]),
        _first_value(row, ["video_name", "文件名"]),
        _first_value(row, ["output", "output_video", "packaged_video_path", "original_video_path"]),
    ]
    keys: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        path = Path(text)
        keys.extend([text.lower(), path.name.lower(), path.stem.lower()])
    return [key for key in dict.fromkeys(keys) if key]


def _caption_for(caption_map: dict[str, str], resolved_file: Path, raw_file: str) -> str:
    keys = [
        str(resolved_file).lower(),
        resolved_file.name.lower(),
        resolved_file.stem.lower(),
    ]
    raw = str(raw_file or "").strip()
    if raw:
        raw_path = Path(raw)
        keys.extend([raw.lower(), raw_path.name.lower(), raw_path.stem.lower()])
    for key in keys:
        caption = caption_map.get(key)
        if caption:
            return caption
    return resolved_file.stem if resolved_file.name else raw


def _find_item(session: AssistantSession, index: int) -> AssistantItem:
    for item in session.items:
        if item.index == index:
            return item
    raise IndexError(f"发布会话中没有第 {index} 条。")


def _next_actionable_cursor(items: list[AssistantItem], start: int = 0) -> int:
    if not items:
        return 0
    ordered = list(range(max(0, min(start, len(items))), len(items))) + list(range(0, max(0, min(start, len(items)))))
    for index in ordered:
        if _item_needs_attention(items[index]):
            return index
    return len(items)


def _item_needs_attention(item: AssistantItem) -> bool:
    if item.status == STATUS_PENDING:
        return True
    return item.status == STATUS_MISSING and not item.marked_at


def _item_to_dict(item: AssistantItem) -> dict[str, Any]:
    payload = asdict(item)
    payload["file"] = str(item.file)
    return payload


def _item_from_dict(payload: dict[str, Any]) -> AssistantItem:
    return AssistantItem(
        index=int(payload.get("index", 0) or 0),
        account_slot=str(payload.get("account_slot", "") or ""),
        platform=str(payload.get("platform", "") or ""),
        file=Path(str(payload.get("file", "") or "")),
        caption=str(payload.get("caption", "") or ""),
        status=str(payload.get("status", STATUS_PENDING) or STATUS_PENDING),
        fail_reason=str(payload.get("fail_reason", "") or ""),
        marked_at=str(payload.get("marked_at", "") or ""),
        source=str(payload.get("source", "") or ""),
        batch_id=str(payload.get("batch_id", "") or ""),
        dedup_level=str(payload.get("dedup_level", "") or ""),
        similarity_status=str(payload.get("similarity_status", "") or ""),
        original_file=str(payload.get("original_file", "") or ""),
    )


def _accounts_path(config: ProjectConfig) -> Path:
    primary = config.root / DIR_CONFIG / "publish_accounts.csv"
    secondary = config.root / "publish_accounts.csv"
    if primary.exists() or not secondary.exists():
        return primary
    return secondary


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{str(key): str(value or "") for key, value in row.items() if key is not None} for row in csv.DictReader(handle)]


def _read_csv_with_fields(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [{str(key): str(value or "") for key, value in row.items() if key is not None} for row in reader]
        return rows, list(reader.fieldnames or [])


def _write_csv_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _pick_column(fieldnames: list[str], candidates: list[str]) -> str:
    lower_map = {field.strip().lower(): field for field in fieldnames}
    for candidate in candidates:
        found = lower_map.get(candidate.strip().lower())
        if found:
            return found
    return ""


def _first_value(row: dict[str, str], keys: list[str]) -> str:
    lower_map = {str(key).strip().lower(): str(value or "").strip() for key, value in row.items()}
    for key in keys:
        value = lower_map.get(key.lower())
        if value:
            return value
    return ""


def _resolve_path(config: ProjectConfig, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if path.exists():
        return path
    return config.root / path


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_session_id(ready_dir: Path) -> str:
    base = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    session_id = base
    for index in range(1, 1000):
        if not (ready_dir / f"{SESSION_PREFIX}{session_id}{SESSION_SUFFIX}").exists():
            return session_id
        session_id = f"{base}_{index:03d}"
    raise FileExistsError(f"无法生成不重复发布会话文件名：{ready_dir}")
