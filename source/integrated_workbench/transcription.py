from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
from .proc import run_silent
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .model_registry import best_verified_model, whisper_available as _registry_whisper_available, whisper_cli_path, whisper_root
from .models import DIR_SCRIPT, DIR_TRANSCRIPTS, ProjectConfig, VIDEO_EXTENSIONS
from .project import iter_project_media, log_line, require_files


@dataclass
class TranscriptCue:
    index: int
    start: float
    end: float
    text: str
    source: str


@dataclass
class TranscriptResult:
    video_path: str
    engine: str
    mode: str
    cue_count: int
    srt_path: Path
    csv_path: Path
    json_path: Path
    copied_srt_path: Path | None
    copied_csv_path: Path | None
    message: str
    fallback_reason: str = ""


@dataclass
class WhisperEnvironment:
    executable: Path | None
    model: Path | None
    detail: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.executable and self.model)

    @property
    def message(self) -> str:
        if self.ready:
            return f"真实语音转写已就绪：{self.executable}，模型：{self.model}"
        if not self.executable and not self.model:
            return f"真实语音转写未就绪：缺少 whisper-cli.exe 和校验通过的 ggml 模型；当前会生成可校对时间轴草稿。{self.detail}"
        if not self.executable:
            return f"真实语音转写未就绪：已找到模型 {self.model}，但缺少 whisper-cli.exe。{self.detail}"
        return f"真实语音转写未就绪：已找到 {self.executable}，但缺少校验通过的 ggml-tiny/base/small 模型。{self.detail}"


def whisper_environment() -> WhisperEnvironment:
    executable, model = _whisper_paths()
    _ok, detail = whisper_available()
    return WhisperEnvironment(executable, model, detail)


def whisper_available(project_root: str | Path | None = None) -> tuple[bool, str]:
    return _registry_whisper_available(project_root)


def generate_transcript(
    config: ProjectConfig,
    video_path: str | Path,
    engine: str = "auto",
    text: str = "",
    text_file: str | Path | None = None,
    copy_to_script: bool = True,
    silence_db: float = -35.0,
    min_silence: float = 0.45,
) -> TranscriptResult:
    video = Path(video_path)
    if not video.exists():
        raise FileNotFoundError(video)
    output_dir = config.root / DIR_TRANSCRIPTS / _safe_stem(video.stem)
    output_dir.mkdir(parents=True, exist_ok=True)

    reference_text = _load_reference_text(text, text_file)
    requested_engine = engine if engine in {"auto", "whisper", "timeline"} else "auto"
    fallback_reason = ""
    if requested_engine in {"auto", "whisper"}:
        try:
            whisper, model_label = _try_whisper_cpp(config, video, output_dir)
            if whisper:
                return _finish_result(
                    config,
                    video,
                    output_dir,
                    whisper,
                    f"real(whisper-{model_label})",
                    "asr",
                    copy_to_script,
                    "whisper.cpp 真实转写完成。",
                )
            fallback_reason = "whisper-cli.exe 或校验通过的 ggml 模型未就绪。"
        except Exception as exc:
            fallback_reason = str(exc).strip() or exc.__class__.__name__
            log_line(config, f"真实转写降级：{video.name}，原因：{fallback_reason}")

    cues = build_timeline_cues(config, video, reference_text, silence_db=silence_db, min_silence=min_silence)
    if not fallback_reason:
        fallback_reason = "用户选择 timeline 引擎。"
    message = "已生成可校对时间轴草稿；请在文案分镜中校对台词后再用于字幕/台词剪辑。"
    return _finish_result(config, video, output_dir, cues, "mock_timeline", "draft", copy_to_script, message, fallback_reason)


def generate_transcripts_for_project(
    config: ProjectConfig,
    source: str = "auto_input",
    engine: str = "auto",
    text: str = "",
    text_file: str | Path | None = None,
    copy_to_script: bool = True,
) -> list[TranscriptResult]:
    videos = _source_videos(config, source)
    videos = require_files("字幕转写视频", videos)
    return [
        generate_transcript(
            config,
            video,
            engine=engine,
            text=text,
            text_file=text_file,
            copy_to_script=copy_to_script,
        )
        for video in videos
    ]


def build_timeline_cues(
    config: ProjectConfig,
    video: Path,
    reference_text: str = "",
    silence_db: float = -35.0,
    min_silence: float = 0.45,
) -> list[TranscriptCue]:
    duration = _duration(config, video)
    if duration <= 0:
        raise RuntimeError(f"无法读取视频时长：{video}")
    silences = _detect_silences(config, video, silence_db, min_silence)
    speech_ranges = _invert_silences(duration, silences, pad=0.08)
    if not speech_ranges:
        speech_ranges = _fixed_ranges(duration, 5.0)
    speech_ranges = _split_long_ranges(speech_ranges, max_seconds=7.0)
    lines = _split_reference_text(reference_text)
    cues: list[TranscriptCue] = []
    for index, (start, end) in enumerate(speech_ranges, start=1):
        if end - start < 0.25:
            continue
        text = lines[index - 1] if index - 1 < len(lines) else f"待补台词 {index:03d}"
        cues.append(TranscriptCue(index=index, start=round(start, 3), end=round(end, 3), text=text, source=str(video)))
    return cues


