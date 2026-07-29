from __future__ import annotations

import csv
import json
import math
import re
from .proc import run_silent
from array import array
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from .edit_compose import aspect_canvas, aspect_fit_filter
from .edit_genre import apply_genre
from .edit_library import build_video_filter_chain
from .editing_engine import AutoEditResult, run_auto_edit
from .models import DIR_A, DIR_AUTO_EDIT_INPUT, DIR_AUTO_EDIT_TASKS, DIR_SCRIPT, ProjectConfig, TABLE_EXTENSIONS, TEXT_EXTENSIONS, VIDEO_EXTENSIONS
from .project import iter_media, iter_project_media, log_line, require_files


STRATEGIES = ["爆点融合", "智能粗剪", "字幕关键词", "音量高光", "去静音粗剪", "镜头节奏粗剪", "顺序分镜"]
DEFAULT_KEYWORDS = "反转,高能,秘密,证据,救命,背叛,复仇,真相,打脸,崩溃"
DRAMA_BOOST_WORDS = ["反转", "高能", "秘密", "证据", "救命", "背叛", "复仇", "真相", "打脸", "崩溃", "冲突", "误会", "威胁", "求饶", "哭", "笑"]


@dataclass
class AutoCutSettings:
    strategy: str = "智能粗剪"
    target_seconds: float = 60.0
    clip_seconds: float = 4.0
    silence_db: float = -35.0
    min_silence: float = 0.45
    scene_threshold: float = 0.35
    keywords: str = DEFAULT_KEYWORDS
    # 前置剪辑参数：自动剪辑生成时按用户预设套用同一套剪辑库能力。
    genre: str = ""  # 分类赛道，非空时软件自适应对应画风（见 edit_genre）
    filter_name: str = "原片"
    effect_names: list[str] = field(default_factory=list)
    transition_name: str = "叠化"
    subtitle_style: str = "默认白字黑边"
    aspect_ratio: str = "9:16 竖屏"
    audio_match_mode: str = "裁剪多余画面"  # 裁剪多余画面 / 变速匹配 / 不处理


@dataclass
class AutoCutSegment:
    shot_id: str
    source: str
    start: float
    duration: float
    reason: str
    text: str = ""


@dataclass
class SubtitleCue:
    source: Path | None
    start: float
    end: float
    text: str


@dataclass
class StrategyAutoEditResult:
    edit: AutoEditResult
    plan_csv: Path
    report_json: Path
    strategy: str
    segments: int


@dataclass
class AutoCutRecommendation:
    goal: str
    settings: AutoCutSettings
    reason: str
    github_basis: list[str]
    report_json: Path | None = None


