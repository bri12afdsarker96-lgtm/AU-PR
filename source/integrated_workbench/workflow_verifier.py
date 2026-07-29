from __future__ import annotations

import csv
import hashlib
import json
import shutil
from .proc import run_silent
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .adapters import export_editor_handoff, export_publish_handoff, import_editor_result
from .auto_cut import AutoCutSettings, run_strategy_auto_edit
from .batch_archive import build_batch_archive
from .capcut_export import export_capcut_draft_package
from .component_download import append_part_bytes
from .dub_sync import dub_assemble, has_audio_stream
from .editing_engine import _probe_duration
from .episode_pipeline import episode_rework
from .inventory import import_material_folder, write_inventory
from .model_registry import validate_component_file
from .models import (
    DIR_A,
    DIR_AUDIO,
    DIR_AUTO_EDIT_INPUT,
    DIR_B,
    DIR_C,
    DIR_DUB_ASSEMBLED,
    DIR_EDITOR_HANDOFF,
    DIR_EPISODE_REWORK,
    DIR_SCRIPT,
    DIR_PACKAGED_VIDEO,
    DIR_PUBLISH_STATUS,
    DIR_READY,
    DIR_REPORTS,
    ProjectConfig,
    VIDEO_EXTENSIONS,
)
from .production_line import produce
from .publish_feedback import (
    INSUFFICIENT_SAMPLE_MESSAGE,
    ORIGIN_ASSISTANT,
    TEMPLATE_COLUMNS,
    build_feedback_review,
    feedback_records_path,
    generate_feedback_template,
    import_feedback,
)
from .publish_assistant import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PUBLISHED,
    STATUS_SKIPPED,
    create_session,
    finish_session,
    load_session,
    mark_item,
    resume_session,
    session_counts,
    session_path,
)
from .project import create_project, iter_media, ready_videos, save_config
from .publish_queue import build_queue, ensure_demo_account, import_publish_status
from .production_state import promote_videos_to_ready
from .scene_detect import detect_scenes
from .social_clipper import SocialClipOptions, generate_social_clip_package
from .title_engine import generate_title_package
from .visual_package import generate_visual_package
from .video_engine import render_high_similarity_replacements, render_project


APP_NAME = "水星剪辑"


@dataclass
class WorkflowCheck:
    group: str
    item: str
    level: str
    message: str
    evidence: str = ""
    fix: str = ""


@dataclass
class WorkflowVerificationReport:
    app_name: str
    generated_at: str
    version_root: str
    project_root: str
    overall_level: str
    counts: dict[str, int]
    artifacts: dict[str, str]
    checks: list[WorkflowCheck]


@dataclass
class WorkflowVerificationFiles:
    report: WorkflowVerificationReport
    markdown_path: Path
    json_path: Path
    csv_path: Path


LEVEL_LABELS = {"ok": "通过", "warn": "提醒", "error": "需处理"}


def default_version_root() -> Path:
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "source" / "integrated_workbench").exists() and (candidate / "docs").exists():
            return candidate
    return Path(__file__).resolve().parents[2]


