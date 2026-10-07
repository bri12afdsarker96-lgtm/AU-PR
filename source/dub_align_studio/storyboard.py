"""Import a five-column storyboard and match its shots against an existing video library.

This is deliberately local and deterministic.  The JSON plan is an editable hand-off:
changing a shot's ``source`` pins that choice on subsequent runs of the same inputs.
No source video is renamed, moved, or rewritten during planning.
"""

from __future__ import annotations

import hashlib
import csv
import json
import math
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from . import xlsx_reader
from .material_select import VIDEO_EXTENSIONS

PLAN_NAME = "剪辑方案.json"
_HEADERS = ("镜头序号", "口播文稿", "时长", "图片生成提示词", "视频生成提示词")
_EXCLUDED_FOLDERS = {"待人工复核", "非宇宙内容"}
_BOILERPLATE = ("严格使用", "唯一首帧", "视觉基准", "全程", "不要", "水印", "Logo", "字幕")


@dataclass(frozen=True)
class StoryboardShot:
    row: int
    label: str
    narration: str
    duration: float
    image_prompt: str
    video_prompt: str


@dataclass(frozen=True)
class Storyboard:
    path: Path
    sha256: str
    sheet_name: str
    shots: tuple[StoryboardShot, ...]

    @property
    def text(self) -> str:
        return "\n".join(shot.narration for shot in self.shots)


def _seconds(raw: str, row: int) -> float:
    value = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:秒|s|S)?\s*", raw)
    if not value or float(value.group(1)) <= 0:
        raise ValueError(f"第 {row} 行时长无效：{raw!r}（填写正数秒，如 7秒）。")
    return float(value.group(1))


def read_storyboard(source: str | Path) -> Storyboard:
    """Read columns A–E from the first worksheet, retaining Excel row numbers."""
    path = Path(source).resolve()
    data = path.read_bytes()
    try:
        with zipfile.ZipFile(path) as bundle:
            shared = xlsx_reader._shared_strings(bundle)
            root = ET.fromstring(bundle.read(xlsx_reader._first_sheet_path(bundle)))
            try:
                book = ET.fromstring(bundle.read("xl/workbook.xml"))
                sheet = book.find(".//m:sheets/m:sheet", xlsx_reader._NS)
                sheet_name = sheet.get("name", "sheet1") if sheet is not None else "sheet1"
            except (KeyError, ET.ParseError):
                sheet_name = "sheet1"
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise ValueError(f"无法读取分镜表：{path.name}") from exc
    rows: dict[int, dict[str, str]] = {}
    for cell in root.iterfind(".//m:sheetData/m:row/m:c", xlsx_reader._NS):
        match = xlsx_reader._CELL_REF.match(cell.get("r") or "")
        if match and match.group(1) in "ABCDE":
            rows.setdefault(int(match.group(2)), {})[match.group(1)] = xlsx_reader._cell_text(cell, shared)
    if not rows:
        raise ValueError("分镜表没有内容。")
    header_row = min(rows)
    header = tuple(rows[header_row].get(letter, "") for letter in "ABCDE")
    if header != _HEADERS:
        raise ValueError("分镜表列标题不匹配；需要 A 镜头序号、B 口播文稿、C 时长、D 图片生成提示词、E 视频生成提示词。")
    shots = []
    for number in sorted(rows):
        if number <= header_row:
            continue
        cells = rows[number]
        if not any(cells.get(letter) for letter in "ABCDE"):
            continue
        narration = cells.get("B", "").strip()
        if not narration:
            raise ValueError(f"第 {number} 行缺少口播文稿，无法生成配音。")
        shots.append(StoryboardShot(number, cells.get("A", ""), narration,
                                    _seconds(cells.get("C", ""), number),
                                    cells.get("D", ""), cells.get("E", "")))
    if not shots:
        raise ValueError("分镜表没有可用镜头。")
    return Storyboard(path, hashlib.sha256(data).hexdigest(), sheet_name, tuple(shots))


def _terms(value: str) -> set[str]:
    # CJK character n-grams work without third-party segmentation and preserve
    # compound names such as 地球太空 / 黑洞恒星 found in BJ video filenames.
    terms: set[str] = set()
    for part in re.findall(r"[\u3400-\u9fff]+|[a-zA-Z0-9]+", value.lower()):
        if re.fullmatch(r"[\u3400-\u9fff]+", part):
            terms.update(part[i:i + width] for width in (2, 3)
                         for i in range(max(0, len(part) - width + 1)))
        elif len(part) > 2 and not re.fullmatch(r"bj\d+", part):
            terms.add(part)
    return terms


