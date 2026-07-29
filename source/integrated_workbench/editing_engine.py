from __future__ import annotations

import csv
import json
import shutil
from .proc import run_silent
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    DIR_A,
    DIR_AUTO_EDIT_INPUT,
    DIR_AUTO_EDIT_OUTPUT,
    DIR_AUTO_EDIT_PREVIEW,
    DIR_LOGS,
    ProjectConfig,
    VIDEO_EXTENSIONS,
)
from .project import iter_media, log_line, require_files


@dataclass
class StoryboardShot:
    shot_id: str
    source: Path
    start: float
    duration: float
    text: str = ""
    speaker: str = ""
    voice_type: str = "旁白"
    note: str = ""
    selected_clip: Path | None = None
    audio_placeholder: Path | None = None


@dataclass
class AutoEditResult:
    work_dir: Path
    selected_dir: Path
    audio_dir: Path
    preview_path: Path | None
    plan_csv: Path
    import_csv: Path
    handoff_json: Path
    contact_sheet: Path | None
    selected_count: int
    total_seconds: float


def _run(command: list[str]) -> None:
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr[-3000:] if completed.stderr else completed.stdout[-3000:]
        raise RuntimeError(detail.strip() or "ffmpeg 执行失败")


def _probe_duration(config: ProjectConfig, path: Path) -> float | None:
    ffprobe = config.tools.ffprobe or "ffprobe"
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def _parse_float(value: Any, default: float) -> float:
    if value is None:
        return default
    text = str(value).strip().replace("秒", "")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _pick(row: dict[str, Any], names: list[str], default: str = "") -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _safe_folder_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid else char for char in name).strip()
    return cleaned or datetime.now().strftime("auto_edit_%Y%m%d_%H%M%S")


def _timecode(seconds: float) -> str:
    seconds = max(0.0, seconds)
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    hours = whole // 3600
    minutes = (whole % 3600) // 60
    secs = whole % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _read_table_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() in {".csv", ".txt"}:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        try:
            import openpyxl  # type: ignore
        except ImportError as exc:
            raise RuntimeError("读取 Excel 分镜表需要安装 openpyxl；也可以先另存为 CSV。") from exc

        workbook = openpyxl.load_workbook(path, data_only=True)
        sheet = workbook.active
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(cell).strip() if cell is not None else "" for cell in rows[0]]
        result: list[dict[str, Any]] = []
        for values in rows[1:]:
            if not any(value is not None and str(value).strip() for value in values):
                continue
            result.append({headers[index]: value for index, value in enumerate(values) if index < len(headers)})
        return result

    raise RuntimeError("分镜表目前支持 CSV、TXT、XLSX、XLSM。")


def _match_source(token: str, script_dir: Path, input_dir: Path, sources: list[Path]) -> Path | None:
    token = token.strip().strip('"')
    if not token:
        return None

    raw = Path(token)
    candidates = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(script_dir / raw)
        candidates.append(input_dir / raw)

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    normalized = token.lower()
    for source in sources:
        if source.name.lower() == normalized or source.stem.lower() == normalized:
            return source
    for source in sources:
        if normalized in source.name.lower():
            return source
    return None


