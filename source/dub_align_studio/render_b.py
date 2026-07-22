"""B 方案渲染路径：逐行裁/变速拼画面 + 叠整轨 master 音频 + 帧收口断言。

复用 integrated_workbench 对齐内核：
    - edit_compose.match_video_to_audio：按「画面对齐到音频时长」决定裁剪/变速（五档策略）。
    - proc.run_silent：静默调 ffmpeg/ffprobe（Windows 不弹黑框）。

流程：
    ① 逐行时长 → frames.quantize_to_frames 量化到整数帧（末段收口）。
    ② 每行渲染一段【无声】视频，长度精确为该行帧数（裁剪多余 / 变速或克隆末帧补足）。
    ③ concat 拼成整条无声视频（总帧数 == Σ帧数 == round(master×fps)）。
    ④ 把整条 master.wav 作为唯一音轨叠加（-map 0:v -map 1:a）。
    ⑤ 断言：成片总帧数 == 预期；成片含音频流；|画面总长 − master 总长| ≤ 1 帧。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from integrated_workbench.edit_compose import match_video_to_audio
from integrated_workbench.proc import run_silent

from .audio_mix import AudioMix, build_audio_filtergraph, build_audio_inputs
from .frames import quantize_to_frames
from .overlays import OverlayText, overlay_filters
from .subtitles import (
    SubtitleStyle,
    drawtext_filters,
    entries_from_frame_windows,
    find_cjk_font,
    write_srt,
)
from .timing import LineTiming


DEFAULT_MODE = "裁剪多余画面"  # 画面比音频长→裁；短→放慢/克隆末帧补足（复用内核语义）


@dataclass
class RenderConfig:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    preset: str = "veryfast"
    crf: int = 20
    audio_bitrate: str = "192k"
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    mode: str = DEFAULT_MODE


@dataclass
class ShotPlan:
    index: int
    text: str
    video: Path
    src_seconds: float
    target_seconds: float   # = frames / fps（帧量化后的目标时长）
    frames: int
    strategy: str           # 复用 SyncPlan.note，人类可读
    segment_file: Path | None = None


@dataclass
class DubBResult:
    output_path: Path
    master_seconds: float
    video_seconds: float
    total_frames: int
    expected_frames: int
    has_audio: bool
    frame_locked: bool      # 画面总长是否收口到 master（≤1 帧）
    shots: list[ShotPlan] = field(default_factory=list)
    srt_path: Path | None = None       # 有台词就导出（剪映可直接导入）
    subtitles_burned: bool = False
    subtitle_note: str = ""

    @property
    def ok(self) -> bool:
        return (
            self.total_frames == self.expected_frames
            and self.has_audio
            and self.frame_locked
        )


# ------------------------------------------------------------------ ffprobe 探测
def probe_seconds(config: RenderConfig, path: Path) -> float:
    out = _run_out(
        [config.ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        "读取时长",
    )
    try:
        return float(out.strip())
    except ValueError as exc:
        raise RuntimeError(f"无法解析时长：{path}（{out!r}）") from exc


def count_video_frames(config: RenderConfig, path: Path) -> int:
    out = _run_out(
        [config.ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        "读取帧数",
    )
    try:
        return int(out.strip())
    except ValueError as exc:
        raise RuntimeError(f"无法解析帧数：{path}（{out!r}）") from exc


def has_audio_stream(config: RenderConfig, path: Path) -> bool:
    out = _run_out(
        [config.ffprobe, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
        "探测音频流", allow_empty=True,
    )
    return bool(out.strip())


# ------------------------------------------------------------------ 单段画面滤镜（纯逻辑，可单测）
def shot_video_filter(
    src_seconds: float,
    target_seconds: float,
    width: int,
    height: int,
    fps: int,
    mode: str = DEFAULT_MODE,
) -> tuple[list[str], str]:
    """按「画面对齐到该行音频时长」构建单段视频滤镜链，返回 (滤镜列表, 策略说明)。

    始终额外克隆末帧补足，保证帧数足够（多余帧由 -frames:v 截断）；
    再统一 scale+pad 到画布、setsar=1、fps 定帧。纯字符串构造，便于单测。
    """
    plan = match_video_to_audio(src_seconds, target_seconds, mode)
    parts: list[str] = []
    if abs(plan.video_speed - 1.0) > 1e-3:
        # setpts=PTS/speed：speed<1 放慢、>1 加速（与内核 SyncPlan.video_filter 一致）。
        parts.append(f"setpts=(PTS-STARTPTS)/{plan.video_speed:.6f}")
    else:
        parts.append("setpts=PTS-STARTPTS")
    # 克隆末帧把流补到至少 target 长度（短素材撑满 / 末段收口），多余帧后续截断。
    parts.append(f"tpad=stop_mode=clone:stop_duration={max(0.05, target_seconds):.3f}")
    parts.append(f"scale={width}:{height}:force_original_aspect_ratio=decrease")
    parts.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
    parts.append("setsar=1")
    parts.append(f"fps={fps}")
    return parts, plan.note


# ------------------------------------------------------------------ 渲染
def render_b(
    master_wav: Path,
    lines: list[LineTiming],
    videos: list[Path],
    output_path: Path,
    config: RenderConfig | None = None,
    subtitle_style: SubtitleStyle | None = None,
    overlays: list[OverlayText] | None = None,
    audio_mix: AudioMix | None = None,
) -> DubBResult:
    """整条 master 叠加 + 逐行画面收口渲染，返回带断言结果的 DubBResult。

    subtitle_style 给定时烧录字幕（字号等自定义；字体缺失自动降级为只出 SRT）。
    overlays 为用户自定义文本框（书名/旁白/引导语等），绘制在字幕之上。
    audio_mix 给定 BGM/音效/总音量时混流（BGM 循环到片尾，音效定点，各自可调音量）。
    有台词的行始终导出 .srt（剪映可直接导入）。
    """
    config = config or RenderConfig()
    master_wav = Path(master_wav)
    output_path = Path(output_path)
    if len(lines) != len(videos):
        raise ValueError(f"计时行数({len(lines)})与画面数({len(videos)})不一致。")
    if not lines:
        raise ValueError("没有可渲染的行。")

    master_seconds = probe_seconds(config, master_wav)
    frames = quantize_to_frames([t.duration for t in lines], config.fps, master_seconds)
    expected_frames = sum(frames)

    work_dir = output_path.parent / f"{output_path.stem}_segments"
    work_dir.mkdir(parents=True, exist_ok=True)

    shots: list[ShotPlan] = []
    segment_files: list[Path] = []
    for position, (timing, video, frame_count) in enumerate(zip(lines, videos, frames), start=1):
        video = Path(video)
        src_seconds = probe_seconds(config, video)
        target_seconds = frame_count / config.fps
        vf, note = shot_video_filter(
            src_seconds, target_seconds, config.width, config.height, config.fps, config.mode
        )
        segment = work_dir / f"{position:03d}.mp4"
        _render_silent_segment(config, video, ",".join(vf), frame_count, segment)
        shot = ShotPlan(
            index=timing.index,
            text=timing.text,
            video=video,
            src_seconds=round(src_seconds, 3),
            target_seconds=round(target_seconds, 3),
            frames=frame_count,
            strategy=note,
            segment_file=segment,
        )
        shots.append(shot)
        segment_files.append(segment)

    silent_full = work_dir / "_full_silent.mp4"
    _concat_copy(config, segment_files, silent_full)

    # 字幕：行窗口来自帧量化结果，与画面严格同轴。有台词就导出 SRT；样式给定且有字体则烧录。
    entries = entries_from_frame_windows([t.text for t in lines], frames, config.fps)
    has_text = any(entry.text for entry in entries)
    srt_path: Path | None = None
    burn_filters: list[str] = []
    subtitle_note = ""
    if has_text:
        srt_path = write_srt(output_path.with_suffix(".srt"), entries)
        if subtitle_style is not None:
            from .subtitles import pick_font

            font = pick_font(subtitle_style.font_name)
            if font:
                burn_filters = drawtext_filters(entries, subtitle_style, font, work_dir, config.width)
                subtitle_note = f"已烧录字幕（字号 {subtitle_style.font_size_px}px，字体 {font.name}）"
            else:
                subtitle_note = "字体缺失，未烧字幕（SRT 已导出，可导入剪映）"
    elif subtitle_style is not None:
        subtitle_note = "没有台词文本，未烧字幕"

    # 用户文本框（书名/旁白/引导语）：绘制在字幕之上；字体缺失同样降级记提示。
    if overlays:
        font = find_cjk_font()
        if font:
            burn_filters += overlay_filters(overlays, font, work_dir, config.width, master_seconds)
            subtitle_note = (subtitle_note + "；" if subtitle_note else "") + f"已叠加 {len(overlays)} 个文本框"
        else:
            subtitle_note = (subtitle_note + "；" if subtitle_note else "") + "字体缺失，文本框未叠加"

    _overlay_master(config, silent_full, master_wav, output_path, burn_filters, audio_mix)

    total_frames = count_video_frames(config, output_path)
    video_seconds = total_frames / config.fps
    frame_locked = abs(video_seconds - master_seconds) <= (1.0 / config.fps) + 1e-6

    return DubBResult(
        output_path=output_path,
        master_seconds=round(master_seconds, 3),
        video_seconds=round(video_seconds, 3),
        total_frames=total_frames,
        expected_frames=expected_frames,
        has_audio=has_audio_stream(config, output_path),
        frame_locked=frame_locked,
        shots=shots,
        srt_path=srt_path,
        subtitles_burned=bool(burn_filters),
        subtitle_note=subtitle_note,
    )


def _render_silent_segment(
    config: RenderConfig, source: Path, vf: str, frame_count: int, output: Path
) -> None:
    command = [
        config.ffmpeg, "-y", "-i", str(source),
        "-an", "-vf", vf,
        "-frames:v", str(frame_count),
        "-c:v", "libx264", "-preset", config.preset, "-crf", str(config.crf),
        "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", str(config.fps),
        "-video_track_timescale", str(config.fps * 512),
        str(output),
    ]
    _run(command, f"渲染画面段 {output.name}")


def _concat_copy(config: RenderConfig, segments: list[Path], output: Path) -> None:
    list_file = output.parent / "_concat.txt"
    list_file.write_text(
        "".join(f"file '{seg.resolve().as_posix()}'\n" for seg in segments),
        encoding="utf-8",
    )
    command = [
        config.ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c", "copy", str(output),
    ]
    _run(command, "拼接无声画面")


def _overlay_master(
    config: RenderConfig,
    silent_video: Path,
    master_wav: Path,
    output: Path,
    burn_filters: list[str] | None = None,
    audio_mix: AudioMix | None = None,
) -> None:
    """叠加整轨配音；给定字幕滤镜时同步烧录（此步才重编码，否则视频流直拷）。

    audio_mix 非空且非平凡时走 filter_complex 混流（master+BGM循环+音效，各自音量）；
    视频侧的字幕/文本框烧录与之并存（视频链也进 filter_complex 以免与音频链冲突）。"""
    command = [config.ffmpeg, "-y", "-i", str(silent_video), "-i", str(master_wav)]
    mix = audio_mix if (audio_mix is not None and not audio_mix.is_trivial()) else None

    if mix is None:
        # 原路径：单轨 master 直接映射
        if burn_filters:
            command += [
                "-filter:v", ",".join(burn_filters),
                "-c:v", "libx264", "-preset", config.preset, "-crf", str(config.crf),
                "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", str(config.fps),
            ]
        else:
            command += ["-c:v", "copy"]
        command += [
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:a", "aac", "-b:a", config.audio_bitrate,
            "-movflags", "+faststart", str(output),
        ]
        _run(command, "叠加整轨配音" + ("+烧录字幕" if burn_filters else ""))
        return

    # 混流路径：master 之后追加 BGM/音效输入，音频走 filter_complex
    for extra in build_audio_inputs(mix):
        command += extra
    audio_graph, aout = build_audio_filtergraph(mix, master_input=1)
    if burn_filters:
        video_graph = "[0:v]" + ",".join(burn_filters) + "[vout]"
        command += ["-filter_complex", audio_graph + ";" + video_graph,
                    "-map", "[vout]",
                    "-c:v", "libx264", "-preset", config.preset, "-crf", str(config.crf),
                    "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", str(config.fps)]
    else:
        command += ["-filter_complex", audio_graph, "-map", "0:v:0", "-c:v", "copy"]
    command += [
        "-map", aout,
        "-c:a", "aac", "-b:a", config.audio_bitrate,
        "-movflags", "+faststart", str(output),
    ]
    bits = ["配音"]
    if mix.bgm is not None:
        bits.append("BGM")
    if mix.sfx:
        bits.append(f"{len(mix.sfx)}个音效")
    _run(command, "混流(" + "+".join(bits) + ")" + ("+烧录字幕" if burn_filters else ""))


# ------------------------------------------------------------------ 子进程
def _run(command: list[str], label: str) -> None:
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise RuntimeError(f"{label}失败：{detail or '未知错误'}")


def _run_out(command: list[str], label: str, allow_empty: bool = False) -> str:
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise RuntimeError(f"{label}失败：{detail or '未知错误'}")
    out = completed.stdout or ""
    if not out.strip() and not allow_empty:
        raise RuntimeError(f"{label}无输出。")
    return out
