"""任务指纹——用来在成功产物已存在且**所有输入未变**时复用产物（幂等）。

指纹字段（决定同一条任务）：
    - 规范化视频路径 / 文件大小 / 修改时间（整秒）
    - 文案（去首尾空白）
    - 音色 ID / 语速 / pitch / style
    - 是否保留原声
    - 镜像 & 放大比例（130）
    - 编码画面参数
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _canonical_video_key(video_path: str) -> tuple[str, int, int]:
    try:
        p = Path(video_path).resolve()
        st = p.stat()
        return (p.as_posix(), int(st.st_size), int(st.st_mtime))
    except OSError:
        return (Path(video_path).as_posix(), 0, 0)


def compute_fingerprint(*, video_path: str, text: str, voice_id: str, speed: float,
                        pitch: int = 0, style: str = "general",
                        keep_original_audio: bool = False,
                        mirror: bool = True, zoom_percent: int = 130,
                        encoder_preference: str = "auto",
                        preset: str = "medium",
                        crf: int = 20) -> str:
    """R11-7：`encoder_preference` 而不是硬编码 `encoder="libx264"`——
    不同硬件偏好会走不同真实编码器（nvenc/qsv/amf/libx264），产物字节流不同，必须分开复用。

    schema 升到 v2：所有 v1 指纹失效——避免用旧硬编码 encoder 复用错。
    """
    key = {
        "video": _canonical_video_key(video_path),
        "text": (text or "").strip(),
        "voice_id": voice_id,
        "speed": round(float(speed), 4),
        "pitch": int(pitch),
        "style": style,
        "keep_original_audio": bool(keep_original_audio),
        "mirror": bool(mirror),
        "zoom_percent": int(zoom_percent),
        "encoder_preference": encoder_preference,
        "preset": preset,
        "crf": int(crf),
        "schema": "bulk_dub@v2",
    }
    payload = json.dumps(key, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(payload).hexdigest()
