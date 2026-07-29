from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapters import export_editor_handoff, export_publish_handoff
from .auto_cut import AutoCutSettings, run_strategy_auto_edit
from .editing_engine import run_auto_edit
from .models import DIR_A, DIR_AUTO_EDIT_INPUT, ProjectConfig, VIDEO_EXTENSIONS
from .project import iter_media, iter_project_media, log_line, ready_videos, save_config
from .publish_queue import build_queue
from .production_state import promote_videos_to_ready
from .title_engine import generate_title_package
from .video_engine import render_project


def _blocking_errors(config: ProjectConfig, skip_render: bool, with_edit: bool = False) -> list[str]:
    errors: list[str] = []
    edit_inputs = iter_project_media(config, DIR_AUTO_EDIT_INPUT, VIDEO_EXTENSIONS)
    if not config.tools.ffmpeg:
        errors.append("未找到 FFmpeg，请检查随包视频处理引擎。")
    if not config.tools.ffprobe:
        errors.append("未找到 FFprobe，请检查随包视频探测引擎。")
    if skip_render and ready_videos(config):
        return []
    if with_edit:
        if not edit_inputs:
            errors.append("自动剪辑输入目录没有视频，请先导入剪辑输入素材。")
    elif not iter_project_media(config, DIR_A, VIDEO_EXTENSIONS):
        errors.append("原始视频目录没有视频，请先导入原片。")
    return errors


def run_full_pipeline(
    config: ProjectConfig,
    copies: int | None = None,
    mode: str | None = None,
    skip_render: bool = False,
    reuse_videos: bool = False,
    with_edit: bool = False,
    edit_target_seconds: float | None = None,
    edit_clip_seconds: float | None = None,
    edit_strategy: str = "",
    silence_db: float = -35.0,
    min_silence: float = 0.45,
    scene_threshold: float = 0.35,
    keywords: str = "",
) -> dict[str, Any]:
    if mode:
        config.recipe.mode = mode
        save_config(config)

    errors = _blocking_errors(config, skip_render=skip_render, with_edit=with_edit or bool(edit_strategy))
    if errors:
        raise RuntimeError("项目未准备好：\n" + "\n".join(errors))

    log_line(config, "开始一键流水线")
    edit_result = None
    render_sources: list[Path] | None = None
    source_label = "A 原始视频"
    if with_edit:
        if edit_strategy:
            strategy_result = run_strategy_auto_edit(
                config,
                AutoCutSettings(
                    strategy=edit_strategy,
                    target_seconds=edit_target_seconds or config.edit.target_seconds,
                    clip_seconds=edit_clip_seconds or config.edit.clip_seconds,
                    silence_db=silence_db,
                    min_silence=min_silence,
                    scene_threshold=scene_threshold,
                    keywords=keywords,
                ),
            )
            edit_result = strategy_result.edit
        else:
            strategy_result = None
            edit_result = run_auto_edit(
                config,
                target_seconds=edit_target_seconds,
                clip_seconds=edit_clip_seconds,
                output_name="一键自动剪辑",
            )
        render_sources = iter_media(edit_result.selected_dir, VIDEO_EXTENSIONS)
        source_label = "自动剪辑分镜"
    else:
        strategy_result = None

    rendered: list[Path] = []
    approved: list[Path] = []
    if not skip_render:
        rendered = render_project(config, copies=copies, source_files=render_sources, source_label=source_label)
        approved = promote_videos_to_ready(config, rendered)

    title_package = generate_title_package(config)
    queue_path = build_queue(config, reuse_videos=reuse_videos)
    editor_handoff = export_editor_handoff(config)
    publish_handoff = export_publish_handoff(config)
    log_line(config, "一键流水线完成")

    result: dict[str, Any] = {
        "生成视频": len(rendered),
        "进入待发布": len(approved),
        "标题表": str(title_package["titles"]),
        "发布文案": str(title_package["copywriting"]),
        "封面图": len(title_package["covers"]),
        "发布队列": str(queue_path),
        "剪辑交接包": str(editor_handoff),
        "发布交接包": str(publish_handoff),
    }
    if edit_result:
        result.update(
            {
                "自动剪辑素材包": str(edit_result.work_dir),
                "自动剪辑预览": str(edit_result.preview_path or ""),
                "剪映导入清单": str(edit_result.import_csv),
            }
        )
    if strategy_result:
        result.update(
            {
                "自动剪辑策略": strategy_result.strategy,
                "自动剪辑计划": str(strategy_result.plan_csv),
                "自动剪辑报告": str(strategy_result.report_json),
                "计划分镜数": strategy_result.segments,
            }
        )
    return result
