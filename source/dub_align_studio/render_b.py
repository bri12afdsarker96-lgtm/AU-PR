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

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from integrated_workbench.edit_compose import match_video_to_audio
from integrated_workbench.proc import run_silent

from .audio_mix import AudioMix, build_audio_filtergraph, build_audio_inputs
from .frames import quantize_to_frames
from .overlays import OverlayText, overlay_filters
from .progressbar import FillOverlay, ProgressBar, progressbar_layers
from .subtitles import (
    SubtitleStyle,
    drawtext_filters,
    entries_from_frame_windows,
    find_cjk_font,
    write_srt,
)
from .timing import LineTiming


DEFAULT_MODE = "裁剪多余画面"  # 画面比音频长→裁；短→放慢/克隆末帧补足（复用内核语义）


def _staged_font(font: Path, work_dir: Path) -> Path:
    """把字体复制到一条**纯 ASCII、不含 &** 的路径再交给 ffmpeg drawtext。

    根因（2026-07-25 用户实测字幕烧成 □□□）：Windows 版 ffmpeg 的 drawtext 用系统
    ANSI 代码页打开 fontfile，字体若在含 `&` / 非 ASCII 的目录（本机字体在
    `D:\\GitHub\\By\\AU&PR\\水星配音数据\\字体\\`）就加载失败 → 回退到无中文字形的默认
    字体 → 满屏方块。文本框叠层用的是 `C:\\Windows\\Fonts`（纯 ASCII）所以正常，唯独
    字幕字体在坏路径上——正是「字幕 □□□、文本框正常」的现象。
    复制到 ASCII 落点即可规避；复制失败或找不到 ASCII 落点则原样返回（不比现状更差）。
    """
    src = Path(font)
    try:
        if str(src.resolve()).isascii() and "&" not in str(src.resolve()):
            return src  # 本就安全（如 C:\\Windows\\Fonts）——不复制
    except Exception:
        return src
    name = "subfont" + src.suffix.lower()
    drive = os.path.splitdrive(str(Path(work_dir).resolve()))[0]  # 例 'D:'
    candidates = []
    if drive:
        candidates.append(Path(drive + os.sep) / ".mercury_cache" / "fonts")
    candidates.append(Path(tempfile.gettempdir()) / "mercury_fonts")
    for base in candidates:
        target = base / name
        if not str(target).isascii() or "&" in str(target):
            continue  # 落点本身不安全（用户名含中文的 %TEMP% 等）→ 换下一个
        try:
            base.mkdir(parents=True, exist_ok=True)
            if not target.exists() or target.stat().st_size != src.stat().st_size:
                shutil.copy2(src, target)
            return target
        except Exception:
            continue
    return src  # 无安全落点 → 原样（保持现状，不引入新失败）


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
    progress=None,
    progress_bar: ProgressBar | None = None,
) -> DubBResult:
    """整条 master 叠加 + 逐行画面收口渲染，返回带断言结果的 DubBResult。

    subtitle_style 给定时烧录字幕（字号等自定义；字体缺失自动降级为只出 SRT）。
    overlays 为用户自定义文本框（书名/旁白/引导语等），绘制在字幕之上。
    progress_bar 给定时烧录短剧风格视频进度条（随播放增长，片尾走满），绘制在最上层。
    audio_mix 给定 BGM/音效/总音量时混流（BGM 循环到片尾，音效定点，各自可调音量）。
    有台词的行始终导出 .srt（剪映可直接导入）。
    """
    config = config or RenderConfig()
    _require_binaries(config)
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
        if progress is not None:
            try:
                progress(position, len(lines))  # 逐段渲染进度回调
            except Exception:
                pass
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

    # 字幕：行窗口来自帧量化结果，与画面严格同轴；再按标点展开为逐句条目——
    # 每个短句在行窗口内按字数占比拿到自己的显示窗，按时间轴一句句出现（2026-07-25 定案）。
    from .subtitles import entries_to_phrases

    entries = entries_to_phrases(entries_from_frame_windows([t.text for t in lines], frames, config.fps))
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
                safe_font = _staged_font(font, work_dir)  # 规避 Windows drawtext 坏路径→□□□
                burn_filters = drawtext_filters(entries, subtitle_style, safe_font, work_dir, config.width)
                subtitle_note = f"已烧录字幕（字号 {subtitle_style.font_size_px}px，字体 {font.name}）"
            else:
                subtitle_note = "字体缺失，未烧字幕（SRT 已导出，可导入剪映）"
    elif subtitle_style is not None:
        subtitle_note = "没有台词文本，未烧字幕"

    # 用户文本框（书名/旁白/引导语）：绘制在字幕之上；字体缺失同样降级记提示。
    if overlays:
        font = find_cjk_font()
        if font:
            font = _staged_font(font, work_dir)  # 同样规避坏路径（文本框叠层字体）
            burn_filters += overlay_filters(overlays, font, work_dir, config.width, master_seconds)
            subtitle_note = (subtitle_note + "；" if subtitle_note else "") + f"已叠加 {len(overlays)} 个文本框"
        else:
            subtitle_note = (subtitle_note + "；" if subtitle_note else "") + "字体缺失，文本框未叠加"

    # 视频进度条（短剧风格）：随播放丝滑增长、片尾走满；绘制在字幕/文本框之上（最上层）。
    #   track（底色条带）与 text（文字）是线性滤镜，fill（已播图层）是 overlay，
    #   由 _overlay_master 拼进 filter_complex：subs/overlays/track → overlay(fill) → text。
    pb_below: list[str] = []
    pb_fill: FillOverlay | None = None
    pb_above: list[str] = []
    if progress_bar is not None:
        pb_font = find_cjk_font()
        if pb_font:
            pb_font = _staged_font(pb_font, work_dir)
        pb_below, pb_fill, pb_above = progressbar_layers(
            progress_bar, pb_font, work_dir, config.width, config.height, master_seconds)
        subtitle_note = (subtitle_note + "；" if subtitle_note else "") + "已加视频进度条"

    _overlay_master(config, silent_full, master_wav, output_path, burn_filters, audio_mix,
                    pb_below=pb_below, pb_fill=pb_fill, pb_above=pb_above)

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