def verify_workflow(
    work_root: str | Path | None = None,
    make_packaged_videos: bool = True,
) -> WorkflowVerificationReport:
    version = default_version_root()
    parent = Path(work_root) if work_root else version / "tmp_workflow_verification"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    project_name = f"业务流程验收_{stamp}"
    project_root = parent / project_name
    checks: list[WorkflowCheck] = []
    artifacts: dict[str, str] = {}

    def add(group: str, item: str, level: str, message: str, evidence: str | Path = "", fix: str = "") -> None:
        checks.append(WorkflowCheck(group, item, level, message, str(evidence), fix))

    try:
        config = create_project(parent, project_name, drama_name="水星样片")
        config.workflow_mode = "full"
        config.recipe.copies_per_source = 1
        config.recipe.dedup_similarity_threshold = 0.60
        config.edit.target_seconds = 18.0
        config.edit.clip_seconds = 6.0
        save_config(config)
        artifacts["验收项目"] = str(config.root)
        add("项目", "创建项目", "ok", "已创建独立验收项目。", config.root)

        ffmpeg = Path(config.tools.ffmpeg) if config.tools.ffmpeg else Path()
        ffprobe = Path(config.tools.ffprobe) if config.tools.ffprobe else Path()
        add("环境", "FFmpeg", "ok" if ffmpeg.exists() else "error", "可调用视频处理引擎。" if ffmpeg.exists() else "未找到 FFmpeg。", ffmpeg, "检查随包 ffmpeg/bin。")
        add("环境", "FFprobe", "ok" if ffprobe.exists() else "error", "可调用视频探测引擎。" if ffprobe.exists() else "未找到 FFprobe。", ffprobe, "检查随包 ffmpeg/bin。")
        if not ffmpeg.exists() or not ffprobe.exists():
            return _finish(version, project_root, checks, artifacts)

        sample_assets = _create_sample_assets(config)
        artifacts.update({key: str(path) for key, path in sample_assets.items()})
        add("素材入库", "样本素材", "ok", "已生成原始视频、自动剪辑输入、背景视频、贴图和 BGM。", config.root)

        folder_source = config.root / "_验收_文件夹导入源"
        folder_source.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sample_assets["原始视频"], folder_source / "folder_clip_01.mp4")
        shutil.copy2(sample_assets["原始视频"], folder_source / "folder_clip_02.mov")
        (folder_source / "readme.tmp").write_text("skip me", encoding="utf-8")
        folder_import = import_material_folder(config, "原始视频", folder_source, include_subdirs=False)
        folder_import_ok = (
            folder_import.imported == 2
            and folder_import.skipped_non_media == 1
            and not folder_import.failed
            and all(path.exists() and path.parent == config.root / DIR_A for path in folder_import.imported_files)
        )
        artifacts["文件夹导入源"] = str(folder_source)
        add(
            "素材入库",
            "文件夹导入",
            "ok" if folder_import_ok else "error",
            f"导入 {folder_import.imported} 个，跳过 {folder_import.skipped_non_media} 个，失败 {len(folder_import.failed)} 个。",
            folder_source,
            "检查文件夹导入扫描、扩展名过滤和重名落盘逻辑。",
        )

        inventory_csv, inventory_json = write_inventory(config, with_hash=False)
        artifacts["素材清单"] = str(inventory_csv)
        add("素材入库", "素材清单", "ok" if inventory_csv.exists() and inventory_json.exists() else "error", "素材清单已生成。", inventory_csv)

        model_check_dir = config.root / DIR_REPORTS / "03_模型下载校验"
        model_check_dir.mkdir(parents=True, exist_ok=True)
        sample_bytes = b"mercury-whisper-model-sample"
        sample_sha = hashlib.sha256(sample_bytes).hexdigest()
        damaged = model_check_dir / "damaged-model.bin"
        good = model_check_dir / "sample-model.bin"
        part = model_check_dir / "sample-model.bin.part"
        damaged.write_bytes(b"broken")
        good.write_bytes(sample_bytes)
        part.write_bytes(b"abc")
        appended_size = append_part_bytes(part, b"def")
        damaged_check = validate_component_file(damaged, len(sample_bytes), sample_sha)
        good_check = validate_component_file(good, len(sample_bytes), sample_sha)
        part_ok = part.read_bytes() == b"abcdef" and appended_size == 6
        model_check_ok = (not damaged_check.ok and damaged_check.state == "damaged" and good_check.ok and part_ok)
        model_check_report = model_check_dir / "模型下载校验.json"
        model_check_report.write_text(
            json.dumps(
                {
                    "damaged_state": damaged_check.state,
                    "damaged_message": damaged_check.message,
                    "sample_ok": good_check.ok,
                    "sample_sha": sample_sha,
                    "part_content": part.read_bytes().decode("ascii"),
                    "part_size": appended_size,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        artifacts["模型下载校验"] = str(model_check_report)
        add(
            "工具箱",
            "模型下载校验",
            "ok" if model_check_ok else "error",
            "损坏模型被判定为不可用；样本模型 sha/size 校验通过；.part 续传字节拼接正确。",
            model_check_report,
            "检查模型注册表校验逻辑与断点续传字节拼接。",
        )

        scene = detect_scenes(config, sample_assets["自动剪辑输入"])
        scene_strategy = run_strategy_auto_edit(
            config,
            AutoCutSettings(strategy="镜头节奏粗剪", target_seconds=6.0, clip_seconds=3.0),
            input_dir=config.root / DIR_AUTO_EDIT_INPUT,
            make_preview=False,
        )
        try:
            scene_strategy_payload = json.loads(scene_strategy.report_json.read_text(encoding="utf-8"))
        except Exception:
            scene_strategy_payload = {}
        scene_report_ok = (
            Path(scene.csv_path).exists()
            and Path(scene.json_path).exists()
            and len(scene.scenes) >= 1
            and scene_strategy_payload.get("scene_engine") in {"pyscenedetect", "ffmpeg_fallback"}
        )
        scene_message = f"引擎 {scene.engine}，场景 {len(scene.scenes)} 个"
        if scene.fallback_reason:
            scene_message += f"，兜底：{scene.fallback_reason[:120]}"
        artifacts["场景检测报告"] = scene.json_path
        add(
            "自动剪辑",
            "镜头切点",
            "ok" if scene_report_ok else "error",
            scene_message,
            scene.json_path,
            "检查 FFmpeg 场景分析与 scenedetect 安装状态。",
        )

        cut = run_strategy_auto_edit(
            config,
            AutoCutSettings(strategy="顺序分镜", target_seconds=18.0, clip_seconds=6.0),
            input_dir=config.root / DIR_AUTO_EDIT_INPUT,
            make_preview=True,
        )
        selected = iter_media(cut.edit.selected_dir, VIDEO_EXTENSIONS)
        artifacts["自动剪辑计划"] = str(cut.plan_csv)
        artifacts["自动剪辑报告"] = str(cut.report_json)
        artifacts["自动剪辑预览"] = str(cut.edit.preview_path or "")
        add(
            "自动剪辑",
            "分镜素材包",
            "ok" if cut.segments > 0 and selected else "error",
            f"生成 {cut.segments} 个计划分镜，实际片段 {len(selected)} 条。",
            cut.edit.work_dir,
            "检查自动剪辑输入素材和 FFmpeg 截取命令。",
        )
        add("自动剪辑", "预览样片", "ok" if cut.edit.preview_path and cut.edit.preview_path.exists() else "error", "预览样片已生成。", cut.edit.preview_path or "")

        dub_assets = _create_dub_sync_assets(config)
        dub = dub_assemble(
            config,
            audio_dir=dub_assets["配音包"],
            video_dir=dub_assets["分镜视频"],
            name="配音对齐验收",
            trim_anchor="head",
            burn_subtitle=False,
        )
        fallback = dub_assemble(
            config,
            audio_dir=dub_assets["兜底配音包"],
            video_dir=dub_assets["兜底分镜视频"],
            name="配音对齐兜底验收",
            trim_anchor="head",
            burn_subtitle=False,
        )
        dub_duration = _probe_duration(config, dub.output_path) or 0.0
        dub_strategies = [segment.strategy for segment in dub.segments[:3]]
        dub_ok = (
            dub.output_path.exists()
            and abs(dub_duration - 6.2) <= 0.3
            and dub_strategies == ["trim_tail", "slow", "freeze"]
            and has_audio_stream(config, dub.output_path)
            and fallback.output_path.exists()
            and len([segment for segment in fallback.segments if segment.segment_file]) == 1
        )
        artifacts["配音对齐成片"] = str(dub.output_path)
        artifacts["配音对齐表"] = str(dub.plan_csv)
        artifacts["配音对齐兜底成片"] = str(fallback.output_path)
        artifacts["配音成片目录"] = str(config.root / DIR_DUB_ASSEMBLED)
        add(
            "剪辑整合",
            "配音对齐成片",
            "ok" if dub_ok else "error",
            f"策略 {dub_strategies}，总时长 {dub_duration:.2f}s，兜底段 {len(fallback.segments)} 个。",
            dub.plan_csv,
            "检查配音清单读取、兜底排序、三档对齐策略和音轨替换。",
        )

        rendered = render_project(config, copies=1, source_files=selected, source_label="自动剪辑分镜")
        add("二创成片", "批量生成", "ok" if rendered else "error", f"中间结果视频 {len(rendered)} 条。", rendered[0] if rendered else "")

        episode_assets = _create_episode_rework_assets(config)
        episode = episode_rework(
            config,
            episode_assets["多切点整集"],
            versions=2,
            dedup_level="light",
            threshold=0.10,
            min_scene_seconds=0.25,
            min_clip_seconds=0.25,
            name="workflow_episode_rework",
            write_similarity_report=False,
        )
        flat_episode = episode_rework(
            config,
            episode_assets["零切点整集"],
            versions=1,
            dedup_level="light",
            threshold=99.0,
            min_scene_seconds=0.25,
            min_clip_seconds=0.25,
            name="workflow_episode_flat_fallback",
            write_similarity_report=False,
        )
        variant_signatures = {
            json.dumps(segment.variant, ensure_ascii=False, sort_keys=True)
            for version in episode.versions
            for segment in version.segments
        }
        ordered_segments = all(
            [segment.order for segment in version.segments] == sorted(segment.order for segment in version.segments)
            for version in episode.versions
        )
        outputs_probe_ok = all((_probe_duration(config, path) or 0.0) > 0 for path in episode.output_paths)
        episode_ok = (
            len(episode.output_paths) == 2
            and all(path.exists() for path in episode.output_paths)
            and outputs_probe_ok
            and episode.scene_count >= 3
            and len(variant_signatures) > 2
            and ordered_segments
            and not episode.failed_segments
            and flat_episode.scene_count == 1
            and flat_episode.fallback_used
        )
        artifacts["整集重组批次"] = str(episode.batch_dir)
        artifacts["整集重组目录"] = str(config.root / DIR_EPISODE_REWORK)
        add(
            "二创成片",
            "整集切片重组",
            "ok" if episode_ok else "error",
            f"多切点输出 {len(episode.output_paths)} 条，场景 {episode.scene_count} 段；变体快照 {len(variant_signatures)} 组；零切点兜底场景 {flat_episode.scene_count} 段。",
            episode.manifest_csv,
            "检查 scene_detect 切片、逐段去重变体、按原顺序合并和零切点兜底。",
        )

        editor_status_csv = config.root / DIR_EDITOR_HANDOFF / "editor_result.csv"
        editor_status_csv.parent.mkdir(parents=True, exist_ok=True)
        with editor_status_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["output_video", "status", "title", "message"])
            writer.writeheader()
            if rendered:
                writer.writerow({"output_video": str(rendered[0]), "status": "ok", "title": "剪辑器回写样片", "message": "外部剪辑器回写验收"})
        editor_import = import_editor_result(config, editor_status_csv)
        artifacts["剪辑器回写导入报告"] = str(editor_import.report_json)
        add(
            "发布交接",
            "剪辑器回写导入",
            "ok" if editor_import.imported else "error",
            f"导入剪辑器回写 {len(editor_import.imported)} 条。",
            editor_import.report_json,
            "检查 editor_result.csv/json 的 output_video 字段。",
        )

        promote_videos_to_ready(config, rendered + editor_import.imported)
        ready = ready_videos(config)
        artifacts["合格待发布目录"] = str(config.root / DIR_READY)

        title_package = generate_title_package(config)
        artifacts["标题表"] = str(title_package["titles"])
        artifacts["标题候选表"] = str(title_package.get("title_candidates") or "")
        artifacts["发布文案"] = str(title_package["copywriting"])
        artifacts["封面候选表"] = str(title_package.get("cover_candidates") or "")
        add(
            "标题包装",
            "标题封面",
            "ok" if Path(title_package["titles"]).exists() and len(title_package["covers"]) >= len(ready) else "error",
            f"标题表已生成，封面 {len(title_package['covers'])} 张。",
            title_package["titles"],
            "检查合格待发布视频是否可抽帧。",
        )
        add(
            "标题包装",
            "标题候选与智能封面",
            "ok" if Path(title_package["title_candidates"]).exists() and title_package.get("cover_candidates") and Path(title_package["cover_candidates"]).exists() else "error",
            "标题候选表和封面候选评分表已生成。",
            title_package.get("title_candidates") or "",
            "检查标题候选生成和封面抽帧评分。",
        )

        visual = generate_visual_package(config, make_videos=make_packaged_videos, intro_seconds=0.5, outro_seconds=0.4, template="解说口播")
        artifacts["包装素材清单"] = str(visual.manifest_csv)
        artifacts["包装成片目录"] = str(config.root / DIR_PACKAGED_VIDEO)
        expected_packaged = len(ready) if make_packaged_videos else 0
        add(
            "标题包装",
            "包装成片",
            "ok" if not make_packaged_videos or len(visual.packaged_videos) >= expected_packaged else "error",
            f"海报 {len(visual.posters)} 张，包装成片 {len(visual.packaged_videos)} 条。",
            visual.manifest_csv,
            "检查 Pillow 和 FFmpeg 包装合成。",
        )
        add(
            "标题包装",
            "动态包装模板",
            "ok" if visual.items and all(item.template == "解说口播" for item in visual.items) else "error",
            f"已使用动态包装模板生成 {len(visual.items)} 条包装记录。",
            visual.manifest_csv,
            "检查包装模板参数和模板字段。",
        )

        social = generate_social_clip_package(config, SocialClipOptions(source="ready", use_bgm=True, bgm_mode="ducking"))
        artifacts["社媒增强清单"] = str(social.manifest_csv)
        add("二创成片", "社媒增强版", "ok" if social.items else "error", f"生成社媒增强版 {len(social.items)} 条。", social.output_dir)

        account_path = ensure_demo_account(config)
        queue_path = build_queue(config, source="auto", auto_account=False)
        artifacts["发布账号表"] = str(account_path)
        artifacts["发布队列"] = str(queue_path)
        add("发布交接", "发布队列", "ok" if queue_path.exists() else "error", "发布队列已生成。", queue_path)

        with queue_path.open("r", encoding="utf-8-sig", newline="") as handle:
            queue_rows = list(csv.DictReader(handle))
        status_dir = config.root / DIR_PUBLISH_STATUS
        status_dir.mkdir(parents=True, exist_ok=True)
        status_csv = status_dir / "publish_status.csv"
        with status_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["video_path", "status", "platform", "platform_url", "external_task_id", "publish_error"])
            writer.writeheader()
            if queue_rows:
                writer.writerow(
                    {
                        "video_path": queue_rows[0].get("video_path", ""),
                        "status": "published",
                        "platform": "本地验收平台",
                        "platform_url": "https://example.com/local-publish-proof",
                        "external_task_id": "verify_task_001",
                        "publish_error": "",
                    }
                )
        status_import = import_publish_status(config, status_csv, archive_published=False)
        status_import_payload = json.loads(status_import.result_path.read_text(encoding="utf-8"))
        queue_feedback_rows = _read_csv_file(feedback_records_path(config))
        queue_review_json, _queue_review_md = build_feedback_review(config)
        queue_review_payload = json.loads(queue_review_json.read_text(encoding="utf-8"))
        queue_origin_count = (queue_review_payload.get("overview") or {}).get("by_origin", {}).get("queue_import", 0)
        queue_feedback_ok = (
            status_import.updated > 0
            and status_import.log_path.exists()
            and status_import_payload.get("feedback_imported", 0) > 0
            and status_import_payload.get("unmatched_feedback") == 0
            and any(row.get("origin") == "queue_import" and row.get("publish_status") == "published" for row in queue_feedback_rows)
            and queue_origin_count > 0
        )
        artifacts["发布回写结果"] = str(status_import.result_path)
        add(
            "发布交接",
            "发布状态回写",
            "ok" if queue_feedback_ok else "error",
            f"导入发布工具回写 {status_import.imported} 条，更新 {status_import.updated} 条，汇入回流 {status_import.feedback_imported} 条。",
            status_import.result_path,
            "检查 publish_status.csv/json 的 video_path/status/platform_url 字段、queue_import 回流记录和复盘 origin 计数。",
        )

        editor_handoff = export_editor_handoff(config)
        publish_handoff = export_publish_handoff(config)
        capcut = export_capcut_draft_package(config, create_real_draft=False)
        artifacts["剪辑交接包"] = str(editor_handoff)
        artifacts["发布交接包"] = str(publish_handoff)
        artifacts["剪映草稿包"] = str(capcut.package_dir)
        add("发布交接", "剪辑交接包", "ok" if editor_handoff.exists() else "error", "本地剪辑器交接包已生成。", editor_handoff)
        add("发布交接", "发布工具交接", "ok" if publish_handoff.exists() else "error", "发布工具交接包已生成。", publish_handoff)
        add("发布交接", "剪映草稿包", "ok" if capcut.manifest_json.exists() and capcut.timeline_csv.exists() else "error", capcut.message, capcut.package_dir)

        final_inventory_csv, _final_inventory_json = write_inventory(config, with_hash=False)
        artifacts["最终素材清单"] = str(final_inventory_csv)

        rework = render_high_similarity_replacements(config)
        artifacts["高相似重做报告"] = str(rework.summary_md)
        artifacts["高相似重做目录"] = str(rework.output_dir)
        add(
            "二创成片",
            "高相似重做",
            "ok" if rework.rendered and rework.summary_md.exists() else "error",
            f"原 high 组 {rework.high_pair_count} 组，重新生成 {len(rework.rendered)} 条。",
            rework.summary_md,
            "检查去重指纹报告和 render_manifest 中的 source 映射。",
        )
        comparison_ok = False
        comparison_message = "未生成高相似重做对比报告。"
        if rework.comparison_json and rework.comparison_md and rework.comparison_json.exists() and rework.comparison_md.exists():
            try:
                comparison_payload = json.loads(rework.comparison_json.read_text(encoding="utf-8"))
                comparison_pairs = comparison_payload.get("pairs") or []
                comparison_ok = bool(comparison_payload.get("conclusion")) and bool(comparison_pairs)
                comparison_message = (
                    f"旧 high {comparison_payload.get('old_high_count', 0)} 组，"
                    f"重做后仍 high {comparison_payload.get('new_high_count', 0)} 组，"
                    f"未对齐 {comparison_payload.get('unaligned_count', 0)} 组。"
                )
            except Exception as exc:
                comparison_message = f"对比报告解析失败：{exc}"
        artifacts["高相似重做对比报告"] = str(rework.comparison_md or "")
        add(
            "二创成片",
            "高相似重做对比",
            "ok" if comparison_ok else "error",
            comparison_message,
            rework.comparison_md or "",
            "检查旧指纹报告、render_manifest source 映射和新批次指纹报告。",
        )

        archive = build_batch_archive(config, batch_dir=Path(rework.source_report).parent.parent)
        archive_payload = json.loads(archive.archive_json.read_text(encoding="utf-8"))
        rework_items = archive_payload.get("high_similarity_rework") or []
        archive_ok = (
            archive.archive_json.exists()
            and bool(archive_payload.get("rendered_videos"))
            and bool(archive_payload.get("dedup_report"))
            and any(item.get("comparison_json") or item.get("comparison_md") for item in rework_items)
        )
        artifacts["成品批次档案"] = str(archive.archive_md)
        add(
            "批次档案",
            "成品批次档案",
            "ok" if archive_ok else "error",
            f"档案含成片 {len(archive_payload.get('rendered_videos') or [])} 条，缺失项 {len(archive.missing_artifacts)} 项。",
            archive.archive_md,
            "检查批次 render_manifest、去重报告、高相似重做报告和对比报告。",
        )

        previous_mode = config.workflow_mode
        previous_threshold = config.recipe.dedup_similarity_threshold
        config.workflow_mode = "simple"
        config.recipe.dedup_similarity_threshold = 1.01
        try:
            production = produce(
                config,
                input_dir=cut.edit.selected_dir,
                copies=3,
                account_labels=["A01", "A02", "A03"],
                dedup_level="标准",
                name="产线样片",
            )
        finally:
            config.workflow_mode = previous_mode
            config.recipe.dedup_similarity_threshold = previous_threshold
        production_payload = json.loads(production.report_json.read_text(encoding="utf-8"))
        production_manifest = json.loads((production.batch_dir / "render_manifest.json").read_text(encoding="utf-8"))
        production_ready_videos = list(production.ready_dir.glob("*.mp4"))
        ready_line_dirs_before_full = {path.name for path in (config.root / DIR_READY).glob("产线_*") if path.is_dir()}
        previous_mode = config.workflow_mode
        config.workflow_mode = "full"
        try:
            full_production = produce(
                config,
                input_dir=cut.edit.selected_dir,
                copies=1,
                account_labels=["F01"],
                dedup_level="标准",
                name="完整模式产线样片",
            )
        finally:
            config.workflow_mode = previous_mode
        full_payload = json.loads(full_production.report_json.read_text(encoding="utf-8"))
        ready_line_dirs_after_full = {path.name for path in (config.root / DIR_READY).glob("产线_*") if path.is_dir()}
        full_mode_ok = (
            full_payload.get("promotion") == "skipped_full_mode"
            and full_payload.get("ready_dir") == ""
            and not full_payload.get("feedback_template")
            and full_production.production_csv.parent == full_production.batch_dir
            and ready_line_dirs_after_full == ready_line_dirs_before_full
        )
        production_ok = (
            production.assembled is not None
            and production.assembled.output_path.exists()
            and any(str(row.get("account_slot", "")).strip() for row in production_manifest)
            and bool(production_payload.get("fingerprint_report"))
            and Path(str(production_payload.get("fingerprint_report"))).exists()
            and production.production_csv.exists()
            and bool(production_ready_videos)
            and "comparison_conclusion" in production_payload
            and production_payload.get("promotion") == "promoted"
            and full_mode_ok
        )
        artifacts["一键产线报告"] = str(production.report_md)
        artifacts["一键产线合格目录"] = str(production.ready_dir)
        add(
            "生产线",
            "一键产线",
            "ok" if production_ok else "error",
            f"simple 账号 {production.account_count}，合格 {len(production_ready_videos)} 条；full 模式 promotion={full_payload.get('promotion')}，未生成产线待发布目录。",
            production.report_md,
            "检查整合成片、render_manifest account_slot、指纹报告、生产清单、产线报告和 full 模式闸门。",
        )

        feedback_template = production.feedback_template or generate_feedback_template(config, ready_dir=production.ready_dir).template_csv
        with Path(feedback_template).open("r", encoding="utf-8-sig", newline="") as handle:
            template_rows = list(csv.DictReader(handle))
        feedback_csv = production.ready_dir / "模拟发布回写.csv"
        filled_rows: list[dict[str, str]] = []
        for index, row in enumerate(template_rows[:3]):
            item = {field: row.get(field, "") for field in TEMPLATE_COLUMNS}
            item["platform"] = "本地验收平台"
            item["publish_time"] = "2026-07-04 10:00"
            item["publish_url"] = f"https://example.com/feedback/{index + 1}"
            item["note"] = "业务流程验收"
            if index == 0:
                item["publish_status"] = "published"
                item["play_count"] = "128"
                item["duplicate_flag"] = "否"
            elif index == 1:
                item["publish_status"] = "雷同"
                item["play_count"] = "0"
                item["duplicate_flag"] = "是"
                item["fail_reason"] = "平台判雷同"
            else:
                item["publish_status"] = "飞天"
                item["play_count"] = "8"
                item["duplicate_flag"] = "否"
            filled_rows.append(item)
        with feedback_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TEMPLATE_COLUMNS)
            writer.writeheader()
            writer.writerows(filled_rows)
        feedback_import = import_feedback(config, feedback_csv)
        feedback_payload = json.loads(feedback_import.review_json.read_text(encoding="utf-8"))
        feedback_record_rows = _read_csv_file(feedback_import.records_csv)
        feedback_template_record_count = sum(1 for row in feedback_record_rows if row.get("origin") == "feedback_template")
        duplicate_rows = feedback_payload.get("duplicate_association_rows") or []
        duplicate_similarity = duplicate_rows[0].get("batch_max_similarity") if duplicate_rows else None
        legacy_root = config.root / "_legacy_origin_check"
        legacy_config = ProjectConfig(project_name="legacy_origin_check", project_root=str(legacy_root), drama_name="legacy_origin_check")
        legacy_records = feedback_records_path(legacy_config)
        legacy_records.parent.mkdir(parents=True, exist_ok=True)
        legacy_fieldnames = TEMPLATE_COLUMNS + ["import_id", "matched_path"]
        legacy_row = {field: "" for field in legacy_fieldnames}
        legacy_row.update(
            {
                "file": "legacy_queue.mp4",
                "platform": "历史平台",
                "publish_status": "published",
                "publish_time": "2026-07-04 10:00",
                "import_id": "20260704_100000",
                "matched_path": "legacy_queue.mp4",
            }
        )
        with legacy_records.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=legacy_fieldnames)
            writer.writeheader()
            writer.writerow(legacy_row)
        legacy_review_json, _legacy_review_md = build_feedback_review(legacy_config)
        legacy_payload = json.loads(legacy_review_json.read_text(encoding="utf-8"))
        legacy_origin_ok = (legacy_payload.get("overview") or {}).get("by_origin", {}).get("feedback_template", 0) == 1
        feedback_archive = build_batch_archive(config, batch_dir=production.batch_dir)
        feedback_archive_payload = json.loads(feedback_archive.archive_json.read_text(encoding="utf-8"))
        feedback_ok = (
            feedback_import.imported == 2
            and len(feedback_import.invalid) == 1
            and feedback_import.records_csv.exists()
            and feedback_template_record_count >= 2
            and len(duplicate_rows) == 1
            and isinstance(duplicate_similarity, (int, float))
            and feedback_payload.get("calibration_suggestion") == INSUFFICIENT_SAMPLE_MESSAGE
            and bool(feedback_archive_payload.get("publish_status_rows"))
            and legacy_origin_ok
        )
        artifacts["发布回流模板"] = str(feedback_template)
        artifacts["发布回流复盘"] = str(feedback_import.review_md)
        add(
            "发布回流",
            "回写与复盘",
            "ok" if feedback_ok else "error",
            "导入 2/3，非法 1，复盘就绪",
            feedback_import.review_md,
            "检查回流模板、发布回写记录、雷同关联表、样本不足建议、批次档案发布状态和无 origin 历史记录兼容。",
        )

        production_rows = _read_csv_file(production.production_csv)
        assistant_before_rows = _read_csv_file(feedback_records_path(config))
        assistant_before_count = sum(1 for row in assistant_before_rows if row.get("origin") == ORIGIN_ASSISTANT)
        assistant_session = create_session(config, ready_dir=production.ready_dir)
        assistant_files_ok = all(item.file.exists() for item in assistant_session.items)
        assistant_fail_reason = "验收模拟失败原因"
        if len(assistant_session.items) >= 3:
            mark_item(config, assistant_session, assistant_session.items[0].index, STATUS_PUBLISHED)
            mark_item(config, assistant_session, assistant_session.items[1].index, STATUS_FAILED, fail_reason=assistant_fail_reason)
            mark_item(config, assistant_session, assistant_session.items[2].index, STATUS_SKIPPED)
        assistant_session_payload = json.loads(session_path(assistant_session).read_text(encoding="utf-8"))
        assistant_after_rows = _read_csv_file(feedback_records_path(config))
        assistant_rows = [row for row in assistant_after_rows if row.get("origin") == ORIGIN_ASSISTANT]
        assistant_new_rows = assistant_rows[assistant_before_count:]
        skipped_file = assistant_session.items[2].file.name if len(assistant_session.items) >= 3 else ""
        assistant_summary = finish_session(config, assistant_session)
        assistant_review_payload = json.loads(assistant_summary.review_json.read_text(encoding="utf-8"))
        assistant_origin_count = (assistant_review_payload.get("overview") or {}).get("by_origin", {}).get(ORIGIN_ASSISTANT, 0)

        interrupted_session = create_session(config, ready_dir=production.ready_dir)
        interrupted_path = session_path(interrupted_session)
        interrupted_before = interrupted_path.read_text(encoding="utf-8")
        interrupted_tmp = interrupted_path.with_name(f".{interrupted_path.name}.interrupted.tmp")
        interrupted_tmp.write_text('{"session_id": "interrupted"', encoding="utf-8")
        resumed_session = resume_session(config, production.ready_dir)
        resumed_counts = session_counts(resumed_session)
        interrupted_after = interrupted_path.read_text(encoding="utf-8")
        reloaded_after_interruption = load_session(interrupted_path)
        atomic_session_ok = (
            interrupted_tmp.exists()
            and interrupted_after == interrupted_before
            and reloaded_after_interruption.session_id == interrupted_session.session_id
            and resumed_session.session_id == interrupted_session.session_id
        )
        assistant_cursor_expected = 3 if len(assistant_session.items) > 3 else len(assistant_session.items)
        assistant_ok = (
            len(assistant_session.items) == len(production_rows)
            and len(assistant_session.items) >= 3
            and assistant_files_ok
            and assistant_session_payload.get("cursor") == assistant_cursor_expected
            and assistant_session_payload.get("items", [])[0].get("status") == STATUS_PUBLISHED
            and assistant_session_payload.get("items", [])[1].get("status") == STATUS_FAILED
            and assistant_session_payload.get("items", [])[2].get("status") == STATUS_SKIPPED
            and len(assistant_new_rows) == 2
            and all(row.get("origin") == ORIGIN_ASSISTANT for row in assistant_new_rows)
            and any(row.get("publish_status") == STATUS_PUBLISHED for row in assistant_new_rows)
            and any(row.get("publish_status") == STATUS_FAILED and assistant_fail_reason in row.get("note", "") for row in assistant_new_rows)
            and not any(row.get("file") == skipped_file for row in assistant_new_rows)
            and assistant_summary.published == 1
            and assistant_summary.failed == 1
            and assistant_summary.skipped == 1
            and assistant_origin_count >= 2
            and resumed_session.session_id == interrupted_session.session_id
            and resumed_counts.get(STATUS_PENDING, 0) == len(interrupted_session.items)
            and atomic_session_ok
        )
        artifacts["发布助手会话"] = str(session_path(assistant_session))
        artifacts["发布助手续发会话"] = str(interrupted_path)
        artifacts["发布助手中断临时文件"] = str(interrupted_tmp)
        add(
            "发布助手",
            "引导发布会话",
            "ok" if assistant_ok else "error",
            f"会话 {len(assistant_session.items)} 条，回流新增 {len(assistant_new_rows)} 条，续发 pending {resumed_counts.get(STATUS_PENDING, 0)} 条，中断保护 {'通过' if atomic_session_ok else '失败'}。",
            session_path(assistant_session),
            "检查发布助手会话 JSON、publish_assistant 回流记录、失败原因 note、跳过不回流、续发读取和原子写盘中断保护。",
        )

    except Exception as exc:
        add("验收执行", "流程中断", "error", str(exc), project_root, "根据异常定位对应流程模块。")

    return _finish(version, project_root, checks, artifacts)


