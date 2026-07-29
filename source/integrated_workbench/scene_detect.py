from __future__ import annotations

import csv
import json
import re
from .proc import run_silent
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .models import DIR_A, DIR_AUTO_EDIT_INPUT, DIR_SCENE_DETECT, ProjectConfig
from .project import log_line


DEFAULT_FFMPEG_SCENE_THRESHOLD = 0.35
DEFAULT_PYSCENEDETECT_THRESHOLD = 27.0


@dataclass
class SceneSpan:
    index: int
    start_seconds: float
    end_seconds: float
    duration_seconds: float


@dataclass
class SceneDetectionResult:
    video: str
    engine: str
    engine_detail: str
    fallback_reason: str
    threshold: float
    scenes: list[SceneSpan]
    generated_at: str
    csv_path: str
    json_path: str


@dataclass
class ScenesSplitResult:
    clips: list[Path]
    skipped_short: int
    output_dir: Path
    manifest_csv: Path
    manifest_json: Path
    engine: str


def scenedetect_available() -> tuple[bool, str]:
    try:
        import scenedetect  # type: ignore
    except Exception as exc:
        return False, str(exc)
    version = getattr(scenedetect, "__version__", "") or "unknown"
    return True, str(version)


def detect_scenes(
    config: ProjectConfig,
    video: Path,
    threshold: float | None = None,
    min_scene_seconds: float = 0.5,
) -> SceneDetectionResult:
    video = _resolve_video_path(config, Path(video))
    if not video.exists():
        raise FileNotFoundError(f"视频不存在：{video}")

    ffmpeg_threshold, pyscene_threshold = _threshold_values(threshold)
    fallback_reason = ""
    try:
        scenes, detail = _detect_with_pyscenedetect(video, pyscene_threshold, min_scene_seconds)
        engine = "pyscenedetect"
        engine_detail = detail
        effective_threshold = pyscene_threshold
    except Exception as exc:
        fallback_reason = _fallback_reason(exc)
        points = _ffmpeg_scene_points(config, video, ffmpeg_threshold)
        duration = _video_duration(config, video)
        scenes = _spans_from_points(points, duration)
        engine = "ffmpeg_fallback"
        engine_detail = f"ffmpeg select=gt(scene,{ffmpeg_threshold})"
        effective_threshold = ffmpeg_threshold

    duration = _video_duration(config, video)
    if not scenes:
        scenes = _single_scene(duration)
        note = "未检出切点，按整片处理"
        fallback_reason = f"{fallback_reason}；{note}" if fallback_reason else note
    scenes = _merge_short_scenes(scenes, max(0.0, min_scene_seconds))
    scenes = _normalize_scene_indexes(scenes)

    result = SceneDetectionResult(
        video=str(video),
        engine=engine,
        engine_detail=engine_detail,
        fallback_reason=fallback_reason,
        threshold=round(effective_threshold, 3),
        scenes=scenes,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        csv_path="",
        json_path="",
    )
    csv_path, json_path = _write_scene_result(config, video, result)
    result.csv_path = str(csv_path)
    result.json_path = str(json_path)
    json_path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")
    log_line(config, f"场景检测完成：{video.name}，引擎 {result.engine}，场景 {len(result.scenes)} 个。")
    return result


def latest_scene_result(config: ProjectConfig, video: Path) -> SceneDetectionResult | None:
    root = config.root / DIR_SCENE_DETECT
    if not root.exists():
        return None
    video = Path(video)
    matches = sorted(root.glob(f"{_safe_name(video.stem)}_场景检测_*.json"), key=lambda path: (path.stat().st_mtime, str(path)), reverse=True)
    for path in matches:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            recorded = Path(str(payload.get("video", "")))
            if recorded.exists() and _path_key(recorded) != _path_key(video):
                continue
            if recorded.name and recorded.name.lower() != video.name.lower() and not recorded.exists():
                continue
            return _scene_result_from_payload(payload)
        except Exception:
            continue
    return None


