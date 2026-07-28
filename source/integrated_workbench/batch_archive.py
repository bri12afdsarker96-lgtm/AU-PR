from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    DIR_BATCH_ARCHIVE,
    DIR_COVER,
    DIR_DONE,
    DIR_PACKAGED_VIDEO,
    DIR_PUBLISH_STATUS,
    DIR_QUEUE,
    DIR_READY,
    DIR_REJECT,
    DIR_REPORTS,
    DIR_REVIEW,
    DIR_SOCIAL_CLIPS,
    DIR_TITLE_TABLE,
    DIR_VISUAL_PACKAGE,
    ProjectConfig,
    VIDEO_EXTENSIONS,
)
from .project import candidate_dirs, iter_media
from .publish_feedback import latest_feedback_records_for_batch
from .video_dedup import _path_key, _read_render_manifest


@dataclass
class BatchArchiveResult:
    archive_json: Path
    archive_md: Path
    batch_id: str
    missing_artifacts: list[str]


def build_batch_archive(config: ProjectConfig, batch_dir: Path | None = None) -> BatchArchiveResult:
    selected_batch = _select_batch_dir(config, batch_dir)
    missing: list[str] = []
    now = datetime.now()
    archive_id = f"批次档案_{now:%Y%m%d_%H%M%S}"

    manifest_path = selected_batch / "render_manifest.json"
    manifest_rows = _read_render_manifest(manifest_path)
    if not manifest_rows:
        missing.append("二创 render_manifest")
    source_videos = _unique_strings(row.get("source", "") for row in manifest_rows)
    rendered_videos = _unique_strings(row.get("output", "") for row in manifest_rows)

    dedup_report = _collect_dedup_report(selected_batch, missing)
    rework_records = _collect_rework_records(config, selected_batch, missing)
    release_status = _collect_release_status(config, selected_batch, rendered_videos)

    title_rows = _read_csv_rows(config.root / DIR_TITLE_TABLE / "titles.csv", "标题表", missing)
    cover_rows = _read_csv_rows(config.root / DIR_COVER / "封面候选.csv", "封面候选表", missing)
    visual_manifest = config.root / DIR_VISUAL_PACKAGE / "包装素材清单.csv"
    if not visual_manifest.exists():
        missing.append("包装素材清单")
    packaged_videos = [str(path) for path in iter_media(config.root / DIR_PACKAGED_VIDEO, VIDEO_EXTENSIONS)]
    social_manifest = _latest_social_manifest(config)
    if social_manifest is None:
        missing.append("社媒切条增强清单")

    publish_queue_rows = _read_csv_rows(config.root / DIR_QUEUE / "publish_queue.csv", "发布队列", missing)
    publish_status_rows = _read_publish_status_rows(config, selected_batch, rendered_videos, missing)

    payload: dict[str, Any] = {
        "archive_id": archive_id,
        "generated_at": now.isoformat(timespec="seconds"),
        "batch_id": selected_batch.name,
        "batch_dir": str(selected_batch),
        "source_videos": source_videos,
        "rendered_videos": rendered_videos,
        "render_manifest": str(manifest_path) if manifest_path.exists() else "",
        "dedup_report": dedup_report,
        "high_similarity_rework": rework_records,
        "release_status": release_status,
        "title_rows": title_rows,
        "cover_rows": cover_rows,
        "visual_package_manifest": str(visual_manifest) if visual_manifest.exists() else "",
        "packaged_videos": packaged_videos,
        "social_clip_manifest": str(social_manifest or ""),
        "publish_queue_rows": publish_queue_rows,
        "publish_status_rows": publish_status_rows,
        "missing_artifacts": _unique_strings(missing),
    }

    output_dir = config.root / DIR_BATCH_ARCHIVE
    output_dir.mkdir(parents=True, exist_ok=True)
    archive_json = output_dir / f"{archive_id}.json"
    archive_md = output_dir / f"{archive_id}.md"
    archive_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    archive_md.write_text(_archive_markdown(payload), encoding="utf-8")
    return BatchArchiveResult(archive_json, archive_md, selected_batch.name, payload["missing_artifacts"])


