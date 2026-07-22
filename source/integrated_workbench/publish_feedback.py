from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from .models import DIR_PUBLISH_STATUS, DIR_READY, DIR_REVIEW, ProjectConfig, VIDEO_EXTENSIONS
from .project import iter_media, log_line
from .video_dedup import _path_key, _read_render_manifest


TEMPLATE_COLUMNS = [
    "account_slot",
    "file",
    "source",
    "batch_id",
    "dedup_level",
    "similarity_status",
    "platform",
    "publish_status",
    "publish_time",
    "publish_url",
    "play_count",
    "duplicate_flag",
    "fail_reason",
    "note",
]

ORIGIN_FEEDBACK_TEMPLATE = "feedback_template"
ORIGIN_QUEUE_IMPORT = "queue_import"
ORIGIN_ASSISTANT = "publish_assistant"
RECORD_COLUMNS = TEMPLATE_COLUMNS + ["origin", "import_id", "matched_path"]
INSUFFICIENT_SAMPLE_MESSAGE = "样本不足，仅记录，不给出阈值调整建议"


@dataclass
class FeedbackTemplateResult:
    template_csv: Path
    row_count: int
    ready_dir: Path


@dataclass
class FeedbackImportResult:
    imported: int
    unmatched: list[str]
    invalid: list[str]
    records_csv: Path
    review_json: Path
    review_md: Path


@dataclass
class QueueFeedbackAppendResult:
    imported: int
    unmatched: list[str]
    records_csv: Path
    review_json: Path
    review_md: Path


def generate_feedback_template(config: ProjectConfig, ready_dir: str | Path | None = None) -> FeedbackTemplateResult:
    selected_dir = _resolve_ready_dir(config, ready_dir)
    production_csv = selected_dir / "生产清单.csv"
    if not production_csv.exists():
        raise FileNotFoundError(f"产线目录缺少生产清单.csv：{selected_dir}")

    production_rows = _read_csv_rows(production_csv)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    template_csv = selected_dir / f"发布回流模板_{stamp}.csv"
    rows: list[dict[str, str]] = []
    for row in production_rows:
        file_value = str(row.get("file", "") or "").strip()
        rows.append(
            {
                "account_slot": str(row.get("account_slot", "") or ""),
                "file": Path(file_value).name if file_value else "",
                "source": str(row.get("source", "") or ""),
                "batch_id": str(row.get("batch_id", "") or ""),
                "dedup_level": str(row.get("dedup_level", "") or ""),
                "similarity_status": str(row.get("similarity_status", "") or ""),
                "platform": "",
                "publish_status": "",
                "publish_time": "",
                "publish_url": "",
                "play_count": "",
                "duplicate_flag": "",
                "fail_reason": "",
                "note": "",
            }
        )

    _write_csv(template_csv, rows, TEMPLATE_COLUMNS)
    _write_template_guide(template_csv.with_suffix(".md"))
    return FeedbackTemplateResult(template_csv=template_csv, row_count=len(rows), ready_dir=selected_dir)


def import_feedback(config: ProjectConfig, filled_csv: str | Path) -> FeedbackImportResult:
    path = _resolve_path(config, filled_csv)
    if not path.exists():
        raise FileNotFoundError(f"发布回写文件不存在：{filled_csv}")

    raw_rows = _read_csv_rows(path)
    import_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    imported_rows: list[dict[str, str]] = []
    invalid: list[str] = []
    unmatched: list[str] = []

    for line_no, raw in enumerate(raw_rows, start=2):
        normalized, errors = _normalize_feedback_row(raw)
        label = normalized.get("file") or raw.get("file") or raw.get("文件") or f"第 {line_no} 行"
        if errors:
            invalid.append(f"第 {line_no} 行：{'；'.join(errors)}")
            continue
        matched_path = _match_feedback_video(config, normalized, path.parent)
        if not matched_path:
            unmatched.append(f"第 {line_no} 行：{label}")
            continue
        normalized["origin"] = ORIGIN_FEEDBACK_TEMPLATE
        normalized["import_id"] = import_id
        normalized["matched_path"] = str(matched_path)
        imported_rows.append(normalized)

    records_csv = feedback_records_path(config)
    _append_feedback_records(records_csv, imported_rows)
    review_json, review_md = build_feedback_review(config)
    return FeedbackImportResult(
        imported=len(imported_rows),
        unmatched=unmatched,
        invalid=invalid,
        records_csv=records_csv,
        review_json=review_json,
        review_md=review_md,
    )


