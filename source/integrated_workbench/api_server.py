"""水星剪辑对外 HTTP API：给第三方系统编程接入核心生产能力。

设计约束（与全局口径一致）：
- 仅用 Python 标准库（http.server），不引入 Flask/FastAPI，契合离线 wheelhouse 与轻量安装策略。
- 长任务（自动剪辑/配音对齐/矩阵生产/整集重组）异步执行：POST 立即返回 job_id，
  通过 GET /api/jobs/{id} 轮询百分比进度（复用引擎 progress(done,total) 回调）。
- 鉴权：Authorization: Bearer <token>，token 由启动参数或环境变量 MERCURY_API_TOKEN 提供；
  未显式提供时启动阶段自动生成并打印，绝不以“无鉴权”方式对外开放。
- 默认仅绑定 127.0.0.1（本机接入）。需要局域网第三方接入时显式指定 --host 0.0.0.0，
  风险自负，请配合防火墙与强 token。

不改变 APP ID、授权模式与既有引擎行为；本模块只是既有能力的一层薄封装。
"""
from __future__ import annotations

import csv
import hmac
import json
import hashlib
import secrets
import tempfile
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

from .models import DIR_A, DIR_AUDIO, DIR_AUTO_EDIT_INPUT, DIR_B, DIR_C, DIR_SCRIPT
from .product_identity import APP_ID_MODE, software_app_id
from .project import (
    background_videos,
    create_project,
    load_config,
    ready_videos,
    source_videos,
    stickers,
)

API_VERSION = "1.2"

# 跨机部署上传上限（2GB），流式落盘，避免整块读入内存。
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024
_UPLOAD_CHUNK = 1024 * 1024

# 素材类别 → 项目内落盘子目录：调用方只需给 kind，无需了解目录结构。
KIND_DIRS: dict[str, str] = {
    "source": DIR_A,
    "background": DIR_B,
    "sticker": DIR_C,
    "audio": DIR_AUDIO,
    "auto_edit_input": DIR_AUTO_EDIT_INPUT,
    "script": DIR_SCRIPT,
}

# 完成任务在内存中的保留时长与上限：防止 _jobs 只增不减。
JOB_TTL_SECONDS = 6 * 3600
JOB_MAX_KEEP = 500


# ---------------------------------------------------------------------------
# JSON 序列化：Path/集合/日期统一转可序列化结构
# ---------------------------------------------------------------------------
def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


