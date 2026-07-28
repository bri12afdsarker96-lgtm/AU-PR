from __future__ import annotations

import random
from .proc import run_silent
import csv
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .edit_compose import enforce_distinct_intro
from .models import DIR_REVIEW, ProjectConfig
from .project import background_videos, log_line, require_files, source_videos, stickers
from .video_dedup import (
    DedupVariant,
    compare_rework_similarity,
    foreground_filter_suffix,
    make_dedup_variant,
    select_high_similarity_sources,
    variant_manifest,
    video_eq_filter,
    write_dedup_report,
    write_rework_comparison_report,
)


@dataclass
class HighSimilarityRegenerateResult:
    source_report: Path
    output_dir: Path
    rendered: list[Path]
    summary_json: Path
    summary_md: Path
    high_pair_count: int
    high_video_count: int
    missing_sources: list[str]
    comparison_json: Path | None = None
    comparison_md: Path | None = None
    comparison_conclusion: str = ""


def _run(command: list[str]) -> None:
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr[-2000:] if completed.stderr else completed.stdout[-2000:]
        raise RuntimeError(detail.strip() or "ffmpeg 执行失败")


def _probe_duration(config: ProjectConfig, path: Path) -> float | None:
    ffprobe = config.tools.ffprobe or "ffprobe"
    try:
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
    except (OSError, ValueError):
        # ffprobe 路径不可执行/不存在等 → 视为探测失败，交由上层降级，不崩渲染。
        return None
    if completed.returncode != 0:
        return None
    try:
        return max(0.0, float(completed.stdout.strip()))
    except ValueError:
        return None