def _load_storyboard_script(
    config: ProjectConfig,
    script_path: Path,
    input_dir: Path,
    sources: list[Path],
    target_seconds: float,
    default_clip_seconds: float,
) -> list[StoryboardShot]:
    rows = _read_table_rows(script_path)
    if not rows:
        return []

    shots: list[StoryboardShot] = []
    source_index = 0
    total = 0.0
    for row_index, row in enumerate(rows, start=1):
        if total >= target_seconds:
            break

        source_token = _pick(row, ["file", "candidate_file", "视频文件", "视频", "素材", "片段", "文件"])
        source = _match_source(source_token, script_path.parent, input_dir, sources) if source_token else None
        note = _pick(row, ["note", "备注", "提示词"])
        if source is None:
            source = sources[source_index % len(sources)]
            source_index += 1
            note = (note + "；" if note else "") + "未指定素材，按顺序自动分配"

        source_duration = _probe_duration(config, source)
        start = _parse_float(_pick(row, ["start", "起点", "开始秒", "开始时间"]), 0.0)
        remaining_source = max(0.5, (source_duration - start) if source_duration else default_clip_seconds)
        requested = _parse_float(_pick(row, ["duration", "时长", "秒数", "持续秒数"]), default_clip_seconds)
        duration = max(0.5, min(requested, remaining_source, target_seconds - total))

        shot_id = _pick(row, ["shot_id", "镜号", "分镜", "分镜编号", "镜头", "编号"], f"S{row_index:03d}")
        shots.append(
            StoryboardShot(
                shot_id=shot_id,
                source=source,
                start=start,
                duration=duration,
                text=_pick(row, ["text", "台词", "文案", "旁白", "字幕"]),
                speaker=_pick(row, ["speaker", "人物", "角色", "说话人"]),
                voice_type=_pick(row, ["voice_type", "声音类型", "配音类型", "类型"], "旁白"),
                note=note,
            )
        )
        total += duration
    return shots


def build_auto_edit_shots(
    config: ProjectConfig,
    input_dir: str | Path | None = None,
    script_path: str | Path | None = None,
    target_seconds: float | None = None,
    clip_seconds: float | None = None,
) -> list[StoryboardShot]:
    recipe = config.edit
    target = target_seconds or recipe.target_seconds
    default_clip = clip_seconds or recipe.clip_seconds
    auto_input = config.root / DIR_AUTO_EDIT_INPUT
    chosen_input = Path(input_dir) if input_dir else auto_input
    sources = iter_media(chosen_input, VIDEO_EXTENSIONS)
    if not sources and chosen_input != config.root / DIR_A:
        sources = iter_media(config.root / DIR_A, VIDEO_EXTENSIONS)
        chosen_input = config.root / DIR_A
    sources = require_files("自动剪辑输入视频", sources)

    if script_path:
        scripted = _load_storyboard_script(config, Path(script_path), chosen_input, sources, target, default_clip)
        if scripted:
            return scripted

    shots: list[StoryboardShot] = []
    total = 0.0
    for index, source in enumerate(sources, start=1):
        if total >= target:
            break
        source_duration = _probe_duration(config, source)
        duration = min(default_clip, target - total)
        if source_duration:
            duration = min(duration, max(0.5, source_duration))
        shots.append(
            StoryboardShot(
                shot_id=f"S{index:03d}",
                source=source,
                start=0.0,
                duration=max(0.5, duration),
                voice_type="原声/待配音",
                note="自动按文件名顺序选入",
            )
        )
        total += shots[-1].duration
    return shots


def _trim_shot(config: ProjectConfig, shot: StoryboardShot, output: Path) -> None:
    recipe = config.edit
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    # Assemble reuses these clips, so each segment is normalized before concat.
    vf = (
        f"scale={recipe.output_width}:{recipe.output_height}:force_original_aspect_ratio=decrease,"
        f"pad={recipe.output_width}:{recipe.output_height}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,fps={recipe.fps}"
    )
    command = [
        ffmpeg,
        "-y",
        "-ss",
        f"{shot.start:.3f}",
        "-t",
        f"{shot.duration:.3f}",
        "-i",
        str(shot.source),
        "-vf",
        vf,
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
        "-shortest",
        str(output),
    ]
    _run(command)


def _make_silent_wav(config: ProjectConfig, duration: float, output: Path) -> None:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    command = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=stereo",
        "-t",
        f"{duration:.3f}",
        "-c:a",
        "pcm_s16le",
        str(output),
    ]
    _run(command)


def _concat_file_line(path: Path) -> str:
    normalized = str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
    return f"file '{normalized}'"


