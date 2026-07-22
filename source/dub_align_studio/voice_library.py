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


def export_voices_zip(library_root: Path) -> bytes:
    """把整个音色库打包为 zip 字节（跨机备份/迁移；下次导入即用）。"""
    import io
    import zipfile

    buffer = io.BytesIO()
    root = voices_root(library_root)
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for entry in list_voices(library_root):
            voice_dir = root / entry.voice_id
            for file in voice_dir.iterdir():
                if file.is_file():
                    bundle.write(file, f"{entry.voice_id}/{file.name}")
    return buffer.getvalue()


def import_voices_zip(library_root: Path, payload: bytes) -> list[str]:
    """导入音色包 zip；重名音色自动加序号，返回导入的 voice_id 列表。"""
    import io
    import tempfile
    import zipfile

    imported: list[str] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        names = [n for n in bundle.namelist() if "/" in n and not n.endswith("/")]
        voice_ids = sorted({n.split("/", 1)[0] for n in names})
        if not voice_ids:
            raise ValueError("音色包为空或结构不对（应为 音色ID/参考音频.* …）。")
        with tempfile.TemporaryDirectory(prefix="voice_import_") as temp:
            bundle.extractall(temp)
            for voice_id in voice_ids:
                voice_dir = Path(temp) / voice_id
                entry = _load_entry(voice_dir)
                if entry is None:
                    continue
                saved = register_voice(library_root, entry.name, entry.reference_wav,
                                       transcript=entry.transcript, note=entry.note)
                imported.append(saved.voice_id)
    if not imported:
        raise ValueError("音色包里没有可导入的音色（缺参考音频）。")
    return imported


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
