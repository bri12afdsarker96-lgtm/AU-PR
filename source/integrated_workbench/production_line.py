from __future__ import annotations

import csv
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .assemble import AssembleResult, assemble_clips
from .models import DIR_READY, DIR_REVIEW, ProjectConfig
from .publish_feedback import generate_feedback_template
from .project import log_line
from .video_engine import HighSimilarityRegenerateResult, render_high_similarity_replacements, render_project


@dataclass
class ProductionLineResult:
    input_source: Path
    assembled: AssembleResult | None
    batch_dir: Path
    ready_dir: Path | None
    production_csv: Path
    report_json: Path
    report_md: Path
    rendered: list[Path]
    ready_videos: list[Path]
    account_count: int
    remaining_high_count: int
    comparison_conclusion: str = ""
    feedback_template: Path | None = None


def produce(
    config: ProjectConfig,
    source: str | Path | None = None,
    plan: str | Path | None = None,
    input_dir: str | Path | None = None,
    copies: int = 3,
    account_labels: list[str] | None = None,
    dedup_level: str = "标准",
    name: str | None = None,
    resume: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> ProductionLineResult:
    if copies <= 0:
        raise ValueError("账号数必须大于 0。")

    assembled: AssembleResult | None = None
    if plan or input_dir:
        assembled = assemble_clips(config, plan=plan, input_dir=input_dir, name=name)
        input_source = assembled.output_path
    else:
        if not source:
            raise ValueError("一键产线需要提供成片路径、整合清单或片段目录。")
        input_source = _resolve_path(config, Path(source))
        if not input_source.exists():
            raise FileNotFoundError(f"成片不存在：{source}")

    slots = _account_slots(copies, account_labels)
    render_level = _dedup_level(dedup_level)
    previous_level = config.recipe.dedup_level
    config.recipe.dedup_level = render_level
    # opt-in 断点续跑：同一产线（同 name/源）用确定性目录+状态文件，重跑时跳过已完成的份。
    render_output_dir = None
    resume_state_path = None
    if resume:
        stable = _safe_batch_name(name or input_source.stem)
        render_output_dir = config.root / DIR_REVIEW / f"续跑_{stable}"
        render_output_dir.mkdir(parents=True, exist_ok=True)
        resume_state_path = render_output_dir / ".batch_resume.json"
    try:
        rendered = render_project(
            config,
            copies=copies,
            source_files=[input_source],
            source_label="产线成片",
            account_slots=slots,
            output_dir=render_output_dir,
            resume_state_path=resume_state_path,
            progress=progress,
        )
    finally:
        config.recipe.dedup_level = previous_level

    batch_dir = rendered[0].parent if rendered else config.root / DIR_REVIEW
    original_manifest = _read_manifest(batch_dir)
    dedup_report = _latest_dedup_report(batch_dir)
    original_high = _high_outputs_from_report(config, batch_dir, dedup_report) if dedup_report else set()
    clean_originals = [video for video in rendered if _path_key(video) not in original_high]

    rework: HighSimilarityRegenerateResult | None = None
    rework_manifest: list[dict[str, str]] = []
    rework_high: set[str] = set()
    rework_triggered = bool(original_high and dedup_report)
    rework_failed = False
    rework_error = ""
    if original_high and dedup_report:
        try:
            rework = render_high_similarity_replacements(config, report_path=dedup_report, dedup_level="strong", progress=progress)
            rework_manifest = _read_manifest(rework.output_dir)
            rework_report = _latest_dedup_report(rework.output_dir)
            rework_high = _high_outputs_from_report(config, rework.output_dir, rework_report) if rework_report else set()
        except Exception as exc:
            rework_failed = True
            rework_error = str(exc)[-500:]
            log_line(config, f"高相似自动重做失败，产线继续放行 clean originals：{rework_error}")
            rework = None

    clean_reworked = [video for video in (rework.rendered if rework else []) if _path_key(video) not in rework_high]
    remaining_high_count = len(rework_high) if rework else len(original_high)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    promote_to_ready = config.workflow_mode != "full"
    promotion = "promoted" if promote_to_ready else "skipped_full_mode"
    ready_dir = config.root / DIR_READY / f"产线_{stamp}" if promote_to_ready else None
    report_dir = ready_dir if ready_dir else batch_dir
    report_dir.mkdir(parents=True, exist_ok=True)

    high_slots = _slots_for_high_outputs(original_manifest, original_high)
    rows: list[dict[str, str]] = []
    ready_videos: list[Path] = []
    for video in clean_originals:
        manifest_row = _row_for_output(original_manifest, video)
        output = _copy_ready(video, ready_dir) if ready_dir else video
        if ready_dir:
            ready_videos.append(output)
        rows.append(_production_row(output, video, manifest_row, render_level, "clean"))

    for index, video in enumerate(clean_reworked):
        manifest_row = _row_for_output(rework_manifest, video)
        if index < len(high_slots):
            manifest_row = dict(manifest_row)
            manifest_row["account_slot"] = high_slots[index]
        output = _copy_ready(video, ready_dir) if ready_dir else video
        if ready_dir:
            ready_videos.append(output)
        rows.append(_production_row(output, video, manifest_row, "strong", "reworked"))

    production_csv = report_dir / "生产清单.csv"
    _write_production_csv(production_csv, rows)
    feedback_template: Path | None = None
    feedback_template_error = ""
    if promote_to_ready and ready_dir:
        try:
            feedback_template = generate_feedback_template(config, ready_dir=ready_dir).template_csv
        except Exception as exc:
            feedback_template_error = str(exc)
            log_line(config, f"发布回流模板生成失败：{feedback_template_error}")
    report_payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "workflow_mode": config.workflow_mode,
        "input": str(input_source),
        "assembly": {
            "used": assembled is not None,
            "output": str(assembled.output_path) if assembled else "",
            "manifest_csv": str(assembled.manifest_csv) if assembled else "",
            "segments": len(assembled.items) if assembled else 0,
        },
        "batch": str(batch_dir),
        "account_count": copies,
        "account_slots": slots,
        "dedup_level": render_level,
        "fingerprint_report": str(dedup_report or ""),
        "fingerprint_high_outputs": len(original_high),
        "rework_triggered": rework_triggered,
        "rework_failed": rework_failed,
        "rework_error": rework_error,
        "rework_batch": str(rework.output_dir) if rework else "",
        "comparison_conclusion": rework.comparison_conclusion if rework else "",
        "promotion": promotion,
        "ready_dir": str(ready_dir or ""),
        "ready_count": len(ready_videos),
        "ready_videos": [str(path) for path in ready_videos],
        "remaining_high_count": remaining_high_count,
        "feedback_template": str(feedback_template or ""),
        "feedback_template_error": feedback_template_error,
    }
    report_json = report_dir / "产线报告.json"
    report_md = report_dir / "产线报告.md"
    report_json.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_md.write_text(_report_markdown(report_payload), encoding="utf-8")

    return ProductionLineResult(
        input_source=input_source,
        assembled=assembled,
        batch_dir=batch_dir,
        ready_dir=ready_dir,
        production_csv=production_csv,
        report_json=report_json,
        report_md=report_md,
        rendered=rendered,
        ready_videos=ready_videos,
        account_count=copies,
        remaining_high_count=remaining_high_count,
        comparison_conclusion=report_payload["comparison_conclusion"],
        feedback_template=feedback_template,
    )