# ---------------------------------------------------------------------------
# 异步任务模型
# ---------------------------------------------------------------------------
@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"  # queued / running / succeeded / failed
    done: int = 0
    total: int = 0
    percent: int = 0
    result: Any = None
    error: str = ""
    created_at: str = ""
    started_at: str = ""
    finished_at: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "status": self.status,
            "done": self.done,
            "total": self.total,
            "percent": self.percent,
            "result": _jsonable(self.result),
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobRegistry:
    """线程安全的任务表：提交后台线程执行，进度回调回写百分比。

    并发治理：
    - max_workers 全局信号量限制整机同时运行的任务数（默认 2，适配 FFmpeg 重负载）；
    - 同一项目的任务串行化（per-project 锁），避免并发写坏共享项目目录；不同项目并行。
    - 完成任务按 TTL + 上限裁剪，避免 _jobs 只增不减。
    """

    def __init__(self, max_workers: int = 2) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._project_locks: dict[str, threading.Lock] = {}
        self._slots = threading.Semaphore(max_workers) if max_workers and max_workers > 0 else None

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _project_lock(self, key: str) -> threading.Lock:
        with self._lock:
            lock = self._project_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._project_locks[key] = lock
            return lock

    def _prune(self) -> None:
        """在锁内调用：清理超龄已完成任务，并对总量封顶。"""
        now = datetime.now()
        finished = [
            (jid, job)
            for jid, job in self._jobs.items()
            if job.status in {"succeeded", "failed"} and job.finished_at
        ]
        for jid, job in finished:
            try:
                age = (now - datetime.fromisoformat(job.finished_at)).total_seconds()
            except ValueError:
                age = 0
            if age > JOB_TTL_SECONDS:
                self._jobs.pop(jid, None)
        if len(self._jobs) > JOB_MAX_KEEP:
            done_oldest = sorted(
                (j for j in self._jobs.values() if j.status in {"succeeded", "failed"}),
                key=lambda j: j.finished_at or j.created_at,
            )
            for job in done_oldest[: len(self._jobs) - JOB_MAX_KEEP]:
                self._jobs.pop(job.id, None)

    def submit(
        self,
        kind: str,
        action: Callable[[Callable[[int, int], None]], Any],
        serialize_key: str = "",
    ) -> Job:
        """action 接收一个 progress(done,total) 回调，返回可 JSON 化的结果。

        serialize_key 非空时，同一 key（通常是项目路径）的任务串行执行。
        """
        job = Job(id=uuid.uuid4().hex, kind=kind, created_at=self._now())
        with self._lock:
            self._prune()
            self._jobs[job.id] = job

        def progress(done: int, total: int) -> None:
            with self._lock:
                job.done = int(done)
                job.total = int(total)
                job.percent = int(done * 100 / total) if total and total > 0 else 0

        def run_action() -> None:
            with self._lock:
                job.status = "running"
                job.started_at = self._now()
            try:
                result = action(progress)
                with self._lock:
                    job.result = result
                    job.status = "succeeded"
                    if job.total <= 0:
                        job.total = job.done = 1
                    job.percent = 100
                    job.finished_at = self._now()
            except Exception as exc:  # noqa: BLE001 —— 任务异常统一收敛为 failed 状态
                with self._lock:
                    job.status = "failed"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.finished_at = self._now()

        def worker() -> None:
            proj_lock = self._project_lock(serialize_key) if serialize_key else None
            if proj_lock is not None:
                proj_lock.acquire()
            try:
                if self._slots is not None:
                    self._slots.acquire()
                try:
                    run_action()
                finally:
                    if self._slots is not None:
                        self._slots.release()
            finally:
                if proj_lock is not None:
                    proj_lock.release()

        threading.Thread(target=worker, name=f"api-job-{kind}", daemon=True).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.snapshot() for job in self._jobs.values()]


# ---------------------------------------------------------------------------
# 结果序列化：把各引擎 dataclass 结果转成精简 JSON
# ---------------------------------------------------------------------------
def _serialize_produce(result: Any) -> dict[str, Any]:
    return {
        "input_source": _jsonable(result.input_source),
        "batch_dir": _jsonable(result.batch_dir),
        "ready_dir": _jsonable(result.ready_dir),
        "production_csv": _jsonable(result.production_csv),
        "report_json": _jsonable(result.report_json),
        "report_md": _jsonable(result.report_md),
        "rendered_count": len(result.rendered),
        "ready_count": len(result.ready_videos),
        "ready_videos": _jsonable(result.ready_videos),
        "account_count": result.account_count,
        "remaining_high_count": result.remaining_high_count,
        "comparison_conclusion": result.comparison_conclusion,
        "feedback_template": _jsonable(result.feedback_template),
    }


def _serialize_dub(result: Any) -> dict[str, Any]:
    successful = sum(1 for seg in result.segments if getattr(seg, "segment_file", None))
    return {
        "output_path": _jsonable(result.output_path),
        "plan_csv": _jsonable(result.plan_csv),
        "plan_json": _jsonable(result.plan_json),
        "segment_count": len(result.segments),
        "successful": successful,
        "total_seconds": round(float(result.total_seconds), 3),
        "unmatched_audio": _jsonable(result.unmatched_audio),
        "unmatched_video": _jsonable(result.unmatched_video),
    }


def _serialize_auto_edit(result: Any, strategy: str = "") -> dict[str, Any]:
    return {
        "strategy": strategy,
        "work_dir": _jsonable(result.work_dir),
        "selected_dir": _jsonable(result.selected_dir),
        "preview_path": _jsonable(result.preview_path),
        "plan_csv": _jsonable(result.plan_csv),
        "import_csv": _jsonable(result.import_csv),
        "handoff_json": _jsonable(result.handoff_json),
        "selected_count": result.selected_count,
        "total_seconds": round(float(result.total_seconds), 3),
    }


