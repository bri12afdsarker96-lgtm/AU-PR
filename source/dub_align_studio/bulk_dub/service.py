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
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from .. import settings as studio_settings
from ..engines.edge_tts import EDGE_STYLES, EDGE_VOICES, edge_tts_endpoint

from . import excel_reader, ffmpeg_pipeline, gpu_profile
from .concurrency_controller import (
    ALL_MODES, MODE_AUTO, MODE_MANUAL, MODE_CPU_SAFE,
)
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
        # R14-5 基准运行状态。基准必须**后台**运行，页面轮询进度；
        # 绝不阻塞 HTTP handler；生产池正在跑任务时禁止启动基准。
        # R14-FIX-3 P0-2：显式状态机 — idle/preparing/running/stopping/
        # done/error/cancelled；生成号（generation/run_id）确保后台闭包
        # 只更新与自己 generation 相同的状态，旧线程不得覆盖新任务。
        self._benchmark_lock = threading.Lock()
        self._benchmark_thread: threading.Thread | None = None
        self._benchmark_cancel = threading.Event()
        # R14-FIX-3 P0-1：每次 benchmark 独立 registry；service.stop 只收
        # 本次 benchmark 的 FFmpeg，绝不动全局 _ACTIVE。
        self._benchmark_registry: gpu_profile.BenchmarkProcessRegistry | None = None
        self._benchmark_generation: int = 0
        self._benchmark_state: dict[str, Any] = {
            "running": False,
            "state": "idle",     # idle/preparing/running/stopping/done/error/cancelled/rejected
            "generation": 0,
            "phase": "",
            "concurrency_now": 0,
            "results": [],
            "error": "",
            "started_at": 0.0,
            "finished_at": 0.0,
            "sample_video": "",
        }
        # R14-2 已加载/写盘的 profile；None 表示未加载
        self._gpu_profile: gpu_profile.DeviceCapability | None = gpu_profile.load_profile()
        # R14-FIX2 P1-6：mode/user_max/manual_video/profile_fingerprint 状态
        # 只加载**一次**；后续 apply/set_mode 只写内存并 save_state。
        # 每次 _ensure_scheduler_for_controller() 重读并覆盖是错的——会把
        # UI 刚设的模式立刻还原。
        self._persistent_state: dict | None = None
        try:
            self._persistent_state = gpu_profile.load_state()
        except Exception:  # noqa: BLE001
            self._persistent_state = None
        self._state_applied_to_scheduler = False

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

    def stop(self, wait_seconds: float = 5.0) -> bool:
        """R14-FIX-3 P0-1：服务退出必须收走 benchmark，**只收本次 benchmark
        自己的 FFmpeg**；上一轮 `service.stop()` 会遍历 `_ACTIVE` 终止**全部**
        登记进程——那是跨组件误杀（生产队列、其他页面工具会被一起杀）。
        本轮只 terminate 本次 benchmark 的 `_benchmark_registry` 里的进程；
        生产池 FFmpeg 由 `scheduler.stop()` 内的自然收敛负责。

        流程：
          1) 状态机切到 stopping；触发本次 benchmark cancel；
          2) terminate/wait/kill/wait 本次 registry 里的进程；
          3) **锁外**等 benchmark 线程退出（其 finally 会 leave_exclusive）；
          4) 再 stop scheduler。任一步没在时限内完成 → 返回 False；
          5) 从不持 _benchmark_lock 等 benchmark 线程。
        """
        # 1) 触发 benchmark cancel（同时把状态机推向 stopping）
        with self._benchmark_lock:
            bench_thread = self._benchmark_thread
            registry = self._benchmark_registry
            if self._benchmark_state.get("running"):
                self._benchmark_cancel.set()
                self._benchmark_state["state"] = "stopping"

        deadline = time.time() + max(0.5, float(wait_seconds))

        # 2) 只收本次 benchmark 自己的子进程（**绝不**动全局 _ACTIVE）
        if registry is not None:
            try:
                registry.terminate_all(grace_seconds=3.0, kill_wait=2.0)
            except Exception:  # noqa: BLE001
                pass

        # 3) 锁外 join benchmark 线程（bench_thread.finally 会 leave_exclusive）
        benchmark_gone = True
        if bench_thread is not None and bench_thread.is_alive():
            remaining = max(0.5, deadline - time.time())
            bench_thread.join(timeout=remaining)
            benchmark_gone = not bench_thread.is_alive()

        # 4) 再停 scheduler
        with self._scheduler_lock:
            sched = self._scheduler
        sched_gone = True
        if sched is not None:
            sched_wait = max(0.5, deadline - time.time())
            sched_gone = sched.stop(sched_wait)
            if sched_gone:
                with self._scheduler_lock:
                    if self._scheduler is sched:
                        self._scheduler = None
        return bool(benchmark_gone and sched_gone)

    def resize_pools(self, *, tts: int | None = None,
                     video: int | None = None) -> dict:
        from .concurrency_controller import (
            MAX_TTS_CONCURRENCY as _MTC, MAX_VIDEO_CONCURRENCY as _MVC,
        )
        if tts is not None and not (1 <= int(tts) <= _MTC):
            raise ValidationError(f"TTS 并发超范围 [1,{_MTC}]：{tts}")
        if video is not None and not (1 <= int(video) <= _MVC):
            raise ValidationError(f"视频并发超范围 [1,{_MVC}]：{video}")
        sched = self._ensure_scheduler()
        # R14-FIX P0-6：基准独占期禁止 resize
        if sched.is_benchmark_exclusive():
            raise ValidationError("benchmark 独占运行中，禁止 resize")
        return sched.resize_pools(tts=tts, video=video)

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
        from .concurrency_controller import (
            MAX_TTS_CONCURRENCY as _MTC, MAX_VIDEO_CONCURRENCY as _MVC,
        )
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
        if not (1 <= tts_concurrency <= _MTC):
            raise ValidationError(f"TTS 并发超范围 [1,{_MTC}]：{tts_concurrency}")
        # R14-FIX P0-4：视频并发上限统一到 MAX_VIDEO_CONCURRENCY(16)
        if video_concurrency and not (1 <= video_concurrency <= _MVC):
            raise ValidationError(f"视频并发超范围 [1,{_MVC}]：{video_concurrency}")
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
        # R13-FIX-P1-B：跨目录 hardlink 到本 output_dir 的复用任务——需要在
        # 批次落库后为它们写 marker，让新 task 也带 ownership metadata
        _reused_hardlink_todo: list[tuple[int, Path, str, float]] = []
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
            did_cross_dir_link = False
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
                                did_cross_dir_link = True
                            except (OSError, NotImplementedError):
                                # 无 hardlink → 复用不成立，正常走 leader/follower
                                reused_ok = False
                        else:
                            # 同名文件已存在 → 不覆盖，视作复用不成立
                            reused_ok = False
                    if reused_ok:
                        _reused_fp_dest[fp] = str(dest_path)
            if reused_ok:
                if did_cross_dir_link:
                    # R13-FIX-P1-B：登记 (excel_row, dest_path, fp, final_duration)
                    # 用于批次落库后写 marker
                    _reused_hardlink_todo.append((
                        int(r.row_number), dest_path, fp,
                        float(existing.final_duration or 0),
                    ))
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

        # R13-FIX-P1-B：为跨目录 hardlink 复用的任务补写 marker（ownership）——
        # 让新 output_dir 里的成片带上本 task/batch/fp 的 metadata，
        # 未来任何校验/恢复都能核对身份。写失败不阻塞批次成功。
        if _reused_hardlink_todo:
            from . import ffmpeg_pipeline as _vp
            id_by_row = {}
            try:
                for _row in self.store.list_tasks(batch_id=batch_id, limit=100000):
                    id_by_row[int(_row.excel_row)] = _row.task_id
            except Exception:  # noqa: BLE001
                id_by_row = {}
            for excel_row, dest_path, fp, final_dur in _reused_hardlink_todo:
                tid = id_by_row.get(excel_row)
                if not tid:
                    continue
                try:
                    marker_path = _vp.marker_path_for(dest_path)
                    hash_hex = _vp._blake2b_of_file(dest_path)
                    _vp._write_marker(
                        marker_path, task_id=tid, batch_id=batch_id,
                        fingerprint=fp,
                        target_final_seconds=final_dur,
                        file_size=dest_path.stat().st_size,
                        output_name=dest_path.name,
                        encoder_used="", hw_fallback_used=False,
                        content_hash=hash_hex,
                        commit_stage="reused_hardlink",
                    )
                except Exception:  # noqa: BLE001
                    # marker 写失败不影响任务本身（已在 DB 中 completed）
                    pass

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
        # R14-FIX P0-6：benchmark 独占期禁止手动 resume，
        # 防止误 resume 让生产池抢显卡/磁盘
        sched = self._ensure_scheduler()
        if sched.is_benchmark_exclusive():
            raise ValidationError("benchmark 独占运行中，禁止 resume")
        sched.resume()

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

    def delete_task(self, task_id: str) -> bool:
        """删除单个终态任务（completed/failed/cancelled/...）。
        非终态返回 False（前端应先「取消」再删）。"""
        if self.store.get(task_id) is None:
            raise ValidationError(f"task_id 不存在：{task_id}")
        return self.store.delete_task(task_id)

    def clear_batch(self, batch_id: str) -> dict:
        """批量「取消 + 删除」：先取消所有等待中的任务（→ cancelled），
        再物理删除本批次所有终态任务（含刚取消的）。返回 {cancelled, deleted}。
        活动态（正在跑）的任务会被转 cancelling，不会立刻删除——收敛后再点一次即可清掉。"""
        if not self.store.get_batch(batch_id):
            raise ValidationError(f"batch_id 不存在：{batch_id}")
        cancelled = self._ensure_scheduler().cancel_all_waiting(batch_id)
        deleted = self.store.delete_finished_in_batch(batch_id)
        return {"cancelled": cancelled, "deleted": deleted}

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

    # -------------------- R14 GPU 能力 / 基准 / 模式 --------------------

    def _ensure_scheduler_for_controller(self) -> Scheduler:
        """确保 scheduler 已建立，并把**有效** profile 推给 controller。

        R14-FIX P0-5：scheduler 启动时**只自动加载有效 profile**——
        重探当前指纹与 profile 记录不匹配时不 push profile_recommended，
        controller 保持默认（config.video_concurrency）；UI 会显示"profile 已失效"。

        R14-FIX2 P1-6：mode/user_max/manual_video 状态**只在 service init
        时读一次**（存于 self._persistent_state），此处只在**首次**推给
        scheduler；后续调用 apply/set_mode 只写内存并 save_state，不再
        每次都重新 load_state 覆盖 UI 刚设置的模式。
        """
        sched = self._ensure_scheduler()
        prof = self._gpu_profile
        if prof is not None:
            try:
                ffmpeg = studio_settings.ffmpeg_tool("ffmpeg")
                # P1-5：按 profile 记录的 encoder_preference 探测，不是硬编码 auto
                pref = getattr(prof, "encoder_preference", None) or "auto"
                fresh = gpu_profile.detect_capability_metadata(
                    ffmpeg, encoder_preference=pref,
                )
                if fresh.device_fingerprint == prof.device_fingerprint:
                    sched.controller.set_profile_recommended(
                        int(prof.recommended_concurrency or 0)
                    )
            except Exception:  # noqa: BLE001
                pass
        # R14-FIX2 P1-6：state 只加载一次（在 __init__）；这里只在首次
        # 推给 scheduler。后续 UI/API 已经写过内存的 mode 不会被这里
        # 无脑覆盖掉。
        if not self._state_applied_to_scheduler:
            st = self._persistent_state or {}
            if st:
                mode = st.get("mode") or MODE_AUTO
                manual = int(st.get("manual_video") or 0) or None
                user_max = int(st.get("user_max") or 0)
                try:
                    sched.controller.set_mode(mode, manual_video=manual)
                except Exception:  # noqa: BLE001
                    pass
                sched.controller.set_user_max(user_max)
                if mode == MODE_CPU_SAFE:
                    sched.set_encoder_override("libx264")
                else:
                    sched.set_encoder_override(None)
            self._state_applied_to_scheduler = True
        return sched

    def _apply_profile_and_mode(self, sched: Scheduler, *,
                                 mode: str, target_video: int | None,
                                 manual_video: int | None = None,
                                 user_max: int | None = None) -> dict:
        """R14-FIX2 P0-4 内部统一入口：**同一处**更新 mode + encoder_override +
        resize；resize 失败必须体现在返回值里，不能仍宣称"应用成功"。

        R14-FIX-3 P1-3：任何一步失败——必须**回滚**到应用前快照
        （mode / encoder_override / manual_video / user_max），返回值同时
        暴露 requested_video / actual_video / resize_error / applied=False，
        绝不把失败状态持久化成"完整成功"。

        返回：{
          mode, encoder_override, requested_video, actual_video, applied_video,
          resize_ok, resize_error, applied, controller
        }
        """
        # 应用前快照——resize 失败可回滚
        pre_mode = sched.controller.state.mode
        pre_manual = sched.controller.state.manual_video_concurrency
        pre_user_max = sched.controller.state.user_max
        pre_override = sched.encoder_override()

        # 1) 更新 mode + encoder_override（CPU_SAFE 打开 libx264；其他清除）
        sched.controller.set_mode(mode, manual_video=manual_video)
        if user_max is not None:
            sched.controller.set_user_max(int(user_max))
        if mode == MODE_CPU_SAFE:
            sched.set_encoder_override("libx264")
        else:
            sched.set_encoder_override(None)

        # 2) resize（可选）
        resize_ok = True
        resize_error = ""
        applied_video: int | None = None
        actual_video: int | None = None
        applied_flag = True
        if target_video is not None:
            try:
                r = sched.resize_pools(video=int(target_video))
                applied_video = int(
                    r.get("after", {}).get("video_alive") or target_video
                )
                actual_video = applied_video
            except Exception as exc:  # noqa: BLE001
                resize_ok = False
                resize_error = str(exc)[:400]
                applied_flag = False
                # 尝试观察实际 serving
                try:
                    snap = sched.snapshot()
                    actual_video = int(snap.get("video_alive") or 0)
                except Exception:  # noqa: BLE001
                    actual_video = 0
                # P1-3：回滚到应用前快照——mode / override / manual / user_max
                try:
                    sched.controller.set_mode(
                        pre_mode, manual_video=pre_manual or None,
                    )
                    sched.controller.set_user_max(pre_user_max)
                    sched.set_encoder_override(pre_override)
                except Exception:  # noqa: BLE001
                    pass

        # 3) 计算 serving 实际值
        try:
            snap = sched.snapshot()
            serving = int(snap.get("video_alive") or 0)
        except Exception:  # noqa: BLE001
            serving = 0
        return {
            # 回滚后 mode 反映真实生效值
            "mode": sched.controller.state.mode,
            "encoder_override": sched.encoder_override() or "",
            "requested_video": target_video,
            "actual_video": actual_video,
            "applied_video": applied_video,
            "video_serving": serving,
            "resize_ok": resize_ok,
            "resize_error": resize_error,
            "applied": applied_flag,
            "controller": sched.controller.snapshot(),
        }

    def _persist_state(self, *, mode: str, sched: Scheduler,
                        profile_fp: str = "") -> tuple[bool, str]:
        """R14-FIX2 P1-6：save_state 失败必须**可见**——API 至少返回
        persisted=false 和 warning。返回 (ok, warning)。"""
        payload = {
            "mode": mode,
            "user_max": sched.controller.state.user_max,
            "manual_video": sched.controller.state.manual_video_concurrency,
            "profile_fingerprint": profile_fp,
        }
        try:
            gpu_profile.save_state(payload)
            self._persistent_state = payload
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"gpu_state 落盘失败：{exc}"[:200]

    def gpu_capability(self) -> dict:
        """返回当前设备能力档案。若未做基准 → 返回骨架元数据（sample=空）。"""
        ffmpeg = studio_settings.ffmpeg_tool("ffmpeg")
        loaded = self._gpu_profile
        # 探测当前设备指纹；与已加载 profile 不同 → 视为失效
        try:
            fresh_meta = gpu_profile.detect_capability_metadata(ffmpeg)
        except Exception as exc:  # noqa: BLE001
            fresh_meta = None
        fingerprint_matches = (
            loaded is not None and fresh_meta is not None
            and loaded.device_fingerprint == fresh_meta.device_fingerprint
        )
        controller_snap = {}
        if self._scheduler is not None:
            try:
                controller_snap = self._scheduler.controller.snapshot()
            except Exception:  # noqa: BLE001
                pass
        return {
            "current": (fresh_meta.to_json() if fresh_meta else None),
            "profile": (loaded.to_json() if loaded else None),
            "profile_valid": fingerprint_matches,
            "controller": controller_snap,
            "disclaimer": (
                "该数字来自当前样本短时测试，不等于真实 24 小时产能承诺。"
                "达到 10000/日目标必须做真机 24h 耐久后才能宣称。"
            ),
        }

    def _running_task_count(self) -> int:
        """R14-3 基准前置检查：正在跑的物理任务数（tts_running + video_running）。"""
        try:
            counts = self.store.count_by_status(None)
        except Exception:  # noqa: BLE001
            return 0
        return int(counts.get("tts_running", 0)) + int(counts.get("video_running", 0))

    def benchmark_status(self) -> dict:
        with self._benchmark_lock:
            return dict(self._benchmark_state)

    def cancel_benchmark(self) -> bool:
        """取消进行中的基准。返回是否触发（未运行时返回 False）。
        R14-FIX-3 P0-2：cancel 只取消当前 generation。"""
        with self._benchmark_lock:
            st = self._benchmark_state.get("state", "idle")
            if st not in ("preparing", "running"):
                return False
            self._benchmark_state["state"] = "stopping"
            self._benchmark_cancel.set()
            return True

    def start_benchmark(self, *, sample_video: str,
                          sample_tts_audio: str | None = None,
                          encoder_preference: str = "auto",
                          zoom_percent: int = 130,
                          preset: str = "medium", crf: int = 20,
                          ladder: Iterable[int] | None = None,
                          exclusive_wait_seconds: float = 30.0) -> dict:
        """R14-3 / R14-FIX P0-6：后台跑基准。

        - 若 scheduler 已启动 → **原子进入 benchmark 独占态**（暂停领取、等运行
          任务收敛到 0）；期间禁止 resume/apply/resize/第二 benchmark；
          结束/失败/取消后恢复此前的暂停状态；
        - `sample_tts_audio` 可选：不传则 make_silent_wav(2×video.duration)，
          覆盖生产完整"原视频＋镜像副本"时长；
        - 立即返回，实际测量在后台线程；
        - 只允许**一个**运行中的 benchmark。
        """
        sv = Path(sample_video).expanduser()
        if not sv.is_file():
            raise ValidationError(f"代表样本视频不存在：{sv}")
        # R14-FIX-3 P0-7：ladder 服务端硬校验
        try:
            ladder_list = gpu_profile.validate_ladder(
                ladder if ladder is not None else gpu_profile.DEFAULT_LADDER,
            )
        except ValueError as exc:
            raise ValidationError(f"ladder 非法：{exc}")
        # 先取 video duration 供后续 make_silent_wav 使用（tts_len = 2×video）
        try:
            from . import ffmpeg_pipeline as _vp
            v_probe = _vp.ffprobe_video(sv)
            audio_seconds = max(1.0, float(v_probe.duration) * 2.0)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"代表样本视频不可读：{exc}")

        # R14-FIX2 P0-1：**始终**独占——无论 scheduler 是否已存在。
        sched_ref = self._ensure_scheduler()

        # R14-FIX-3 P0-2：原子 CAS——从 idle/终态进入 preparing；
        # **preparing 也拒绝第二次启动**；分配唯一 generation；
        # 每次 benchmark 独立的 cancel_event 和 registry。
        with self._benchmark_lock:
            cur = self._benchmark_state.get("state", "idle")
            if cur in ("preparing", "running", "stopping"):
                raise ValidationError(
                    f"已有基准在 {cur} 状态，请先取消或等待完成"
                )
            self._benchmark_generation += 1
            gen = self._benchmark_generation
            self._benchmark_cancel = threading.Event()
            local_cancel = self._benchmark_cancel
            self._benchmark_registry = gpu_profile.BenchmarkProcessRegistry()
            local_registry = self._benchmark_registry
            self._benchmark_state = {
                "running": False,
                "state": "preparing",
                "generation": gen,
                "phase": "prepare",
                "concurrency_now": 0,
                "results": [],
                "error": "",
                "started_at": time.time(),
                "finished_at": 0.0,
                "sample_video": str(sv),
                "audio_seconds": audio_seconds,
                "exclusive": False,
            }

        def _finalize_error(msg: str, terminal_state: str = "error") -> None:
            """R14-FIX-3 P0-3：所有失败路径复用——只更新与本 gen 相同的状态。"""
            with self._benchmark_lock:
                if self._benchmark_state.get("generation") != gen:
                    return
                self._benchmark_state["state"] = terminal_state
                self._benchmark_state["phase"] = terminal_state
                self._benchmark_state["error"] = msg[:400]
                self._benchmark_state["finished_at"] = time.time()
                self._benchmark_state["running"] = False
            try:
                local_registry.terminate_all()
            except Exception:  # noqa: BLE001
                pass

        exclusive_ok = False
        try:
            exclusive_ok = sched_ref.enter_benchmark_exclusive(
                wait_seconds=exclusive_wait_seconds,
                cancel_event=local_cancel,
            )
        except Exception as exc:  # noqa: BLE001
            _finalize_error(f"benchmark 准备失败：{exc}", "error")
            raise ValidationError(f"benchmark 准备失败：{exc}")

        if not exclusive_ok:
            reject_msg = (
                "等待生产池收敛超时或有另一个 benchmark 在运行；"
                "请先手动暂停并等待任务完成后再试"
            )
            _finalize_error(reject_msg, "rejected")
            raise ValidationError(reject_msg)

        # 二次 CAS：只有本 gen 的 preparing 才能升 running
        with self._benchmark_lock:
            if self._benchmark_state.get("generation") == gen and \
                    self._benchmark_state.get("state") == "preparing":
                self._benchmark_state["state"] = "running"
                self._benchmark_state["running"] = True
                self._benchmark_state["exclusive"] = True

        def _bg() -> None:
            silent: Path | None = None
            silent_owned = False
            # R14-FIX-3 P0-3：**所有**准备步骤都在 try/except/finally 内
            try:
                ffmpeg = studio_settings.ffmpeg_tool("ffmpeg")
                tmp_dir = studio_settings.data_root() / "批量带货" / "基准_临时"
                tmp_dir.mkdir(parents=True, exist_ok=True)
                if sample_tts_audio:
                    silent = Path(sample_tts_audio).expanduser()
                else:
                    silent = tmp_dir / (
                        f"silent_{int(audio_seconds*10)}dS_"
                        f"{os.getpid()}_g{gen}.wav"
                    )
                    gpu_profile.make_silent_wav(silent, seconds=audio_seconds)
                    silent_owned = True

                def _cb(ev: dict) -> None:
                    with self._benchmark_lock:
                        # 只更新本 gen——旧线程不能覆盖新任务
                        if self._benchmark_state.get("generation") != gen:
                            return
                        self._benchmark_state["phase"] = ev.get("phase") or ""
                        self._benchmark_state["concurrency_now"] = \
                            ev.get("concurrency") or 0
                        if ev.get("phase") == "measure":
                            self._benchmark_state.setdefault(
                                "results", []
                            ).append(ev.get("result") or {})

                cap = gpu_profile.run_benchmark(
                    ffmpeg=ffmpeg, sample_video=sv,
                    sample_tts_audio=silent,
                    encoder_preference=encoder_preference,
                    zoom_percent=zoom_percent, keep_original_audio=False,
                    preset=preset, crf=crf,
                    ladder=ladder_list,
                    warmup_rounds=1, per_job_timeout=300.0,
                    measured_rounds=3,
                    progress_cb=_cb, cancel_event=local_cancel,
                    registry=local_registry,
                )
                cap.encoder_preference = encoder_preference or "auto"
                gpu_profile.save_profile(cap)
                with self._benchmark_lock:
                    if self._benchmark_state.get("generation") == gen:
                        self._gpu_profile = cap
                        self._benchmark_state["profile"] = cap.to_json()
                        self._benchmark_state["running"] = False
                        self._benchmark_state["state"] = "done"
                        self._benchmark_state["phase"] = "done"
                        self._benchmark_state["finished_at"] = time.time()
            except gpu_profile.BenchmarkCancelled:
                with self._benchmark_lock:
                    if self._benchmark_state.get("generation") == gen:
                        self._benchmark_state["running"] = False
                        self._benchmark_state["state"] = "cancelled"
                        self._benchmark_state["phase"] = "cancelled"
                        self._benchmark_state["finished_at"] = time.time()
            except Exception as exc:  # noqa: BLE001
                with self._benchmark_lock:
                    if self._benchmark_state.get("generation") == gen:
                        self._benchmark_state["running"] = False
                        self._benchmark_state["state"] = "error"
                        self._benchmark_state["phase"] = "error"
                        self._benchmark_state["error"] = str(exc)[:400]
                        self._benchmark_state["finished_at"] = time.time()
            finally:
                if silent_owned and silent is not None:
                    try: silent.unlink()
                    except OSError: pass
                try:
                    local_registry.terminate_all()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    sched_ref.leave_benchmark_exclusive()
                except Exception:  # noqa: BLE001
                    pass

        t = threading.Thread(target=_bg, name=f"gpu-benchmark-{gen}",
                              daemon=True)
        try:
            with self._benchmark_lock:
                self._benchmark_thread = t
            t.start()
        except Exception as exc:  # noqa: BLE001
            # R14-FIX-3 P0-3：Thread.start() 抛错也必须同步恢复 exclusive
            try:
                sched_ref.leave_benchmark_exclusive()
            except Exception:  # noqa: BLE001
                pass
            _finalize_error(f"benchmark 线程启动失败：{exc}", "error")
            raise ValidationError(f"benchmark 线程启动失败：{exc}")
        return {"started": True, "sample_video": str(sv),
                 "audio_seconds": audio_seconds,
                 "exclusive": exclusive_ok,
                 "generation": gen,
                 "state": "running"}

    def apply_gpu_profile(self) -> dict:
        """R14-FIX P0-5：应用 profile 前**必须重探当前指纹**——
        OS/FFmpeg/编码器/GPU/驱动 任一变化 → 拒绝应用，要求重新测试。
        R14-FIX P0-6：benchmark 独占期禁止 apply。
        R14-FIX2 P0-4：切到 AUTO 必须**清 encoder_override**——统一走
        `_apply_profile_and_mode`。
        R14-FIX2 P1-5：按 profile 保存的 encoder_preference 探测，不硬编码 auto。
        R14-FIX2 P1-6：save_state 失败在返回值中可见（persisted=false + warning）。
        """
        if self._gpu_profile is None:
            raise ValidationError("尚无 profile，请先运行显卡性能测试")
        if self._scheduler is not None and self._scheduler.is_benchmark_exclusive():
            raise ValidationError("benchmark 独占运行中，禁止 apply profile")
        ffmpeg = studio_settings.ffmpeg_tool("ffmpeg")
        pref = getattr(self._gpu_profile, "encoder_preference", None) or "auto"
        try:
            fresh = gpu_profile.detect_capability_metadata(
                ffmpeg, encoder_preference=pref,
            )
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"当前设备指纹探测失败：{exc}")
        if fresh.device_fingerprint != self._gpu_profile.device_fingerprint:
            raise ValidationError(
                "profile 已失效：当前设备/FFmpeg/编码器/驱动指纹与 profile "
                f"不匹配（profile={self._gpu_profile.device_fingerprint[:10]}…，"
                f"current={fresh.device_fingerprint[:10]}…）；请重新运行显卡性能测试。"
            )
        sched = self._ensure_scheduler_for_controller()
        rec = int(self._gpu_profile.recommended_concurrency or 1)
        sched.controller.set_profile_recommended(rec)
        # 统一 apply：mode=AUTO + 清 encoder_override + resize=rec
        result = self._apply_profile_and_mode(
            sched, mode=MODE_AUTO, target_video=rec,
        )
        # R14-FIX-3 P1-3：resize 失败绝不 persist——避免把失败状态写盘为成功
        if result["applied"]:
            persisted, warning = self._persist_state(
                mode=MODE_AUTO, sched=sched,
                profile_fp=self._gpu_profile.device_fingerprint,
            )
        else:
            persisted, warning = False, "resize 失败已回滚，未持久化 state"
        return {
            # requested_video / actual_video 精确暴露；applied_video_concurrency
            # 保留旧字段名，值取实际 serving（回滚后可能是旧值）
            "applied_video_concurrency": (
                result["actual_video"] if result["actual_video"] is not None
                else result["video_serving"]
            ),
            "requested_video": result["requested_video"],
            "actual_video": result["actual_video"],
            "applied": result,   # 结构化子对象
            "resize_ok": result["resize_ok"],
            "resize_error": result["resize_error"],
            "encoder_override": result["encoder_override"],
            "video_serving": result["video_serving"],
            "persisted": persisted,
            "persist_warning": warning,
            "controller": result["controller"],
        }

    def set_concurrency_mode(self, mode: str, *,
                              manual_video: int | None = None,
                              user_max: int | None = None) -> dict:
        if mode not in ALL_MODES:
            raise ValidationError(f"未知模式：{mode}")
        sched = self._ensure_scheduler_for_controller()
        # R14-FIX P0-6：benchmark 独占期禁止改模式
        if sched.is_benchmark_exclusive():
            raise ValidationError("benchmark 独占运行中，禁止切换模式")

        # R14-FIX2 P0-4 统一入口：mode + encoder_override + resize 在同一处
        target_video: int | None = None
        if mode == MODE_CPU_SAFE:
            target_video = 2
        elif mode == MODE_MANUAL and manual_video is not None:
            from .concurrency_controller import MAX_VIDEO_CONCURRENCY as _MVC
            target_video = max(1, min(_MVC, int(manual_video)))
        result = self._apply_profile_and_mode(
            sched, mode=mode, target_video=target_video,
            manual_video=manual_video, user_max=user_max,
        )
        fp = self._gpu_profile.device_fingerprint if self._gpu_profile else ""
        # R14-FIX-3 P1-3：resize 失败已回滚 → 不 persist，也不宣称成功
        if result["applied"]:
            persisted, warning = self._persist_state(
                mode=mode, sched=sched, profile_fp=fp,
            )
        else:
            persisted, warning = False, "resize 失败已回滚，未持久化 state"
        return {
            # 回滚后 mode 反映真实生效值
            "mode": result["mode"],
            "requested_mode": mode,
            "requested_video": result["requested_video"],
            "actual_video": result["actual_video"],
            "applied": result,
            "resize_ok": result["resize_ok"],
            "resize_error": result["resize_error"],
            "encoder_override": result["encoder_override"],
            "video_serving": result["video_serving"],
            "persisted": persisted,
            "persist_warning": warning,
            "controller": result["controller"],
        }

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
