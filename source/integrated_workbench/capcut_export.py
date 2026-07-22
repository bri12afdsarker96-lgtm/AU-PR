from __future__ import annotations

import csv
import json
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import DIR_EDITOR_HANDOFF, ProjectConfig
from .project import log_line
from .production_state import latest_auto_edit_dir


@dataclass
class CapCutDraftPackage:
    package_dir: Path
    manifest_json: Path
    script_py: Path
    timeline_csv: Path
    material_dir: Path
    real_draft_dir: Path | None
    message: str


@dataclass
class CapCutEnvironment:
    pycapcut_root: Path | None
    import_ready: bool

    @property
    def message(self) -> str:
        if self.import_ready:
            return "真实剪映/CapCut 草稿创建依赖已就绪，可尝试直接创建草稿。"
        if self.pycapcut_root:
            return f"已找到 pyCapCut 源码：{self.pycapcut_root}；如无法创建真实草稿，请先安装依赖。"
        return "未找到 pyCapCut 依赖；当前可生成剪映脚本包和时间线 CSV，暂不能保证真实草稿。"


def capcut_environment() -> CapCutEnvironment:
    root = _pycapcut_root()
    import_ready = False
    if root and str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        import importlib.util

        import_ready = importlib.util.find_spec("pycapcut") is not None
    except Exception:
        import_ready = False
    return CapCutEnvironment(root, import_ready)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _safe_name(name: str) -> str:
    invalid = '<>:"/\\|?*'
    cleaned = "".join("_" if char in invalid else char for char in name).strip()
    return cleaned or datetime.now().strftime("capcut_%Y%m%d_%H%M%S")


def _pick(row: dict[str, str], keys: list[str]) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return str(value).strip()
    return ""


def _video_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    video_rows = [row for row in rows if (row.get("track_type") or "").strip().lower() in {"", "video"}]
    return video_rows or rows


