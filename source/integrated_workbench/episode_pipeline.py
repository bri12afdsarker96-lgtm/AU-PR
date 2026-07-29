from __future__ import annotations

import csv
import json
import re
import shutil
from .proc import run_silent
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .assemble import _concat_segments
from .editing_engine import _probe_duration
from .models import AUDIO_EXTENSIONS, DIR_A, DIR_AUDIO, DIR_AUTO_EDIT_INPUT, DIR_EPISODE_REWORK, ProjectConfig
from .project import log_line
from .scene_detect import SceneDetectionResult, detect_scenes, split_scenes_to_clips
from .video_dedup import make_dedup_variant, variant_manifest, write_dedup_report
from .video_engine import build_horizontal_command, build_vertical_command


@dataclass
class EpisodeVariantSegment:
    version: int
    order: int
    source_clip: Path
    output_clip: Path
    dedup_level: str
    variant: dict[str, str]
    fallback_reason: str = ""


@dataclass
class EpisodeVersionResult:
    version: int
    output_path: Path
    segment_dir: Path
    segments: list[EpisodeVariantSegment]
    audio_strategy: str = "keep_original"


@dataclass
class EpisodeReworkResult:
    batch_dir: Path
    source_video: Path
    audio_mode: str
    scene_result: SceneDetectionResult
    split_dir: Path
    output_paths: list[Path]
    versions: list[EpisodeVersionResult]
    manifest_csv: Path
    manifest_json: Path
    failed_segments: list[str]
    dedup_report_json: Path | None = None
    dedup_report_markdown: Path | None = None

    @property
    def scene_count(self) -> int:
        return len(self.scene_result.scenes)

    @property
    def fallback_used(self) -> bool:
        return bool(self.scene_result.fallback_reason)


