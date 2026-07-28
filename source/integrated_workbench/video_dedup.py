from __future__ import annotations

import csv
import json
import random
import shutil
from .proc import run_silent
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .audio_fingerprint import DEFAULT_AUDIO_SIMILARITY_THRESHOLD, collect_audio_report
from .models import DIR_REVIEW, ProjectConfig


@dataclass
class DedupVariant:
    level: str
    hflip: bool
    foreground_scale: float
    x_shift: int
    y_shift: int
    background_blur: float
    brightness: float
    contrast: float
    saturation: float
    gamma: float
    hue_degrees: float
    noise_strength: int
    sharpen: float
    border_style: str = "none"
    border_width_ratio: float = 0.0
    border_color: str = "black"
    zoom_path: str = "none"
    zoom_amount: float = 0.0
    pan_direction: str = "none"
    pan_amount: float = 0.0
    speed_factor: float = 1.0
    intro_trim: float = 0.0
    outro_trim: float = 0.0


@dataclass
class VideoHashItem:
    video: str
    duration: float
    hashes: list[str]
    error: str = ""


@dataclass
class SimilarityPair:
    left: str
    right: str
    similarity: float
    level: str


@dataclass
class DedupReport:
    markdown_path: Path
    json_path: Path
    csv_path: Path
    high_similarity_count: int
    pair_count: int
    audio_high_similarity_count: int = 0
    audio_engine: str = ""


@dataclass
class HighSimilarityIssue:
    left: str
    right: str
    similarity: float
    level: str = "high"


@dataclass
class HighSimilaritySelection:
    report_path: Path
    batch_dir: Path
    manifest_path: Path | None
    high_pairs: list[HighSimilarityIssue]
    high_outputs: list[Path]
    source_files: list[Path]
    missing_sources: list[str]
    mode: str = ""


@dataclass
class PairComparison:
    old_left: str
    old_right: str
    old_similarity: float
    new_left: str
    new_right: str
    new_similarity: float | None
    delta: float | None
    new_level: str
    aligned: bool
    reason: str


@dataclass
class ReworkComparisonReport:
    generated_at: str
    old_report: str
    rework_batch: str
    new_report: str
    pairs: list[PairComparison]
    old_high_count: int
    new_high_count: int
    resolved_count: int
    unaligned_count: int
    conclusion: str


PROFILE_RANGES = {
    "light": {
        "scale": (0.95, 1.00),
        "shift": 8,
        "blur": (14.0, 20.0),
        "brightness": (-0.025, 0.025),
        "contrast": (0.98, 1.04),
        "saturation": (0.96, 1.06),
        "gamma": (0.98, 1.03),
        "hue": (-2.0, 2.0),
        "noise": (0, 2),
        "sharpen": (0.0, 0.25),
        "hflip_rate": 0.15,
        "border_rate": 0.12,
        "zoom_rate": 0.10,
        "pan_rate": 0.0,
        "speed_rate": 0.10,
        "trim_rate": 0.06,
        "border_width": (0.008, 0.016),
        "zoom_amount": (1.025, 1.035),
        "pan_amount": (0.0, 0.0),
        "speed": (0.985, 1.015),
        "trim": (0.05, 0.18),
    },
    "balanced": {
        "scale": (0.90, 1.02),
        "shift": 18,
        "blur": (18.0, 30.0),
        "brightness": (-0.045, 0.045),
        "contrast": (0.94, 1.09),
        "saturation": (0.90, 1.14),
        "gamma": (0.95, 1.06),
        "hue": (-4.0, 4.0),
        "noise": (1, 5),
        "sharpen": (0.05, 0.45),
        "hflip_rate": 0.35,
        "border_rate": 0.48,
        "zoom_rate": 0.52,
        "pan_rate": 0.50,
        "speed_rate": 0.36,
        "trim_rate": 0.34,
        "border_width": (0.012, 0.026),
        "zoom_amount": (1.03, 1.06),
        "pan_amount": (0.005, 0.014),
        "speed": (0.96, 1.04),
        "trim": (0.10, 0.45),
    },
    "strong": {
        "scale": (0.84, 1.06),
        "shift": 32,
        "blur": (24.0, 42.0),
        "brightness": (-0.065, 0.065),
        "contrast": (0.90, 1.15),
        "saturation": (0.82, 1.24),
        "gamma": (0.92, 1.10),
        "hue": (-7.0, 7.0),
        "noise": (2, 8),
        "sharpen": (0.10, 0.70),
        "hflip_rate": 0.50,
        "border_rate": 0.72,
        "zoom_rate": 1.00,
        "pan_rate": 0.82,
        "speed_rate": 0.58,
        "trim_rate": 0.54,
        "border_width": (0.018, 0.040),
        "zoom_amount": (1.05, 1.08),
        "pan_amount": (0.008, 0.020),
        "speed": (0.92, 1.08),
        "trim": (0.20, 0.80),
    },
}


