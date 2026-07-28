from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from .adapters import export_editor_handoff, export_publish_handoff, import_editor_result, launch_local_editor, launch_publish_tool
from .assemble import assemble_clips
from .auto_cut import DEFAULT_KEYWORDS, STRATEGIES, AutoCutSettings, recommend_auto_cut_settings, run_strategy_auto_edit
from .batch_archive import build_batch_archive
from .capcut_export import export_capcut_draft_package
from .capability_check import format_capability_report, write_capability_report
from .dub_sync import dub_assemble
from .editing_engine import run_auto_edit
from .episode_pipeline import episode_rework
from .github_live_analysis import write_live_search_report
from .github_research import priority_candidates, research_summary, write_github_research_report
from .inventory import import_assets, write_inventory
from .plugin_adapters import (
    build_silence_cut_command,
    build_subtitle_convert_command,
    build_transcribe_command,
    download_video,
    install_whisper_runtime,
)
from .plugins import plugin_statuses, write_vendor_manifest
from .production_line import produce
from .pipeline import run_full_pipeline
from .models import DIR_A, DIR_AUTO_EDIT_INPUT, DIR_REVIEW, VIDEO_EXTENSIONS
from .project import create_project, iter_media, load_config, save_config
from .purity_audit import format_purity_audit, write_purity_audit_report
from .publish_feedback import generate_feedback_template, import_feedback
from .publish_queue import build_queue, ensure_demo_account, import_publish_status, mark_queue_failed, mark_queue_published
from .release_verifier import format_release_verification_report, write_release_verification_report
from .scene_detect import detect_scenes as detect_scene_spans, scenedetect_available, split_scenes_to_clips
from .social_clipper import SocialClipOptions, generate_social_clip_package
from .title_engine import generate_title_package
from .visual_package import DEFAULT_TEMPLATE, generate_visual_package, packaging_template_names
from .video_dedup import compare_rework_similarity, write_rework_comparison_report
from .video_engine import render_high_similarity_replacements, render_project
from .workflow_verifier import format_workflow_verification_report, write_workflow_verification_report


def command_init(args: argparse.Namespace) -> None:
    config = create_project(args.root, args.name, args.drama_name)
    print(f"项目已创建：{config.project_root}")