def episode_rework(
    config: ProjectConfig,
    video: str | Path,
    versions: int = 3,
    audio_mode: str = "keep_original",
    replacement_audio: str | Path | None = None,
    dedup_level: str | None = None,
    mode: str | None = None,
    threshold: float | None = None,
    min_scene_seconds: float = 0.5,
    min_clip_seconds: float = 1.0,
    name: str | None = None,
    keep_segments: bool = False,
    write_similarity_report: bool | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> EpisodeReworkResult:
    source_video = _resolve_episode_video(config, Path(video))
    if not source_video.exists():
        raise FileNotFoundError(f"找不到整集视频：{video}")
    version_count = max(1, int(versions or 1))
    if version_count > 50:
        raise ValueError("整集重组版本数不能超过 50。")
    selected_audio_mode = audio_mode if audio_mode in {"keep_original", "replace_track"} else "keep_original"
    selected_replacement_audio = _resolve_audio_path(config, Path(replacement_audio)) if replacement_audio else None
    if selected_audio_mode == "replace_track":
        if selected_replacement_audio is None or not selected_replacement_audio.exists():
            raise FileNotFoundError("选择替换音频时，请提供有效的音频文件。")
        if selected_replacement_audio.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ValueError("替换音频格式仅支持 mp3、wav、m4a、aac、flac、ogg。")
    selected_level = dedup_level or config.recipe.dedup_level
    selected_mode = mode or config.recipe.mode
    if selected_mode not in {"vertical", "horizontal"}:
        selected_mode = "vertical"

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _safe_name(name or source_video.stem)
    batch_dir = config.root / DIR_EPISODE_REWORK / f"{safe_name}_{stamp}"
    segment_root = batch_dir / "segments"
    output_root = batch_dir / "outputs"
    batch_dir.mkdir(parents=True, exist_ok=True)
    segment_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)

    log_line(config, f"整集重组开始：{source_video.name}，版本 {version_count} 条")
    scene_result = detect_scenes(config, source_video, threshold=threshold, min_scene_seconds=min_scene_seconds)
    split_result = split_scenes_to_clips(
        config,
        source_video,
        result=scene_result,
        min_clip_seconds=min_clip_seconds,
    )

    versions_result: list[EpisodeVersionResult] = []
    failed_segments: list[str] = []
    _ep_total = max(1, version_count * max(1, len(split_result.clips)))
    _ep_done = 0
    if progress:
        progress(0, _ep_total)
    for version_index in range(1, version_count + 1):
        version_dir = segment_root / f"v{version_index:02d}"
        version_dir.mkdir(parents=True, exist_ok=True)
        variant_segments: list[EpisodeVariantSegment] = []
        for order, clip in enumerate(split_result.clips, start=1):
            output_clip = version_dir / f"{order:03d}_{_safe_name(clip.stem)}_v{version_index:02d}.mp4"
            variant = make_dedup_variant(selected_level, f"{batch_dir.name}|{clip}|{version_index}|{order}")
            fallback_reason = ""
            if selected_mode == "horizontal":
                command = build_horizontal_command(config, clip, None, output_clip, variant)
            else:
                command = build_vertical_command(config, clip, None, None, output_clip, variant)
            try:
                _run(command, f"整集重组片段生成失败：v{version_index:02d}/{order:03d}")
            except Exception as exc:
                fallback_reason = str(exc)[-500:]
                failed_segments.append(f"v{version_index:02d}/S{order:03d}: {fallback_reason}")
                _normalize_clip(config, clip, output_clip, selected_mode)
            variant_segments.append(
                EpisodeVariantSegment(
                    version=version_index,
                    order=order,
                    source_clip=clip,
                    output_clip=output_clip,
                    dedup_level=variant.level,
                    variant=variant_manifest(variant),
                    fallback_reason=fallback_reason,
                )
            )
            _ep_done += 1
            if progress:
                progress(_ep_done, _ep_total)
        output_path = output_root / f"{safe_name}_v{version_index:02d}.mp4"
        concat_list = _concat_segments(config, [item.output_clip for item in variant_segments], output_path)
        concat_list.unlink(missing_ok=True)
        audio_strategy = "keep_original"
        if selected_audio_mode == "replace_track" and selected_replacement_audio:
            output_path, audio_strategy = _replace_audio_track(config, output_path, selected_replacement_audio)
        versions_result.append(EpisodeVersionResult(version_index, output_path, version_dir, variant_segments, audio_strategy))
        log_line(config, f"整集重组版本完成：{output_path}")

    manifest_csv = batch_dir / "episode_rework_manifest.csv"
    manifest_json = batch_dir / "episode_rework_manifest.json"
    _write_manifest(
        manifest_csv,
        manifest_json,
        source_video,
        scene_result,
        split_result.output_dir,
        selected_mode,
        selected_level,
        selected_audio_mode,
        selected_replacement_audio,
        versions_result,
        failed_segments,
    )

    dedup_report_json = None
    dedup_report_markdown = None
    should_report = config.recipe.make_dedup_report if write_similarity_report is None else write_similarity_report
    if should_report and len(versions_result) > 1:
        try:
            report = write_dedup_report(
                config,
                [item.output_path for item in versions_result],
                batch_dir,
                config.recipe.dedup_similarity_threshold,
            )
            dedup_report_json = report.json_path
            dedup_report_markdown = report.markdown_path
        except Exception as exc:
            log_line(config, f"整集重组去重指纹报告生成失败：{exc}")

    if not keep_segments:
        shutil.rmtree(segment_root, ignore_errors=True)

    result = EpisodeReworkResult(
        batch_dir=batch_dir,
        source_video=source_video,
        audio_mode=selected_audio_mode,
        scene_result=scene_result,
        split_dir=split_result.output_dir,
        output_paths=[item.output_path for item in versions_result],
        versions=versions_result,
        manifest_csv=manifest_csv,
        manifest_json=manifest_json,
        failed_segments=failed_segments,
        dedup_report_json=dedup_report_json,
        dedup_report_markdown=dedup_report_markdown,
    )
    log_line(config, f"整集重组完成：输出 {len(result.output_paths)} 条，目录 {batch_dir}")
    return result


