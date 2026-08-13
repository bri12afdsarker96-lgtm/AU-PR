"""视频处理管线：原视频 + 镜像放大 130% 拼接 → 按 min(视频, TTS) 裁剪。

严格规则（用户 2026-08-13）：
    - **不是**左右并排，**不是**上下并排——时间轴首尾拼接：原视频 → 镜像并放大 130% 副本。
    - 不加转场、不拉伸速度、不循环视频、不加字幕、不加 BGM。
    - 镜像分支：hflip → 中心放大 130% → 裁回原宽高（保持源分辨率/帧率）。
    - 最终时长严格 = min(拼接视频实际时长, TTS 实际时长)——不循环、不补静音、不二次变速。
    - 时长偏差>0.4s 判定失败（R6：不再只 warning）。
    - 硬件编码运行失败 → 自动回退 libx264 重跑一次（R6）。
    - 默认不含原声；勾选"保留原视频声音"时才混入原声（amix+alimiter 防削波）。
    - 每条任务独立 staging 目录；成功并校验后才原子搬到正式输出路径。
    - 失败/取消/超时清理本条临时文件，**不删已有成片**。
    - cleanup_staging 硬护栏：只允许清理 data_root/批量带货/staging/&lt;batch_id&gt;/&lt;task_id&gt;。

pix_fmt：显式 yuv420p，保证 H.264 通用兼容；scale 后强制偶数尺寸。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from integrated_workbench.proc import popen_silent, run_silent

from .. import settings as studio_settings
from .hw_encoder import EncoderProbe
from .store import is_safe_id


# 时长偏差容忍（P0-5 R6）：超过即视为失败，不再 warning 后仍提交。
DURATION_TOLERANCE_SECONDS = 0.4


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


@dataclass
class RenderResult:
    output_path: str
    final_duration: float
    concat_duration: float
    tts_duration: float
    warnings: list = field(default_factory=list)
    encoder_used: str = ""
    hw_fallback_used: bool = False


def build_filter_chain(*, width: int, height: int, zoom_percent: int = 130,
                       keep_original_audio: bool = False,
                       final_seconds: float) -> tuple[list[str], list[str]]:
    """构造一次 ffmpeg 命令用的滤镜（video）+ 映射（audio）。

    scale 后强制偶数尺寸，crop 也用偶数（yuv420p 要求宽高偶数）。
    输出宽高沿用原视频（已在调用方保证偶数）。
    """
    zoom_ratio = zoom_percent / 100.0
    scaled_w = _even(width * zoom_ratio)
    scaled_h = _even(height * zoom_ratio)
    out_w = _even(width)
    out_h = _even(height)
    filter_lines = [
        "[0:v]split=2[v0][v0b]",
        f"[v0b]hflip,scale={scaled_w}:{scaled_h},"
        f"crop={out_w}:{out_h}:(in_w-{out_w})/2:(in_h-{out_h})/2[v1]",
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


def _run_ffmpeg_cancellable(cmd: list[str], *, cancel_flag, timeout: float,
                             capture_stderr: bool = True) -> tuple[int, str]:
    """启动 ffmpeg 进程，轮询 cancel_flag；触发时 terminate 并抛 VideoCancelled。

    返回 (returncode, stderr_tail_text)。不做 raise（HW 回退时上层要看 returncode）。
    """
    stderr_tail: list[str] = []
    proc = popen_silent(cmd, stdout=subprocess.DEVNULL,
                        stderr=(subprocess.PIPE if capture_stderr else subprocess.DEVNULL),
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
            time.sleep(0.15)
        if capture_stderr:
            try:
                for line in proc.stderr or []:
                    stderr_tail.append(line)
                    if len(stderr_tail) > 60:
                        stderr_tail.pop(0)
            except Exception:  # noqa: BLE001
                pass
    finally:
        if proc.stderr:
            try:
                proc.stderr.close()
            except Exception:  # noqa: BLE001
                pass
    return proc.returncode, "".join(stderr_tail).strip()[-500:]


def render_single(*, input_video: str | Path, tts_audio: str | Path,
                   reserved_output: str | Path, staging_dir: str | Path,
                   encoder: EncoderProbe, keep_original_audio: bool = False,
                   zoom_percent: int = 130, preset: str = "medium", crf: int = 20,
                   video_probe: VideoProbe | None = None,
                   tts_seconds: float | None = None,
                   cancel_flag=None, timeout: float = 1800.0,
                   allow_hw_fallback: bool = True) -> RenderResult:
    """一次 ffmpeg 完成整个滤镜链，写到 staging，再原子搬到 reserved_output。

    - reserved_output：调用方通过 TaskStore.reserve_output_path() 预留的最终路径。
    - allow_hw_fallback：硬件编码运行失败 → 自动重跑 libx264（R6）。
    - 时长偏差超 DURATION_TOLERANCE_SECONDS → 抛 VideoError（不再 warning 后仍提交）。
    - 若最终路径已存在（并发或第三方写入），拒绝覆盖，抛 VideoError。
    """
    input_video = Path(input_video)
    tts_audio = Path(tts_audio)
    reserved_output = Path(reserved_output)
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

    # 先按传入编码器跑；失败 + allow_hw_fallback + 不是 libx264 → 用 libx264 再跑一次
    used_encoder = encoder
    hw_fallback = False
    for attempt_pass in ("primary", "fallback"):
        tmp_out = staging_dir / f"{uuid.uuid4().hex[:8]}.mp4"
        encoder_args = list(used_encoder.args)
        cmd = [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(input_video),
            "-i", str(tts_audio),
            "-filter_complex", filter_complex,
            *map_args,
            "-c:v", used_encoder.encoder, *encoder_args,
            "-pix_fmt", "yuv420p",   # R6：明确兼容像素格式
        ]
        if used_encoder.encoder == "libx264":
            cmd += ["-preset", preset, "-crf", str(int(crf))]
        cmd += [
            "-c:a", "aac", "-b:a", "192k",
            "-r", f"{probe.fps:.3f}",
            "-movflags", "+faststart",
            str(tmp_out),
        ]
        try:
            code, err_tail = _run_ffmpeg_cancellable(
                cmd, cancel_flag=cancel_flag, timeout=timeout,
            )
        except (VideoCancelled, VideoError):
            _safe_unlink(tmp_out)
            raise

        if code == 0 and tmp_out.is_file() and tmp_out.stat().st_size >= 1024:
            break

        # 编码失败：清理，看是否可回退
        _safe_unlink(tmp_out)
        can_fallback = (
            attempt_pass == "primary"
            and allow_hw_fallback
            and used_encoder.encoder != "libx264"
        )
        if not can_fallback:
            raise VideoError(
                f"ffmpeg 失败 (code={code}, encoder={used_encoder.encoder}): "
                f"{err_tail or '无 stderr'}"
            )
        warnings.append(
            f"硬件编码器 {used_encoder.encoder} 运行失败，自动回退 libx264。"
            f"（stderr 尾部：{err_tail[-160:]}）"
        )
        used_encoder = EncoderProbe("cpu", "libx264", ["-preset", preset], True,
                                     "运行时回退")
        hw_fallback = True
    else:
        # 循环走完仍失败（正常不会到，因 primary 失败会 raise 或回退）
        raise VideoError("ffmpeg 全部尝试失败")

    # 校验产物
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
    # R6：偏差超限视为失败（不 replace）
    if abs(actual_seconds - final_seconds) > DURATION_TOLERANCE_SECONDS:
        _safe_unlink(tmp_out)
        raise VideoError(
            f"成片时长 {actual_seconds:.2f}s 偏离目标 {final_seconds:.2f}s "
            f"（超出 {DURATION_TOLERANCE_SECONDS}s 容忍）"
        )

    # R4：拒绝覆盖已有文件（无论谁写的都不动）
    if reserved_output.exists():
        _safe_unlink(tmp_out)
        raise VideoError(
            f"预留输出路径已被占用：{reserved_output}（并发冲突或第三方写入）"
        )
    reserved_output.parent.mkdir(parents=True, exist_ok=True)
    # 用 os.link 优先（不覆盖）；跨盘不支持时降级 rename（同盘原子）；
    # 最终若目标路径在 rename 后仍存在冲突，则失败清理。
    try:
        os.link(tmp_out, reserved_output)   # 硬链接（同盘）不覆盖，POSIX 原子
        tmp_out.unlink()
    except (OSError, NotImplementedError):
        # Windows 或跨盘：兜底用 os.rename（同盘原子；跨盘走 shutil）
        try:
            # 二次检查——最小化 TOCTOU 窗口
            if reserved_output.exists():
                _safe_unlink(tmp_out)
                raise VideoError(
                    f"预留输出路径已被占用（rename 前二次检查）：{reserved_output}"
                )
            os.rename(tmp_out, reserved_output)
        except OSError:
            # 跨盘 rename 会 EXDEV，改用 copy2 + unlink
            shutil.copy2(tmp_out, reserved_output)
            _safe_unlink(tmp_out)
    return RenderResult(
        output_path=str(reserved_output),
        final_duration=actual_seconds,
        concat_duration=concat_seconds,
        tts_duration=tts_seconds,
        warnings=warnings,
        encoder_used=used_encoder.encoder,
        hw_fallback_used=hw_fallback,
    )


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def bulk_staging_root() -> Path:
    """R7 允许清理的白名单根目录：data_root()/批量带货/staging（resolve 后）。"""
    from .. import settings as studio_settings

    return (studio_settings.data_root() / "批量带货" / "staging").resolve()


def cleanup_staging(staging_dir: str | Path,
                     *, allowed_root: Path | None = None) -> bool:
    """清理某条任务的 staging 目录。

    R7 硬护栏：
        - 参数为空/不存在 → 直接返回 False；
        - 必须是**目录**（不是文件）；
        - 用 resolve(strict=False) 后必须是 allowed_root（默认 bulk_staging_root()）的
          真子目录（Path.is_relative_to）；
        - allowed_root 本身不能被删除；
        - 不允许目录名穿越（.. / ~ 等 → resolve 后靠 is_relative_to 阻止）；
        - 拒绝符号链接（staging_dir 或任意父层是符号链接就拒绝）。
    返回 True 表示已删除。
    """
    if not staging_dir:
        return False
    path = Path(str(staging_dir))
    root = allowed_root or bulk_staging_root()
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    # 允许清理"未真实存在"但已经从数据库看到的目录（幂等）——但仍要通过白名单校验
    if resolved == root:
        return False  # 禁删根本身
    try:
        # is_relative_to 要求 3.9+
        if not _is_relative_to(resolved, root):
            return False
    except (OSError, ValueError):
        return False
    # 拒绝符号链接（路径本身或某一层）
    if path.exists() and path.is_symlink():
        return False
    cur = path if path.exists() else path.parent
    while cur != cur.parent:
        try:
            if cur.is_symlink():
                return False
        except OSError:
            return False
        if cur == root:
            break
        cur = cur.parent
    # 二级 batch_id / 三级 task_id 必须是受控标识
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return False
    # 至少要到 <batch_id>；task_id 层允许——两级都必须是安全 ID
    if not is_safe_id(parts[0]):
        return False
    if len(parts) >= 2 and not is_safe_id(parts[1]):
        return False
    if len(parts) > 2:
        return False  # 超过 task 层不许清
    # 只删除目录（防误删文件）
    if not path.exists():
        return False
    if not path.is_dir():
        return False
    try:
        shutil.rmtree(path, ignore_errors=False)
    except Exception:  # noqa: BLE001
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:  # noqa: BLE001
            return False
    return True


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


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