def write_workflow_verification_report(
    work_root: str | Path | None = None,
    output_dir: str | Path | None = None,
    make_packaged_videos: bool = True,
) -> WorkflowVerificationFiles:
    version = default_version_root()
    report = verify_workflow(work_root=work_root, make_packaged_videos=make_packaged_videos)
    target = Path(output_dir) if output_dir else version / "docs" / "workflow_verification"
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    markdown_path = target / f"业务流程验收_{stamp}.md"
    json_path = target / f"业务流程验收_{stamp}.json"
    csv_path = target / f"业务流程验收_{stamp}.csv"

    json_path.write_text(json.dumps(asdict(report), ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(report), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["group", "item", "level", "message", "evidence", "fix"])
        writer.writeheader()
        for check in report.checks:
            writer.writerow(asdict(check))

    latest = target / "业务流程验收_latest.md"
    latest.write_text(_markdown(report), encoding="utf-8")
    return WorkflowVerificationFiles(report, markdown_path, json_path, csv_path)


def format_workflow_verification_report(report: WorkflowVerificationReport) -> str:
    label = LEVEL_LABELS.get(report.overall_level, report.overall_level)
    lines = [
        f"业务流程验收：{report.app_name}",
        f"总体状态：{label}",
        f"通过 {report.counts['ok']} 项，提醒 {report.counts['warn']} 项，需处理 {report.counts['error']} 项，总计 {report.counts['total']} 项。",
        f"验收项目：{report.project_root}",
    ]
    issues = [check for check in report.checks if check.level != "ok"]
    if issues:
        lines.append("优先处理：")
        for check in issues[:8]:
            lines.append(f"- {check.group}/{check.item}: {check.message} {check.fix}".strip())
    return "\n".join(lines)


def _finish(version: Path, project_root: Path, checks: list[WorkflowCheck], artifacts: dict[str, str]) -> WorkflowVerificationReport:
    counts = {
        "ok": sum(1 for check in checks if check.level == "ok"),
        "warn": sum(1 for check in checks if check.level == "warn"),
        "error": sum(1 for check in checks if check.level == "error"),
        "total": len(checks),
    }
    overall = "error" if counts["error"] else ("warn" if counts["warn"] else "ok")
    return WorkflowVerificationReport(
        app_name=APP_NAME,
        generated_at=datetime.now().isoformat(timespec="seconds"),
        version_root=str(version),
        project_root=str(project_root),
        overall_level=overall,
        counts=counts,
        artifacts=artifacts,
        checks=checks,
    )


def _create_sample_assets(config) -> dict[str, Path]:
    source = config.root / DIR_A / "source_demo.mp4"
    auto_source = config.root / DIR_AUTO_EDIT_INPUT / "source_demo.mp4"
    background = config.root / DIR_B / "blue_motion_background.mp4"
    bgm = config.root / DIR_AUDIO / "steady_bgm.m4a"
    sticker = config.root / DIR_C / "brand_mark.png"
    script = config.root / DIR_SCRIPT / "source_demo.srt"
    for path in [source.parent, auto_source.parent, background.parent, bgm.parent, sticker.parent, script.parent]:
        path.mkdir(parents=True, exist_ok=True)

    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=720x1280:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=44100",
            "-t",
            "20",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(source),
        ],
        "生成样本原始视频",
    )
    shutil.copy2(source, auto_source)

    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x12335d:size=1080x1920:rate=30",
            "-t",
            "20",
            "-vf",
            "noise=alls=12:allf=t+u,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "24",
            str(background),
        ],
        "生成样本背景视频",
    )

    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=48000",
            "-t",
            "20",
            "-c:a",
            "aac",
            str(bgm),
        ],
        "生成样本 BGM",
    )
    _write_sticker(sticker)
    script.write_text(
        "\n".join(
            [
                "1",
                "00:00:01,000 --> 00:00:05,000",
                "女主发现关键证据，冲突马上爆发。",
                "",
                "2",
                "00:00:07,000 --> 00:00:12,000",
                "对方突然反转，现场情绪升高。",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {"原始视频": source, "自动剪辑输入": auto_source, "背景视频": background, "BGM": bgm, "贴图": sticker, "字幕样本": script}


def _create_episode_rework_assets(config: ProjectConfig) -> dict[str, Path]:
    root = config.root / "_验收_整集重组"
    root.mkdir(parents=True, exist_ok=True)
    scene_source = root / "episode_scene_blocks.mp4"
    flat_source = root / "episode_flat.mp4"
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    color_inputs: list[str] = []
    for color in ["red", "green", "blue", "yellow"]:
        color_inputs.extend(["-f", "lavfi", "-t", "1.200", "-i", f"color=c={color}:size=720x1280:rate=30"])
    _run(
        [
            ffmpeg,
            "-y",
            *color_inputs,
            "-filter_complex",
            "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0,format=yuv420p[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "24",
            str(scene_source),
        ],
        "生成整集重组多切点样本",
    )
    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x2456a6:size=720x1280:rate=30",
            "-t",
            "2.400",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
            str(flat_source),
        ],
        "生成整集重组零切点样本",
    )
    return {"多切点整集": scene_source, "零切点整集": flat_source}


def _create_dub_sync_assets(config: ProjectConfig) -> dict[str, Path]:
    root = config.root / "_验收_配音对齐"
    audio_dir = root / "配音包"
    video_dir = root / "分镜视频"
    fallback_audio_dir = root / "兜底配音包"
    fallback_video_dir = root / "兜底分镜视频"
    for path in [audio_dir, video_dir, fallback_audio_dir, fallback_video_dir]:
        path.mkdir(parents=True, exist_ok=True)

    cases = [
        ("001_trim", 3.0, 2.0, 440, "视频长于配音，预期裁尾。"),
        ("002_slow", 2.0, 2.2, 550, "视频略短，预期放慢。"),
        ("003_freeze", 1.0, 2.0, 660, "视频明显短，预期末帧定格。"),
    ]
    manifest_rows: list[dict[str, str]] = []
    for index, (stem, video_seconds, audio_seconds, frequency, text) in enumerate(cases, start=1):
        video = video_dir / f"{stem}.mp4"
        audio = audio_dir / f"{stem}.wav"
        _make_dub_sample_video(config, video, video_seconds)
        _make_dub_sample_wav(config, audio, audio_seconds, frequency)
        manifest_rows.append(
            {
                "index": str(index),
                "text": text,
                "audio_file": audio.name,
                "duration_seconds": f"{audio_seconds:.3f}",
                "voice_id": "verify_voice",
                "engine": "mock",
            }
        )

    manifest = audio_dir / "配音清单.csv"
    with manifest.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["index", "text", "audio_file", "duration_seconds", "voice_id", "engine"])
        writer.writeheader()
        writer.writerows(manifest_rows)

    _make_dub_sample_video(config, fallback_video_dir / "001_fallback.mp4", 1.0)
    _make_dub_sample_wav(config, fallback_audio_dir / "001_fallback.wav", 1.0, 770)
    return {
        "配音包": audio_dir,
        "分镜视频": video_dir,
        "兜底配音包": fallback_audio_dir,
        "兜底分镜视频": fallback_video_dir,
    }


