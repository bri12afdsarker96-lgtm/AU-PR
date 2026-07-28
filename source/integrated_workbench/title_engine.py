from __future__ import annotations

import csv
import json
import shutil
import subprocess
from .proc import run_silent
from dataclasses import dataclass
from pathlib import Path

from .models import DIR_COPYWRITING, DIR_COVER, DIR_TITLE_TABLE, ProjectConfig
from .project import log_line, ready_videos

try:
    from PIL import Image, ImageStat
except Exception:  # pragma: no cover - packaged release includes Pillow
    Image = ImageStat = None


TITLE_TEMPLATES = [
    "{drama} 第{episode}集：这一幕太解气了",
    "{drama} 第{episode}集：反转来得太突然",
    "{drama} 第{episode}集：她终于不忍了",
    "{drama} 第{episode}集：真相藏不住了",
    "{drama} 第{episode}集：这段一定要看到最后",
]

TITLE_STYLES = [
    ("冲突钩子", "{drama} 第{episode}集：她终于拿出关键证据", 96, "人物+证据+冲突，适合短剧开场。"),
    ("反转钩子", "{drama} 第{episode}集：谁也没想到真相会这样反转", 93, "强调反转，适合二创高光。"),
    ("情绪钩子", "{drama} 第{episode}集：忍了这么久，她终于爆发", 91, "情绪强，适合女性向剧情。"),
    ("悬念钩子", "{drama} 第{episode}集：看到最后才知道谁在说谎", 89, "保留悬念，适合完播。"),
    ("爽点钩子", "{drama} 第{episode}集：这一段真的太解气了", 87, "直给爽点，适合发布标题。"),
]

COPY_TEMPLATES = [
    "高能片段来了，前面越憋屈，后面越解气。",
    "这段反转很适合连看，结尾还有一层真相。",
    "别只看开头，真正的爽点在后面。",
]


@dataclass
class CoverCandidate:
    video_path: Path
    image_path: Path
    second: float
    score: float
    brightness: float
    contrast: float
    selected: bool = False


def guess_episode(path: Path, fallback: int) -> int:
    digits = "".join(ch for ch in path.stem if ch.isdigit())
    if digits:
        try:
            return int(digits[-3:])
        except ValueError:
            return fallback
    return fallback


def generate_title_candidates(config: ProjectConfig, per_video: int = 5) -> Path:
    videos = ready_videos(config)
    output = config.root / DIR_TITLE_TABLE / "标题候选.csv"
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = ["video_path", "video_name", "episode", "candidate_no", "style", "title", "score", "reason", "selected"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index, video in enumerate(videos, start=1):
            episode = guess_episode(video, index)
            styles = TITLE_STYLES[: max(1, min(per_video, len(TITLE_STYLES)))]
            for candidate_no, (style, template, base_score, reason) in enumerate(styles, start=1):
                writer.writerow(
                    {
                        "video_path": str(video),
                        "video_name": video.name,
                        "episode": episode,
                        "candidate_no": candidate_no,
                        "style": style,
                        "title": template.format(drama=config.drama_name or config.project_name, episode=episode),
                        "score": base_score - candidate_no,
                        "reason": reason,
                        "selected": "1" if candidate_no == 1 else "0",
                    }
                )
    log_line(config, f"标题候选表已生成：{len(videos)} 个视频。")
    return output


def generate_titles(config: ProjectConfig) -> Path:
    videos = ready_videos(config)
    candidate_path = config.root / DIR_TITLE_TABLE / "标题候选.csv"
    if not candidate_path.exists():
        candidate_path = generate_title_candidates(config)
    candidates = _load_selected_title_candidates(candidate_path)
    output = config.root / DIR_TITLE_TABLE / "titles.csv"
    output.parent.mkdir(parents=True, exist_ok=True)

    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["video_path", "video_name", "episode", "title"])
        writer.writeheader()
        for index, video in enumerate(videos, start=1):
            episode = guess_episode(video, index)
            template = TITLE_TEMPLATES[(index - 1) % len(TITLE_TEMPLATES)]
            title = candidates.get(str(video)) or template.format(drama=config.drama_name or config.project_name, episode=episode)
            writer.writerow(
                {
                    "video_path": str(video),
                    "video_name": video.name,
                    "episode": episode,
                    "title": title,
                }
            )
    log_line(config, f"标题表已生成：{len(videos)} 条。")
    return output