def latest_transcript_dir(config: ProjectConfig) -> Path | None:
    root = config.root / DIR_TRANSCRIPTS
    if not root.exists():
        return None
    dirs = [item for item in root.iterdir() if item.is_dir()]
    return max(dirs, key=lambda item: item.stat().st_mtime) if dirs else None


def _finish_result(
    config: ProjectConfig,
    video: Path,
    output_dir: Path,
    cues: list[TranscriptCue],
    engine: str,
    mode: str,
    copy_to_script: bool,
    message: str,
    fallback_reason: str = "",
) -> TranscriptResult:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = f"{video.stem}_{stamp}_字幕草稿"
    srt_path = output_dir / f"{base}.srt"
    csv_path = output_dir / f"{base}.csv"
    json_path = output_dir / f"{base}.json"
    _write_srt(cues, srt_path, engine, fallback_reason)
    _write_csv(cues, csv_path, engine, fallback_reason)
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "video_path": str(video),
        "engine": engine,
        "mode": mode,
        "fallback_reason": fallback_reason,
        "cue_count": len(cues),
        "srt_path": str(srt_path),
        "csv_path": str(csv_path),
        "cues": [asdict(cue) for cue in cues],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    copied_srt: Path | None = None
    copied_csv: Path | None = None
    if copy_to_script:
        script_dir = config.root / DIR_SCRIPT
        script_dir.mkdir(parents=True, exist_ok=True)
        copied_srt = script_dir / srt_path.name
        copied_csv = script_dir / csv_path.name
        shutil.copy2(srt_path, copied_srt)
        shutil.copy2(csv_path, copied_csv)

    log_line(config, f"字幕转写完成：{video.name}，{len(cues)} 条，engine={engine}")
    return TranscriptResult(str(video), engine, mode, len(cues), srt_path, csv_path, json_path, copied_srt, copied_csv, message, fallback_reason)


def _try_whisper_cpp(config: ProjectConfig, video: Path, output_dir: Path) -> tuple[list[TranscriptCue] | None, str]:
    executable, model = _whisper_paths(config.root)
    if not executable or not model:
        return None, ""
    runtime_root = whisper_root()
    scratch_parent = runtime_root / "runtime_downloads"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    scratch_dir = Path(tempfile.mkdtemp(prefix="transcribe_", dir=scratch_parent))
    wav = scratch_dir / "input_16k.wav"
    duration = _duration(config, video)
    timeout = max(60, int(duration * 3 + 60))
    try:
        _extract_wav(config, video, wav)
    except Exception as exc:
        raise RuntimeError(f"音轨预处理失败：{exc}") from exc
    out_base = scratch_dir / "whisper_output"
    command = [
        str(executable),
        "-m",
        _relative_cli_arg(model, runtime_root),
        "-f",
        _relative_cli_arg(wav, runtime_root),
        "-osrt",
        "-of",
        _relative_cli_arg(out_base, runtime_root),
    ]
    try:
        completed = run_silent(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(runtime_root),
        )
        if completed.returncode != 0:
            detail = completed.stderr[-1500:] if completed.stderr else completed.stdout[-1500:]
            raise RuntimeError(detail.strip() or "whisper.cpp 转写失败")
        srt = out_base.with_suffix(".srt")
        if not srt.exists():
            return None, ""
        cues = _read_srt(srt, video)
        return cues or None, model.stem.replace("ggml-", "")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("转写超时") from exc
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _whisper_paths(project_root: str | Path | None = None) -> tuple[Path | None, Path | None]:
    return whisper_cli_path(), best_verified_model(project_root)


