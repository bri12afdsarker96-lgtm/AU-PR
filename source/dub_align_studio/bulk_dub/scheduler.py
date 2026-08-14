"""批处理调度器：SQLite 持久队列 → TTS 池 → 视频池 → 校验 → 原子提交。

本轮 R11 修复要点：
    R11-1 并发是"全局 worker 池"设置，`resize_pools(tts, video)` 安全增减线程数；
          snapshot 同时返回 configured / alive / active 三个数字；
    R11-2 批次暂停下推到 claim_next 的 SQL WHERE，杜绝领了再放回的死循环；
    R11-3 stop() 返回是否全部停止；不清空存活线程；启动时用 store 恢复接口
          （含视频层面"文件已提交 DB 未提交"补记 completed）；
          正常完成走 store.complete_task_transactional 单事务；
    R11-10 取消 → 释放 reservation + 清 staging；TTS 请求返回后取消也清 wav；
           异常 catch 分支同样清理；终态清 cancel flag。
"""

from __future__ import annotations

import threading
import time
import traceback
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .circuit_breaker import CircuitBreaker, classify_http_error, compute_backoff
from . import ffmpeg_pipeline as vp
from .ffmpeg_pipeline import VideoCancelled, VideoError
from .hw_encoder import EncoderProbe, default_video_concurrency, resolve_encoder
from .store import (
    STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING, STATUS_RETRY_WAIT,
    STATUS_TTS_DONE, STATUS_TTS_RUNNING, STATUS_VIDEO_RUNNING,
    STATUS_CANCELLED, STATUS_CANCELLING, STATUS_OUTPUT_COMMITTED,
    STATUS_WAITING_DEPENDENCY,
    TaskRow, TaskStore, is_safe_id,
)


class TtsBackend:
    """R13-P1-7：`requires_endpoint` 是 backend capability——生产 backend
    表达"必须先配置 Worker 才能启动"，mock backend 明示不需要。
    service 依此判断，不再暴露"跳过端点校验"的生产入口。"""
    requires_endpoint: bool = True

    def synthesize(self, *, text: str, voice_id: str, speed: float,
                   pitch: int, style: str, output_wav: Path) -> float:
        raise NotImplementedError