def _query_terms(shot: StoryboardShot) -> dict[str, float]:
    weighted: dict[str, float] = {}
    for value, weight, limit in ((shot.narration, 3.0, 120),
                                 (shot.image_prompt, 1.0, 180),
                                 (shot.video_prompt, 0.5, 180)):
        clean = value[:limit]
        for phrase in _BOILERPLATE:
            clean = clean.replace(phrase, "")
        for term in _terms(clean):
            weighted[term] = weighted.get(term, 0.0) + weight
    return weighted


def _library_files(root: Path) -> tuple[list[Path], str]:
    if not root.is_dir():
        raise FileNotFoundError(f"素材库不存在：{root}")
    files = []
    digest = hashlib.sha256()
    for path in root.rglob("*"):
        if path.suffix.lower() not in VIDEO_EXTENSIONS or not path.is_file():
            continue
        relative = path.relative_to(root)
        if (path.name == "成片.mp4" or any(part in _EXCLUDED_FOLDERS or part.endswith("_segments")
                                             for part in relative.parts[:-1])):
            continue
        files.append(path)
    files.sort(key=lambda p: str(p.relative_to(root)).casefold())
    for path in files:
        stat = path.stat()
        digest.update(f"{path.relative_to(root)}|{stat.st_size}|{stat.st_mtime_ns}\n".encode("utf-8"))
    if not files:
        raise ValueError(f"素材库中没有可用视频：{root}（待人工复核/非宇宙内容默认排除）。")
    return files, digest.hexdigest()


def _rank_shots(board: Storyboard, files: list[Path], root: Path) -> list[dict]:
    descriptions = [str(path.relative_to(root).with_suffix("")) for path in files]
    terms = [_terms(name) for name in descriptions]
    frequency: dict[str, int] = {}
    inverted: dict[str, list[int]] = {}
    for index, item in enumerate(terms):
        for term in item:
            frequency[term] = frequency.get(term, 0) + 1
            inverted.setdefault(term, []).append(index)
    shots = []
    used: set[Path] = set()
    count = len(files)
    for index, shot in enumerate(board.shots):
        scores: dict[int, float] = {}
        for term, weight in _query_terms(shot).items():
            matches = inverted.get(term, ())
            if not matches:
                continue
            idf = math.log1p((count + 1) / (len(matches) + 1))
            for file_index in matches:
                scores[file_index] = scores.get(file_index, 0.0) + weight * idf
        # Keep enough options to make a 9–30-shot workbook without needless
        # reuse of the same source; the UI still shows a bounded list.
        ranked = sorted(scores, key=lambda i: (-scores[i], str(files[i]).casefold()))[:max(5, len(board.shots))]
        if not ranked:
            ranked = [index % count]  # deterministic last resort, explicitly flagged
        candidates = [{"source": str(files[i]), "score": round(scores.get(i, 0.0), 3),
                       "reason": descriptions[i]} for i in ranked]
        # A whole film should not repeatedly cut back to the exact same file
        # when another comparably relevant candidate is available.
        best_score = candidates[0]["score"]
        winner = next((candidate for candidate in candidates
                       if Path(candidate["source"]) not in used
                       and candidate["score"] >= best_score * 0.5), candidates[0])
        used.add(Path(winner["source"]))
        shots.append({"row": shot.row, "label": shot.label, "narration": shot.narration,
                      "target_seconds": shot.duration,
                      "image_prompt": shot.image_prompt, "video_prompt": shot.video_prompt,
                      "source": winner["source"], "auto_source": winner["source"],
                      "source_in_seconds": 0.0, "locked": False, "confidence": "text_only",
                      "review_required": True,
                      "review_reason": "仅完成文件名/目录文本匹配，尚未核验视频画面与运动",
                      "candidates": candidates})
    return shots


def _write_review_report(plan: dict, path: Path) -> None:
    report = path.parent / "选片复核清单.csv"
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["表格行", "镜头", "口播", "目标秒数", "选中素材", "入点秒数", "匹配分数", "人工锁定", "复核原因"])
        for shot in plan["shots"]:
            chosen = next((x for x in shot["candidates"] if x["source"] == shot["source"]), None)
            writer.writerow([shot["row"], shot["label"], shot["narration"], shot["target_seconds"],
                             shot["source"], shot.get("source_in_seconds", 0.0),
                             chosen["score"] if chosen else "人工指定",
                             "是" if shot["locked"] else "否", shot.get("review_reason", "")])