def _serialize_episode(result: Any) -> dict[str, Any]:
    return {
        "batch_dir": _jsonable(result.batch_dir),
        "source_video": _jsonable(result.source_video),
        "audio_mode": result.audio_mode,
        "split_dir": _jsonable(result.split_dir),
        "output_paths": _jsonable(result.output_paths),
        "version_count": len(result.versions),
        "manifest_csv": _jsonable(result.manifest_csv),
        "manifest_json": _jsonable(result.manifest_json),
        "failed_segments": _jsonable(result.failed_segments),
        "dedup_report_json": _jsonable(result.dedup_report_json),
        "dedup_report_markdown": _jsonable(result.dedup_report_markdown),
    }


# ---------------------------------------------------------------------------
# 产物文件收集：从任务结果里挑出真实存在的成品文件，供跨机下载
# ---------------------------------------------------------------------------
def _collect_result_files(result: Any) -> dict[str, Path]:
    """递归遍历结果对象里的字符串路径，登记真实存在的文件，按文件名索引。

    只有登记过的文件才允许下载，天然规避路径穿越（下载按 name 精确匹配，不拼接用户输入）。
    """
    files: dict[str, Path] = {}

    def walk(value: Any) -> None:
        if isinstance(value, str):
            path = Path(value)
            try:
                if path.is_file():
                    files.setdefault(path.name, path)
            except OSError:
                pass
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)

    walk(result)
    return files


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_UPLOAD_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _enum_payload() -> dict[str, Any]:
    from .auto_cut import STRATEGIES
    from .visual_package import packaging_template_names

    return {
        "strategies": list(STRATEGIES),
        "dedup_level": ["标准", "增强", "light", "balanced", "strong"],
        "audio_mode": ["keep_original", "replace_track"],
        "trim_anchor": ["head", "center", "tail"],
        "upload_kinds": list(KIND_DIRS.keys()),
        "dub_aspects": [
            {"label": "9:16 竖屏", "width": 1080, "height": 1920},
            {"label": "16:9 横屏", "width": 1920, "height": 1080},
            {"label": "1:1 方形", "width": 1080, "height": 1080},
            {"label": "4:3", "width": 1440, "height": 1080},
            {"label": "3:4", "width": 1080, "height": 1440},
        ],
        "render_mode": ["vertical", "horizontal"],
        "packaging_templates": packaging_template_names(),
        "social_sources": ["ready", "intermediate"],
        "social_ratios": ["9:16"],
        "bgm_modes": ["ducking", "background"],
        "transcribe_engines": ["auto", "whisper", "timeline"],
    }


# ---------------------------------------------------------------------------
# 业务错误：用于同步阶段的参数校验（映射为 4xx）
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _require(params: dict[str, Any], key: str) -> Any:
    value = params.get(key)
    if value in (None, ""):
        raise ApiError(400, f"缺少必填参数：{key}")
    return value


def _load_project(params: dict[str, Any]):
    project = _require(params, "project")
    try:
        return load_config(project)
    except FileNotFoundError as exc:
        raise ApiError(400, str(exc)) from exc


# ---------------------------------------------------------------------------
# 各业务动作：返回 (kind, action)；action 接收 progress 回调
# ---------------------------------------------------------------------------
def _build_produce(params: dict[str, Any]):
    from .production_line import produce

    config = _load_project(params)
    labels = params.get("account_labels") or None
    if isinstance(labels, str):
        labels = [item.strip() for item in labels.split(",") if item.strip()] or None

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        result = produce(
            config,
            source=params.get("source"),
            plan=params.get("plan"),
            input_dir=params.get("input_dir"),
            copies=int(params.get("copies", 3)),
            account_labels=labels,
            dedup_level=str(params.get("dedup_level", "标准")),
            name=params.get("name") or None,
            progress=progress,
        )
        return _serialize_produce(result)

    return "produce", action


