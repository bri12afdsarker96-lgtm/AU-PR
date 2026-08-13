"""批处理调度器：SQLite 持久队列 → TTS 池 → 视频池 → 校验 → 原子提交。

R2/R3/R4/R5/R6 修复：
    - 每条任务严格用 params_snapshot 决定 output_dir/voice/encoder/zoom/keep_original 等；
      SchedulerConfig 只提供池大小 / 编码偏好等 process-level 默认，不用于任务级参数覆盖；
    - 新增批次不重启 scheduler；同一 scheduler 处理所有批次；
    - 启动时用 reap_and_recover_running() 分类恢复；tts.wav 完整 → tts_done，否则 pending；
    - retry_wait 的下次到期时间持久化到 SQLite (next_attempt_at)；重启后 claim_next 仍尊重；
    - HALF_OPEN 探测锁带租约超时；线程异常/无任务领取时主动 release_probe()；
    - 取消检查两次：请求前 + 请求后；已取消任务不进入下一阶段；
    - 输出路径用 store.reserve_output_path() 原子预留；渲染完 commit_output_path()；
    - 硬件编码运行失败 → 自动回退 libx264；warnings 落库并转 CSV/UI。
"""

from __future__ import annotations

import threading
import time
import traceback
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
    """process-level 默认（不用于任务级覆盖）。

    每条任务级参数（output_dir/voice/speed/zoom/encoder/keep_original）
    严格从 task.params_snapshot 读取。
    """
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