def recommend_auto_cut_settings(config: ProjectConfig, goal: str, save_report: bool = True) -> AutoCutRecommendation:
    text = (goal or "").strip()
    lowered = text.lower()
    strategy = "爆点融合"
    reason = "目标描述较泛，使用爆点融合把字幕、音量、去静音和镜头节奏合并评分。"
    basis = [
        "FireRed-OpenStoryline: 意图到剪辑计划",
        "video-use: 目标式 Agent 动作链",
        "FunClip: ASR 文本切片",
        "AutoClip: 高光提取",
        "Auto-Editor: 去静音粗剪",
    ]

    if _contains_any(lowered, ["台词", "字幕", "关键词", "对白", "反转", "证据", "秘密", "冲突", "剧情", "解说", "文案"]):
        strategy = "爆点融合"
        reason = "目标里有剧情、台词或关键词信号，用字幕命中作为主权重，再叠加音量和镜头信号。"
        basis.append("FunClip: ASR 文本切片")
    if _contains_any(lowered, ["高光", "爆点", "高潮", "吵架", "尖叫", "大声", "哭", "笑", "情绪", "打脸"]):
        strategy = "爆点融合"
        reason = "目标里有高能、情绪或爆点信号，用音量高光作为主权重，再叠加字幕和去静音信号。"
        basis.append("AutoClip: 高光提取")
    if _contains_any(lowered, ["废话", "去静音", "静音", "停顿", "口播", "采访", "课程", "空白"]):
        strategy = "去静音粗剪"
        reason = "目标里有口播停顿或废片段信号，优先剔除静音和空白。"
        basis.append("Auto-Editor: 去静音粗剪")
    if _contains_any(lowered, ["镜头", "节奏", "转场", "混剪", "动作", "快剪", "卡点"]):
        strategy = "镜头节奏粗剪"
        reason = "目标里有镜头节奏或混剪信号，优先按镜头变化拆条。"
        basis.append("PySceneDetect: 镜头切点")
    if _contains_any(lowered, ["顺序", "完整", "从头", "不打乱"]):
        strategy = "顺序分镜"
        reason = "目标要求保持顺序，使用顺序分镜避免打乱剧情。"

    target_seconds = _infer_seconds(text, default=config.edit.target_seconds)
    clip_seconds = config.edit.clip_seconds
    if strategy in {"音量高光", "镜头节奏粗剪"}:
        clip_seconds = min(clip_seconds, 3.5)
    if strategy == "爆点融合":
        clip_seconds = max(3.0, min(clip_seconds, 5.0))
    if strategy == "字幕关键词":
        clip_seconds = max(clip_seconds, 4.0)

    keywords = _keywords_from_goal(text) or DEFAULT_KEYWORDS
    settings = AutoCutSettings(
        strategy=strategy,
        target_seconds=target_seconds,
        clip_seconds=clip_seconds,
        silence_db=-35.0,
        min_silence=0.45,
        scene_threshold=0.35,
        keywords=keywords,
    )
    report_json: Path | None = None
    if save_report:
        task_dir = config.root / DIR_AUTO_EDIT_TASKS
        task_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_json = task_dir / f"{stamp}_AI推荐剪辑策略.json"
        report_json.write_text(
            json.dumps(
                {
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "goal": text,
                    "settings": asdict(settings),
                    "reason": reason,
                    "github_basis": basis,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        log_line(config, f"AI 推荐剪辑策略：{strategy}，目标 {target_seconds:.0f} 秒")
    return AutoCutRecommendation(text, settings, reason, basis, report_json)


def compose_auto_edit_filter(settings: AutoCutSettings) -> str:
    """把前置剪辑参数（画面比例 + 滤镜 + 特效）编译成自动剪辑渲染用的 -vf 链。

    这是"自动剪辑真实调用新剪辑功能"的打通点：用户在自动剪辑前选的滤镜/特效/画面比例，
    经由此函数注入 run_auto_edit 的预览渲染，最终成片按预设呈现。
    """
    parts: list[str] = []
    width, height = aspect_canvas(settings.aspect_ratio)
    if settings.aspect_ratio != "原始":
        parts.append(aspect_fit_filter(width, height, "contain"))
    look = build_video_filter_chain(settings.filter_name, settings.effect_names)
    if look:
        parts.append(look)
    return ",".join(parts)


def run_strategy_auto_edit(
    config: ProjectConfig,
    settings: AutoCutSettings,
    input_dir: str | Path | None = None,
    make_preview: bool = True,
    progress: "Callable[[int, int], None] | None" = None,
) -> StrategyAutoEditResult:
    if settings.strategy not in STRATEGIES:
        raise ValueError(f"未知自动剪辑策略：{settings.strategy}")

    # 先选赛道 → 软件自适应画风：把赛道预设铺到剪辑参数。
    if settings.genre:
        apply_genre(settings, settings.genre)

    sources = _sources(config, input_dir)
    requested_strategy = settings.strategy
    effective_strategy = requested_strategy
    fallback_reason = ""
    segments = build_segments(config, sources, settings)
    if not segments and requested_strategy != "顺序分镜":
        fallback_settings = AutoCutSettings(
            strategy="顺序分镜",
            target_seconds=settings.target_seconds,
            clip_seconds=settings.clip_seconds,
            silence_db=settings.silence_db,
            min_silence=settings.min_silence,
            scene_threshold=settings.scene_threshold,
            keywords=settings.keywords,
        )
        segments = _sequential_segments(config, sources, fallback_settings)
        effective_strategy = "顺序分镜"
        fallback_reason = f"{requested_strategy} 没有生成可用分镜，已自动回退到顺序分镜。"
    segments = require_files("自动剪辑计划", segments)
    task_dir = config.root / DIR_AUTO_EDIT_TASKS
    task_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    plan_csv = task_dir / f"{stamp}_{effective_strategy}_剪辑计划.csv"
    write_strategy_plan(segments, plan_csv)

    result = run_auto_edit(
        config,
        input_dir=input_dir,
        script_path=plan_csv,
        target_seconds=settings.target_seconds,
        clip_seconds=settings.clip_seconds,
        output_name=f"{effective_strategy}_{stamp}",
        make_preview=make_preview,
        video_filter=compose_auto_edit_filter(settings),
        progress=progress,
    )
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "requested_strategy": requested_strategy,
        "strategy": effective_strategy,
        "fallback_reason": fallback_reason,
        "scene_engine": _scene_report_info(config, sources, requested_strategy)[0],
        "scene_detail": _scene_report_info(config, sources, requested_strategy)[1],
        "settings": asdict(settings),
        "source_count": len(sources),
        "segment_count": len(segments),
        "plan_csv": str(plan_csv),
        "auto_edit_dir": str(result.work_dir),
        "preview_path": str(result.preview_path or ""),
        "segments": [asdict(segment) for segment in segments],
    }
    report_json = result.work_dir / "自动剪辑策略报告.json"
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if fallback_reason:
        log_line(config, fallback_reason)
    log_line(config, f"策略自动剪辑完成：{effective_strategy}，{len(segments)} 个分镜。")
    return StrategyAutoEditResult(result, plan_csv, report_json, effective_strategy, len(segments))


def build_segments(config: ProjectConfig, sources: list[Path], settings: AutoCutSettings) -> list[AutoCutSegment]:
    if settings.strategy == "顺序分镜":
        return _sequential_segments(config, sources, settings)
    if settings.strategy == "字幕关键词":
        return _keyword_segments(config, sources, settings)
    if settings.strategy == "音量高光":
        return _audio_highlight_segments(config, sources, settings)
    if settings.strategy == "去静音粗剪":
        return _silence_segments(config, sources, settings)
    if settings.strategy == "镜头节奏粗剪":
        return _scene_segments(config, sources, settings)
    if settings.strategy in {"爆点融合", "智能粗剪"}:
        fused = _fusion_segments(config, sources, settings)
        if fused:
            return fused

    keywords = _keyword_segments(config, sources, settings)
    if keywords:
        return keywords
    highlights = _audio_highlight_segments(config, sources, settings)
    if len(highlights) >= 2:
        return highlights
    silence = _silence_segments(config, sources, settings)
    if len(silence) >= 2 or _total_duration(silence) >= min(settings.target_seconds, settings.clip_seconds):
        return silence
    scenes = _scene_segments(config, sources, settings)
    if len(scenes) >= 2:
        return scenes
    return _sequential_segments(config, sources, settings)


def _fusion_segments(config: ProjectConfig, sources: list[Path], settings: AutoCutSettings) -> list[AutoCutSegment]:
    pool_settings = AutoCutSettings(
        strategy=settings.strategy,
        target_seconds=max(settings.target_seconds * 1.8, settings.clip_seconds * 8),
        clip_seconds=settings.clip_seconds,
        silence_db=settings.silence_db,
        min_silence=settings.min_silence,
        scene_threshold=settings.scene_threshold,
        keywords=settings.keywords,
    )
    raw_groups = [
        ("字幕命中", 105.0, _keyword_segments(config, sources, pool_settings)),
        ("音量高光", 78.0, _audio_highlight_segments(config, sources, pool_settings)),
        ("非静音段", 50.0, _silence_segments(config, sources, pool_settings)),
        ("镜头切点", 42.0, _scene_segments(config, sources, pool_settings)),
    ]
    weighted: list[tuple[float, AutoCutSegment]] = []
    all_segments = [segment for _name, _base, segments in raw_groups for segment in segments]
    for group_name, base_score, segments in raw_groups:
        for segment in segments:
            score = base_score
            text = segment.text or segment.reason
            score += _drama_text_score(text)
            if group_name != "字幕命中" and _overlaps_any(segment, raw_groups[0][2]):
                score += 28.0
            if group_name != "音量高光" and _overlaps_any(segment, raw_groups[1][2]):
                score += 18.0
            if group_name != "非静音段" and _overlaps_any(segment, raw_groups[2][2]):
                score += 8.0
            if group_name != "镜头切点" and _overlaps_any(segment, raw_groups[3][2]):
                score += 6.0
            if len(all_segments) > 0:
                score += max(0.0, 5.0 - abs(segment.duration - settings.clip_seconds))
            reason = f"爆点融合：{group_name} | {segment.reason}"
            weighted.append((score, AutoCutSegment(segment.shot_id, segment.source, segment.start, segment.duration, reason, segment.text)))

    if not weighted:
        return []

    selected: list[tuple[float, AutoCutSegment]] = []
    total = 0.0
    for score, segment in sorted(weighted, key=lambda item: item[0], reverse=True):
        if total >= settings.target_seconds:
            break
        if _is_duplicate_segment(segment, [old for _old_score, old in selected], min_gap=max(0.75, settings.clip_seconds * 0.75)):
            continue
        seconds = min(segment.duration, settings.target_seconds - total)
        if seconds < 0.35:
            continue
        selected_segment = AutoCutSegment(
            f"S{len(selected) + 1:03d}",
            segment.source,
            max(0.0, segment.start),
            seconds,
            f"{segment.reason} | 评分 {score:.1f}",
            segment.text,
        )
        selected.append((score, selected_segment))
        total += seconds

    if not selected:
        return []
    ordered = sorted((segment for _score, segment in selected), key=lambda item: (item.source, item.start))
    for index, segment in enumerate(ordered, start=1):
        segment.shot_id = f"S{index:03d}"
    return ordered


def write_strategy_plan(segments: list[AutoCutSegment], path: Path) -> Path:
    fieldnames = ["镜号", "素材文件", "开始秒", "时长", "人物", "声音类型", "台词", "备注"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for segment in segments:
            writer.writerow(
                {
                    "镜号": segment.shot_id,
                    "素材文件": segment.source,
                    "开始秒": f"{segment.start:.3f}",
                    "时长": f"{segment.duration:.3f}",
                    "人物": "",
                    "声音类型": "原声/待配音",
                    "台词": segment.text,
                    "备注": segment.reason,
                }
            )
    return path


def _sources(config: ProjectConfig, input_dir: str | Path | None) -> list[Path]:
    if input_dir:
        return require_files("自动剪辑输入", iter_media(Path(input_dir), VIDEO_EXTENSIONS))
    sources = iter_project_media(config, DIR_AUTO_EDIT_INPUT, VIDEO_EXTENSIONS)
    if not sources:
        sources = iter_project_media(config, DIR_A, VIDEO_EXTENSIONS)
    return require_files("自动剪辑输入", sources)


def _duration(config: ProjectConfig, path: Path) -> float | None:
    ffprobe = config.tools.ffprobe or "ffprobe"
    completed = run_silent(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return None
    try:
        return float(completed.stdout.strip())
    except ValueError:
        return None


def _sequential_segments(config: ProjectConfig, sources: list[Path], settings: AutoCutSettings) -> list[AutoCutSegment]:
    segments: list[AutoCutSegment] = []
    total = 0.0
    index = 1
    for source in sources:
        duration = _duration(config, source) or settings.clip_seconds
        start = 0.0
        while start < duration and total < settings.target_seconds:
            seconds = min(settings.clip_seconds, duration - start, settings.target_seconds - total)
            if seconds <= 0.2:
                break
            segments.append(AutoCutSegment(f"S{index:03d}", str(source), start, seconds, "顺序分镜"))
            index += 1
            total += seconds
            start += settings.clip_seconds
        if total >= settings.target_seconds:
            break
    return segments


def _silence_segments(config: ProjectConfig, sources: list[Path], settings: AutoCutSettings) -> list[AutoCutSegment]:
    segments: list[AutoCutSegment] = []
    total = 0.0
    index = 1
    for source in sources:
        duration = _duration(config, source)
        if not duration:
            continue
        silences = _detect_silences(config, source, settings.silence_db, settings.min_silence)
        keep_ranges = _invert_silences(duration, silences, pad=0.08)
        for start, end in keep_ranges:
            cursor = start
            while cursor < end and total < settings.target_seconds:
                seconds = min(settings.clip_seconds, end - cursor, settings.target_seconds - total)
                if seconds >= 0.35:
                    segments.append(AutoCutSegment(f"S{index:03d}", str(source), cursor, seconds, "去静音保留段"))
                    index += 1
                    total += seconds
                cursor += settings.clip_seconds
            if total >= settings.target_seconds:
                break
        if total >= settings.target_seconds:
            break
    return segments


def _scene_segments(config: ProjectConfig, sources: list[Path], settings: AutoCutSettings) -> list[AutoCutSegment]:
    segments: list[AutoCutSegment] = []
    total = 0.0
    index = 1
    for source in sources:
        duration = _duration(config, source)
        if not duration:
            continue
        scene_result = _scene_result(config, source, settings)
        ranges = [(scene.start_seconds, scene.end_seconds) for scene in scene_result.scenes] if scene_result else []
        if not ranges:
            points = _detect_scene_points(config, source, settings.scene_threshold)
            ranges = list(zip(sorted({0.0, *points, duration}), sorted({0.0, *points, duration})[1:])) if points else []
        for start, end in ranges:
            seconds = min(end - start, settings.clip_seconds, settings.target_seconds - total)
            if seconds >= 0.35:
                engine_note = f"镜头切点分镜/{scene_result.engine}" if scene_result else "镜头切点分镜"
                segments.append(AutoCutSegment(f"S{index:03d}", str(source), start, seconds, engine_note))
                index += 1
                total += seconds
            if total >= settings.target_seconds:
                break
        if total >= settings.target_seconds:
            break
    return segments


def _scene_result(config: ProjectConfig, source: Path, settings: AutoCutSettings):
    from .scene_detect import detect_scenes, latest_scene_result

    existing = latest_scene_result(config, source)
    if existing:
        return existing
    return detect_scenes(config, source, threshold=settings.scene_threshold)


def _scene_report_info(config: ProjectConfig, sources: list[Path], requested_strategy: str) -> tuple[str, str]:
    if requested_strategy not in {"镜头节奏粗剪", "爆点融合", "智能粗剪"}:
        return "not_used", ""
    from .scene_detect import latest_scene_result

    for source in sources:
        result = latest_scene_result(config, source)
        if result:
            detail = result.engine_detail
            if result.fallback_reason:
                detail = f"{detail}；{result.fallback_reason}" if detail else result.fallback_reason
            return result.engine, detail
    return "not_used", ""


def _audio_highlight_segments(config: ProjectConfig, sources: list[Path], settings: AutoCutSettings) -> list[AutoCutSegment]:
    candidates: list[tuple[float, Path, float, float]] = []
    window = max(0.8, settings.clip_seconds)
    for source in sources:
        duration = _duration(config, source)
        if not duration:
            continue
        samples = _audio_rms_samples(config, source)
        buckets: dict[int, list[float]] = {}
        for seconds, rms in samples:
            if rms <= -90:
                continue
            buckets.setdefault(int(seconds // window), []).append(rms)
        for bucket, values in buckets.items():
            start = min(bucket * window, max(0.0, duration - 0.35))
            clip_duration = min(window, duration - start)
            if clip_duration >= 0.35:
                score = sum(values) / len(values)
                candidates.append((score, source, start, clip_duration))

    selected: list[tuple[Path, float, float, float]] = []
    total = 0.0
    for score, source, start, seconds in sorted(candidates, key=lambda item: item[0], reverse=True):
        if total >= settings.target_seconds:
            break
        if any(source == old_source and abs(start - old_start) < settings.clip_seconds for old_source, old_start, _old_seconds, _old_score in selected):
            continue
        clip_duration = min(seconds, settings.target_seconds - total)
        selected.append((source, start, clip_duration, score))
        total += clip_duration

    segments: list[AutoCutSegment] = []
    for index, (source, start, seconds, score) in enumerate(sorted(selected, key=lambda item: (str(item[0]), item[1])), start=1):
        segments.append(AutoCutSegment(f"S{index:03d}", str(source), start, seconds, f"音量高光 RMS {score:.1f} dB"))
    return segments


def _keyword_segments(config: ProjectConfig, sources: list[Path], settings: AutoCutSettings) -> list[AutoCutSegment]:
    keywords = _keywords(settings.keywords)
    if not keywords:
        return []
    transcript_files = iter_project_media(config, DIR_SCRIPT, TABLE_EXTENSIONS | TEXT_EXTENSIONS | {".vtt"})
    cues: list[SubtitleCue] = []
    for path in transcript_files:
        cues.extend(_load_cues(path, sources))
    if not cues:
        return []

    segments: list[AutoCutSegment] = []
    total = 0.0
    index = 1
    for cue in cues:
        hit = next((keyword for keyword in keywords if keyword.lower() in cue.text.lower()), "")
        if not hit:
            continue
        source = cue.source or (sources[0] if len(sources) == 1 else None)
        if not source:
            continue
        source_duration = _duration(config, source) or max(cue.end, cue.start + settings.clip_seconds)
        start = max(0.0, cue.start - 0.6)
        desired = max(settings.clip_seconds, cue.end - cue.start + 1.2)
        seconds = min(desired, source_duration - start, settings.target_seconds - total)
        if seconds < 0.35:
            continue
        segments.append(AutoCutSegment(f"S{index:03d}", str(source), start, seconds, f"字幕关键词：{hit}", cue.text))
        index += 1
        total += seconds
        if total >= settings.target_seconds:
            break
    return segments


def _audio_rms_samples(config: ProjectConfig, source: Path) -> list[tuple[float, float]]:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    completed = run_silent(
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            "8000",
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
    )
    if completed.returncode != 0 or not completed.stdout:
        return []
    pcm = array("h")
    pcm.frombytes(completed.stdout)
    sample_rate = 8000
    window = sample_rate // 4
    samples: list[tuple[float, float]] = []
    for start in range(0, len(pcm), window):
        chunk = pcm[start : start + window]
        if not chunk:
            continue
        mean_square = sum(sample * sample for sample in chunk) / len(chunk)
        if mean_square <= 0:
            rms_db = -96.0
        else:
            rms_db = 20 * math.log10(math.sqrt(mean_square) / 32768.0)
        samples.append((start / sample_rate, rms_db))
    return samples


def _keywords(text: str) -> list[str]:
    raw = text or DEFAULT_KEYWORDS
    return [item.strip() for item in re.split(r"[,，、\s]+", raw) if item.strip()]


def _drama_text_score(text: str) -> float:
    value = text or ""
    score = 0.0
    for word in DRAMA_BOOST_WORDS:
        if word in value:
            score += 7.0
    if "?" in value or "？" in value or "!" in value or "！" in value:
        score += 4.0
    return min(score, 35.0)


def _overlaps_any(segment: AutoCutSegment, others: list[AutoCutSegment]) -> bool:
    return any(_overlap_seconds(segment, other) >= 0.25 for other in others)


def _overlap_seconds(left: AutoCutSegment, right: AutoCutSegment) -> float:
    if left.source != right.source:
        return 0.0
    left_end = left.start + left.duration
    right_end = right.start + right.duration
    return max(0.0, min(left_end, right_end) - max(left.start, right.start))


def _is_duplicate_segment(segment: AutoCutSegment, selected: list[AutoCutSegment], min_gap: float) -> bool:
    for old in selected:
        if segment.source != old.source:
            continue
        if _overlap_seconds(segment, old) >= 0.25:
            return True
        if abs(segment.start - old.start) < min_gap:
            return True
    return False


def _contains_any(text: str, words: list[str]) -> bool:
    return any(word.lower() in text for word in words)


def _infer_seconds(text: str, default: float) -> float:
    minute_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:分钟|分)", text)
    if minute_match:
        return max(5.0, float(minute_match.group(1)) * 60)
    second_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:秒|s)", text, flags=re.IGNORECASE)
    if second_match:
        return max(5.0, float(second_match.group(1)))
    return default


def _keywords_from_goal(text: str) -> str:
    presets = ["反转", "高能", "秘密", "证据", "救命", "背叛", "复仇", "真相", "打脸", "崩溃", "冲突", "误会", "求饶", "威胁"]
    hits = [word for word in presets if word in text]
    quoted = re.findall(r"[\"“']([^\"”']{1,12})[\"”']", text)
    words = []
    for word in [*hits, *quoted]:
        if word not in words:
            words.append(word)
    return ",".join(words)


def _load_cues(path: Path, sources: list[Path]) -> list[SubtitleCue]:
    suffix = path.suffix.lower()
    if suffix in {".srt", ".vtt"}:
        return _load_srt_cues(path, sources)
    if suffix in {".csv", ".txt"}:
        return _load_csv_or_text_cues(path, sources)
    return []


def _load_srt_cues(path: Path, sources: list[Path]) -> list[SubtitleCue]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    source = _cue_source(path, sources)
    cues: list[SubtitleCue] = []
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        time_line = next((line for line in lines if "-->" in line), "")
        if not time_line:
            continue
        left, right = [part.strip() for part in time_line.split("-->", 1)]
        start = _parse_timecode(left)
        end = _parse_timecode(right.split()[0])
        body = " ".join(line for line in lines if "-->" not in line and not line.isdigit())
        if end > start and body:
            cues.append(SubtitleCue(source, start, end, body))
    return cues


def _load_csv_or_text_cues(path: Path, sources: list[Path]) -> list[SubtitleCue]:
    if path.suffix.lower() == ".txt":
        return []
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        rows = [dict(row) for row in reader]

    cues: list[SubtitleCue] = []
    fallback_source = _cue_source(path, sources)
    for row in rows:
        text = _pick(row, ["text", "字幕", "台词", "文案", "旁白", "content"])
        start_text = _pick(row, ["start", "开始秒", "开始", "start_time", "起点"])
        end_text = _pick(row, ["end", "结束秒", "结束", "end_time"])
        duration_text = _pick(row, ["duration", "时长", "秒数"])
        if not text or not start_text:
            continue
        start = _parse_time_or_float(start_text)
        end = _parse_time_or_float(end_text) if end_text else start + _parse_time_or_float(duration_text, 0.0)
        source_token = _pick(row, ["source", "素材文件", "视频", "视频文件", "file"])
        source = _match_source(source_token, sources) or fallback_source
        if end > start:
            cues.append(SubtitleCue(source, start, end, text))
    return cues


def _cue_source(path: Path, sources: list[Path]) -> Path | None:
    return _match_source(path.stem, sources) or (sources[0] if len(sources) == 1 else None)


def _match_source(token: str, sources: list[Path]) -> Path | None:
    normalized = token.strip().lower()
    if not normalized:
        return None
    for source in sources:
        if normalized in {source.stem.lower(), source.name.lower()}:
            return source
    for source in sources:
        if normalized in source.stem.lower() or source.stem.lower() in normalized:
            return source
    return None


def _pick(row: dict[str, str], names: list[str], default: str = "") -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def _parse_time_or_float(text: str, default: float = 0.0) -> float:
    stripped = str(text).strip()
    if not stripped:
        return default
    if ":" in stripped:
        return _parse_timecode(stripped)
    try:
        return float(stripped.replace("秒", ""))
    except ValueError:
        return default


def _parse_timecode(text: str) -> float:
    cleaned = text.strip().replace(",", ".")
    parts = cleaned.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def _detect_silences(config: ProjectConfig, source: Path, silence_db: float, min_silence: float) -> list[tuple[float, float]]:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    completed = run_silent(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(source),
            "-af",
            f"silencedetect=noise={silence_db}dB:d={min_silence}",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    text = completed.stderr + "\n" + completed.stdout
    starts = [float(item) for item in re.findall(r"silence_start:\s*([0-9.]+)", text)]
    ends = [float(item) for item in re.findall(r"silence_end:\s*([0-9.]+)", text)]
    return list(zip(starts, ends))


def _detect_scene_points(config: ProjectConfig, source: Path, threshold: float) -> list[float]:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    completed = run_silent(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(source),
            "-vf",
            f"select='gt(scene,{threshold})',showinfo",
            "-an",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    text = completed.stderr + "\n" + completed.stdout
    points = [float(item) for item in re.findall(r"pts_time:([0-9.]+)", text)]
    return [point for point in points if point > 0.2]


def _invert_silences(duration: float, silences: list[tuple[float, float]], pad: float) -> list[tuple[float, float]]:
    if not silences:
        return [(0.0, duration)]
    ranges: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in silences:
        keep_start = cursor
        keep_end = max(cursor, start + pad)
        if keep_end - keep_start >= 0.35:
            ranges.append((keep_start, keep_end))
        cursor = max(cursor, end - pad)
    if duration - cursor >= 0.35:
        ranges.append((cursor, duration))
    return ranges


def _total_duration(segments: list[AutoCutSegment]) -> float:
    return sum(segment.duration for segment in segments)