def _pycapcut_root() -> Path | None:
    candidates = [
        Path(__file__).resolve().parents[2] / "vendor_tools" / "pyCapCut",
        Path(sys.executable).resolve().parent / "vendor_tools" / "pyCapCut",
        Path.cwd() / "vendor_tools" / "pyCapCut",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _latest_or_raise(config: ProjectConfig, work_dir: str | Path | None = None) -> Path:
    if work_dir:
        path = Path(work_dir)
        if not path.exists():
            raise FileNotFoundError(f"自动剪辑包不存在：{path}")
        return path
    latest = latest_auto_edit_dir(config)
    if not latest:
        raise FileNotFoundError("还没有自动剪辑包，请先在自动剪辑页面生成分镜素材包。")
    return latest


def export_capcut_draft_package(
    config: ProjectConfig,
    draft_root: str | Path | None = None,
    work_dir: str | Path | None = None,
    create_real_draft: bool = False,
) -> CapCutDraftPackage:
    auto_dir = _latest_or_raise(config, work_dir)
    handoff_path = auto_dir / "edit_handoff.json"
    handoff: dict[str, Any] = {}
    if handoff_path.exists():
        handoff = json.loads(handoff_path.read_text(encoding="utf-8"))

    rows = _read_csv(auto_dir / "剪映导入清单.csv") or _read_csv(auto_dir / "自动剪辑计划.csv")
    if not rows:
        raise FileNotFoundError(f"自动剪辑包缺少剪映导入清单：{auto_dir}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    package_dir = config.root / DIR_EDITOR_HANDOFF / f"capcut_draft_{stamp}"
    material_dir = package_dir / "materials"
    material_dir.mkdir(parents=True, exist_ok=True)

    timeline_rows = _video_rows(rows)
    copied_rows: list[dict[str, str]] = []
    for index, row in enumerate(timeline_rows, start=1):
        source_text = _pick(row, ["path", "clip_path", "selected_clip", "素材文件", "candidate_file", "source_path"])
        source = Path(source_text)
        copied = ""
        if source.exists() and source.is_file():
            target = material_dir / f"{index:03d}_{_safe_name(source.name)}"
            if not target.exists():
                shutil.copy2(source, target)
            copied = str(target)
        copied_rows.append(row | {"copied_material": copied})

    timeline_csv = package_dir / "capcut_timeline.csv"
    _write_timeline(copied_rows, timeline_csv)
    script_py = package_dir / "create_capcut_draft.py"
    _write_script(script_py, timeline_csv, package_dir)

    real_draft_dir: Path | None = None
    message = "已生成剪映草稿脚本包。"
    if create_real_draft:
        real_draft_dir = _try_create_real_draft(config, package_dir, timeline_csv, draft_root)
        if real_draft_dir:
            message = "已生成剪映草稿脚本包，并创建真实 CapCut 草稿。"
        else:
            message = "已生成剪映草稿脚本包；真实 CapCut 草稿未创建，请按使用说明补齐依赖或草稿目录后再运行脚本。"

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "project_name": config.project_name,
        "drama_name": config.drama_name,
        "source_auto_edit_dir": str(auto_dir),
        "source_handoff": str(handoff_path if handoff_path.exists() else ""),
        "selected_dir": handoff.get("selected_dir", ""),
        "draft_root": str(draft_root or ""),
        "create_real_draft": create_real_draft,
        "real_draft_dir": str(real_draft_dir or ""),
        "timeline_csv": str(timeline_csv),
        "script_py": str(script_py),
        "material_dir": str(material_dir),
        "rows": copied_rows,
        "source_rows_count": len(rows),
        "timeline_rows_count": len(copied_rows),
        "note": "如果未安装 pyCapCut，可先查看 capcut_timeline.csv；安装后运行 create_capcut_draft.py 生成草稿。",
    }
    manifest_json = package_dir / "capcut_draft_manifest.json"
    manifest_json.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (package_dir / "使用说明.txt").write_text(
        "\n".join(
            [
                "这是自动剪辑结果导出的剪映/CapCut 草稿交接包。",
                "capcut_timeline.csv：分镜时间线和本地复制素材。",
                "create_capcut_draft.py：安装 pyCapCut 后可运行的草稿生成脚本。",
                "materials：已复制的自动剪辑片段，方便转移到剪映草稿目录。",
                "如果你已经设置 CapCut 草稿目录，可在命令行使用 --draft-root 直接尝试生成真实草稿。",
            ]
        ),
        encoding="utf-8",
    )
    log_line(config, f"剪映草稿包已生成：{package_dir}")
    return CapCutDraftPackage(package_dir, manifest_json, script_py, timeline_csv, material_dir, real_draft_dir, message)


def _write_timeline(rows: list[dict[str, str]], output: Path) -> None:
    fields = [
        "index",
        "shot_id",
        "material_path",
        "start",
        "duration",
        "text",
        "note",
    ]
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow(
                {
                    "index": index,
                    "shot_id": row.get("shot_id") or row.get("镜号") or f"S{index:03d}",
                    "material_path": _pick(row, ["copied_material", "path", "clip_path", "selected_clip", "素材文件", "candidate_file", "source_path"]),
                    "start": _pick(row, ["timeline_start", "start_seconds", "开始秒"]),
                    "duration": _pick(row, ["duration", "duration_seconds", "时长"]),
                    "text": row.get("text") or row.get("台词") or row.get("subtitle") or "",
                    "note": row.get("note") or row.get("备注") or "",
                }
            )


def _write_script(path: Path, timeline_csv: Path, package_dir: Path) -> None:
    pycapcut_root = str(_pycapcut_root() or "")
    path.write_text(
        f'''from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

PYCAPCUT_ROOT = Path(r"{pycapcut_root}")
if str(PYCAPCUT_ROOT) and PYCAPCUT_ROOT.exists() and str(PYCAPCUT_ROOT) not in sys.path:
    sys.path.insert(0, str(PYCAPCUT_ROOT))

import pycapcut as cc
from pycapcut import trange


TIMELINE_CSV = Path(r"{timeline_csv}")
PACKAGE_DIR = Path(r"{package_dir}")
DRAFT_ROOT = Path(os.environ.get("CAPCUT_DRAFT_ROOT", PACKAGE_DIR / "CapCut Drafts"))
DRAFT_NAME = os.environ.get("CAPCUT_DRAFT_NAME", PACKAGE_DIR.name)


def seconds(value: str, default: float = 0.0) -> float:
    text = str(value or "").strip().replace("秒", "")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def main() -> None:
    DRAFT_ROOT.mkdir(parents=True, exist_ok=True)
    draft_folder = cc.DraftFolder(str(DRAFT_ROOT))
    script = draft_folder.create_draft(DRAFT_NAME, 1080, 1920, allow_replace=True)
    script.add_track(cc.TrackType.video, track_name="自动剪辑")
    script.add_track(cc.TrackType.text, track_name="字幕")

    cursor = 0.0
    with TIMELINE_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            material = Path(row["material_path"])
            if not material.exists():
                continue
            duration = max(0.2, seconds(row.get("duration"), 3.0))
            video_segment = cc.VideoSegment(str(material), trange(f"{{cursor}}s", f"{{duration}}s"))
            script.add_segment(video_segment, "自动剪辑")
            text = (row.get("text") or "").strip()
            if text:
                text_segment = cc.TextSegment(text, trange(f"{{cursor}}s", f"{{duration}}s"))
                script.add_segment(text_segment, "字幕")
            cursor += duration
    script.save()
    print(f"草稿已生成：{{DRAFT_ROOT / DRAFT_NAME}}")


if __name__ == "__main__":
    main()
''',
        encoding="utf-8",
    )


def _try_create_real_draft(config: ProjectConfig, package_dir: Path, timeline_csv: Path, draft_root: str | Path | None) -> Path | None:
    pycapcut_root = _pycapcut_root()
    if pycapcut_root and str(pycapcut_root) not in sys.path:
        sys.path.insert(0, str(pycapcut_root))
    try:
        import pycapcut as cc  # type: ignore
        from pycapcut import trange  # type: ignore
    except Exception as exc:
        log_line(config, f"pyCapCut 未就绪，仅生成草稿脚本包：{exc}")
        return None

    root = Path(draft_root) if draft_root else package_dir / "CapCut Drafts"
    root.mkdir(parents=True, exist_ok=True)
    draft_name = _safe_name(package_dir.name)
    try:
        draft_folder = cc.DraftFolder(str(root))
        script = draft_folder.create_draft(draft_name, 1080, 1920, allow_replace=True)
        script.add_track(cc.TrackType.video, track_name="自动剪辑")
        script.add_track(cc.TrackType.text, track_name="字幕")

        cursor = 0.0
        for row in _read_csv(timeline_csv):
            material = Path(row.get("material_path", ""))
            if not material.exists():
                continue
            duration = _float(row.get("duration"), 3.0)
            video_segment = cc.VideoSegment(str(material), trange(f"{cursor}s", f"{duration}s"))
            script.add_segment(video_segment, "自动剪辑")
            text = row.get("text", "").strip()
            if text:
                text_segment = cc.TextSegment(text, trange(f"{cursor}s", f"{duration}s"))
                script.add_segment(text_segment, "字幕")
            cursor += duration
        script.save()
        return root / draft_name
    except Exception as exc:
        log_line(config, f"pyCapCut 创建真实草稿失败，仅保留草稿交接包：{exc}")
        return None


def _float(value: Any, default: float) -> float:
    try:
        return float(str(value or "").replace("秒", ""))
    except ValueError:
        return default