def make_dedup_variant(level: str, seed: str) -> DedupVariant:
    selected = level if level in PROFILE_RANGES else "balanced"
    ranges = PROFILE_RANGES[selected]
    rng = random.Random(seed)
    shift = int(ranges["shift"])
    noise_min, noise_max = ranges["noise"]
    dimensions = {
        "border": rng.random() < float(ranges["border_rate"]),
        "zoom": rng.random() < float(ranges["zoom_rate"]),
        "speed": rng.random() < float(ranges["speed_rate"]),
        "trim": rng.random() < float(ranges["trim_rate"]),
    }
    if selected == "strong":
        dimensions["zoom"] = True
        missing = [name for name, enabled in dimensions.items() if not enabled]
        rng.shuffle(missing)
        while sum(1 for enabled in dimensions.values() if enabled) < 2 and missing:
            dimensions[missing.pop()] = True
    border_style = rng.choice(["solid", "blur_extend"]) if dimensions["border"] else "none"
    zoom_path = rng.choice(["in", "out"]) if dimensions["zoom"] else "none"
    pan_enabled = dimensions["zoom"] and rng.random() < float(ranges["pan_rate"])
    pan_direction = rng.choice(["left", "right", "up", "down"]) if pan_enabled else "none"
    speed_factor = round(rng.uniform(*ranges["speed"]), 4) if dimensions["speed"] else 1.0
    if speed_factor == 1.0:
        speed_factor = 1.0
    trim_seconds = round(rng.uniform(*ranges["trim"]), 3) if dimensions["trim"] else 0.0
    return DedupVariant(
        level=selected,
        hflip=rng.random() < float(ranges["hflip_rate"]),
        foreground_scale=round(rng.uniform(*ranges["scale"]), 4),
        x_shift=rng.randint(-shift, shift),
        y_shift=rng.randint(-shift, shift),
        background_blur=round(rng.uniform(*ranges["blur"]), 2),
        brightness=round(rng.uniform(*ranges["brightness"]), 4),
        contrast=round(rng.uniform(*ranges["contrast"]), 4),
        saturation=round(rng.uniform(*ranges["saturation"]), 4),
        gamma=round(rng.uniform(*ranges["gamma"]), 4),
        hue_degrees=round(rng.uniform(*ranges["hue"]), 3),
        noise_strength=int(rng.randint(int(noise_min), int(noise_max))),
        sharpen=round(rng.uniform(*ranges["sharpen"]), 3),
        border_style=border_style,
        border_width_ratio=round(rng.uniform(*ranges["border_width"]), 4) if dimensions["border"] else 0.0,
        border_color=rng.choice(["black", "white", "0x111111", "0xf2f2f2"]) if dimensions["border"] else "black",
        zoom_path=zoom_path,
        zoom_amount=round(rng.uniform(*ranges["zoom_amount"]), 4) if dimensions["zoom"] else 0.0,
        pan_direction=pan_direction,
        pan_amount=round(rng.uniform(*ranges["pan_amount"]), 4) if pan_enabled else 0.0,
        speed_factor=speed_factor,
        intro_trim=trim_seconds,
        outro_trim=round(trim_seconds * rng.uniform(0.5, 1.1), 3) if dimensions["trim"] else 0.0,
    )


def variant_manifest(variant: DedupVariant) -> dict[str, str]:
    data = asdict(variant)
    return {f"dedup_{key}": str(value) for key, value in data.items()}


def video_eq_filter(variant: DedupVariant) -> str:
    return (
        f"eq=brightness={variant.brightness}:contrast={variant.contrast}:"
        f"saturation={variant.saturation}:gamma={variant.gamma},"
        f"hue=h={variant.hue_degrees}"
    )


