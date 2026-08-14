from __future__ import annotations

import csv
import json
import re
from .proc import run_silent
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .assemble import _cleanup_success_artifacts, _concat_segments, _safe_name
from .editing_engine import _probe_duration
from .models import DIR_DUB_ASSEMBLED, ProjectConfig, VIDEO_EXTENSIONS


DUB_MANIFEST_NAME = "配音清单.csv"
WAV_EXTENSIONS = {".wav"}


@dataclass
class DubSegment:
    index: int
    text: str
    audio: Path
    audio_seconds: float
    video: Path
    video_seconds: float
    strategy: str
    adjust_detail: str
    output_seconds: float
    segment_file: Path | None = None


@dataclass
class DubAssembleResult:
    output_path: Path
    plan_csv: Path
    plan_json: Path
    segments: list[DubSegment]
    total_seconds: float
    unmatched_audio: list[str]
    unmatched_video: list[str]


@dataclass
class _AudioEntry:
    index: int
    text: str
    path: Path
    manifest_seconds: float | None = None


def dub_assemble(
    config: ProjectConfig,
    audio_dir: Path,
    video_dir: Path,
    name: str | None = None,
    trim_anchor: str = "head",
    burn_subtitle: bool = False,
    keep_segments: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> DubAssembleResult:
    if trim_anchor not in {"head", "center", "tail"}:
        raise ValueError("trim_anchor 只能是 head、center 或 tail。")

    audio_root = _resolve_user_path(config, Path(audio_dir))
    video_root = _resolve_user_path(config, Path(video_dir))
    if not audio_root.exists() or not audio_root.is_dir():
        raise FileNotFoundError(f"配音包目录不存在：{audio_root}")
    if not video_root.exists() or not video_root.is_dir():
        raise FileNotFoundError(f"分镜视频目录不存在：{video_root}")

    audio_entries = _load_audio_entries(audio_root)
    videos = _iter_flat_media(video_root, VIDEO_EXTENSIONS)
    pair_count = min(len(audio_entries), len(videos))
    if pair_count <= 0:
        raise RuntimeError(
            "没有可配对的配音和分镜视频。\n"
            f"配音目录：{_content_summary(audio_root, WAV_EXTENSIONS)}\n"
            f"视频目录：{_content_summary(video_root, VIDEO_EXTENSIONS)}"
        )

    unmatched_audio = [entry.path.name for entry in audio_entries[pair_count:]]
    unmatched_video = [path.name for path in videos[pair_count:]]

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    requested_name = _safe_name(name or f"配音成片_{stamp}")
    stem = _safe_name(Path(requested_name).stem)
    output_root = config.root / DIR_DUB_ASSEMBLED
    output_root.mkdir(parents=True, exist_ok=True)
    output_dir = _unique_dir(output_root / stem)
    output_dir.mkdir(parents=True, exist_ok=True)
    segment_dir = output_dir / "临时段"
    segment_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{stem}.mp4"
    plan_csv = output_dir / "音画对齐表.csv"
    plan_json = output_dir / "音画对齐表.json"

    segments: list[DubSegment] = []
    successful: list[Path] = []
    _dub_total = pair_count + 1  # 各分镜段 + 最后拼接
    if progress:
        progress(0, _dub_total)

    for position, (entry, video) in enumerate(zip(audio_entries[:pair_count], videos[:pair_count]), start=1):
        audio_seconds = _required_duration(config, entry.path, "音频")
        video_seconds = _required_duration(config, video, "视频")
        strategy, detail = _choose_strategy(audio_seconds, video_seconds, trim_anchor)
        if entry.manifest_seconds is not None and abs(entry.manifest_seconds - audio_seconds) > 0.3:
            detail = _append_detail(detail, f"清单时长 {entry.manifest_seconds:.2f}s，实测 {audio_seconds:.2f}s")
        segment = DubSegment(
            index=entry.index,
            text=entry.text,
            audio=entry.path,
            audio_seconds=audio_seconds,
            video=video,
            video_seconds=video_seconds,
            strategy=strategy,
            adjust_detail=detail,
            output_seconds=audio_seconds,
        )
        segments.append(segment)

        segment_file = segment_dir / f"{position:03d}_{_safe_name(video.stem)}.mp4"
        try:
            _render_segment(config, segment, segment_file, burn_subtitle, segment_dir)
        except Exception as exc:
            segment.strategy = "failed"
            segment.adjust_detail = _append_detail(segment.adjust_detail, _short_error(exc))
            segment.output_seconds = 0.0
            segment.segment_file = None
            if progress:
                progress(position, _dub_total)
            continue
        segment.segment_file = segment_file
        successful.append(segment_file)
        if progress:
            progress(position, _dub_total)

    total_seconds = round(sum(segment.audio_seconds for segment in segments if segment.segment_file), 3)
    _write_plan(plan_csv, plan_json, output_path, segments, total_seconds, unmatched_audio, unmatched_video)

    if not successful:
        raise RuntimeError(f"配音对齐成片没有成功段。对齐表已写入：{plan_csv}")

    concat_list = _concat_segments(config, successful, output_path)
    if progress:
        progress(_dub_total, _dub_total)
    if not keep_segments:
        _cleanup_success_artifacts(segment_dir, concat_list)

    return DubAssembleResult(
        output_path=output_path,
        plan_csv=plan_csv,
        plan_json=plan_json,
        segments=segments,
        total_seconds=total_seconds,
        unmatched_audio=unmatched_audio,
        unmatched_video=unmatched_video,
    )


def has_audio_stream(config: ProjectConfig, video: Path) -> bool:
    ffprobe = config.tools.ffprobe or "ffprobe"
    command = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        str(video),
    ]
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _load_audio_entries(audio_root: Path) -> list[_AudioEntry]:
    manifest = _find_manifest(audio_root)
    if manifest:
        with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = [dict(row) for row in csv.DictReader(handle)]
        entries: list[_AudioEntry] = []
        for row_number, row in enumerate(rows, start=1):
            audio_file = _pick(row, ["audio_file", "音频文件", "wav", "file"])
            if not audio_file:
                continue
            entries.append(
                _AudioEntry(
                    index=_parse_int(_pick(row, ["index", "序号", "编号"], str(row_number)), row_number),
                    text=_pick(row, ["text", "文案", "台词", "字幕"]),
                    path=_resolve_audio_file(audio_root, audio_file),
                    manifest_seconds=_parse_optional_float(_pick(row, ["duration_seconds", "duration", "时长"])),
                )
            )
        if not entries:
            raise RuntimeError(f"配音清单没有有效 audio_file 行：{manifest}")
        return sorted(entries, key=lambda item: (item.index, _natural_key(item.path.name)))

    wavs = _iter_flat_media(audio_root, WAV_EXTENSIONS)
    if not wavs:
        return []
    return [_AudioEntry(index=index, text="", path=path) for index, path in enumerate(wavs, start=1)]