def _build_dub(params: dict[str, Any]):
    from .dub_sync import dub_assemble

    config = _load_project(params)
    _require(params, "audio_dir")
    _require(params, "video_dir")
    width = params.get("width")
    height = params.get("height")
    if width and height:
        config.edit.output_width = int(width)
        config.edit.output_height = int(height)

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        result = dub_assemble(
            config,
            audio_dir=Path(params["audio_dir"]),
            video_dir=Path(params["video_dir"]),
            name=params.get("name") or None,
            trim_anchor=str(params.get("trim_anchor", "head")),
            burn_subtitle=bool(params.get("subtitle", False)),
            keep_segments=bool(params.get("keep_segments", False)),
            progress=progress,
        )
        return _serialize_dub(result)

    return "dub-assemble", action


def _build_auto_edit(params: dict[str, Any]):
    from .auto_cut import STRATEGIES, AutoCutSettings, run_strategy_auto_edit
    from .editing_engine import run_auto_edit

    config = _load_project(params)
    strategy = str(params.get("strategy", "") or "")
    if strategy and strategy not in STRATEGIES:
        raise ApiError(400, f"未知自动剪辑策略：{strategy}；可选：{'、'.join(STRATEGIES)}")
    make_preview = bool(params.get("make_preview", True))
    input_dir = params.get("input_dir")

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        if strategy:
            settings = AutoCutSettings(
                strategy=strategy,
                target_seconds=float(params.get("target_seconds", config.edit.target_seconds)),
                clip_seconds=float(params.get("clip_seconds", config.edit.clip_seconds)),
            )
            if params.get("keywords"):
                settings.keywords = str(params["keywords"])
            sr = run_strategy_auto_edit(
                config, settings, input_dir=input_dir, make_preview=make_preview, progress=progress
            )
            return _serialize_auto_edit(sr.edit, strategy=sr.strategy)
        result = run_auto_edit(
            config,
            input_dir=input_dir,
            target_seconds=params.get("target_seconds"),
            clip_seconds=params.get("clip_seconds"),
            output_name=params.get("name") or None,
            make_preview=make_preview,
            progress=progress,
        )
        return _serialize_auto_edit(result)

    return "auto-edit", action


def _build_episode(params: dict[str, Any]):
    from .episode_pipeline import episode_rework

    config = _load_project(params)
    _require(params, "video")

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        result = episode_rework(
            config,
            video=params["video"],
            versions=int(params.get("versions", 3)),
            audio_mode=str(params.get("audio_mode", "keep_original")),
            replacement_audio=params.get("replacement_audio"),
            dedup_level=params.get("dedup_level"),
            name=params.get("name") or None,
            keep_segments=bool(params.get("keep_segments", False)),
            progress=progress,
        )
        return _serialize_episode(result)

    return "episode-rework", action


# ---------------------------------------------------------------------------
# 二期扩展动作（P0）
# ---------------------------------------------------------------------------
def _write_plan_csv(rows: list[dict[str, Any]]) -> str:
    """把 JSON 传入的整合清单写成临时 CSV（字段 order,file,start,end），返回路径。"""
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="assemble_plan_")
    import os as _os

    _os.close(fd)
    with open(path, "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["order", "file", "start", "end"])
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in ["order", "file", "start", "end"]})
    return path


def _build_assemble(params: dict[str, Any]):
    from .assemble import assemble_clips

    config = _load_project(params)
    plan = params.get("plan")
    plan_rows = params.get("plan_rows")
    if plan_rows is not None:
        if not isinstance(plan_rows, list):
            raise ApiError(400, "plan_rows 必须是对象数组")
        plan = _write_plan_csv(plan_rows)
    if not plan and not params.get("input_dir"):
        raise ApiError(400, "assemble 需要 plan / plan_rows 或 input_dir 之一")

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        result = assemble_clips(
            config,
            plan=plan,
            input_dir=params.get("input_dir"),
            name=params.get("name") or None,
            keep_segments=bool(params.get("keep_segments", False)),
        )
        return {
            "output_path": _jsonable(result.output_path),
            "manifest_csv": _jsonable(result.manifest_csv),
            "manifest_json": _jsonable(result.manifest_json),
            "item_count": len(result.items),
            "total_seconds": round(float(result.total_seconds), 3),
        }

    return "assemble", action