def _render_preview(
    config: ProjectConfig,
    shots: list[StoryboardShot],
    work_dir: Path,
    output: Path,
    video_filter: str = "",
) -> Path:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    concat_list = work_dir / "concat_list.txt"
    concat_list.write_text("\n".join(_concat_file_line(shot.selected_clip or shot.source) for shot in shots), encoding="utf-8")

    video_only = work_dir / "preview_video_only.mp4"
    total_seconds = sum(shot.duration for shot in shots)
    timeline_audio = work_dir / "timeline_silence.wav"
    _make_silent_wav(config, total_seconds, timeline_audio)

    filter_args = ["-vf", video_filter] if video_filter else []
    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_list),
            *filter_args,
            "-c:v",
            "libx264",
            "-preset",
            config.recipe.preset,
            "-crf",
            str(config.recipe.crf),
            "-an",
            str(video_only),
        ]
    )
    _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(video_only),
            "-i",
            str(timeline_audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-shortest",
            str(output),
        ]
    )
    return output


def _make_contact_sheet(config: ProjectConfig, preview: Path, output: Path) -> Path:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    duration = _probe_duration(config, preview) or 0.0
    if duration <= 5:
        fps_filter = "fps=1"
    elif duration <= 30:
        fps_filter = "fps=1/3"
    else:
        fps_filter = "fps=1/5"
    _run(
        [
            ffmpeg,
            "-y",
            "-i",
            str(preview),
            "-vf",
            f"{fps_filter},scale=216:-1,tile=5x4",
            "-frames:v",
            "1",
            str(output),
        ]
    )
    return output


def _write_plan_csv(shots: list[StoryboardShot], path: Path) -> None:
    fieldnames = [
        "shot_id",
        "source_path",
        "selected_clip",
        "start_seconds",
        "duration_seconds",
        "speaker",
        "voice_type",
        "text",
        "audio_placeholder",
        "note",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for shot in shots:
            writer.writerow(
                {
                    "shot_id": shot.shot_id,
                    "source_path": str(shot.source),
                    "selected_clip": str(shot.selected_clip or ""),
                    "start_seconds": f"{shot.start:.3f}",
                    "duration_seconds": f"{shot.duration:.3f}",
                    "speaker": shot.speaker,
                    "voice_type": shot.voice_type,
                    "text": shot.text,
                    "audio_placeholder": str(shot.audio_placeholder or ""),
                    "note": shot.note,
                }
            )


def _write_import_csv(shots: list[StoryboardShot], path: Path) -> None:
    fieldnames = [
        "track_type",
        "order",
        "shot_id",
        "path",
        "timeline_start",
        "duration_seconds",
        "speaker",
        "voice_type",
        "text",
    ]
    timeline = 0.0
    rows: list[dict[str, str]] = []
    for index, shot in enumerate(shots, start=1):
        rows.append(
            {
                "track_type": "video",
                "order": str(index),
                "shot_id": shot.shot_id,
                "path": str(shot.selected_clip or ""),
                "timeline_start": _timecode(timeline),
                "duration_seconds": f"{shot.duration:.3f}",
                "speaker": shot.speaker,
                "voice_type": shot.voice_type,
                "text": shot.text,
            }
        )
        rows.append(
            {
                "track_type": "audio_placeholder",
                "order": str(index),
                "shot_id": shot.shot_id,
                "path": str(shot.audio_placeholder or ""),
                "timeline_start": _timecode(timeline),
                "duration_seconds": f"{shot.duration:.3f}",
                "speaker": shot.speaker,
                "voice_type": shot.voice_type,
                "text": shot.text,
            }
        )
        timeline += shot.duration

    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_handoff(
    config: ProjectConfig,
    result: AutoEditResult,
    shots: list[StoryboardShot],
    path: Path,
) -> None:
    manifest: dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "project_name": config.project_name,
        "drama_name": config.drama_name,
        "project_root": str(config.root),
        "task_type": "自动化剪辑素材包",
        "work_dir": str(result.work_dir),
        "selected_dir": str(result.selected_dir),
        "audio_dir": str(result.audio_dir),
        "preview_path": str(result.preview_path or ""),
        "plan_csv": str(result.plan_csv),
        "import_csv": str(result.import_csv),
        "contact_sheet": str(result.contact_sheet or ""),
        "selected_count": result.selected_count,
        "total_seconds": round(result.total_seconds, 3),
        "shots": [
            {
                "shot_id": shot.shot_id,
                "source_path": str(shot.source),
                "selected_clip": str(shot.selected_clip or ""),
                "audio_placeholder": str(shot.audio_placeholder or ""),
                "start_seconds": shot.start,
                "duration_seconds": shot.duration,
                "speaker": shot.speaker,
                "voice_type": shot.voice_type,
                "text": shot.text,
                "note": shot.note,
            }
            for shot in shots
        ],
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)


