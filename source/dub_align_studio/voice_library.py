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
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .engines.voice_ref import VoiceRef


VOICE_DIR_NAME = "音色库"
REFERENCE_STEM = "参考音频"  # 实际文件保留原始后缀：参考音频.wav / .mp3 / .flac …
TRANSCRIPT_NAME = "转写.txt"
META_NAME = "音色.json"


# 音色设计默认参数（对照 dots.tts / fish-speech 设置面板；随音色一起存档，可迁移）
DEFAULT_PARAMS: dict = {
    "num_steps": 10,
    "guidance_scale": 1.2,
    "speed": 1.0,
    "max_pause_seconds": 0.0,
    "seed": 42,
}
DEFAULT_ENGINE = "dots_local"


def merge_params(raw: dict | None) -> dict:
    """把外部参数并入默认（只保留已知键，类型收敛），缺项用默认补齐。"""
    params = dict(DEFAULT_PARAMS)
    for key in DEFAULT_PARAMS:
        if raw and key in raw and raw[key] is not None:
            try:
                params[key] = int(raw[key]) if key in ("num_steps", "seed") else float(raw[key])
            except (TypeError, ValueError):
                pass
    return params


@dataclass(frozen=True)
class VoiceEntry:
    voice_id: str
    name: str
    reference_wav: Path
    transcript: str
    note: str
    created: str
    engine: str = DEFAULT_ENGINE           # 该音色设计所用引擎/模型
    params: dict = field(default_factory=lambda: dict(DEFAULT_PARAMS))  # 可调合成参数
    released: bool = False                 # 发行音色：已定稿投产，成片页置顶展示（2026-07-25）

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
    engine: str = DEFAULT_ENGINE,
    params: dict | None = None,
) -> VoiceEntry:
    """登记/设计音色：拷入参考音频、落转写、引擎与可调参数、元数据，返回条目。"""
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
    merged = merge_params(params)
    (voice_dir / META_NAME).write_text(
        json.dumps({"name": name.strip(), "created": created, "note": note,
                    "engine": engine or DEFAULT_ENGINE, "params": merged, "released": False},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return VoiceEntry(
        voice_id=voice_id,
        name=name.strip(),
        reference_wav=reference_target,
        transcript=transcript.strip(),
        note=note,
        created=created,
        engine=engine or DEFAULT_ENGINE,
        params=merged,
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
        engine=str(meta.get("engine") or DEFAULT_ENGINE),
        params=merge_params(meta.get("params")),
        released=bool(meta.get("released")),
    )


def set_released(library_root: Path, voice_id: str, released: bool) -> None:
    """发行/取消发行：只改元数据里的 released 标记（音频/转写不动）。"""
    voice_dir = voices_root(library_root) / voice_id
    meta_path = voice_dir / META_NAME
    if not voice_dir.is_dir():
        raise KeyError(f"音色不存在：{voice_id}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    except Exception:
        meta = {}
    meta["released"] = bool(released)
    meta.setdefault("name", voice_dir.name)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}


def batch_import_folder(library_root: Path, folder: Path,
                        engine: str = DEFAULT_ENGINE) -> list[str]:
    """文件夹批量建音色（2026-07-25：人工逐个录入太慢）。

    文件夹里每个音频 = 一个音色（文件名即音色名）；同名 .txt 自动作为参考转写。
    重名自动加序号（_unique_voice_id）。返回建好的音色名列表。"""
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"文件夹不存在：{folder}")
    created: list[str] = []
    for audio in sorted(p for p in folder.iterdir()
                        if p.suffix.lower() in AUDIO_SUFFIXES and p.is_file()):
        transcript = ""
        txt = audio.with_suffix(".txt")
        if txt.exists():
            try:
                transcript = txt.read_text(encoding="utf-8").strip()
            except Exception:
                transcript = ""
        register_voice(library_root, name=audio.stem, reference_wav=audio,
                       transcript=transcript, note="批量导入", engine=engine)
        created.append(audio.stem)
    if not created:
        raise ValueError(f"文件夹里没有音频文件（支持 {'/'.join(sorted(AUDIO_SUFFIXES))}）：{folder}")
    return created


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
                                       transcript=entry.transcript, note=entry.note,
                                       engine=entry.engine, params=entry.params)
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