def _relative_cli_arg(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _extract_wav(config: ProjectConfig, video: Path, output: Path) -> None:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    command = [ffmpeg, "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output)]
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr[-1500:] if completed.stderr else completed.stdout[-1500:]
        raise RuntimeError(detail.strip() or "音频提取失败")


def _source_videos(config: ProjectConfig, source: str) -> list[Path]:
    from .models import DIR_A, DIR_AUTO_EDIT_INPUT

    if source == "original":
        return iter_project_media(config, DIR_A, VIDEO_EXTENSIONS)
    if source == "all":
        return iter_project_media(config, DIR_AUTO_EDIT_INPUT, VIDEO_EXTENSIONS) + iter_project_media(config, DIR_A, VIDEO_EXTENSIONS)
    return iter_project_media(config, DIR_AUTO_EDIT_INPUT, VIDEO_EXTENSIONS) or iter_project_media(config, DIR_A, VIDEO_EXTENSIONS)


def _duration(config: ProjectConfig, video: Path) -> float:
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
    return _float(completed.stdout.strip())


def _detect_silences(config: ProjectConfig, video: Path, silence_db: float, min_silence: float) -> list[tuple[float, float]]:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    completed = run_silent(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(video),
            "-af",
            f"silencedetect=noise={silence_db}dB:d={min_silence}",
            "-vn",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    text = completed.stderr + "\n" + completed.stdout
    starts = [_float(item) for item in re.findall(r"silence_start:\s*([0-9.]+)", text)]
    ends = [_float(item) for item in re.findall(r"silence_end:\s*([0-9.]+)", text)]
    return list(zip(starts, ends))


def _invert_silences(duration: float, silences: list[tuple[float, float]], pad: float) -> list[tuple[float, float]]:
    if not silences:
        return _fixed_ranges(duration, 5.0)
    ranges: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in silences:
        keep_start = cursor
        keep_end = max(cursor, start + pad)
        if keep_end - keep_start >= 0.4:
            ranges.append((keep_start, keep_end))
        cursor = max(cursor, end - pad)
    if duration - cursor >= 0.4:
        ranges.append((cursor, duration))
    return ranges


def _fixed_ranges(duration: float, seconds: float) -> list[tuple[float, float]]:
    ranges: list[tuple[float, float]] = []
    cursor = 0.0
    while cursor < duration:
        end = min(duration, cursor + seconds)
        if end - cursor >= 0.25:
            ranges.append((cursor, end))
        cursor = end
    return ranges


def _split_long_ranges(ranges: list[tuple[float, float]], max_seconds: float) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for start, end in ranges:
        cursor = start
        while end - cursor > max_seconds:
            result.append((cursor, cursor + max_seconds))
            cursor += max_seconds
        if end - cursor >= 0.25:
            result.append((cursor, end))
    return result


def _load_reference_text(text: str, text_file: str | Path | None) -> str:
    if text_file:
        path = Path(text_file)
        if path.exists():
            return path.read_text(encoding="utf-8-sig", errors="replace")
    return text or ""


def _split_reference_text(text: str) -> list[str]:
    cleaned = (text or "").strip()
    if not cleaned:
        return []
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if len(lines) > 1:
        return lines
    parts = [item.strip() for item in re.split(r"(?<=[。！？!?；;])|[,，、]", cleaned) if item.strip()]
    return parts or [cleaned]


def _write_srt(cues: list[TranscriptCue], path: Path, engine: str, fallback_reason: str = "") -> None:
    chunks = [f"NOTE engine: {engine}"]
    if fallback_reason:
        chunks.append(f"NOTE fallback_reason: {fallback_reason}")
    for cue in cues:
        chunks.append(f"{cue.index}\n{_timecode(cue.start)} --> {_timecode(cue.end)}\n{cue.text}\n")
    path.write_text("\n".join(chunks), encoding="utf-8")


def _write_csv(cues: list[TranscriptCue], path: Path, engine: str, fallback_reason: str = "") -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["start", "end", "duration", "text", "source", "engine", "fallback_reason"])
        writer.writeheader()
        for cue in cues:
            writer.writerow(
                {
                    "start": f"{cue.start:.3f}",
                    "end": f"{cue.end:.3f}",
                    "duration": f"{cue.end - cue.start:.3f}",
                    "text": cue.text,
                    "source": cue.source,
                    "engine": engine,
                    "fallback_reason": fallback_reason,
                }
            )


def _read_srt(path: Path, source: Path) -> list[TranscriptCue]:
    text = path.read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    cues: list[TranscriptCue] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line = next((line for line in lines if "-->" in line), "")
        if not time_line:
            continue
        left, right = [part.strip() for part in time_line.split("-->", 1)]
        body = " ".join(line for line in lines if "-->" not in line and not line.isdigit())
        if not body:
            continue
        cues.append(TranscriptCue(len(cues) + 1, _parse_timecode(left), _parse_timecode(right.split()[0]), body, str(source)))
    return cues


def _timecode(seconds: float) -> str:
    whole = int(max(0.0, seconds))
    millis = int(round((seconds - whole) * 1000))
    hours = whole // 3600
    minutes = (whole % 3600) // 60
    secs = whole % 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _parse_timecode(text: str) -> float:
    parts = text.strip().replace(",", ".").split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def _safe_stem(stem: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", stem).strip() or "transcript"


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
