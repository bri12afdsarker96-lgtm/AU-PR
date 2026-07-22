from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import (
    DIR_CONFIG,
    DIR_COPYWRITING,
    DIR_COVER,
    DIR_DONE,
    DIR_PACKAGED_VIDEO,
    DIR_QUEUE,
    DIR_REPORTS,
    DIR_PUBLISH_STATUS_RESULT,
    DIR_SOCIAL_CLIPS,
    DIR_VISUAL_PACKAGE,
    IMAGE_EXTENSIONS,
    ProjectConfig,
    PublishAccount,
    QueueItem,
)
from .publish_feedback import append_queue_status_feedback_records
from .project import iter_media, log_line, ready_videos
from .title_engine import generate_copywriting, generate_titles, load_title_map


QUEUE_FIELDS = [
    "group",
    "account_id",
    "nickname",
    "video_path",
    "source_type",
    "original_video_path",
    "title",
    "cover_path",
    "description",
    "tags",
    "scheduled_time",
    "chrome_port",
    "platform",
    "platform_url",
    "external_task_id",
    "status",
    "published_at",
    "publish_error",
    "updated_at",
]


@dataclass
class PublishArchiveResult:
    queue_path: Path
    moved: list[Path]
    skipped: list[str]
    log_path: Path


@dataclass
class PublishStatusImportResult:
    queue_path: Path
    imported: int
    updated: int
    unmatched: list[str]
    log_path: Path
    result_path: Path
    feedback_imported: int = 0
    unmatched_feedback: int = 0
    feedback_error: str = ""


@dataclass
class PublishSource:
    video_path: Path
    original_video_path: Path
    title: str
    cover_path: str
    source_type: str


def load_accounts(config: ProjectConfig) -> list[PublishAccount]:
    paths = [config.root / DIR_CONFIG / "publish_accounts.csv", config.root / "publish_accounts.csv"]
    path = next((candidate for candidate in paths if candidate.exists()), None)
    accounts: list[PublishAccount] = []
    if not path:
        return accounts

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            enabled_value = str(row.get("enabled", "true")).strip().lower()
            accounts.append(
                PublishAccount(
                    group=row.get("group", "A组"),
                    account_id=row.get("account_id", ""),
                    nickname=row.get("nickname", ""),
                    publish_count=int(row.get("publish_count") or 2),
                    interval_minutes=int(row.get("interval_minutes") or 20),
                    scheduled_time=row.get("scheduled_time", ""),
                    chrome_port=int(row.get("chrome_port") or 9222),
                    enabled=enabled_value not in {"0", "false", "no", "否"},
                )
            )
    return [account for account in accounts if account.enabled]


def ensure_demo_account(config: ProjectConfig) -> Path:
    path = config.root / DIR_CONFIG / "publish_accounts.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    if not rows:
        rows = [
            {
                "group": "默认组",
                "account_id": "demo_001",
                "nickname": "本地测试账号",
                "publish_count": "10",
                "interval_minutes": "20",
                "scheduled_time": "",
                "chrome_port": "9222",
                "enabled": "true",
            }
        ]
    else:
        enabled = [row for row in rows if str(row.get("enabled", "")).strip().lower() in {"1", "true", "yes", "是"}]
        if not enabled:
            row = rows[0]
            row["group"] = row.get("group") or "默认组"
            row["account_id"] = row.get("account_id") or "demo_001"
            current_name = row.get("nickname") or ""
            row["nickname"] = "本地测试账号" if current_name.startswith("请填写") else (current_name or "本地测试账号")
            row["publish_count"] = row.get("publish_count") or "10"
            row["interval_minutes"] = row.get("interval_minutes") or "20"
            row["scheduled_time"] = row.get("scheduled_time") or ""
            row["chrome_port"] = row.get("chrome_port") or "9222"
            row["enabled"] = "true"
    for row in rows:
        if (row.get("nickname") or "").startswith("请填写") and str(row.get("enabled", "")).strip().lower() in {"1", "true", "yes", "是"}:
            row["nickname"] = "本地测试账号"
    _write_accounts(config, rows)
    log_line(config, f"发布账号已初始化/启用：{path}")
    return path