def _build_render(params: dict[str, Any]):
    from .models import VIDEO_EXTENSIONS
    from .project import iter_media
    from .video_engine import render_project

    config = _load_project(params)
    mode = str(params.get("mode", "") or "")
    if mode:
        if mode not in {"vertical", "horizontal"}:
            raise ApiError(400, "mode 只能是 vertical 或 horizontal")
        config.recipe.mode = mode
    source_files = None
    source_label = "A 原始视频"
    source_dir = params.get("source_dir")
    if source_dir:
        directory = Path(source_dir)
        if not directory.is_absolute() and not directory.exists():
            directory = config.root / directory
        source_files = iter_media(directory, VIDEO_EXTENSIONS)
        source_label = "镜头切片素材"
        if not source_files:
            raise ApiError(400, f"指定素材目录没有视频文件：{directory}")

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        outputs = render_project(
            config,
            copies=params.get("copies"),
            source_files=source_files,
            source_label=source_label,
            progress=progress,
        )
        return {"rendered_count": len(outputs), "rendered": _jsonable(outputs)}

    return "render", action


def _build_titles(params: dict[str, Any]):
    from .title_engine import generate_title_package

    config = _load_project(params)

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        result = generate_title_package(config, make_covers=not bool(params.get("no_covers", False)))
        return {
            "titles": _jsonable(result.get("titles")),
            "title_candidates": _jsonable(result.get("title_candidates")),
            "copywriting": _jsonable(result.get("copywriting")),
            "covers": _jsonable(result.get("covers")),
            "cover_candidates": _jsonable(result.get("cover_candidates")),
            "cover_count": len(result.get("covers") or []),
        }

    return "titles", action


def _build_social(params: dict[str, Any]):
    from .social_clipper import SocialClipOptions, generate_social_clip_package

    config = _load_project(params)
    options = SocialClipOptions(
        source=str(params.get("source", "ready")),
        ratio=str(params.get("ratio", "9:16")),
        use_bgm=not bool(params.get("no_bgm", False)),
        bgm_mode=str(params.get("bgm_mode", "ducking")),
    )

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        result = generate_social_clip_package(config, options)
        return {
            "output_dir": _jsonable(result.output_dir),
            "manifest_csv": _jsonable(result.manifest_csv),
            "manifest_json": _jsonable(result.manifest_json),
            "item_count": len(result.items),
        }

    return "social-clips", action


def _build_visual(params: dict[str, Any]):
    from .visual_package import DEFAULT_TEMPLATE, generate_visual_package, packaging_template_names

    config = _load_project(params)
    template = str(params.get("template", DEFAULT_TEMPLATE) or DEFAULT_TEMPLATE)
    if template not in packaging_template_names():
        raise ApiError(400, f"未知包装模板：{template}；可选：{'、'.join(packaging_template_names())}")

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        result = generate_visual_package(
            config,
            make_videos=bool(params.get("videos", False)),
            intro_seconds=float(params.get("intro_seconds", 1.4)),
            outro_seconds=float(params.get("outro_seconds", 1.0)),
            template=template,
        )
        return {
            "manifest_csv": _jsonable(result.manifest_csv),
            "manifest_json": _jsonable(result.manifest_json),
            "posters": _jsonable(result.posters),
            "cards": _jsonable(result.cards),
            "card_videos": _jsonable(result.card_videos),
            "packaged_videos": _jsonable(result.packaged_videos),
            "poster_count": len(result.posters),
            "packaged_count": len(result.packaged_videos),
        }

    return "visual-package", action


