"""BulkDubService：把 store + scheduler + edge backend 组装成单例服务。

R11 改造：
    - fingerprint 计算包含 encoder_preference/preset/crf（不再硬编码 libx264）
    - 一次批量查询所有 fingerprint 的可复用结果（避免 N+1）
    - 批次内相同 fingerprint 去重（共享同一 output 记录）
    - Endpoint 未配置 → 直接拒绝启动（不建大量必失败任务）
    - resize_pools 转发 scheduler
"""

from __future__ import annotations

import copy
import os
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
    STATUS_RETRY_WAIT, STATUS_TTS_DONE, STATUS_WAITING_DEPENDENCY,
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

    def has_scheduler(self) -> bool:
        with self._scheduler_lock:
            return self._scheduler is not None

    def stop(self) -> bool:
        with self._scheduler_lock:
            if self._scheduler is None:
                return True
            ok = self._scheduler.stop(3.0)
            if ok:
                self._scheduler = None
            return ok

    def resize_pools(self, *, tts: int | None = None,
                     video: int | None = None) -> dict:
        if tts is not None and not (1 <= int(tts) <= 16):
            raise ValidationError(f"TTS 并发超范围 [1,16]：{tts}")
        if video is not None and not (1 <= int(video) <= 8):
            raise ValidationError(f"视频并发超范围 [1,8]：{video}")
        return self._ensure_scheduler().resize_pools(tts=tts, video=video)

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
        probe = p / ".bulk_dub_write_probe"
        try:
            probe.write_bytes(b"x")
            probe.unlink()
        except OSError as exc:
            raise ValidationError(f"输出目录不可写：{exc}") from exc
        return p

    def _require_endpoint_configured(self) -> None:
        """R13-P1-7：从 backend capability `requires_endpoint` 读取——生产 backend
        (`EdgeTtsBackend.requires_endpoint=True`) 必须配置 Worker；mock backend
        (`MockTtsBackend.requires_endpoint=False`) 天然跳过。**生产参数不再暴露
        `_skip_endpoint_check` / `require_endpoint=False` 的绕过入口。**
        """
        if not getattr(self.tts_backend, "requires_endpoint", True):
            return
        if not (edge_tts_endpoint() or "").strip():
            raise ValidationError(
                "Edge TTS 端点未配置。请到主页 · 工具箱 · 「免费 Edge TTS」填入 "
                "Cloudflare Worker 地址后再试。"
            )

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
                    encoder_preference: str = "auto",
                    check_exists: bool = True,
                    tts_concurrency: int | None = None,
                    video_concurrency: int | None = None) -> dict:
        """R12-2 批次创建走**单事务**：batches + invalid + leader + follower + reuse。

        R12-1 leader/follower：Excel 每条行都插一条 task 记录（保留 100% 可追溯）；
        同一 batch + fingerprint 集合里，第一条为 leader，其余为 follower
        （status=waiting_dependency, leader_task_id=leader）。
        leader 成功→copy 输出到 follower；leader 失败/取消→follower 同步终态。

        R12-12：`require_endpoint` 仅在 service 内部（例如测试注入 mock backend）
        跳过。默认必查 Endpoint；API 层不再接受该参数。
        """
        # R13-P0-5：显式传入并发数才应用；未传则保持 scheduler 现值不动
        eff_tts = DEFAULT_TTS_CONCURRENCY if tts_concurrency is None else int(tts_concurrency)
        eff_video = 0 if video_concurrency is None else int(video_concurrency)
        self.validate_params(
            voice_id=voice_id, speed=speed, pitch=pitch, style=style,
            zoom_percent=zoom_percent,
            tts_concurrency=eff_tts,
            video_concurrency=eff_video,
            encoder_preference=encoder_preference,
        )
        # R13-P1-7：从 backend capability 判断，生产无绕过入口
        self._require_endpoint_configured()
        out_path = self._validate_output_dir(output_dir)
        preview = excel_reader.parse_excel(source_bytes, check_exists=check_exists)
        if preview.total > excel_reader.MAX_TASKS_PER_BATCH:
            raise ValidationError(
                f"单批任务数上限 {excel_reader.MAX_TASKS_PER_BATCH}，"
                f"当前 {preview.total} 超上限"
            )
        voice_name = _voice_name_by_id(voice_id) or voice_id

        params_snapshot = dict(
            voice_id=voice_id, voice_name=voice_name, speed=float(speed),
            pitch=int(pitch), style=style,
            keep_original_audio=bool(keep_original_audio),
            zoom_percent=int(zoom_percent),
            encoder_preference=encoder_preference,
            output_dir=str(out_path),
            crf=20, preset="medium",
        )

        # 阶段 1：算所有有效行的 fingerprint
        rows_valid: list[tuple[Any, str]] = []
        for r in preview.rows:
            if not r.valid:
                continue
            fp = compute_fingerprint(
                video_path=r.video_path, text=r.text, voice_id=voice_id,
                speed=speed, pitch=pitch, style=style,
                keep_original_audio=keep_original_audio,
                mirror=True, zoom_percent=zoom_percent,
                encoder_preference=encoder_preference,
                preset=params_snapshot["preset"], crf=params_snapshot["crf"],
            )
            rows_valid.append((r, fp))
        fp_hits = self.store.find_completed_by_fingerprints(
            {fp for _, fp in rows_valid}
        )

        # 阶段 2：为 create_batch_with_tasks 拼装每条 task dict
        all_tasks: list[dict] = []
        added = 0
        reused = 0
        invalid_n = 0
        follower_n = 0
        # 同批内相同 fp → 记录 leader index（在 all_tasks 列表中的下标）
        leader_index_by_fp: dict[str, int] = {}
        # R13-P1-6：本次 start_batch 内已复用/落地到本目录的 fp → dest_path
        _reused_fp_dest: dict[str, str] = {}
        preview_index_by_row = {r.row_number: r for r in preview.rows}

        for r in preview.rows:
            if not r.valid:
                all_tasks.append(dict(
                    excel_row=r.row_number, input_video=r.video_path or "",
                    text=r.text or "",
                    fingerprint="invalid:" + str(r.row_number),
                    status=STATUS_FAILED,
                    error_type="excel_invalid", error_detail=r.reason,
                    voice_id=voice_id, voice_name=voice_name,
                    speed=float(speed),
                    keep_original_audio=bool(keep_original_audio),
                    params_snapshot=params_snapshot,
                    stage="失败",
                ))
                invalid_n += 1
                continue
            fp = compute_fingerprint(
                video_path=r.video_path, text=r.text, voice_id=voice_id,
                speed=speed, pitch=pitch, style=style,
                keep_original_audio=keep_original_audio,
                mirror=True, zoom_percent=zoom_percent,
                encoder_preference=encoder_preference,
                preset=params_snapshot["preset"], crf=params_snapshot["crf"],
            )
            existing = fp_hits.get(fp)
            common = dict(
                excel_row=r.row_number, input_video=r.video_path, text=r.text,
                fingerprint=fp,
                voice_id=voice_id, voice_name=voice_name, speed=float(speed),
                keep_original_audio=bool(keep_original_audio),
                params_snapshot=params_snapshot,
            )
            # R13-P1-6：跨批次 fingerprint 命中——旧成片必须在**本批次 output_dir**
            # 才算复用。若在别的目录：安全地把旧成片链/复制进本批次目录（marker 一并
            # 落本批次任务归属），永不覆盖。同 fp 在本次 start_batch 内已经落地过 →
            # 直接沿用（第二次不需要再 link）。
            reused_ok = False
            if existing and existing.output_path and Path(existing.output_path).is_file():
                src = Path(existing.output_path)
                # 本次 start_batch 内已经复用过同 fp？直接沿用其 dest_path
                already = _reused_fp_dest.get(fp)
                if already and Path(already).is_file():
                    dest_path = Path(already)
                    reused_ok = True
                else:
                    try:
                        src.resolve().relative_to(out_path.resolve())
                        reused_ok = True
                        dest_path = src
                    except ValueError:
                        # 旧成片不在本批 output_dir 下 → 尝试落地到本目录
                        dest_path = out_path / src.name
                        if not dest_path.exists():
                            import os as _os
                            try:
                                _os.link(src, dest_path)
                                reused_ok = True
                            except (OSError, NotImplementedError):
                                # 无 hardlink → 复用不成立，正常走 leader/follower
                                reused_ok = False
                        else:
                            # 同名文件已存在 → 不覆盖，视作复用不成立
                            reused_ok = False
                    if reused_ok:
                        _reused_fp_dest[fp] = str(dest_path)
            if reused_ok:
                task = dict(common,
                             status=STATUS_COMPLETED,
                             output_path=str(dest_path),
                             video_duration=existing.video_duration,
                             tts_duration=existing.tts_duration,
                             concat_duration=existing.concat_duration,
                             final_duration=existing.final_duration,
                             progress=100, stage="完成（复用已有成片）")
                all_tasks.append(task)
                reused += 1
                continue
            # 批内 leader/follower
            if fp not in leader_index_by_fp:
                leader_index_by_fp[fp] = len(all_tasks)
                all_tasks.append(dict(common, status=STATUS_PENDING))
                added += 1
            else:
                leader_idx = leader_index_by_fp[fp]
                all_tasks.append(dict(common,
                                       status=STATUS_WAITING_DEPENDENCY,
                                       leader_task_id=f"<INDEX:{leader_idx}>",
                                       stage="等待批内 leader"))
                follower_n += 1

        # R12-2：单事务落地——批次+全部任务；失败自动 ROLLBACK
        batch_id = self.store.create_batch_with_tasks(
            label=label or "", output_dir=str(out_path),
            params=params_snapshot, tasks=all_tasks,
        )
        self._current_batch_id = batch_id
        self._current_frozen_params = copy.deepcopy(params_snapshot)

        sched = self._ensure_scheduler()
        # R13-P0-5：显式传入并发数 → 真实应用到 scheduler 池（方案 A）
        effective_pools = None
        if tts_concurrency is not None or video_concurrency is not None:
            try:
                effective_pools = sched.resize_pools(
                    tts=tts_concurrency, video=video_concurrency,
                )
            except Exception as exc:  # noqa: BLE001
                effective_pools = {"error": str(exc)}
        sched.notify()

        return {
            "batch_id": batch_id,
            "added": added,
            "reused": reused,
            "followers": follower_n,
            "invalid": invalid_n,
            "total_rows": preview.total,
            "effective_pools": effective_pools,
        }

    # -------------------- 控制 --------------------

    def pause(self) -> None:
        self._ensure_scheduler().pause()

    def resume(self) -> None:
        self._ensure_scheduler().resume()

    def pause_batch(self, batch_id: str) -> None:
        if not self.store.get_batch(batch_id):
            raise ValidationError(f"batch_id 不存在：{batch_id}")
        self._ensure_scheduler().pause_batch(batch_id)

    def resume_batch(self, batch_id: str) -> None:
        if not self.store.get_batch(batch_id):
            raise ValidationError(f"batch_id 不存在：{batch_id}")
        self._ensure_scheduler().resume_batch(batch_id)

    def cancel_task(self, task_id: str) -> bool:
        if self.store.get(task_id) is None:
            raise ValidationError(f"task_id 不存在：{task_id}")
        return self._ensure_scheduler().cancel_task(task_id)

    def cancel_waiting(self, batch_id: str) -> int:
        if not self.store.get_batch(batch_id):
            raise ValidationError(f"batch_id 不存在：{batch_id}")
        return self._ensure_scheduler().cancel_all_waiting(batch_id)

    def retry_failed(self, batch_id: str, only_retryable: bool = False) -> int:
        if not self.store.get_batch(batch_id):
            raise ValidationError(f"batch_id 不存在：{batch_id}")
        return self._ensure_scheduler().retry_failed(batch_id, only_retryable)

    # -------------------- 查询 / 导出 --------------------

    def list_tasks(self, **kwargs: Any) -> list[dict]:
        rows = self.store.list_tasks(**kwargs)
        return [_task_to_dict(r) for r in rows]

    def changed_since_cursor(self, cursor: str | None,
                              batch_id: str | None = None,
                              limit: int = 500) -> dict:
        rows, next_cursor, has_more = self.store.changed_since_cursor(
            cursor, batch_id=batch_id, limit=limit,
        )
        return {
            "changes": [_task_to_dict(r) for r in rows],
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    def list_batches(self, limit: int = 50) -> list[dict]:
        return self.store.list_batches(limit)

    def summary(self, batch_id: str | None = None) -> dict:
        counts = self.store.count_by_status(batch_id)
        sched_snap = self._ensure_scheduler().snapshot()
        staging_root = studio_settings.data_root() / "批量带货" / "staging"
        staging_bytes = _dir_size(staging_root)
        # frozen_params 优先取"所选批次"（R11-9）
        frozen_params: dict = {}
        target_batch = batch_id or self._current_batch_id
        if target_batch:
            b = self.store.get_batch(target_batch)
            if b:
                frozen_params = b.get("params") or {}
        out_dir_str = str(frozen_params.get("output_dir", "") or "")
        out_dir = Path(out_dir_str or ".")
        try:
            disk = shutil.disk_usage(out_dir if out_dir.exists() else out_dir.parent)
            output_free_gb = round(disk.free / 1024 / 1024 / 1024, 2)
        except OSError:
            output_free_gb = 0.0
        # R12-11：分开估算 TTS 与视频阶段的 ETA
        m = sched_snap.get("metrics", {})
        avg_tts = float(m.get("avg_tts_processing_seconds") or 0)
        avg_video = float(m.get("avg_video_seconds") or 0)
        tts_alive = max(1, int(sched_snap.get("tts_alive") or 1))
        video_alive = max(1, int(sched_snap.get("video_alive") or 1))
        pending = counts.get(STATUS_PENDING, 0) + counts.get(STATUS_RETRY_WAIT, 0)
        waiting_video = counts.get(STATUS_TTS_DONE, 0)
        eta_tts = (pending * avg_tts / tts_alive) if avg_tts > 0 else 0
        eta_video = ((pending + waiting_video) * avg_video / video_alive) if avg_video > 0 else 0
        eta_seconds = round(max(eta_tts, eta_video))
        return {
            "counts": counts,
            "totals": {
                "total": sum(counts.values()),
                "waiting": pending,
                "tts_running": counts.get("tts_running", 0),
                "video_running": counts.get("video_running", 0),
                "succeeded": counts.get(STATUS_COMPLETED, 0),
                "failed": counts.get(STATUS_FAILED, 0),
                "retry_wait": counts.get("retry_wait", 0),
                "interrupted": counts.get("interrupted", 0),
                "cancelled": counts.get("cancelled", 0),
                "waiting_dependency": counts.get(STATUS_WAITING_DEPENDENCY, 0),
                "output_committed": counts.get("output_committed", 0),
                "cancelling": counts.get("cancelling", 0),
                "tts_done": counts.get(STATUS_TTS_DONE, 0),
            },
            "eta": {
                "eta_seconds": eta_seconds,
                "eta_tts_seconds": round(eta_tts),
                "eta_video_seconds": round(eta_video),
                "confidence": m.get("projection_confidence", "low"),
            },
            "scheduler": sched_snap,
            "system": system_summary(),
            "disk": {
                "staging_bytes": staging_bytes,
                "output_free_gb": output_free_gb,
                "output_dir": out_dir_str,
            },
            "edge_endpoint": edge_tts_endpoint(),
            "edge_endpoint_configured": bool(edge_tts_endpoint()),
            "batch_id": batch_id or self._current_batch_id or "",
            "frozen_params": frozen_params,
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


def peek_service() -> BulkDubService | None:
    """不触发创建的旁路查看——供 web_server 退出时决定要不要 stop()。"""
    with _SERVICE_LOCK:
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
