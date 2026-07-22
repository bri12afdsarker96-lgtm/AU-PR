"""真尺子（M3）：从整篇 master.wav 量出每行脚本的真实时长。

口径（docs/配音对齐工作室架构与路线_20260722.md §一/§二）：
    - whisper 只当尺子不当刀：量「这句多长」，不切音频、不做静音吸附；
    - 行级粒度天然容错（每句 ≥5s，边界偏一两百毫秒观感无感）；
    - 产出满足 Σduration == master 总长（末行吸收尾差）。

结构（确定性纪律）：
    - allocate_line_durations：纯逻辑分配核心——给定「转写线索(带时间)」+「已知脚本行」，
      按字符占比在线索时间轴上插值出行边界 → 每行时长。可单元测试。
    - WhisperAligner：非确定外壳——ffmpeg 抽 16k 单声道 WAV → whisper-cli -osrt →
      解析 SRT 线索 → 调纯逻辑核心。复用水星 model_registry 的 whisper 组件模式。
      只进 capability-check，不进单测。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from integrated_workbench.model_registry import best_verified_model, whisper_cli_path, whisper_root
from integrated_workbench.proc import run_silent

from .timing import LineTiming


# ------------------------------------------------------------------ 纯逻辑核心
@dataclass(frozen=True)
class Cue:
    """一条转写线索：master 中某段语音的 [start, end] 与识别文本（来自 whisper SRT）。"""

    start: float
    end: float
    text: str


_PUNCT = re.compile(r"[\s，。！？!?,.、；;：:\"'“”‘’…—\-()（）\[\]【】]+")


def _clean(text: str) -> str:
    """归一化用于长度配比：去标点空白（whisper 与脚本的标点不可比，字数才可比）。"""
    return _PUNCT.sub("", text)


def allocate_line_durations(
    cues: list[Cue],
    lines: list[str],
    total_seconds: float,
) -> list[LineTiming]:
    """按字符占比把线索时间轴划分给各脚本行，返回逐行时长（Σ == total_seconds）。

    原理：脚本已知（TTS 读的就是它），无需识别对错——只需把「语音时间」按
    「各行字数在整篇中的占比」对应到线索时间轴上：
        · 遍历线索，把线索字符按顺序"消耗"给当前行；
        · 行末落在某条线索中间时，边界 = 线索内按字符占比线性插值；
        · 线索之间的静音间隙归前一行（气口跟前句走）；
        · 末行边界强制 = total_seconds（吸收尾差，保证完整划分）。
    """
    if total_seconds <= 0:
        raise ValueError(f"master 总时长必须为正：{total_seconds}")
    clean_lines = [_clean(line) for line in lines]
    if not lines or any(not text for text in clean_lines):
        bad = [i + 1 for i, text in enumerate(clean_lines) if not text]
        raise ValueError(f"脚本行为空或仅标点：行 {bad or '（无行）'}")
    usable = [cue for cue in cues if _clean(cue.text)]
    if not usable:
        raise RuntimeError("没有可用转写线索（whisper 输出为空），无法量行时长。")

    # 展平线索为「字符 → 时间段」序列
    total_chars = sum(len(_clean(cue.text)) for cue in usable)
    line_chars = [len(text) for text in clean_lines]
    script_chars = sum(line_chars)

    # 每行应消耗的线索字符数（按行字数占全篇比例折算到线索总字数；至少 1）
    quotas = [max(1, round(total_chars * chars / script_chars)) for chars in line_chars]

    boundaries: list[float] = []
    cue_index = 0
    used_in_cue = 0
    for quota in quotas[:-1]:  # 末行不用算，边界固定为 total_seconds
        remaining = quota
        while cue_index < len(usable):
            cue = usable[cue_index]
            cue_len = len(_clean(cue.text))
            available = cue_len - used_in_cue
            if remaining < available:
                used_in_cue += remaining
                remaining = 0
                fraction = used_in_cue / cue_len
                boundaries.append(cue.start + fraction * (cue.end - cue.start))
                break
            remaining -= available
            cue_index += 1
            used_in_cue = 0
            if remaining == 0:
                # 行末恰好耗尽本条线索：边界 = 本条线索结束（间隙静音归前一行由下一行 start 决定）
                boundaries.append(cue.end)
                break
        else:
            boundaries.append(usable[-1].end)

    boundaries.append(float(total_seconds))

    timings: list[LineTiming] = []
    previous = 0.0
    for i, (line, boundary) in enumerate(zip(lines, boundaries), start=1):
        boundary = min(max(boundary, previous), total_seconds)
        duration = round(boundary - previous, 3)
        timings.append(LineTiming(index=i, text=line, duration=duration))
        previous = boundary
    # 数值收口：浮点累计差全部并入末行
    drift = round(total_seconds - sum(t.duration for t in timings), 3)
    if abs(drift) > 0:
        last = timings[-1]
        timings[-1] = LineTiming(index=last.index, text=last.text, duration=round(last.duration + drift, 3))
    return timings


# ------------------------------------------------------------------ whisper 外壳
@dataclass(frozen=True)
class AlignerStatus:
    key: str
    available: bool
    detail: str


@dataclass
class WhisperAligner:
    """whisper-cli 尺子：复用水星 whisper 组件（vendor_tools/whisper.cpp + ggml 模型）。"""

    ffmpeg: str = "ffmpeg"

    key: str = "whisper"

    def probe(self) -> AlignerStatus:
        executable = whisper_cli_path()
        model = best_verified_model()
        if executable and model:
            return AlignerStatus(self.key, True, f"whisper-cli 就绪：{executable.name}，模型 {model.name}")
        missing = []
        if not executable:
            missing.append("whisper-cli 未下载")
        if not model:
            missing.append("无校验通过的 ggml 模型")
        return AlignerStatus(self.key, False, "；".join(missing) + "（工具箱组件下载可补齐）。")

    def measure(self, master_wav: Path, lines: list[str]) -> list[LineTiming]:
        status = self.probe()
        if not status.available:
            raise RuntimeError(f"whisper 尺子不可用：{status.detail}")
        master_wav = Path(master_wav)
        if not master_wav.exists():
            raise FileNotFoundError(f"master 音频不存在：{master_wav}")
        total_seconds = _wav_or_probe_seconds(master_wav, self.ffmpeg)
        cues = self._transcribe(master_wav, total_seconds)
        return allocate_line_durations(cues, lines, total_seconds)

    def _transcribe(self, master_wav: Path, total_seconds: float) -> list[Cue]:
        executable = whisper_cli_path()
        model = best_verified_model()
        runtime_root = whisper_root()
        scratch = Path(tempfile.mkdtemp(prefix="dub_align_whisper_"))
        try:
            wav16k = scratch / "master_16k.wav"
            _run(
                [self.ffmpeg, "-y", "-i", str(master_wav), "-ar", "16000", "-ac", "1", str(wav16k)],
                "master 重采样 16k",
            )
            out_base = scratch / "whisper_output"
            timeout = max(60, int(total_seconds * 3 + 60))
            command = [str(executable), "-m", str(model), "-f", str(wav16k), "-osrt", "-of", str(out_base)]
            completed = run_silent(
                command, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, cwd=str(runtime_root) if runtime_root.exists() else None,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "")[-1500:]
                raise RuntimeError(f"whisper 转写失败：{detail.strip() or '未知错误'}")
            srt = out_base.with_suffix(".srt")
            if not srt.exists():
                raise RuntimeError("whisper 未产出 SRT。")
            return parse_srt_cues(srt.read_text(encoding="utf-8", errors="replace"))
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("whisper 转写超时。") from exc
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def parse_srt_cues(content: str) -> list[Cue]:
    """解析 SRT 文本为线索列表（纯逻辑，可单测）。"""
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", content.strip())
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        match = None
        text_start = 0
        for i, line in enumerate(lines):
            match = _SRT_TIME.search(line)
            if match:
                text_start = i + 1
                break
        if not match:
            continue
        start = _srt_seconds(match.group(1), match.group(2), match.group(3), match.group(4))
        end = _srt_seconds(match.group(5), match.group(6), match.group(7), match.group(8))
        text = " ".join(lines[text_start:]).strip()
        if end > start and text:
            cues.append(Cue(start=start, end=end, text=text))
    return cues


def _srt_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def _wav_or_probe_seconds(path: Path, ffmpeg: str) -> float:
    if path.suffix.lower() == ".wav":
        try:
            from .engines.base import wav_seconds

            return wav_seconds(path)
        except Exception:
            pass
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe") if "ffmpeg" in ffmpeg else "ffprobe"
    completed = run_silent(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        return float((completed.stdout or "").strip())
    except ValueError as exc:
        raise RuntimeError(f"无法读取 master 时长：{path}") from exc


def _run(command: list[str], label: str) -> None:
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-1500:]
        raise RuntimeError(f"{label}失败：{detail or '未知错误'}")