# ---------------------------------------------------------------------------
# 二期扩展动作（P1）
# ---------------------------------------------------------------------------
def _build_detect_scenes(params: dict[str, Any]):
    from .scene_detect import detect_scenes

    config = _load_project(params)
    video = _require(params, "video")
    threshold = params.get("threshold")

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        result = detect_scenes(
            config,
            video=Path(video),
            threshold=float(threshold) if threshold is not None else None,
            min_scene_seconds=float(params.get("min_scene_seconds", 0.5)),
        )
        return {
            "video": _jsonable(result.video),
            "engine": result.engine,
            "engine_detail": result.engine_detail,
            "fallback_reason": result.fallback_reason,
            "threshold": result.threshold,
            "scene_count": len(result.scenes),
            "csv_path": _jsonable(result.csv_path),
            "json_path": _jsonable(result.json_path),
        }

    return "detect-scenes", action


def _build_split_scenes(params: dict[str, Any]):
    from .scene_detect import split_scenes_to_clips

    config = _load_project(params)
    video = _require(params, "video")

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        result = split_scenes_to_clips(
            config,
            video=Path(video),
            min_clip_seconds=float(params.get("min_clip_seconds", 1.0)),
        )
        return {
            "clip_count": len(result.clips),
            "clips": _jsonable(result.clips),
            "skipped_short": result.skipped_short,
            "output_dir": _jsonable(result.output_dir),
            "manifest_csv": _jsonable(result.manifest_csv),
            "manifest_json": _jsonable(result.manifest_json),
            "engine": result.engine,
        }

    return "split-scenes", action


def _build_transcribe(params: dict[str, Any]):
    from .transcription import generate_transcript, whisper_available

    config = _load_project(params)
    video = _require(params, "video")
    engine = str(params.get("engine", "auto") or "auto")
    if engine not in {"auto", "whisper", "timeline"}:
        raise ApiError(400, "engine 只能是 auto、whisper 或 timeline")
    # whisper 未就绪时明确报错，不静默降级（除非调用方显式选 timeline 草稿）。
    if engine in {"auto", "whisper"}:
        ok, detail = whisper_available(config.root)
        if not ok:
            raise ApiError(422, f"真实语音转写未就绪，未按要求降级：{detail}")

    def action(progress: Callable[[int, int], None]) -> dict[str, Any]:
        result = generate_transcript(config, video_path=video, engine=engine)
        return {
            "video_path": _jsonable(result.video_path),
            "engine": result.engine,
            "mode": result.mode,
            "cue_count": result.cue_count,
            "srt_path": _jsonable(result.srt_path),
            "csv_path": _jsonable(result.csv_path),
            "json_path": _jsonable(result.json_path),
            "message": result.message,
            "fallback_reason": result.fallback_reason,
        }

    return "transcribe", action


_JOB_BUILDERS: dict[str, Callable[[dict[str, Any]], tuple[str, Callable]]] = {
    "produce": _build_produce,
    "dub-assemble": _build_dub,
    "auto-edit": _build_auto_edit,
    "episode-rework": _build_episode,
    "assemble": _build_assemble,
    "render": _build_render,
    "titles": _build_titles,
    "social-clips": _build_social,
    "visual-package": _build_visual,
    "detect-scenes": _build_detect_scenes,
    "split-scenes": _build_split_scenes,
    "transcribe": _build_transcribe,
}


def _recommend_edit(params: dict[str, Any]) -> dict[str, Any]:
    """同步返回推荐剪辑策略，供网页表单预填（不开 job）。"""
    from .auto_cut import recommend_auto_cut_settings

    config = _load_project(params)
    goal = _require(params, "goal")
    rec = recommend_auto_cut_settings(config, str(goal), save_report=False)
    s = rec.settings
    return {
        "goal": rec.goal,
        "reason": rec.reason,
        "github_basis": _jsonable(rec.github_basis),
        "settings": {
            "strategy": s.strategy,
            "target_seconds": s.target_seconds,
            "clip_seconds": s.clip_seconds,
            "keywords": s.keywords,
            "silence_db": s.silence_db,
            "min_silence": s.min_silence,
            "scene_threshold": s.scene_threshold,
        },
    }


# ---------------------------------------------------------------------------
# 同步动作：建项目、查状态
# ---------------------------------------------------------------------------
def _create_project(params: dict[str, Any]) -> dict[str, Any]:
    root = _require(params, "root")
    name = _require(params, "name")
    config = create_project(root, name, params.get("drama_name") or None)
    return {
        "project_root": _jsonable(config.root),
        "name": config.project_name,
        "drama_name": config.drama_name,
    }


