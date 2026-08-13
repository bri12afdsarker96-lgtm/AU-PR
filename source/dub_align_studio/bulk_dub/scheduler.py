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
    STATUS_CANCELLED, TaskRow, TaskStore, is_safe_id,
)


class TtsBackend:
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
    """单个 worker 的状态。每个 worker 有独立 stop event，供 resize_pools 用。"""
    thread: threading.Thread
    stop_event: threading.Event
    kind: str        # "tts" or "video"
    active: bool = False


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
            # 已有存活线程 → 不重启，直接返回（防止创建第二套 scheduler）
            alive_any = any(w.thread.is_alive()
                             for w in self._tts_workers + self._video_workers)
            if alive_any:
                return
            # 清 dead 残留
            self._tts_workers = []
            self._video_workers = []
            self._stop.clear()
            # 启动恢复（含"文件已提交 DB 未提交"补记 completed）
            self.store.reap_and_recover_running(
                tts_wav_ok=self._tts_wav_valid,
                reserved_output_verifier=self._verify_reserved_output,
                staging_cleanup=vp.cleanup_staging,
            )
            self._started_at = time.time()
            for _ in range(max(1, min(16, self.config.tts_concurrency))):
                self._spawn_worker("tts")
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
        alive_pool = [w for w in pool if w.thread.is_alive()]
        current = len(alive_pool)
        if target > current:
            for _ in range(target - current):
                self._spawn_worker(kind)
        elif target < current:
            # 把最新加的 worker 先停（LIFO）——它们通常闲置概率更大
            to_stop = alive_pool[target:]
            for w in to_stop:
                w.stop_event.set()
            with self._cond:
                self._cond.notify_all()
            deadline = time.time() + wait_seconds
            for w in to_stop:
                remaining = max(0.05, deadline - time.time())
                w.thread.join(remaining)
            # 清理已经真的死了的（还活着的保留跟踪）
            if kind == "tts":
                self._tts_workers = [w for w in self._tts_workers
                                      if w.thread.is_alive() or (w not in to_stop)]
            else:
                self._video_workers = [w for w in self._video_workers
                                        if w.thread.is_alive() or (w not in to_stop)]

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
        """R11-3 恢复：预留正式文件已存在 → 校验后返回 meta 用于补记 completed。

        校验：视频流 + 音频流 + ffprobe 时长 ≈ 已知 tts_duration or > 0。
        """
        try:
            if not path.is_file() or path.stat().st_size < 1024:
                return False, {}
            has_v, has_a = vp.has_video_and_audio_streams(path)
            if not (has_v and has_a):
                return False, {}
            actual = vp.ffprobe_seconds(path)
            if actual <= 0:
                return False, {}
            return True, {
                "final_duration": actual,
                "tts_duration": row.tts_duration or 0,
                "concat_duration": row.concat_duration or 0,
                "video_duration": row.video_duration or 0,
                "encoder_used": row.encoder_used or "unknown",
                "hw_fallback_used": row.hw_fallback_used,
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
        with self._batch_pause_lock:
            self._batch_paused_set.add(batch_id)

    def resume_batch(self, batch_id: str) -> None:
        with self._batch_pause_lock:
            self._batch_paused_set.discard(batch_id)
        with self._cond:
            self._cond.notify_all()

    def is_batch_paused(self, batch_id: str) -> bool:
        with self._batch_pause_lock:
            return batch_id in self._batch_paused_set

    def _paused_batches_snapshot(self) -> tuple[str, ...]:
        with self._batch_pause_lock:
            return tuple(self._batch_paused_set)

    def cancel_task(self, task_id: str) -> bool:
        row = self.store.get(task_id)
        if row is None:
            return False
        with self._cancel_lock:
            flag = self._cancel_flags.get(task_id)
            if flag is None:
                flag = threading.Event()
                self._cancel_flags[task_id] = flag
            flag.set()
        # 未开始/等待类：立刻标 cancelled + 清 staging + 释放 reservation
        if row.status in (STATUS_PENDING, STATUS_RETRY_WAIT, STATUS_TTS_DONE):
            self.store.update(task_id, status=STATUS_CANCELLED,
                              error_type="cancelled", error_detail="用户取消")
            self.store.release_reservation(task_id)
            vp.cleanup_staging(row.staging_dir)
        return True

    def cancel_all_waiting(self, batch_id: str) -> int:
        """R11-10：等待类含 pending + retry_wait + tts_done；每条都要清 staging+reservation。"""
        n = 0
        for st in (STATUS_PENDING, STATUS_RETRY_WAIT, STATUS_TTS_DONE):
            for row in self.store.list_tasks(batch_id=batch_id, status=st, limit=100000):
                self.store.update(row.task_id, status=STATUS_CANCELLED,
                                   error_type="cancelled", error_detail="用户批量取消")
                self.store.release_reservation(row.task_id)
                vp.cleanup_staging(row.staging_dir)
                n += 1
        return n

    def retry_failed(self, batch_id: str, only_retryable: bool = False) -> int:
        n = 0
        for row in self.store.list_tasks(batch_id=batch_id, status=STATUS_FAILED,
                                          limit=100000):
            if only_retryable and row.error_type in ("client_4xx", "excel_invalid",
                                                     "video_error"):
                continue
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
            self.store.update(row.task_id, status=STATUS_CANCELLED,
                              error_type="cancelled",
                              error_detail="TTS 请求返回后检测到取消")
            self._breaker.release_probe()
            # 清 wav 和 staging（release_probe 不清 wav；这里主动清）
            vp.cleanup_staging(str(staging))
            self._cancel_flag_pop(row.task_id)
            return

        self._breaker.record_success()
        elapsed = time.time() - started
        self.store.update(row.task_id, status=STATUS_TTS_DONE,
                          tts_duration=duration, stage="等待视频池")
        with self._metrics_lock:
            self.metrics.tts_times.append(elapsed)
            if len(self.metrics.tts_times) > 500:
                del self.metrics.tts_times[:-500]
        self.notify()

    def _handle_tts_http_error(self, row: TaskRow, exc: TtsHttpError,
                                attempt: int) -> None:
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
        self.store.update(row.task_id, status=STATUS_RETRY_WAIT,
                          stage=f"TTS 等待重试 ({int(wait)}s)",
                          error_type=kind, error_detail=str(exc),
                          next_attempt_at=next_at)
        with self._metrics_lock:
            self.metrics.retry_count += 1

    def _handle_tts_generic_error(self, row: TaskRow, exc: Exception,
                                   attempt: int) -> None:
        self._breaker.record_failure()
        if attempt >= self.config.tts_max_retries:
            self._fail(row.task_id, "tts_error", str(exc)[:400])
            return
        wait = compute_backoff(attempt)
        next_at = time.time() + wait
        self.store.update(row.task_id, status=STATUS_RETRY_WAIT,
                          stage=f"TTS 等待重试 ({int(wait)}s)",
                          error_type="tts_error", error_detail=str(exc)[:400],
                          next_attempt_at=next_at)
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
            )
        except VideoCancelled:
            self.store.update(row.task_id, status=STATUS_CANCELLED,
                              error_type="cancelled",
                              error_detail="视频渲染中被取消")
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

        elapsed = time.time() - started
        if result.hw_fallback_used:
            with self._metrics_lock:
                self.metrics.hw_fallback_count += 1
        # R11-3 单事务完成：写 output_path + 清 reservation + 记 warnings + 编码器/时长/completed
        self.store.complete_task_transactional(
            row.task_id,
            output_path=result.output_path,
            final_duration=result.final_duration,
            tts_duration=result.tts_duration,
            concat_duration=result.concat_duration,
            video_duration=probe.duration,
            encoder_used=result.encoder_used or "libx264",
            hw_fallback_used=result.hw_fallback_used,
            warnings_to_add=list(result.warnings),
        )
        vp.cleanup_staging(staging)
        self._cancel_flag_pop(row.task_id)
        with self._metrics_lock:
            self.metrics.record_success(row.tts_duration, elapsed)

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
        self.store.update(task_id, status=STATUS_FAILED,
                          error_type=error_type, error_detail=detail,
                          finished_at=time.time(), stage="失败")
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
            m = {
                "http_429_count": self.metrics.http_429_count,
                "http_5xx_count": self.metrics.http_5xx_count,
                "retry_count": self.metrics.retry_count,
                "hw_fallback_count": self.metrics.hw_fallback_count,
                "avg_tts_seconds": round(avg_tts, 2),
                "avg_video_seconds": round(avg_video, 2),
                "recent_rate_1min": round(recent1, 2),
                "recent_rate_5min": round(recent5, 2),
                "recent_rate_60min": round(recent60, 2),
                "projected_24h_estimate": round(recent60 * 60 * 24, 0) if recent60 else 0,
            }
        with self._active_lock:
            active_tts = self._active_tts
            active_video = self._active_video
        with self._batch_pause_lock:
            batches_paused = sorted(self._batch_paused_set)
        tts_alive = sum(1 for w in self._tts_workers if w.thread.is_alive())
        video_alive = sum(1 for w in self._video_workers if w.thread.is_alive())
        return {
            "started_at": self._started_at,
            "paused": self._pause.is_set(),
            "stopped": self._stop.is_set(),
            "batches_paused": batches_paused,
            # R11-1：三段数字都返回
            "tts_configured": self.config.tts_concurrency,
            "tts_alive": tts_alive,
            "tts_active": active_tts,
            "video_configured": (self.config.video_concurrency
                                  or default_video_concurrency()),
            "video_alive": video_alive,
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