def _find_manifest(audio_root: Path) -> Path | None:
    exact = audio_root / DUB_MANIFEST_NAME
    if exact.exists():
        return exact
    candidates = sorted(audio_root.glob("配音清单*.csv"), key=lambda item: (item.stat().st_mtime, item.name))
    return candidates[-1] if candidates else None


def _resolve_audio_file(audio_root: Path, token: str) -> Path:
    raw = Path(str(token).strip().strip('"'))
    if raw.is_absolute():
        return raw
    return audio_root / raw


def _render_segment(
    config: ProjectConfig,
    segment: DubSegment,
    output: Path,
    burn_subtitle: bool,
    segment_dir: Path,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    vf_parts = _video_filter_parts(config, segment)
    if burn_subtitle and segment.text.strip():
        subtitle_filter, subtitle_note = _subtitle_filter(config, segment, segment_dir)
        if subtitle_filter:
            vf_parts.append(subtitle_filter)
        elif subtitle_note:
            segment.adjust_detail = _append_detail(segment.adjust_detail, subtitle_note)

    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(segment.video),
        "-i",
        str(segment.audio),
        "-filter:v",
        ",".join(vf_parts),
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
        f"{segment.audio_seconds:.3f}",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]
    _run(command, "配音段合成")


def _video_filter_parts(config: ProjectConfig, segment: DubSegment) -> list[str]:
    audio = max(0.1, segment.audio_seconds)
    video = max(0.1, segment.video_seconds)
    diff = audio - video
    width = max(2, int(config.edit.output_width))
    height = max(2, int(config.edit.output_height))

    if segment.strategy.startswith("trim_"):
        trim_start = 0.0
        if segment.strategy == "trim_head":
            trim_start = max(0.0, video - audio)
        elif segment.strategy == "trim_center":
            trim_start = max(0.0, (video - audio) / 2)
        first = f"trim=start={trim_start:.3f}:duration={audio:.3f},setpts=PTS-STARTPTS"
    elif segment.strategy == "slow":
        factor = audio / video
        first = f"setpts=(PTS-STARTPTS)*{factor:.8f},trim=duration={audio:.3f}"
    elif segment.strategy == "freeze":
        first = f"tpad=stop_mode=clone:stop_duration={max(0.0, diff):.3f},trim=duration={audio:.3f},setpts=PTS-STARTPTS"
    else:
        parts = []
        if diff > 0.001:
            parts.append(f"tpad=stop_mode=clone:stop_duration={diff:.3f}")
        parts.append(f"trim=duration={audio:.3f}")
        parts.append("setpts=PTS-STARTPTS")
        first = ",".join(parts)

    return [
        first,
        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        "setsar=1",
        "fps=30",
    ]