def _project_status(params: dict[str, Any]) -> dict[str, Any]:
    config = _load_project(params)
    return {
        "project_root": _jsonable(config.root),
        "name": config.project_name,
        "drama_name": config.drama_name,
        "counts": {
            "source_videos": len(source_videos(config)),
            "background_videos": len(background_videos(config)),
            "stickers": len(stickers(config)),
            "ready_videos": len(ready_videos(config)),
        },
    }


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "MercuryAPI/" + API_VERSION

    # 由 ApiServer 注入
    token: str = ""
    registry: JobRegistry
    log_sink: Callable[[str], None] | None = None

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802 —— 覆盖默认 stderr 噪声
        if self.log_sink:
            self.log_sink(fmt % args)

    # -- 工具 --
    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        size = path.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        # RFC 5987：文件名可能含中文，用 UTF-8 百分号编码，避免 header 的 latin-1 编码报错。
        self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(path.name))
        self.end_headers()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_UPLOAD_CHUNK), b""):
                self.wfile.write(chunk)

    def _log_error(self) -> None:
        """内部错误堆栈只进日志，绝不回传给调用方（防信息泄露）。"""
        if self.log_sink:
            self.log_sink("internal error:\n" + traceback.format_exc())

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), self.token)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise ApiError(400, f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    # -- 路由 --
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path == "/api/health":
                self._send_json(200, {"status": "ok", "api_version": API_VERSION, "app_id": software_app_id()})
                return
            if not self._authorized():
                self._send_json(401, {"error": "未授权：缺少或错误的 Bearer token"})
                return
            if path == "/api/version":
                self._send_json(200, {
                    "api_version": API_VERSION,
                    "app_id": software_app_id(),
                    "app_id_mode": APP_ID_MODE,
                })
                return
            if path == "/api/enums":
                self._send_json(200, _enum_payload())
                return
            if path == "/api/projects/status":
                params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                self._send_json(200, _project_status(params))
                return
            if path == "/api/jobs":
                self._send_json(200, {"jobs": self.registry.list()})
                return
            if path.startswith("/api/jobs/"):
                self._handle_job_get(path)
                return
            self._send_json(404, {"error": f"未知路径：{path}"})
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception:  # noqa: BLE001
            self._log_error()
            self._send_json(500, {"error": "服务器内部错误"})

    def _handle_job_get(self, path: str) -> None:
        # /api/jobs/{id} | /api/jobs/{id}/files | /api/jobs/{id}/files/{name}
        segments = [seg for seg in path.split("/") if seg]  # api, jobs, id, [files, name]
        job = self.registry.get(segments[2]) if len(segments) >= 3 else None
        if job is None:
            self._send_json(404, {"error": "任务不存在"})
            return
        if len(segments) == 3:
            self._send_json(200, job.snapshot())
            return
        if segments[3] != "files":
            self._send_json(404, {"error": "未知路径"})
            return
        available = _collect_result_files(job.result)
        if len(segments) == 4:
            listing = [
                {"name": name, "size": p.stat().st_size, "sha256": _sha256_of(p)}
                for name, p in available.items()
            ]
            self._send_json(200, {"files": listing})
            return
        name = unquote(segments[4])
        target = available.get(name)  # 仅允许下载该 job 结果登记过的文件，规避路径穿越
        if target is None:
            self._send_json(404, {"error": "产物不存在或不属于该任务"})
            return
        self._send_file(target)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if not self._authorized():
                self._send_json(401, {"error": "未授权：缺少或错误的 Bearer token"})
                return
            if path == "/api/projects/files":
                self._handle_upload(parsed)
                return
            params = self._read_json_body()
            if path == "/api/projects":
                self._send_json(201, _create_project(params))
                return
            if path == "/api/recommend-edit":
                self._send_json(200, _recommend_edit(params))  # 同步返回，不开 job
                return
            kind = path.rsplit("/", 1)[-1]
            builder = _JOB_BUILDERS.get(kind)
            if builder is None:
                self._send_json(404, {"error": f"未知路径：{path}"})
                return
            job_kind, action = builder(params)  # 同步校验，非法参数在此抛 ApiError → 4xx
            job = self.registry.submit(job_kind, action, serialize_key=str(params.get("project", "")))
            self._send_json(202, {"job_id": job.id, "kind": job.kind, "status": job.status})
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception:  # noqa: BLE001
            self._log_error()
            self._send_json(500, {"error": "服务器内部错误"})

    def _handle_upload(self, parsed: Any) -> None:
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        project = _require(query, "project")
        kind = _require(query, "kind")
        filename = Path(str(_require(query, "filename"))).name  # 只取 basename，防路径穿越
        if not filename or filename in {".", ".."}:
            raise ApiError(400, "filename 非法")
        if kind not in KIND_DIRS:
            raise ApiError(400, f"未知素材类别 kind：{kind}；可选：{'、'.join(KIND_DIRS)}")
        try:
            config = load_config(project)
        except FileNotFoundError as exc:
            raise ApiError(400, str(exc)) from exc
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            raise ApiError(400, "缺少 Content-Length 或文件为空")
        if length > MAX_UPLOAD_BYTES:
            raise ApiError(413, f"文件超过上限 {MAX_UPLOAD_BYTES} 字节")

        dest_dir = config.root / KIND_DIRS[kind]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        tmp = dest_dir / f".{filename}.{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        written = 0
        try:
            with tmp.open("wb") as handle:  # 流式落盘，1MB 分块，避免整块入内存
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(_UPLOAD_CHUNK, remaining))
                    if not chunk:
                        break
                    handle.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    remaining -= len(chunk)
            if written != length:
                raise ApiError(400, f"传输不完整：声明 {length} 字节，实收 {written} 字节")
            tmp.replace(dest)  # 同名覆盖（幂等）
        finally:
            tmp.unlink(missing_ok=True)
        self._send_json(201, {"path": str(dest), "size": written, "sha256": digest.hexdigest()})