def _load_selected_title_candidates(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    rows_by_video: dict[str, list[dict[str, str]]] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            rows_by_video.setdefault(row.get("video_path", ""), []).append(row)
    for video_path, rows in rows_by_video.items():
        selected = next((row for row in rows if str(row.get("selected", "")).strip() in {"1", "是", "true", "True"}), None)
        if not selected:
            selected = max(rows, key=lambda row: _float(row.get("score"), 0.0)) if rows else None
        if selected and selected.get("title"):
            result[video_path] = selected["title"]
    return result


def generate_copywriting(config: ProjectConfig) -> Path:
    videos = ready_videos(config)
    title_map = load_title_map(config)
    output = config.root / DIR_COPYWRITING / "发布文案.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["video_path", "title", "description", "tags"])
        writer.writeheader()
        for index, video in enumerate(videos, start=1):
            title = title_map.get(str(video), f"{config.drama_name or config.project_name} 精彩片段")
            writer.writerow(
                {
                    "video_path": str(video),
                    "title": title,
                    "description": COPY_TEMPLATES[(index - 1) % len(COPY_TEMPLATES)],
                    "tags": "#短剧 #剧情 #反转",
                }
            )
    log_line(config, f"发布文案已生成：{len(videos)} 条。")
    return output


def generate_covers(config: ProjectConfig, seconds: float = 0.2) -> list[Path]:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    output_dir = config.root / DIR_COVER
    output_dir.mkdir(parents=True, exist_ok=True)
    covers: list[Path] = []
    all_candidates: list[CoverCandidate] = []
    for video in ready_videos(config):
        output = output_dir / f"{video.stem}.jpg"
        if output.exists():
            covers.append(output)
            continue
        candidates = _extract_cover_candidates(config, video, seconds)
        all_candidates.extend(candidates)
        selected = _select_cover_candidate(candidates)
        if selected:
            selected.selected = True
            shutil.copy2(selected.image_path, output)
            covers.append(output)
        else:
            completed = _extract_cover(ffmpeg, video, output, 0.0)
            if completed.returncode == 0 and output.exists():
                covers.append(output)
            else:
                detail = completed.stderr[-800:] if completed.stderr else completed.stdout[-800:]
                log_line(config, f"封面抽帧失败：{video.name} {detail.strip()}")
    if all_candidates:
        _write_cover_candidate_report(config, all_candidates)
    log_line(config, f"封面图已生成/确认：{len(covers)} 张。")
    return covers


def _extract_cover_candidates(config: ProjectConfig, video: Path, fallback_second: float) -> list[CoverCandidate]:
    duration = _probe_duration(config, video)
    seconds = [fallback_second]
    if duration > 1:
        seconds.extend([duration * 0.33, duration * 0.55, duration * 0.75])
    seconds = sorted({round(max(0.0, min(second, max(duration - 0.1, 0.0))), 2) for second in seconds})
    candidate_dir = config.root / DIR_COVER / "候选帧" / video.stem
    candidate_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    candidates: list[CoverCandidate] = []
    for index, second in enumerate(seconds, start=1):
        output = candidate_dir / f"{index:02d}_{second:.2f}s.jpg"
        completed = _extract_cover(ffmpeg, video, output, second)
        if completed.returncode != 0 or not output.exists():
            continue
        brightness, contrast = _image_stats(output)
        score = _cover_score(brightness, contrast, index)
        candidates.append(CoverCandidate(video, output, second, score, brightness, contrast))
    return candidates


def _select_cover_candidate(candidates: list[CoverCandidate]) -> CoverCandidate | None:
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.score)


def _write_cover_candidate_report(config: ProjectConfig, candidates: list[CoverCandidate]) -> Path:
    output = config.root / DIR_COVER / "封面候选.csv"
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        fieldnames = ["video_path", "candidate_image", "second", "score", "brightness", "contrast", "selected"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for item in candidates:
            writer.writerow(
                {
                    "video_path": str(item.video_path),
                    "candidate_image": str(item.image_path),
                    "second": f"{item.second:.2f}",
                    "score": f"{item.score:.2f}",
                    "brightness": f"{item.brightness:.2f}",
                    "contrast": f"{item.contrast:.2f}",
                    "selected": "1" if item.selected else "0",
                }
            )
    payload = [
        {
            "video_path": str(item.video_path),
            "candidate_image": str(item.image_path),
            "second": item.second,
            "score": item.score,
            "brightness": item.brightness,
            "contrast": item.contrast,
            "selected": item.selected,
        }
        for item in candidates
    ]
    output.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def _extract_cover(ffmpeg: str, video: Path, output: Path, seconds: float) -> subprocess.CompletedProcess[str]:
    command = [
        ffmpeg,
        "-y",
        "-ss",
        f"{seconds:.2f}",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(output),
    ]
    return run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")


def _probe_duration(config: ProjectConfig, video: Path) -> float:
    ffprobe = config.tools.ffprobe or "ffprobe"
    completed = run_silent(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return 0.0
    return _float(completed.stdout.strip(), 0.0)


def _image_stats(path: Path) -> tuple[float, float]:
    if Image is None or ImageStat is None:
        return 128.0, 32.0
    try:
        with Image.open(path).convert("L") as image:
            stat = ImageStat.Stat(image)
            return float(stat.mean[0]), float(stat.stddev[0])
    except Exception:
        return 128.0, 32.0


def _cover_score(brightness: float, contrast: float, order: int) -> float:
    brightness_balance = max(0.0, 60.0 - abs(brightness - 132.0) * 0.55)
    contrast_score = min(45.0, contrast * 1.2)
    return brightness_balance + contrast_score - order * 0.6


def _float(value: object, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        text = str(value).strip()
        if not text:
            return default
        return float(text)
    except ValueError:
        return default


def generate_title_package(config: ProjectConfig, make_covers: bool = True) -> dict[str, object]:
    candidates = config.root / DIR_TITLE_TABLE / "标题候选.csv"
    if not candidates.exists():
        candidates = generate_title_candidates(config)
    titles = generate_titles(config)
    copywriting = generate_copywriting(config)
    covers = generate_covers(config) if make_covers else []
    cover_candidates = config.root / DIR_COVER / "封面候选.csv"
    return {"titles": titles, "title_candidates": candidates, "copywriting": copywriting, "covers": covers, "cover_candidates": cover_candidates if cover_candidates.exists() else ""}


def load_title_map(config: ProjectConfig) -> dict[str, str]:
    path = config.root / DIR_TITLE_TABLE / "titles.csv"
    if not path.exists():
        generate_titles(config)

    result: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            result[row["video_path"]] = row["title"]
    return result
