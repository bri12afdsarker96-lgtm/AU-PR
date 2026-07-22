from __future__ import annotations

import csv
import json
import shutil
from .proc import run_silent
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .editing_engine import StoryboardShot, _probe_duration, _trim_shot
from .models import DIR_A, DIR_ASSEMBLED, DIR_AUTO_EDIT_OUTPUT, DIR_SCENE_DETECT, ProjectConfig, VIDEO_EXTENSIONS
from .project import iter_media


@dataclass
class AssemblePlanItem:
    order: int
    file: str
    source: Path
    start: float
    end: float
    duration: float
    segment: Path | None = None


@dataclass
class AssembleResult:
    output_path: Path
    manifest_csv: Path
    manifest_json: Path
    segment_dir: Path
    items: list[AssemblePlanItem]
    total_seconds: float


def assemble_clips(
    config: ProjectConfig,
    plan: str | Path | None = None,
    input_dir: str | Path | None = None,
    name: str | None = None,
    keep_segments: bool = False,
) -> AssembleResult:
    items = _load_plan_items(config, Path(plan) if plan else None, Path(input_dir) if input_dir else None)
    if not items:
        raise RuntimeError("没有可整合的片段。请提供清单 CSV 或包含视频的片段目录。")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _safe_name(name or f"预告_{stamp}")
    output_dir = config.root / DIR_ASSEMBLED
    output_dir.mkdir(parents=True, exist_ok=True)
    segment_dir = output_dir / f"{safe_name}_segments_{stamp}"
    segment_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / (safe_name if safe_name.lower().endswith(".mp4") else f"{safe_name}.mp4")
    manifest_csv = output_dir / f"整合清单_{Path(safe_name).stem}.csv"
    manifest_json = output_dir / f"整合清单_{Path(safe_name).stem}.json"

    original_fps = config.edit.fps
    config.edit.fps = 30
    try:
        for item in items:
            segment = segment_dir / f"{item.order:03d}_{_safe_name(item.source.stem)}.mp4"
            shot = StoryboardShot(
                shot_id=f"A{item.order:03d}",
                source=item.source,
                start=item.start,
                duration=item.duration,
                text="",
                voice_type="原声",
            )
            _trim_shot(config, shot, segment)
            item.segment = segment
    finally:
        config.edit.fps = original_fps

    concat_list = _concat_segments(config, [item.segment for item in items if item.segment], output_path)
    _write_manifest(manifest_csv, manifest_json, items, output_path)
    if not keep_segments:
        _cleanup_success_artifacts(segment_dir, concat_list)
    return AssembleResult(
        output_path=output_path,
        manifest_csv=manifest_csv,
        manifest_json=manifest_json,
        segment_dir=segment_dir,
        items=items,
        total_seconds=round(sum(item.duration for item in items), 3),
    )


def _load_plan_items(config: ProjectConfig, plan: Path | None, input_dir: Path | None) -> list[AssemblePlanItem]:
    if plan:
        plan_path = _resolve_user_path(config, plan)
        if not plan_path.exists():
            raise FileNotFoundError(f"整合清单不存在：{plan}")
        with plan_path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
        items: list[AssemblePlanItem] = []
        for index, row in enumerate(rows, start=1):
            file_text = _pick(row, ["file", "文件", "片段", "素材", "video"])
            if not file_text:
                raise RuntimeError(f"整合清单第 {index} 行缺少 file 字段。")
            source = _resolve_clip_path(config, file_text, plan_path.parent, input_dir)
            if not source.exists():
                raise FileNotFoundError(f"整合清单第 {index} 行片段不存在：{file_text}")
            start = _parse_seconds(_pick(row, ["start", "开始", "起点"], "0"), 0.0)
            end_value = _pick(row, ["end", "结束", "终点"], "")
            source_duration = _probe_duration(config, source) or 0.0
            end = _parse_seconds(end_value, source_duration) if end_value else source_duration
            if end <= start:
                raise RuntimeError(f"整合清单第 {index} 行结束时间必须大于开始时间：{file_text}")
            items.append(
                AssemblePlanItem(
                    order=int(_parse_seconds(_pick(row, ["order", "排序", "序号"], str(index)), float(index))),
                    file=file_text,
                    source=source,
                    start=max(0.0, start),
                    end=end,
                    duration=max(0.3, end - start),
                )
            )
        return sorted(items, key=lambda item: item.order)

    source_dir = _resolve_user_path(config, input_dir) if input_dir else config.root / DIR_AUTO_EDIT_OUTPUT
    clips = iter_media(source_dir, VIDEO_EXTENSIONS)
    if not clips:
        raise FileNotFoundError(f"片段目录没有视频文件：{source_dir}")
    result: list[AssemblePlanItem] = []
    for index, source in enumerate(clips, start=1):
        duration = _probe_duration(config, source) or 0.0
        if duration <= 0:
            raise RuntimeError(f"无法读取片段时长：{source}")
        result.append(
            AssemblePlanItem(
                order=index,
                file=source.name,
                source=source,
                start=0.0,
                end=duration,
                duration=duration,
            )
        )
    return result


