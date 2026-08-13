"""视频处理管线：原视频 + 镜像放大 130% 拼接 → 按 min(视频, TTS) 裁剪。

严格规则（用户 2026-08-13）：
    - **不是**左右并排，**不是**上下并排——时间轴首尾拼接：原视频 → 镜像并放大 130% 副本。
    - 不加转场、不拉伸速度、不循环视频、不加字幕、不加 BGM。
    - 镜像分支：hflip → 中心放大 130% → 裁回原宽高（保持原分辨率/帧率）。
    - 最终时长严格 = min(拼接视频实际时长, TTS 实际时长)——不循环、不补静音、不二次变速。
    - 默认不含原声；勾选"保留原视频声音"时才混入原声。
    - 每条任务独立 staging 目录；成功并校验后才原子搬到正式输出路径。
    - 失败清理本条临时文件，**不删已有成片**。

尽量**一次 ffmpeg 滤镜链**完成所有画面步骤，避免生成巨大的中间文件。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from integrated_workbench.proc import popen_silent, run_silent

from .. import settings as studio_settings
from .hw_encoder import EncoderProbe


class VideoError(RuntimeError):
    pass


class VideoCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoProbe:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool


def ffprobe_video(video_path: str | Path) -> VideoProbe:
    ffprobe = studio_settings.ffmpeg_tool("ffprobe")
    if not ffprobe:
        raise VideoError("未找到 ffprobe。请安装 ffmpeg 或使用「一键修复ffmpeg.bat」。")
    cmd = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(video_path),
    ]
    try:
        completed = run_silent(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=30)
    except FileNotFoundError as exc:
        raise VideoError(f"未找到 ffprobe：{exc}") from exc
    if completed.returncode != 0:
        raise VideoError(
            f"ffprobe 读取失败：{(completed.stderr or '').strip()[-200:] or '未知错误'}"
        )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VideoError(f"ffprobe 输出不是有效 JSON：{exc}") from exc
    video_stream = None
    has_audio = False
    for s in payload.get("streams") or []:
        if s.get("codec_type") == "video" and video_stream is None:
            video_stream = s
        if s.get("codec_type") == "audio":
            has_audio = True
    if video_stream is None:
        raise VideoError("视频文件无视频流")
    duration = _extract_duration(payload, video_stream)
    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    fps = _extract_fps(video_stream)
    if not width or not height:
        raise VideoError("视频宽高无效")
    if duration <= 0:
        raise VideoError("视频时长无效")
    return VideoProbe(duration=duration, width=width, height=height,
                       fps=fps, has_audio=has_audio)


def _extract_duration(payload: dict, video_stream: dict) -> float:
    for candidate in (video_stream.get("duration"), (payload.get("format") or {}).get("duration")):
        if candidate:
            try:
                return float(candidate)
            except (TypeError, ValueError):
                pass
    return 0.0


def _extract_fps(video_stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        raw = video_stream.get(key) or ""
        if "/" in raw:
            try:
                num, den = raw.split("/")
                num_f = float(num); den_f = float(den)
                if den_f > 0:
                    return num_f / den_f
            except ValueError:
                continue
        elif raw:
            try:
                return float(raw)
            except ValueError:
                continue
    return 30.0


def ffprobe_seconds(media_path: str | Path) -> float:
    ffprobe = studio_settings.ffmpeg_tool("ffprobe")
    if not ffprobe:
        raise VideoError("未找到 ffprobe")
    cmd = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", str(media_path),
    ]
    completed = run_silent(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=15)
    if completed.returncode != 0:
        raise VideoError(f"ffprobe 失败：{(completed.stderr or '').strip()[-200:]}")
    payload = json.loads(completed.stdout or "{}")
    raw = (payload.get("format") or {}).get("duration") or 0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def has_video_and_audio_streams(media_path: str | Path) -> tuple[bool, bool]:
    ffprobe = studio_settings.ffmpeg_tool("ffprobe")
    if not ffprobe:
        raise VideoError("未找到 ffprobe")
    cmd = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_streams", str(media_path),
    ]
    completed = run_silent(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=15)
    if completed.returncode != 0:
        raise VideoError(f"ffprobe 失败：{(completed.stderr or '').strip()[-200:]}")
    payload = json.loads(completed.stdout or "{}")
    has_v = has_a = False
    for s in payload.get("streams") or []:
        if s.get("codec_type") == "video":
            has_v = True
        elif s.get("codec_type") == "audio":
            has_a = True
    return has_v, has_a


@dataclass(frozen=True)
class RenderResult:
    output_path: str
    final_duration: float
    concat_duration: float
    tts_duration: float
    warnings: list[str]


def build_filter_chain(*, width: int, height: int, zoom_percent: int = 130,
                       keep_original_audio: bool = False,
                       final_seconds: float) -> tuple[list[str], list[str]]:
    """构造一次 ffmpeg 命令用的滤镜（video）+ 映射（audio）。"""
    zoom_ratio = zoom_percent / 100.0
    scaled_w = _even(width * zoom_ratio)
    scaled_h = _even(height * zoom_ratio)
    filter_lines = [
        "[0:v]split=2[v0][v0b]",
        f"[v0b]hflip,scale={scaled_w}:{scaled_h},"
        f"crop={width}:{height}:(in_w-{width})/2:(in_h-{height})/2[v1]",
        "[v0][v1]concat=n=2:v=1:a=0[vout]",
    ]
    map_args: list[str] = ["-map", "[vout]"]
    if keep_original_audio:
        filter_lines.append("[0:a]asplit=2[a0][a0b]")
        filter_lines.append("[a0][a0b]concat=n=2:v=0:a=1[a_orig]")
        filter_lines.append(
            "[a_orig][1:a]amix=inputs=2:duration=shortest:normalize=0[a_mixed]"
        )
        filter_lines.append("[a_mixed]alimiter=limit=0.95[aout]")
        map_args += ["-map", "[aout]"]
    else:
        map_args += ["-map", "1:a:0"]
    map_args += ["-t", f"{final_seconds:.3f}"]
    return filter_lines, map_args


def _even(value: float) -> int:
    v = int(round(value))
    return v if v % 2 == 0 else v + 1


def render_single(*, input_video: str | Path, tts_audio: str | Path,
                   output_path: str | Path, staging_dir: str | Path,
                   encoder: EncoderProbe, keep_original_audio: bool = False,
                   zoom_percent: int = 130, preset: str = "medium", crf: int = 20,
                   video_probe: VideoProbe | None = None,
                   tts_seconds: float | None = None,
                   cancel_flag=None, timeout: float = 1800.0) -> RenderResult:
    input_video = Path(input_video)
    tts_audio = Path(tts_audio)
    output_path = Path(output_path)
    staging_dir = Path(staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    if not input_video.is_file():
        raise VideoError(f"输入视频不存在：{input_video}")
    if not tts_audio.is_file():
        raise VideoError(f"TTS 旁白不存在：{tts_audio}")

    probe = video_probe or ffprobe_video(input_video)
    tts_seconds = tts_seconds if tts_seconds is not None else ffprobe_seconds(tts_audio)
    if tts_seconds <= 0:
        raise VideoError("TTS 旁白时长无效")

    concat_seconds = probe.duration * 2
    final_seconds = min(concat_seconds, tts_seconds)

    actual_keep_audio = keep_original_audio
    if keep_original_audio and not probe.has_audio:
        actual_keep_audio = False
        warnings.append("该视频没有原声音轨，本条仅使用旁白。")

    ffmpeg = studio_settings.ffmpeg_tool("ffmpeg")
    if not ffmpeg:
        raise VideoError("未找到 ffmpeg。请安装 ffmpeg 或使用「一键修复ffmpeg.bat」。")

    filter_lines, map_args = build_filter_chain(
        width=probe.width, height=probe.height,
        zoom_percent=zoom_percent, keep_original_audio=actual_keep_audio,
        final_seconds=final_seconds,
    )
    filter_complex = ";".join(filter_lines)

    tmp_out = staging_dir / f"{uuid.uuid4().hex[:8]}.mp4"
    encoder_args = list(encoder.args)
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(input_video),
        "-i", str(tts_audio),
        "-filter_complex", filter_complex,
        *map_args,
        "-c:v", encoder.encoder, *encoder_args,
    ]
    if encoder.encoder == "libx264":
        cmd += ["-preset", preset, "-crf", str(int(crf))]
    cmd += [
        "-c:a", "aac", "-b:a", "192k",
        "-r", f"{probe.fps:.3f}",
        "-movflags", "+faststart",
        str(tmp_out),
    ]

    _run_ffmpeg_cancellable(cmd, cancel_flag=cancel_flag, timeout=timeout)

    if not tmp_out.is_file() or tmp_out.stat().st_size < 1024:
        _safe_unlink(tmp_out)
        raise VideoError("ffmpeg 输出为空或损坏")
    has_v, has_a = has_video_and_audio_streams(tmp_out)
    if not has_v:
        _safe_unlink(tmp_out)
        raise VideoError("成片校验失败：缺视频流")
    if not has_a:
        _safe_unlink(tmp_out)
        raise VideoError("成片校验失败：缺音频流")
    actual_seconds = ffprobe_seconds(tmp_out)
    if abs(actual_seconds - final_seconds) > 0.4:
        warnings.append(
            f"成片实际时长 {actual_seconds:.2f}s 与目标 {final_seconds:.2f}s 有偏差 (≤0.4s 正常)"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final_output = _next_unique_path(output_path)
    tmp_out.replace(final_output)
    return RenderResult(
        output_path=str(final_output),
        final_duration=actual_seconds,
        concat_duration=concat_seconds,
        tts_duration=tts_seconds,
        warnings=warnings,
    )


def _run_ffmpeg_cancellable(cmd: list[str], *, cancel_flag, timeout: float) -> None:
    stderr_tail: list[str] = []
    proc = popen_silent(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace")
    started = time.time()
    try:
        while True:
            if cancel_flag is not None and cancel_flag.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise VideoCancelled("任务被取消")
            if (time.time() - started) > timeout:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise VideoError(f"ffmpeg 超时 ({int(timeout)}s)")
            code = proc.poll()
            if code is not None:
                break
            time.sleep(0.2)
        try:
            for line in proc.stderr or []:
                stderr_tail.append(line)
                if len(stderr_tail) > 40:
                    stderr_tail.pop(0)
        except Exception:  # noqa: BLE001
            pass
        if proc.returncode != 0:
            tail = "".join(stderr_tail).strip()[-500:]
            raise VideoError(f"ffmpeg 失败 (code={proc.returncode}): {tail or '无 stderr'}")
    finally:
        if proc.stderr:
            try:
                proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass


def _next_unique_path(target: Path) -> Path:
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    for i in range(2, 10000):
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise VideoError("重名太多，放弃编号（超过 10000 次）")


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def cleanup_staging(staging_dir: str | Path) -> None:
    """清理某条任务的 staging 目录。空/None 直接返回；正式成片不在里面。

    安全护栏：拒绝清理 . / .. / 存在的 CWD 或 数据总目录本身，避免误触。
    """
    if not staging_dir:
        return
    path = Path(str(staging_dir)).resolve()
    if not path.is_dir():
        return
    # 白名单：只清"批量带货/staging"下的子目录
    if "批量带货" not in path.parts or "staging" not in path.parts:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def build_output_filename(input_video: str | Path, voice_short_name: str) -> str:
    stem = Path(input_video).stem
    return f"{stem}_{voice_short_name}_带货.mp4"


def voice_short_name(voice_name: str) -> str:
    if not voice_name:
        return "配音"
    for sep in ("（", "("):
        if sep in voice_name:
            return voice_name.split(sep, 1)[0].strip() or "配音"
    return voice_name.strip() or "配音"