def _make_dub_sample_video(config: ProjectConfig, output: Path, duration: float) -> None:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=720x1280:rate=30",
            "-t",
            f"{duration:.3f}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "24",
            "-pix_fmt",
            "yuv420p",
            str(output),
        ],
        "生成配音对齐样本视频",
    )


def _make_dub_sample_wav(config: ProjectConfig, output: Path, duration: float, frequency: int) -> None:
    ffmpeg = config.tools.ffmpeg or "ffmpeg"
    _run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:sample_rate=44100",
            "-t",
            f"{duration:.3f}",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        "生成配音对齐样本 WAV",
    )


def _write_sticker(output: Path) -> None:
    try:
        from PIL import Image, ImageDraw
    except Exception as exc:  # pragma: no cover - packaged release includes Pillow
        raise RuntimeError("缺少 Pillow，无法生成验收贴图。") from exc

    image = Image.new("RGBA", (460, 140), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rounded_rectangle((0, 0, 460, 140), radius=34, fill=(24, 119, 242, 220), outline=(255, 255, 255, 180), width=4)
    draw.text((44, 44), "SHUIXING CUT", fill=(255, 255, 255, 255))
    output.parent.mkdir(parents=True, exist_ok=True)
    image.save(output)


def _run(command: list[str], label: str) -> None:
    completed = run_silent(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        detail = completed.stderr[-3000:] if completed.stderr else completed.stdout[-3000:]
        raise RuntimeError(f"{label}失败：{detail.strip() or '未知错误'}")


def _read_csv_file(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _markdown(report: WorkflowVerificationReport) -> str:
    lines = [
        f"# 业务流程验收 - {report.app_name}",
        "",
        f"- 生成时间：{report.generated_at}",
        f"- 版本目录：`{report.version_root}`",
        f"- 验收项目：`{report.project_root}`",
        f"- 总体状态：**{LEVEL_LABELS.get(report.overall_level, report.overall_level)}**",
        f"- 通过：{report.counts['ok']}，提醒：{report.counts['warn']}，需处理：{report.counts['error']}，总数：{report.counts['total']}",
        "",
        "## 关键产物",
        "",
    ]
    for key, value in report.artifacts.items():
        lines.append(f"- {key}：`{value}`")
    lines.extend(
        [
            "",
            "## 检查明细",
            "",
            "| 分组 | 项目 | 状态 | 说明 | 证据 | 修复建议 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for check in report.checks:
        lines.append(
            f"| {check.group} | {check.item} | {LEVEL_LABELS.get(check.level, check.level)} | {check.message} | `{check.evidence}` | {check.fix or '-'} |"
        )
    lines.append("")
    return "\n".join(lines)