def split_scenes_to_clips(
    config: ProjectConfig,
    video: Path,
    result: SceneDetectionResult | None = None,
    min_clip_seconds: float = 1.0,
) -> ScenesSplitResult:
    video = _resolve_video_path(config, Path(video))
    result = result or detect_scenes(config, video)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = config.root / DIR_SCENE_DETECT / f"切片_{_safe_name(video.stem)}_{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    rows: list[dict[str, str]] = []
    skipped_short = 0

    for scene in result.scenes:
        if scene.duration_seconds < min_clip_seconds:
            skipped_short += 1
            continue
        output = output_dir / f"S{scene.index:03d}_{_safe_name(video.stem)}.mp4"
        try:
            _split_one_scene(config, video, scene, output)
        except Exception as exc:
            log_line(config, f"镜头切片跳过：S{scene.index:03d}，{exc}")
            continue
        clips.append(output)
        rows.append(
            {
                "index": str(scene.index),
                "start": f"{scene.start_seconds:.3f}",
                "end": f"{scene.end_seconds:.3f}",
                "duration": f"{scene.duration_seconds:.3f}",
                "output_file": str(output),
            }
        )

    if not clips:
        raise RuntimeError("没有成功切出可用镜头片段。")

    manifest_csv = output_dir / "切片清单.csv"
    manifest_json = output_dir / "切片清单.json"
    with manifest_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["index", "start", "end", "duration", "output_file"])
        writer.writeheader()
        writer.writerows(rows)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "video": str(video),
        "scene_result": result.json_path,
        "engine": result.engine,
        "skipped_short": skipped_short,
        "clips": rows,
    }
    manifest_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log_line(config, f"镜头切片完成：{len(clips)} 条，跳过短片段 {skipped_short} 条。")
    return ScenesSplitResult(clips, skipped_short, output_dir, manifest_csv, manifest_json, result.engine)


def _detect_with_pyscenedetect(video: Path, threshold: float, min_scene_seconds: float) -> tuple[list[SceneSpan], str]:
    import scenedetect  # type: ignore
    from scenedetect import SceneManager, open_video  # type: ignore
    from scenedetect.detectors import ContentDetector  # type: ignore

    video_stream = open_video(str(video))
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(threshold=threshold))
    scene_manager.detect_scenes(video=video_stream)
    spans = [
        SceneSpan(
            index=index,
            start_seconds=round(start.get_seconds(), 3),
            end_seconds=round(end.get_seconds(), 3),
            duration_seconds=round(max(0.0, end.get_seconds() - start.get_seconds()), 3),
        )
        for index, (start, end) in enumerate(scene_manager.get_scene_list(), start=1)
    ]
    spans = _merge_short_scenes(spans, min_scene_seconds)
    version = getattr(scenedetect, "__version__", "") or "unknown"
    return spans, f"scenedetect {version} ContentDetector threshold={threshold}"


def _ffmpeg_scene_points(config: ProjectConfig, video: Path, threshold: float) -> list[float]:
    try:
        from .auto_cut import _detect_scene_points

        return _detect_scene_points(config, video, threshold)
    except Exception as exc:
        raise RuntimeError(f"FFmpeg 场景分析失败：{exc}") from exc


def _video_duration(config: ProjectConfig, video: Path) -> float:
    try:
        from .auto_cut import _duration

        duration = _duration(config, video)
        if duration:
            return duration
    except Exception as exc:
        log_line(config, f"视频时长读取降级到 ffprobe：{video.name}，{exc}")
    ffprobe = config.tools.ffprobe or "ffprobe"
    completed = run_silent(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return 0.0
    try:
        return max(0.0, float(completed.stdout.strip()))
    except ValueError:
        return 0.0


def _spans_from_points(points: list[float], duration: float) -> list[SceneSpan]:
    if duration <= 0:
        return []
    cuts = sorted({0.0, *[point for point in points if 0.0 < point < duration], duration})
    spans: list[SceneSpan] = []
    for index, (start, end) in enumerate(zip(cuts, cuts[1:]), start=1):
        if end - start <= 0.01:
            continue
        spans.append(SceneSpan(index, round(start, 3), round(end, 3), round(end - start, 3)))
    return spans


def _single_scene(duration: float) -> list[SceneSpan]:
    end = round(max(0.0, duration), 3)
    return [SceneSpan(1, 0.0, end, end)]


def _merge_short_scenes(scenes: list[SceneSpan], min_seconds: float) -> list[SceneSpan]:
    if not scenes or min_seconds <= 0:
        return scenes
    merged: list[SceneSpan] = []
    for scene in scenes:
        if scene.duration_seconds >= min_seconds:
            merged.append(scene)
            continue
        if merged:
            previous = merged[-1]
            previous.end_seconds = scene.end_seconds
            previous.duration_seconds = round(previous.end_seconds - previous.start_seconds, 3)
        elif len(scenes) > 1:
            next_scene = scenes[1]
            next_scene.start_seconds = scene.start_seconds
            next_scene.duration_seconds = round(next_scene.end_seconds - next_scene.start_seconds, 3)
        else:
            merged.append(scene)
    return merged


def _normalize_scene_indexes(scenes: list[SceneSpan]) -> list[SceneSpan]:
    normalized: list[SceneSpan] = []
    for index, scene in enumerate(scenes, start=1):
        start = round(max(0.0, scene.start_seconds), 3)
        end = round(max(start, scene.end_seconds), 3)
        normalized.append(SceneSpan(index, start, end, round(end - start, 3)))
    return normalized


def _write_scene_result(config: ProjectConfig, video: Path, result: SceneDetectionResult) -> tuple[Path, Path]:
    root = config.root / DIR_SCENE_DETECT
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = root / f"{_safe_name(video.stem)}_场景检测_{stamp}"
    csv_path = base.with_suffix(".csv")
    json_path = base.with_suffix(".json")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["index", "start_seconds", "end_seconds", "duration_seconds"])
        writer.writeheader()
        for scene in result.scenes:
            writer.writerow(asdict(scene))
    return csv_path, json_path