def _build_video_graph(below: list[str], fill: "FillOverlay | None", above: list[str]) -> str:
    """视频侧 filter_complex：subs/overlays/track → overlay(fill) → text → [vout]。

    fill 为 None 时退化为一条线性链；有 fill 时中间插入 overlay（进度条丝滑填充）。"""
    if fill is None:
        return "[0:v]" + (",".join(below) if below else "null") + "[vout]"
    decls = [
        fill.source("[pbfill]"),
        "[0:v]" + (",".join(below) if below else "null") + "[pbbase]",
        fill.overlay_step("[pbbase]", "[pbfill]", "[pbfilled]"),
        "[pbfilled]" + (",".join(above) if above else "null") + "[vout]",
    ]
    return ";".join(decls)


def _overlay_master(
    config: RenderConfig,
    silent_video: Path,
    master_wav: Path,
    output: Path,
    burn_filters: list[str] | None = None,
    audio_mix: AudioMix | None = None,
    pb_below: list[str] | None = None,
    pb_fill: "FillOverlay | None" = None,
    pb_above: list[str] | None = None,
) -> None:
    """叠加整轨配音；给定字幕/文本框/进度条滤镜时同步烧录（此步才重编码，否则视频流直拷）。

    audio_mix 非空且非平凡时走 filter_complex 混流（master+BGM循环+音效，各自音量）。
    进度条 fill 是 overlay（需第二个色块源），一旦存在就走 filter_complex；其余
    字幕/文本框（burn_filters）为线性滤镜，铺在进度条 track 之下、fill 之下。"""
    burn_filters = burn_filters or []
    pb_below = pb_below or []
    pb_above = pb_above or []
    command = [config.ffmpeg, "-y", "-i", str(silent_video), "-i", str(master_wav)]
    mix = audio_mix if (audio_mix is not None and not audio_mix.is_trivial()) else None
    has_fill = pb_fill is not None
    below = list(burn_filters) + list(pb_below)   # 铺在 fill 之下：字幕→文本框→进度条底色带
    venc = [
        "-c:v", "libx264", "-preset", config.preset, "-crf", str(config.crf),
        "-pix_fmt", "yuv420p", "-fps_mode", "cfr", "-r", str(config.fps),
    ]
    burn_note = "+烧录字幕" if burn_filters else ""
    pb_note = "+进度条" if has_fill else ""

    if mix is None:
        if not has_fill:
            # 原快路径：单轨 master 直接映射（有字幕走 -filter:v，无则视频直拷）
            command += (["-filter:v", ",".join(burn_filters), *venc] if burn_filters
                        else ["-c:v", "copy"])
            command += ["-map", "0:v:0", "-map", "1:a:0",
                        "-c:a", "aac", "-b:a", config.audio_bitrate,
                        "-movflags", "+faststart", str(output)]
            _run(command, "叠加整轨配音" + burn_note)
            return
        # 有进度条填充：视频走 filter_complex，音频仍直接映射 master
        command += ["-filter_complex", _build_video_graph(below, pb_fill, pb_above),
                    "-map", "[vout]", *venc,
                    "-map", "1:a:0", "-c:a", "aac", "-b:a", config.audio_bitrate,
                    "-movflags", "+faststart", str(output)]
        _run(command, "叠加整轨配音" + burn_note + pb_note)
        return

    # 混流路径：master 之后追加 BGM/音效输入，音频走 filter_complex
    for extra in build_audio_inputs(mix):
        command += extra
    audio_graph, aout = build_audio_filtergraph(mix, master_input=1)
    if below or has_fill:
        video_graph = _build_video_graph(below, pb_fill, pb_above)
        command += ["-filter_complex", audio_graph + ";" + video_graph,
                    "-map", "[vout]", *venc]
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
    _run(command, "混流(" + "+".join(bits) + ")" + burn_note + pb_note)