def command_render(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    if args.mode:
        config.recipe.mode = args.mode
        save_config(config)
    source_files = None
    source_label = "A 原始视频"
    if args.source_dir:
        source_dir = Path(args.source_dir)
        if not source_dir.is_absolute() and not source_dir.exists():
            source_dir = config.root / source_dir
        source_files = iter_media(source_dir, VIDEO_EXTENSIONS)
        source_label = "镜头切片素材"
        if not source_files:
            raise SystemExit(f"指定素材目录没有视频文件：{source_dir}")
    outputs = render_project(config, copies=args.copies, source_files=source_files, source_label=source_label)
    print(f"生成完成：{len(outputs)} 条")
    for item in outputs:
        print(item)


def command_regenerate_high_similarity(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = render_high_similarity_replacements(config, report_path=args.report, dedup_level=args.dedup_level)
    print(f"高相似重做完成：{len(result.rendered)} 条")
    print(f"原 high 组：{result.high_pair_count} 组，涉及成片：{result.high_video_count} 条")
    print(f"输出目录：{result.output_dir}")
    print(f"重做报告：{result.summary_md}")
    if result.comparison_md:
        print(f"对比报告：{result.comparison_md}")
    if result.comparison_conclusion:
        print(f"对比结论：{result.comparison_conclusion}")
    if result.missing_sources:
        print(f"路径兜底：{len(result.missing_sources)} 条")


def command_compare_rework(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    rework_dir = _resolve_rework_dir(config, args.rework)
    old_report = args.report or _report_from_rework_summary(rework_dir)
    if not old_report.exists():
        raise SystemExit(f"原指纹报告不存在：{old_report}")
    report = compare_rework_similarity(config, old_report, rework_dir, config.recipe.dedup_similarity_threshold)
    json_path, md_path = write_rework_comparison_report(report, rework_dir)
    print(f"对比报告 JSON：{json_path}")
    print(f"对比报告 MD：{md_path}")
    print(f"对比结论：{report.conclusion}")


def command_batch_archive(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    try:
        result = build_batch_archive(config, batch_dir=args.batch)
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"成品批次档案已生成：{result.batch_id}")
    print(f"JSON：{result.archive_json}")
    print(f"MD：{result.archive_md}")
    print(f"缺失项：{len(result.missing_artifacts)}")


def command_assemble(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = assemble_clips(config, plan=args.plan, input_dir=args.input_dir, name=args.name, keep_segments=args.keep_segments)
    print(f"整合成片完成：{result.output_path}")
    print(f"片段数：{len(result.items)}，总时长约 {result.total_seconds:.1f} 秒")
    print(f"整合清单：{result.manifest_csv}")


def command_dub_assemble(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = dub_assemble(
        config,
        audio_dir=args.audio_dir,
        video_dir=args.video_dir,
        name=args.name,
        trim_anchor=args.trim_anchor,
        burn_subtitle=args.subtitle,
        keep_segments=args.keep_segments,
    )
    counts = Counter(segment.strategy for segment in result.segments)
    successful = sum(1 for segment in result.segments if segment.segment_file)
    strategy_text = "，".join(f"{key}={value}" for key, value in sorted(counts.items())) or "无"
    print(f"配音对齐成片完成：{result.output_path}")
    print(f"成功段数：{successful}/{len(result.segments)}，总时长约 {result.total_seconds:.1f} 秒")
    print(f"策略统计：{strategy_text}")
    print(f"未匹配配音：{len(result.unmatched_audio)}，未匹配视频：{len(result.unmatched_video)}")
    print(f"音画对齐表：{result.plan_csv}")
    print(f"音画对齐 JSON：{result.plan_json}")
    print(f"可直接执行 produce --source {result.output_path} 进入矩阵产线")


def command_produce(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    labels = [item.strip() for item in (args.account_labels or "").split(",") if item.strip()]
    result = produce(
        config,
        source=args.source,
        plan=args.plan,
        input_dir=args.input_dir,
        copies=args.copies,
        account_labels=labels or None,
        dedup_level=args.dedup_level,
        name=args.name,
    )
    print(f"一键产线完成：账号 {result.account_count}，合格 {len(result.ready_videos)} 条，遗留 high {result.remaining_high_count} 条")
    print(f"合格目录：{result.ready_dir}")
    if result.feedback_template:
        print(f"回流模板：{result.feedback_template}")
    print(f"产线报告：{result.report_md}")


def _resolve_rework_dir(config, rework: Path) -> Path:
    candidate = Path(rework)
    if not candidate.is_absolute():
        direct = config.root / DIR_REVIEW / candidate
        if direct.exists():
            candidate = direct
    if candidate.exists() and candidate.is_dir():
        return candidate
    available = _available_rework_dirs(config)
    choices = "\n".join(f"- {path}" for path in available) or "无"
    raise SystemExit(f"重做批次目录不存在：{rework}\n可选高相似重做批次：\n{choices}")


def _available_rework_dirs(config) -> list[Path]:
    root = config.root / DIR_REVIEW
    if not root.exists():
        return []
    return sorted([path for path in root.iterdir() if path.is_dir() and "高相似重做" in path.name], key=lambda path: path.name)


def _report_from_rework_summary(rework_dir: Path) -> Path:
    summary = rework_dir / "高相似重做报告.json"
    if not summary.exists():
        raise SystemExit("该目录不是高相似重做批次或重做报告缺失。")
    try:
        payload = json.loads(summary.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f"重做报告解析失败：{exc}") from exc
    source_report = str(payload.get("source_report", "") or "").strip()
    if not source_report:
        raise SystemExit("该目录不是高相似重做批次或重做报告缺失。")
    return Path(source_report)


def command_edit(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    if args.strategy:
        strategy_result = run_strategy_auto_edit(
            config,
            AutoCutSettings(
                strategy=args.strategy,
                target_seconds=args.target_seconds or config.edit.target_seconds,
                clip_seconds=args.clip_seconds or config.edit.clip_seconds,
                silence_db=args.silence_db,
                min_silence=args.min_silence,
                scene_threshold=args.scene_threshold,
                keywords=args.keywords,
            ),
            input_dir=args.input,
            make_preview=not args.no_preview,
        )
        result = strategy_result.edit
        print(f"策略自动剪辑完成：{strategy_result.strategy}，{strategy_result.segments} 个计划分镜")
        print(f"策略计划：{strategy_result.plan_csv}")
        print(f"策略报告：{strategy_result.report_json}")
    else:
        result = run_auto_edit(
            config,
            input_dir=args.input,
            script_path=args.script,
            target_seconds=args.target_seconds,
            clip_seconds=args.clip_seconds,
            output_name=args.name,
            make_preview=not args.no_preview,
        )
    print(f"自动剪辑完成：{result.selected_count} 个分镜，约 {result.total_seconds:.1f} 秒")
    print(f"素材包：{result.work_dir}")
    print(f"剪辑计划：{result.plan_csv}")
    print(f"剪映导入清单：{result.import_csv}")
    if result.preview_path:
        print(f"预览成片：{result.preview_path}")


def command_recommend_edit(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    recommendation = recommend_auto_cut_settings(config, args.goal)
    print(f"推荐策略：{recommendation.settings.strategy}")
    print(f"推荐理由：{recommendation.reason}")
    print(f"目标时长：{recommendation.settings.target_seconds:.1f} 秒")
    print(f"单段时长：{recommendation.settings.clip_seconds:.1f} 秒")
    print(f"关键词：{recommendation.settings.keywords}")
    if recommendation.report_json:
        print(f"推荐报告：{recommendation.report_json}")
    if args.run:
        result = run_strategy_auto_edit(config, recommendation.settings, make_preview=not args.no_preview)
        print(f"自动剪辑完成：{result.strategy}，{result.segments} 个分镜")
        print(f"素材包：{result.edit.work_dir}")


def command_titles(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = generate_title_package(config, make_covers=not args.no_covers)
    print(f"标题清单已生成：{result['titles']}")
    print(f"发布文案已生成：{result['copywriting']}")
    print(f"封面图：{len(result['covers'])} 张")


def command_visual_package(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = generate_visual_package(
        config,
        make_videos=args.videos,
        intro_seconds=args.intro_seconds,
        outro_seconds=args.outro_seconds,
        template=args.template,
    )
    print(f"包装清单：{result.manifest_csv}")
    print(f"海报：{len(result.posters)} 张")
    print(f"片头/片尾卡：{len(result.cards)} 张")
    if args.videos:
        print(f"片头/片尾视频：{len(result.card_videos)} 条")
        print(f"包装成片：{len(result.packaged_videos)} 条")


def command_inventory(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    csv_path, json_path = write_inventory(config, with_hash=args.hash)
    print(f"素材清单已生成：{csv_path}")
    print(f"素材清单 JSON：{json_path}")


def command_import_assets(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    imported = import_assets(config, args.role, args.files)
    print(f"已导入 {len(imported)} 个文件")
    for item in imported:
        print(item)


def command_queue(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    path = build_queue(config, reuse_videos=args.reuse_videos, source=args.source, auto_account=args.auto_account)
    print(f"发布队列已生成：{path}")


def command_init_accounts(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    path = ensure_demo_account(config)
    print(f"发布账号已初始化/启用：{path}")


def command_mark_published(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = mark_queue_published(config, limit=args.limit)
    print(f"发布完成归档：{len(result.moved)} 条")
    print(f"发布队列：{result.queue_path}")
    print(f"发布记录：{result.log_path}")
    if result.skipped:
        print(f"跳过缺失文件：{len(result.skipped)} 条")
        for item in result.skipped:
            print(item)


def command_import_publish_status(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = import_publish_status(config, args.file, archive_published=args.archive)
    print(f"发布回写导入：读取 {result.imported} 条，更新 {result.updated} 条")
    print(f"发布队列：{result.queue_path}")
    print(f"发布记录：{result.log_path}")
    print(f"导入结果：{result.result_path}")
    if result.unmatched:
        print(f"未匹配：{len(result.unmatched)} 条")
        for item in result.unmatched:
            print(item)


def command_feedback_template(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = generate_feedback_template(config, ready_dir=args.ready_dir)
    print(f"发布回流模板已生成：{result.template_csv}")
    print(f"产线目录：{result.ready_dir}")
    print(f"行数：{result.row_count}")


def command_import_feedback(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = import_feedback(config, args.file)
    print(f"导入发布回写：导入 {result.imported} 条，未匹配 {len(result.unmatched)} 条，非法 {len(result.invalid)} 条")
    print(f"记录表：{result.records_csv}")
    print(f"复盘报告：{result.review_md}")
    if result.unmatched:
        print("未匹配前 5 条：")
        for item in result.unmatched[:5]:
            print(f"- {item}")
    if result.invalid:
        print("非法前 5 条：")
        for item in result.invalid[:5]:
            print(f"- {item}")


def command_mark_failed(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = mark_queue_failed(config, reason=args.reason, limit=args.limit)
    print(f"已标记失败：{result.updated} 条")
    print(f"发布队列：{result.queue_path}")
    print(f"发布记录：{result.log_path}")


def command_export_editor(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    path = export_editor_handoff(config)
    print(f"剪辑交接包已生成：{path}")


def command_import_editor_result(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = import_editor_result(config, args.file)
    print(f"剪辑器回写导入：{len(result.imported)} 条")
    print(f"报告：{result.report_json}")
    if result.skipped:
        print(f"跳过：{len(result.skipped)} 条")
        for item in result.skipped:
            print(item)


def command_export_publish(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    path = export_publish_handoff(config)
    print(f"发布工具交接包已生成：{path}")


def command_export_capcut(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = export_capcut_draft_package(
        config,
        draft_root=args.draft_root,
        work_dir=args.work_dir,
        create_real_draft=args.create_draft,
    )
    print(result.message)
    print(f"剪映草稿包：{result.package_dir}")
    print(f"时间线：{result.timeline_csv}")
    print(f"生成脚本：{result.script_py}")
    if result.real_draft_dir:
        print(f"真实草稿：{result.real_draft_dir}")


def command_pipeline(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = run_full_pipeline(
        config,
        copies=args.copies,
        mode=args.mode or None,
        skip_render=args.skip_render,
        reuse_videos=args.reuse_videos,
        with_edit=args.with_edit or bool(args.edit_strategy),
        edit_target_seconds=args.edit_target_seconds,
        edit_clip_seconds=args.edit_clip_seconds,
        edit_strategy=args.edit_strategy,
        silence_db=args.silence_db,
        min_silence=args.min_silence,
        scene_threshold=args.scene_threshold,
        keywords=args.keywords,
    )
    for key, value in result.items():
        print(f"{key}: {value}")


def command_launch(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    if args.tool == "local-editor":
        launch_local_editor(config)
        print("已打开本地剪辑器。")
    else:
        launch_publish_tool(config)
        print("已打开发布工具。")


def command_plugins(args: argparse.Namespace) -> None:
    manifest = write_vendor_manifest()
    print(f"能力清单已刷新：{manifest}")
    for item in plugin_statuses():
        state = "已下载" if item["downloaded"] else "未下载"
        ready = "可运行" if item["entry_ready"] or item.get("python_import_ready") or item.get("path_ready") else "待安装/待编译"
        print(f"{item['key']} | {state} | {ready} | {item['business_goal']}")


def command_capability_check(args: argparse.Namespace) -> None:
    result = write_capability_report(args.output)
    print(format_capability_report(result.report))
    print(f"Markdown报告：{result.markdown_path}")
    print(f"JSON报告：{result.json_path}")
    print(f"明细CSV：{result.csv_path}")


def command_install_whisper(args: argparse.Namespace) -> None:
    result = install_whisper_runtime(model=args.model, with_model=not args.no_model, project_root=args.project)
    print(result.message)
    print(f"状态：{'成功' if result.ok else '失败'}")
    print(f"结果记录：{Path(result.output_dir) / 'runtime_install_result.json'}")


def command_verify_release(args: argparse.Namespace) -> None:
    result = write_release_verification_report(args.version_root, args.output, smoke=args.smoke)
    print(format_release_verification_report(result.report))
    print(f"Markdown报告：{result.markdown_path}")
    print(f"JSON报告：{result.json_path}")
    print(f"明细CSV：{result.csv_path}")
    if result.report.overall_level == "error":
        raise SystemExit(1)


def command_purity_audit(args: argparse.Namespace) -> None:
    result = write_purity_audit_report(args.version_root, args.output)
    print(format_purity_audit(result.report))
    print(f"Markdown报告：{result.markdown_path}")
    print(f"JSON报告：{result.json_path}")
    print(f"明细CSV：{result.csv_path}")
    if result.report.overall_level == "error":
        raise SystemExit(1)


def command_verify_workflow(args: argparse.Namespace) -> None:
    result = write_workflow_verification_report(
        work_root=args.work_root,
        output_dir=args.output,
        make_packaged_videos=not args.no_packaged_videos,
    )
    print(format_workflow_verification_report(result.report))
    print(f"Markdown报告：{result.markdown_path}")
    print(f"JSON报告：{result.json_path}")
    print(f"明细CSV：{result.csv_path}")
    if result.report.overall_level == "error":
        raise SystemExit(1)


def command_github_radar(args: argparse.Namespace) -> None:
    paths = write_github_research_report(args.output or None)
    live_paths = write_live_search_report(args.output or None)
    summary = research_summary()
    print(f"GitHub 分析矩阵已生成：{paths['markdown']}")
    print(f"GitHub 逐项分析已生成：{live_paths['markdown']}")
    print(f"候选仓库：{summary['candidate_count']} 个；检索入口：{summary['search_url_count']} 个")
    for item in priority_candidates(args.limit):
        print(f"{item.repo} | {item.decision} | {item.integration_target}")


def command_social_clips(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = generate_social_clip_package(
        config,
        SocialClipOptions(
            source=args.source,
            ratio=args.ratio,
            use_bgm=not args.no_bgm,
            bgm_mode=args.bgm_mode,
        ),
    )
    print(f"社媒增强版：{len(result.items)} 条")
    print(f"输出目录：{result.output_dir}")
    print(f"清单：{result.manifest_csv}")


def command_detect_scenes(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    raw_input = args.input or args.video
    if not raw_input:
        raise SystemExit("请提供 --input <视频路径>。")
    video = _resolve_scene_input(config, raw_input)
    result = detect_scene_spans(config, video, threshold=args.threshold, min_scene_seconds=args.min_scene_seconds)
    print(f"场景检测完成：引擎 {result.engine}，场景 {len(result.scenes)} 个")
    print(f"CSV：{result.csv_path}")
    print(f"JSON：{result.json_path}")
    if result.engine == "ffmpeg_fallback" and result.fallback_reason:
        scene_ok, _scene_detail = scenedetect_available()
        if scene_ok:
            print("PySceneDetect 运行异常，已使用 FFmpeg 兜底。")
        else:
            print("PySceneDetect 未安装，已使用 FFmpeg 兜底。可在开发环境执行 pip install scenedetect[opencv] 启用。")
        print(f"兜底原因：{result.fallback_reason}")


def command_split_scenes(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    video = _resolve_scene_input(config, args.input)
    result = split_scenes_to_clips(config, video, min_clip_seconds=args.min_clip_seconds)
    print(f"镜头切片完成：{len(result.clips)} 条，跳过短片段 {result.skipped_short} 条")
    print(f"引擎：{result.engine}")
    print(f"输出目录：{result.output_dir}")
    print(f"清单：{result.manifest_csv}")


def command_episode_rework(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    level = {
        "基础": "light",
        "标准": "balanced",
        "增强": "strong",
        "basic": "light",
        "standard": "balanced",
        "enhanced": "strong",
    }.get(args.dedup_level or "", args.dedup_level)
    result = episode_rework(
        config,
        video=args.input,
        versions=args.versions,
        audio_mode=args.audio_mode,
        replacement_audio=args.replacement_audio,
        dedup_level=level,
        mode=args.mode,
        threshold=args.threshold,
        min_scene_seconds=args.min_scene_seconds,
        min_clip_seconds=args.min_clip_seconds,
        name=args.name,
        keep_segments=args.keep_segments,
        write_similarity_report=False if args.no_dedup_report else None,
    )
    print(f"整集重组完成：输出 {len(result.output_paths)} 条")
    print(f"镜头片段：{result.scene_count} 段；引擎：{result.scene_result.engine}")
    print(f"音频模式：{result.audio_mode}；失败片段：{len(result.failed_segments)}")
    if result.fallback_used:
        print(f"兜底说明：{result.scene_result.fallback_reason}")
    print(f"批次目录：{result.batch_dir}")
    print(f"重组清单：{result.manifest_csv}")
    for item in result.output_paths:
        print(item)


def _resolve_scene_input(config, value: str | Path) -> Path:
    raw = Path(value)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.extend([raw, config.root / raw, config.root / DIR_AUTO_EDIT_INPUT / raw, config.root / DIR_A / raw])
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    available = _scene_input_choices(config)
    choices = "\n".join(f"- {item}" for item in available) or "无"
    raise SystemExit(f"找不到视频：{value}\n可用视频文件：\n{choices}")


def _scene_input_choices(config) -> list[str]:
    result: list[str] = []
    for root in [config.root / DIR_AUTO_EDIT_INPUT, config.root / DIR_A]:
        for video in iter_media(root, VIDEO_EXTENSIONS):
            result.append(f"{video.name} ({video.parent})")
    return result


def command_download(args: argparse.Namespace) -> None:
    config = load_config(args.project)
    result = download_video(config, args.url, target=args.target)
    print(result.message)
    print(f"输出目录：{result.output_dir}")


def command_plugin_command(args: argparse.Namespace) -> None:
    if args.kind == "silence-cut":
        result = build_silence_cut_command(args.input, args.output)
    elif args.kind == "transcribe":
        result = build_transcribe_command(args.input, args.output, model_path=args.model)
    else:
        result = build_subtitle_convert_command(args.input, args.output)
    print(result.message)
    print(" ".join(result.command))


def command_serve_api(args: argparse.Namespace) -> None:
    import os

    from .api_server import run_server

    token = args.token or os.environ.get("MERCURY_API_TOKEN", "")
    run_server(host=args.host, port=args.port, token=token, max_workers=args.max_workers)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="水星剪辑")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="创建统一项目目录")
    init.add_argument("--root", required=True, help="项目父目录")
    init.add_argument("--name", required=True, help="项目名称")
    init.add_argument("--drama-name", default="", help="剧名，不填则使用项目名称")
    init.set_defaults(func=command_init)

    render = subparsers.add_parser("render", help="批量生成二创视频")
    render.add_argument("--project", required=True, type=Path, help="项目目录")
    render.add_argument("--copies", type=int, default=None, help="每条原视频生成几条")
    render.add_argument("--mode", choices=["vertical", "horizontal"], default="", help="生成模式")
    render.add_argument("--source-dir", type=Path, default=None, help="指定视频素材目录，例如镜头切片输出目录")
    render.set_defaults(func=command_render)

    regenerate = subparsers.add_parser("regenerate-high-similarity", help="读取去重指纹报告并重新生成 high 相似成片")
    regenerate.add_argument("--project", required=True, type=Path, help="项目目录")
    regenerate.add_argument("--report", type=Path, default=None, help="指定去重指纹报告 JSON；不填则使用最新报告")
    regenerate.add_argument("--dedup-level", choices=["light", "balanced", "strong"], default="strong", help="重做使用的去重强度")
    regenerate.set_defaults(func=command_regenerate_high_similarity)

    compare_rework = subparsers.add_parser("compare-rework", help="补跑高相似重做前后相似度对比")
    compare_rework.add_argument("--project", required=True, type=Path, help="项目目录")
    compare_rework.add_argument("--rework", required=True, type=Path, help="高相似重做批次目录")
    compare_rework.add_argument("--report", type=Path, default=None, help="原去重指纹报告 JSON；不填则读取重做报告中的 source_report")
    compare_rework.set_defaults(func=command_compare_rework)

    batch_archive = subparsers.add_parser("batch-archive", help="生成成品批次档案快照")
    batch_archive.add_argument("--project", required=True, type=Path, help="项目目录")
    batch_archive.add_argument("--batch", type=Path, default=None, help="指定普通二创批次目录；不填则使用最新普通批次")
    batch_archive.set_defaults(func=command_batch_archive)

    assemble = subparsers.add_parser("assemble", help="按清单或片段目录整合短剧预告/高能混剪")
    assemble.add_argument("--project", required=True, type=Path, help="项目目录")
    assemble.add_argument("--plan", type=Path, default=None, help="整合清单 CSV，字段 order,file,start,end")
    assemble.add_argument("--input-dir", type=Path, default=None, help="片段目录；不提供清单时按文件名顺序整合")
    assemble.add_argument("--name", default="", help="输出成片名")
    assemble.add_argument("--keep-segments", action="store_true", help="保留整合临时片段和 concat 列表，便于排查")
    assemble.set_defaults(func=command_assemble)

    dub = subparsers.add_parser("dub-assemble", help="配音包与分镜视频按时长对齐后合成为音画同步成片")
    dub.add_argument("--project", required=True, type=Path, help="项目目录")
    dub.add_argument("--audio-dir", required=True, type=Path, help="配音包目录，优先读取 配音清单.csv")
    dub.add_argument("--video-dir", required=True, type=Path, help="分镜视频目录，按文件名自然排序配对")
    dub.add_argument("--name", default="", help="输出成片名")
    dub.add_argument("--trim-anchor", choices=["head", "center", "tail"], default="head", help="视频长于配音时保留哪一段")
    dub.add_argument("--subtitle", action="store_true", help="按配音清单 text 烧录底部字幕")
    dub.add_argument("--keep-segments", action="store_true", help="保留配音对齐临时段，便于排查")
    dub.set_defaults(func=command_dub_assemble)

    produce_cmd = subparsers.add_parser("produce", help="短剧矩阵一键产线：整合/二创/指纹/重做/合格出片")
    produce_cmd.add_argument("--project", required=True, type=Path, help="项目目录")
    produce_source = produce_cmd.add_mutually_exclusive_group(required=True)
    produce_source.add_argument("--source", type=Path, default=None, help="直接作为产线输入的成片路径")
    produce_source.add_argument("--plan", type=Path, default=None, help="先按整合清单生成成片，再进入产线")
    produce_source.add_argument("--input-dir", type=Path, default=None, help="先按片段目录顺序整合，再进入产线")
    produce_cmd.add_argument("--copies", type=int, default=3, help="账号数/差异化副本数")
    produce_cmd.add_argument("--account-labels", default="", help="账号标签，逗号分隔，例如 A号,B号,C号")
    produce_cmd.add_argument("--dedup-level", choices=["标准", "增强", "light", "balanced", "strong"], default="标准", help="差异化强度")
    produce_cmd.add_argument("--name", default="", help="整合成片名；仅 plan/input-dir 模式使用")
    produce_cmd.set_defaults(func=command_produce)

    edit = subparsers.add_parser("edit", help="自动化剪辑：筛选分镜、生成剪映导入清单和预览样片")
    edit.add_argument("--project", required=True, type=Path, help="项目目录")
    edit.add_argument("--input", type=Path, default=None, help="自动剪辑输入目录；不填则优先用 02_自动剪辑/01_粗剪输入")
    edit.add_argument("--script", type=Path, default=None, help="分镜脚本 CSV/XLSX，可包含镜号、台词、人物、声音类型、时长、素材文件")
    edit.add_argument("--target-seconds", type=float, default=None, help="目标样片时长，默认读取 project.json")
    edit.add_argument("--clip-seconds", type=float, default=None, help="没有分镜时每个素材截取几秒")
    edit.add_argument("--name", default="", help="本次自动剪辑输出文件夹名")
    edit.add_argument("--strategy", choices=STRATEGIES, default="", help="自动剪辑策略；不填则按分镜表或顺序截取")
    edit.add_argument("--silence-db", type=float, default=-35.0, help="去静音阈值，数值越高越容易判定为静音")
    edit.add_argument("--min-silence", type=float, default=0.45, help="静音持续多久才切掉")
    edit.add_argument("--scene-threshold", type=float, default=0.35, help="镜头变化阈值，越低切点越多")
    edit.add_argument("--keywords", default=DEFAULT_KEYWORDS, help="字幕关键词策略使用的词，逗号分隔")
    edit.add_argument("--no-preview", action="store_true", help="只生成素材包和清单，不合成预览成片")
    edit.set_defaults(func=command_edit)

    recommend_edit = subparsers.add_parser("recommend-edit", help="按一句剪辑目标自动推荐策略，必要时直接执行自动剪辑")
    recommend_edit.add_argument("--project", required=True, type=Path, help="项目目录")
    recommend_edit.add_argument("--goal", required=True, help="剪辑目标，例如：60秒冲突反转高光")
    recommend_edit.add_argument("--run", action="store_true", help="推荐后立即按该策略执行自动剪辑")
    recommend_edit.add_argument("--no-preview", action="store_true", help="执行剪辑时不生成预览成片")
    recommend_edit.set_defaults(func=command_recommend_edit)

    titles = subparsers.add_parser("titles", help="生成标题清单、发布文案和封面图")
    titles.add_argument("--project", required=True, type=Path, help="项目目录")
    titles.add_argument("--no-covers", action="store_true", help="不自动抽封面图")
    titles.set_defaults(func=command_titles)

    visual_package = subparsers.add_parser("visual-package", help="生成海报封面、片头片尾卡和包装版视频")
    visual_package.add_argument("--project", required=True, type=Path, help="项目目录")
    visual_package.add_argument("--videos", action="store_true", help="同时生成加片头片尾的包装版视频")
    visual_package.add_argument("--intro-seconds", type=float, default=1.4, help="片头时长")
    visual_package.add_argument("--outro-seconds", type=float, default=1.0, help="片尾时长")
    visual_package.add_argument("--template", choices=packaging_template_names(), default=DEFAULT_TEMPLATE, help="包装模板")
    visual_package.set_defaults(func=command_visual_package)

    inventory = subparsers.add_parser("inventory", help="生成素材清单")
    inventory.add_argument("--project", required=True, type=Path, help="项目目录")
    inventory.add_argument("--hash", action="store_true", help="为小文件计算 sha1，便于去重")
    inventory.set_defaults(func=command_inventory)

    import_cmd = subparsers.add_parser("import-assets", help="素材入库：自动复制到正确业务目录")
    import_cmd.add_argument("--project", required=True, type=Path, help="项目目录")
    import_cmd.add_argument("--role", required=True, choices=["原始视频", "去重背景", "贴图素材", "音频素材", "文案分镜", "自动剪辑输入", "封面图"])
    import_cmd.add_argument("files", nargs="+", type=Path)
    import_cmd.set_defaults(func=command_import_assets)

    queue = subparsers.add_parser("queue", help="生成发布队列")
    queue.add_argument("--project", required=True, type=Path, help="项目目录")
    queue.add_argument("--reuse-videos", action="store_true", help="视频数量不足时允许重复分配")
    queue.add_argument("--source", choices=["auto", "ready", "social", "packaged"], default="auto", help="发布视频来源；auto 优先包装成片，其次社媒增强版")
    queue.add_argument("--auto-account", action="store_true", help="没有启用账号时自动启用一个本地测试账号")
    queue.set_defaults(func=command_queue)

    accounts = subparsers.add_parser("init-accounts", help="启用一个本地测试发布账号，便于打通发布队列")
    accounts.add_argument("--project", required=True, type=Path, help="项目目录")
    accounts.set_defaults(func=command_init_accounts)

    published = subparsers.add_parser("mark-published", help="发布完成后归档视频并更新发布队列")
    published.add_argument("--project", required=True, type=Path, help="项目目录")
    published.add_argument("--limit", type=int, default=None, help="最多归档几条；不填则归档队列中所有待发布视频")
    published.set_defaults(func=command_mark_published)

    import_status = subparsers.add_parser("import-publish-status", help="导入发布工具回写状态，更新队列、链接和失败原因")
    import_status.add_argument("--project", required=True, type=Path, help="项目目录")
    import_status.add_argument("--file", required=True, type=Path, help="发布工具回写 CSV/JSON")
    import_status.add_argument("--archive", action="store_true", help="已发布视频同步归档到发布完成目录")
    import_status.set_defaults(func=command_import_publish_status)

    feedback_template = subparsers.add_parser("feedback-template", help="生成发布数据回流模板")
    feedback_template.add_argument("--project", required=True, type=Path, help="项目目录")
    feedback_template.add_argument("--ready-dir", type=Path, default=None, help="指定产线合格目录；不填则使用最新产线目录")
    feedback_template.set_defaults(func=command_feedback_template)

    import_feedback_cmd = subparsers.add_parser("import-feedback", help="导入发布回写并生成表现复盘")
    import_feedback_cmd.add_argument("--project", required=True, type=Path, help="项目目录")
    import_feedback_cmd.add_argument("--file", required=True, type=Path, help="填写后的发布回流 CSV")
    import_feedback_cmd.set_defaults(func=command_import_feedback)

    failed = subparsers.add_parser("mark-failed", help="手动把发布队列中的待发布项标记为失败")
    failed.add_argument("--project", required=True, type=Path, help="项目目录")
    failed.add_argument("--reason", default="", help="失败原因")
    failed.add_argument("--limit", type=int, default=1, help="最多标记几条")
    failed.set_defaults(func=command_mark_failed)

    export_editor = subparsers.add_parser("export-editor", help="导出剪辑交接包")
    export_editor.add_argument("--project", required=True, type=Path, help="项目目录")
    export_editor.set_defaults(func=command_export_editor)

    import_editor = subparsers.add_parser("import-editor-result", help="导入本地剪辑器回写结果到中间结果目录")
    import_editor.add_argument("--project", required=True, type=Path, help="项目目录")
    import_editor.add_argument("--file", required=True, type=Path, help="剪辑器回写 CSV/JSON")
    import_editor.set_defaults(func=command_import_editor_result)

    export_publish = subparsers.add_parser("export-publish", help="导出给发布工具的交接包")
    export_publish.add_argument("--project", required=True, type=Path, help="项目目录")
    export_publish.set_defaults(func=command_export_publish)

    export_capcut = subparsers.add_parser("export-capcut", help="导出剪映/CapCut 草稿交接包")
    export_capcut.add_argument("--project", required=True, type=Path, help="项目目录")
    export_capcut.add_argument("--work-dir", type=Path, default=None, help="指定自动剪辑素材包；不填则使用最新素材包")
    export_capcut.add_argument("--draft-root", type=Path, default=None, help="CapCut/剪映草稿目录；不填则只在交接包中生成本地草稿目录")
    export_capcut.add_argument("--create-draft", action="store_true", help="如果 pyCapCut 可用，尝试直接创建真实草稿")
    export_capcut.set_defaults(func=command_export_capcut)

    pipeline = subparsers.add_parser("pipeline", help="一键生成视频、标题、发布队列和交接包")
    pipeline.add_argument("--project", required=True, type=Path, help="项目目录")
    pipeline.add_argument("--copies", type=int, default=None, help="每条原视频生成几条")
    pipeline.add_argument("--mode", choices=["vertical", "horizontal"], default="", help="生成模式")
    pipeline.add_argument("--skip-render", action="store_true", help="跳过视频生成，只生成标题/队列/交接包")
    pipeline.add_argument("--reuse-videos", action="store_true", help="视频数量不足时允许发布队列重复分配")
    pipeline.add_argument("--with-edit", action="store_true", help="先生成自动剪辑素材包和预览样片")
    pipeline.add_argument("--edit-target-seconds", type=float, default=None, help="自动剪辑目标时长")
    pipeline.add_argument("--edit-clip-seconds", type=float, default=None, help="自动剪辑每个素材默认截取几秒")
    pipeline.add_argument("--edit-strategy", choices=STRATEGIES, default="", help="一键流水线使用的自动剪辑策略")
    pipeline.add_argument("--silence-db", type=float, default=-35.0, help="去静音阈值，数值越高越容易判定为静音")
    pipeline.add_argument("--min-silence", type=float, default=0.45, help="静音持续多久才切掉")
    pipeline.add_argument("--scene-threshold", type=float, default=0.35, help="镜头变化阈值，越低切点越多")
    pipeline.add_argument("--keywords", default=DEFAULT_KEYWORDS, help="字幕关键词策略使用的词，逗号分隔")
    pipeline.set_defaults(func=command_pipeline)

    launch = subparsers.add_parser("launch", help="打开本地剪辑器或发布工具")
    launch.add_argument("--project", required=True, type=Path, help="项目目录")
    launch.add_argument("--tool", choices=["local-editor", "publish"], required=True, help="要打开的软件")
    launch.set_defaults(func=command_launch)

    plugins = subparsers.add_parser("plugins", help="查看能力组件下载和可用状态")
    plugins.set_defaults(func=command_plugins)

    capability = subparsers.add_parser("capability-check", help="生成能力组件自检报告")
    capability.add_argument("--output", type=Path, default=None, help="输出目录，默认写入当前版本 docs")
    capability.set_defaults(func=command_capability_check)

    install_whisper = subparsers.add_parser("install-whisper", help="安装本地真实语音转写运行时和模型")
    install_whisper.add_argument("--model", choices=["tiny", "base", "small"], default="tiny", help="下载的 whisper.cpp ggml 模型")
    install_whisper.add_argument("--no-model", action="store_true", help="只安装 whisper-cli，不下载模型")
    install_whisper.add_argument("--project", type=Path, default=None, help="可选项目目录；存在 00_项目配置/模型源.json 时覆盖官方下载源")
    install_whisper.set_defaults(func=command_install_whisper)

    verify_release = subparsers.add_parser("verify-release", help="验收当前版本发布包")
    verify_release.add_argument("--version-root", type=Path, default=None, help="版本目录；默认使用当前源码所在版本")
    verify_release.add_argument("--output", type=Path, default=None, help="输出目录，默认写入 docs/release_verification")
    verify_release.add_argument("--smoke", action="store_true", help="启动主程序做 8 秒烟测")
    verify_release.set_defaults(func=command_verify_release)

    purity_audit = subparsers.add_parser("purity-audit", help="终审水星纯净版字段、产物和流程验收")
    purity_audit.add_argument("--version-root", type=Path, default=None, help="版本目录；默认使用当前源码所在版本")
    purity_audit.add_argument("--output", type=Path, default=None, help="输出目录，默认写入 docs/purity_audit")
    purity_audit.set_defaults(func=command_purity_audit)

    verify_workflow = subparsers.add_parser("verify-workflow", help="跑通真实业务流程验收")
    verify_workflow.add_argument("--work-root", type=Path, default=None, help="验收项目生成目录，默认写入当前版本 tmp_workflow_verification")
    verify_workflow.add_argument("--output", type=Path, default=None, help="报告输出目录，默认写入 docs/workflow_verification")
    verify_workflow.add_argument("--no-packaged-videos", action="store_true", help="只生成包装素材，不合成包装成片")
    verify_workflow.set_defaults(func=command_verify_workflow)

    github_radar = subparsers.add_parser("github-radar", help="生成 GitHub 自动剪辑仓库分析矩阵")
    github_radar.add_argument("--output", type=Path, default=None, help="输出目录，默认写入当前版本 docs")
    github_radar.add_argument("--limit", type=int, default=12, help="命令行显示多少个优先候选")
    github_radar.set_defaults(func=command_github_radar)

    social = subparsers.add_parser("social-clips", help="生成社媒切条增强版视频")
    social.add_argument("--project", required=True, type=Path, help="项目目录")
    social.add_argument("--source", choices=["ready", "intermediate"], default="ready", help="读取合格待发布或中间结果")
    social.add_argument("--ratio", choices=["9:16"], default="9:16", help="输出比例")
    social.add_argument("--bgm-mode", choices=["ducking", "background"], default="ducking", help="BGM 混音方式")
    social.add_argument("--no-bgm", action="store_true", help="不混入 BGM")
    social.set_defaults(func=command_social_clips)

    scenes = subparsers.add_parser("detect-scenes", help="检测视频镜头切点，PySceneDetect 可用时优先使用，否则 FFmpeg 兜底")
    scenes.add_argument("--project", required=True, type=Path, help="项目目录")
    scenes.add_argument("--input", type=Path, default=None, help="要检测的视频，支持绝对路径或项目素材文件名")
    scenes.add_argument("--threshold", type=float, default=None, help="检测阈值；FFmpeg 使用 0-1，PySceneDetect 使用 0-100")
    scenes.add_argument("--min-scene-seconds", type=float, default=0.5, help="过短场景合并阈值")
    scenes.add_argument("--video", type=Path, default=None, help="兼容旧参数：要检测的视频")
    scenes.add_argument("--output", type=Path, default=None, help="兼容旧参数：当前统一写入 02_自动剪辑/02_场景检测")
    scenes.set_defaults(func=command_detect_scenes)

    split_scenes = subparsers.add_parser("split-scenes", help="按镜头切点切出二创素材片段")
    split_scenes.add_argument("--project", required=True, type=Path, help="项目目录")
    split_scenes.add_argument("--input", required=True, type=Path, help="要切片的视频，支持绝对路径或项目素材文件名")
    split_scenes.add_argument("--min-clip-seconds", type=float, default=1.0, help="短于该时长的场景跳过")
    split_scenes.set_defaults(func=command_split_scenes)

    episode = subparsers.add_parser("episode-rework", help="长剧集切片后逐段去重，并按原顺序重组成多条整集版本")
    episode.add_argument("--project", required=True, type=Path, help="项目目录")
    episode.add_argument("--input", "--episode", dest="input", required=True, type=Path, help="要重组的长剧集视频，支持绝对路径或项目素材文件名")
    episode.add_argument("--versions", type=int, default=3, help="输出多少条完整整集版本")
    episode.add_argument("--dedup-level", "--level", dest="dedup_level", choices=["light", "balanced", "strong", "basic", "standard", "enhanced", "基础", "标准", "增强"], default=None, help="逐段去重强度；不填则使用项目配置")
    episode.add_argument("--audio-mode", choices=["keep_original", "replace_track"], default="keep_original", help="keep_original 保留原声；replace_track 使用整条替换音频")
    episode.add_argument("--replacement-audio", type=Path, default=None, help="audio-mode=replace_track 时使用的整条替换音频")
    episode.add_argument("--mode", choices=["vertical", "horizontal"], default=None, help="输出模式；不填则使用项目配置")
    episode.add_argument("--threshold", type=float, default=None, help="切点阈值；0-1 给 FFmpeg，0-100 给 PySceneDetect")
    episode.add_argument("--min-scene-seconds", type=float, default=0.5, help="过短场景合并阈值")
    episode.add_argument("--min-clip-seconds", type=float, default=1.0, help="短于该时长的切片跳过")
    episode.add_argument("--name", default=None, help="批次和输出文件名前缀")
    episode.add_argument("--keep-segments", action="store_true", help="保留逐段去重后的临时片段")
    episode.add_argument("--no-dedup-report", action="store_true", help="不生成整集版本之间的去重指纹报告")
    episode.set_defaults(func=command_episode_rework)

    download = subparsers.add_parser("download", help="使用 yt-dlp 下载已授权的视频素材")
    download.add_argument("--project", required=True, type=Path, help="项目目录")
    download.add_argument("--url", required=True, help="视频链接")
    download.add_argument("--target", choices=["原始视频", "自动剪辑输入"], default="原始视频", help="下载到哪个素材区")
    download.set_defaults(func=command_download)

    plugin_command = subparsers.add_parser("plugin-command", help="生成外部能力组件调用命令")
    plugin_command.add_argument("--kind", choices=["silence-cut", "transcribe", "subtitle-convert"], required=True)
    plugin_command.add_argument("--input", required=True, type=Path)
    plugin_command.add_argument("--output", required=True, type=Path)
    plugin_command.add_argument("--model", default="", type=Path)
    plugin_command.set_defaults(func=command_plugin_command)

    serve_api = subparsers.add_parser("serve-api", help="启动对外 HTTP API 服务，供第三方系统编程接入生产能力")
    serve_api.add_argument("--host", default="127.0.0.1", help="绑定地址；默认仅本机 127.0.0.1，局域网接入用 0.0.0.0（需自备防火墙与强 token）")
    serve_api.add_argument("--port", type=int, default=8756, help="监听端口，默认 8756")
    serve_api.add_argument("--token", default="", help="Bearer 鉴权 token；不填则读环境变量 MERCURY_API_TOKEN，仍为空则自动生成并打印")
    serve_api.add_argument("--max-workers", type=int, default=2, help="整机最大并发任务数，默认 2；同一项目内任务自动串行，0 表示不限")
    serve_api.set_defaults(func=command_serve_api)
    return parser


def _configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def main() -> None:
    _configure_stdio()
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