def _scene_result_from_payload(payload: dict) -> SceneDetectionResult:
    return SceneDetectionResult(
        video=str(payload.get("video", "")),
        engine=str(payload.get("engine", "")),
        engine_detail=str(payload.get("engine_detail", "")),
        fallback_reason=str(payload.get("fallback_reason", "")),
        threshold=float(payload.get("threshold", 0.0) or 0.0),
        scenes=[
            SceneSpan(
                index=int(row.get("index", 0) or 0),
                start_seconds=float(row.get("start_seconds", 0.0) or 0.0),
                end_seconds=float(row.get("end_seconds", 0.0) or 0.0),
                duration_seconds=float(row.get("duration_seconds", 0.0) or 0.0),
            )
            for row in payload.get("scenes", [])
            if isinstance(row, dict)
        ],
        generated_at=str(payload.get("generated_at", "")),
        csv_path=str(payload.get("csv_path", "")),
        json_path=str(payload.get("json_path", "")),
    )


def _split_one_scene(config: ProjectConfig, video: Path, scene: SceneSpan, output: Path) -> None:
    try:
        _copy_scene_clip(config, video, scene, output)
        return
    except Exception:
        if output.exists():
            output.unlink()
    from .editing_engine import StoryboardShot, _trim_shot

    _trim_shot(config, StoryboardShot(f"S{scene.index:03d}", video, scene.start_seconds, scene.duration_seconds), output)


def _copy_scene_clip(config: ProjectConfig, video: Path, scene: SceneSpan, output: Path) -> None:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    command = [
        ffmpeg,
        "-y",
        "-ss",
        f"{scene.start_seconds:.3f}",
        "-t",
        f"{scene.duration_seconds:.3f}",
        "-i",
        str(video),
        "-map",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        str(output),
    ]
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr[-1200:] if completed.stderr else completed.stdout[-1200:]
        raise RuntimeError(detail.strip() or "无损切片失败")
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("无损切片未生成有效文件")


def _threshold_values(threshold: float | None) -> tuple[float, float]:
    if threshold is None:
        return DEFAULT_FFMPEG_SCENE_THRESHOLD, DEFAULT_PYSCENEDETECT_THRESHOLD
    value = float(threshold)
    if value <= 1.0:
        return value, DEFAULT_PYSCENEDETECT_THRESHOLD
    return DEFAULT_FFMPEG_SCENE_THRESHOLD, value


def _resolve_video_path(config: ProjectConfig, video: Path) -> Path:
    if video.exists():
        return video
    if video.is_absolute():
        return video
    for root in [config.root / DIR_AUTO_EDIT_INPUT, config.root / DIR_A]:
        candidate = root / video
        if candidate.exists():
            return candidate
        matches = sorted(root.glob(video.name)) if root.exists() and video.name else []
        if matches:
            return matches[0]
    return video


def _safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\s]+', "_", value).strip("_") or datetime.now().strftime("%Y%m%d_%H%M%S")


def _path_key(path: Path) -> str:
    try:
        return str(path.resolve()).lower()
    except OSError:
        return str(path).lower()


def _fallback_reason(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text[-500:]
