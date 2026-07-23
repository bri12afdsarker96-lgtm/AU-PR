"""剪映草稿导出：把「逐行分镜段 + 整轨 master + 字幕」导成剪映/CapCut 草稿。

需求（用户 2026-07-22）：导出剪映草稿；字幕大小自定义（与 subtitles.SubtitleStyle 同源）。

沿用水星 capcut_export 的三层降级模式（pyCapCut 为软依赖，不进安装包）：
    ① 恒产出「草稿交接包」：时间线 CSV + 素材副本 + 字幕 SRT + 可运行脚本 + 使用说明；
    ② 交接包里的 create_capcut_draft.py：装了 pyCapCut 即可在剪辑机上一键生成真草稿；
    ③ 本机 pyCapCut 就绪时（复用内核 capcut_environment 探测），当场直接创建真实草稿。

轨道结构（与 B 方案一致）：
    视频轨 = 逐行分镜段按序排（时长即行时长，帧窗口已收口）；
    音频轨 = 整条 master.wav 一段铺满（音频不切的口径带进草稿）；
    文本轨 = 每行台词按行窗口放置，字号来自 SubtitleStyle。
"""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import settings as studio_settings
from .subtitles import SubtitleEntry, SubtitleStyle, write_srt
from .timing import LineTiming


def _pycapcut_import_ready() -> bool:
    """pyCapCut 可导入性探测（本地实现，避免拖入水星项目管理层依赖）。

    与水星 capcut_export 同口径：优先组件目录 / vendor_tools/pyCapCut，其次 pip 安装。"""
    import importlib.util
    import sys

    candidates = [
        studio_settings.components_root() / "pyCapCut",
        studio_settings.components_root() / "pycapcut",
        Path(__file__).resolve().parents[2] / "vendor_tools" / "pyCapCut",
        Path(sys.executable).resolve().parent / "vendor_tools" / "pyCapCut",
        Path.cwd() / "vendor_tools" / "pyCapCut",
    ]
    for candidate in candidates:
        if candidate.exists() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    try:
        return importlib.util.find_spec("pycapcut") is not None
    except Exception:
        return False


TIMELINE_CSV_NAME = "剪映时间线.csv"
SCRIPT_NAME = "create_capcut_draft.py"
SRT_NAME = "字幕.srt"


@dataclass
class CapcutPackage:
    package_dir: Path
    timeline_csv: Path
    srt_path: Path | None
    script_py: Path
    material_dir: Path
    real_draft_dir: Path | None
    message: str


def draft_font_size(style: SubtitleStyle) -> float:
    """像素字号 → 剪映文本字号的换算基准（剪映字号约 15 对应 1080 宽画布上 ~108px）。

    只是合理初值；交接包脚本顶部有 FONT_SIZE 常量，可在剪辑机上直接微调。
    """
    return round(style.font_size_px * 15.0 / 108.0, 1)