def _select_batch_dir(config: ProjectConfig, batch_dir: Path | None) -> Path:
    if batch_dir is None:
        batches = _normal_batch_dirs(config)
        if not batches:
            raise FileNotFoundError("没有可归档的二创批次。")
        return batches[-1]

    candidate = Path(batch_dir)
    if not candidate.is_absolute():
        for root in candidate_dirs(config, DIR_REVIEW):
            direct = root / candidate
            if direct.exists():
                candidate = direct
                break
    if "高相似重做" in candidate.name:
        raise ValueError("高相似重做批次不单独建档，请对其原批次执行归档，重做记录会自动纳入原批次档案。")
    if not candidate.exists() or not candidate.is_dir():
        available = "、".join(path.name for path in _normal_batch_dirs(config)) or "无"
        raise FileNotFoundError(f"批次目录不存在：{batch_dir}。当前可用批次：{available}")
    return candidate


def _normal_batch_dirs(config: ProjectConfig) -> list[Path]:
    batches: list[Path] = []
    for root in candidate_dirs(config, DIR_REVIEW):
        if not root.exists():
            continue
        batches.extend(
            path
            for path in root.iterdir()
            if path.is_dir() and path.name.startswith("batch_") and "高相似重做" not in path.name
        )
    return sorted(batches, key=lambda path: (path.stat().st_mtime, path.name))


def _collect_dedup_report(batch_dir: Path, missing: list[str]) -> dict[str, Any] | None:
    report_dir = batch_dir / "去重指纹报告"
    reports = sorted(report_dir.glob("去重指纹报告_*.json"), key=lambda path: (path.stat().st_mtime, str(path))) if report_dir.exists() else []
    if not reports:
        missing.append("去重指纹报告")
        return None
    json_path = reports[-1]
    md_path = json_path.with_suffix(".md")
    try:
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        high_count = int((payload.get("summary") or {}).get("high_similarity", 0))
        if not high_count:
            high_count = sum(1 for pair in payload.get("pairs", []) if str(pair.get("level", "")).lower() == "high")
    except Exception:
        missing.append("去重指纹报告(解析失败)")
        high_count = 0
    return {"json": str(json_path), "md": str(md_path) if md_path.exists() else "", "high_count": high_count}


