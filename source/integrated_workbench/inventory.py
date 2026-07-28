from __future__ import annotations

import csv
import hashlib
import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .models import (
    DIRECTORY_SPECS,
    DIR_A,
    DIR_AUDIO,
    DIR_AUTO_EDIT_INPUT,
    DIR_B,
    DIR_C,
    DIR_INVENTORY,
    DIR_SCRIPT,
    IMAGE_EXTENSIONS,
    MEDIA_EXTENSIONS,
    TABLE_EXTENSIONS,
    TEXT_EXTENSIONS,
    VIDEO_EXTENSIONS,
    ProjectConfig,
)
from .project import ensure_project_structure, iter_media, log_line


@dataclass
class AssetRecord:
    role: str
    path: str
    name: str
    suffix: str
    size_mb: float
    modified_at: str
    sha1: str = ""


@dataclass
class FolderImportResult:
    imported: int
    skipped_non_media: int
    failed: list[str]
    imported_files: list[Path]


ASSET_TARGETS = {
    "原始视频": (DIR_A, VIDEO_EXTENSIONS),
    "去重背景": (DIR_B, VIDEO_EXTENSIONS),
    "贴图素材": (DIR_C, IMAGE_EXTENSIONS),
    "音频素材": (DIR_AUDIO, {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}),
    "文案分镜": (DIR_SCRIPT, TEXT_EXTENSIONS | TABLE_EXTENSIONS),
    "自动剪辑输入": (DIR_AUTO_EDIT_INPUT, VIDEO_EXTENSIONS),
}


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{index:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法生成不重复文件名：{path}")


def _sha1(path: Path, limit_mb: int = 80) -> str:
    if path.stat().st_size > limit_mb * 1024 * 1024:
        return ""
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_assets(config: ProjectConfig, role: str, files: list[str | Path]) -> list[Path]:
    ensure_project_structure(config)
    if role not in ASSET_TARGETS:
        raise ValueError(f"未知素材类型：{role}")
    target_dir, allowed = ASSET_TARGETS[role]
    destination_dir = config.root / target_dir
    destination_dir.mkdir(parents=True, exist_ok=True)

    imported: list[Path] = []
    for raw in files:
        source = Path(raw)
        if not source.exists() or not source.is_file():
            continue
        if source.suffix.lower() not in allowed:
            raise ValueError(f"{source.name} 不适合放入“{role}”。")
        destination = _unique_destination(destination_dir / source.name)
        shutil.copy2(source, destination)
        imported.append(destination)

    if imported:
        log_line(config, f"素材入库：{role} {len(imported)} 个文件。")
        append_import_log(config, role, imported)
    return imported


def count_matching_files_in_subdirs(role: str, folder: str | Path) -> int:
    if role not in ASSET_TARGETS:
        raise ValueError(f"未知素材类型：{role}")
    root = Path(folder)
    if not root.exists() or not root.is_dir():
        return 0
    _target_dir, allowed = ASSET_TARGETS[role]
    count = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        for item in child.rglob("*"):
            if item.is_file() and item.suffix.lower() in allowed:
                count += 1
    return count


def _iter_folder_import_files(root: Path, include_subdirs: bool) -> tuple[list[Path], int]:
    files: list[Path] = []
    skipped = 0
    if include_subdirs:
        iterator = root.rglob("*")
    else:
        iterator = root.iterdir()
    for item in iterator:
        if not item.is_file():
            continue
        files.append(item)
    return sorted(files, key=lambda item: (str(item.parent).lower(), item.name.lower())), skipped


def import_material_folder(
    config: ProjectConfig,
    category: str,
    folder: Path,
    include_subdirs: bool | None = None,
) -> FolderImportResult:
    ensure_project_structure(config)
    if category not in ASSET_TARGETS:
        raise ValueError(f"未知素材类型：{category}")
    source_root = Path(folder)
    if not source_root.exists() or not source_root.is_dir():
        raise NotADirectoryError(f"文件夹不存在：{source_root}")

    target_dir, allowed = ASSET_TARGETS[category]
    destination_dir = config.root / target_dir
    destination_dir.mkdir(parents=True, exist_ok=True)
    include_children = bool(include_subdirs)

    scanned, _skipped = _iter_folder_import_files(source_root, include_children)
    imported: list[Path] = []
    failed: list[str] = []
    skipped_non_media = 0
    for source in scanned:
        if source.suffix.lower() not in allowed:
            skipped_non_media += 1
            continue
        try:
            destination = _unique_destination(destination_dir / source.name)
            shutil.copy2(source, destination)
            imported.append(destination)
        except Exception as exc:
            failed.append(f"{source.name}：{exc}")

    if imported:
        log_line(config, f"素材文件夹入库：{category} {len(imported)} 个文件，跳过 {skipped_non_media} 个，失败 {len(failed)} 个。")
        append_import_log(config, category, imported)
    return FolderImportResult(
        imported=len(imported),
        skipped_non_media=skipped_non_media,
        failed=failed,
        imported_files=imported,
    )


def append_import_log(config: ProjectConfig, role: str, files: list[Path]) -> Path:
    log_path = config.root / DIR_INVENTORY / "素材入库记录.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    exists = log_path.exists()
    with log_path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["time", "role", "path", "name", "size_mb"])
        if not exists:
            writer.writeheader()
        for path in files:
            writer.writerow(
                {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "role": role,
                    "path": str(path),
                    "name": path.name,
                    "size_mb": f"{path.stat().st_size / 1024 / 1024:.2f}",
                }
            )
    return log_path


def collect_inventory(config: ProjectConfig, with_hash: bool = False) -> list[AssetRecord]:
    ensure_project_structure(config)
    records: list[AssetRecord] = []
    for spec in DIRECTORY_SPECS:
        root = config.root / spec.path
        for path in iter_media(root, MEDIA_EXTENSIONS):
            stat = path.stat()
            records.append(
                AssetRecord(
                    role=spec.title,
                    path=str(path),
                    name=path.name,
                    suffix=path.suffix.lower(),
                    size_mb=round(stat.st_size / 1024 / 1024, 3),
                    modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    sha1=_sha1(path) if with_hash else "",
                )
            )
    return records


def write_inventory(config: ProjectConfig, with_hash: bool = False) -> tuple[Path, Path]:
    records = collect_inventory(config, with_hash=with_hash)
    output_dir = config.root / DIR_INVENTORY
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "素材清单.csv"
    json_path = output_dir / "素材清单.json"

    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(AssetRecord.__dataclass_fields__.keys()))
        writer.writeheader()
        for record in records:
            writer.writerow(asdict(record))

    json_path.write_text(json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=2), encoding="utf-8")
    log_line(config, f"素材清单已更新：{len(records)} 条。")
    return csv_path, json_path
