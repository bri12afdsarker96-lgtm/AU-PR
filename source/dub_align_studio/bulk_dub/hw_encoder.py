"""硬件编码探测——只声明"存在"不够，必须做**小样本编码验证**。"""

from __future__ import annotations

import os
import platform
import sys
import time
from dataclasses import dataclass

from integrated_workbench.proc import run_silent

_CANDIDATES: dict[str, tuple[str, list[str]]] = {
    "nvidia": ("h264_nvenc", ["-preset", "p4"]),
    "intel":  ("h264_qsv",   ["-preset", "medium"]),
    "amd":    ("h264_amf",   ["-quality", "balanced"]),
    "cpu":    ("libx264",    ["-preset", "medium"]),
}

FAMILY_PREFERENCE: dict[str, list[str]] = {
    "auto":   ["nvidia", "intel", "amd", "cpu"],
    "nvidia": ["nvidia", "cpu"],
    "intel":  ["intel",  "cpu"],
    "amd":    ["amd",    "cpu"],
    "cpu":    ["cpu"],
}


@dataclass(frozen=True)
class EncoderProbe:
    family: str
    encoder: str
    args: list[str]
    ok: bool
    detail: str


_CACHE: dict[str, tuple[float, EncoderProbe]] = {}
_CACHE_TTL_SECONDS = 600


def null_sink() -> str:
    return "NUL" if sys.platform.startswith("win") else "/dev/null"


def _list_encoders(ffmpeg: str) -> set[str]:
    try:
        completed = run_silent([ffmpeg, "-hide_banner", "-encoders"],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return set()
    names: set[str] = set()
    for line in (completed.stdout or "").splitlines():
        line = line.strip()
        if not line or line.startswith("---") or not line.startswith("V"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            names.add(parts[1])
    return names


def _sample_encode(ffmpeg: str, encoder: str, extra_args: list[str]) -> tuple[bool, str]:
    cmd = [
        # loglevel=warning 而非 error：nvenc「要求驱动版本 X.Y」这类关键信息
        # 有时记在 warning 级，用 error 会被吞掉。用 720p 而非 320x240——
        # 某些 nvenc 对过小分辨率会报无关错误，掩盖真正的驱动问题。
        ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "lavfi", "-i", "testsrc=size=1280x720:duration=1:rate=30",
        "-c:v", encoder, *extra_args,
        "-f", "null", null_sink(),
    ]
    try:
        completed = run_silent(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=30)
    except Exception as exc:  # noqa: BLE001
        return False, f"探测失败：{exc}"
    if completed.returncode == 0:
        return True, "样本编码通过"
    out = (completed.stderr or completed.stdout or "").strip()
    # 优先提取真正有用的那一行（含 encoder 名 / driver / nvenc / api / cuda 等关键字），
    # 而不是取尾部（尾部往往是 "Terminating thread / Nothing was written" 的无用收尾）。
    keywords = (encoder, "driver", "nvenc", "api version", "nvcuda",
                "cuda", "not support", "minimum required", "qsv", "amf",
                "no capable", "no device")
    hits = []
    for line in out.splitlines():
        low = line.lower()
        if any(k.lower() in low for k in keywords):
            hits.append(line.strip())
    detail = " / ".join(hits[:3]) if hits else out[-200:]
    return False, f"样本编码失败：{detail or '未知错误'}"


def probe_family(ffmpeg: str, family: str) -> EncoderProbe:
    encoder, args = _CANDIDATES[family]
    names = _list_encoders(ffmpeg)
    if encoder not in names:
        return EncoderProbe(family, encoder, args, False, f"当前 ffmpeg 不带 {encoder}")
    ok, detail = _sample_encode(ffmpeg, encoder, args)
    return EncoderProbe(family, encoder, args, ok, detail)


def resolve_encoder(ffmpeg: str, preference: str = "auto",
                    *, use_cache: bool = True) -> EncoderProbe:
    key = f"{ffmpeg}:{preference}"
    if use_cache:
        cached = _CACHE.get(key)
        if cached and (time.time() - cached[0] < _CACHE_TTL_SECONDS):
            return cached[1]
    order = FAMILY_PREFERENCE.get(preference) or FAMILY_PREFERENCE["auto"]
    last: EncoderProbe | None = None
    for family in order:
        probe = probe_family(ffmpeg, family)
        last = probe
        if probe.ok:
            _CACHE[key] = (time.time(), probe)
            return probe
    if last is None:
        last = EncoderProbe("cpu", "libx264", [], False, "无可用编码器")
    _CACHE[key] = (time.time(), last)
    return last


def clear_cache() -> None:
    _CACHE.clear()


def system_summary() -> dict:
    try:
        cpu_count = os.cpu_count() or 1
    except Exception:  # noqa: BLE001
        cpu_count = 1
    return {
        "cpu_count": cpu_count,
        "platform": platform.system(),
        "python": platform.python_version(),
    }


def default_video_concurrency() -> int:
    cpu = system_summary()["cpu_count"]
    return max(1, min(4, cpu // 4 or 1))