def foreground_filter_suffix(variant: DedupVariant) -> str:
    filters: list[str] = []
    if variant.hflip:
        filters.append("hflip")
    filters.append(video_eq_filter(variant))
    if variant.noise_strength > 0:
        filters.append(f"noise=alls={variant.noise_strength}:allf=t+u")
    if variant.sharpen > 0:
        filters.append(f"unsharp=5:5:{variant.sharpen}:3:3:0.0")
    return ",".join(filters)


def write_dedup_report(
    config: ProjectConfig,
    videos: list[Path],
    output_dir: Path,
    threshold: float = 0.92,
) -> DedupReport:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_dir = output_dir / "去重指纹报告"
    frame_dir = report_dir / "sample_frames"
    report_dir.mkdir(parents=True, exist_ok=True)
    frame_dir.mkdir(parents=True, exist_ok=True)

    items = [_video_hash_item(config, video, frame_dir) for video in videos]
    pairs = _pairwise_similarity(items, threshold)
    high_count = sum(1 for item in pairs if item.level == "high")

    json_path = report_dir / f"去重指纹报告_{stamp}.json"
    csv_path = report_dir / f"去重相似度明细_{stamp}.csv"
    markdown_path = report_dir / f"去重指纹报告_{stamp}.md"

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "threshold": threshold,
        "items": [asdict(item) for item in items],
        "pairs": [asdict(item) for item in pairs],
        "summary": {"videos": len(items), "pairs": len(pairs), "high_similarity": high_count},
    }

    audio_high_count = 0
    audio_engine = ""
    try:
        audio_report = collect_audio_report(config, videos, DEFAULT_AUDIO_SIMILARITY_THRESHOLD)
        payload["audio"] = audio_report.to_payload()
        audio_high_count = audio_report.high_similarity_count
        audio_engine = audio_report.engine
        _write_audio_pairs_csv(report_dir / f"音频相似度明细_{stamp}.csv", audio_report)
    except Exception as exc:
        payload["audio"] = {"error": str(exc)}

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_pairs_csv(csv_path, pairs)
    markdown_path.write_text(_markdown(payload), encoding="utf-8")
    shutil.rmtree(frame_dir, ignore_errors=True)
    return DedupReport(markdown_path, json_path, csv_path, high_count, len(pairs), audio_high_count, audio_engine)


def latest_dedup_report(config: ProjectConfig) -> Path | None:
    review_root = config.root / DIR_REVIEW
    if not review_root.exists():
        return None
    reports = [
        path
        for path in review_root.rglob("去重指纹报告_*.json")
        if path.is_file() and path.parent.name == "去重指纹报告"
    ]
    if not reports:
        return None
    return max(reports, key=lambda path: (path.stat().st_mtime, str(path)))


def select_high_similarity_sources(config: ProjectConfig, report_path: str | Path | None = None) -> HighSimilaritySelection:
    selected_report = Path(report_path) if report_path else latest_dedup_report(config)
    if selected_report is None or not selected_report.exists():
        raise FileNotFoundError("没有找到去重指纹报告。请先生成二创成片。")

    payload = json.loads(selected_report.read_text(encoding="utf-8"))
    batch_dir = selected_report.parent.parent
    manifest_path = batch_dir / "render_manifest.json"
    manifest_rows = _read_render_manifest(manifest_path)
    output_map, output_name_map, mode = _build_manifest_maps(config, batch_dir, manifest_rows)

    high_pairs = [
        HighSimilarityIssue(
            left=str(pair.get("left", "")),
            right=str(pair.get("right", "")),
            similarity=float(pair.get("similarity", 0.0) or 0.0),
            level=str(pair.get("level", "high") or "high"),
        )
        for pair in payload.get("pairs", [])
        if str(pair.get("level", "")).lower() == "high"
    ]

    high_outputs: list[Path] = []
    source_files: list[Path] = []
    missing_sources: list[str] = []
    seen_outputs: set[str] = set()
    search_roots = [batch_dir, config.root / DIR_REVIEW, config.root]

    for pair in high_pairs:
        for output_text in [pair.left, pair.right]:
            output = _resolve_existing_path(output_text, search_roots)
            output_key = _path_key(output)
            if output_key in seen_outputs:
                continue
            seen_outputs.add(output_key)
            high_outputs.append(output)

            row = output_map.get(output_key)
            if row is None:
                row = output_name_map.get(output.name.lower())
            if row is None:
                if output.exists():
                    source_files.append(output)
                    missing_sources.append(f"未找到清单映射，已临时使用高相似成片自身：{output}")
                else:
                    missing_sources.append(f"找不到高相似成片：{output_text}")
                continue

            source = _resolve_existing_path(str(row.get("source", "")), [config.root, batch_dir, batch_dir.parent])
            if source.exists():
                source_files.append(source)
            elif output.exists():
                source_files.append(output)
                missing_sources.append(f"找不到原始素材，已临时使用高相似成片自身：{row.get('source', '')}")
            else:
                missing_sources.append(f"找不到原始素材：{row.get('source', '')}")
            mode = str(row.get("mode") or mode or "")

    return HighSimilaritySelection(
        report_path=selected_report,
        batch_dir=batch_dir,
        manifest_path=manifest_path if manifest_path.exists() else None,
        high_pairs=high_pairs,
        high_outputs=high_outputs,
        source_files=source_files,
        missing_sources=missing_sources,
        mode=mode,
    )