def _subtitle_filter(config: ProjectConfig, segment: DubSegment, segment_dir: Path) -> tuple[str, str]:
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if not font.exists():
        return "", "字体缺失，未烧字幕"
    text_path = segment_dir / f"subtitle_{segment.index:03d}.txt"
    text_path.write_text(_subtitle_text(segment.text), encoding="utf-8")
    fontsize = max(18, int(config.edit.output_height / 22))
    return (
        "drawtext="
        f"fontfile='{_filter_path(font)}':"
        f"textfile='{_filter_path(text_path)}':"
        f"fontsize={fontsize}:fontcolor=white:borderw=3:bordercolor=black:"
        "x=(w-text_w)/2:y=h-text_h-96",
        "",
    )


def _choose_strategy(audio_seconds: float, video_seconds: float, trim_anchor: str) -> tuple[str, str]:
    diff = video_seconds - audio_seconds
    if abs(diff) <= 0.1:
        if diff > 0:
            return "as_is", f"差值 {abs(diff):.2f}s，保持原速对齐"
        if diff < 0:
            return "as_is", f"差值 {abs(diff):.2f}s，保持原速补齐"
        return "as_is", "时长一致"

    if video_seconds > audio_seconds:
        trim_seconds = video_seconds - audio_seconds
        if trim_anchor == "tail":
            return "trim_head", f"裁头 {trim_seconds:.2f}s"
        if trim_anchor == "center":
            return "trim_center", f"居中裁剪 {trim_seconds:.2f}s"
        return "trim_tail", f"裁尾 {trim_seconds:.2f}s"

    gap = audio_seconds - video_seconds
    if gap <= audio_seconds * 0.15:
        speed = video_seconds / audio_seconds
        return "slow", f"放慢至 {speed:.2f}x"
    return "freeze", f"末帧定格 {gap:.2f}s"


