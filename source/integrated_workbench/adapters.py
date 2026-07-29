from __future__ import annotations

import csv
import json
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    DIR_A,
    DIR_AUTO_EDIT_OUTPUT,
    DIR_B,
    DIR_C,
    DIR_EDITOR_HANDOFF,
    DIR_PACKAGED_VIDEO,
    DIR_PUBLISH_HANDOFF,
    DIR_PUBLISH_STATUS,
    DIR_QUEUE,
    DIR_READY,
    DIR_REVIEW,
    ProjectConfig,
)
from .project import background_videos, ready_videos, source_videos, stickers
from .publish_queue import QUEUE_FIELDS, build_queue
from .production_state import latest_auto_edit_dir


@dataclass
class EditorImportResult:
    imported: list[Path]
    skipped: list[str]
    report_json: Path


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def export_editor_handoff(config: ProjectConfig) -> Path:
    handoff_dir = config.root / DIR_EDITOR_HANDOFF
    handoff_dir.mkdir(parents=True, exist_ok=True)

    latest_edit = latest_auto_edit_dir(config)
    plan_rows = [
        {
            "task_type": "二创视频生成",
            "directory_version": config.directory_version,
            "mode": config.recipe.mode,
            "source_folder": str(config.root / DIR_A),
            "background_folder": str(config.root / DIR_B),
            "sticker_folder": str(config.root / DIR_C),
            "intermediate_output": str(config.root / DIR_REVIEW),
            "ready_publish": str(config.root / DIR_READY),
            "auto_edit_output": str(config.root / DIR_AUTO_EDIT_OUTPUT),
            "latest_auto_edit_dir": str(latest_edit or ""),
            "copies_per_source": str(config.recipe.copies_per_source),
            "b_opacity": str(config.recipe.b_opacity),
            "c_scale": str(config.recipe.c_scale),
            "c_opacity": str(config.recipe.c_opacity),
            "background_blur": str(config.recipe.background_blur),
        }
    ]

    csv_path = handoff_dir / "editor_task_plan.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(plan_rows[0].keys()))
        writer.writeheader()
        writer.writerows(plan_rows)

    manifest: dict[str, Any] = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "project_name": config.project_name,
        "drama_name": config.drama_name,
        "project_root": str(config.root),
        "directory_version": config.directory_version,
        "local_editor_exe": config.tools.local_editor_exe,
        "recipe": asdict(config.recipe),
        "source_count": len(source_videos(config)),
        "background_count": len(background_videos(config)),
        "sticker_count": len(stickers(config)),
        "source_folder": str(config.root / DIR_A),
        "background_folder": str(config.root / DIR_B),
        "sticker_folder": str(config.root / DIR_C),
        "ready_count": len(ready_videos(config)),
        "intermediate_output": str(config.root / DIR_REVIEW),
        "ready_publish": str(config.root / DIR_READY),
        "auto_edit_output": str(config.root / DIR_AUTO_EDIT_OUTPUT),
        "latest_auto_edit_dir": str(latest_edit or ""),
        "latest_auto_edit_handoff": str((latest_edit / "edit_handoff.json") if latest_edit else ""),
        "task_plan_csv": str(csv_path),
        "editor_result_import_dir": str(handoff_dir),
        "expected_editor_result_files": ["editor_result.csv", "editor_result.json"],
        "expected_editor_result_fields": ["output_video", "status", "title", "message"],
    }
    json_path = handoff_dir / "editor_handoff.json"
    json_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    (handoff_dir / "给本地剪辑器的说明.txt").write_text(
        "\n".join(
            [
                "这个目录是给本地剪辑器或人工精修流程使用的交接包。",
                "editor_task_plan.csv 是任务表，记录素材目录、输出目录、生成参数和最新自动剪辑包。",
                "editor_handoff.json 是机器可读配置，后续可以让剪辑器直接读取。",
                "剪辑器处理完成后，可回写 editor_result.csv 或 editor_result.json，字段包含 output_video/status/title/message。",
                "工作台点击“导入剪辑器回写”后，会把成功输出导入二创生产的中间结果目录。",
                "如果用户已经在本工作台生成二创视频，合格视频会在 03_二创生产/03_合格待发布。",
            ]
        ),
        encoding="utf-8",
    )
    return json_path