def compare_rework_similarity(
    config: ProjectConfig,
    old_report_path: Path,
    rework_batch_dir: Path,
    threshold: float,
) -> ReworkComparisonReport:
    old_report_path = Path(old_report_path)
    rework_batch_dir = Path(rework_batch_dir)
    old_payload = json.loads(old_report_path.read_text(encoding="utf-8"))
    old_pairs = [
        HighSimilarityIssue(
            left=str(pair.get("left", "")),
            right=str(pair.get("right", "")),
            similarity=float(pair.get("similarity", 0.0) or 0.0),
            level=str(pair.get("level", "high") or "high"),
        )
        for pair in old_payload.get("pairs", [])
        if str(pair.get("level", "")).lower() == "high"
    ]

    old_batch_dir = old_report_path.parent.parent
    old_manifest = _read_render_manifest(old_batch_dir / "render_manifest.json")
    old_output_map, old_output_name_map, _old_mode = _build_manifest_maps(config, old_batch_dir, old_manifest)
    new_source_map, new_source_name_map = _build_rework_source_map(config, rework_batch_dir)
    new_source_lists, new_source_name_lists = _build_rework_source_lists(config, rework_batch_dir)
    old_source_output_order = _build_source_output_order(config, old_batch_dir, old_manifest)
    new_report_path = _latest_rework_dedup_report(rework_batch_dir)
    new_pair_map = _load_similarity_pair_map(config, rework_batch_dir, new_report_path) if new_report_path else {}

    comparisons: list[PairComparison] = []
    search_roots = [old_batch_dir, config.root / DIR_REVIEW, config.root]
    for pair in old_pairs:
        old_left = _resolve_existing_path(pair.left, search_roots)
        old_right = _resolve_existing_path(pair.right, search_roots)
        source_left = _source_for_output(config, old_batch_dir, old_left, old_output_map, old_output_name_map)
        source_right = _source_for_output(config, old_batch_dir, old_right, old_output_map, old_output_name_map)

        if source_left is None:
            comparisons.append(_unaligned_pair(pair, old_left, old_right, f"source 缺失: {old_left.name}"))
            continue
        if source_right is None:
            comparisons.append(_unaligned_pair(pair, old_left, old_right, f"source 缺失: {old_right.name}"))
            continue
        if _path_key(source_left) == _path_key(source_right):
            new_candidates = new_source_lists.get(_path_key(source_left)) or new_source_name_lists.get(source_left.name.lower()) or []
            old_candidates = old_source_output_order.get(_path_key(source_left)) or []
            old_keys = [_path_key(item) for item in old_candidates]
            try:
                left_index = old_keys.index(_path_key(old_left))
                right_index = old_keys.index(_path_key(old_right))
            except ValueError:
                comparisons.append(_unaligned_pair(pair, old_left, old_right, "同源旧成片排序缺失，无法映射新副本"))
                continue
            if max(left_index, right_index) >= len(new_candidates):
                comparisons.append(_unaligned_pair(pair, old_left, old_right, "同源重做新副本不足，无法完成 pair 级对比"))
                continue
            new_left = new_candidates[left_index]
            new_right = new_candidates[right_index]
        else:
            new_left = _lookup_rework_output(source_left, new_source_map, new_source_name_map)
            new_right = _lookup_rework_output(source_right, new_source_map, new_source_name_map)
        if new_left is None:
            comparisons.append(_unaligned_pair(pair, old_left, old_right, f"新成片缺失: {source_left.name}"))
            continue
        if new_right is None:
            comparisons.append(_unaligned_pair(pair, old_left, old_right, f"新成片缺失: {source_right.name}"))
            continue

        try:
            score = _lookup_similarity(new_left, new_right, new_pair_map)
            if score is None:
                score = _calculate_pair_similarity(config, rework_batch_dir, new_left, new_right)
            score = round(float(score), 4)
            level = _similarity_level(score, threshold)
            comparisons.append(
                PairComparison(
                    old_left=str(old_left),
                    old_right=str(old_right),
                    old_similarity=round(pair.similarity, 4),
                    new_left=str(new_left),
                    new_right=str(new_right),
                    new_similarity=score,
                    delta=round(pair.similarity - score, 4),
                    new_level=level,
                    aligned=True,
                    reason="",
                )
            )
        except Exception as exc:
            comparisons.append(
                PairComparison(
                    old_left=str(old_left),
                    old_right=str(old_right),
                    old_similarity=round(pair.similarity, 4),
                    new_left=str(new_left),
                    new_right=str(new_right),
                    new_similarity=None,
                    delta=None,
                    new_level="unaligned",
                    aligned=False,
                    reason=str(exc)[-500:],
                )
            )

    new_high_count = sum(1 for item in comparisons if item.aligned and item.new_level == "high")
    resolved_count = sum(1 for item in comparisons if item.aligned and item.new_level in {"medium", "low"})
    unaligned_count = sum(1 for item in comparisons if not item.aligned)
    conclusion = _rework_comparison_conclusion(new_high_count, unaligned_count, len(comparisons))
    return ReworkComparisonReport(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        old_report=str(old_report_path),
        rework_batch=str(rework_batch_dir),
        new_report=str(new_report_path or ""),
        pairs=comparisons,
        old_high_count=len(old_pairs),
        new_high_count=new_high_count,
        resolved_count=resolved_count,
        unaligned_count=unaligned_count,
        conclusion=conclusion,
    )