def export_capcut_package(
    timings: list[LineTiming],
    segment_files: list[Path],
    master_wav: Path,
    output_dir: Path,
    style: SubtitleStyle | None = None,
    draft_root: str | Path | None = None,
    create_real_draft: bool = True,
    canvas: tuple[int, int] = (1080, 1920),
) -> CapcutPackage:
    """导出剪映草稿交接包；pyCapCut 就绪时顺带创建真实草稿。"""
    if len(timings) != len(segment_files):
        raise ValueError(f"计时行数({len(timings)})与分镜段数({len(segment_files)})不一致。")
    if not timings:
        raise ValueError("没有可导出的行。")
    master_wav = Path(master_wav)
    if not master_wav.exists():
        raise FileNotFoundError(f"master 音频不存在：{master_wav}")
    style = style or SubtitleStyle()

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    package_dir = Path(output_dir) / f"剪映草稿包_{stamp}"
    material_dir = package_dir / "materials"
    material_dir.mkdir(parents=True, exist_ok=True)

    # 素材副本：逐行分镜段 + 整轨 master（交接包自足，可整体拷去剪辑机）
    copied_segments: list[Path] = []
    for index, segment in enumerate(segment_files, start=1):
        segment = Path(segment)
        if not segment.exists():
            raise FileNotFoundError(f"分镜段不存在：{segment}")
        target = material_dir / f"{index:03d}{segment.suffix}"
        shutil.copy2(segment, target)
        copied_segments.append(target)
    master_copy = material_dir / f"master{master_wav.suffix}"
    shutil.copy2(master_wav, master_copy)

    # 时间线 CSV（行窗口由时长累加；与计时表同口径）
    timeline_csv = package_dir / TIMELINE_CSV_NAME
    entries: list[SubtitleEntry] = []
    cursor = 0.0
    with timeline_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["index", "text", "start", "end", "duration", "material"])
        for timing, material in zip(timings, copied_segments):
            start = cursor
            cursor = round(cursor + timing.duration, 3)
            writer.writerow(
                [timing.index, timing.text, f"{start:.3f}", f"{cursor:.3f}", f"{timing.duration:.3f}", material.name]
            )
            entries.append(SubtitleEntry(index=timing.index, start=start, end=cursor, text=timing.text))

    srt_path = write_srt(package_dir / SRT_NAME, entries) if any(e.text for e in entries) else None

    script_py = package_dir / SCRIPT_NAME
    _write_script(script_py, package_dir, draft_font_size(style), canvas)

    real_draft_dir: Path | None = None
    message = "已生成剪映草稿交接包。"
    if create_real_draft:
        real_draft_dir = _try_real_draft(package_dir, timeline_csv, master_copy, draft_font_size(style), draft_root, canvas)
        message = (
            "已生成剪映草稿交接包，并创建真实剪映草稿。"
            if real_draft_dir
            else "已生成剪映草稿交接包；本机未装 pyCapCut，请在剪辑机上运行 create_capcut_draft.py。"
        )

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "line_count": len(timings),
        "total_seconds": round(sum(t.duration for t in timings), 3),
        "font_size_px": style.font_size_px,
        "canvas": list(canvas),
        "draft_font_size": draft_font_size(style),
        "master": master_copy.name,
        "srt": srt_path.name if srt_path else "",
        "real_draft_dir": str(real_draft_dir or ""),
    }
    (package_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (package_dir / "使用说明.txt").write_text(
        "\n".join(
            [
                "这是配音对齐工作室导出的剪映草稿交接包。",
                "剪映时间线.csv：逐行分镜时间线（与配音计时表同口径）。",
                "materials/：逐行分镜段 + 整轨配音 master（音频整条不切）。",
                "字幕.srt：整片字幕，可在剪映内直接导入（新建文本→导入字幕）。",
                "create_capcut_draft.py：装好 pyCapCut 后运行即可生成真草稿；",
                f"  脚本顶部 FONT_SIZE={draft_font_size(style)} 对应约 {style.font_size_px}px 字号，可直接改。",
                "草稿目录可用环境变量 CAPCUT_DRAFT_ROOT 指定（默认包内 CapCut Drafts）。",
            ]
        ),
        encoding="utf-8",
    )
    return CapcutPackage(package_dir, timeline_csv, srt_path, script_py, material_dir, real_draft_dir, message)


def _write_script(path: Path, package_dir: Path, font_size: float, canvas: tuple[int, int]) -> None:
    path.write_text(
        f'''from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

# 可调项：剪映文本字号（约 15 ≈ 1080 宽画布 108px）与画布尺寸（与成片比例一致）
FONT_SIZE = {font_size}
CANVAS_W, CANVAS_H = {canvas[0]}, {canvas[1]}

PACKAGE_DIR = Path(__file__).resolve().parent
TIMELINE_CSV = PACKAGE_DIR / "{TIMELINE_CSV_NAME}"
MATERIALS = PACKAGE_DIR / "materials"
DRAFT_ROOT = Path(os.environ.get("CAPCUT_DRAFT_ROOT", PACKAGE_DIR / "CapCut Drafts"))
DRAFT_NAME = os.environ.get("CAPCUT_DRAFT_NAME", PACKAGE_DIR.name)

PYCAPCUT_ROOT = os.environ.get("PYCAPCUT_ROOT", "")
if PYCAPCUT_ROOT and PYCAPCUT_ROOT not in sys.path:
    sys.path.insert(0, PYCAPCUT_ROOT)

import pycapcut as cc
from pycapcut import trange


def main() -> None:
    DRAFT_ROOT.mkdir(parents=True, exist_ok=True)
    draft_folder = cc.DraftFolder(str(DRAFT_ROOT))
    script = draft_folder.create_draft(DRAFT_NAME, CANVAS_W, CANVAS_H, allow_replace=True)
    script.add_track(cc.TrackType.video, track_name="分镜")
    script.add_track(cc.TrackType.audio, track_name="配音")
    script.add_track(cc.TrackType.text, track_name="字幕")

    total = 0.0
    rows = []
    with TIMELINE_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows.append(row)
            total = max(total, float(row["end"]))

    for row in rows:
        material = MATERIALS / row["material"]
        if not material.exists():
            continue
        start, duration = float(row["start"]), float(row["duration"])
        script.add_segment(cc.VideoSegment(str(material), trange(f"{{start}}s", f"{{duration}}s")), "分镜")
        text = (row.get("text") or "").strip()
        if text:
            try:
                style = cc.TextStyle(size=FONT_SIZE)
                segment = cc.TextSegment(text, trange(f"{{start}}s", f"{{duration}}s"), style=style)
            except Exception:
                segment = cc.TextSegment(text, trange(f"{{start}}s", f"{{duration}}s"))
            script.add_segment(segment, "字幕")

    master = next(MATERIALS.glob("master.*"), None)
    if master:
        script.add_segment(cc.AudioSegment(str(master), trange("0s", f"{{total}}s")), "配音")

    script.save()
    print(f"草稿已生成：{{DRAFT_ROOT / DRAFT_NAME}}")


if __name__ == "__main__":
    main()
''',
        encoding="utf-8",
    )


def _try_real_draft(
    package_dir: Path,
    timeline_csv: Path,
    master_copy: Path,
    font_size: float,
    draft_root: str | Path | None,
    canvas: tuple[int, int] = (1080, 1920),
) -> Path | None:
    """本机 pyCapCut 就绪时直接创建真实草稿；任何失败都降级为交接包，不抛出。"""
    if not _pycapcut_import_ready():
        return None
    try:
        import pycapcut as cc  # type: ignore
        from pycapcut import trange  # type: ignore

        root = Path(draft_root) if draft_root else package_dir / "CapCut Drafts"
        root.mkdir(parents=True, exist_ok=True)
        draft_folder = cc.DraftFolder(str(root))
        script = draft_folder.create_draft(package_dir.name, canvas[0], canvas[1], allow_replace=True)
        script.add_track(cc.TrackType.video, track_name="分镜")
        script.add_track(cc.TrackType.audio, track_name="配音")
        script.add_track(cc.TrackType.text, track_name="字幕")

        total = 0.0
        with timeline_csv.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        for row in rows:
            total = max(total, float(row["end"]))
            material = package_dir / "materials" / row["material"]
            if not material.exists():
                continue
            start, duration = float(row["start"]), float(row["duration"])
            script.add_segment(cc.VideoSegment(str(material), trange(f"{start}s", f"{duration}s")), "分镜")
            text = (row.get("text") or "").strip()
            if text:
                try:
                    segment = cc.TextSegment(text, trange(f"{start}s", f"{duration}s"), style=cc.TextStyle(size=font_size))
                except Exception:
                    segment = cc.TextSegment(text, trange(f"{start}s", f"{duration}s"))
                script.add_segment(segment, "字幕")
        script.add_segment(cc.AudioSegment(str(master_copy), trange("0s", f"{total}s")), "配音")
        script.save()
        return root / package_dir.name
    except Exception:
        return None