def _write_accounts(config: ProjectConfig, rows: list[dict[str, str]]) -> None:
    fields = ["group", "account_id", "nickname", "publish_count", "interval_minutes", "scheduled_time", "chrome_port", "enabled"]
    for path in [config.root / DIR_CONFIG / "publish_accounts.csv", config.root / "publish_accounts.csv"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fields})


def load_copywriting_map(config: ProjectConfig) -> dict[str, dict[str, str]]:
    path = config.root / DIR_COPYWRITING / "发布文案.csv"
    if not path.exists():
        generate_copywriting(config)
    result: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            result[row["video_path"]] = row
    return result


def choose_cover(config: ProjectConfig, video: Path) -> str:
    images = iter_media(config.root / DIR_COVER, IMAGE_EXTENSIONS)
    if not images:
        return ""
    matched = [image for image in images if image.stem in video.stem or video.stem in image.stem]
    return str(matched[0] if matched else images[0])


def load_packaged_sources(config: ProjectConfig) -> list[PublishSource]:
    manifest = config.root / DIR_VISUAL_PACKAGE / "包装素材清单.csv"
    sources: list[PublishSource] = []
    if manifest.exists():
        with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                packaged = Path(row.get("packaged_video_path", ""))
                original = Path(row.get("video_path", ""))
                if packaged.exists():
                    sources.append(
                        PublishSource(
                            video_path=packaged,
                            original_video_path=original if original.exists() else packaged,
                            title=row.get("title") or packaged.stem,
                            cover_path=row.get("poster_path", ""),
                            source_type="包装成片",
                        )
                    )
    if not sources:
        for video in sorted((config.root / DIR_PACKAGED_VIDEO).glob("*.mp4")) if (config.root / DIR_PACKAGED_VIDEO).exists() else []:
            sources.append(
                PublishSource(
                    video_path=video,
                    original_video_path=video,
                    title=video.stem,
                    cover_path=choose_cover(config, video),
                    source_type="包装成片",
                )
            )
    return sources


def load_ready_sources(config: ProjectConfig) -> list[PublishSource]:
    generate_titles(config)
    generate_copywriting(config)
    titles = load_title_map(config)
    return [
        PublishSource(
            video_path=video,
            original_video_path=video,
            title=titles.get(str(video), f"{config.drama_name} 精彩片段"),
            cover_path=choose_cover(config, video),
            source_type="合格待发布",
        )
        for video in ready_videos(config)
    ]