def write_rework_comparison_report(report: ReworkComparisonReport, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "高相似重做对比报告.json"
    markdown_path = output_dir / "高相似重做对比报告.md"
    json_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_rework_comparison_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _unaligned_pair(pair: HighSimilarityIssue, old_left: Path, old_right: Path, reason: str) -> PairComparison:
    return PairComparison(
        old_left=str(old_left),
        old_right=str(old_right),
        old_similarity=round(pair.similarity, 4),
        new_left="",
        new_right="",
        new_similarity=None,
        delta=None,
        new_level="unaligned",
        aligned=False,
        reason=reason,
    )


def _source_for_output(
    config: ProjectConfig,
    batch_dir: Path,
    output: Path,
    output_map: dict[str, dict],
    output_name_map: dict[str, dict],
) -> Path | None:
    row = output_map.get(_path_key(output)) or output_name_map.get(output.name.lower())
    if row is None:
        return None
    source_text = str(row.get("source", "") or "").strip()
    if not source_text:
        return None
    return _resolve_existing_path(source_text, [config.root, batch_dir, batch_dir.parent])


def _build_rework_source_map(config: ProjectConfig, rework_batch_dir: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    rows = _read_render_manifest(rework_batch_dir / "render_manifest.json")
    source_map: dict[str, Path] = {}
    source_name_map: dict[str, Path] = {}
    for row in rows:
        source_text = str(row.get("source", "") or "").strip()
        output_text = str(row.get("output", "") or "").strip()
        if not source_text or not output_text:
            continue
        source = _resolve_existing_path(source_text, [config.root, rework_batch_dir, rework_batch_dir.parent])
        output = _resolve_existing_path(output_text, [rework_batch_dir, config.root / DIR_REVIEW, config.root])
        if not output.exists():
            continue
        source_map[_path_key(source)] = output
        if source.name:
            source_name_map[source.name.lower()] = output
    return source_map, source_name_map


def _build_rework_source_lists(config: ProjectConfig, rework_batch_dir: Path) -> tuple[dict[str, list[Path]], dict[str, list[Path]]]:
    rows = _read_render_manifest(rework_batch_dir / "render_manifest.json")
    source_map: dict[str, list[Path]] = {}
    source_name_map: dict[str, list[Path]] = {}
    for row in rows:
        source_text = str(row.get("source", "") or "").strip()
        output_text = str(row.get("output", "") or "").strip()
        if not source_text or not output_text:
            continue
        source = _resolve_existing_path(source_text, [config.root, rework_batch_dir, rework_batch_dir.parent])
        output = _resolve_existing_path(output_text, [rework_batch_dir, config.root / DIR_REVIEW, config.root])
        if not output.exists():
            continue
        source_map.setdefault(_path_key(source), []).append(output)
        if source.name:
            source_name_map.setdefault(source.name.lower(), []).append(output)
    return source_map, source_name_map


def _build_source_output_order(config: ProjectConfig, batch_dir: Path, rows: list[dict]) -> dict[str, list[Path]]:
    grouped: dict[str, list[Path]] = {}
    for row in rows:
        source_text = str(row.get("source", "") or "").strip()
        output_text = str(row.get("output", "") or "").strip()
        if not source_text or not output_text:
            continue
        source = _resolve_existing_path(source_text, [config.root, batch_dir, batch_dir.parent])
        output = _resolve_existing_path(output_text, [batch_dir, config.root / DIR_REVIEW, config.root])
        if output.exists():
            grouped.setdefault(_path_key(source), []).append(output)
    for key, outputs in grouped.items():
        grouped[key] = sorted(outputs, key=lambda item: item.name.lower())
    return grouped


def _lookup_rework_output(source: Path, source_map: dict[str, Path], source_name_map: dict[str, Path]) -> Path | None:
    return source_map.get(_path_key(source)) or source_name_map.get(source.name.lower())


def _latest_rework_dedup_report(rework_batch_dir: Path) -> Path | None:
    report_dir = rework_batch_dir / "去重指纹报告"
    if not report_dir.exists():
        return None
    reports = [path for path in report_dir.glob("去重指纹报告_*.json") if path.is_file()]
    if not reports:
        return None
    return max(reports, key=lambda path: (path.stat().st_mtime, str(path)))


def _load_similarity_pair_map(config: ProjectConfig, rework_batch_dir: Path, report_path: Path) -> dict[frozenset[str], float]:
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    pair_map: dict[frozenset[str], float] = {}
    for pair in payload.get("pairs", []):
        left_text = str(pair.get("left", "") or "").strip()
        right_text = str(pair.get("right", "") or "").strip()
        if not left_text or not right_text:
            continue
        left = _resolve_existing_path(left_text, [rework_batch_dir, config.root / DIR_REVIEW, config.root])
        right = _resolve_existing_path(right_text, [rework_batch_dir, config.root / DIR_REVIEW, config.root])
        pair_map[frozenset({_path_key(left), _path_key(right)})] = float(pair.get("similarity", 0.0) or 0.0)
    return pair_map


def _lookup_similarity(left: Path, right: Path, pair_map: dict[frozenset[str], float]) -> float | None:
    return pair_map.get(frozenset({_path_key(left), _path_key(right)}))


def _calculate_pair_similarity(config: ProjectConfig, rework_batch_dir: Path, left: Path, right: Path) -> float:
    frame_dir = rework_batch_dir / "对比临时帧"
    frame_dir.mkdir(parents=True, exist_ok=True)
    try:
        left_item = _video_hash_item(config, left, frame_dir)
        right_item = _video_hash_item(config, right, frame_dir)
        errors = [item.error for item in [left_item, right_item] if item.error]
        if errors:
            raise RuntimeError("；".join(errors))
        if not left_item.hashes or not right_item.hashes:
            raise RuntimeError("未生成可用哈希")
        return _hash_similarity(left_item.hashes, right_item.hashes)
    finally:
        shutil.rmtree(frame_dir, ignore_errors=True)


def _similarity_level(score: float, threshold: float) -> str:
    if score >= threshold:
        return "high"
    return "medium" if score >= max(0.78, threshold - 0.10) else "low"


def _rework_comparison_conclusion(new_high_count: int, unaligned_count: int, total: int) -> str:
    if total > 0 and unaligned_count == total:
        return "全部旧高相似组无法对齐，无法判断风险变化，请人工复核。"
    if new_high_count == 0 and unaligned_count == 0:
        return "风险已下降：全部高相似组重做后均低于阈值。"
    if new_high_count > 0:
        conclusion = f"仍有 {new_high_count} 组高相似，建议再次执行高相似重做或人工复核。"
    else:
        conclusion = "风险已下降：全部高相似组重做后均低于阈值。"
    if unaligned_count:
        conclusion += f"另有 {unaligned_count} 组未对齐。"
    return conclusion


def _rework_comparison_markdown(report: ReworkComparisonReport) -> str:
    lines = [
        "# 高相似重做对比报告",
        "",
        f"- 生成时间：{report.generated_at}",
        f"- 原指纹报告：`{report.old_report}`",
        f"- 重做批次：`{report.rework_batch}`",
        f"- 新指纹报告：`{report.new_report or '-'}`",
        f"- 旧 high 组数：{report.old_high_count}",
        f"- 仍为 high：{report.new_high_count}",
        f"- 已解决：{report.resolved_count}",
        f"- 未对齐：{report.unaligned_count}",
        f"- 结论：{report.conclusion}",
        "",
        "| 旧相似度 | 新相似度 | 变化 | 新等级 | 视频A | 视频B | 备注 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for pair in report.pairs:
        new_similarity = "-" if pair.new_similarity is None else f"{pair.new_similarity:.4f}"
        delta = "-" if pair.delta is None else f"{pair.delta:+.4f}"
        lines.append(
            "| "
            f"{pair.old_similarity:.4f} | {new_similarity} | {delta} | {pair.new_level} | "
            f"{_comparison_video_label(pair.old_left, pair.new_left)} | "
            f"{_comparison_video_label(pair.old_right, pair.new_right)} | "
            f"{pair.reason or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)


def _comparison_video_label(old_path: str, new_path: str) -> str:
    old_name = Path(old_path).name
    if not new_path:
        return old_name
    return f"{old_name} -> {Path(new_path).name}"


def _video_hash_item(config: ProjectConfig, video: Path, frame_dir: Path) -> VideoHashItem:
    duration = _duration(config, video)
    if duration <= 0:
        return VideoHashItem(str(video), duration, [], "无法读取视频时长")
    try:
        hashes = []
        points = _sample_points(duration)
        for index, second in enumerate(points, start=1):
            frame = frame_dir / f"{video.stem}_{index:02d}.png"
            _extract_frame(config, video, second, frame)
            hashes.append(_average_hash(frame))
        return VideoHashItem(str(video), duration, hashes)
    except Exception as exc:
        return VideoHashItem(str(video), duration, [], str(exc))


def _sample_points(duration: float) -> list[float]:
    ratios = [0.12, 0.30, 0.50, 0.70, 0.88]
    return [max(0.0, min(duration - 0.05, duration * ratio)) for ratio in ratios]


def _duration(config: ProjectConfig, video: Path) -> float:
    ffprobe = config.tools.ffprobe or "ffprobe"
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        return 0.0
    try:
        return max(0.0, float(completed.stdout.strip()))
    except ValueError:
        return 0.0


def _extract_frame(config: ProjectConfig, video: Path, second: float, output: Path) -> None:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    command = [
        ffmpeg,
        "-y",
        "-ss",
        f"{second:.3f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        "scale=16:16,format=gray",
        str(output),
    ]
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr[-1200:] if completed.stderr else completed.stdout[-1200:]
        raise RuntimeError(detail.strip() or "抽帧失败")


def _average_hash(image_path: Path, hash_size: int = 16) -> str:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("缺少 Pillow，无法生成去重指纹。") from exc
    image = Image.open(image_path).convert("L").resize((hash_size, hash_size))
    pixels = list(image.getdata())
    average = sum(pixels) / max(1, len(pixels))
    value = 0
    for pixel in pixels:
        value = (value << 1) | (1 if pixel >= average else 0)
    return f"{value:0{hash_size * hash_size // 4}x}"


def _pairwise_similarity(items: list[VideoHashItem], threshold: float) -> list[SimilarityPair]:
    pairs: list[SimilarityPair] = []
    for left_index, left in enumerate(items):
        for right in items[left_index + 1 :]:
            score = _hash_similarity(left.hashes, right.hashes)
            level = "high" if score >= threshold else ("medium" if score >= max(0.78, threshold - 0.10) else "low")
            pairs.append(SimilarityPair(left.video, right.video, round(score, 4), level))
    return sorted(pairs, key=lambda item: item.similarity, reverse=True)


def _hash_similarity(left: list[str], right: list[str]) -> float:
    count = min(len(left), len(right))
    if count <= 0:
        return 0.0
    distances = []
    for index in range(count):
        a = int(left[index], 16)
        b = int(right[index], 16)
        distances.append((a ^ b).bit_count() / 256.0)
    return max(0.0, 1.0 - (sum(distances) / len(distances)))


def _write_pairs_csv(path: Path, pairs: list[SimilarityPair]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["left", "right", "similarity", "level"])
        writer.writeheader()
        for item in pairs:
            writer.writerow(asdict(item))


def _write_audio_pairs_csv(path: Path, audio_report) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["left", "right", "similarity", "level", "engine"])
        writer.writeheader()
        for item in audio_report.pairs:
            row = asdict(item)
            row["engine"] = audio_report.engine
            writer.writerow(row)


def _read_render_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _build_manifest_maps(
    config: ProjectConfig,
    batch_dir: Path,
    rows: list[dict],
) -> tuple[dict[str, dict], dict[str, dict], str]:
    output_map: dict[str, dict] = {}
    output_name_map: dict[str, dict] = {}
    mode = ""
    for row in rows:
        output = _resolve_existing_path(str(row.get("output", "")), [batch_dir, config.root / DIR_REVIEW, config.root])
        output_map[_path_key(output)] = row
        if output.name:
            output_name_map[output.name.lower()] = row
        if not mode and row.get("mode"):
            mode = str(row["mode"])
    return output_map, output_name_map, mode


def _resolve_existing_path(value: str, search_roots: list[Path]) -> Path:
    candidate = Path(value)
    if candidate.exists():
        return candidate
    name = candidate.name
    if not name:
        return candidate
    for root in search_roots:
        if not root.exists():
            continue
        direct = root / name
        if direct.exists():
            return direct
        try:
            matches = sorted(root.rglob(name), key=lambda item: len(str(item)))
        except OSError:
            matches = []
        if matches:
            return matches[0]
    return candidate


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()


def _markdown(payload: dict) -> str:
    summary = payload["summary"]
    lines = [
        "# 去重指纹报告",
        "",
        f"- 视频数：{summary['videos']}",
        f"- 对比组数：{summary['pairs']}",
        f"- 高相似组：{summary['high_similarity']}",
        f"- 阈值：{payload['threshold']}",
        "",
        "| 相似度 | 等级 | 视频 A | 视频 B |",
        "| --- | --- | --- | --- |",
    ]
    for pair in payload["pairs"][:80]:
        lines.append(f"| {pair['similarity']:.4f} | {pair['level']} | {Path(pair['left']).name} | {Path(pair['right']).name} |")
    lines.append("")

    audio = payload.get("audio") or {}
    audio_summary = audio.get("summary")
    if audio_summary:
        engine_label = "Chromaprint fpcalc" if audio.get("engine") == "fpcalc" else "FFmpeg 能量包络（兜底）"
        lines.extend(
            [
                "## 音频指纹",
                "",
                f"- 引擎：{engine_label}",
                f"- 高相似组：{audio_summary['high_similarity']}",
                f"- 阈值：{audio['threshold']}",
                "",
                "| 音频相似度 | 等级 | 视频 A | 视频 B |",
                "| --- | --- | --- | --- |",
            ]
        )
        for pair in audio.get("pairs", [])[:80]:
            lines.append(
                f"| {pair['similarity']:.4f} | {pair['level']} | {Path(pair['left']).name} | {Path(pair['right']).name} |"
            )
        lines.append("")
    elif audio.get("error"):
        lines.extend(["## 音频指纹", "", f"- 未生成：{audio['error']}", ""])
    return "\n".join(lines)