class Scheduler:
    def __init__(self, store: TaskStore, config: SchedulerConfig,
                 tts_backend: TtsBackend,
                 ffmpeg_path: str | None = None) -> None:
        self.store = store
        self.config = config
        self.tts_backend = tts_backend
        self._ffmpeg_path = ffmpeg_path
        # process-level 默认编码器缓存（按 preference）——每条任务按 params_snapshot 再挑
        self._encoder_cache: dict[str, EncoderProbe] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()
        # 池级暂停（全局领取控制，语义：暂停领新任务；正在跑的任务不动）
        self._pause = threading.Event()
        # 按批次暂停（batch_id → Event）
        self._batch_pause: dict[str, threading.Event] = {}
        self._batch_pause_lock = threading.Lock()
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._tts_workers: list[threading.Thread] = []
        self._video_workers: list[threading.Thread] = []
        self._breaker = CircuitBreaker(
            failure_threshold=config.breaker_failure_threshold,
            open_seconds=config.breaker_open_seconds,
        )
        self._metrics_lock = threading.Lock()
        self.metrics = SchedulerMetrics()
        self._started_at: float = 0.0
        # 保护 workers 生命周期
        self._lifecycle_lock = threading.Lock()

    # -------------------- 启动 / 停止 --------------------

    def start(self) -> None:
        with self._lifecycle_lock:
            # 旧 worker 未真正退出时不启新（防同一任务被两套线程处理）
            if self._tts_workers or self._video_workers:
                alive = any(t.is_alive() for t in self._tts_workers + self._video_workers)
                if alive:
                    return
                # 清理 dead thread 残留
                self._tts_workers.clear()
                self._video_workers.clear()
                self._stop.clear()
            # R3：分类恢复运行中任务（自动排队；不留 interrupted）
            self.store.reap_and_recover_running(tts_wav_ok=self._tts_wav_valid)
            self._started_at = time.time()
            for i in range(max(1, min(16, self.config.tts_concurrency))):
                t = threading.Thread(target=self._tts_loop, name=f"bulk-tts-{i+1}",
                                     daemon=True)
                t.start()
                self._tts_workers.append(t)
            video_count = self.config.video_concurrency or default_video_concurrency()
            for i in range(max(1, min(8, video_count))):
                t = threading.Thread(target=self._video_loop, name=f"bulk-video-{i+1}",
                                     daemon=True)
                t.start()
                self._video_workers.append(t)

    def stop(self, wait_seconds: float = 5.0) -> None:
        """安全停止：唤醒所有 worker 走到 loop 顶部检测 _stop 后退出。"""
        with self._lifecycle_lock:
            self._stop.set()
            with self._cond:
                self._cond.notify_all()
            deadline = time.time() + wait_seconds
            for t in self._tts_workers + self._video_workers:
                remaining = max(0.05, deadline - time.time())
                t.join(remaining)
            self._tts_workers.clear()
            self._video_workers.clear()

    def notify(self) -> None:
        with self._cond:
            self._cond.notify_all()

    def _get_encoder(self, preference: str) -> EncoderProbe:
        """按任务参数指定的偏好挑编码器；带小缓存。"""
        pref = preference or "auto"
        cached = self._encoder_cache.get(pref)
        if cached:
            return cached
        try:
            probe = resolve_encoder(
                self._ffmpeg_path or _default_ffmpeg(), preference=pref,
            )
        except Exception:  # noqa: BLE001
            probe = EncoderProbe("cpu", "libx264", [], True, "回退：libx264（探测异常）")
        self._encoder_cache[pref] = probe
        return probe

    def _tts_wav_valid(self, row: TaskRow) -> bool:
        """启动恢复时判断：video_running 任务的 tts.wav 是否可用。"""
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

    # -------------------- 暂停 / 恢复 / 取消 --------------------

    def pause(self) -> None:
        """全局暂停领取新任务。"""
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()
        with self._cond:
            self._cond.notify_all()

    def is_paused(self) -> bool:
        return self._pause.is_set()

    def pause_batch(self, batch_id: str) -> None:
        with self._batch_pause_lock:
            evt = self._batch_pause.get(batch_id) or threading.Event()
            evt.set()
            self._batch_pause[batch_id] = evt

    def resume_batch(self, batch_id: str) -> None:
        with self._batch_pause_lock:
            evt = self._batch_pause.get(batch_id)
            if evt is not None:
                evt.clear()
        with self._cond:
            self._cond.notify_all()

    def is_batch_paused(self, batch_id: str) -> bool:
        with self._batch_pause_lock:
            evt = self._batch_pause.get(batch_id)
            return bool(evt and evt.is_set())

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
        if row.status in (STATUS_PENDING, STATUS_RETRY_WAIT, STATUS_TTS_DONE):
            self.store.update(task_id, status=STATUS_CANCELLED,
                              error_type="cancelled", error_detail="用户取消")
            self.store.release_reservation(task_id)
            vp.cleanup_staging(row.staging_dir)
        return True

    def cancel_all_waiting(self, batch_id: str) -> int:
        n = self.store.bulk_reset(batch_id, STATUS_PENDING, STATUS_CANCELLED)
        n += self.store.bulk_reset(batch_id, STATUS_RETRY_WAIT, STATUS_CANCELLED)
        return n

    def retry_failed(self, batch_id: str, only_retryable: bool = False) -> int:
        n = 0
        for row in self.store.list_tasks(batch_id=batch_id, status=STATUS_FAILED,
                                          limit=100000):
            if only_retryable and row.error_type in ("client_4xx", "excel_invalid",
                                                     "video_error"):
                continue
            self.store.reset_status(row.task_id, STATUS_PENDING)
            # 清掉旧取消标志
            with self._cancel_lock:
                self._cancel_flags.pop(row.task_id, None)
            n += 1
        self.notify()
        return n

    # -------------------- worker: TTS --------------------

    def _tts_loop(self) -> None:
        while not self._stop.is_set():
            if self._pause.is_set():
                self._wait(0.5)
                continue
            allow, wait = self._breaker.acquire()
            if not allow:
                self._wait(min(wait, 1.5))
                continue
            row = None
            try:
                row = self.store.claim_next(
                    (STATUS_PENDING, STATUS_RETRY_WAIT), STATUS_TTS_RUNNING,
                    respect_next_attempt=True,
                )
                if row is None:
                    # 探测锁必须释放，否则 HALF_OPEN 会永久锁住
                    self._breaker.release_probe()
                    self._wait(0.3)
                    continue
                # 批次级暂停：领了就放回去 pending，避免占用探测
                if self.is_batch_paused(row.batch_id):
                    self.store.update(row.task_id, status=STATUS_PENDING)
                    self._breaker.release_probe()
                    self._wait(0.4)
                    continue
                self._process_tts(row)
            except Exception as exc:  # noqa: BLE001 —— worker 必须存活
                # 意外异常也要释放探测锁
                self._breaker.release_probe()
                if row is not None:
                    self._fail(row.task_id, "scheduler_error",
                               f"调度异常：{exc}"[:400])
                self._wait(0.5)

    def _process_tts(self, row: TaskRow) -> None:
        started = time.time()
        cancel_flag = self._ensure_cancel_flag(row.task_id)
        # 请求前检查取消
        if cancel_flag.is_set():
            self.store.update(row.task_id, status=STATUS_CANCELLED)
            self._breaker.release_probe()
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

        # R5：请求后再次检查取消——防止任务已被取消却写成 tts_done
        if cancel_flag.is_set():
            self.store.update(row.task_id, status=STATUS_CANCELLED,
                              error_type="cancelled",
                              error_detail="TTS 请求返回后检测到取消")
            self._breaker.release_probe()
            return

        self._breaker.record_success()
        elapsed = time.time() - started
        self.store.update(row.task_id, status=STATUS_TTS_DONE,
                          tts_duration=duration, stage="等待视频池")
        with self._metrics_lock:
            self.metrics.tts_times.append(elapsed)
            if len(self.metrics.tts_times) > 500:
                del self.metrics.tts_times[:-500]
        # 完成即清理该 task 的取消标志内存
        with self._cancel_lock:
            self._cancel_flags.pop(row.task_id, None)
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
        # 可重试：更新熔断
        self._breaker.record_failure(retry_after_seconds=exc.retry_after)
        if attempt >= self.config.tts_max_retries:
            self._fail(row.task_id, kind,
                       f"HTTP {exc.status_code}：{exc}（已达最大重试）")
            return
        wait = exc.retry_after if exc.retry_after and exc.retry_after > 0 \
            else compute_backoff(attempt)
        next_at = time.time() + wait
        # R5：持久化下次到期时间到 SQLite；重启后仍生效
        self.store.update(row.task_id, status=STATUS_RETRY_WAIT,
                          stage=f"TTS 等待重试 ({int(wait)}s)",
                          error_type=kind, error_detail=str(exc),
                          next_attempt_at=next_at)
        with self._metrics_lock:
            self.metrics.retry_count += 1

    def _handle_tts_generic_error(self, row: TaskRow, exc: Exception,
                                   attempt: int) -> None:
        # 通用网络异常（不该到这里，edge_backend 已转 TtsHttpError；
        # 兜底：视为可重试并计入熔断——避免"网络故障不熔断"的漏洞）
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

    def _video_loop(self) -> None:
        while not self._stop.is_set():
            if self._pause.is_set():
                self._wait(0.5)
                continue
            row = None
            try:
                row = self.store.claim_next((STATUS_TTS_DONE,), STATUS_VIDEO_RUNNING)
                if row is None:
                    self._wait(0.3)
                    continue
                if self.is_batch_paused(row.batch_id):
                    self.store.update(row.task_id, status=STATUS_TTS_DONE)
                    self._wait(0.4)
                    continue
                self._process_video(row)
            except Exception as exc:  # noqa: BLE001
                if row is not None:
                    self._fail(row.task_id, "scheduler_error",
                               f"调度异常：{exc}"[:400])
                self._wait(0.5)

    def _process_video(self, row: TaskRow) -> None:
        started = time.time()
        cancel_flag = self._ensure_cancel_flag(row.task_id)
        if cancel_flag.is_set():
            self.store.update(row.task_id, status=STATUS_CANCELLED)
            self.store.release_reservation(row.task_id)
            vp.cleanup_staging(row.staging_dir)
            return

        params = row.params_snapshot or {}
        # 严格按任务级参数读取
        task_output_dir = params.get("output_dir") or ""
        if not task_output_dir:
            self._fail(row.task_id, "video_error", "任务缺少输出目录（params_snapshot.output_dir 为空）")
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

        # R4：原子预留输出路径（DB UNIQUE）
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

        # 成品已落到 reserved 路径；把 reserved 提升为正式 output_path
        elapsed = time.time() - started
        # 累加 warnings（含 render_single 里的时长/回退警告）
        for w in result.warnings:
            self.store.add_warning(row.task_id, w)
        if result.hw_fallback_used:
            with self._metrics_lock:
                self.metrics.hw_fallback_count += 1
        self.store.update(row.task_id, status=STATUS_COMPLETED,
                          output_path=result.output_path,
                          final_duration=result.final_duration,
                          concat_duration=result.concat_duration,
                          tts_duration=result.tts_duration,
                          finished_at=time.time(), stage="完成", progress=100,
                          error_type="", error_detail="",
                          encoder_used=result.encoder_used,
                          hw_fallback_used=1 if result.hw_fallback_used else 0)
        vp.cleanup_staging(staging)
        with self._cancel_lock:
            self._cancel_flags.pop(row.task_id, None)
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

    def _fail(self, task_id: str, error_type: str, detail: str) -> None:
        self.store.update(task_id, status=STATUS_FAILED,
                          error_type=error_type, error_detail=detail,
                          finished_at=time.time(), stage="失败")

    def _wait(self, seconds: float) -> None:
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
        with self._batch_pause_lock:
            batches_paused = {bid for bid, evt in self._batch_pause.items() if evt.is_set()}
        return {
            "started_at": self._started_at,
            "paused": self._pause.is_set(),
            "stopped": self._stop.is_set(),
            "batches_paused": sorted(batches_paused),
            "tts_concurrency": self.config.tts_concurrency,
            "video_concurrency": len(self._video_workers),
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