def build_feedback_review(config: ProjectConfig) -> tuple[Path, Path]:
    records_csv = feedback_records_path(config)
    rows = _read_feedback_record_rows(records_csv) if records_csv.exists() else []
    latest_rows = _latest_records(rows)

    status_counts: dict[str, int] = {}
    platform_counts: dict[str, int] = {}
    account_counts: dict[str, int] = {}
    origin_counts: dict[str, int] = {}
    duplicate_count = 0
    duplicate_rows: list[dict[str, Any]] = []
    normal_similarity_values: list[float] = []
    play_by_account: dict[str, dict[str, float]] = {}

    enriched: list[dict[str, Any]] = []
    for row in latest_rows:
        status = str(row.get("publish_status", "") or "")
        platform = str(row.get("platform", "") or "未填写")
        account = str(row.get("account_slot", "") or "未填写")
        origin = _record_origin(row)
        duplicate_flag = str(row.get("duplicate_flag", "") or "")
        status_counts[status] = status_counts.get(status, 0) + 1
        platform_counts[platform] = platform_counts.get(platform, 0) + 1
        account_counts[account] = account_counts.get(account, 0) + 1
        origin_counts[origin] = origin_counts.get(origin, 0) + 1
        if duplicate_flag == "是":
            duplicate_count += 1

        context = _video_context(config, row)
        batch_max_similarity = context.get("batch_max_similarity")
        variation = _variation_summary(context.get("manifest_row") or {}, row)
        item = {
            **row,
            "batch_dir": context.get("batch_dir", ""),
            "batch_max_similarity": batch_max_similarity,
            "variation_summary": variation,
            "variation_fields": context.get("variation_fields") or {},
        }
        enriched.append(item)

        if _is_duplicate_feedback(row):
            duplicate_rows.append(
                {
                    "file": row.get("file", ""),
                    "account_slot": row.get("account_slot", ""),
                    "platform": row.get("platform", ""),
                    "publish_status": row.get("publish_status", ""),
                    "duplicate_flag": row.get("duplicate_flag", ""),
                    "batch_id": row.get("batch_id", ""),
                    "batch_max_similarity": batch_max_similarity,
                    "dedup_level": row.get("dedup_level", ""),
                    "variation_summary": variation,
                    "fail_reason": row.get("fail_reason", ""),
                }
            )
        elif row.get("publish_status") == "published" and isinstance(batch_max_similarity, (int, float)):
            normal_similarity_values.append(float(batch_max_similarity))

        play_count = _parse_int(row.get("play_count", ""))
        if play_count is not None:
            bucket = play_by_account.setdefault(account, {"total": 0.0, "count": 0.0, "average": 0.0})
            bucket["total"] += play_count
            bucket["count"] += 1
            bucket["average"] = round(bucket["total"] / bucket["count"], 2)

    duplicate_similarity_values = [
        float(row["batch_max_similarity"])
        for row in duplicate_rows
        if isinstance(row.get("batch_max_similarity"), (int, float))
    ]
    normal_stats = _number_stats(normal_similarity_values)
    calibration = _calibration_suggestion(duplicate_similarity_values, normal_similarity_values)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = config.root / DIR_PUBLISH_STATUS
    output_dir.mkdir(parents=True, exist_ok=True)
    review_json = output_dir / f"回流复盘_{stamp}.json"
    review_md = output_dir / f"回流复盘_{stamp}.md"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "records_csv": str(records_csv),
        "latest_record_count": len(latest_rows),
        "overview": {
            "by_publish_status": status_counts,
            "duplicate_flag_yes": duplicate_count,
            "by_platform": platform_counts,
            "by_account_slot": account_counts,
            "by_origin": origin_counts,
        },
        "duplicate_association_rows": duplicate_rows,
        "normal_published_similarity_distribution": normal_stats,
        "playback_by_account_slot": play_by_account,
        "calibration_suggestion": calibration,
        "records": enriched,
    }
    review_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    review_md.write_text(_feedback_review_markdown(payload), encoding="utf-8")
    return review_json, review_md