def _collect_rework_records(config: ProjectConfig, batch_dir: Path, missing: list[str]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for review_root in candidate_dirs(config, DIR_REVIEW):
        if not review_root.exists():
            continue
        for rework_dir in sorted(review_root.iterdir(), key=lambda path: path.name):
            if not rework_dir.is_dir() or "高相似重做" not in rework_dir.name:
                continue
            report_json = rework_dir / "高相似重做报告.json"
            if not report_json.exists():
                continue
            try:
                payload = json.loads(report_json.read_text(encoding="utf-8"))
            except Exception:
                missing.append(f"高相似重做报告(解析失败): {rework_dir.name}")
                continue
            if not _rework_matches_batch(payload, batch_dir):
                continue
            comparison_json = rework_dir / "高相似重做对比报告.json"
            comparison_md = rework_dir / "高相似重做对比报告.md"
            conclusion = ""
            if comparison_json.exists():
                try:
                    conclusion = str((json.loads(comparison_json.read_text(encoding="utf-8")) or {}).get("conclusion", ""))
                except Exception:
                    missing.append(f"高相似重做对比报告(解析失败): {rework_dir.name}")
            records.append(
                {
                    "rework_batch_dir": str(rework_dir),
                    "rework_report_json": str(report_json),
                    "rework_report_md": str(rework_dir / "高相似重做报告.md") if (rework_dir / "高相似重做报告.md").exists() else "",
                    "comparison_json": str(comparison_json) if comparison_json.exists() else "",
                    "comparison_md": str(comparison_md) if comparison_md.exists() else "",
                    "comparison_conclusion": conclusion,
                }
            )
    return records


def _rework_matches_batch(payload: dict[str, Any], batch_dir: Path) -> bool:
    source_batch = str(payload.get("source_batch", "") or "")
    if _path_or_name_matches(source_batch, batch_dir):
        return True
    source_report = str(payload.get("source_report", "") or "")
    if not source_report:
        return False
    report_path = Path(source_report)
    if _path_or_name_matches(str(report_path.parent.parent), batch_dir):
        return True
    return report_path.parent.parent.name == batch_dir.name


def _path_or_name_matches(value: str, target: Path) -> bool:
    if not value:
        return False
    candidate = Path(value)
    return _path_key(candidate) == _path_key(target) or candidate.name == target.name


def _collect_release_status(config: ProjectConfig, batch_dir: Path, rendered_videos: list[str]) -> list[dict[str, str]]:
    release_rows = _read_csv_rows(config.root / DIR_REPORTS / "放行记录.csv", "", [])
    result: list[dict[str, str]] = []
    for value in rendered_videos:
        original = Path(value)
        name = original.name
        status = "unknown"
        if (batch_dir / name).exists() or original.exists():
            status = "中间结果"
        if (config.root / DIR_READY / name).exists():
            status = "已放行"
        if (config.root / DIR_REJECT / name).exists():
            status = "已退回"
        if (config.root / DIR_DONE / name).exists():
            status = "已发布"
        if status == "unknown":
            for row in reversed(release_rows):
                row_name = row.get("video_name") or Path(row.get("video_path", "")).name
                if row_name != name:
                    continue
                action = row.get("action", "")
                if "退回" in action:
                    status = "已退回"
                elif "放行" in action:
                    status = "已放行"
                break
        result.append({"video": name, "status": status})
    return result


def _latest_social_manifest(config: ProjectConfig) -> Path | None:
    root = config.root / DIR_SOCIAL_CLIPS
    if not root.exists():
        return None
    matches = sorted(root.glob("*/社媒切条增强清单.csv"), key=lambda path: (path.stat().st_mtime, str(path)))
    return matches[-1] if matches else None


def _read_publish_status_rows(config: ProjectConfig, batch_dir: Path, rendered_videos: list[str], missing: list[str]) -> list[dict[str, str]]:
    feedback_rows = latest_feedback_records_for_batch(config, batch_dir.name, rendered_videos)
    if feedback_rows:
        return feedback_rows
    root = config.root / DIR_PUBLISH_STATUS
    if not root.exists():
        missing.append("发布回写")
        return []
    csv_files = sorted(
        [path for path in root.glob("*.csv") if path.name != "发布回写记录.csv" and not path.name.startswith("回流复盘_")],
        key=lambda path: (path.stat().st_mtime, str(path)),
    )
    if csv_files:
        return _read_csv_rows(csv_files[-1], "发布回写", missing)
    json_files = sorted(root.glob("*.json"), key=lambda path: (path.stat().st_mtime, str(path)))
    if not json_files:
        missing.append("发布回写")
        return []
    try:
        payload = json.loads(json_files[-1].read_text(encoding="utf-8-sig"))
    except Exception:
        missing.append("发布回写(解析失败)")
        return []
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("rows") or payload.get("items") or []
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def _read_csv_rows(path: Path, label: str, missing: list[str]) -> list[dict[str, str]]:
    if not path.exists():
        if label:
            missing.append(label)
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except Exception:
        if label:
            missing.append(f"{label}(解析失败)")
        return []


def _unique_strings(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _archive_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 成品批次档案",
        "",
        f"- 批次：`{payload['batch_id']}`",
        f"- 生成时间：{payload['generated_at']}",
        f"- 缺失项数：{len(payload['missing_artifacts'])}",
        "",
        "## 产物索引",
        "",
        f"- 批次目录：`{payload['batch_dir']}`",
        f"- render_manifest：`{payload['render_manifest'] or '-'}`",
        f"- 源视频：{len(payload['source_videos'])} 条",
        f"- 成片：{len(payload['rendered_videos'])} 条",
        f"- 去重报告：`{(payload.get('dedup_report') or {}).get('md') or '-'}`",
        f"- 包装素材清单：`{payload['visual_package_manifest'] or '-'}`",
        f"- 包装成片：{len(payload['packaged_videos'])} 条",
        f"- 社媒增强清单：`{payload['social_clip_manifest'] or '-'}`",
        f"- 发布队列：{len(payload['publish_queue_rows'])} 行",
        f"- 发布回写：{len(payload['publish_status_rows'])} 行",
        "",
        "## 重做与对比",
        "",
    ]
    if payload["high_similarity_rework"]:
        for item in payload["high_similarity_rework"]:
            lines.append(f"- `{Path(item['rework_batch_dir']).name}`：{item.get('comparison_conclusion') or '未生成对比结论'}")
            lines.append(f"  - 重做报告：`{item.get('rework_report_md') or item.get('rework_report_json') or '-'}`")
            lines.append(f"  - 对比报告：`{item.get('comparison_md') or item.get('comparison_json') or '-'}`")
    else:
        lines.append("- 暂无高相似重做记录。")
    lines.extend(["", "## 成片追溯示例", ""])
    lines.extend(_trace_example_lines(payload))
    lines.extend(["", "## 缺失产物", ""])
    if payload["missing_artifacts"]:
        for item in payload["missing_artifacts"]:
            lines.append(f"- {item}")
    else:
        lines.append("- 无")
    lines.append("")
    return "\n".join(lines)


def _trace_example_lines(payload: dict[str, Any]) -> list[str]:
    rendered = payload.get("rendered_videos") or []
    if not rendered:
        return ["- 暂无成片。"]
    video = rendered[0]
    manifest_rows = _safe_json_rows(payload.get("render_manifest", ""))
    manifest_row = _match_row_by_path(manifest_rows, "output", video)
    source = manifest_row.get("source", "") if manifest_row else ""
    dedup_level = _dedup_level_for_video(payload.get("dedup_report"), video)
    release_status = _release_status_for_video(payload.get("release_status") or [], video)
    queue_row = _match_row_by_path(payload.get("publish_queue_rows") or [], "video_path", video)
    queue_label = queue_row.get("status", "") if queue_row else "-"
    publish_row = _publish_status_for_video(payload.get("publish_status_rows") or [], video)
    publish_label = _format_publish_status(publish_row) if publish_row else "-"
    return [
        f"- source：`{source or '-'}`",
        f"- 成片：`{video}`",
        f"- 去重等级：{dedup_level}",
        f"- 流转状态：{release_status}",
        f"- 发布队列行：{queue_label}",
        f"- 发布回流状态：{publish_label}",
    ]


def _safe_json_rows(path_text: str) -> list[dict[str, Any]]:
    if not path_text:
        return []
    try:
        payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def _match_row_by_path(rows: list[dict[str, Any]], key: str, value: str) -> dict[str, Any]:
    target = Path(value).name.lower()
    for row in rows:
        candidate = str(row.get(key, "") or "")
        if candidate and Path(candidate).name.lower() == target:
            return row
    return {}


def _dedup_level_for_video(dedup_report: dict[str, Any] | None, video: str) -> str:
    if not dedup_report or not dedup_report.get("json"):
        return "-"
    try:
        payload = json.loads(Path(dedup_report["json"]).read_text(encoding="utf-8"))
    except Exception:
        return "-"
    levels = {"high": 3, "medium": 2, "low": 1}
    best = ""
    target = Path(video).name.lower()
    for pair in payload.get("pairs", []):
        left = Path(str(pair.get("left", ""))).name.lower()
        right = Path(str(pair.get("right", ""))).name.lower()
        if target not in {left, right}:
            continue
        level = str(pair.get("level", "") or "")
        if levels.get(level, 0) > levels.get(best, 0):
            best = level
    return best or "-"


def _release_status_for_video(rows: list[dict[str, str]], video: str) -> str:
    target = Path(video).name
    for row in rows:
        if row.get("video") == target:
            return row.get("status") or "unknown"
    return "unknown"


def _publish_status_for_video(rows: list[dict[str, str]], video: str) -> dict[str, str]:
    target = Path(video).name.lower()
    for row in rows:
        names = {
            Path(str(row.get("file", "") or "")).name.lower(),
            Path(str(row.get("matched_path", "") or "")).name.lower(),
        }
        if target in names:
            return row
    return {}


def _format_publish_status(row: dict[str, str]) -> str:
    status = row.get("publish_status") or row.get("status") or "-"
    platform = row.get("platform") or "-"
    duplicate = row.get("duplicate_flag") or "-"
    play = row.get("play_count") or "-"
    return f"{platform}/{status}/雷同={duplicate}/播放={play}"
