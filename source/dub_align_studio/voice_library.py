"""统一音色库：一个音色 = 参考音频（+可选转写文本），dots/fish 两引擎共用。

目录契约（口径基准 §四）：
    <库根>/音色库/<voice_id>/
        ├─ 参考音频.wav     必有
        ├─ 转写.txt         可选（dots continuation clone 用，相似度最高）
        └─ 音色.json        元数据（name / created / note）

纯标准库实现，可单元测试。voice_id 由名称安全化生成，重名自动加序号。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .engines.voice_ref import VoiceRef


VOICE_DIR_NAME = "音色库"
REFERENCE_STEM = "参考音频"  # 实际文件保留原始后缀：参考音频.wav / .mp3 / .flac …
TRANSCRIPT_NAME = "转写.txt"
META_NAME = "音色.json"


@dataclass(frozen=True)
class VoiceEntry:
    voice_id: str
    name: str
    reference_wav: Path
    transcript: str
    note: str
    created: str

    def to_ref(self) -> VoiceRef:
        return VoiceRef(
            voice_id=self.voice_id,
            reference_wav=self.reference_wav,
            transcript=self.transcript,
            name=self.name,
        )


def voices_root(library_root: Path) -> Path:
    return Path(library_root) / VOICE_DIR_NAME


def register_voice(
    library_root: Path,
    name: str,
    reference_wav: Path,
    transcript: str = "",
    note: str = "",
) -> VoiceEntry:
    """登记音色：拷入参考音频、落转写与元数据，返回条目。"""
    reference_wav = Path(reference_wav)
    if not reference_wav.exists():
        raise FileNotFoundError(f"参考音频不存在：{reference_wav}")
    if not name.strip():
        raise ValueError("音色名称不能为空。")

    root = voices_root(library_root)
    voice_id = _unique_voice_id(root, _safe_id(name))
    voice_dir = root / voice_id
    voice_dir.mkdir(parents=True, exist_ok=True)

    suffix = reference_wav.suffix.lower() or ".wav"
    reference_target = voice_dir / f"{REFERENCE_STEM}{suffix}"
    shutil.copyfile(reference_wav, reference_target)
    if transcript.strip():
        (voice_dir / TRANSCRIPT_NAME).write_text(transcript.strip(), encoding="utf-8")
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    (voice_dir / META_NAME).write_text(
        json.dumps({"name": name.strip(), "created": created, "note": note}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return VoiceEntry(
        voice_id=voice_id,
        name=name.strip(),
        reference_wav=reference_target,
        transcript=transcript.strip(),
        note=note,
        created=created,
    )


def list_voices(library_root: Path) -> list[VoiceEntry]:
    root = voices_root(library_root)
    if not root.exists():
        return []
    entries: list[VoiceEntry] = []
    for voice_dir in sorted(root.iterdir()):
        if not voice_dir.is_dir():
            continue
        entry = _load_entry(voice_dir)
        if entry is not None:
            entries.append(entry)
    return entries


def delete_voice(library_root: Path, voice_id: str) -> None:
    """删除音色目录；不存在时视为已删除（幂等）。"""
    voice_dir = voices_root(library_root) / voice_id
    if voice_dir.is_dir():
        shutil.rmtree(voice_dir, ignore_errors=True)


def get_voice(library_root: Path, voice_id: str) -> VoiceEntry:
    voice_dir = voices_root(library_root) / voice_id
    entry = _load_entry(voice_dir)
    if entry is None:
        raise KeyError(f"音色不存在或缺参考音频：{voice_id}")
    return entry


def _load_entry(voice_dir: Path) -> VoiceEntry | None:
    reference = next(iter(sorted(voice_dir.glob(f"{REFERENCE_STEM}.*"))), None)
    if reference is None:
        return None
    meta: dict = {}
    meta_path = voice_dir / META_NAME
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}
    transcript_path = voice_dir / TRANSCRIPT_NAME
    transcript = transcript_path.read_text(encoding="utf-8").strip() if transcript_path.exists() else ""
    return VoiceEntry(
        voice_id=voice_dir.name,
        name=str(meta.get("name") or voice_dir.name),
        reference_wav=reference,
        transcript=transcript,
        note=str(meta.get("note") or ""),
        created=str(meta.get("created") or ""),
    )


def _safe_id(name: str) -> str:
    cleaned = re.sub(r"[^\w一-鿿-]+", "_", name.strip()).strip("_")
    return cleaned or "voice"


def _unique_voice_id(root: Path, base: str) -> str:
    candidate = base
    index = 2
    while (root / candidate).exists():
        candidate = f"{base}_{index}"
        index += 1
    return candidate