def run_auto_edit(
    config: ProjectConfig,
    input_dir: str | Path | None = None,
    script_path: str | Path | None = None,
    target_seconds: float | None = None,
    clip_seconds: float | None = None,
    output_name: str | None = None,
    make_preview: bool = True,
    video_filter: str = "",
    progress: Callable[[int, int], None] | None = None,
) -> AutoEditResult:
    if not config.tools.ffmpeg:
        raise FileNotFoundError("没有找到 ffmpeg。请在 project.json 里配置 tools.ffmpeg。")

    shots = build_auto_edit_shots(
        config,
        input_dir=input_dir,
        script_path=script_path,
        target_seconds=target_seconds,
        clip_seconds=clip_seconds,
    )
    shots = require_files("自动剪辑分镜", shots)
    # 进度：逐分镜裁剪 + 预览渲染合成一步
    _ae_total = len(shots) + (1 if make_preview else 0)
    _ae_done = 0
    if progress:
        progress(0, max(1, _ae_total))

    folder_name = _safe_folder_name(output_name or datetime.now().strftime("auto_edit_%Y%m%d_%H%M%S"))
    work_dir = config.root / DIR_AUTO_EDIT_OUTPUT / folder_name
    selected_dir = work_dir / "01_筛选分镜"
    audio_dir = work_dir / "02_音频占位"
    frame_check_dir = work_dir / "03_抽帧检查"
    for directory in [work_dir, selected_dir, audio_dir, frame_check_dir, config.root / DIR_LOGS]:
        directory.mkdir(parents=True, exist_ok=True)

    log_line(config, f"开始自动化剪辑：{work_dir}")
    for index, shot in enumerate(shots, start=1):
        clip_path = selected_dir / f"{index:03d}_{shot.shot_id}_{shot.source.stem}.mp4"
        audio_path = audio_dir / f"{index:03d}_{shot.shot_id}_待配音.wav"
        _trim_shot(config, shot, clip_path)
        _make_silent_wav(config, shot.duration, audio_path)
        shot.selected_clip = clip_path
        shot.audio_placeholder = audio_path
        _ae_done += 1
        if progress:
            progress(_ae_done, max(1, _ae_total))

    plan_csv = work_dir / "自动剪辑计划.csv"
    import_csv = work_dir / "剪映导入清单.csv"
    _write_plan_csv(shots, plan_csv)
    _write_import_csv(shots, import_csv)

    preview_path: Path | None = None
    contact_sheet: Path | None = None
    if make_preview:
        preview_path = work_dir / "自动剪辑预览.mp4"
        _render_preview(config, shots, work_dir, preview_path, video_filter=video_filter)
        preview_public = config.root / DIR_AUTO_EDIT_PREVIEW / f"{work_dir.name}_自动剪辑预览.mp4"
        preview_public.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(preview_path, preview_public)
        _ae_done += 1
        if progress:
            progress(_ae_done, max(1, _ae_total))
        if config.edit.make_contact_sheet:
            contact_sheet = frame_check_dir / "预览抽帧.jpg"
            try:
                _make_contact_sheet(config, preview_path, contact_sheet)
            except RuntimeError as exc:
                log_line(config, f"抽帧检查图生成失败：{exc}")
                contact_sheet = None

    result = AutoEditResult(
        work_dir=work_dir,
        selected_dir=selected_dir,
        audio_dir=audio_dir,
        preview_path=preview_path,
        plan_csv=plan_csv,
        import_csv=import_csv,
        handoff_json=work_dir / "edit_handoff.json",
        contact_sheet=contact_sheet,
        selected_count=len(shots),
        total_seconds=sum(shot.duration for shot in shots),
    )
    _write_handoff(config, result, shots, result.handoff_json)
    log_line(config, f"自动化剪辑完成：{result.selected_count} 个分镜，{result.total_seconds:.1f} 秒")
    return result
