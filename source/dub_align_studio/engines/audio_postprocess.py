"""WAV 后处理层：把"引擎不原生支持的 speed/max_pause"落到实处。

设计动机（v0.7.71 P0）：
    过去 4 个 TTS 引擎里只有 edge_tts 真消费 speed；dots/fish/mock 都收了 speed
    却什么都没做——UI 有滑杆但成片时长不变，属于**假功能**。max_pause_seconds 更严重，
    4 个真引擎全都没消费。

统一做法：
    · engines/longform.synthesize_long 每合成一段 WAV 后，按 EngineCapabilities 决定
      是否要 atempo（native_speed=True 的引擎跳过，防双倍变速；native_speed=False 时
      调 atempo 真改语速、保持音调）。
    · max_pause_seconds > 0 时**所有**引擎都用同一静音压缩逻辑，与引擎能力无关。
    · 处理顺序：**先变速、再压缩静音**——用最终秒数为准（用户滑杆语义直觉如此）。
    · **原子替换**：临时文件写在同目录 `.part.wav`，成功后 `Path.replace` 覆盖旧文件；
      失败时清临时文件、**不动**旧 output（用户重跑同一 chunk 时不误删）。
    · 处理完 wave 读回实际 nframes/sample_rate → 最终秒数（manifest / master.json 用此）。

纯 stdlib + ffmpeg（走 settings.ffmpeg_tool + integrated_workbench.proc.run_silent，
Windows 不弹黑框，与 dots/edge_tts 一致）。
"""

from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Iterable

from integrated_workbench.proc import run_silent

from .. import settings as studio_settings


_ATEMPO_MIN = 0.5    # ffmpeg atempo 单节点最小值——ffmpeg 官方限制 0.5–100.0
_ATEMPO_MAX = 100.0
_ATEMPO_EPS = 1e-3


class PostProcessError(RuntimeError):
    """后处理失败（ffmpeg 缺失 / 转码错误 / 输入 WAV 损坏）。上层清临时文件、保留旧 output。"""


def _resolve_ffmpeg() -> str:
    """解析 ffmpeg 绝对路径；缺失时抛 PostProcessError 给友好中文错。"""
    tool = studio_settings.ffmpeg_tool("ffmpeg")
    if tool and (tool != "ffmpeg" or _which("ffmpeg")):
        return tool if Path(tool).is_file() else (_which("ffmpeg") or tool)
    which = _which("ffmpeg")
    if which:
        return which
    raise PostProcessError(
        "未找到 ffmpeg（用来做 speed/max_pause 后处理）。"
        "请把 ffmpeg.exe 放到软件目录或加入 PATH。"
    )


def _which(name: str) -> str | None:
    import shutil
    return shutil.which(name)


def atempo_chain(speed: float) -> list[str]:
    """把任意 speed（正数）分解为 ffmpeg `atempo=...,atempo=...` 链的每节值。

    ffmpeg 官方 atempo 单节点范围 0.5~100.0。超出用多节链：
      · speed=4.0 → [2.0, 2.0]
      · speed=0.25 → [0.5, 0.5]

    speed<=0（含 NaN、非数字）→ 抛 ValueError，禁止静默按 1.0 处理——用户显式设成 0
    或负数是明确错误，静默兜底会掩盖问题。speed≈1.0 返回空链（调用方跳过 -af）。"""
    try:
        s = float(speed)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"speed 参数不是数字：{speed!r}") from exc
    if s != s:   # NaN
        raise ValueError("speed 不能是 NaN")
    if s <= 0:
        raise ValueError(f"speed 必须为正数，收到 {s!r}（0 或负数无物理意义，"
                         "如需静默请去掉后处理调用）")
    if abs(s - 1.0) < _ATEMPO_EPS:
        return []
    speed = s
    factors: list[float] = []
    remaining = float(speed)
    if remaining > 1.0:
        while remaining > _ATEMPO_MAX + _ATEMPO_EPS:
            factors.append(_ATEMPO_MAX)
            remaining /= _ATEMPO_MAX
    else:
        while remaining < _ATEMPO_MIN - _ATEMPO_EPS:
            factors.append(_ATEMPO_MIN)
            remaining /= _ATEMPO_MIN
    if abs(remaining - 1.0) >= _ATEMPO_EPS:
        factors.append(remaining)
    return [f"{v:.6f}" for v in factors]


def _read_wav(path: Path) -> tuple[bytes, int, int, int]:
    """读回 WAV → (raw_frames_bytes, nchannels, sampwidth, framerate)。"""
    with wave.open(str(path), "rb") as w:
        n = w.getnframes()
        return w.readframes(n), w.getnchannels(), w.getsampwidth(), w.getframerate()