def prepare_plan(board: Storyboard, library: str | Path, output_path: str | Path) -> dict:
    """Return/rebuild a source-fingerprinted plan; preserve user source edits on reuse."""
    root = Path(library).resolve()
    output_path = Path(output_path)
    files, library_sha = _library_files(root)
    old = None
    if output_path.is_file():
        try:
            old = json.loads(output_path.read_text(encoding="utf-8"))
            if (old.get("workbook_sha256") == board.sha256
                    and old.get("library_sha256") == library_sha
                    and old.get("library") == str(root)
                    and len(old.get("shots", ())) == len(board.shots)
                    and all(Path(entry.get("source", "")).is_file() for entry in old["shots"])):
                for entry in old["shots"]:
                    entry["locked"] = (entry["source"] != entry.get("auto_source")
                                       or float(entry.get("source_in_seconds", 0)) != 0)
                output_path.write_text(json.dumps(old, ensure_ascii=False, indent=2), encoding="utf-8")
                _write_review_report(old, output_path)
                return old
        except (OSError, ValueError, TypeError, KeyError):
            old = None
    ranked = _rank_shots(board, files, root)
    if old and old.get("library") == str(root):
        previous = {entry.get("row"): entry for entry in old.get("shots", [])}
        for shot in ranked:
            prior = previous.get(shot["row"])
            if not prior or not (prior.get("source") != prior.get("auto_source")
                                 or float(prior.get("source_in_seconds", 0)) != 0):
                continue
            fields = ("label", "narration", "target_seconds", "image_prompt", "video_prompt")
            source = Path(prior["source"]).resolve()
            if (all(prior.get(field) == shot.get(field) for field in fields)
                    and source.is_relative_to(root) and source.is_file()):
                shot["source"] = str(source)
                shot["source_in_seconds"] = float(prior.get("source_in_seconds", 0))
                shot["locked"] = True
                if all(candidate["source"] != str(source) for candidate in shot["candidates"]):
                    shot["candidates"].append({"source": str(source), "score": 0.0,
                                               "reason": "上次人工指定"})
    plan = {"schema_version": 1, "workbook": str(board.path), "sheet_name": board.sheet_name,
            "workbook_sha256": board.sha256, "library": str(root),
            "library_sha256": library_sha,
            "timing_policy": "配音实测时长优先；表格 C 列为目标时长与复核参考",
            "shots": ranked}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_review_report(plan, output_path)
    return plan


def plan_videos(plan: dict) -> list[Path]:
    root = Path(plan["library"]).resolve()
    videos = []
    for entry in plan["shots"]:
        path = Path(entry["source"]).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() not in VIDEO_EXTENSIONS or not path.is_file():
            raise ValueError(f"第 {entry['row']} 行选片不在素材库中或文件已失效：{path}")
        videos.append(path)
    return videos


def choose_candidate(plan_path: str | Path, row: int, source: str,
                     source_in_seconds: float | None = None) -> dict:
    """Pin one of the prepared candidates for an Excel row; never accept arbitrary paths."""
    path = Path(plan_path)
    plan = json.loads(path.read_text(encoding="utf-8"))
    match = next((shot for shot in plan["shots"] if shot["row"] == row), None)
    if match is None:
        raise ValueError(f"剪辑方案中没有表格第 {row} 行。")
    allowed = {candidate["source"] for candidate in match["candidates"]}
    if source not in allowed or not Path(source).is_file():
        raise ValueError(f"第 {row} 行只能选择剪辑方案中仍然存在的候选视频。")
    if source_in_seconds is None:
        source_in_seconds = match.get("source_in_seconds", 0.0) if source == match["source"] else 0.0
    source_in_seconds = float(source_in_seconds)
    if not math.isfinite(source_in_seconds) or source_in_seconds < 0:
        raise ValueError("视频入点必须是非负秒数。")
    match["source"] = source
    match["source_in_seconds"] = source_in_seconds
    match["locked"] = source != match["auto_source"] or source_in_seconds != 0
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_review_report(plan, path)
    return plan


def dub_signature(board: Storyboard, engine: str, voice_id: str,
                  options: dict, reference_audio: Path | None = None) -> str:
    """Key reuse of master.wav to all synthesis inputs, not merely workbook row count."""
    reference = None
    if reference_audio is not None:
        stat = Path(reference_audio).stat()
        reference = [str(Path(reference_audio).resolve()), stat.st_size, stat.st_mtime_ns]
    payload = {"workbook_sha256": board.sha256, "engine": engine,
               "voice_id": voice_id, "options": options, "reference_audio": reference}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