# ------------------------------------------------------------------ ffmpeg 预检
_FFMPEG_HINT = (
    "未找到 {miss}：渲染成片需要 ffmpeg（配音克隆不需要，所以前面几步能过）。\n"
    "请把 ffmpeg.exe、ffprobe.exe 放到软件目录（「启动.bat」旁边）或加入系统 PATH，再重试。\n"
    "下载：https://www.gyan.dev/ffmpeg/builds/ 里的 ffmpeg-release-essentials.zip，"
    "解压后 bin 目录内的 ffmpeg.exe / ffprobe.exe 两个文件拷过去即可。"
)


def _require_binaries(config: "RenderConfig") -> None:
    """渲染前预检 ffmpeg/ffprobe；缺失时给看得懂的中文指引，而非裸 WinError 2。"""
    missing = [name for name, exe in (("ffmpeg", config.ffmpeg), ("ffprobe", config.ffprobe))
               if shutil.which(exe) is None]
    if missing:
        raise RuntimeError(_FFMPEG_HINT.format(miss=" 与 ".join(missing)))


def _guard_missing(exc: FileNotFoundError, command: list[str]) -> RuntimeError:
    """把子进程「找不到可执行文件」(WinError 2) 翻译成 ffmpeg 缺失指引。"""
    exe = command[0] if command else "ffmpeg"
    return RuntimeError(_FFMPEG_HINT.format(miss=Path(exe).name))


# ------------------------------------------------------------------ 子进程
def _run(command: list[str], label: str) -> None:
    try:
        completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise _guard_missing(exc, command) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise RuntimeError(f"{label}失败：{detail or '未知错误'}")


def _run_out(command: list[str], label: str, allow_empty: bool = False) -> str:
    try:
        completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise _guard_missing(exc, command) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise RuntimeError(f"{label}失败：{detail or '未知错误'}")
    out = completed.stdout or ""
    if not out.strip() and not allow_empty:
        raise RuntimeError(f"{label}无输出。")
    return out