def _dedup_level(label: str) -> str:
    normalized = (label or "").strip().lower()
    if normalized in {"增强", "strong", "enhanced"}:
        return "strong"
    if normalized in {"轻量", "light"}:
        return "light"
    return "balanced"


def _safe_batch_name(text: str) -> str:
    bad = '\\/:*?"<>|'
    safe = "".join(ch for ch in str(text) if ch not in bad).strip()
    return safe or "产线"


def _account_slots(copies: int, labels: list[str] | None) -> list[str]:
    clean_labels = [label.strip() for label in labels or [] if label and label.strip()]
    if len(clean_labels) >= copies:
        return clean_labels[:copies]
    result = clean_labels[:]
    for index in range(len(result) + 1, copies + 1):
        result.append(f"A{index:02d}")
    return result


def _resolve_path(config: ProjectConfig, path: Path) -> Path:
    if path.is_absolute():
        return path
    direct = Path.cwd() / path
    if direct.exists():
        return direct
    return config.root / path


def _latest_dedup_report(batch_dir: Path) -> Path | None:
    report_dir = batch_dir / "去重指纹报告"
    if not report_dir.exists():
        return None
    reports = sorted(report_dir.glob("去重指纹报告_*.json"), key=lambda item: (item.stat().st_mtime, item.name))
    return reports[-1] if reports else None


