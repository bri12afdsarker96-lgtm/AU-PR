"""导出设置引擎：分辨率/格式/帧率/码率/编码 → FFmpeg 输出参数。

纯逻辑：产出 FFmpeg 输出参数列表与体积估算，交由渲染层执行；含常见平台预设。
"""

from __future__ import annotations

from dataclasses import dataclass

from .edit_compose import aspect_canvas


# 分辨率名 -> 短边像素（None 表示保持原始）。与画面比例组合出完整 W×H。
RESOLUTIONS: dict[str, int | None] = {
    "480P": 480,
    "720P": 720,
    "1080P": 1080,
    "2K": 1440,
    "4K": 2160,
    "8K": 4320,
    "原始": None,
}

# 容器格式 -> 扩展名
FORMATS: dict[str, str] = {"MP4": ".mp4", "MOV": ".mov", "MXF": ".mxf"}

# 编码名 -> FFmpeg 视频编码器
CODECS: dict[str, str] = {
    "H.264": "libx264",
    "HEVC": "libx265",
    "HEVC (Alpha)": "libx265",
    "AV1": "libsvtav1",
    "RLE": "qtrle",
}

FRAME_RATES: tuple[int, ...] = (24, 25, 30, 50, 60)

BITRATE_MODES: tuple[str, ...] = ("更低", "推荐", "更高", "自定义")

# 各短边的“推荐”视频码率（kbps）
_RECOMMENDED_KBPS: dict[int, int] = {
    480: 2500, 720: 5000, 1080: 8000, 1440: 16000, 2160: 35000, 4320: 80000,
}
_MODE_FACTOR = {"更低": 0.6, "推荐": 1.0, "更高": 1.6}


@dataclass
class ExportSettings:
    resolution: str = "1080P"
    aspect_ratio: str = "9:16 竖屏"
    fmt: str = "MP4"
    fps: int = 30
    bitrate_mode: str = "推荐"
    custom_kbps: int = 8000
    codec: str = "H.264"
    audio_kbps: int = 192
    hardware_upscale: bool = False  # 一键超清（对应外部超分组件）
    backup_to_cloud: bool = False


def _short_side(resolution: str, fallback: int = 1080) -> int:
    value = RESOLUTIONS.get(resolution, fallback)
    return value or fallback


def export_dimensions(settings: ExportSettings, source_short_side: int = 1080) -> tuple[int, int]:
    base = _short_side(settings.resolution, fallback=source_short_side)
    return aspect_canvas(settings.aspect_ratio, base=base)


def video_bitrate_kbps(settings: ExportSettings) -> int:
    if settings.bitrate_mode == "自定义":
        return max(200, int(settings.custom_kbps))
    short = _short_side(settings.resolution)
    base = _RECOMMENDED_KBPS.get(short, 8000)
    factor = _MODE_FACTOR.get(settings.bitrate_mode, 1.0)
    return int(base * factor)


def estimate_size_mb(settings: ExportSettings, duration_seconds: float) -> float:
    """估算成片体积（MB）= (视频码率 + 音频码率) × 时长 / 8 / 1024。"""
    total_kbps = video_bitrate_kbps(settings) + max(0, settings.audio_kbps)
    megabytes = total_kbps * max(0.0, duration_seconds) / 8 / 1024
    return round(megabytes, 1)


def output_extension(settings: ExportSettings) -> str:
    return FORMATS.get(settings.fmt, ".mp4")


def build_output_args(settings: ExportSettings, source_short_side: int = 1080) -> list[str]:
    """构造 FFmpeg 输出侧参数（不含 -i 输入，交由渲染层拼接）。"""
    width, height = export_dimensions(settings, source_short_side)
    codec = CODECS.get(settings.codec, "libx264")
    v_kbps = video_bitrate_kbps(settings)
    args: list[str] = [
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        "-r", str(settings.fps),
        "-c:v", codec,
        "-b:v", f"{v_kbps}k",
        "-pix_fmt", "yuv420p" if settings.codec not in {"RLE", "HEVC (Alpha)"} else "yuva420p",
        "-c:a", "aac",
        "-b:a", f"{settings.audio_kbps}k",
    ]
    if settings.codec == "AV1":
        args += ["-preset", "8"]
    return args


# 平台导出预设：名称 -> ExportSettings 覆盖字段
PLATFORM_PRESETS: dict[str, dict] = {
    "抖音竖屏": {"aspect_ratio": "9:16 竖屏", "resolution": "1080P", "fps": 30, "codec": "H.264"},
    "快手竖屏": {"aspect_ratio": "9:16 竖屏", "resolution": "1080P", "fps": 30, "codec": "H.264"},
    "视频号竖屏": {"aspect_ratio": "9:16 竖屏", "resolution": "1080P", "fps": 30, "codec": "H.264"},
    "横屏高清": {"aspect_ratio": "16:9 横屏", "resolution": "1080P", "fps": 30, "codec": "H.264"},
    "B站4K": {"aspect_ratio": "16:9 横屏", "resolution": "4K", "fps": 60, "codec": "HEVC"},
}


def preset_settings(name: str) -> ExportSettings:
    overrides = PLATFORM_PRESETS.get(name, {})
    return ExportSettings(**overrides)
