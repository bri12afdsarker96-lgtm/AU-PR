"""音频指纹：识别"画面已差异化但声音高度一致"的二创成片。

主引擎使用 Chromaprint fpcalc（工具箱可下载，外部进程调用，LGPL 组件不入包）。
fpcalc 不可用时自动回退到 FFmpeg 能量包络指纹，保持与项目"软依赖 + FFmpeg 兜底"
一致的策略：核心流程永远可用，外部组件只做增强。
"""

from __future__ import annotations

import json
import math
import shutil
import struct
import subprocess
from .proc import run_silent
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import ProjectConfig

DEFAULT_AUDIO_SIMILARITY_THRESHOLD = 0.97
ANALYSIS_SECONDS = 120
ENVELOPE_WINDOWS = 128
ENVELOPE_SAMPLE_RATE = 8000

ENGINE_FPCALC = "fpcalc"
ENGINE_FFMPEG_ENVELOPE = "ffmpeg_envelope"


@dataclass
class AudioFingerprintItem:
    video: str
    duration: float
    engine: str
    fingerprint: list[int]
    envelope: list[float]
    error: str = ""


@dataclass
class AudioSimilarityPair:
    left: str
    right: str
    similarity: float
    level: str


@dataclass
class AudioFingerprintReport:
    engine: str
    threshold: float
    items: list[AudioFingerprintItem]
    pairs: list[AudioSimilarityPair]
    high_similarity_count: int

    def to_payload(self) -> dict:
        return {
            "engine": self.engine,
            "threshold": self.threshold,
            "items": [asdict(item) for item in self.items],
            "pairs": [asdict(pair) for pair in self.pairs],
            "summary": {
                "videos": len(self.items),
                "pairs": len(self.pairs),
                "high_similarity": self.high_similarity_count,
            },
        }


def resolve_fpcalc() -> Path | None:
    from .plugins import plugin_catalog

    for plugin in plugin_catalog():
        if plugin.key == "chromaprint_fpcalc" and plugin.is_executable_ready():
            return plugin.executable_path()
    which = shutil.which("fpcalc")
    return Path(which) if which else None


def collect_audio_report(
    config: ProjectConfig,
    videos: list[Path],
    threshold: float = DEFAULT_AUDIO_SIMILARITY_THRESHOLD,
) -> AudioFingerprintReport:
    fpcalc = resolve_fpcalc()
    engine = ENGINE_FPCALC if fpcalc else ENGINE_FFMPEG_ENVELOPE
    items: list[AudioFingerprintItem] = []
    for video in videos:
        if fpcalc:
            items.append(_fpcalc_item(fpcalc, video))
        else:
            items.append(_envelope_item(config, video))
    pairs = pairwise_audio_similarity(items, threshold)
    high_count = sum(1 for pair in pairs if pair.level == "high")
    return AudioFingerprintReport(engine, threshold, items, pairs, high_count)


def _fpcalc_item(fpcalc: Path, video: Path) -> AudioFingerprintItem:
    command = [str(fpcalc), "-json", "-raw", "-length", str(ANALYSIS_SECONDS), str(video)]
    try:
        completed = run_silent(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AudioFingerprintItem(str(video), 0.0, ENGINE_FPCALC, [], [], str(exc))
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-500:]
        return AudioFingerprintItem(str(video), 0.0, ENGINE_FPCALC, [], [], detail or "fpcalc 执行失败")
    try:
        data = json.loads(completed.stdout)
        fingerprint = [int(value) for value in data.get("fingerprint", [])]
        duration = float(data.get("duration", 0.0))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        return AudioFingerprintItem(str(video), 0.0, ENGINE_FPCALC, [], [], f"fpcalc 输出解析失败：{exc}")
    if not fingerprint:
        return AudioFingerprintItem(str(video), duration, ENGINE_FPCALC, [], [], "未生成音频指纹（可能没有音轨）")
    return AudioFingerprintItem(str(video), duration, ENGINE_FPCALC, fingerprint, [])


