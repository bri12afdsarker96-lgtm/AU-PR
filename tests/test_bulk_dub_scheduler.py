"""调度器测试。覆盖 20-32、40、以及 R2/R3/R4/R5 改造后的语义。"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub.edge_backend import MockTtsBackend  # noqa: E402
from dub_align_studio.bulk_dub.scheduler import (  # noqa: E402
    SchedulerConfig, TtsBackend, TtsHttpError,
)
from dub_align_studio.bulk_dub.store import (  # noqa: E402
    STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, TaskStore,
)


class _RecordingBackend(TtsBackend):
    def __init__(self, sleep_seconds: float = 0.05,
                 fail_first_n_with_429: int = 0) -> None:
        self.sleep = sleep_seconds
        self.fail_first = fail_first_n_with_429
        self._count = 0
        self._lock = threading.Lock()
        self.max_concurrent = 0
        self._active = 0

    def synthesize(self, *, text, voice_id, speed, pitch, style, output_wav):
        with self._lock:
            self._count += 1
            n = self._count
            self._active += 1
            self.max_concurrent = max(self.max_concurrent, self._active)
        try:
            if n <= self.fail_first:
                raise TtsHttpError(429, "mock 429", retry_after=0.1)
            time.sleep(self.sleep)
            import wave
            output_wav.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(output_wav), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
                w.writeframes(b"\x00\x00" * 4410)
            return 0.1
        finally:
            with self._lock:
                self._active -= 1


def _add_tasks(store, batch_id, n, params_snapshot=None):
    params = params_snapshot or {"output_dir": "/tmp/out",
                                  "encoder_preference": "cpu",
                                  "zoom_percent": 130}
    tasks = []
    for i in range(2, 2 + n):
        tasks.append(dict(
            excel_row=i, input_video="/x.mp4", text=f"文案 {i}",
            fingerprint=f"fp-{batch_id}-{i}",
            voice_id="zh-CN-XiaoshuangNeural",
            voice_name="晓双（女·青春）",
            speed=1.25, keep_original_audio=False, params_snapshot=params,
        ))
    return store.bulk_insert(batch_id, tasks)


def _make_scheduler(tmp_path, backend, *, tts_conc=4, video_conc=2,
                    max_retries=2):
    from dub_align_studio.bulk_dub import scheduler as sched_mod

    store = TaskStore(tmp_path / "q.sqlite3")
    # 替换 _process_video 为 no-op（不真的渲染）
    orig_process_video = sched_mod.Scheduler._process_video

    def _fake_process_video(self, row):
        # 用任务级 params_snapshot 里的 output_dir 决定输出位置——测试关键点
        out_dir = row.params_snapshot.get("output_dir", "/tmp/out")
        self.store.update(row.task_id, status=STATUS_COMPLETED,
                          output_path=f"{out_dir}/{row.excel_row}.mp4",
                          final_duration=1.0, progress=100,
                          encoder_used="libx264")
        with self._metrics_lock:
            self.metrics.record_success(0.1, 0.05)

    sched_mod.Scheduler._process_video = _fake_process_video

    config = SchedulerConfig(
        tts_concurrency=tts_conc, video_concurrency=video_conc,
        tts_max_retries=max_retries,
    )
    sched = sched_mod.Scheduler(store, config, backend)

    def restore():
        sched_mod.Scheduler._process_video = orig_process_video

    return sched, store, restore


def _wait_until(cond, timeout=8.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return False


def test_20_default_voice_and_speed_wired():
    from dub_align_studio.bulk_dub.service import (
        DEFAULT_SPEED, DEFAULT_VOICE_ID,
    )
    assert DEFAULT_VOICE_ID == "zh-CN-XiaoshuangNeural"
    assert DEFAULT_SPEED == 1.25


def test_22_voices_come_from_edge_voices_module():
    from dub_align_studio.bulk_dub.api import EDGE_VOICES as api_voices
    from dub_align_studio.engines.edge_tts import EDGE_VOICES as source_voices
    assert api_voices is source_voices


def test_27_tts_pool_respects_concurrency(tmp_path):
    backend = _RecordingBackend(sleep_seconds=0.15)
    sched, store, restore = _make_scheduler(tmp_path, backend, tts_conc=3, video_conc=2)
    try:
        batch = store.create_batch("t", "/o", {})
        _add_tasks(store, batch, 12)
        sched.start()
        assert _wait_until(
            lambda: store.count_by_status(batch).get(STATUS_COMPLETED, 0) >= 12,
            timeout=20,
        )
        assert backend.max_concurrent <= 3, f"实际峰值 {backend.max_concurrent} 超上限 3"
    finally:
        sched.stop(); restore()


def test_29_10000_tasks_do_not_spawn_10000_threads(tmp_path):
    backend = _RecordingBackend(sleep_seconds=0.001)
    sched, store, restore = _make_scheduler(tmp_path, backend, tts_conc=4, video_conc=2)
    try:
        batch = store.create_batch("t", "/o", {})
        _add_tasks(store, batch, 500)
        sched.start()
        assert len(sched._tts_workers) == 4
        assert len(sched._video_workers) == 2
        time.sleep(0.5)
    finally:
        sched.stop(); restore()


def test_30_pause_stops_new_task_pickup(tmp_path):
    backend = _RecordingBackend(sleep_seconds=0.02)
    sched, store, restore = _make_scheduler(tmp_path, backend, tts_conc=2, video_conc=2)
    try:
        batch = store.create_batch("t", "/o", {})
        _add_tasks(store, batch, 5)
        sched.start()
        sched.pause()
        time.sleep(0.4)
        done_paused = store.count_by_status(batch).get(STATUS_COMPLETED, 0)
        time.sleep(0.5)
        done_after = store.count_by_status(batch).get(STATUS_COMPLETED, 0)
        assert done_after - done_paused <= 2, "暂停后仍在领新任务"
    finally:
        sched.stop(); restore()


def test_31_resume_continues_processing(tmp_path):
    backend = _RecordingBackend(sleep_seconds=0.02)
    sched, store, restore = _make_scheduler(tmp_path, backend, tts_conc=2, video_conc=2)
    try:
        batch = store.create_batch("t", "/o", {})
        _add_tasks(store, batch, 6)
        sched.start()
        sched.pause()
        time.sleep(0.2)
        sched.resume()
        assert _wait_until(
            lambda: store.count_by_status(batch).get(STATUS_COMPLETED, 0) >= 6, 5
        )
    finally:
        sched.stop(); restore()


def test_32_cancel_task_transitions_to_cancelled(tmp_path):
    backend = _RecordingBackend(sleep_seconds=0.5)
    sched, store, restore = _make_scheduler(tmp_path, backend, tts_conc=1, video_conc=1)
    try:
        batch = store.create_batch("t", "/o", {})
        _add_tasks(store, batch, 3)
        rows = store.list_tasks(batch_id=batch, limit=5)
        sched.start()
        tid = rows[-1].task_id
        assert sched.cancel_task(tid)
        row = store.get(tid)
        assert row.status == STATUS_CANCELLED
    finally:
        sched.stop(); restore()


def test_38_39_tts_429_retries_then_succeeds(tmp_path):
    backend = _RecordingBackend(sleep_seconds=0.01, fail_first_n_with_429=2)
    sched, store, restore = _make_scheduler(tmp_path, backend, tts_conc=1, video_conc=1,
                                              max_retries=5)
    try:
        batch = store.create_batch("t", "/o", {})
        _add_tasks(store, batch, 1)
        sched.start()
        assert _wait_until(
            lambda: store.count_by_status(batch).get(STATUS_COMPLETED, 0) == 1, 15
        )
        assert sched.metrics.retry_count >= 2
        assert sched.metrics.http_429_count >= 2
    finally:
        sched.stop(); restore()


def test_40_client_4xx_not_retried(tmp_path):
    class _400(TtsBackend):
        def synthesize(self, **_):
            raise TtsHttpError(400, "配置错误")
    sched, store, restore = _make_scheduler(tmp_path, _400(), tts_conc=1, video_conc=1,
                                              max_retries=5)
    try:
        batch = store.create_batch("t", "/o", {})
        _add_tasks(store, batch, 1)
        sched.start()
        assert _wait_until(
            lambda: store.count_by_status(batch).get(STATUS_FAILED, 0) == 1, 5
        )
        row = store.list_tasks(batch_id=batch, limit=1)[0]
        assert row.attempts == 1
        assert row.error_type == "client_4xx"
    finally:
        sched.stop(); restore()


def test_26_batch_freezes_params(tmp_path):
    """26: 冻结参数——用 service.start_batch 语义。"""
    from dub_align_studio.bulk_dub.service import BulkDubService
    from dub_align_studio.bulk_dub.excel_reader import build_minimal_xlsx

    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(base_seconds=0.05),
    )
    xlsx = build_minimal_xlsx([("/x.mp4", "abc")])
    (tmp_path / "out1").mkdir()
    (tmp_path / "out2").mkdir()
    r1 = svc.start_batch(source_bytes=xlsx, label="b1",
                          output_dir=str(tmp_path / "out1"),
                          voice_id="zh-CN-XiaoshuangNeural", speed=1.25,
                          check_exists=False)
    r2 = svc.start_batch(source_bytes=xlsx, label="b2",
                          output_dir=str(tmp_path / "out2"),
                          voice_id="zh-CN-YunxiNeural", speed=1.0,
                          check_exists=False)
    tasks_b1 = svc.store.list_tasks(batch_id=r1["batch_id"])
    tasks_b2 = svc.store.list_tasks(batch_id=r2["batch_id"])
    # batch 1 参数不能被 batch 2 覆盖
    assert all(t.params_snapshot["speed"] == 1.25 for t in tasks_b1)
    assert all(t.params_snapshot["output_dir"].endswith("out1") for t in tasks_b1)
    assert all(t.params_snapshot["voice_id"] == "zh-CN-XiaoshuangNeural" for t in tasks_b1)
    assert all(t.params_snapshot["speed"] == 1.0 for t in tasks_b2)
    assert all(t.params_snapshot["output_dir"].endswith("out2") for t in tasks_b2)
    svc.stop()
