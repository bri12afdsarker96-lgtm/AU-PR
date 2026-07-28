from __future__ import annotations

import csv
import json
import shutil
from .proc import run_silent
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import (
    DIR_A,
    DIR_AUTO_EDIT_INPUT,
    DIR_AUTO_EDIT_OUTPUT,
    DIR_B,
    DIR_C,
    DIR_DONE,
    DIR_READY,
    DIR_REJECT,
    DIR_REPORTS,
    DIR_REVIEW,
    DIR_TITLES,
    IMAGE_EXTENSIONS,
    ProjectConfig,
    VIDEO_EXTENSIONS,
)
from .project import iter_media, iter_project_media, log_line
from .publish_queue import load_accounts


AUTO_REJECT_MIN_SECONDS = 1.0


@dataclass
class WorkflowCounts:
    source: int
    background: int
    stickers: int
    auto_input: int
    auto_packages: int
    titles: int
    intermediate: int
    ready: int
    done: int
    accounts: int


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{index:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法生成不重复文件名：{path}")


def latest_auto_edit_dir(config: ProjectConfig) -> Path | None:
    output_root = config.root / DIR_AUTO_EDIT_OUTPUT
    if not output_root.exists():
        return None
    manifests = sorted(output_root.rglob("edit_handoff.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    if manifests:
        return manifests[0].parent
    folders = [item for item in output_root.iterdir() if item.is_dir()]
    if not folders:
        return None
    return max(folders, key=lambda item: item.stat().st_mtime)


def latest_auto_edit_selected_dir(config: ProjectConfig) -> Path | None:
    work_dir = latest_auto_edit_dir(config)
    if not work_dir:
        return None
    manifest = work_dir / "edit_handoff.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            selected = Path(str(data.get("selected_dir") or ""))
            if selected.exists():
                return selected
        except (OSError, json.JSONDecodeError):
            pass
    for name in ["01_筛选分镜", "01_筛选片段", "selected"]:
        candidate = work_dir / name
        if candidate.exists():
            return candidate
    return work_dir


def workflow_counts(config: ProjectConfig) -> WorkflowCounts:
    output_root = config.root / DIR_AUTO_EDIT_OUTPUT
    auto_packages = len(list(output_root.glob("*/edit_handoff.json"))) if output_root.exists() else 0
    return WorkflowCounts(
        source=len(iter_media(config.root / DIR_A, VIDEO_EXTENSIONS)),
        background=len(iter_media(config.root / DIR_B, VIDEO_EXTENSIONS)),
        stickers=len(iter_media(config.root / DIR_C, IMAGE_EXTENSIONS)),
        auto_input=len(iter_media(config.root / DIR_AUTO_EDIT_INPUT, VIDEO_EXTENSIONS)),
        auto_packages=auto_packages,
        titles=len(iter_media(config.root / DIR_TITLES, IMAGE_EXTENSIONS)),
        intermediate=len(iter_project_media(config, DIR_REVIEW, VIDEO_EXTENSIONS)),
        ready=len(iter_media(config.root / DIR_READY, VIDEO_EXTENSIONS)),
        done=len(iter_media(config.root / DIR_DONE, VIDEO_EXTENSIONS)),
        accounts=len(load_accounts(config)),
    )


def _probe_duration_seconds(config: ProjectConfig, video: Path) -> float | None:
    ffprobe = config.tools.ffprobe or ""
    if not ffprobe:
        return None
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video),
    ]
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def _release_block_reason(config: ProjectConfig, video: Path) -> str:
    duration = _probe_duration_seconds(config, video)
    if duration is not None and duration <= AUTO_REJECT_MIN_SECONDS:
        return f"视频时长只有 {duration:.1f} 秒，低于可发布底线。"
    return ""


def promote_videos_to_ready(config: ProjectConfig, videos: list[Path]) -> list[Path]:
    ready_dir = config.root / DIR_READY
    ready_dir.mkdir(parents=True, exist_ok=True)

    moved: list[Path] = []
    for video in videos:
        if not video.exists():
            continue
        block_reason = _release_block_reason(config, video)
        if block_reason:
            _move_to_reject(config, video, block_reason)
            continue
        destination = _unique_destination(ready_dir / video.name)
        shutil.move(str(video), str(destination))
        moved.append(destination)

    if moved:
        log_line(config, f"本次生成视频自动进入待发布：{len(moved)} 条。")
        append_release_log(config, "自动放行", moved)
    return moved


def _move_to_reject(config: ProjectConfig, video: Path, reason: str = "") -> Path:
    reject_root = config.root / DIR_REJECT
    reject_root.mkdir(parents=True, exist_ok=True)
    destination = _unique_destination(reject_root / video.name)
    shutil.move(str(video), str(destination))
    message = f"视频进入退回回收：{video.name}"
    if reason:
        message += f"，原因：{reason}"
    log_line(config, message)
    append_release_log(config, "自动退回", [destination], reason=reason)
    return destination


def append_release_log(config: ProjectConfig, action: str, videos: list[Path], reason: str = "") -> Path:
    path = config.root / DIR_REPORTS / "放行记录.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["time", "action", "video_path", "video_name", "reason"])
        if not exists:
            writer.writeheader()
        for video in videos:
            writer.writerow(
                {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "action": action,
                    "video_path": str(video),
                    "video_name": video.name,
                    "reason": reason,
                }
            )
    return path


def write_workflow_snapshot(config: ProjectConfig) -> Path:
    counts = workflow_counts(config)
    output = config.root / DIR_REPORTS / f"流程状态_{datetime.now():%Y%m%d_%H%M%S}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(counts.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
    return output