def _high_outputs_from_report(config: ProjectConfig, batch_dir: Path, report_path: Path | None) -> set[str]:
    if not report_path or not report_path.exists():
        return set()
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    result: set[str] = set()
    for pair in payload.get("pairs", []):
        if str(pair.get("level", "")).lower() != "high":
            continue
        for key in ["left", "right"]:
            result.add(_path_key(_resolve_report_path(config, batch_dir, str(pair.get(key, "")))))
    return result


def _resolve_report_path(config: ProjectConfig, batch_dir: Path, value: str) -> Path:
    candidate = Path(value)
    if candidate.exists():
        return candidate
    for root in [batch_dir, config.root / DIR_REVIEW, config.root]:
        direct = root / candidate.name
        if direct.exists():
            return direct
    return candidate


def _read_manifest(batch_dir: Path) -> list[dict[str, str]]:
    path = batch_dir / "render_manifest.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [dict(row) for row in data] if isinstance(data, list) else []


def _row_for_output(rows: list[dict[str, str]], output: Path) -> dict[str, str]:
    key = _path_key(output)
    for row in rows:
        if _path_key(Path(str(row.get("output", "")))) == key:
            return row
    for row in rows:
        if Path(str(row.get("output", ""))).name.lower() == output.name.lower():
            return row
    return {}


def _slots_for_high_outputs(rows: list[dict[str, str]], high_outputs: set[str]) -> list[str]:
    slots: list[str] = []
    for row in rows:
        output = Path(str(row.get("output", "")))
        if _path_key(output) in high_outputs:
            slots.append(str(row.get("account_slot", "") or f"A{len(slots) + 1:02d}"))
    return slots


def _production_row(copied: Path, original: Path, manifest_row: dict[str, str], dedup_level: str, status: str) -> dict[str, str]:
    return {
        "account_slot": str(manifest_row.get("account_slot", "")),
        "file": str(copied),
        "source": str(manifest_row.get("source", original)),
        "batch_id": str(manifest_row.get("batch_id", original.parent.name)),
        "dedup_level": dedup_level,
        "similarity_status": status,
        "variant_border": str(manifest_row.get("dedup_border_style", "")),
        "variant_zoom": str(manifest_row.get("dedup_zoom_path", "")),
        "variant_speed": str(manifest_row.get("dedup_speed_factor", "")),
        "variant_trim": f"{manifest_row.get('dedup_intro_trim', '')}/{manifest_row.get('dedup_outro_trim', '')}",
        "original_file": str(original),
    }


def _write_production_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = [
        "account_slot",
        "file",
        "source",
        "batch_id",
        "dedup_level",
        "similarity_status",
        "variant_border",
        "variant_zoom",
        "variant_speed",
        "variant_trim",
        "original_file",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _copy_ready(video: Path, ready_dir: Path) -> Path:
    target = ready_dir / video.name
    if target.exists():
        stem = video.stem
        suffix = video.suffix
        for index in range(2, 1000):
            candidate = ready_dir / f"{stem}_{index:02d}{suffix}"
            if not candidate.exists():
                target = candidate
                break
    shutil.copy2(video, target)
    return target


def _report_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 短剧矩阵产线报告",
        "",
        f"- 输入：`{payload['input']}`",
        f"- 账号数：{payload['account_count']}",
        f"- 二创批次：`{payload['batch']}`",
        f"- 指纹报告：`{payload['fingerprint_report'] or '-'}`",
        f"- 原 high 成片数：{payload['fingerprint_high_outputs']}",
        f"- 触发重做：{payload['rework_triggered']}",
        f"- 重做失败：{payload.get('rework_failed', False)}",
        f"- 对比结论：{payload['comparison_conclusion'] or '-'}",
        f"- 放行模式：{payload.get('promotion') or '-'}",
        f"- 合格输出：{payload['ready_count']}",
        f"- 遗留 high：{payload['remaining_high_count']}",
        f"- 合格目录：`{payload['ready_dir'] or '-'}`",
        f"- 回流模板：`{payload.get('feedback_template') or '-'}`",
        "",
    ]
    if payload.get("promotion") == "skipped_full_mode":
        lines.insert(-1, "- 完整模式：成片停留在中间结果目录，未自动进入产线待发布目录。")
    if payload.get("rework_error"):
        lines.insert(-1, f"- 重做错误：{payload['rework_error']}")
    if payload.get("feedback_template_error"):
        lines.insert(-1, f"- 回流模板错误：{payload['feedback_template_error']}")
    return "\n".join(lines)


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()