def _write_wav(path: Path, frames: bytes, nchannels: int, sampwidth: int, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(nchannels)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(frames)


def _run_ffmpeg(cmd: list[str], what: str) -> None:
    try:
        completed = run_silent(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise PostProcessError(f"{what}：ffmpeg 未找到（{exc}）") from exc
    if getattr(completed, "returncode", 1) != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-500:]
        raise PostProcessError(f"{what} 失败：{detail or '未知错误'}")


def _apply_atempo(src: Path, dst: Path, ffmpeg: str, speed: float) -> None:
    """对 src.wav 做 ffmpeg atempo=... 变速，写入 dst.wav（PCM16，采样率/声道随源）。"""
    chain = atempo_chain(speed)
    if not chain:
        # speed≈1.0：直接 copy，仍走一次 wave 读写把格式规整化
        raw, ch, sw, sr = _read_wav(src)
        _write_wav(dst, raw, ch, sw, sr)
        return
    # 保留源采样率/声道，只重编码为 PCM16（wave 只读得了 PCM16）
    _, nch, sampwidth, sr = _read_wav(src)
    filt = ",".join(f"atempo={v}" for v in chain)
    _run_ffmpeg([
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(src),
        "-vn", "-filter:a", filt,
        "-ar", str(sr), "-ac", str(nch),
        "-acodec", "pcm_s16le",
        str(dst),
    ], "atempo 变速")


def _compress_silence(src: Path, dst: Path, ffmpeg: str, max_pause_seconds: float) -> None:
    """把 src.wav 里的**长静音段**压到 max_pause_seconds；其余保留原样。

    实现：ffmpeg silencedetect → 拿到每段静音的 [start,end] → 按顺序拼接：
      · 静音段：末尾-开始 > max_pause → 只保留 max_pause 秒；
      · 静音段：末尾-开始 ≤ max_pause → 原样保留；
      · 非静音段：原样保留。
    max_pause≤0 视为"不处理"，直接 copy。

    优先在**纯 wave** 上剪切（避免二次 ffmpeg 重编码带来质量损失）：
      1. 先运行 silencedetect 拿区间；
      2. 用 stdlib wave 按帧索引裁剪、拼接、写回。
    """
    if max_pause_seconds <= 0:
        raw, ch, sw, sr = _read_wav(src)
        _write_wav(dst, raw, ch, sw, sr)
        return

    # 1) 用 ffmpeg silencedetect 拿区间（stderr 输出）
    silence_intervals = _detect_silence(src, ffmpeg, max_pause_seconds)

    # 2) 纯 wave 剪切拼接
    raw, nch, sampwidth, sr = _read_wav(src)
    total_frames = len(raw) // (nch * sampwidth)
    kept_regions = list(_plan_kept_regions(silence_intervals, total_frames, sr,
                                            max_pause_seconds))

    def _slice(a: int, b: int) -> bytes:
        a = max(0, min(a, total_frames))
        b = max(0, min(b, total_frames))
        if a >= b:
            return b""
        step = nch * sampwidth
        return raw[a * step:b * step]

    out = b"".join(_slice(a, b) for a, b in kept_regions)
    _write_wav(dst, out, nch, sampwidth, sr)


def _detect_silence(src: Path, ffmpeg: str, threshold_seconds: float,
                     noise_db: float = -35.0) -> list[tuple[float, float]]:
    """跑 ffmpeg -af silencedetect，从 stderr 解析静音区间列表 [(start, end), ...]。

    threshold_seconds 是**最小静音时长**（<该值的静音不会被 silencedetect 报告）——
    但为了让"短静音也检测得到"，这里传入的应是 max_pause_seconds 的一半，
    过短会被自然过滤；我们再自己按 (end-start) > max_pause 挑要压缩的段。

    ffmpeg 非零退出时**必须**抛 PostProcessError——不能当成"没检测到静音"就无声
    落空，否则用户设了 max_pause 却发现压缩没生效，还不知道 ffmpeg 早就崩了。
    """
    from integrated_workbench.proc import run_silent as _run

    min_dur = max(0.05, threshold_seconds * 0.5)  # 略小于阈值，防漏检
    try:
        completed = _run([
            ffmpeg, "-hide_banner", "-nostats",
            "-i", str(src),
            "-af", f"silencedetect=noise={noise_db}dB:d={min_dur:.3f}",
            "-f", "null", "-",
        ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as exc:
        raise PostProcessError(f"silencedetect：ffmpeg 未找到（{exc}）") from exc
    rc = getattr(completed, "returncode", 1)
    if rc != 0:
        detail = ((completed.stderr or "") + (completed.stdout or "")).strip()[-500:]
        raise PostProcessError(
            f"静音检测失败（ffmpeg 退出码 {rc}）：{detail or '未知错误'}"
        )
    # ffmpeg 把 silencedetect 输出到 stderr（正常情况 returncode=0）
    err = (completed.stderr or "") + (completed.stdout or "")
    intervals: list[tuple[float, float]] = []
    start: float | None = None
    for line in err.splitlines():
        if "silence_start:" in line:
            try:
                start = float(line.split("silence_start:", 1)[1].strip().split()[0])
            except Exception:  # noqa: BLE001
                start = None
        elif "silence_end:" in line and start is not None:
            try:
                end = float(line.split("silence_end:", 1)[1].strip().split()[0])
            except Exception:  # noqa: BLE001
                start = None
                continue
            intervals.append((start, end))
            start = None
    return intervals


def _plan_kept_regions(silence_intervals: list[tuple[float, float]],
                        total_frames: int, sample_rate: int,
                        max_pause_seconds: float) -> Iterable[tuple[int, int]]:
    """把整段 WAV 划分为若干"保留区间"（按帧索引）：
      · 音频区间保留全部；
      · 静音区间 > max_pause 只保留 max_pause 秒（贴左侧起点开始）；
      · 静音区间 ≤ max_pause 原样保留。
    """
    cursor = 0                            # 当前处理位置（帧）
    max_pause_frames = int(round(max_pause_seconds * sample_rate))
    for start_s, end_s in silence_intervals:
        s_frame = max(0, int(round(start_s * sample_rate)))
        e_frame = min(total_frames, int(round(end_s * sample_rate)))
        if e_frame <= s_frame:
            continue
        # 静音前的音频段：cursor → s_frame
        if s_frame > cursor:
            yield (cursor, s_frame)
        # 静音段本身：取头 max_pause_frames
        keep = min(e_frame - s_frame, max_pause_frames)
        if keep > 0:
            yield (s_frame, s_frame + keep)
        cursor = e_frame
    # 结尾余下的音频段
    if cursor < total_frames:
        yield (cursor, total_frames)


def postprocess_wav(path: Path, *, speed: float, max_pause_seconds: float,
                    native_speed: bool) -> float:
    """就地后处理一段 WAV，返回后处理后的**实际秒数**（wave.getnframes/framerate 读回）。

    策略：
      · native_speed=True → 引擎自己已经变速，绝不能再 atempo，防双倍。
      · native_speed=False 且 |speed-1|>1e-3 → atempo 真变速。
      · max_pause_seconds>0 → 压长静音段。
      · 处理顺序：先 atempo、再压静音（用户滑杆语义："最终多久停顿"）。
      · 原子替换：所有中间态写 .part.wav；成功一次 Path.replace 覆盖 path；失败清临时。

    speed 必须为正数（0/负数/NaN 抛 ValueError，不静默兜底）；speed≈1.0 视为无需
    变速；max_pause<=0 视为不压。全部不需要处理时，只做一次"读回真实秒数"就返回，
    不动 path。"""
    # 参数校验：非法 speed 直接报错（P0-3），禁止把 0/负数当作 1.0 静默通过
    try:
        s = float(speed)
    except (TypeError, ValueError) as exc:
        raise PostProcessError(f"speed 参数不是数字：{speed!r}") from exc
    if s != s:
        raise PostProcessError("speed 不能是 NaN")
    if s <= 0:
        raise PostProcessError(
            f"speed 必须为正数，收到 {s!r}（0/负数无物理意义；"
            "如无需变速请显式传 speed=1.0）"
        )
    speed = s
    need_speed = (not native_speed) and abs(speed - 1.0) >= _ATEMPO_EPS
    need_pause = max_pause_seconds is not None and max_pause_seconds > 0

    if not need_speed and not need_pause:
        # 没有任何后处理需求：只回读实际秒数
        return _wav_seconds_of(path)

    ffmpeg = _resolve_ffmpeg()
    path = Path(path)
    tmp_a = path.with_suffix(path.suffix + ".step_a.part.wav")
    tmp_b = path.with_suffix(path.suffix + ".step_b.part.wav")
    tmp_final = path.with_suffix(path.suffix + ".final.part.wav")
    cleanup: list[Path] = [tmp_a, tmp_b, tmp_final]

    def _clean() -> None:
        for p in cleanup:
            try:
                if p.exists():
                    p.unlink()
            except Exception:  # noqa: BLE001
                pass

    try:
        # step 1：atempo
        step1_out = tmp_a
        if need_speed:
            _apply_atempo(path, step1_out, ffmpeg, speed)
        else:
            # 不需要变速时把 path 复制到 tmp_a，供后续静音压缩用；避免直接原地读写风险
            raw, ch, sw, sr = _read_wav(path)
            _write_wav(step1_out, raw, ch, sw, sr)
        # step 2：silence compress
        if need_pause:
            _compress_silence(step1_out, tmp_b, ffmpeg, max_pause_seconds)
            step2_out = tmp_b
        else:
            step2_out = step1_out
        # step 3：把最终结果写到 tmp_final，再原子替换 path
        raw, ch, sw, sr = _read_wav(step2_out)
        _write_wav(tmp_final, raw, ch, sw, sr)
        tmp_final.replace(path)
    except Exception:
        _clean()
        raise
    finally:
        _clean()

    return _wav_seconds_of(path)


def _wav_seconds_of(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        n = w.getnframes()
        sr = w.getframerate()
    if sr <= 0:
        raise PostProcessError(f"WAV 采样率异常：{path}")
    return round(n / sr, 3)