def feedback_records_path(config: ProjectConfig) -> Path:
    return config.root / DIR_PUBLISH_STATUS / "发布回写记录.csv"


def latest_feedback_records_for_batch(
    config: ProjectConfig,
    batch_id: str,
    rendered_videos: list[str],
) -> list[dict[str, str]]:
    records_csv = feedback_records_path(config)
    if not records_csv.exists():
        return []
    targets = {Path(value).name.lower() for value in rendered_videos if value}
    latest = _latest_records(_read_feedback_record_rows(records_csv))
    result: list[dict[str, str]] = []
    for row in latest:
        row_batch = str(row.get("batch_id", "") or "")
        row_names = {
            Path(str(row.get("file", "") or "")).name.lower(),
            Path(str(row.get("matched_path", "") or "")).name.lower(),
        }
        if row_batch == batch_id or bool(targets & row_names):
            result.append(row)
    return result


def append_queue_status_feedback_records(
    config: ProjectConfig,
    raw_status_rows: list[dict[str, str]],
    queue_rows: list[dict[str, str]],
    status_file: str | Path,
    import_id: str,
) -> QueueFeedbackAppendResult:
    source_path = _resolve_path(config, status_file)
    source_dir = source_path.parent if source_path.exists() else config.root / DIR_PUBLISH_STATUS
    imported_rows: list[dict[str, str]] = []
    unmatched: list[str] = []

    for raw, queue_row in zip(raw_status_rows, queue_rows):
        file_value = (
            _first_value(raw, ["video_path", "视频路径", "发布视频", "file", "文件"])
            or str(queue_row.get("video_path", "") or "")
            or str(queue_row.get("original_video_path", "") or "")
        )
        candidate_values = [
            file_value,
            str(queue_row.get("original_video_path", "") or ""),
            str(queue_row.get("video_path", "") or ""),
        ]
        matched_path = None
        for candidate_value in candidate_values:
            if not candidate_value:
                continue
            candidate = {
                "file": candidate_value,
                "matched_path": candidate_value,
            }
            matched_path = _match_feedback_video(config, candidate, source_dir)
            if matched_path:
                break
        label = file_value or raw.get("external_task_id") or raw.get("title") or str(raw)
        if not matched_path:
            unmatched.append(str(label))
            continue

        raw_status = _first_value(raw, ["publish_status", "status", "状态"])
        normalized_status = _normalize_publish_status(raw_status) or _normalize_publish_status(str(queue_row.get("status", "") or ""))
        note_values = [
            _first_value(raw, ["note", "备注", "说明"]),
            str(queue_row.get("description", "") or ""),
        ]
        if not normalized_status:
            normalized_status = "pending"
            if raw_status:
                note_values.append(f"队列状态原文：{raw_status}")

        published_at = (
            _first_value(raw, ["publish_time", "published_at", "发布时间"])
            or str(queue_row.get("published_at", "") or "")
        )
        row = {
            "account_slot": str(queue_row.get("account_id") or queue_row.get("nickname") or ""),
            "file": matched_path.name,
            "source": str(queue_row.get("original_video_path") or queue_row.get("source_type") or ""),
            "batch_id": "",
            "dedup_level": "",
            "similarity_status": "",
            "platform": _first_value(raw, ["platform", "平台"]) or str(queue_row.get("platform", "") or ""),
            "publish_status": normalized_status,
            "publish_time": _parse_publish_time(published_at) if published_at else "",
            "publish_url": (
                _first_value(raw, ["publish_url", "platform_url", "url", "链接", "发布链接"])
                or str(queue_row.get("platform_url", "") or "")
            ),
            "play_count": _first_value(raw, ["play_count", "播放量", "播放次数"]),
            "duplicate_flag": _normalize_duplicate_flag(_first_value(raw, ["duplicate_flag", "雷同标记", "是否雷同", "重复标记"])),
            "fail_reason": _first_value(raw, ["fail_reason", "publish_error", "error", "失败原因", "错误"]) or str(queue_row.get("publish_error", "") or ""),
            "note": "；".join(value for value in note_values if value),
            "origin": ORIGIN_QUEUE_IMPORT,
            "import_id": import_id,
            "matched_path": str(matched_path),
        }
        imported_rows.append(row)

    records_csv = feedback_records_path(config)
    _append_feedback_records(records_csv, imported_rows)
    review_json, review_md = build_feedback_review(config)
    return QueueFeedbackAppendResult(
        imported=len(imported_rows),
        unmatched=unmatched,
        records_csv=records_csv,
        review_json=review_json,
        review_md=review_md,
    )