def _write_plan(
    csv_path: Path,
    json_path: Path,
    output_path: Path,
    segments: list[DubSegment],
    total_seconds: float,
    unmatched_audio: list[str],
    unmatched_video: list[str],
) -> None:
    strategy_counts = Counter(segment.strategy for segment in segments)
    fieldnames = [
        "row_type",
        "index",
        "text",
        "audio",
        "audio_seconds",
        "video",
        "video_seconds",
        "strategy",
        "adjust_detail",
        "output_seconds",
        "segment_file",
        "unmatched_audio",
        "unmatched_video",
        "total_seconds",
        "segment_count",
        "strategy_counts",
        "output_path",
    ]
    rows: list[dict[str, Any]] = []
    for segment in segments:
        row = _segment_payload(segment)
        row["row_type"] = "segment"
        row["strategy_counts"] = ""
        row["output_path"] = str(output_path)
        rows.append(row)
    for item in unmatched_audio:
        rows.append({"row_type": "unmatched_audio", "unmatched_audio": item, "output_path": str(output_path)})
    for item in unmatched_video:
        rows.append({"row_type": "unmatched_video", "unmatched_video": item, "output_path": str(output_path)})
    rows.append(
        {
            "row_type": "summary",
            "total_seconds": f"{total_seconds:.3f}",
            "segment_count": sum(1 for segment in segments if segment.segment_file),
            "strategy_counts": json.dumps(dict(strategy_counts), ensure_ascii=False),
            "output_path": str(output_path),
        }
    )

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})

    payload = {
        "output_path": str(output_path),
        "plan_csv": str(csv_path),
        "segments": [_segment_payload(segment) for segment in segments],
        "total_seconds": total_seconds,
        "unmatched_audio": unmatched_audio,
        "unmatched_video": unmatched_video,
        "strategy_counts": dict(strategy_counts),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _segment_payload(segment: DubSegment) -> dict[str, Any]:
    return {
        "index": segment.index,
        "text": segment.text,
        "audio": str(segment.audio),
        "audio_seconds": round(segment.audio_seconds, 3),
        "video": str(segment.video),
        "video_seconds": round(segment.video_seconds, 3),
        "strategy": segment.strategy,
        "adjust_detail": segment.adjust_detail,
        "output_seconds": round(segment.output_seconds, 3),
        "segment_file": str(segment.segment_file or ""),
    }


def _iter_flat_media(root: Path, extensions: set[str]) -> list[Path]:
    if not root.exists():
        return []
    files = [item for item in root.iterdir() if item.is_file() and item.suffix.lower() in extensions]
    return sorted(files, key=lambda item: _natural_key(item.name))


def _resolve_user_path(config: ProjectConfig, path: Path) -> Path:
    if path.is_absolute():
        return path
    direct = Path.cwd() / path
    if direct.exists():
        return direct
    return config.root / path


def _required_duration(config: ProjectConfig, path: Path, label: str) -> float:
    if not path.exists():
        raise FileNotFoundError(f"{label}文件不存在：{path}")
    duration = _probe_duration(config, path)
    if duration is None or duration <= 0:
        raise RuntimeError(f"无法读取{label}时长：{path}")
    return float(duration)


def _content_summary(root: Path, extensions: set[str]) -> str:
    files = _iter_flat_media(root, extensions)
    examples = "、".join(path.name for path in files[:5]) or "无"
    manifest = root / DUB_MANIFEST_NAME
    manifest_state = "有配音清单" if manifest.exists() else "无配音清单"
    return f"{root}（{manifest_state}，匹配文件 {len(files)} 个，示例：{examples}）"


def _unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = path.with_name(f"{path.name}_{stamp}")
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}_{stamp}_{index}")
        index += 1
    return candidate


def _natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def _pick(row: dict[str, Any], names: list[str], default: str = "") -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _parse_int(value: str, default: int) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _parse_optional_float(value: str) -> float | None:
    if not value:
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _append_detail(current: str, extra: str) -> str:
    extra = extra.strip()
    if not extra:
        return current
    return f"{current}；{extra}" if current else extra


def _subtitle_text(text: str) -> str:
    compact = " ".join(str(text).strip().split())
    if len(compact) > 36:
        compact = compact[:35] + "..."
    lines = [compact[index : index + 18] for index in range(0, len(compact), 18)]
    return "\n".join(lines[:2])


def _filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _short_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text.replace("\r", " ").replace("\n", " ")[:500]


def _run(command: list[str], label: str) -> None:
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr[-3000:] if completed.stderr else completed.stdout[-3000:]
        raise RuntimeError(f"{label}失败：{detail.strip() or '未知错误'}")
