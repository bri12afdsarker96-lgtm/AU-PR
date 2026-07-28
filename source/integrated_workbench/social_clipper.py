from __future__ import annotations

import csv
import json
import random
from .proc import run_silent
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .models import DIR_AUDIO, DIR_READY, DIR_REVIEW, DIR_SOCIAL_CLIPS, ProjectConfig, AUDIO_EXTENSIONS, VIDEO_EXTENSIONS
from .project import iter_media, iter_project_media, log_line, require_files


@dataclass
class SocialClipOptions:
    source: str = "ready"
    ratio: str = "9:16"
    width: int = 1080
    height: int = 1920
    use_bgm: bool = True
    bgm_mode: str = "ducking"
    bgm_volume: float = 0.18
    crf: int = 23
    preset: str = "veryfast"


@dataclass
class SocialClipItem:
    source_video: str
    bgm: str
    output_video: str
    ratio: str
    has_voice: bool
    audio_mode: str
    note: str


@dataclass
class SocialClipResult:
    output_dir: Path
    manifest_csv: Path
    manifest_json: Path
    items: list[SocialClipItem]


SOURCE_DIRS = {
    "ready": DIR_READY,
    "intermediate": DIR_REVIEW,
    "review": DIR_REVIEW,
}


def generate_social_clip_package(config: ProjectConfig, options: SocialClipOptions | None = None) -> SocialClipResult:
    """Create platform-ready vertical clips with loudness normalization and optional BGM ducking."""
    options = options or SocialClipOptions()
    source_key = options.source if options.source in SOURCE_DIRS else "ready"
    videos = _source_videos(config, source_key)
    if not videos and source_key == "ready":
        videos = _source_videos(config, "intermediate")
    videos = require_files("社媒切条增强视频", videos)

    stamp = datetime.now().strftime("batch_%Y%m%d_%H%M%S")
    output_dir = config.root / DIR_SOCIAL_CLIPS / stamp
    output_dir.mkdir(parents=True, exist_ok=True)
    bgms = iter_project_media(config, DIR_AUDIO, AUDIO_EXTENSIONS)

    items: list[SocialClipItem] = []
    for video in videos:
        bgm = random.choice(bgms) if options.use_bgm and bgms else None
        output = output_dir / f"{video.stem}_社媒增强.mp4"
        duration = _duration(config, video)
        has_voice = _has_audio(config, video)
        command, audio_mode = _build_command(config, video, output, options, duration, has_voice, bgm)
        log_line(config, f"开始社媒增强：{video.name}")
        _run(command)
        log_line(config, f"社媒增强完成：{output.name}")
        note = "竖屏裁切 + 响度标准化"
        if bgm:
            note += " + BGM 自动循环"
            if has_voice and options.bgm_mode == "ducking":
                note += " + 说话时压低音乐"
        items.append(
            SocialClipItem(
                source_video=str(video),
                bgm=str(bgm or ""),
                output_video=str(output),
                ratio=options.ratio,
                has_voice=has_voice,
                audio_mode=audio_mode,
                note=note,
            )
        )

    manifest_csv = output_dir / "社媒切条增强清单.csv"
    manifest_json = output_dir / "social_clip_manifest.json"
    _write_manifest(items, manifest_csv, manifest_json)
    return SocialClipResult(output_dir, manifest_csv, manifest_json, items)


def _source_videos(config: ProjectConfig, source: str) -> list[Path]:
    directory = SOURCE_DIRS[source]
    videos = iter_project_media(config, directory, VIDEO_EXTENSIONS)
    if videos:
        return videos
    return iter_media(config.root / directory, VIDEO_EXTENSIONS)


def _build_command(
    config: ProjectConfig,
    video: Path,
    output: Path,
    options: SocialClipOptions,
    duration: float,
    has_voice: bool,
    bgm: Path | None,
) -> tuple[list[str], str]:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    video_filter = (
        f"[0:v]scale={options.width}:{options.height}:force_original_aspect_ratio=increase,"
        f"crop={options.width}:{options.height},setsar=1[vout]"
    )
    command = [ffmpeg, "-y", "-i", str(video)]
    filter_parts = [video_filter]
    maps = ["-map", "[vout]"]
    audio_mode = "无音频"

    if bgm:
        command.extend(["-stream_loop", "-1", "-i", str(bgm)])
        if has_voice and options.bgm_mode == "ducking":
            filter_parts.append(
                f"[0:a]loudnorm=I=-16:TP=-1.5:LRA=11[voice];"
                f"[1:a]volume={options.bgm_volume},atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[bgm];"
                "[bgm][voice]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=500[duck];"
                "[voice][duck]amix=inputs=2:duration=first:dropout_transition=2,aformat=sample_rates=48000[aout]"
            )
            audio_mode = "原声响度标准化 + BGM 自动压低"
        elif has_voice:
            filter_parts.append(
                f"[0:a]loudnorm=I=-16:TP=-1.5:LRA=11[voice];"
                f"[1:a]volume={options.bgm_volume},atrim=duration={duration:.3f},asetpts=PTS-STARTPTS[bgm];"
                "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2,aformat=sample_rates=48000[aout]"
            )
            audio_mode = "原声响度标准化 + 固定低音量 BGM"
        else:
            filter_parts.append(
                f"[1:a]volume={options.bgm_volume},atrim=duration={duration:.3f},asetpts=PTS-STARTPTS,"
                "aformat=sample_rates=48000[aout]"
            )
            audio_mode = "仅 BGM"
        maps.extend(["-map", "[aout]"])
    elif has_voice:
        filter_parts.append("[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,aformat=sample_rates=48000[aout]")
        maps.extend(["-map", "[aout]"])
        audio_mode = "原声响度标准化"
    else:
        maps.append("-an")

    command.extend(
        [
            "-filter_complex",
            ";".join(filter_parts),
            *maps,
            "-c:v",
            "libx264",
            "-preset",
            options.preset,
            "-crf",
            str(options.crf),
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            "-shortest",
            str(output),
        ]
    )
    return command, audio_mode


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
        return max(0.1, float(completed.stdout.strip()))
    except ValueError:
        return 0.1


def _has_audio(config: ProjectConfig, video: Path) -> bool:
    ffprobe = config.tools.ffprobe or "ffprobe"
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(video),
    ]
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _run(command: list[str]) -> None:
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr[-3000:] if completed.stderr else completed.stdout[-3000:]
        raise RuntimeError(detail.strip() or "ffmpeg 执行失败")


def _write_manifest(items: list[SocialClipItem], csv_path: Path, json_path: Path) -> None:
    rows = [asdict(item) for item in items]
    fieldnames = list(rows[0].keys()) if rows else [field.name for field in SocialClipItem.__dataclass_fields__.values()]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