def export_publish_handoff(config: ProjectConfig) -> Path:
    queue_path = build_queue(config)
    handoff_dir = config.root / DIR_PUBLISH_HANDOFF
    handoff_dir.mkdir(parents=True, exist_ok=True)

    queue_rows = _read_csv(queue_path)
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "project_name": config.project_name,
        "drama_name": config.drama_name,
        "directory_version": config.directory_version,
        "publish_accounts_csv": str(config.root / "00_项目配置" / "publish_accounts.csv"),
        "publish_queue_csv": str(queue_path),
        "publish_queue_dir": str(config.root / DIR_QUEUE),
        "ready_video_dir": str(config.root / DIR_READY),
        "packaged_video_dir": str(config.root / DIR_PACKAGED_VIDEO),
        "status_callback_dir": str(config.root / DIR_PUBLISH_STATUS),
        "expected_status_files": ["publish_status.csv", "publish_status.json"],
        "expected_status_fields": ["video_path", "status", "platform", "platform_url", "external_task_id", "published_at", "publish_error"],
        "queue": queue_rows,
    }

    json_path = handoff_dir / "publish_handoff.json"
    json_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    publish_csv = handoff_dir / "发布工具导入队列.csv"
    with publish_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=QUEUE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(queue_rows)

    (handoff_dir / "给发布工具的说明.txt").write_text(
        "\n".join(
            [
                "这个目录是给发布模块使用的交接包。",
                "发布工具导入队列.csv 是普通表格格式，适合直接导入或人工检查。",
                "publish_handoff.json 是机器可读格式，适合后续让发布模块一键读取。",
                f"发布完成或失败后，把 publish_status.csv/json 写入 {config.root / DIR_PUBLISH_STATUS}，工作台会回写队列状态、链接和失败原因。",
                "发布队列只来自 03_二创生产/03_合格待发布，中间结果不会被误发。",
            ]
        ),
        encoding="utf-8",
    )
    return json_path


def import_editor_result(config: ProjectConfig, result_file: str | Path) -> EditorImportResult:
    path = Path(result_file)
    rows = _read_result_rows(path)
    intermediate_dir = config.root / DIR_REVIEW / f"editor_import_{datetime.now():%Y%m%d_%H%M%S}"
    intermediate_dir.mkdir(parents=True, exist_ok=True)
    imported: list[Path] = []
    skipped: list[str] = []
    for row in rows:
        status = str(row.get("status") or row.get("状态") or "ok").strip().lower()
        if status in {"fail", "failed", "失败", "error", "异常"}:
            skipped.append(row.get("message") or row.get("output_video") or str(row))
            continue
        source = Path(row.get("output_video") or row.get("video_path") or row.get("输出视频") or "")
        if not source.exists():
            skipped.append(f"输出不存在：{source}")
            continue
        destination = _unique_copy_destination(intermediate_dir / source.name)
        shutil.copy2(source, destination)
        imported.append(destination)
    report = {
        "source": str(path),
        "intermediate_dir": str(intermediate_dir),
        "imported": [str(item) for item in imported],
        "skipped": skipped,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    report_json = intermediate_dir / "editor_result_import.json"
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return EditorImportResult(imported, skipped, report_json)


def _read_result_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"剪辑器回写文件不存在：{path}")
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = payload.get("items") or payload.get("rows") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        return [{str(key): str(value) for key, value in row.items()} for row in rows if isinstance(row, dict)]
    return _read_csv(path)


def _unique_copy_destination(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{index:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法生成不重复文件名：{path}")


def launch_local_editor(config: ProjectConfig) -> subprocess.Popen[str]:
    path = Path(config.tools.local_editor_exe)
    if not path.exists():
        raise FileNotFoundError(f"本地剪辑器不存在：{path}")
    return subprocess.Popen([str(path)], cwd=str(path.parent))


def launch_publish_tool(config: ProjectConfig) -> subprocess.Popen[str]:
    if not config.tools.publish_tool_exe:
        raise FileNotFoundError("尚未配置 publish_tool_exe。")
    path = Path(config.tools.publish_tool_exe)
    if not path.exists():
        raise FileNotFoundError(f"发布工具不存在：{path}")
    return subprocess.Popen([str(path)], cwd=str(path.parent))