def _probe_video_size(config: ProjectConfig, path: Path) -> tuple[int, int] | None:
    ffprobe = config.tools.ffprobe or "ffprobe"
    try:
        completed = run_silent(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, ValueError):
        return None
    if completed.returncode != 0:
        return None
    try:
        width_text, height_text = completed.stdout.strip().split("x", 1)
        width = int(width_text)
        height = int(height_text)
    except (ValueError, TypeError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _has_audio(config: ProjectConfig, path: Path) -> bool:
    ffprobe = config.tools.ffprobe or "ffprobe"
    try:
        completed = run_silent(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, ValueError):
        # ffprobe 不可用时保守假设“无音轨”，避免崩渲染（上层据此跳过变速音频处理）。
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _source_input_args(config: ProjectConfig, source: Path, variant: DedupVariant) -> list[str]:
    args: list[str] = []
    intro = max(0.0, float(variant.intro_trim or 0.0))
    outro = max(0.0, float(variant.outro_trim or 0.0))
    duration = _probe_duration(config, source) if (intro > 0 or outro > 0) else None
    if intro > 0:
        args += ["-ss", f"{intro:.3f}"]
    if duration is not None and duration > intro + outro + 0.5:
        args += ["-t", f"{max(0.5, duration - intro - outro):.3f}"]
    args += ["-i", str(source)]
    return args


def _even(value: int) -> int:
    return max(2, value - (value % 2))


def _zoom_multiplier(variant: DedupVariant) -> float:
    return max(1.0, float(variant.zoom_amount or 0.0))


def _is_dynamic_motion(variant: DedupVariant) -> bool:
    return variant.zoom_path in {"in", "out"} and _zoom_multiplier(variant) > 1.0001


def _is_static_zoom(variant: DedupVariant) -> bool:
    return variant.zoom_path not in {"none", "in", "out"} and _zoom_multiplier(variant) > 1.0001


def _variant_foreground_scale(variant: DedupVariant) -> float:
    scale = variant.foreground_scale
    if _is_static_zoom(variant):
        scale *= _zoom_multiplier(variant)
    if variant.border_style == "blur_extend":
        scale *= max(0.82, 1.0 - (variant.border_width_ratio * 2.0))
    return scale


def _degrade_motion_variant(config: ProjectConfig, source: Path, variant: DedupVariant, reason: str) -> None:
    if not _is_dynamic_motion(variant):
        return
    variant.zoom_path = f"{variant.zoom_path}_static"
    variant.pan_direction = "none"
    variant.pan_amount = 0.0
    log_line(config, f"动态运动路径降级为 {variant.zoom_path}：{source.name}，{reason}")


def _fit_decrease_dimensions(source_size: tuple[int, int], max_width: int, max_height: int) -> tuple[int, int]:
    source_width, source_height = source_size
    ratio = min(max_width / max(1, source_width), max_height / max(1, source_height))
    return (
        _even(max(2, int(source_width * ratio))),
        _even(max(2, int(source_height * ratio))),
    )


def _ff_min(left: str, right: str) -> str:
    return f"min({left}\\,{right})"


def _ff_max(left: str, right: str) -> str:
    return f"max({left}\\,{right})"


def _ff_clamp(value: str, low: str, high: str) -> str:
    return _ff_max(low, _ff_min(high, value))


def _motion_foreground_filter(
    config: ProjectConfig,
    source: Path,
    source_label: str,
    target_width: int,
    target_height: int,
    variant: DedupVariant,
) -> str | None:
    if not _is_dynamic_motion(variant):
        return None
    duration = _probe_duration(config, source)
    if duration is None:
        _degrade_motion_variant(config, source, variant, "无法读取有效时长")
        return None
    effective_duration = duration - max(0.0, float(variant.intro_trim or 0.0)) - max(0.0, float(variant.outro_trim or 0.0))
    if effective_duration <= 0:
        _degrade_motion_variant(config, source, variant, f"裁剪后有效时长无效：{effective_duration:.3f}s")
        return None

    source_size = _probe_video_size(config, source)
    if source_size is None:
        _degrade_motion_variant(config, source, variant, "无法读取视频分辨率")
        return None
    fitted_width, fitted_height = _fit_decrease_dimensions(source_size, target_width, target_height)
    zoom = _zoom_multiplier(variant)
    scaled_width = _even(max(fitted_width + 2, int(fitted_width * zoom + 1)))
    scaled_height = _even(max(fitted_height + 2, int(fitted_height * zoom + 1)))
    if scaled_width <= fitted_width or scaled_height <= fitted_height:
        _degrade_motion_variant(config, source, variant, "动态放大量不足")
        return None

    progress = _ff_min(_ff_max(f"t/{effective_duration:.6f}", "0"), "1")
    zoom_progress = progress if variant.zoom_path == "in" else f"1-({progress})"
    dynamic_width = f"trunc(({fitted_width}+({scaled_width}-{fitted_width})*({zoom_progress}))/2)*2"
    dynamic_height = f"trunc(({fitted_height}+({scaled_height}-{fitted_height})*({zoom_progress}))/2)*2"
    pan_direction = str(variant.pan_direction or "none")
    pan_amount = max(0.0, min(0.05, float(variant.pan_amount or 0.0)))
    x_shift = 0.0
    y_shift = 0.0
    if pan_direction == "right":
        x_shift = scaled_width * pan_amount
    elif pan_direction == "left":
        x_shift = -scaled_width * pan_amount
    elif pan_direction == "down":
        y_shift = scaled_height * pan_amount
    elif pan_direction == "up":
        y_shift = -scaled_height * pan_amount
    x_expr = _ff_clamp(f"(iw-ow)/2+({x_shift:.3f})*({progress})", "0", "iw-ow")
    y_expr = _ff_clamp(f"(ih-oh)/2+({y_shift:.3f})*({progress})", "0", "ih-oh")
    fg_suffix = foreground_filter_suffix(variant)
    return (
        f"[{source_label}]scale=w={dynamic_width}:h={dynamic_height}:eval=frame,"
        f"crop={fitted_width}:{fitted_height}:x={x_expr}:y={y_expr}:exact=0,"
        f"scale={fitted_width}:{fitted_height},setsar=1,{fg_suffix}[fg]"
    )


def _foreground_filter(
    config: ProjectConfig,
    source: Path,
    source_label: str,
    target_width: int,
    target_height: int,
    variant: DedupVariant,
) -> str:
    motion_filter = _motion_foreground_filter(config, source, source_label, target_width, target_height, variant)
    if motion_filter:
        return motion_filter
    fg_suffix = foreground_filter_suffix(variant)
    return (
        f"[{source_label}]scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
        f"setsar=1,{fg_suffix}[fg]"
    )


def _finish_video_filters(
    filter_parts: list[str],
    last: str,
    output_width: int,
    output_height: int,
    variant: DedupVariant,
) -> None:
    current = last
    border_px = int(output_width * max(0.0, variant.border_width_ratio))
    if variant.border_style == "solid" and border_px > 0:
        inner_width = _even(max(320, output_width - border_px * 2))
        inner_height = _even(max(320, output_height - border_px * 2))
        filter_parts.append(
            f"[{current}]scale={inner_width}:{inner_height}:force_original_aspect_ratio=decrease,"
            f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2:color={variant.border_color},setsar=1[framed]"
        )
        current = "framed"
    speed = max(0.5, min(2.0, float(variant.speed_factor or 1.0)))
    if abs(speed - 1.0) > 0.001:
        filter_parts.append(f"[{current}]setpts=PTS/{speed:.4f}[outv]")
    else:
        filter_parts.append(f"[{current}]null[outv]")


def _audio_speed_args(config: ProjectConfig, source: Path, variant: DedupVariant) -> list[str]:
    speed = max(0.5, min(2.0, float(variant.speed_factor or 1.0)))
    if abs(speed - 1.0) <= 0.001 or not _has_audio(config, source):
        return []
    return ["-filter:a", f"atempo={speed:.4f}"]


def build_vertical_command(
    config: ProjectConfig,
    source: Path,
    background: Path | None,
    sticker: Path | None,
    output: Path,
    variant: DedupVariant | None = None,
) -> list[str]:
    recipe = config.recipe
    variant = variant or make_dedup_variant(recipe.dedup_level, f"{source}|{output}")
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    inputs = _source_input_args(config, source, variant)
    if background:
        inputs += ["-stream_loop", "-1", "-i", str(background)]
    sticker_index = None
    if sticker:
        sticker_index = 2 if background else 1
        inputs += ["-loop", "1", "-i", str(sticker)]

    filter_parts: list[str] = []
    if background:
        filter_parts.append(
            f"[1:v]scale={recipe.output_width}:{recipe.output_height}:force_original_aspect_ratio=increase,"
            f"crop={recipe.output_width}:{recipe.output_height},gblur=sigma={variant.background_blur},"
            f"{video_eq_filter(variant)},setsar=1[bg]"
        )
        source_label = "0:v"
    else:
        filter_parts = ["[0:v]split=2[srcbg][srcfg]"]
        filter_parts.append(
            f"[srcbg]scale={recipe.output_width}:{recipe.output_height}:force_original_aspect_ratio=increase,"
            f"crop={recipe.output_width}:{recipe.output_height},gblur=sigma={variant.background_blur},"
            f"{video_eq_filter(variant)},setsar=1[bg]"
        )
        source_label = "srcfg"

    foreground_scale = _variant_foreground_scale(variant)
    foreground_width = _even(max(320, int(recipe.output_width * foreground_scale)))
    foreground_height = _even(max(320, int(recipe.output_height * foreground_scale)))
    filter_parts.append(_foreground_filter(config, source, source_label, foreground_width, foreground_height, variant))
    filter_parts.append(f"[bg][fg]overlay=(W-w)/2+{variant.x_shift}:(H-h)/2+{variant.y_shift}:shortest=1[mix]")
    last = "mix"

    if sticker:
        filter_parts.append(
            f"[{sticker_index}:v]scale=iw*{recipe.c_scale}:ih*{recipe.c_scale},"
            f"format=rgba,colorchannelmixer=aa={recipe.c_opacity}[st]"
        )
        filter_parts.append(f"[{last}][st]overlay=(W-w)/2:(H-h)/2:shortest=1[stickered]")
        last = "stickered"
    _finish_video_filters(filter_parts, last, recipe.output_width, recipe.output_height, variant)

    return [
        ffmpeg,
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[outv]",
        "-map",
        "0:a?",
        *_audio_speed_args(config, source, variant),
        "-c:v",
        "libx264",
        "-preset",
        recipe.preset,
        "-crf",
        str(recipe.crf),
        "-c:a",
        "aac",
        "-shortest",
        str(output),
    ]


def build_horizontal_command(
    config: ProjectConfig,
    source: Path,
    background: Path | None,
    output: Path,
    variant: DedupVariant | None = None,
) -> list[str]:
    recipe = config.recipe
    variant = variant or make_dedup_variant(recipe.dedup_level, f"{source}|{output}")
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    bg = background or source
    width = 1920
    height = 1080
    blur = max(1.0, variant.background_blur)
    fg_width = _even(max(640, int(width * _variant_foreground_scale(variant))))
    filter_parts = [
        (
            f"[1:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma={blur},{video_eq_filter(variant)}[bg]"
        ),
        _foreground_filter(config, source, "0:v", fg_width, height, variant),
        f"[bg][fg]overlay=(W-w)/2+{variant.x_shift}:(H-h)/2+{variant.y_shift}:shortest=1[mix]",
    ]
    _finish_video_filters(filter_parts, "mix", width, height, variant)
    return [
        ffmpeg,
        "-y",
        *_source_input_args(config, source, variant),
        "-stream_loop",
        "-1",
        "-i",
        str(bg),
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[outv]",
        "-map",
        "0:a?",
        *_audio_speed_args(config, source, variant),
        "-c:v",
        "libx264",
        "-preset",
        recipe.preset,
        "-crf",
        str(recipe.crf),
        "-c:a",
        "aac",
        "-shortest",
        str(output),
    ]


def render_project(
    config: ProjectConfig,
    copies: int | None = None,
    source_files: list[Path] | None = None,
    source_label: str = "A 原始视频",
    output_dir: Path | None = None,
    account_slots: list[str] | None = None,
    resume_state_path: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[Path]:
    if not config.tools.ffmpeg:
        raise FileNotFoundError("没有找到 ffmpeg。请在 project.json 里配置 tools.ffmpeg。")

    sources = require_files(source_label, source_files if source_files is not None else source_videos(config))
    b_files = background_videos(config)
    c_files = stickers(config)
    copies_per_source = copies or config.recipe.copies_per_source
    # 进度总数 = 源数 × 每源份数（每渲染完一份成片汇报一次）
    _prog_total = max(1, len(sources) * copies_per_source)
    _prog_done = 0
    if progress:
        progress(0, _prog_total)
    batch_id = datetime.now().strftime("batch_%Y%m%d_%H%M%S")
    output_dir = output_dir or (config.root / DIR_REVIEW / batch_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    # opt-in 断点续跑：给定状态文件时，已完成且成片仍在的份跳过重渲染并复用其清单行。
    # 默认 None → 整段逻辑不触发，与冻结基线逐字节一致。
    resume_state = None
    if resume_state_path is not None:
        from .batch_queue import BatchState

        resume_state = BatchState.load(resume_state_path)

    rendered: list[Path] = []
    rows: list[dict[str, str]] = []
    source_copy_offsets: dict[str, int] = {}
    for source in sources:
        source_key = str(source.resolve()).lower() if source.exists() else str(source).lower()
        base_copy_index = source_copy_offsets.get(source_key, 0)
        for copy_index in range(1, copies_per_source + 1):
            copy_slot = base_copy_index + copy_index
            variant = make_dedup_variant(config.recipe.dedup_level, f"{batch_id}|{source}|{copy_slot}")
            if getattr(config.recipe, "force_distinct_intro", False):
                # opt-in：保证同源每份首帧微裁量互不相同（只增不减，默认关时不触发）。
                variant.intro_trim = enforce_distinct_intro(
                    variant.intro_trim, copy_index - 1, copies_per_source
                )
            background = random.choice(b_files) if b_files else None
            sticker = random.choice(c_files) if c_files else None
            # 续跑用确定性文件名（重跑时能命中既有成片）；非续跑保持唯一命名（基线不变）。
            if resume_state is not None:
                output = output_dir / f"{source.stem}_二创{copy_slot:02d}.mp4"
            else:
                output = _unique_output_path(output_dir, source, copy_slot)
            task_id = f"{source}|{copy_slot}"
            if resume_state is not None and resume_state.is_done(task_id) and output.exists():
                try:
                    cached_row = json.loads(resume_state.done[task_id])
                    rendered.append(output)
                    rows.append(cached_row)
                    log_line(config, f"断点续跑跳过：{output.name}")
                    _prog_done += 1
                    if progress:
                        progress(_prog_done, _prog_total)
                    continue
                except Exception:
                    pass  # 缓存行损坏则照常重渲染
            dynamic_attempted = _is_dynamic_motion(variant)
            if config.recipe.mode == "horizontal":
                command = build_horizontal_command(config, source, background, output, variant)
            else:
                command = build_vertical_command(config, source, background, sticker, output, variant)
            log_line(config, f"开始生成：{output.name}")
            try:
                _run(command)
            except Exception as exc:
                if not (dynamic_attempted and _is_dynamic_motion(variant)):
                    raise
                _degrade_motion_variant(config, source, variant, f"动态渲染失败：{str(exc)[-500:]}")
                if output.exists():
                    output.unlink()
                if config.recipe.mode == "horizontal":
                    command = build_horizontal_command(config, source, background, output, variant)
                else:
                    command = build_vertical_command(config, source, background, sticker, output, variant)
                _run(command)
            log_line(config, f"生成完成：{output.name}")
            rendered.append(output)
            row = {
                    "batch_id": output_dir.name,
                    "mode": config.recipe.mode,
                    "source": str(source),
                    "background": str(background or ""),
                    "sticker": str(sticker or ""),
                    "output": str(output),
                    "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            if account_slots is not None:
                row["account_slot"] = account_slots[len(rows)] if len(rows) < len(account_slots) else f"A{len(rows) + 1:02d}"
            row.update(variant_manifest(variant))
            rows.append(row)
            if resume_state is not None:
                resume_state.mark_done(task_id, json.dumps(row, ensure_ascii=False))
            _prog_done += 1
            if progress:
                progress(_prog_done, _prog_total)
        source_copy_offsets[source_key] = base_copy_index + copies_per_source
    _write_render_manifest(output_dir, rows)
    if rendered and config.recipe.make_dedup_report:
        try:
            report = write_dedup_report(config, rendered, output_dir, config.recipe.dedup_similarity_threshold)
            log_line(
                config,
                f"去重指纹报告完成：对比 {report.pair_count} 组，高相似 {report.high_similarity_count} 组。{report.markdown_path}",
            )
        except Exception as exc:
            log_line(config, f"去重指纹报告生成失败：{exc}")
    return rendered


def render_high_similarity_replacements(
    config: ProjectConfig,
    report_path: str | Path | None = None,
    dedup_level: str = "strong",
    progress: Callable[[int, int], None] | None = None,
) -> HighSimilarityRegenerateResult:
    selection = select_high_similarity_sources(config, report_path)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = config.root / DIR_REVIEW / f"batch_{stamp}_高相似重做"

    if not selection.high_pairs:
        raise RuntimeError("最新去重指纹报告没有 high 相似组。")
    if not selection.source_files:
        raise FileNotFoundError("没有找到可重新生成的原始素材。")
    selection.source_files = _cap_rework_sources(selection.source_files, max_copies_per_source=6)

    previous_level = config.recipe.dedup_level
    previous_mode = config.recipe.mode
    config.recipe.dedup_level = dedup_level
    if selection.mode in {"vertical", "horizontal"}:
        config.recipe.mode = selection.mode
    try:
        rendered = render_project(
            config,
            copies=1,
            source_files=selection.source_files,
            source_label="高相似成片原始素材",
            output_dir=output_dir,
            progress=progress,
        )
    finally:
        config.recipe.dedup_level = previous_level
        config.recipe.mode = previous_mode

    summary_json, summary_md = _write_high_similarity_regenerate_summary(
        output_dir=output_dir,
        selection=selection,
        rendered=rendered,
        dedup_level=dedup_level,
    )
    comparison_json = None
    comparison_md = None
    comparison_conclusion = ""
    try:
        comparison = compare_rework_similarity(
            config,
            old_report_path=selection.report_path,
            rework_batch_dir=output_dir,
            threshold=config.recipe.dedup_similarity_threshold,
        )
        comparison_json, comparison_md = write_rework_comparison_report(comparison, output_dir)
        comparison_conclusion = comparison.conclusion
        log_line(config, f"高相似重做对比完成：{comparison.conclusion}")
    except Exception as exc:
        log_line(config, f"高相似重做对比生成失败：{exc}")
    log_line(config, f"高相似重做完成：{len(rendered)} 条，输出目录：{output_dir}")
    return HighSimilarityRegenerateResult(
        source_report=selection.report_path,
        output_dir=output_dir,
        rendered=rendered,
        summary_json=summary_json,
        summary_md=summary_md,
        high_pair_count=len(selection.high_pairs),
        high_video_count=len(selection.high_outputs),
        missing_sources=selection.missing_sources,
        comparison_json=comparison_json,
        comparison_md=comparison_md,
        comparison_conclusion=comparison_conclusion,
    )


def _cap_rework_sources(sources: list[Path], max_copies_per_source: int) -> list[Path]:
    counts: dict[str, int] = {}
    result: list[Path] = []
    for source in sources:
        key = str(source.resolve()).lower() if source.exists() else str(source).lower()
        if counts.get(key, 0) >= max_copies_per_source:
            continue
        counts[key] = counts.get(key, 0) + 1
        result.append(source)
    return result


def _write_render_manifest(output_dir: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    csv_path = output_dir / "二创生成清单.csv"
    json_path = output_dir / "render_manifest.json"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


def _unique_output_path(output_dir: Path, source: Path, copy_index: int) -> Path:
    base = output_dir / f"{source.stem}_二创{copy_index:02d}.mp4"
    if not base.exists():
        return base
    stamp = datetime.now().strftime("%H%M%S")
    for suffix in range(1, 1000):
        candidate = output_dir / f"{source.stem}_二创{copy_index:02d}_{stamp}_{suffix:02d}.mp4"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"无法生成唯一输出文件名：{source.name}")


def _write_high_similarity_regenerate_summary(
    output_dir: Path,
    selection,
    rendered: list[Path],
    dedup_level: str,
) -> tuple[Path, Path]:
    json_path = output_dir / "高相似重做报告.json"
    markdown_path = output_dir / "高相似重做报告.md"
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_report": str(selection.report_path),
        "source_batch": str(selection.batch_dir),
        "render_manifest": str(selection.manifest_path or ""),
        "dedup_level": dedup_level,
        "high_pair_count": len(selection.high_pairs),
        "high_video_count": len(selection.high_outputs),
        "high_outputs": [str(path) for path in selection.high_outputs],
        "source_files": [str(path) for path in selection.source_files],
        "rendered": [str(path) for path in rendered],
        "missing_sources": selection.missing_sources,
        "pairs": [asdict(pair) for pair in selection.high_pairs],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_high_similarity_regenerate_markdown(payload), encoding="utf-8")
    return json_path, markdown_path


def _high_similarity_regenerate_markdown(payload: dict) -> str:
    lines = [
        "# 高相似重做报告",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 原指纹报告：`{payload['source_report']}`",
        f"- 原批次：`{payload['source_batch']}`",
        f"- 去重强度：{payload['dedup_level']}",
        f"- 高相似组：{payload['high_pair_count']}",
        f"- 涉及成片：{payload['high_video_count']}",
        f"- 新生成：{len(payload['rendered'])}",
        "",
        "## 新成片",
        "",
    ]
    for video in payload["rendered"]:
        lines.append(f"- `{video}`")
    if payload["missing_sources"]:
        lines.extend(["", "## 路径兜底", ""])
        for item in payload["missing_sources"]:
            lines.append(f"- {item}")
    lines.extend(["", "## 高相似组", "", "| 相似度 | 视频 A | 视频 B |", "| --- | --- | --- |"])
    for pair in payload["pairs"]:
        lines.append(f"| {pair['similarity']:.4f} | {Path(pair['left']).name} | {Path(pair['right']).name} |")
    lines.append("")
    return "\n".join(lines)