def _write_manifest(
    csv_path: Path,
    json_path: Path,
    source_video: Path,
    scene_result: SceneDetectionResult,
    split_dir: Path,
    mode: str,
    dedup_level: str,
    audio_mode: str,
    replacement_audio: Path | None,
    versions: list[EpisodeVersionResult],
    failed_segments: list[str],
) -> None:
    rows: list[dict[str, Any]] = []
    for version in versions:
        for segment in version.segments:
            row = {
                "version": segment.version,
                "order": segment.order,
                "source_video": str(source_video),
                "source_clip": str(segment.source_clip),
                "output_clip": str(segment.output_clip),
                "output_video": str(version.output_path),
                "mode": mode,
                "dedup_level": segment.dedup_level,
                "audio_mode": audio_mode,
                "audio_strategy": version.audio_strategy,
                "variant_json": json.dumps(segment.variant, ensure_ascii=False, sort_keys=True),
                "fallback_reason": segment.fallback_reason,
            }
            rows.append(row)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = [
            "version",
            "order",
            "source_video",
            "source_clip",
            "output_clip",
            "output_video",
            "mode",
            "dedup_level",
            "audio_mode",
            "audio_strategy",
            "variant_json",
            "fallback_reason",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_video": str(source_video),
        "scene_result": scene_result.json_path,
        "scene_engine": scene_result.engine,
        "scene_count": len(scene_result.scenes),
        "fallback_reason": scene_result.fallback_reason,
        "split_dir": str(split_dir),
        "mode": mode,
        "audio_mode": audio_mode,
        "replacement_audio": str(replacement_audio or ""),
        "dedup_level": dedup_level,
        "failed_segments": failed_segments,
        "outputs": [str(item.output_path) for item in versions],
        "segments": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_episode_video(config: ProjectConfig, video: Path) -> Path:
    if video.exists():
        return video
    if video.is_absolute():
        return video
    for root in [Path.cwd(), config.root, config.root / DIR_AUTO_EDIT_INPUT, config.root / DIR_A]:
        candidate = root / video
        if candidate.exists() and candidate.is_file():
            return candidate
    return video


def _resolve_audio_path(config: ProjectConfig, audio: Path) -> Path:
    if audio.exists() or audio.is_absolute():
        return audio
    for root in [Path.cwd(), config.root, config.root / DIR_AUDIO]:
        candidate = root / audio
        if candidate.exists() and candidate.suffix.lower() in AUDIO_EXTENSIONS:
            return candidate
    return audio


def _normalize_clip(config: ProjectConfig, source: Path, output: Path, mode: str) -> None:
    width, height = (1920, 1080) if mode == "horizontal" else (config.recipe.output_width, config.recipe.output_height)
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        config.recipe.preset,
        "-crf",
        str(config.recipe.crf),
        "-c:a",
        "aac",
        "-pix_fmt",
        "yuv420p",
        "-shortest",
        str(output),
    ]
    _run(command, f"整集重组片段兜底失败：{source.name}")


def _replace_audio_track(config: ProjectConfig, video: Path, audio: Path) -> tuple[Path, str]:
    video_seconds = _probe_duration(config, video) or 0.0
    audio_seconds = _probe_duration(config, audio) or 0.0
    if video_seconds <= 0 or audio_seconds <= 0:
        raise RuntimeError("无法读取整集或替换音频时长。")
    diff = audio_seconds - video_seconds
    if abs(diff) <= 0.1:
        video_filter = "null"
        strategy = "as_is"
    elif diff > 0 and diff <= audio_seconds * 0.15:
        factor = audio_seconds / max(0.1, video_seconds)
        video_filter = f"setpts=(PTS-STARTPTS)*{factor:.8f},trim=duration={audio_seconds:.3f}"
        strategy = "slow"
    elif diff > 0:
        video_filter = f"tpad=stop_mode=clone:stop_duration={diff:.3f},trim=duration={audio_seconds:.3f},setpts=PTS-STARTPTS"
        strategy = "freeze"
    else:
        video_filter = f"trim=duration={audio_seconds:.3f},setpts=PTS-STARTPTS"
        strategy = "trim_tail"

    replaced = video.with_name(f"{video.stem}_replace_audio.mp4")
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-filter:v",
        video_filter,
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:v",
        "libx264",
        "-preset",
        config.recipe.preset,
        "-crf",
        str(config.recipe.crf),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-t",
        f"{audio_seconds:.3f}",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(replaced),
    ]
    _run(command, f"整集替换音频失败：{video.name}")
    video.unlink(missing_ok=True)
    replaced.replace(video)
    return video, strategy


def _safe_name(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*\s]+', "_", str(value)).strip("_") or datetime.now().strftime("%Y%m%d_%H%M%S")


def _run(command: list[str], label: str) -> None:
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr[-3000:] if completed.stderr else completed.stdout[-3000:]
        raise RuntimeError(f"{label}：{detail.strip() or '未知错误'}")
