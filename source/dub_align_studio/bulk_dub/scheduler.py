"""批处理调度器：SQLite 持久队列 → TTS 池 → 视频池 → 校验 → 原子提交。"""

from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path
from typing import Optional

from .circuit_breaker import CircuitBreaker, classify_http_error, compute_backoff
from . import ffmpeg_pipeline as vp
from .ffmpeg_pipeline import VideoCancelled, VideoError
from .hw_encoder import EncoderProbe, default_video_concurrency, resolve_encoder
from .store import (
    STATUS_COMPLETED, STATUS_FAILED, STATUS_PENDING, STATUS_RETRY_WAIT,
    STATUS_TTS_DONE, STATUS_TTS_RUNNING, STATUS_VIDEO_RUNNING,
    STATUS_CANCELLED, TaskRow, TaskStore,
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


class SchedulerConfig:
    def __init__(self, *, tts_concurrency: int = 4, video_concurrency: int = 0,
                 tts_max_retries: int = 5, encoder_preference: str = "auto",
                 zoom_percent: int = 130, keep_original_audio: bool = False,
                 voice_short: str = "配音", output_dir: str = "",
                 ffmpeg_timeout: float = 1800.0,
                 breaker_failure_threshold: int = 5,
                 breaker_open_seconds: float = 30.0) -> None:
        self.tts_concurrency = tts_concurrency
        self.video_concurrency = video_concurrency
        self.tts_max_retries = tts_max_retries
        self.encoder_preference = encoder_preference
        self.zoom_percent = zoom_percent
        self.keep_original_audio = keep_original_audio
        self.voice_short = voice_short
        self.output_dir = output_dir
        self.ffmpeg_timeout = ffmpeg_timeout
        self.breaker_failure_threshold = breaker_failure_threshold
        self.breaker_open_seconds = breaker_open_seconds


class SchedulerMetrics:
    def __init__(self) -> None:
        self.finished_times: list[float] = []
        self.tts_times: list[float] = []
        self.video_times: list[float] = []
        self.http_429_count = 0
        self.http_5xx_count = 0
        self.retry_count = 0

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
        self._encoder: EncoderProbe | None = None
        self._cancel_flags: dict[str, threading.Event] = {}
        self._cancel_lock = threading.Lock()
        self._pause = threading.Event()
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
        self._retry_bookkeeping: dict[str, tuple[float, int]] = {}
        self._started_at: float = 0.0

    def start(self) -> None:
        if self._tts_workers or self._video_workers:
            return
        self.store.reap_interrupted()
        self._started_at = time.time()
        self._encoder = self._resolve_encoder_safe()
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

    def _resolve_encoder_safe(self) -> EncoderProbe:
        try:
            return resolve_encoder(
                self._ffmpeg_path or _default_ffmpeg(),
                preference=self.config.encoder_preference,
            )
        except Exception:  # noqa: BLE001
            return EncoderProbe("cpu", "libx264", [], True, "回退：libx264（探测异常）")

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()
        with self._cond:
            self._cond.notify_all()

    def is_paused(self) -> bool:
        return self._pause.is_set()

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
            if only_retryable and row.error_type == "client_4xx":
                continue
            self.store.reset_status(row.task_id, STATUS_PENDING)
            n += 1
        self.notify()
        return n

    def _tts_loop(self) -> None:
        while not self._stop.is_set():
            if self._pause.is_set():
                self._wait(0.5)
                continue
            allow, wait = self._breaker.acquire()
            if not allow:
                self._wait(min(wait, 1.5))
                continue
            row = self.store.claim_next(
                (STATUS_PENDING, STATUS_RETRY_WAIT), STATUS_TTS_RUNNING,
            )
            if row is None:
                self._wait(0.3)
                continue
            wait_until = self._retry_bookkeeping.get(row.task_id)
            if wait_until and wait_until[0] > time.time():
                self.store.update(row.task_id, status=STATUS_RETRY_WAIT)
                self._wait(min(0.5, wait_until[0] - time.time()))
                continue
            self._process_tts(row)

    def _process_tts(self, row: TaskRow) -> None:
        started = time.time()
        cancel_flag = self._ensure_cancel_flag(row.task_id)
        if cancel_flag.is_set():
            self.store.update(row.task_id, status=STATUS_CANCELLED)
            return

        staging = _pick_staging_dir(row)
        staging.mkdir(parents=True, exist_ok=True)
        self.store.update(row.task_id, staging_dir=str(staging),
                          started_at=started, stage="TTS 合成")
        self.store.bump_attempts(row.task_id)

        tts_wav = staging / "tts.wav"
        attempt = row.attempts + 1
        try:
            duration = self.tts_backend.synthesize(
                text=row.text, voice_id=row.voice_id, speed=row.speed,
                pitch=int(row.params_snapshot.get("pitch", 0)),
                style=str(row.params_snapshot.get("style", "general")),
                output_wav=tts_wav,
            )
        except TtsHttpError as exc:
            self._handle_tts_http_error(row, exc, attempt)
            return
        except Exception as exc:  # noqa: BLE001
            self._handle_tts_generic_error(row, exc, attempt)
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
            return
        self._breaker.record_failure(retry_after_seconds=exc.retry_after)
        if attempt >= self.config.tts_max_retries:
            self._fail(row.task_id, kind, f"HTTP {exc.status_code}：{exc}（已达最大重试）")
            return
        wait = exc.retry_after if exc.retry_after and exc.retry_after > 0 \
            else compute_backoff(attempt)
        self._retry_bookkeeping[row.task_id] = (time.time() + wait, attempt)
        self.store.update(row.task_id, status=STATUS_RETRY_WAIT,
                          stage=f"TTS 等待重试 ({int(wait)}s)",
                          error_type=kind, error_detail=str(exc))
        with self._metrics_lock:
            self.metrics.retry_count += 1

    def _handle_tts_generic_error(self, row: TaskRow, exc: Exception,
                                   attempt: int) -> None:
        if attempt >= self.config.tts_max_retries:
            self._fail(row.task_id, "tts_error", str(exc)[:400])
            return
        wait = compute_backoff(attempt)
        self._retry_bookkeeping[row.task_id] = (time.time() + wait, attempt)
        self.store.update(row.task_id, status=STATUS_RETRY_WAIT,
                          stage=f"TTS 等待重试 ({int(wait)}s)",
                          error_type="tts_error", error_detail=str(exc)[:400])
        with self._metrics_lock:
            self.metrics.retry_count += 1

    def _video_loop(self) -> None:
        while not self._stop.is_set():
            if self._pause.is_set():
                self._wait(0.5)
                continue
            row = self.store.claim_next((STATUS_TTS_DONE,), STATUS_VIDEO_RUNNING)
            if row is None:
                self._wait(0.3)
                continue
            self._process_video(row)

    def _process_video(self, row: TaskRow) -> None:
        started = time.time()
        cancel_flag = self._ensure_cancel_flag(row.task_id)
        if cancel_flag.is_set():
            self.store.update(row.task_id, status=STATUS_CANCELLED)
            vp.cleanup_staging(row.staging_dir)
            return
        staging = Path(row.staging_dir)
        tts_wav = staging / "tts.wav"
        output_dir = Path(self.config.output_dir or
                           row.params_snapshot.get("output_dir") or ".")
        output_dir.mkdir(parents=True, exist_ok=True)
        voice_short = vp.voice_short_name(row.voice_name)
        default_name = vp.build_output_filename(row.input_video, voice_short)
        output_path = output_dir / default_name

        self.store.update(row.task_id, stage="视频渲染")
        try:
            probe = vp.ffprobe_video(row.input_video)
            self.store.update(row.task_id, video_duration=probe.duration,
                              concat_duration=probe.duration * 2)
            result = vp.render_single(
                input_video=row.input_video,
                tts_audio=tts_wav,
                output_path=output_path,
                staging_dir=staging,
                encoder=self._encoder or EncoderProbe("cpu", "libx264", [], True, ""),
                keep_original_audio=bool(row.keep_original_audio),
                zoom_percent=int(row.params_snapshot.get("zoom_percent", 130)),
                video_probe=probe,
                tts_seconds=row.tts_duration,
                cancel_flag=cancel_flag,
                timeout=self.config.ffmpeg_timeout,
            )
        except VideoCancelled:
            self.store.update(row.task_id, status=STATUS_CANCELLED,
                              error_type="cancelled",
                              error_detail="视频渲染中被取消")
            vp.cleanup_staging(staging)
            return
        except VideoError as exc:
            self._fail(row.task_id, "video_error", str(exc)[:400])
            vp.cleanup_staging(staging)
            return
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exception_only(exc)
            self._fail(row.task_id, "video_error",
                       f"未预期错误：{''.join(tb).strip()[:400]}")
            vp.cleanup_staging(staging)
            return
        elapsed = time.time() - started
        self.store.update(row.task_id, status=STATUS_COMPLETED,
                          output_path=result.output_path,
                          final_duration=result.final_duration,
                          concat_duration=result.concat_duration,
                          tts_duration=result.tts_duration,
                          finished_at=time.time(), stage="完成", progress=100,
                          error_type="", error_detail="")
        vp.cleanup_staging(staging)
        with self._metrics_lock:
            self.metrics.record_success(row.tts_duration, elapsed)

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
                "avg_tts_seconds": round(avg_tts, 2),
                "avg_video_seconds": round(avg_video, 2),
                "recent_rate_1min": round(recent1, 2),
                "recent_rate_5min": round(recent5, 2),
                "recent_rate_60min": round(recent60, 2),
                "projected_24h": round(recent60 * 60 * 24, 0) if recent60 else 0,
            }
        return {
            "started_at": self._started_at,
            "paused": self._pause.is_set(),
            "stopped": self._stop.is_set(),
            "encoder": {
                "family": (self._encoder.family if self._encoder else "cpu"),
                "name": (self._encoder.encoder if self._encoder else "libx264"),
                "ok": (self._encoder.ok if self._encoder else True),
                "detail": (self._encoder.detail if self._encoder else ""),
            },
            "tts_concurrency": self.config.tts_concurrency,
            "video_concurrency": len(self._video_workers),
            "breaker": self._breaker.snapshot(),
            "metrics": m,
        }


def _default_ffmpeg() -> str:
    from .. import settings as studio_settings

    return studio_settings.ffmpeg_tool("ffmpeg")


def _pick_staging_dir(row: TaskRow) -> Path:
    from .. import settings as studio_settings

    root = studio_settings.data_root() / "批量带货" / "staging"
    return root / row.batch_id / row.task_id
