"""素材调度：按「文件夹顺序」或「关键字匹配」为每行文案选一个视频（2026-07-24 需求）。

三种素材模式（UI「素材模式」下拉）：
    flat          平铺顺序（现状）：分镜目录下 1.mp4、2.mp4…，第 i 行用第 i 个；
    folder_order  文件夹顺序（需求2）：第 i 行用第 i 个子文件夹，行多夹少从头循环；
    keyword       关键字匹配（需求1）：文件夹名↔行文案打分，最高分的文件夹供片；
                  无匹配按文件夹顺序兜底（颗粒度已与用户对齐，2026-07-24）。

夹内选片：按 Seed 确定性随机（同 Seed 结果可复现；换 Seed 同文案换一套画面——
矩阵多版本天然去重）；夹内不重复取，用完循环。

选片结果写 选片清单.csv（行/文件夹/文件/得分/备注）：重渲染时复用同一份选片，
画面不乱跳；删除该文件即重新选片。纯标准库、全确定性、可单测。
"""

from __future__ import annotations

import csv
import random
import re
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
MATERIAL_MODES = ["flat", "folder_order", "keyword"]
MATCH_THRESHOLD = 0.34          # 关键字得分低于此视为无匹配 → 兜底
SELECTION_CSV = "选片清单.csv"


@dataclass(frozen=True)
class SelectedShot:
    index: int          # 行号（1 起）
    line: str           # 该行文案
    folder: str         # 供片文件夹名（flat 模式为空）
    file: Path          # 选中的视频
    score: float        # 关键字得分（非 keyword 模式为 0）
    note: str           # 「匹配…」/「循环复用」/「⚠ 无匹配，按顺序兜底」等


def _natural_key(name: str):
    """自然排序：01夹、2夹、10夹 按数值序而非字典序。"""
    return [(0, int(t)) if t.isdigit() else (1, t) for t in re.split(r"(\d+)", name)]


def list_material_folders(root: Path) -> list[Path]:
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"分镜目录不存在：{root}")
    return sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: _natural_key(p.name))


def folder_videos(folder: Path) -> list[Path]:
    return sorted((p for p in Path(folder).iterdir()
                   if p.suffix.lower() in VIDEO_EXTENSIONS and p.is_file()),
                  key=lambda p: _natural_key(p.name))


def clean_folder_name(name: str) -> str:
    """去掉排序前缀（数字/分隔符），留下描述本体：「01健身房撸铁」→「健身房撸铁」。"""
    return re.sub(r"^[\d\s._\-—、·）)（(]+", "", name).strip()


def score_folder_name(name: str, line: str) -> float:
    """文件夹名 ↔ 行文案 打分（0~1，确定性）。

    整名连续出现在行内 = 1.0（最强信号）；否则按名字的字符二元组在行内的命中率
    打分（封顶 0.9，保证弱于整名命中）。名字过短/为空 → 0。
    """
    n = clean_folder_name(name)
    if not n:
        return 0.0
    if n in line:
        return 1.0
    if len(n) < 2:
        return 0.0
    grams = {n[i:i + 2] for i in range(len(n) - 1)}
    hits = sum(1 for g in grams if g in line)
    return round(hits / len(grams) * 0.9, 3)


class _FolderPicker:
    """夹内选片：按 (Seed, 夹名) 洗牌成队列，依次弹出——不重复取，用完循环（确定性）。"""

    def __init__(self, seed: int):
        self.seed = seed
        self._queues: dict[str, list[Path]] = {}

    def pick(self, folder: Path) -> Path:
        key = str(folder)
        queue = self._queues.get(key)
        if not queue:
            videos = folder_videos(folder)
            if not videos:
                raise ValueError(f"素材文件夹里没有视频：{folder}（支持 {'/'.join(sorted(VIDEO_EXTENSIONS))}）")
            queue = list(videos)
            random.Random(f"{self.seed}|{folder.name}").shuffle(queue)
            self._queues[key] = queue
        return queue.pop(0)


def select_videos(root: Path, lines: list[str], mode: str, seed: int = 42) -> list[SelectedShot]:
    """为每行选一个视频。mode ∈ folder_order / keyword（flat 走旧的平铺逻辑，不进这里）。"""
    if mode not in ("folder_order", "keyword"):
        raise KeyError(f"未知素材模式：{mode}（可选：{'、'.join(MATERIAL_MODES)}）")
    if not lines:
        raise ValueError("没有脚本行。")
    folders = list_material_folders(Path(root))
    if not folders:
        raise ValueError(
            f"分镜目录下没有子文件夹：{root}。「文件夹顺序/关键字匹配」模式需要按内容分好的子文件夹；"
            "平铺的 1.mp4、2.mp4… 请把素材模式切回「平铺顺序」。")
    picker = _FolderPicker(seed)
    shots: list[SelectedShot] = []
    for i, line in enumerate(lines, start=1):
        score = 0.0
        if mode == "keyword":
            scored = [(score_folder_name(f.name, line), f) for f in folders]
            best_score, best = max(scored, key=lambda x: x[0])   # 平分取排序靠前的夹
            if best_score >= MATCH_THRESHOLD:
                folder, score = best, best_score
                note = f"匹配「{clean_folder_name(best.name)}」{best_score:.2f}"
            else:
                folder = folders[(i - 1) % len(folders)]
                note = "⚠ 无匹配，按文件夹顺序兜底"
        else:
            folder = folders[(i - 1) % len(folders)]
            note = "循环复用" if i > len(folders) else ""
        file = picker.pick(folder)
        shots.append(SelectedShot(index=i, line=line, folder=folder.name,
                                  file=file, score=score, note=note))
    return shots


# ------------------------------------------------------------------ 选片清单（重渲染复用）
def write_selection(output_dir: Path, shots: list[SelectedShot]) -> Path:
    path = Path(output_dir) / SELECTION_CSV
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["index", "line", "folder", "file", "score", "note"])
        for s in shots:
            writer.writerow([s.index, s.line, s.folder, str(s.file), f"{s.score:.3f}", s.note])
    return path


def read_selection(output_dir: Path, expect_lines: int) -> list[Path] | None:
    """读回既有选片：行数一致且文件都在才复用；否则返回 None（触发重新选片）。"""
    path = Path(output_dir) / SELECTION_CSV
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except Exception:
        return None
    files = [Path(str(row.get("file") or "")) for row in rows]
    if len(files) != expect_lines or not all(f.is_file() for f in files):
        return None
    return files
