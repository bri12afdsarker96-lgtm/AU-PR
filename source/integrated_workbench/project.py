from __future__ import annotations

import csv
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .models import (
    DIRECTORY_SPECS,
    DIR_A,
    DIR_AUTO_EDIT_INPUT,
    DIR_B,
    DIR_C,
    DIR_CONFIG,
    DIR_LOGS,
    DIR_QUEUE,
    DIR_READY,
    DIR_REVIEW,
    DIR_TITLE_TABLE,
    IMAGE_EXTENSIONS,
    LEGACY_DIR_ALIASES,
    VIDEO_EXTENSIONS,
    ProjectConfig,
    PublishAccount,
)


def all_project_dirs() -> list[str]:
    return [spec.path for spec in DIRECTORY_SPECS]


def project_path(config_or_root: ProjectConfig | str | Path, relative: str) -> Path:
    root = config_or_root.root if isinstance(config_or_root, ProjectConfig) else Path(config_or_root)
    return root / relative


def candidate_dirs(config: ProjectConfig, canonical: str) -> list[Path]:
    result = [config.root / canonical]
    for alias in LEGACY_DIR_ALIASES.get(canonical, []):
        if alias == ".":
            result.append(config.root)
        else:
            result.append(config.root / alias)
    seen: set[str] = set()
    unique: list[Path] = []
    for path in result:
        key = str(path.resolve()) if path.exists() else str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def ensure_project_structure(config_or_root: ProjectConfig | str | Path) -> None:
    root = config_or_root.root if isinstance(config_or_root, ProjectConfig) else Path(config_or_root)
    root.mkdir(parents=True, exist_ok=True)
    for dirname in all_project_dirs():
        (root / dirname).mkdir(parents=True, exist_ok=True)


def atomic_write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_name = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)


def discover_ffmpeg() -> tuple[str, str]:
    if not getattr(sys, "frozen", False):
        ffmpeg = shutil.which("ffmpeg") or ""
        ffprobe = shutil.which("ffprobe") or ""
        if ffmpeg and ffprobe:
            return ffmpeg, ffprobe

    packaged_bins = []
    if getattr(sys, "frozen", False):
        packaged_bins.append(Path(sys.executable).resolve().parent / "ffmpeg" / "bin")
    packaged_bins.append(Path(__file__).resolve().parents[2] / "assets" / "ffmpeg" / "bin")

    for packaged_bin in packaged_bins:
        ffmpeg_path = packaged_bin / "ffmpeg.exe"
        ffprobe_path = packaged_bin / "ffprobe.exe"
        if ffmpeg_path.exists() and ffprobe_path.exists():
            return str(ffmpeg_path), str(ffprobe_path)

    return shutil.which("ffmpeg") or "", shutil.which("ffprobe") or ""


def discover_local_editor() -> tuple[str, str]:
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "local_editor")
    candidates.append(Path(__file__).resolve().parents[2] / "local_editor")

    for root in candidates:
        if not root.exists():
            continue
        executables = sorted(root.glob("*.exe"), key=lambda item: item.name.lower())
        if executables:
            return str(root), str(executables[0])
    return "", ""


def create_project(root: str | Path, name: str, drama_name: str | None = None) -> ProjectConfig:
    project_root = Path(root) / name
    config = ProjectConfig(
        project_name=name,
        project_root=str(project_root),
        drama_name=drama_name or name,
    )
    ensure_project_structure(config)
    ffmpeg, ffprobe = discover_ffmpeg()
    config.tools.ffmpeg = ffmpeg
    config.tools.ffprobe = ffprobe
    config.tools.local_editor_root, config.tools.local_editor_exe = discover_local_editor()
    save_config(config)
    write_default_accounts(config)
    write_directory_table(config)
    write_readme(config)
    write_default_storyboard_template(config)
    return config


def load_config(project_root: str | Path) -> ProjectConfig:
    root = Path(project_root)
    candidates = [root / DIR_CONFIG / "project.json", root / "project.json"]
    for path in candidates:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                config = ProjectConfig.from_dict(json.load(handle))
            ensure_project_structure(config)
            return config
    raise FileNotFoundError(f"没有找到项目配置：{root}")


def save_config(config: ProjectConfig) -> None:
    ensure_project_structure(config)
    payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2)
    for path in [config.root / DIR_CONFIG / "project.json", config.root / "project.json"]:
        atomic_write_text(path, payload)