def load_social_clip_sources(config: ProjectConfig) -> list[PublishSource]:
    generate_titles(config)
    generate_copywriting(config)
    titles = load_title_map(config)
    sources: list[PublishSource] = []
    root = config.root / DIR_SOCIAL_CLIPS
    if not root.exists():
        return sources
    manifests = sorted(root.glob("*/social_clip_manifest.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    seen: set[str] = set()
    for manifest in manifests:
        try:
            rows = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception:
            rows = []
        for row in rows:
            video = Path(row.get("output_video", ""))
            original = Path(row.get("source_video", ""))
            if not video.exists() or str(video) in seen:
                continue
            seen.add(str(video))
            sources.append(
                PublishSource(
                    video_path=video,
                    original_video_path=original if original.exists() else video,
                    title=titles.get(str(original), f"{config.drama_name} 精彩片段"),
                    cover_path=choose_cover(config, original if original.exists() else video),
                    source_type="社媒增强版",
                )
            )
    if not sources:
        for video in sorted(root.glob("**/*.mp4"), key=lambda item: item.stat().st_mtime, reverse=True):
            sources.append(
                PublishSource(
                    video_path=video,
                    original_video_path=video,
                    title=f"{config.drama_name} 精彩片段",
                    cover_path=choose_cover(config, video),
                    source_type="社媒增强版",
                )
            )
    return sources


def queue_sources(config: ProjectConfig, source: str = "auto") -> list[PublishSource]:
    if source == "packaged":
        return load_packaged_sources(config)
    if source == "social":
        return load_social_clip_sources(config)
    if source == "ready":
        return load_ready_sources(config)
    packaged = load_packaged_sources(config)
    social = load_social_clip_sources(config)
    return packaged or social or load_ready_sources(config)


def build_queue(config: ProjectConfig, reuse_videos: bool = False, source: str = "auto", auto_account: bool = False) -> Path:
    videos = queue_sources(config, source=source)
    if auto_account:
        ensure_demo_account(config)
    accounts = load_accounts(config)
    if not accounts:
        raise FileNotFoundError("账号表中没有可用账号。请先在 00_项目配置/publish_accounts.csv 中填写账号。")
    if not videos:
        raise FileNotFoundError("没有可发布视频。请先生成合格待发布视频，或在标题封面页生成包装成片。")

    generate_copywriting(config)
    copywriting = load_copywriting_map(config)
    output = config.root / DIR_QUEUE / "publish_queue.csv"
    output.parent.mkdir(parents=True, exist_ok=True)

    items: list[dict[str, str]] = []
    video_index = 0
    for account in accounts:
        for _ in range(account.publish_count):
            if video_index >= len(videos):
                if not reuse_videos:
                    break
                video_index = 0
            publish_source = videos[video_index]
            video_index += 1
            copy = copywriting.get(str(publish_source.original_video_path), {})
            item = QueueItem(
                group=account.group,
                account_id=account.account_id,
                nickname=account.nickname,
                video_path=str(publish_source.video_path),
                title=publish_source.title,
                cover_path=publish_source.cover_path or choose_cover(config, publish_source.original_video_path),
                scheduled_time=account.scheduled_time,
            )
            row = item.__dict__ | {
                "source_type": publish_source.source_type,
                "original_video_path": str(publish_source.original_video_path),
                "description": copy.get("description", ""),
                "tags": copy.get("tags", ""),
                "chrome_port": str(account.chrome_port),
                "published_at": "",
            }
            items.append(row)

    write_queue_rows(config, items, output)
    return output


def read_queue_rows(config: ProjectConfig) -> tuple[Path, list[dict[str, str]]]:
    path = config.root / DIR_QUEUE / "publish_queue.csv"
    if not path.exists():
        raise FileNotFoundError("发布队列表不存在，请先生成发布队列。")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return path, list(csv.DictReader(handle))


def write_queue_rows(config: ProjectConfig, rows: list[dict[str, str]], output: Path | None = None) -> Path:
    output = output or (config.root / DIR_QUEUE / "publish_queue.csv")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = list(QUEUE_FIELDS)
        extras = sorted({key for row in rows for key in row.keys()} - set(fieldnames))
        writer = csv.DictWriter(handle, fieldnames=fieldnames + extras, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames + extras})
    root_output = config.root / "publish_queue.csv"
    if root_output != output:
        root_output.write_text(output.read_text(encoding="utf-8-sig"), encoding="utf-8-sig")
    return output


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{index:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法生成不重复文件名：{path}")


def append_publish_log(config: ProjectConfig, rows: list[dict[str, str]]) -> Path:
    output = config.root / DIR_REPORTS / "发布记录.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    exists = output.exists()
    with output.open("a", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "time",
            "group",
            "account_id",
            "nickname",
            "video_path",
            "title",
            "status",
            "published_at",
            "platform",
            "platform_url",
            "external_task_id",
            "publish_error",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames} | {"time": datetime.now().isoformat(timespec="seconds")})
    return output


def import_publish_status(config: ProjectConfig, status_file: str | Path, archive_published: bool = False) -> PublishStatusImportResult:
    queue_path, queue_rows = read_queue_rows(config)
    status_rows = _read_status_rows(Path(status_file))
    now_value = datetime.now()
    now = now_value.isoformat(timespec="seconds")
    import_id = now_value.strftime("%Y%m%d_%H%M%S")
    updated_rows: list[dict[str, str]] = []
    updated_status_rows: list[dict[str, str]] = []
    unmatched: list[str] = []

    for status in status_rows:
        target = _match_queue_row(queue_rows, status)
        label = status.get("video_path") or status.get("original_video_path") or status.get("external_task_id") or status.get("title") or str(status)
        if not target:
            unmatched.append(label)
            continue
        normalized = _normalize_publish_status(status.get("status") or status.get("publish_status") or status.get("状态") or "")
        if normalized:
            target["status"] = normalized
        published_at = status.get("published_at") or status.get("publish_time") or status.get("发布时间") or ""
        if normalized == "已发布" and not published_at:
            published_at = now
        if published_at:
            target["published_at"] = published_at
        for target_key, source_keys in {
            "platform": ["platform", "平台"],
            "platform_url": ["platform_url", "url", "链接", "发布链接"],
            "external_task_id": ["external_task_id", "task_id", "任务ID", "发布任务ID"],
            "publish_error": ["publish_error", "error", "失败原因", "错误"],
        }.items():
            value = _first_value(status, source_keys)
            if value:
                target[target_key] = value
        target["updated_at"] = now
        updated_rows.append(dict(target))
        updated_status_rows.append(dict(status))

    if archive_published:
        _archive_rows_marked_published(config, queue_rows)
    write_queue_rows(config, queue_rows, queue_path)
    log_path = append_publish_log(config, updated_rows) if updated_rows else config.root / DIR_REPORTS / "发布记录.csv"
    feedback_imported = 0
    unmatched_feedback: list[str] = []
    feedback_error = ""
    try:
        feedback_result = append_queue_status_feedback_records(
            config,
            updated_status_rows,
            updated_rows,
            status_file,
            import_id=import_id,
        )
        feedback_imported = feedback_result.imported
        unmatched_feedback = feedback_result.unmatched
    except Exception as exc:  # pragma: no cover - defensive non-blocking bridge
        feedback_error = str(exc)[-500:]
        log_line(config, f"队列状态汇入回流记录失败：{feedback_error}")
    result_path = config.root / DIR_PUBLISH_STATUS_RESULT
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_payload = {
        "imported": len(status_rows),
        "updated": len(updated_rows),
        "unmatched": unmatched,
        "feedback_imported": feedback_imported,
        "unmatched_feedback": len(unmatched_feedback),
        "unmatched_feedback_items": unmatched_feedback,
        "feedback_error": feedback_error,
        "source": str(status_file),
        "queue_path": str(queue_path),
        "log_path": str(log_path),
        "created_at": now,
    }
    result_path.write_text(json.dumps(result_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log_line(config, f"发布状态回写：读取 {len(status_rows)} 条，更新 {len(updated_rows)} 条，汇入回流 {feedback_imported} 条。")
    return PublishStatusImportResult(
        queue_path,
        len(status_rows),
        len(updated_rows),
        unmatched,
        log_path,
        result_path,
        feedback_imported,
        len(unmatched_feedback),
        feedback_error,
    )


def mark_queue_failed(config: ProjectConfig, reason: str = "", limit: int | None = 1) -> PublishStatusImportResult:
    queue_path, rows = read_queue_rows(config)
    changed: list[dict[str, str]] = []
    remaining = limit if limit is not None else len(rows)
    now = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        if remaining <= 0:
            break
        if row.get("status") in {"已发布", "发布失败"}:
            continue
        row["status"] = "发布失败"
        row["publish_error"] = reason
        row["updated_at"] = now
        changed.append(dict(row))
        remaining -= 1
    write_queue_rows(config, rows, queue_path)
    log_path = append_publish_log(config, changed) if changed else config.root / DIR_REPORTS / "发布记录.csv"
    result_path = config.root / DIR_PUBLISH_STATUS_RESULT
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps({"imported": len(changed), "updated": len(changed), "unmatched": [], "manual_reason": reason, "created_at": now}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return PublishStatusImportResult(queue_path, len(changed), len(changed), [], log_path, result_path)


def _read_status_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"发布回写文件不存在：{path}")
    suffix = path.suffix.lower()
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = payload.get("items") or payload.get("queue") or payload.get("rows") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        return [{str(key): str(value) for key, value in row.items()} for row in rows if isinstance(row, dict)]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _match_queue_row(queue_rows: list[dict[str, str]], status: dict[str, str]) -> dict[str, str] | None:
    keys = [
        ("external_task_id", ["external_task_id", "task_id", "任务ID", "发布任务ID"]),
        ("video_path", ["video_path", "视频路径", "发布视频"]),
        ("original_video_path", ["original_video_path", "原视频路径"]),
        ("title", ["title", "标题"]),
    ]
    for queue_key, status_keys in keys:
        value = _first_value(status, status_keys)
        if not value:
            continue
        normalized = str(value).strip().lower()
        for row in queue_rows:
            candidate = str(row.get(queue_key, "")).strip().lower()
            if candidate and (candidate == normalized or Path(candidate).name.lower() == Path(normalized).name.lower()):
                return row
    return None


def _first_value(row: dict[str, str], keys: list[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _normalize_publish_status(value: str) -> str:
    lowered = str(value).strip().lower()
    if lowered in {"已发布", "published", "success", "succeeded", "done", "完成", "成功"}:
        return "已发布"
    if lowered in {"发布失败", "failed", "fail", "error", "失败", "异常"}:
        return "发布失败"
    if lowered in {"发布中", "publishing", "running", "处理中"}:
        return "发布中"
    if lowered in {"已交接", "submitted", "queued", "待平台处理"}:
        return "已交接"
    if lowered:
        return value
    return ""


def _archive_rows_marked_published(config: ProjectConfig, rows: list[dict[str, str]]) -> None:
    done_dir = config.root / DIR_DONE
    done_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        if row.get("status") != "已发布":
            continue
        source = Path(row.get("video_path", ""))
        if not source.exists() or source.parent == done_dir:
            continue
        destination = _unique_destination(done_dir / source.name)
        shutil.move(str(source), str(destination))
        row["video_path"] = str(destination)


def mark_queue_published(config: ProjectConfig, limit: int | None = None) -> PublishArchiveResult:
    queue_path, rows = read_queue_rows(config)
    done_dir = config.root / DIR_DONE
    done_dir.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    skipped: list[str] = []
    changed_rows: list[dict[str, str]] = []
    remaining = limit if limit is not None else len(rows)

    for row in rows:
        if remaining <= 0:
            continue
        if row.get("status") == "已发布":
            continue
        source = Path(row.get("video_path", ""))
        if not source.exists():
            skipped.append(str(source))
            continue
        destination = _unique_destination(done_dir / source.name)
        shutil.move(str(source), str(destination))
        row["video_path"] = str(destination)
        row["status"] = "已发布"
        row["published_at"] = datetime.now().isoformat(timespec="seconds")
        moved.append(destination)
        changed_rows.append(dict(row))
        remaining -= 1

    write_queue_rows(config, rows, queue_path)
    log_path = append_publish_log(config, changed_rows) if changed_rows else config.root / DIR_REPORTS / "发布记录.csv"
    if moved:
        log_line(config, f"发布完成归档 {len(moved)} 条视频。")
    return PublishArchiveResult(queue_path=queue_path, moved=moved, skipped=skipped, log_path=log_path)