def _envelope_item(config: ProjectConfig, video: Path) -> AudioFingerprintItem:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    command = [
        ffmpeg,
        "-v",
        "error",
        "-t",
        str(ANALYSIS_SECONDS),
        "-i",
        str(video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(ENVELOPE_SAMPLE_RATE),
        "-f",
        "s16le",
        "-",
    ]
    try:
        completed = run_silent(command, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return AudioFingerprintItem(str(video), 0.0, ENGINE_FFMPEG_ENVELOPE, [], [], str(exc))
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[-500:] if completed.stderr else ""
        return AudioFingerprintItem(
            str(video), 0.0, ENGINE_FFMPEG_ENVELOPE, [], [], detail or "无法解出音频（可能没有音轨）"
        )
    duration = len(completed.stdout) / 2 / ENVELOPE_SAMPLE_RATE
    envelope = envelope_from_pcm(completed.stdout)
    return AudioFingerprintItem(str(video), round(duration, 3), ENGINE_FFMPEG_ENVELOPE, [], envelope)


def envelope_from_pcm(pcm: bytes, windows: int = ENVELOPE_WINDOWS) -> list[float]:
    """把 s16le 单声道 PCM 压成归一化 RMS 能量包络，长度固定 windows。"""
    sample_count = len(pcm) // 2
    if sample_count <= 0 or windows <= 0:
        return []
    samples = struct.unpack(f"<{sample_count}h", pcm[: sample_count * 2])
    window_size = max(1, sample_count // windows)
    envelope: list[float] = []
    for index in range(windows):
        start = index * window_size
        chunk = samples[start : start + window_size]
        if not chunk:
            envelope.append(0.0)
            continue
        rms = math.sqrt(sum(value * value for value in chunk) / len(chunk))
        envelope.append(rms)
    peak = max(envelope) if envelope else 0.0
    if peak <= 0.0:
        return [0.0] * windows
    return [round(value / peak, 6) for value in envelope]


def pairwise_audio_similarity(
    items: list[AudioFingerprintItem],
    threshold: float = DEFAULT_AUDIO_SIMILARITY_THRESHOLD,
) -> list[AudioSimilarityPair]:
    pairs: list[AudioSimilarityPair] = []
    for left_index, left in enumerate(items):
        for right in items[left_index + 1 :]:
            score = audio_similarity(left, right)
            if score < 0:
                continue
            level = "high" if score >= threshold else ("medium" if score >= max(0.85, threshold - 0.10) else "low")
            pairs.append(AudioSimilarityPair(left.video, right.video, round(score, 4), level))
    return sorted(pairs, key=lambda pair: pair.similarity, reverse=True)


def audio_similarity(left: AudioFingerprintItem, right: AudioFingerprintItem) -> float:
    """返回 0..1 相似度；任一方无有效指纹时返回 -1 表示不可比。"""
    if left.error or right.error:
        return -1.0
    if left.fingerprint and right.fingerprint:
        return fingerprint_similarity(left.fingerprint, right.fingerprint)
    if left.envelope and right.envelope:
        return envelope_similarity(left.envelope, right.envelope)
    return -1.0


def fingerprint_similarity(left: list[int], right: list[int], max_offset: int = 3) -> float:
    """chromaprint 原始指纹按位错误率比较，允许小偏移取最优对齐。"""
    if not left or not right:
        return 0.0
    best = 0.0
    for offset in range(-max_offset, max_offset + 1):
        if offset >= 0:
            a, b = left[offset:], right
        else:
            a, b = left, right[-offset:]
        count = min(len(a), len(b))
        if count < 8:
            continue
        errors = sum(((a[i] ^ b[i]) & 0xFFFFFFFF).bit_count() for i in range(count))
        best = max(best, 1.0 - errors / (count * 32.0))
    return max(0.0, best)


def envelope_similarity(left: list[float], right: list[float]) -> float:
    """能量包络余弦相似度（去均值），双静音视为一致。"""
    count = min(len(left), len(right))
    if count <= 0:
        return 0.0
    a = left[:count]
    b = right[:count]
    mean_a = sum(a) / count
    mean_b = sum(b) / count
    centered_a = [value - mean_a for value in a]
    centered_b = [value - mean_b for value in b]
    norm_a = math.sqrt(sum(value * value for value in centered_a))
    norm_b = math.sqrt(sum(value * value for value in centered_b))
    if norm_a < 1e-9 and norm_b < 1e-9:
        return 1.0
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 0.0
    dot = sum(x * y for x, y in zip(centered_a, centered_b))
    return max(0.0, min(1.0, (dot / (norm_a * norm_b) + 1.0) / 2.0))