def _resolve_ready_dir(config: ProjectConfig, ready_dir: str | Path | None) -> Path:
    if ready_dir:
        candidate = _resolve_path(config, ready_dir)
        if candidate.exists() and candidate.is_dir():
            return candidate
        raise FileNotFoundError(f"合格产线目录不存在：{ready_dir}")

    root = config.root / DIR_READY
    dirs = _available_ready_dirs(config)
    if dirs:
        return dirs[-1]
    available = "、".join(path.name for path in sorted(root.iterdir())) if root.exists() else "无"
    raise FileNotFoundError(f"没有可生成回流模板的产线目录。当前合格待发布目录：{available}")


def _available_ready_dirs(config: ProjectConfig) -> list[Path]:
    root = config.root / DIR_READY
    if not root.exists():
        return []
    return sorted(
        [path for path in root.iterdir() if path.is_dir() and path.name.startswith("产线_") and (path / "生产清单.csv").exists()],
        key=lambda item: (item.stat().st_mtime, item.name),
    )


def _resolve_path(config: ProjectConfig, value: str | Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    if path.exists():
        return path
    return config.root / path


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [{str(key): str(value or "") for key, value in row.items() if key is not None} for row in csv.DictReader(handle)]


def _read_feedback_record_rows(path: Path) -> list[dict[str, str]]:
    rows = _read_csv_rows(path)
    for row in rows:
        row["origin"] = _record_origin(row)
    return rows


def _write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def _append_feedback_records(path: Path, rows: list[dict[str, str]]) -> None:
    _ensure_record_columns(path)
    if not rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            _write_csv(path, [], RECORD_COLUMNS)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECORD_COLUMNS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            normalized = dict(row)
            normalized["origin"] = _record_origin(normalized)
            writer.writerow({field: normalized.get(field, "") for field in RECORD_COLUMNS})


def _ensure_record_columns(path: Path) -> None:
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = [dict(row) for row in reader]
    except Exception:
        return
    if all(field in fieldnames for field in RECORD_COLUMNS):
        return
    migrated: list[dict[str, str]] = []
    for row in rows:
        clean = {str(key): str(value or "") for key, value in row.items() if key is not None}
        clean["origin"] = _record_origin(clean)
        migrated.append(clean)
    _write_csv(path, migrated, RECORD_COLUMNS)


def _write_template_guide(path: Path) -> None:
    lines = [
        "# 发布回流模板填写说明",
        "",
        "## 软件已填写",
        "",
        "- account_slot、file、source、batch_id、dedup_level、similarity_status 不建议改动。",
        "",
        "## 人工填写",
        "",
        "- platform：发布平台，例如 抖音、快手、TikTok。",
        "- publish_status：published / rejected_duplicate / limited / deleted / failed / pending。",
        "- publish_time：支持 YYYY-MM-DD、YYYY/MM/DD HH:MM 等格式。",
        "- publish_url：发布链接，没有可空。",
        "- play_count：播放量，非空时必须是非负整数。",
        "- duplicate_flag：平台明确判雷同/重复填 是；否则填 否 或留空。",
        "- fail_reason：失败、雷同、限流原因。",
        "- note：补充说明。",
        "",
        "## 容错规则",
        "",
        "- 导入时坏行会被标出行号，不会影响其他有效行。",
        "- 同一视频同一平台可以多次导入，系统保留历史，复盘使用最后一次状态。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _normalize_feedback_row(row: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    mapped = {field: _first_value(row, _aliases(field)) for field in TEMPLATE_COLUMNS}
    errors: list[str] = []
    status = _normalize_publish_status(mapped.get("publish_status", ""))
    if not status:
        errors.append("publish_status 必填或无法识别")
    mapped["publish_status"] = status

    mapped["duplicate_flag"] = _normalize_duplicate_flag(mapped.get("duplicate_flag", ""))
    play_count = str(mapped.get("play_count", "") or "").strip()
    if play_count:
        parsed = _parse_int(play_count)
        if parsed is None or parsed < 0:
            errors.append("play_count 必须是非负整数")
        else:
            mapped["play_count"] = str(parsed)
    publish_time = str(mapped.get("publish_time", "") or "").strip()
    if publish_time:
        parsed_time = _parse_publish_time(publish_time)
        if not parsed_time:
            errors.append("publish_time 格式无法识别")
        else:
            mapped["publish_time"] = parsed_time
    if not str(mapped.get("file", "") or "").strip():
        errors.append("file 必填")
    return ({field: mapped.get(field, "") for field in TEMPLATE_COLUMNS}, errors)


def _aliases(field: str) -> list[str]:
    return {
        "account_slot": ["account_slot", "账号", "账号位", "账号槽"],
        "file": ["file", "文件", "视频", "视频文件", "video", "video_file"],
        "source": ["source", "来源", "原始来源"],
        "batch_id": ["batch_id", "批次", "批次ID"],
        "dedup_level": ["dedup_level", "去重等级", "差异化等级"],
        "similarity_status": ["similarity_status", "相似状态", "指纹状态"],
        "platform": ["platform", "平台"],
        "publish_status": ["publish_status", "状态", "发布状态", "status"],
        "publish_time": ["publish_time", "发布时间", "published_at"],
        "publish_url": ["publish_url", "链接", "发布链接", "url", "platform_url"],
        "play_count": ["play_count", "播放量", "播放次数"],
        "duplicate_flag": ["duplicate_flag", "雷同标记", "是否雷同", "重复标记"],
        "fail_reason": ["fail_reason", "失败原因", "原因"],
        "note": ["note", "备注", "说明"],
    }.get(field, [field])


def _first_value(row: dict[str, str], keys: list[str]) -> str:
    lower_map = {str(key).strip().lower(): str(value or "").strip() for key, value in row.items()}
    for key in keys:
        value = lower_map.get(key.lower())
        if value:
            return value
    return ""


def _normalize_publish_status(value: str) -> str:
    text = str(value or "").strip().lower()
    mapping = {
        "published": "published",
        "success": "published",
        "succeeded": "published",
        "done": "published",
        "已发布": "published",
        "成功": "published",
        "发布成功": "published",
        "rejected_duplicate": "rejected_duplicate",
        "duplicate": "rejected_duplicate",
        "雷同": "rejected_duplicate",
        "重复": "rejected_duplicate",
        "内容重复": "rejected_duplicate",
        "内容雷同": "rejected_duplicate",
        "审核雷同": "rejected_duplicate",
        "limited": "limited",
        "限流": "limited",
        "流量限制": "limited",
        "deleted": "deleted",
        "删除": "deleted",
        "已删除": "deleted",
        "下架": "deleted",
        "failed": "failed",
        "fail": "failed",
        "失败": "failed",
        "发布失败": "failed",
        "pending": "pending",
        "待发布": "pending",
        "待处理": "pending",
        "审核中": "pending",
        "发布中": "pending",
        "已交接": "pending",
        "submitted": "pending",
        "queued": "pending",
    }
    return mapping.get(text, "")


def _normalize_duplicate_flag(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"是", "y", "yes", "true", "1", "雷同", "重复", "duplicate"}:
        return "是"
    if text in {"否", "n", "no", "false", "0"}:
        return "否"
    return ""


def _parse_int(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if "." in text:
            number = float(text)
            if not number.is_integer():
                return None
            return int(number)
        return int(text)
    except ValueError:
        return None


def _parse_publish_time(value: str) -> str:
    text = str(value or "").strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if "%H" in fmt:
                return parsed.strftime("%Y-%m-%d %H:%M")
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _match_feedback_video(config: ProjectConfig, row: dict[str, str], filled_dir: Path) -> Path | None:
    value = str(row.get("file", "") or "").strip()
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute() and candidate.exists():
        return candidate
    search_roots = [
        filled_dir,
        config.root / DIR_READY,
        config.root / DIR_REVIEW,
    ]
    target_name = candidate.name.lower()
    for root in search_roots:
        direct = root / candidate.name
        if direct.exists():
            return direct
    for root in search_roots:
        if not root.exists():
            continue
        for video in iter_media(root, VIDEO_EXTENSIONS):
            if video.name.lower() == target_name:
                return video
    return None


def _latest_records(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    latest: dict[tuple[str, str], dict[str, str]] = {}
    order: list[tuple[str, str]] = []
    position: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        row["origin"] = _record_origin(row)
        filename = Path(str(row.get("matched_path") or row.get("file") or "")).name.lower()
        platform = str(row.get("platform", "") or "").strip().lower()
        key = (filename, platform)
        if key not in latest:
            order.append(key)
            latest[key] = row
            position[key] = index
            continue
        if _record_is_newer(row, latest[key], index, position.get(key, -1)):
            latest[key] = row
            position[key] = index
    return [latest[key] for key in order if key in latest]


def _record_origin(row: dict[str, str]) -> str:
    origin = str(row.get("origin", "") or "").strip()
    return origin or ORIGIN_FEEDBACK_TEMPLATE


def _record_is_newer(candidate: dict[str, str], current: dict[str, str], candidate_index: int, current_index: int) -> bool:
    candidate_id = str(candidate.get("import_id", "") or "")
    current_id = str(current.get("import_id", "") or "")
    if candidate_id and current_id and candidate_id != current_id:
        return candidate_id > current_id
    if candidate_id and not current_id:
        return True
    if current_id and not candidate_id:
        return False
    return candidate_index >= current_index


def _video_context(config: ProjectConfig, row: dict[str, str]) -> dict[str, Any]:
    batch_dir = _find_batch_dir(config, row.get("batch_id", ""))
    manifest_rows = _read_render_manifest(batch_dir / "render_manifest.json") if batch_dir else []
    manifest_row = _match_manifest_row(manifest_rows, row)
    output_name = Path(str((manifest_row or {}).get("output") or row.get("matched_path") or row.get("file") or "")).name
    max_similarity = _max_similarity_for_video(config, batch_dir, output_name) if batch_dir and output_name else None
    return {
        "batch_dir": str(batch_dir or ""),
        "manifest_row": manifest_row,
        "batch_max_similarity": max_similarity,
        "variation_fields": _variation_fields(manifest_row or {}, row),
    }


def _find_batch_dir(config: ProjectConfig, batch_id: str) -> Path | None:
    text = str(batch_id or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if candidate.exists() and candidate.is_dir():
        return candidate
    root = config.root / DIR_REVIEW
    direct = root / text
    if direct.exists() and direct.is_dir():
        return direct
    if root.exists():
        for item in root.iterdir():
            if item.is_dir() and item.name == text:
                return item
    return None


def _match_manifest_row(rows: list[dict[str, Any]], feedback_row: dict[str, str]) -> dict[str, Any]:
    names = {
        Path(str(feedback_row.get("file", "") or "")).name.lower(),
        Path(str(feedback_row.get("matched_path", "") or "")).name.lower(),
    }
    names.discard("")
    matched_path = Path(str(feedback_row.get("matched_path", "") or ""))
    for row in rows:
        output = Path(str(row.get("output", "") or ""))
        if output.name.lower() in names:
            return row
        if str(output) and _path_key(output) == _path_key(matched_path):
            return row
    return {}


def _max_similarity_for_video(config: ProjectConfig, batch_dir: Path | None, filename: str) -> float | None:
    if not batch_dir:
        return None
    target = filename.lower()
    pairs = _load_similarity_pairs(config, batch_dir)
    values: list[float] = []
    for pair in pairs:
        left = Path(str(pair.get("left", "") or "")).name.lower()
        right = Path(str(pair.get("right", "") or "")).name.lower()
        if target in {left, right}:
            try:
                values.append(float(pair.get("similarity", 0.0) or 0.0))
            except (TypeError, ValueError):
                continue
    if not values:
        return None
    return round(max(values), 4)


def _load_similarity_pairs(config: ProjectConfig, batch_dir: Path) -> list[dict[str, Any]]:
    report_dir = batch_dir / "去重指纹报告"
    if not report_dir.exists():
        return []
    json_files = sorted(report_dir.glob("去重指纹报告_*.json"), key=lambda path: (path.stat().st_mtime, path.name))
    if json_files:
        try:
            payload = json.loads(json_files[-1].read_text(encoding="utf-8"))
            pairs = payload.get("pairs") or []
            if isinstance(pairs, list):
                return [dict(row) for row in pairs if isinstance(row, dict)]
        except Exception as exc:
            log_line(config, f"发布回流相似度明细读取失败：{json_files[-1]}，{exc}")
    csv_files = sorted(report_dir.glob("去重相似度明细_*.csv"), key=lambda path: (path.stat().st_mtime, path.name))
    return _read_csv_rows(csv_files[-1]) if csv_files else []


def _variation_fields(manifest_row: dict[str, Any], feedback_row: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in manifest_row.items():
        if str(key).startswith("dedup_"):
            result[str(key)] = str(value)
    for key in ["dedup_level", "similarity_status"]:
        if feedback_row.get(key):
            result[key] = str(feedback_row.get(key, ""))
    return result


def _variation_summary(manifest_row: dict[str, Any], feedback_row: dict[str, str]) -> str:
    fields = _variation_fields(manifest_row, feedback_row)
    enabled: list[str] = []
    if str(fields.get("dedup_hflip", "")).lower() in {"true", "1", "yes", "是"}:
        enabled.append("镜像")
    if _float_non_default(fields.get("dedup_foreground_scale"), 1.0):
        enabled.append(f"画面缩放 {fields.get('dedup_foreground_scale')}")
    if _int_non_zero(fields.get("dedup_x_shift")) or _int_non_zero(fields.get("dedup_y_shift")):
        enabled.append(f"位移 {fields.get('dedup_x_shift', '0')}/{fields.get('dedup_y_shift', '0')}")
    if _float_non_default(fields.get("dedup_brightness"), 0.0) or _float_non_default(fields.get("dedup_contrast"), 1.0):
        enabled.append("亮度/对比")
    if _float_non_default(fields.get("dedup_saturation"), 1.0) or _float_non_default(fields.get("dedup_gamma"), 1.0) or _float_non_default(fields.get("dedup_hue_degrees"), 0.0):
        enabled.append("色彩")
    if _int_non_zero(fields.get("dedup_noise_strength")):
        enabled.append(f"噪声 {fields.get('dedup_noise_strength')}")
    if _float_non_default(fields.get("dedup_sharpen"), 0.0):
        enabled.append(f"锐化 {fields.get('dedup_sharpen')}")
    if str(fields.get("dedup_border_style", "none")) not in {"", "none"}:
        enabled.append(f"边框 {fields.get('dedup_border_style')}")
    if str(fields.get("dedup_zoom_path", "none")) not in {"", "none"}:
        enabled.append(f"运动路径 {fields.get('dedup_zoom_path')}")
    if _float_non_default(fields.get("dedup_speed_factor"), 1.0):
        enabled.append(f"变速 {fields.get('dedup_speed_factor')}")
    if _float_non_default(fields.get("dedup_intro_trim"), 0.0) or _float_non_default(fields.get("dedup_outro_trim"), 0.0):
        enabled.append(f"裁头尾 {fields.get('dedup_intro_trim', '0')}/{fields.get('dedup_outro_trim', '0')}")
    return "、".join(enabled) if enabled else f"基础差异化({feedback_row.get('dedup_level') or '-'})"


def _float_non_default(value: Any, default: float) -> bool:
    try:
        return abs(float(value) - default) > 0.0001
    except (TypeError, ValueError):
        return False


def _int_non_zero(value: Any) -> bool:
    try:
        return int(float(value)) != 0
    except (TypeError, ValueError):
        return False


def _is_duplicate_feedback(row: dict[str, str]) -> bool:
    return row.get("duplicate_flag") == "是" or row.get("publish_status") == "rejected_duplicate"


def _number_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "min": None, "median": None, "max": None}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 4),
        "median": round(float(median(ordered)), 4),
        "max": round(ordered[-1], 4),
    }


def _calibration_suggestion(duplicate_values: list[float], normal_values: list[float]) -> str:
    if len(duplicate_values) < 5 or len(normal_values) < 10:
        return INSUFFICIENT_SAMPLE_MESSAGE
    duplicate_median = round(float(median(duplicate_values)), 4)
    normal_median = round(float(median(normal_values)), 4)
    return (
        f"仅供人工复盘：雷同样本中位相似度 {duplicate_median}，"
        f"正常发布样本中位相似度 {normal_median}。系统不会自动修改阈值。"
    )


def _feedback_review_markdown(payload: dict[str, Any]) -> str:
    overview = payload.get("overview") or {}
    normal = payload.get("normal_published_similarity_distribution") or {}
    lines = [
        "# 发布数据回流复盘",
        "",
        f"- 生成时间：{payload.get('generated_at')}",
        f"- 最新状态记录：{payload.get('latest_record_count', 0)} 条",
        f"- 校准建议：{payload.get('calibration_suggestion')}",
        "",
        "## 总览",
        "",
        f"- 发布状态：{overview.get('by_publish_status') or {}}",
        f"- 平台判雷同：{overview.get('duplicate_flag_yes', 0)} 条",
        f"- 平台分布：{overview.get('by_platform') or {}}",
        f"- 账号分布：{overview.get('by_account_slot') or {}}",
        "",
        "## 雷同关联表",
        "",
        "| 文件 | 账号 | 平台 | 批次内最大相似度 | 去重等级 | 启用差异维度 | 状态 | 原因 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    duplicate_rows = payload.get("duplicate_association_rows") or []
    if duplicate_rows:
        for row in duplicate_rows:
            similarity = row.get("batch_max_similarity")
            similarity_text = "-" if similarity is None else f"{float(similarity):.4f}"
            lines.append(
                f"| {row.get('file') or '-'} | {row.get('account_slot') or '-'} | {row.get('platform') or '-'} | "
                f"{similarity_text} | {row.get('dedup_level') or '-'} | {row.get('variation_summary') or '-'} | "
                f"{row.get('publish_status') or '-'} | {row.get('fail_reason') or '-'} |"
            )
    else:
        lines.append("| - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## 正常发布相似度分布",
            "",
            f"- 样本数：{normal.get('count', 0)}",
            f"- 最小/中位/最大：{normal.get('min')} / {normal.get('median')} / {normal.get('max')}",
            "",
            "## 播放表现",
            "",
            "| 账号 | 总播放 | 样本数 | 平均播放 |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    playback = payload.get("playback_by_account_slot") or {}
    if playback:
        for account, item in playback.items():
            lines.append(f"| {account} | {int(item.get('total', 0))} | {int(item.get('count', 0))} | {item.get('average', 0)} |")
    else:
        lines.append("| - | 0 | 0 | 0 |")
    lines.append("")
    return "\n".join(lines)