@dataclass
class ApiServer:
    host: str = "127.0.0.1"
    port: int = 8756
    token: str = ""
    max_workers: int = 2
    registry: JobRegistry | None = None
    log_sink: Callable[[str], None] | None = None
    _httpd: ThreadingHTTPServer | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.registry is None:
            self.registry = JobRegistry(max_workers=self.max_workers)

    def _make_handler(self) -> type[_Handler]:
        server = self

        class Handler(_Handler):
            token = server.token
            registry = server.registry
            # 用 staticmethod 包装，避免函数作为类属性被当成绑定方法（会多传 self）。
            log_sink = staticmethod(server.log_sink) if server.log_sink else None

        return Handler

    def create(self) -> ThreadingHTTPServer:
        if not self.token:
            self.token = secrets.token_urlsafe(24)
        self._httpd = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        return self._httpd

    @property
    def bound_port(self) -> int:
        return self._httpd.server_address[1] if self._httpd else self.port

    def serve_forever(self) -> None:
        httpd = self._httpd or self.create()
        httpd.serve_forever()

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()


def run_server(host: str = "127.0.0.1", port: int = 8756, token: str = "", max_workers: int = 2) -> None:
    """阻塞式启动 API 服务（供 CLI serve-api 调用）。"""
    server = ApiServer(
        host=host, port=port, token=token, max_workers=max_workers,
        log_sink=lambda msg: print(f"[api] {msg}"),
    )
    server.create()
    print(f"水星剪辑 API 已启动：http://{host}:{server.bound_port}")
    print(f"鉴权 token：{server.token}")
    print(f"最大并发任务：{max_workers}（同一项目内任务自动串行）")
    print("请求需带请求头 Authorization: Bearer <token>；/api/health 无需鉴权。")
    if host not in {"127.0.0.1", "localhost"}:
        print("警告：已绑定非本机地址，请确保配合防火墙与强 token，避免生产能力被外部滥用。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAPI 已停止。")
        server.shutdown()
