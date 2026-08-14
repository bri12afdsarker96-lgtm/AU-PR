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
                   allow_hw_fallback: bool = True,
                   task_id: str = "",
                   fingerprint: str = "",
                   batch_id: str = "") -> RenderResult:
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

    # R4/R11-4/R12-3：**绝不覆盖** + 任务归属 marker sidecar
    if reserved_output.exists():
        _safe_unlink(tmp_out)
        raise VideoError(
            f"预留输出路径已被占用：{reserved_output}（并发冲突或第三方写入）"
        )
    reserved_output.parent.mkdir(parents=True, exist_ok=True)
    committed_path = _commit_with_marker(
        tmp_out, reserved_output,
        task_id=task_id or "no-task",
        fingerprint=fingerprint,
        batch_id=batch_id,
        target_final_seconds=actual_seconds,
        encoder_used=used_encoder.encoder,
        hw_fallback_used=hw_fallback,
    )
    return RenderResult(
        output_path=str(committed_path),
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


MARKER_SCHEMA_V2 = "bulk_dub_marker@v2"
_MARKER_HASH_ALGO = "blake2b"


def marker_path_for(target: Path) -> Path:
    """R12-3 sidecar marker 位于同目录 `.<name>.bulk_dub.marker.json`。"""
    return target.parent / f".{target.name}.bulk_dub.marker.json"


def _marker_tmp_path(target: Path, task_id: str) -> Path:
    """R13-P0-3：marker 临时文件——原子提升成 marker_path_for(target) 前的中间态。"""
    return target.parent / f".{target.name}.bulk_dub.marker.{task_id}.tmp"


def _hidden_part_path(target: Path, task_id: str) -> Path:
    """R12-3 隐藏 .part 文件：`.<name>.<task_id>.part`——同目录，且以点开头。"""
    return target.parent / f".{target.name}.{task_id}.part"


def _blake2b_of_file(path: Path, chunk: int = 1024 * 1024) -> str:
    """R13-P0-3：文件内容哈希——marker 用来在恢复时严格验证内容一致。"""
    import hashlib
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _write_marker(marker: Path, *, task_id: str, batch_id: str, fingerprint: str,
                    target_final_seconds: float, file_size: int,
                    output_name: str, encoder_used: str,
                    hw_fallback_used: bool,
                    content_hash: str = "",
                    hash_algo: str = _MARKER_HASH_ALGO,
                    commit_stage: str = "target_ready",
                    _atomic: bool = True) -> None:
    """R13-P0-3：marker 通过 `<name>.tmp` + fsync + atomic rename 落盘；
    崩溃不留截断 JSON。字段：schema, task_id, batch_id, fingerprint, name,
    size, duration, hash/algo, encoder, hw_fallback, commit_stage, written_at。
    """
    import json as _json
    data = {
        "schema": MARKER_SCHEMA_V2,
        "task_id": task_id,
        "batch_id": batch_id,
        "fingerprint": fingerprint,
        "final_seconds": round(float(target_final_seconds), 3),
        "size": int(file_size),
        "output_name": output_name,
        "encoder_used": encoder_used,
        "hw_fallback_used": bool(hw_fallback_used),
        "content_hash": content_hash,
        "hash_algo": hash_algo,
        "commit_stage": commit_stage,
        "written_at": time.time(),
    }
    if not _atomic:
        marker.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")
        _fsync_file(marker)
        return
    tmp = _marker_tmp_path(marker.parent / marker.name.rsplit(".bulk_dub.marker.json", 1)[0].lstrip("."),
                            task_id) if marker.name.startswith(".") else marker.with_suffix(marker.suffix + ".tmp")
    # 用固定 tmp 命名规则（避免上面复杂反推）
    tmp = marker.parent / (marker.name + f".tmp.{task_id}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as w:
            w.write(_json.dumps(data, ensure_ascii=False))
            w.flush()
            try:
                os.fsync(w.fileno())
            except OSError:
                pass
    finally:
        # fdopen 已管 close；防御一次
        try: os.close(fd)
        except OSError: pass
    os.replace(str(tmp), str(marker))  # POSIX atomic
    _fsync_file(marker)


def read_marker(marker: Path) -> dict | None:
    import json as _json
    try:
        text = marker.read_text(encoding="utf-8")
        v = _json.loads(text)
        if isinstance(v, dict) and v.get("schema") in (MARKER_SCHEMA_V2, "bulk_dub_marker@v1"):
            return v
    except Exception:  # noqa: BLE001
        return None
    return None


def verify_marker_matches_target(marker: dict, target: Path,
                                    *, expected_task_id: str | None = None,
                                    expected_batch_id: str | None = None,
                                    expected_fingerprint: str | None = None,
                                    duration_tolerance: float = DURATION_TOLERANCE_SECONDS,
                                    ) -> tuple[bool, str]:
    """R13-P0-3：marker + target 内容严格闭环校验。任一失败 → (False, 原因)。"""
    if not target.is_file():
        return False, "target 缺失"
    if marker.get("output_name") and marker["output_name"] != target.name:
        return False, "output_name 与 target 名字不一致"
    if expected_task_id and marker.get("task_id") != expected_task_id:
        return False, "task_id 不匹配"
    if expected_batch_id and marker.get("batch_id") \
            and marker["batch_id"] != expected_batch_id:
        return False, "batch_id 不匹配"
    if expected_fingerprint and marker.get("fingerprint") != expected_fingerprint:
        return False, "fingerprint 不匹配"
    try:
        real_size = target.stat().st_size
    except OSError as exc:
        return False, f"target stat 失败：{exc}"
    if marker.get("size") and int(marker["size"]) != real_size:
        return False, "size 不匹配"
    algo = marker.get("hash_algo") or ""
    h_expected = marker.get("content_hash") or ""
    if algo == _MARKER_HASH_ALGO and h_expected:
        h_actual = _blake2b_of_file(target)
        if h_actual != h_expected:
            return False, "hash 不匹配"
    # 时长（可选，若有 marker.final_seconds 且能 ffprobe）
    if marker.get("final_seconds"):
        try:
            actual_dur = ffprobe_seconds(target)
        except Exception:  # noqa: BLE001
            actual_dur = 0
        if actual_dur > 0 and abs(actual_dur - float(marker["final_seconds"])) > duration_tolerance:
            return False, f"时长偏差 {abs(actual_dur - marker['final_seconds']):.3f}s > {duration_tolerance}s"
    return True, "ok"


def _fsync_file(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _commit_with_marker(source: Path, target: Path, *,
                         task_id: str, fingerprint: str,
                         target_final_seconds: float,
                         encoder_used: str,
                         hw_fallback_used: bool,
                         batch_id: str = "") -> Path:
    """R12-3 完整提交协议：

    1. 在目标目录建立隐藏 `.name.<task_id>.part`；
    2. 从 source **完整复制**、flush、fsync（source 若同盘，先 hardlink 到 .part，
       省一次复制；跨盘 fallback 到 chunk 复制）；
    3. ffprobe .part 校验时长；
    4. 原子 rename `.part → target`。为防覆盖：先用 os.link(target) 试探——
       同目录 rename 也不覆盖需二次 exists 检查；
    5. 写 marker sidecar；
    6. 清理 source。

    任一步失败：清理 .part 和 marker，target 已有字节永不动。
    """
    """R13-P0-3：重新排序的提交协议——**marker 先于 target 存在**。

    1. 把 source 完整复制/链接进目录内隐藏 `.part`；flush + fsync；
    2. ffprobe 校验时长 + 计算内容 hash（blake2b）；
    3. **先写 marker**（`.tmp` + fsync + `os.replace` 原子提升）——此时 marker
       已可作为"本批任务产物"的唯一凭证，即使随后崩溃，恢复能凭 marker 认领
       或清理，绝不误吞外部 mp4；
    4. 用 `os.link(part → target)` 原子且不覆盖；不支持时 fallback 到 `os.link`
       方向 tmp 名再 os.link 到 target；仍不支持 → **明确失败**并保留 .part 与
       marker（不允许退化成直接向 target 流式写，避免半文件暴露）；
    5. 提交后再次 `os.replace` marker 更新 `commit_stage=complete`（同一路径，
       内容变化）；
    6. 清理 source staging。

    任一步失败：清理 .part，marker 保留供恢复对账，target 已有字节永不动。
    """
    part = _hidden_part_path(target, task_id)
    marker = marker_path_for(target)
    _safe_unlink(part)
    # 第 1 步：拷/链 source → part（目标目录内）
    try:
        try:
            os.link(source, part)
        except (OSError, NotImplementedError):
            fd = os.open(str(part), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                with os.fdopen(fd, "wb") as w, open(source, "rb") as r:
                    fd = None
                    shutil.copyfileobj(r, w, length=1024 * 1024)
                    w.flush()
                    try:
                        os.fsync(w.fileno())
                    except OSError:
                        pass
            finally:
                if fd is not None:
                    try: os.close(fd)
                    except OSError: pass
        _fsync_file(part)
        # 第 2 步：校验 part（ffprobe + hash）
        actual = ffprobe_seconds(part)
        if actual <= 0:
            raise VideoError("part 文件 ffprobe 无有效时长")
        part_size = part.stat().st_size
        content_hash = _blake2b_of_file(part)
        # 第 3 步：**marker 先落**（atomic replace；此时 target 尚不存在）
        if target.exists():
            raise VideoError(f"提交前发现 target 已存在：{target}")
        _write_marker(marker, task_id=task_id, batch_id=batch_id,
                       fingerprint=fingerprint,
                       target_final_seconds=target_final_seconds,
                       file_size=part_size, output_name=target.name,
                       encoder_used=encoder_used,
                       hw_fallback_used=hw_fallback_used,
                       content_hash=content_hash,
                       commit_stage="part_ready")
        # 第 4 步：不覆盖提交 part → target（严格 no-replace）
        try:
            os.link(part, target)
            part.unlink()
        except (OSError, NotImplementedError):
            # 无 hardlink 支持 → 明确失败，保留 .part 与 marker 让恢复接管；
            # 绝不退化成直接向 target 流式复制（会有半文件可见窗口）。
            raise VideoError(
                f"当前文件系统不支持安全无覆盖链接（os.link 不可用）；"
                f".part 与 marker 已保留在目标目录，需人工确认或换用支持 link 的卷。"
            )
        # 第 5 步：marker 更新为 target_ready（原子）
        _write_marker(marker, task_id=task_id, batch_id=batch_id,
                       fingerprint=fingerprint,
                       target_final_seconds=target_final_seconds,
                       file_size=target.stat().st_size,
                       output_name=target.name,
                       encoder_used=encoder_used,
                       hw_fallback_used=hw_fallback_used,
                       content_hash=content_hash,
                       commit_stage="target_ready")
        # 第 6 步：清 source staging
        _safe_unlink(source)
        return target
    except Exception:
        # 只清 .part；marker 保留供 recovery 对账
        _safe_unlink(part)
        raise


def _commit_no_overwrite(source: Path, target: Path) -> None:
    """把 source 提交为 target——**永不覆盖**已存在的 target，任何步骤失败清理由本函数
    创建的 part，绝不动 target 已有字节。

    策略（R11-4）：
        1. 首选 os.link（同盘，POSIX 语义：EEXIST 就是 EEXIST，不覆盖）；
        2. os.link 不支持（跨盘/Windows/文件系统不支持硬链接）→ 走"目标目录内 .part
           + O_CREAT|O_EXCL 独占创建 + copy 内容 + O_EXCL 建目标 + rename part→target
           被替换"。这里用**os.rename(part, target)** 也不安全（POSIX 可覆盖）；因此
           改用 **os.link(part, target) + unlink part** 二次尝试；仍不支持则最后手段：
           用 O_EXCL 打开 target 直接 write 全部字节（可能慢，但语义正确）。
        3. 任何步骤失败：如 target 出现新字节，也不能视为提交；抛错让上层清理。
    """
    # 首选：硬链接（同盘）；EEXIST → target 已被别人占用（拒绝）；ENOENT 之类正常抛
    try:
        os.link(source, target)
        source.unlink()
        return
    except FileExistsError:
        # 有人抢了 target；不动 target
        raise VideoError(f"提交时发现目标已存在：{target}")
    except (OSError, NotImplementedError):
        pass  # 走跨盘/无 hardlink 路径

    # 跨盘或不支持 hardlink：手动 O_EXCL 打开 target 写入 → 语义正确的不覆盖
    fd = None
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise VideoError(f"提交时发现目标已存在（EXCL 创建失败）：{target}")
    try:
        with os.fdopen(fd, "wb") as w, open(source, "rb") as r:
            fd = None
            shutil.copyfileobj(r, w, length=1024 * 1024)
    except Exception:
        # 写入过程中失败 → 清理 target（是我们刚创的），也清理 source
        _safe_unlink(target)
        _safe_unlink(source)
        raise
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    _safe_unlink(source)


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
