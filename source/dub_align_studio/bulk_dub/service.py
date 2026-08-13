"""BulkDubService：把 store + scheduler + edge backend 组装成单例服务。

R2 修复：
    - 不再在 start_batch() 里重启 scheduler；scheduler 在第一次调用时启动，之后**永远单例**；
      多批次共用同一 scheduler，任务级参数严格来自每条任务自己的 params_snapshot；
    - 提供 pause_batch/resume_batch（批次级暂停）+ pause/resume（全局暂停）；
    - 服务端参数校验（枚举、范围）；上传/预览大小 API 层再校验一遍；
    - Excel 导入用单事务批量插入（R8）；预览返回上限。
"""

from __future__ import annotations

import copy
import shutil
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .. import settings as studio_settings
from ..engines.edge_tts import EDGE_STYLES, EDGE_VOICES, edge_tts_endpoint

from . import excel_reader, ffmpeg_pipeline
from .csv_export import write_csv
from .edge_backend import EdgeTtsBackend
from .fingerprint import compute_fingerprint
from .hw_encoder import FAMILY_PREFERENCE, default_video_concurrency, system_summary
from .scheduler import Scheduler, SchedulerConfig, TtsBackend
from .store import (
    ALL_STATUSES, STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING,
    TaskRow, TaskStore, is_safe_id,
)


DEFAULT_VOICE_ID = "zh-CN-XiaoshuangNeural"
DEFAULT_SPEED = 1.25
DEFAULT_PITCH = 0
DEFAULT_STYLE = "general"
DEFAULT_TTS_CONCURRENCY = 4
PROBE_SAMPLE_TEXT = "这是一段试听文案，用来预览当前音色和语速。"

VOICE_IDS = frozenset(v["id"] for v in EDGE_VOICES)
STYLE_SET = frozenset(EDGE_STYLES)
ENCODER_PREF_SET = frozenset(FAMILY_PREFERENCE.keys())


class ValidationError(ValueError):
    """服务端参数不合法。API 层转 400。"""