class TtsHttpError(Exception):
    def __init__(self, status_code: int, message: str,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass
class SchedulerConfig:
    """process-level 全局设置（不用于任务级覆盖）。"""
    tts_concurrency: int = 4
    video_concurrency: int = 0
    tts_max_retries: int = 5
    ffmpeg_timeout: float = 1800.0
    breaker_failure_threshold: int = 5
    breaker_open_seconds: float = 30.0


@dataclass
class SchedulerMetrics:
    finished_times: list[float] = field(default_factory=list)
    tts_times: list[float] = field(default_factory=list)
    video_times: list[float] = field(default_factory=list)
    http_429_count: int = 0
    http_5xx_count: int = 0
    retry_count: int = 0
    hw_fallback_count: int = 0

    def record_success(self, tts_seconds: float, video_seconds: float) -> None:
        self.finished_times.append(time.time())
        if tts_seconds > 0:
            self.tts_times.append(tts_seconds)
        if video_seconds > 0:
            self.video_times.append(video_seconds)
        for lst in (self.finished_times, self.tts_times, self.video_times):
            if len(lst) > 500:
                del lst[:-500]

    def recent_rate(self, window_seconds: float) -> float:
        cutoff = time.time() - window_seconds
        count = sum(1 for t in self.finished_times if t >= cutoff)
        return count / (window_seconds / 60.0)

    def avg(self, samples: list[float], last_n: int = 100) -> float:
        if not samples:
            return 0.0
        pick = samples[-last_n:]
        return sum(pick) / len(pick)


@dataclass
class _WorkerRec:
    """单个 worker 的状态。每个 worker 有独立 stop event，供 resize_pools 用。

    R12-5：draining=True 表示已发出 stop 请求；这类 worker **不计入 serving 数**。
    """
    thread: threading.Thread
    stop_event: threading.Event
    kind: str        # "tts" or "video"
    active: bool = False
    draining: bool = False


class Scheduler:
    def __init__(self, store: TaskStore, config: SchedulerConfig,
                 tts_backend: TtsBackend,
                 ffmpeg_path: str | None = None) -> None:
        self.store = store
        self.config = config
        self.tts_backend = tts_backend
        self._ffmpeg_path = ffmpeg_path
        self._encoder_cache: dict[str, EncoderProbe] = {}
        self._encoder_cache_lock = threading.Lock()
        self._cancel_flags: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()
        # 全局暂停
        self._pause = threading.Event()
        # 批次级暂停：batch_id 集合（用 dict[str,bool] 便于线程安全的读快照）
        self._batch_paused_set: set[str] = set()
        self._batch_pause_lock = threading.Lock()
        # 全局 stop（signal 所有 worker 退出）
        self._stop = threading.Event()
        self._cond = threading.Condition()
        # 每个 worker 独立记录（含 stop_event 用于 resize）
        self._tts_workers: list[_WorkerRec] = []
        self._video_workers: list[_WorkerRec] = []
        self._breaker = CircuitBreaker(
            failure_threshold=config.breaker_failure_threshold,
            open_seconds=config.breaker_open_seconds,
        )
        self._metrics_lock = threading.Lock()
        self.metrics = SchedulerMetrics()
        self._started_at: float = 0.0
        self._lifecycle_lock = threading.RLock()
        # active task 数（用于 UI 展示"正在跑的任务数"）
        self._active_lock = threading.Lock()
        self._active_tts = 0
        self._active_video = 0
        self._worker_index = 0

    # -------------------- 生命周期 --------------------

    def start(self) -> None:
        with self._lifecycle_lock:
            # R12-5：**分池**判断——TTS 全死时补 TTS；视频全死时补视频
            alive_tts = any(w.thread.is_alive() for w in self._tts_workers)
            alive_video = any(w.thread.is_alive() for w in self._video_workers)
            if not alive_tts and not alive_video:
                # 两池都无：正常首启
                self._tts_workers = []
                self._video_workers = []
                self._stop.clear()
                # 恢复运行中任务 + 加载持久暂停集合
                self.store.reap_and_recover_running(
                    tts_wav_ok=self._tts_wav_valid,
                    reserved_output_verifier=self._verify_reserved_output,
                    staging_cleanup=vp.cleanup_staging,
                )
                # R13-P0-1：leader 已恢复到终态后，follower 与 leader 对账
                # （幂等；孤儿 follower → failed，避免永远 waiting）
                try:
                    self.store.reconcile_dependencies()
                except Exception:  # noqa: BLE001
                    pass
                self._reload_paused_from_db()
                self._started_at = time.time()
                for _ in range(max(1, min(16, self.config.tts_concurrency))):
                    self._spawn_worker("tts")
                video_count = self.config.video_concurrency or default_video_concurrency()
                for _ in range(max(1, min(8, video_count))):
                    self._spawn_worker("video")
                return
            # R12-5：只有一池全死时补齐——不忽略缺失池
            if not alive_tts:
                self._tts_workers = [w for w in self._tts_workers
                                      if w.thread.is_alive()]
                for _ in range(max(1, min(16, self.config.tts_concurrency))):
                    self._spawn_worker("tts")
            if not alive_video:
                self._video_workers = [w for w in self._video_workers
                                        if w.thread.is_alive()]
                video_count = self.config.video_concurrency or default_video_concurrency()
                for _ in range(max(1, min(8, video_count))):
                    self._spawn_worker("video")

    def stop(self, wait_seconds: float = 5.0) -> bool:
        """安全停止：返回是否全部退出。存活线程**保留在跟踪列表里**——
        禁止在 join 超时后清空后新建"第二套 scheduler"。"""
        with self._lifecycle_lock:
            self._stop.set()
            for w in self._tts_workers + self._video_workers:
                w.stop_event.set()
            with self._cond:
                self._cond.notify_all()
            deadline = time.time() + wait_seconds
            for w in self._tts_workers + self._video_workers:
                remaining = max(0.05, deadline - time.time())
                w.thread.join(remaining)
            all_gone = all(not w.thread.is_alive()
                            for w in self._tts_workers + self._video_workers)
            if all_gone:
                self._tts_workers = []
                self._video_workers = []
            return all_gone

    def notify(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def _spawn_worker(self, kind: str) -> _WorkerRec:
        self._worker_index += 1
        stop_evt = threading.Event()
        if kind == "tts":
            target = self._tts_loop
            name = f"bulk-tts-{self._worker_index}"
        else:
            target = self._video_loop
            name = f"bulk-video-{self._worker_index}"
        t = threading.Thread(target=target, args=(stop_evt,), name=name, daemon=True)
        rec = _WorkerRec(thread=t, stop_event=stop_evt, kind=kind)
        if kind == "tts":
            self._tts_workers.append(rec)
        else:
            self._video_workers.append(rec)
        t.start()
        return rec

    # -------------------- 池 resize（R11-1）--------------------

    def resize_pools(self, *, tts: int | None = None,
                     video: int | None = None,
                     wait_seconds: float = 3.0) -> dict:
        """安全调整全局 worker 池大小。返回执行前后数字。

        - 增：追加新 worker
        - 减：把"多余"worker 的 stop_event 置位 → 它们在下一轮循环退出
        - 等 wait_seconds 让退出的 worker 真正 join；未退出的**保留跟踪**，不静默丢弃
        """
        with self._lifecycle_lock:
            before = {
                "tts": len(self._tts_workers),
                "video": len(self._video_workers),
                "tts_alive": sum(1 for w in self._tts_workers if w.thread.is_alive()),
                "video_alive": sum(1 for w in self._video_workers if w.thread.is_alive()),
            }
            if tts is not None:
                new_tts = max(1, min(16, int(tts)))
                self.config.tts_concurrency = new_tts
                self._resize_kind("tts", new_tts, wait_seconds)
            if video is not None:
                new_v = max(1, min(8, int(video)))
                self.config.video_concurrency = new_v
                self._resize_kind("video", new_v, wait_seconds)
            after = {
                "tts": len(self._tts_workers),
                "video": len(self._video_workers),
                "tts_alive": sum(1 for w in self._tts_workers if w.thread.is_alive()),
                "video_alive": sum(1 for w in self._video_workers if w.thread.is_alive()),
            }
            return {"before": before, "after": after}

    def _resize_kind(self, kind: str, target: int, wait_seconds: float) -> None:
        pool = self._tts_workers if kind == "tts" else self._video_workers
        # R12-5：serving = 存活且未 draining
        serving_pool = [w for w in pool if w.thread.is_alive() and not w.draining]
        current = len(serving_pool)
        if target > current:
            for _ in range(target - current):
                self._spawn_worker(kind)
        elif target < current:
            # 把最新加的 worker 先 drain（LIFO）——它们通常闲置概率更大
            to_stop = serving_pool[target:]
            for w in to_stop:
                w.draining = True
                w.stop_event.set()
            with self._cond:
                self._cond.notify_all()
            deadline = time.time() + wait_seconds
            for w in to_stop:
                remaining = max(0.05, deadline - time.time())
                w.thread.join(remaining)
            # 清 dead：还存活的 draining worker 保留跟踪，不重复计入 serving
        self._reconcile_pool(kind)

    def _reconcile_pool(self, kind: str) -> None:
        """R12-5：清 dead worker + 若 serving < target 再补足。"""
        target = (self.config.tts_concurrency if kind == "tts"
                   else (self.config.video_concurrency
                          or default_video_concurrency()))
        pool = self._tts_workers if kind == "tts" else self._video_workers
        pool[:] = [w for w in pool if w.thread.is_alive()]
        serving = [w for w in pool if not w.draining]
        deficit = max(0, target - len(serving))
        for _ in range(deficit):
            self._spawn_worker(kind)

    # -------------------- 恢复辅助 --------------------

    def _get_encoder(self, preference: str) -> EncoderProbe:
        pref = preference or "auto"
        with self._encoder_cache_lock:
            cached = self._encoder_cache.get(pref)
            if cached:
                return cached
        try:
            probe = resolve_encoder(
                self._ffmpeg_path or _default_ffmpeg(), preference=pref,
            )
        except Exception:  # noqa: BLE001
            probe = EncoderProbe("cpu", "libx264", [], True, "回退：libx264（探测异常）")
        with self._encoder_cache_lock:
            self._encoder_cache[pref] = probe
        return probe

    def _tts_wav_valid(self, row: TaskRow) -> bool:
        if not row.staging_dir:
            return False
        tts_path = Path(row.staging_dir) / "tts.wav"
        if not tts_path.is_file() or tts_path.stat().st_size < 128:
            return False
        try:
            with wave.open(str(tts_path), "rb") as w:
                return (w.getnframes() or 0) > 0
        except Exception:  # noqa: BLE001
            return False

    def _verify_reserved_output(self, row: TaskRow,
                                 path: Path) -> tuple[bool, dict]:
        """R13-P0-3 恢复：正式文件必须**同时**通过 marker + 内容校验才认领。

        校验顺序（任一失败 → False）：
            1. 目标目录内存在同名 marker sidecar；
            2. marker.schema ∈ {v1, v2}；task_id 匹配本任务；
            3. 若 marker.size/hash/output_name 存在 → 与 target 严格一致；
            4. ffprobe 有音视频流 + 时长 > 0；
            5. 时长与 marker.final_seconds 容差 <= 0.4s（可选）。

        无 marker（外部 mp4）→ 绝不认领。
        """
        try:
            if not path.is_file() or path.stat().st_size < 1024:
                return False, {}
            marker_path = vp.marker_path_for(path)
            marker = vp.read_marker(marker_path)
            if marker is None:
                return False, {}
            # 严格校验 marker 归属：task_id 必须一致（外部 mp4 拷进来撞名也不认）
            if marker.get("task_id") != row.task_id:
                return False, {}
            ok, reason = vp.verify_marker_matches_target(
                marker, path, expected_task_id=row.task_id,
                expected_batch_id=row.batch_id,
                expected_fingerprint=row.fingerprint,
            )
            if not ok:
                return False, {"reject_reason": reason}
            has_v, has_a = vp.has_video_and_audio_streams(path)
            if not (has_v and has_a):
                return False, {}
            actual = vp.ffprobe_seconds(path)
            if actual <= 0:
                return False, {}
            return True, {
                "final_duration": marker.get("final_seconds") or actual,
                "tts_duration": row.tts_duration or 0,
                "concat_duration": row.concat_duration or 0,
                "video_duration": row.video_duration or 0,
                "encoder_used": marker.get("encoder_used") or row.encoder_used or "unknown",
                "hw_fallback_used": int(bool(marker.get("hw_fallback_used") or row.hw_fallback_used)),
            }
        except Exception:  # noqa: BLE001
            return False, {}

    # -------------------- 暂停 / 恢复 / 取消 --------------------

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()
        with self._cond:
            self._cond.notify_all()

    def is_paused(self) -> bool:
        return self._pause.is_set()

    def pause_batch(self, batch_id: str) -> None:
        # R12-7：持久化到 batches 表；成功后再刷内存缓存
        self.store.set_batch_paused(batch_id, True)
        with self._batch_pause_lock:
            self._batch_paused_set.add(batch_id)

    def resume_batch(self, batch_id: str) -> None:
        self.store.set_batch_paused(batch_id, False)
        with self._batch_pause_lock:
            self._batch_paused_set.discard(batch_id)
        with self._cond:
            self._cond.notify_all()

    def _reload_paused_from_db(self) -> None:
        """启动/重连时从 batches.paused 加载持久暂停集合到内存。"""
        try:
            ids = self.store.list_paused_batch_ids()
        except Exception:  # noqa: BLE001
            ids = []
        with self._batch_pause_lock:
            self._batch_paused_set = set(ids)

    def is_batch_paused(self, batch_id: str) -> bool:
        with self._batch_pause_lock:
            return batch_id in self._batch_paused_set

    def _paused_batches_snapshot(self) -> tuple[str, ...]:
        with self._batch_pause_lock:
            return tuple(self._batch_paused_set)

    def cancel_task(self, task_id: str) -> bool:
        """R13-P0-2 取消：
        - waiting 类立即 cancelled，同事务广播 follower
        - running 类 → cancelling（worker 稳定点收敛 + 清 staging）；不由取消
          线程并发删除工作文件
        - terminal（completed/output_committed/failed/cancelled/cancelling）→
          返回 False；页面不会再显示"已取消"错觉
        """
        row = self.store.get(task_id)
        if row is None:
            return False
        # cancel flag 必须先置位——即使 running 也让 worker 尽快退出
        with self._cancel_lock:
            flag = self._cancel_flags.get(task_id)
            if flag is None:
                flag = threading.Event()
                self._cancel_flags[task_id] = flag
            flag.set()
        results = self.store.cancel_atomic_detailed([task_id])
        if not results:
            return False
        _, old, new = results[0]
        if new == STATUS_CANCELLED:
            # waiting 类：release reservation + 清 staging（worker 不在跑，安全）
            self.store.release_reservation(task_id)
            vp.cleanup_staging(row.staging_dir)
            # leader 取消 → 广播 follower（同事务）
            try:
                self.store.finalize_leader_cancel(
                    task_id, error_type="cancelled",
                    error_detail="leader 被取消",
                )
            except Exception:  # noqa: BLE001
                pass
            return True
        if new == STATUS_CANCELLING:
            # running：**不**并发清 staging 和 release reservation——由 worker
            # 见到 cancel_flag 时自行收敛并清理，避免与 ffmpeg/TTS worker 撞刀
            return True
        # 未变化：old 已是 terminal → 取消失败
        return False

    def cancel_all_waiting(self, batch_id: str) -> int:
        """R13-P0-2：批量取消——waiting 类立即 cancelled 并广播 follower；
        running 类转 cancelling 由 worker 收敛。绝不并发清 running 任务的
        staging/reservation。"""
        cancelled = self.store.cancel_batch_atomic(batch_id)
        n = 0
        for tid in cancelled:
            row = self.store.get(tid)
            if row is None:
                continue
            n += 1
            if row.status == STATUS_CANCELLED:
                self.store.release_reservation(tid)
                vp.cleanup_staging(row.staging_dir)
                try:
                    self.store.finalize_leader_cancel(
                        tid, error_type="cancelled",
                        error_detail="leader 被批量取消",
                    )
                except Exception:  # noqa: BLE001
                    pass
        return n

    def retry_failed(self, batch_id: str, only_retryable: bool = False) -> int:
        """R13-P0-1 重试语义：
        - 只把 **leader**（leader_task_id 为空）从 failed 转 pending；
        - failed **follower** 恢复为 waiting_dependency，不进入调度队列；
        - `error_type == 'excel_invalid'` 永远跳过；这类是 Excel 校验失败，
          重试也无意义，避免误入调度。
        """
        n = 0
        for row in self.store.list_tasks(batch_id=batch_id, status=STATUS_FAILED,
                                          limit=100000):
            if row.error_type == "excel_invalid":
                continue
            if only_retryable and row.error_type in ("client_4xx", "video_error"):
                continue
            if row.leader_task_id:
                # follower：只回到 waiting_dependency，不进入调度队列
                self.store.reset_status(row.task_id, STATUS_WAITING_DEPENDENCY)
            else:
                self.store.reset_status(row.task_id, STATUS_PENDING)
            with self._cancel_lock:
                self._cancel_flags.pop(row.task_id, None)
            n += 1
        self.notify()
        return n

    # -------------------- worker: TTS --------------------

    def _tts_loop(self, stop_evt: threading.Event) -> None:
        while not self._stop.is_set() and not stop_evt.is_set():
            if self._pause.is_set():
                self._wait(0.5, stop_evt)
                continue
            allow, wait = self._breaker.acquire()
            if not allow:
                self._wait(min(wait, 1.5), stop_evt)
                continue
            row = None
            try:
                paused = self._paused_batches_snapshot()
                row = self.store.claim_next(
                    (STATUS_PENDING, STATUS_RETRY_WAIT), STATUS_TTS_RUNNING,
                    respect_next_attempt=True,
                    exclude_batch_ids=paused,
                )
                if row is None:
                    self._breaker.release_probe()
                    self._wait(0.3, stop_evt)
                    continue
                self._active_incr("tts", 1)
                try:
                    self._process_tts(row)
                finally:
                    self._active_incr("tts", -1)
            except Exception as exc:  # noqa: BLE001
                self._breaker.release_probe()
                if row is not None:
                    self._fail(row.task_id, "scheduler_error",
                               f"调度异常：{exc}"[:400])
                    # 释放临时资源
                    self.store.release_reservation(row.task_id)
                    vp.cleanup_staging(row.staging_dir)
                self._wait(0.5, stop_evt)

    def _process_tts(self, row: TaskRow) -> None:
        started = time.time()
        cancel_flag = self._ensure_cancel_flag(row.task_id)
        if cancel_flag.is_set():
            self.store.update(row.task_id, status=STATUS_CANCELLED)
            self._breaker.release_probe()
            self._cancel_flag_pop(row.task_id)
            return

        staging = _pick_staging_dir(row)
        staging.mkdir(parents=True, exist_ok=True)
        self.store.update(row.task_id, staging_dir=str(staging),
                          started_at=started, stage="TTS 合成")
        self.store.bump_attempts(row.task_id)

        tts_wav = staging / "tts.wav"
        attempt = row.attempts + 1
        params = row.params_snapshot or {}
        try:
            duration = self.tts_backend.synthesize(
                text=row.text, voice_id=row.voice_id, speed=row.speed,
                pitch=int(params.get("pitch", 0)),
                style=str(params.get("style", "general")),
                output_wav=tts_wav,
            )
        except TtsHttpError as exc:
            self._handle_tts_http_error(row, exc, attempt)
            return
        except Exception as exc:  # noqa: BLE001
            self._handle_tts_generic_error(row, exc, attempt)
            return

        # R5+R11-10：请求后再次检查取消——不能让 tts_done 出现在被取消任务上
        if cancel_flag.is_set():
            # 尝试从 tts_running / cancelling 收敛到 cancelled
            for from_st in (STATUS_TTS_RUNNING, STATUS_CANCELLING):
                if self.store.try_advance_status(
                    row.task_id, from_status=from_st, to_status=STATUS_CANCELLED,
                    error_type="cancelled",
                    error_detail="TTS 请求返回后检测到取消",
                    finished_at=time.time(),
                ):
                    break
            self._breaker.release_probe()
            vp.cleanup_staging(str(staging))
            self._cancel_flag_pop(row.task_id)
            return

        self._breaker.record_success()
        elapsed = time.time() - started
        # R12-6：**条件推进**——只有仍是 tts_running 才推进到 tts_done；被取消的直接跳过
        advanced = self.store.try_advance_status(
            row.task_id, from_status=STATUS_TTS_RUNNING, to_status=STATUS_TTS_DONE,
            tts_duration=duration, stage="等待视频池",
        )
        if not advanced:
            # 极端时刻被取消了：清理 wav + staging；后续 video worker 不会领取
            vp.cleanup_staging(str(staging))
            self._cancel_flag_pop(row.task_id)
            return
        with self._metrics_lock:
            # R12-11：只在 TTS 阶段记录处理耗时（不再在视频完成时重复记）
            self.metrics.tts_times.append(elapsed)
            if len(self.metrics.tts_times) > 500:
                del self.metrics.tts_times[:-500]
        self.notify()

    def _converge_cancelled_if_flagged(self, row: TaskRow) -> bool:
        """R13-P0-2：TTS/视频返回后错误路径统一检查——若取消标志已置，
        把当前 tts_running/cancelling → cancelled，并**不**再走 retry_wait/fail。"""
        cancel_flag = self._ensure_cancel_flag(row.task_id)
        if not cancel_flag.is_set():
            return False
        for from_st in (STATUS_TTS_RUNNING, STATUS_VIDEO_RUNNING,
                         STATUS_CANCELLING):
            if self.store.try_advance_status(
                row.task_id, from_status=from_st, to_status=STATUS_CANCELLED,
                error_type="cancelled",
                error_detail="任务返回时检测到取消",
                finished_at=time.time(),
            ):
                # leader 取消 → 广播 follower
                try:
                    self.store.finalize_leader_cancel(
                        row.task_id, error_type="cancelled",
                        error_detail="TTS/视频返回时收敛为 cancelled",
                    )
                except Exception:  # noqa: BLE001
                    pass
                break
        self._breaker.release_probe()
        vp.cleanup_staging(row.staging_dir)
        self._cancel_flag_pop(row.task_id)
        return True

    def _handle_tts_http_error(self, row: TaskRow, exc: TtsHttpError,
                                attempt: int) -> None:
        # R13-P0-2：先看取消标志，避免覆盖 cancelling
        if self._converge_cancelled_if_flagged(row):
            return
        kind = classify_http_error(exc.status_code)
        with self._metrics_lock:
            if kind == "retryable_429":
                self.metrics.http_429_count += 1
            elif kind == "retryable_5xx":
                self.metrics.http_5xx_count += 1
        if kind == "client_4xx":
            self._fail(row.task_id, "client_4xx",
                       f"HTTP {exc.status_code}：{exc}")
            self._breaker.release_probe()
            return
        self._breaker.record_failure(retry_after_seconds=exc.retry_after)
        if attempt >= self.config.tts_max_retries:
            self._fail(row.task_id, kind,
                       f"HTTP {exc.status_code}：{exc}（已达最大重试）")
            return
        wait = exc.retry_after if exc.retry_after and exc.retry_after > 0 \
            else compute_backoff(attempt)
        next_at = time.time() + wait
        # R13-P0-2：retry_wait 转换必须是条件推进——若同一时刻被取消，不覆盖
        advanced = self.store.try_advance_status(
            row.task_id, from_status=STATUS_TTS_RUNNING,
            to_status=STATUS_RETRY_WAIT,
            stage=f"TTS 等待重试 ({int(wait)}s)",
            error_type=kind, error_detail=str(exc),
            next_attempt_at=next_at,
        )
        if not advanced:
            self._converge_cancelled_if_flagged(row)
            return
        with self._metrics_lock:
            self.metrics.retry_count += 1

    def _handle_tts_generic_error(self, row: TaskRow, exc: Exception,
                                   attempt: int) -> None:
        if self._converge_cancelled_if_flagged(row):
            return
        self._breaker.record_failure()
        if attempt >= self.config.tts_max_retries:
            self._fail(row.task_id, "tts_error", str(exc)[:400])
            return
        wait = compute_backoff(attempt)
        next_at = time.time() + wait
        advanced = self.store.try_advance_status(
            row.task_id, from_status=STATUS_TTS_RUNNING,
            to_status=STATUS_RETRY_WAIT,
            stage=f"TTS 等待重试 ({int(wait)}s)",
            error_type="tts_error", error_detail=str(exc)[:400],
            next_attempt_at=next_at,
        )
        if not advanced:
            self._converge_cancelled_if_flagged(row)
            return
        with self._metrics_lock:
            self.metrics.retry_count += 1

    # -------------------- worker: 视频 --------------------

    def _video_loop(self, stop_evt: threading.Event) -> None:
        while not self._stop.is_set() and not stop_evt.is_set():
            if self._pause.is_set():
                self._wait(0.5, stop_evt)
                continue
            row = None
            try:
                paused = self._paused_batches_snapshot()
                row = self.store.claim_next(
                    (STATUS_TTS_DONE,), STATUS_VIDEO_RUNNING,
                    exclude_batch_ids=paused,
                )
                if row is None:
                    self._wait(0.3, stop_evt)
                    continue
                self._active_incr("video", 1)
                try:
                    self._process_video(row)
                finally:
                    self._active_incr("video", -1)
            except Exception as exc:  # noqa: BLE001
                if row is not None:
                    self._fail(row.task_id, "scheduler_error",
                               f"调度异常：{exc}"[:400])
                    self.store.release_reservation(row.task_id)
                    vp.cleanup_staging(row.staging_dir)
                self._wait(0.5, stop_evt)

    def _process_video(self, row: TaskRow) -> None:
        started = time.time()
        cancel_flag = self._ensure_cancel_flag(row.task_id)
        if cancel_flag.is_set():
            self.store.update(row.task_id, status=STATUS_CANCELLED)
            self.store.release_reservation(row.task_id)
            vp.cleanup_staging(row.staging_dir)
            self._cancel_flag_pop(row.task_id)
            return

        params = row.params_snapshot or {}
        task_output_dir = params.get("output_dir") or ""
        if not task_output_dir:
            self._fail(row.task_id, "video_error",
                       "任务缺少输出目录（params_snapshot.output_dir 为空）")
            self.store.release_reservation(row.task_id)
            vp.cleanup_staging(row.staging_dir)
            return
        output_dir = Path(task_output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        keep_orig = bool(row.keep_original_audio)
        zoom_percent = int(params.get("zoom_percent", 130))
        encoder_pref = str(params.get("encoder_preference", "auto"))
        crf = int(params.get("crf", 20))
        preset = str(params.get("preset", "medium"))
        ffmpeg_timeout = float(params.get("ffmpeg_timeout", self.config.ffmpeg_timeout))

        voice_short = vp.voice_short_name(row.voice_name)
        default_name = vp.build_output_filename(row.input_video, voice_short)
        target_path = output_dir / default_name

        # R4：原子预留
        try:
            reserved = self.store.reserve_output_path(row.task_id, target_path)
        except Exception as exc:  # noqa: BLE001
            self._fail(row.task_id, "video_error", f"输出路径预留失败：{exc}")
            vp.cleanup_staging(row.staging_dir)
            return

        staging = Path(row.staging_dir)
        tts_wav = staging / "tts.wav"

        self.store.update(row.task_id, stage="视频渲染")
        try:
            probe = vp.ffprobe_video(row.input_video)
            self.store.update(row.task_id, video_duration=probe.duration,
                              concat_duration=probe.duration * 2)
            encoder = self._get_encoder(encoder_pref)
            result = vp.render_single(
                input_video=row.input_video,
                tts_audio=tts_wav,
                reserved_output=reserved,
                staging_dir=staging,
                encoder=encoder,
                keep_original_audio=keep_orig,
                zoom_percent=zoom_percent,
                preset=preset,
                crf=crf,
                video_probe=probe,
                tts_seconds=row.tts_duration,
                cancel_flag=cancel_flag,
                timeout=ffmpeg_timeout,
                allow_hw_fallback=True,
                task_id=row.task_id,
                fingerprint=row.fingerprint,
                batch_id=row.batch_id,
            )
        except VideoCancelled:
            self.store.try_advance_status(
                row.task_id, from_status=STATUS_VIDEO_RUNNING,
                to_status=STATUS_CANCELLED,
                error_type="cancelled", error_detail="视频渲染中被取消",
                finished_at=time.time(),
            )
            self.store.try_advance_status(
                row.task_id, from_status=STATUS_CANCELLING,
                to_status=STATUS_CANCELLED,
                error_type="cancelled", error_detail="视频渲染中被取消",
                finished_at=time.time(),
            )
            self.store.release_reservation(row.task_id)
            vp.cleanup_staging(staging)
            self._cancel_flag_pop(row.task_id)
            return
        except VideoError as exc:
            self._fail(row.task_id, "video_error", str(exc)[:400])
            self.store.release_reservation(row.task_id)
            vp.cleanup_staging(staging)
            return
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exception_only(exc)
            self._fail(row.task_id, "video_error",
                       f"未预期错误：{''.join(tb).strip()[:400]}")
            self.store.release_reservation(row.task_id)
            vp.cleanup_staging(staging)
            return

        # R12-4：**正式文件已落地** → 先切 output_committed，DB 未写 completed 也能恢复
        marker_moved = self.store.mark_output_committed(row.task_id, result.output_path)
        if not marker_moved:
            # 状态不是 video_running（例如被取消）→ 不能写 completed；
            # 已落文件由恢复逻辑按 marker 认领或清理
            vp.cleanup_staging(staging)
            self._converge_cancelled_if_flagged(row)
            self._cancel_flag_pop(row.task_id)
            return

        elapsed = time.time() - started
        if result.hw_fallback_used:
            with self._metrics_lock:
                self.metrics.hw_fallback_count += 1
        # R13-P0-1：leader 完成 + follower 广播走**同一** SQLite 事务
        ok, follower_affected = self.store.finalize_leader_success(
            row.task_id,
            output_path=result.output_path,
            final_duration=result.final_duration,
            tts_duration=result.tts_duration,
            concat_duration=result.concat_duration,
            video_duration=probe.duration,
            encoder_used=result.encoder_used or "libx264",
            hw_fallback_used=result.hw_fallback_used,
            warnings_to_add=list(result.warnings),
            expected_statuses=(STATUS_VIDEO_RUNNING, STATUS_OUTPUT_COMMITTED),
            expected_reserved_path=str(reserved),
        )
        vp.cleanup_staging(staging)
        self._cancel_flag_pop(row.task_id)
        if ok and follower_affected:
            with self._metrics_lock:
                for _ in range(follower_affected):
                    self.metrics.record_success(0, 0)
        with self._metrics_lock:
            # R12-11：**只**记录视频阶段耗时；TTS 耗时在 _process_tts 里记
            self.metrics.record_success(0, elapsed)

    # -------------------- 辅助 --------------------

    def _ensure_cancel_flag(self, task_id: str) -> threading.Event:
        with self._cancel_lock:
            flag = self._cancel_flags.get(task_id)
            if flag is None:
                flag = threading.Event()
                self._cancel_flags[task_id] = flag
            return flag

    def _cancel_flag_pop(self, task_id: str) -> None:
        """R11-10：终态清 cancel flag，防止长期内存增长。"""
        with self._cancel_lock:
            self._cancel_flags.pop(task_id, None)

    def _fail(self, task_id: str, error_type: str, detail: str) -> None:
        """R13-P0-2 条件失败 + follower 广播：绝不覆盖 cancelled/cancelling/
        completed/output_committed。若 leader 因状态不符没写 failed，也不广播
        follower——否则会让 cancelled 任务的 follower 被误标 failed。"""
        ok, old = self.store.fail_task_cas(
            task_id, error_type=error_type, error_detail=detail,
        )
        if ok:
            try:
                self.store.finalize_leader_fail(
                    task_id, error_type=error_type, error_detail=detail,
                )
            except Exception:  # noqa: BLE001
                pass
        # 若 old ∈ (cancelling, cancelled) → 交给取消收敛路径处理
        self._cancel_flag_pop(task_id)

    def _active_incr(self, kind: str, delta: int) -> None:
        with self._active_lock:
            if kind == "tts":
                self._active_tts = max(0, self._active_tts + delta)
            else:
                self._active_video = max(0, self._active_video + delta)

    def _wait(self, seconds: float, stop_evt: threading.Event | None = None) -> None:
        if stop_evt is not None:
            stop_evt.wait(timeout=max(0.05, min(seconds, 1.0)))
            return
        with self._cond:
            self._cond.wait(timeout=max(0.05, seconds))

    def snapshot(self) -> dict:
        with self._metrics_lock:
            avg_tts = self.metrics.avg(self.metrics.tts_times)
            avg_video = self.metrics.avg(self.metrics.video_times)
            recent1 = self.metrics.recent_rate(60)
            recent5 = self.metrics.recent_rate(300)
            recent60 = self.metrics.recent_rate(3600)
            tts_samples = len(self.metrics.tts_times)
            video_samples = len(self.metrics.video_times)
            m = {
                "http_429_count": self.metrics.http_429_count,
                "http_5xx_count": self.metrics.http_5xx_count,
                "retry_count": self.metrics.retry_count,
                "hw_fallback_count": self.metrics.hw_fallback_count,
                # R12-11：TTS 处理耗时（不再叠加音频时长）
                "avg_tts_processing_seconds": round(avg_tts, 2),
                "avg_video_seconds": round(avg_video, 2),
                "tts_samples": tts_samples,
                "video_samples": video_samples,
                "recent_rate_1min": round(recent1, 2),
                "recent_rate_5min": round(recent5, 2),
                "recent_rate_60min": round(recent60, 2),
                "projected_24h_estimate": round(recent60 * 60 * 24, 0) if recent60 else 0,
                # R12-11：不足 1 小时窗口的样本视为低置信
                "projection_confidence": ("low" if (recent60 <= 0
                                                       or tts_samples < 20
                                                       or video_samples < 20)
                                              else "ok"),
            }
        with self._active_lock:
            active_tts = self._active_tts
            active_video = self._active_video
        with self._batch_pause_lock:
            batches_paused = sorted(self._batch_paused_set)
        # R12-5：snapshot 前 reconcile；alive = serving（非 draining）
        self._reconcile_pool("tts")
        self._reconcile_pool("video")
        tts_alive = sum(1 for w in self._tts_workers
                         if w.thread.is_alive() and not w.draining)
        video_alive = sum(1 for w in self._video_workers
                           if w.thread.is_alive() and not w.draining)
        tts_draining = sum(1 for w in self._tts_workers
                            if w.thread.is_alive() and w.draining)
        video_draining = sum(1 for w in self._video_workers
                              if w.thread.is_alive() and w.draining)
        return {
            "started_at": self._started_at,
            "paused": self._pause.is_set(),
            "stopped": self._stop.is_set(),
            "batches_paused": batches_paused,
            # R11-1：三段数字都返回
            "tts_configured": self.config.tts_concurrency,
            "tts_alive": tts_alive,
            "tts_draining": tts_draining,
            "tts_active": active_tts,
            "video_configured": (self.config.video_concurrency
                                  or default_video_concurrency()),
            "video_alive": video_alive,
            "video_draining": video_draining,
            "video_active": active_video,
            # 兼容旧字段
            "tts_concurrency": tts_alive,
            "video_concurrency": video_alive,
            "breaker": self._breaker.snapshot(),
            "metrics": m,
            "note_24h": "24h 数字为按当前实测速度推算，仅供参考",
        }


def _default_ffmpeg() -> str:
    from .. import settings as studio_settings

    return studio_settings.ffmpeg_tool("ffmpeg")


def _pick_staging_dir(row: TaskRow) -> Path:
    from .. import settings as studio_settings

    if not is_safe_id(row.batch_id) or not is_safe_id(row.task_id):
        raise RuntimeError("batch_id/task_id 非受控标识，拒绝生成 staging 路径")
    root = studio_settings.data_root() / "批量带货" / "staging"
    return root / row.batch_id / row.task_id