def write_default_accounts(config_or_root: ProjectConfig | Path) -> None:
    root = config_or_root.root if isinstance(config_or_root, ProjectConfig) else Path(config_or_root)
    ensure_project_structure(root)
    rows = [
        PublishAccount(group="A组", account_id="", nickname="请填写账号1", enabled=False),
        PublishAccount(group="A组", account_id="", nickname="请填写账号2", enabled=False),
    ]
    for path in [root / DIR_CONFIG / "publish_accounts.csv", root / "publish_accounts.csv"]:
        if path.exists():
            continue
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "group",
                    "account_id",
                    "nickname",
                    "publish_count",
                    "interval_minutes",
                    "scheduled_time",
                    "chrome_port",
                    "enabled",
                ],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row.__dict__)


def write_directory_table(config: ProjectConfig) -> Path:
    path = config.root / DIR_CONFIG / "项目目录表.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["目录", "名称", "用途", "放入内容", "产出内容"])
        writer.writeheader()
        for spec in DIRECTORY_SPECS:
            writer.writerow(
                {
                    "目录": spec.path,
                    "名称": spec.title,
                    "用途": spec.purpose,
                    "放入内容": spec.input_hint,
                    "产出内容": spec.output_hint,
                }
            )
    return path


def write_default_storyboard_template(config: ProjectConfig) -> Path:
    path = config.root / DIR_CONFIG / "分镜表模板.csv"
    if path.exists():
        return path
    rows = [
        {
            "镜号": "S001",
            "素材文件": "",
            "开始秒": "0",
            "时长": "4",
            "人物": "",
            "声音类型": "旁白",
            "台词": "这里填写旁白或字幕。",
            "备注": "素材文件可留空，系统会按文件名自动分配。",
        }
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_readme(config: ProjectConfig) -> Path:
    path = config.root / "使用说明.txt"
    lines = [
        f"{config.project_name} 项目说明",
        "",
        "最短可用流程：",
        f"1. 把原始短剧视频放入 {DIR_A}。",
        f"2. 需要更强去重时，把背景视频放入 {DIR_B}，把贴图/边框放入 {DIR_C}。",
        f"3. 需要粗剪时，把候选片段放入 {DIR_AUTO_EDIT_INPUT}，或直接使用原始视频兜底。",
        f"4. 日常一键产线会把可发成片输出到 {DIR_READY}；手动二创结果仍保留在 {DIR_REVIEW} 便于复查。",
        f"5. 标题表会生成到 {DIR_TITLE_TABLE}，发布队列会生成到 {DIR_QUEUE}。",
        "",
        "关键原则：",
        "- 日常发布读取合格待发布或包装成片。",
        "- 发布队列只读取合格待发布目录。",
        "- 每次生成和发布都会写入清单和日志，方便回查。",
        "",
        "详细目录请看 00_项目配置/项目目录表.csv。",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def iter_media(root: Path, extensions: set[str]) -> list[Path]:
    if not root.exists():
        return []
    files: list[Path] = []
    for item in root.rglob("*"):
        if item.is_file() and item.suffix.lower() in extensions:
            files.append(item)
    return sorted(files, key=lambda item: (str(item.parent).lower(), item.name.lower()))


def iter_project_media(config: ProjectConfig, canonical: str, extensions: set[str]) -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    for directory in candidate_dirs(config, canonical):
        for item in iter_media(directory, extensions):
            key = str(item.resolve())
            if key not in seen:
                seen.add(key)
                files.append(item)
    return sorted(files, key=lambda item: (str(item.parent).lower(), item.name.lower()))


def source_videos(config: ProjectConfig) -> list[Path]:
    return iter_project_media(config, DIR_A, VIDEO_EXTENSIONS)


def background_videos(config: ProjectConfig) -> list[Path]:
    return iter_project_media(config, DIR_B, VIDEO_EXTENSIONS)


def stickers(config: ProjectConfig) -> list[Path]:
    return iter_project_media(config, DIR_C, IMAGE_EXTENSIONS)


def ready_videos(config: ProjectConfig) -> list[Path]:
    from .models import DIR_READY

    return iter_project_media(config, DIR_READY, VIDEO_EXTENSIONS)


def import_material_folder(
    config: ProjectConfig,
    category: str,
    folder: Path,
    include_subdirs: bool | None = None,
):
    from .inventory import import_material_folder as _import_material_folder

    return _import_material_folder(config, category, folder, include_subdirs=include_subdirs)


def log_line(config: ProjectConfig, text: str) -> None:
    ensure_project_structure(config)
    log_path = config.root / DIR_LOGS / f"{datetime.now():%Y-%m-%d}.log"
    timestamp = datetime.now().strftime("%H:%M:%S")
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] {text}\n")


def require_files(label: str, files: Iterable[Path]) -> list[Path]:
    result = list(files)
    if not result:
        raise FileNotFoundError(f"{label} 为空，请先放入素材。")
    return result