class BulkDubService:
    def __init__(self,
                 store: TaskStore | None = None,
                 tts_backend: TtsBackend | None = None,
                 config: SchedulerConfig | None = None) -> None:
        self.store = store or TaskStore()
        self.tts_backend = tts_backend or EdgeTtsBackend()
        self._config = config or SchedulerConfig(
            tts_concurrency=DEFAULT_TTS_CONCURRENCY,
            video_concurrency=default_video_concurrency(),
        )
        self._scheduler: Scheduler | None = None
        self._scheduler_lock = threading.Lock()
        self._current_batch_id: str | None = None
        self._current_frozen_params: dict[str, Any] = {}

    def _ensure_scheduler(self) -> Scheduler:
        with self._scheduler_lock:
            if self._scheduler is None:
                self._scheduler = Scheduler(
                    store=self.store, config=self._config,
                    tts_backend=self.tts_backend,
                )
                self._scheduler.start()
            return self._scheduler

    def stop(self) -> None:
        with self._scheduler_lock:
            if self._scheduler is not None:
                self._scheduler.stop(3.0)
                self._scheduler = None

    # -------------------- 导入 / 预览 --------------------

    def preview_excel(self, source: bytes,
                       *, check_exists: bool = True) -> dict:
        result = excel_reader.parse_excel(source, check_exists=check_exists)
        return result.to_dict(preview_limit=excel_reader.DEFAULT_PREVIEW_LIMIT)

    # -------------------- 参数校验 --------------------

    @staticmethod
    def validate_params(*, voice_id: str, speed: float, pitch: int,
                        style: str, zoom_percent: int,
                        tts_concurrency: int, video_concurrency: int,
                        encoder_preference: str) -> None:
        if voice_id not in VOICE_IDS:
            raise ValidationError(f"未知音色 ID：{voice_id}")
        if not (0.5 <= speed <= 2.0):
            raise ValidationError(f"语速超出范围 [0.5, 2.0]：{speed}")
        if not (-100 <= pitch <= 100):
            raise ValidationError(f"pitch 超范围：{pitch}")
        if style not in STYLE_SET:
            raise ValidationError(f"未知 style：{style}")
        if not (100 <= zoom_percent <= 300):
            raise ValidationError(f"缩放比例超范围（100~300%）：{zoom_percent}")
        if not (1 <= tts_concurrency <= 16):
            raise ValidationError(f"TTS 并发超范围 [1,16]：{tts_concurrency}")
        if video_concurrency and not (1 <= video_concurrency <= 8):
            raise ValidationError(f"视频并发超范围 [1,8]：{video_concurrency}")
        if encoder_preference not in ENCODER_PREF_SET:
            raise ValidationError(f"未知编码偏好：{encoder_preference}")

    @staticmethod
    def _validate_output_dir(output_dir: str) -> Path:
        if not output_dir:
            raise ValidationError("请先选择「输出目录」。")
        p = Path(output_dir).expanduser()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValidationError(f"无法创建输出目录：{exc}") from exc
        # 可写探测
        probe = p / ".bulk_dub_write_probe"
        try:
            probe.write_bytes(b"x")
            probe.unlink()
        except OSError as exc:
            raise ValidationError(f"输出目录不可写：{exc}") from exc
        return p

    # -------------------- 开始批处理 --------------------

    def start_batch(self, *, source_bytes: bytes,
                    label: str,
                    output_dir: str,
                    voice_id: str = DEFAULT_VOICE_ID,
                    speed: float = DEFAULT_SPEED,
                    pitch: int = DEFAULT_PITCH,
                    style: str = DEFAULT_STYLE,
                    keep_original_audio: bool = False,
                    zoom_percent: int = 130,
                    tts_concurrency: int = DEFAULT_TTS_CONCURRENCY,
                    video_concurrency: int = 0,
                    encoder_preference: str = "auto",
                    check_exists: bool = True) -> dict:
        # 校验参数
        self.validate_params(
            voice_id=voice_id, speed=speed, pitch=pitch, style=style,
            zoom_percent=zoom_percent,
            tts_concurrency=tts_concurrency,
            video_concurrency=video_concurrency,
            encoder_preference=encoder_preference,
        )
        out_path = self._validate_output_dir(output_dir)
        preview = excel_reader.parse_excel(source_bytes, check_exists=check_exists)
        if preview.total > excel_reader.MAX_TASKS_PER_BATCH:
            raise ValidationError(
                f"单批任务数上限 {excel_reader.MAX_TASKS_PER_BATCH}，"
                f"当前 {preview.total} 超上限"
            )
        voice_name = _voice_name_by_id(voice_id) or voice_id

        # 冻结到 params_snapshot：**每条任务都带一份**，运行中改控件不会影响已入库任务
        params_snapshot = dict(
            voice_id=voice_id, voice_name=voice_name, speed=float(speed),
            pitch=int(pitch), style=style,
            keep_original_audio=bool(keep_original_audio),
            zoom_percent=int(zoom_percent),
            encoder_preference=encoder_preference,
            output_dir=str(out_path),
            crf=20, preset="medium",
        )
        batch_id = self.store.create_batch(label or "", str(out_path), params_snapshot)
        self._current_batch_id = batch_id
        self._current_frozen_params = copy.deepcopy(params_snapshot)

        # 分组：无效行 / 幂等复用 / 新增
        invalid_tasks: list[dict] = []
        reuse_tasks: list[dict] = []
        add_tasks: list[dict] = []
        reuse_meta: list[dict] = []   # 复用命中的 existing 记录（后续 update 用）

        for r in preview.rows:
            if not r.valid:
                invalid_tasks.append(dict(
                    excel_row=r.row_number, input_video=r.video_path or "",
                    text=r.text or "",
                    fingerprint="invalid:" + str(r.row_number),
                    status=STATUS_FAILED,
                    error_type="excel_invalid", error_detail=r.reason,
                    voice_id=voice_id, voice_name=voice_name,
                    speed=float(speed),
                    keep_original_audio=bool(keep_original_audio),
                    params_snapshot=params_snapshot,
                ))
                continue
            fp = compute_fingerprint(
                video_path=r.video_path, text=r.text, voice_id=voice_id,
                speed=speed, pitch=pitch, style=style,
                keep_original_audio=keep_original_audio,
                mirror=True, zoom_percent=zoom_percent,
            )
            existing = self.store.get_by_fingerprint(fp, status=STATUS_COMPLETED)
            if existing and existing.output_path and Path(existing.output_path).is_file():
                reuse_tasks.append(dict(
                    excel_row=r.row_number, input_video=r.video_path, text=r.text,
                    fingerprint=fp,
                    voice_id=voice_id, voice_name=voice_name, speed=float(speed),
                    keep_original_audio=bool(keep_original_audio),
                    params_snapshot=params_snapshot,
                ))
                reuse_meta.append(dict(
                    output_path=existing.output_path,
                    video_duration=existing.video_duration,
                    tts_duration=existing.tts_duration,
                    concat_duration=existing.concat_duration,
                    final_duration=existing.final_duration,
                ))
                continue
            add_tasks.append(dict(
                excel_row=r.row_number, input_video=r.video_path, text=r.text,
                fingerprint=fp,
                voice_id=voice_id, voice_name=voice_name, speed=float(speed),
                keep_original_audio=bool(keep_original_audio),
                params_snapshot=params_snapshot,
            ))

        # R8：单事务批量插入
        if invalid_tasks:
            self.store.bulk_insert(batch_id, invalid_tasks)
        reuse_ids = self.store.bulk_insert(batch_id, reuse_tasks) if reuse_tasks else []
        self.store.bulk_insert(batch_id, add_tasks) if add_tasks else []
        # 复用任务：批量插入后，逐条 update 成 completed（少量任务，短连接开销可接受）
        for tid, meta in zip(reuse_ids, reuse_meta):
            self.store.update(tid, status=STATUS_COMPLETED,
                               output_path=meta["output_path"],
                               video_duration=meta["video_duration"],
                               tts_duration=meta["tts_duration"],
                               concat_duration=meta["concat_duration"],
                               final_duration=meta["final_duration"],
                               progress=100, stage="完成（复用已有成片）")

        # R2 关键修复：**不 restart scheduler**——单例 scheduler 处理所有批次
        sched = self._ensure_scheduler()
        # 领新任务
        sched.notify()

        return {
            "batch_id": batch_id,
            "added": len(add_tasks),
            "reused": len(reuse_ids),
            "invalid": len(invalid_tasks),
            # R8：total_rows 就是 preview.total（已含 valid + invalid），不再重复加 reused
            "total_rows": preview.total,
        }

    # -------------------- 控制 --------------------

    def pause(self) -> None:
        self._ensure_scheduler().pause()

    def resume(self) -> None:
        self._ensure_scheduler().resume()

    def pause_batch(self, batch_id: str) -> None:
        if not is_safe_id(batch_id):
            raise ValidationError(f"非法 batch_id：{batch_id}")
        self._ensure_scheduler().pause_batch(batch_id)

    def resume_batch(self, batch_id: str) -> None:
        if not is_safe_id(batch_id):
            raise ValidationError(f"非法 batch_id：{batch_id}")
        self._ensure_scheduler().resume_batch(batch_id)

    def cancel_task(self, task_id: str) -> bool:
        return self._ensure_scheduler().cancel_task(task_id)

    def cancel_waiting(self, batch_id: str) -> int:
        if not is_safe_id(batch_id):
            raise ValidationError(f"非法 batch_id：{batch_id}")
        return self._ensure_scheduler().cancel_all_waiting(batch_id)

    def retry_failed(self, batch_id: str, only_retryable: bool = False) -> int:
        if not is_safe_id(batch_id):
            raise ValidationError(f"非法 batch_id：{batch_id}")
        return self._ensure_scheduler().retry_failed(batch_id, only_retryable)

    # -------------------- 查询 / 导出 --------------------

    def list_tasks(self, **kwargs: Any) -> list[dict]:
        rows = self.store.list_tasks(**kwargs)
        return [_task_to_dict(r) for r in rows]

    def changed_since(self, since: float, batch_id: str | None = None) -> list[dict]:
        rows = self.store.changed_since(since, batch_id=batch_id)
        return [_task_to_dict(r) for r in rows]

    def list_batches(self, limit: int = 50) -> list[dict]:
        return self.store.list_batches(limit)

    def summary(self, batch_id: str | None = None) -> dict:
        counts = self.store.count_by_status(batch_id)
        sched_snap = self._ensure_scheduler().snapshot()
        staging_root = studio_settings.data_root() / "批量带货" / "staging"
        staging_bytes = _dir_size(staging_root)
        out_dir_str = ""
        if batch_id:
            b = self.store.get_batch(batch_id)
            out_dir_str = b["output_dir"] if b else ""
        elif self._current_batch_id:
            b = self.store.get_batch(self._current_batch_id)
            out_dir_str = b["output_dir"] if b else ""
        out_dir = Path(out_dir_str or ".")
        try:
            disk = shutil.disk_usage(out_dir if out_dir.exists() else out_dir.parent)
            output_free_gb = round(disk.free / 1024 / 1024 / 1024, 2)
        except OSError:
            output_free_gb = 0.0
        return {
            "counts": counts,
            "totals": {
                "total": sum(counts.values()),
                "waiting": counts.get(STATUS_PENDING, 0),
                "tts_running": counts.get("tts_running", 0),
                "video_running": counts.get("video_running", 0),
                "succeeded": counts.get(STATUS_COMPLETED, 0),
                "failed": counts.get(STATUS_FAILED, 0),
                "retry_wait": counts.get("retry_wait", 0),
                "interrupted": counts.get("interrupted", 0),
                "cancelled": counts.get("cancelled", 0),
            },
            "scheduler": sched_snap,
            "system": system_summary(),
            "disk": {
                "staging_bytes": staging_bytes,
                "output_free_gb": output_free_gb,
                "output_dir": str(out_dir_str),
            },
            "edge_endpoint": edge_tts_endpoint(),
            "batch_id": batch_id or self._current_batch_id or "",
            "frozen_params": copy.deepcopy(self._current_frozen_params),
        }

    def export_csv(self, batch_id: str | None,
                   target_path: str | Path) -> int:
        with open(target_path, "w", newline="", encoding="utf-8-sig") as fh:
            return write_csv(self.store.iter_all(batch_id=batch_id), fh)

    def probe_sample(self, *, voice_id: str = DEFAULT_VOICE_ID,
                     speed: float = DEFAULT_SPEED,
                     text: str = PROBE_SAMPLE_TEXT) -> Path:
        if voice_id not in VOICE_IDS:
            raise ValidationError(f"未知音色 ID：{voice_id}")
        if not (0.5 <= float(speed) <= 2.0):
            raise ValidationError(f"语速超范围：{speed}")
        out = studio_settings.data_root() / "批量带货" / "试听" / f"{voice_id}_{speed:.2f}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        self.tts_backend.synthesize(
            text=text, voice_id=voice_id, speed=float(speed),
            pitch=DEFAULT_PITCH, style=DEFAULT_STYLE, output_wav=out,
        )
        return out


def _voice_name_by_id(voice_id: str) -> str:
    for v in EDGE_VOICES:
        if v["id"] == voice_id:
            return v["name"]
    return ""


def _task_to_dict(row: TaskRow) -> dict:
    return asdict(row)


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


_SERVICE: BulkDubService | None = None
_SERVICE_LOCK = threading.Lock()


def get_service() -> BulkDubService:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            _SERVICE = BulkDubService()
        return _SERVICE


def reset_service_for_tests(service: BulkDubService | None) -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is not None:
            try:
                _SERVICE.stop()
            except Exception:  # noqa: BLE001
                pass
        _SERVICE = service