def _resolve_clip_path(config: ProjectConfig, token: str, plan_dir: Path, input_dir: Path | None) -> Path:
    raw = Path(str(token).strip().strip('"'))
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(plan_dir / raw)
        if input_dir:
            candidates.append(_resolve_user_path(config, input_dir) / raw)
        candidates.append(config.root / raw)
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    name = raw.name.lower()
    search_roots = [
        config.root / DIR_SCENE_DETECT,
        config.root / DIR_AUTO_EDIT_OUTPUT,
        config.root / DIR_A,
    ]
    if input_dir:
        search_roots.insert(0, _resolve_user_path(config, input_dir))
    for root in search_roots:
        if not root.exists():
            continue
        for item in iter_media(root, VIDEO_EXTENSIONS):
            if item.name.lower() == name or item.stem.lower() == raw.stem.lower():
                return item
    return raw


def _resolve_user_path(config: ProjectConfig, path: Path | None) -> Path:
    if path is None:
        return config.root
    if path.is_absolute():
        return path
    direct = Path.cwd() / path
    if direct.exists():
        return direct
    return config.root / path


def _concat_segments(config: ProjectConfig, segments: list[Path | None], output: Path) -> Path:
    clean_segments = [Path(segment) for segment in segments if segment]
    if not clean_segments:
        raise RuntimeError("没有可合并的整合片段。")
    concat_list = output.parent / f"{output.stem}_concat.txt"
    concat_list.write_text("\n".join(_concat_line(path) for path in clean_segments), encoding="utf-8")
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    command = [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list),
        "-c:v",
        "libx264",
        "-preset",
        config.recipe.preset,
        "-crf",
        str(config.recipe.crf),
        "-c:a",
        "aac",
        "-r",
        "30",
        "-pix_fmt",
        "yuv420p",
        str(output),
    ]
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr[-3000:] if completed.stderr else completed.stdout[-3000:]
        raise RuntimeError(detail.strip() or "整合成片合成失败")
    return concat_list


def _cleanup_success_artifacts(segment_dir: Path, concat_list: Path) -> None:
    if concat_list.exists():
        concat_list.unlink()
    if segment_dir.exists():
        shutil.rmtree(segment_dir)


def _write_manifest(csv_path: Path, json_path: Path, items: list[AssemblePlanItem], output_path: Path) -> None:
    rows: list[dict[str, Any]] = []
    for item in items:
        row = asdict(item)
        row["source"] = str(item.source)
        row["segment"] = str(item.segment or "")
        row["output"] = str(output_path)
        rows.append(row)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = ["order", "file", "source", "start", "end", "duration", "segment", "output"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps({"output": str(output_path), "items": rows}, ensure_ascii=False, indent=2), encoding="utf-8")


def _pick(row: dict[str, Any], names: list[str], default: str = "") -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _parse_seconds(value: Any, default: float) -> float:
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).strip().replace("秒", ""))
    except ValueError:
        return default


def _concat_line(path: Path) -> str:
    normalized = str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
    return f"file '{normalized}'"


def _safe_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid else char for char in str(name)).strip()
    return cleaned or datetime.now().strftime("预告_%Y%m%d_%H%M%S")
