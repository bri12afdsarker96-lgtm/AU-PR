"""BulkDubService：把 store + scheduler + edge backend 组装成单例服务。"""

from __future__ import annotations

import copy
import shutil
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .. import settings as studio_settings
from ..engines.edge_tts import EDGE_VOICES

from . import excel_reader, ffmpeg_pipeline
from .csv_export import write_csv
from .edge_backend import EdgeTtsBackend
from .fingerprint import compute_fingerprint
from .hw_encoder import default_video_concurrency, system_summary
from .scheduler import Scheduler, SchedulerConfig, TtsBackend
from .store import (
    ALL_STATUSES, STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING,
    TaskRow, TaskStore,
)


DEFAULT_VOICE_ID = "zh-CN-XiaoshuangNeural"
DEFAULT_SPEED = 1.25
DEFAULT_PITCH = 0
DEFAULT_STYLE = "general"
DEFAULT_TTS_CONCURRENCY = 4
PROBE_SAMPLE_TEXT = "这是一段试听文案，用来预览当前音色和语速。"


class BulkDubService:
    def __init__(self,
                 store: TaskStore | None = None,
                 tts_backend: TtsBackend | None = None,
                 output_dir: str | None = None,
                 config: SchedulerConfig | None = None) -> None:
        self.store = store or TaskStore()
        self.tts_backend = tts_backend or EdgeTtsBackend()
        self._config = config or SchedulerConfig(
            tts_concurrency=DEFAULT_TTS_CONCURRENCY,
            video_concurrency=default_video_concurrency(),
            voice_short="配音",
            output_dir=output_dir or "",
        )
        self._scheduler: Scheduler | None = None
        self._scheduler_lock = threading.Lock()
        self._frozen_params: dict[str, Any] = {}
        self._current_batch_id: str | None = None

    def _ensure_scheduler(self) -> Scheduler:
        with self._scheduler_lock:
            if self._scheduler is None:
                self._scheduler = Scheduler(
                    store=self.store, config=self._config,
                    tts_backend=self.tts_backend,
                )
                self._scheduler.start()
            return self._scheduler

    def restart_scheduler(self, config: SchedulerConfig) -> None:
        with self._scheduler_lock:
            if self._scheduler is not None:
                self._scheduler.stop(2.0)
            self._config = config
            self._scheduler = Scheduler(
                store=self.store, config=self._config,
                tts_backend=self.tts_backend,
            )
            self._scheduler.start()

    def stop(self) -> None:
        with self._scheduler_lock:
            if self._scheduler is not None:
                self._scheduler.stop(3.0)
                self._scheduler = None

    def preview_excel(self, source: bytes,
                       *, check_exists: bool = True) -> dict:
        result = excel_reader.parse_excel(source, check_exists=check_exists)
        return result.to_dict()

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
        if not output_dir:
            raise ValueError("请先选择「输出目录」。")
        preview = excel_reader.parse_excel(source_bytes, check_exists=check_exists)
        voice_name = _voice_name_by_id(voice_id) or voice_id

        params = dict(
            voice_id=voice_id, voice_name=voice_name, speed=float(speed),
            pitch=int(pitch), style=style,
            keep_original_audio=bool(keep_original_audio),
            zoom_percent=int(zoom_percent),
            tts_concurrency=int(tts_concurrency),
            video_concurrency=int(video_concurrency) if video_concurrency else default_video_concurrency(),
            encoder_preference=encoder_preference,
            output_dir=output_dir,
            frozen_at=None,
        )
        batch_id = self.store.create_batch(label or "", output_dir, params)
        self._current_batch_id = batch_id
        self._frozen_params = copy.deepcopy(params)

        added = 0
        reused = 0
        for r in preview.rows:
            if not r.valid:
                self.store.add_task(
                    batch_id=batch_id, excel_row=r.row_number,
                    input_video=r.video_path or "",
                    text=r.text or "",
                    fingerprint="invalid:" + str(r.row_number),
                    voice_id=voice_id, voice_name=voice_name,
                    speed=float(speed),
                    keep_original_audio=bool(keep_original_audio),
                    params_snapshot=params,
                )
                latest = self.store.list_tasks(batch_id=batch_id, excel_row=r.row_number, limit=1)
                if latest:
                    self.store.update(latest[0].task_id, status=STATUS_FAILED,
                                       error_type="excel_invalid",
                                       error_detail=r.reason)
                continue
            fp = compute_fingerprint(
                video_path=r.video_path, text=r.text, voice_id=voice_id,
                speed=speed, pitch=pitch, style=style,
                keep_original_audio=keep_original_audio,
                mirror=True, zoom_percent=zoom_percent,
            )
            existing = self.store.get_by_fingerprint(fp, status=STATUS_COMPLETED)
            if existing and existing.output_path and Path(existing.output_path).is_file():
                new_id = self.store.add_task(
                    batch_id=batch_id, excel_row=r.row_number,
                    input_video=r.video_path, text=r.text, fingerprint=fp,
                    voice_id=voice_id, voice_name=voice_name, speed=float(speed),
                    keep_original_audio=bool(keep_original_audio),
                    params_snapshot=params,
                )
                self.store.update(new_id, status=STATUS_COMPLETED,
                                   output_path=existing.output_path,
                                   video_duration=existing.video_duration,
                                   tts_duration=existing.tts_duration,
                                   concat_duration=existing.concat_duration,
                                   final_duration=existing.final_duration,
                                   progress=100, stage="完成（复用已有成片）")
                reused += 1
                continue
            self.store.add_task(
                batch_id=batch_id, excel_row=r.row_number,
                input_video=r.video_path, text=r.text, fingerprint=fp,
                voice_id=voice_id, voice_name=voice_name, speed=float(speed),
                keep_original_audio=bool(keep_original_audio),
                params_snapshot=params,
            )
            added += 1

        new_config = SchedulerConfig(
            tts_concurrency=int(tts_concurrency),
            video_concurrency=int(video_concurrency) if video_concurrency else default_video_concurrency(),
            voice_short=ffmpeg_pipeline.voice_short_name(voice_name),
            output_dir=output_dir,
            encoder_preference=encoder_preference,
            zoom_percent=int(zoom_percent),
            keep_original_audio=bool(keep_original_audio),
        )
        self.restart_scheduler(new_config)
        return {
            "batch_id": batch_id,
            "added": added,
            "reused": reused,
            "invalid": preview.invalid,
            "total_rows": preview.total + reused,
        }

    def pause(self) -> None:
        self._ensure_scheduler().pause()

    def resume(self) -> None:
        self._ensure_scheduler().resume()

    def cancel_task(self, task_id: str) -> bool:
        return self._ensure_scheduler().cancel_task(task_id)

    def cancel_waiting(self, batch_id: str) -> int:
        return self._ensure_scheduler().cancel_all_waiting(batch_id)

    def retry_failed(self, batch_id: str, only_retryable: bool = False) -> int:
        return self._ensure_scheduler().retry_failed(batch_id, only_retryable)

    def list_tasks(self, **kwargs: Any) -> list[dict]:
        rows = self.store.list_tasks(**kwargs)
        return [asdict(r) for r in rows]

    def changed_since(self, since: float, batch_id: str | None = None) -> list[dict]:
        rows = self.store.changed_since(since, batch_id=batch_id)
        return [asdict(r) for r in rows]

    def summary(self, batch_id: str | None = None) -> dict:
        counts = self.store.count_by_status(batch_id)
        sched_snap = self._ensure_scheduler().snapshot()
        staging_root = studio_settings.data_root() / "批量带货" / "staging"
        staging_bytes = _dir_size(staging_root)
        out_dir = Path(self._config.output_dir or ".")
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
            },
            "batch_id": batch_id or self._current_batch_id or "",
            "frozen_params": copy.deepcopy(self._frozen_params),
        }

    def export_csv(self, batch_id: str | None,
                   target_path: str | Path) -> int:
        with open(target_path, "w", newline="", encoding="utf-8-sig") as fh:
            return write_csv(self.store.iter_all(batch_id=batch_id), fh)

    def probe_sample(self, *, voice_id: str = DEFAULT_VOICE_ID,
                     speed: float = DEFAULT_SPEED,
                     text: str = PROBE_SAMPLE_TEXT) -> Path:
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
