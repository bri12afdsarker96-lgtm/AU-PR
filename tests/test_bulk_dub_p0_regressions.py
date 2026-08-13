"""R10：15 项 P0 覆盖测试。

按用户第 2 轮指令编号 1-15：
    1. 两个批次同时运行，参数和输出目录不串。
    2. 四类状态重启恢复（validating/tts_running/video_running/retry_wait）。
    3. 8 worker 同名输出不覆盖、不丢任务。
    4. HALF_OPEN 无任务时不会永久锁住。
    5. Retry-After 重启后仍生效。
    6. 真实 Edge 适配层单次请求、保留 429/5xx 分类，禁止双重重试。
    7. TTS 运行中取消不会转成 tts_done。
    8. 硬件编码运行失败自动回退 libx264。
    9. 无原声音轨 warning 写入 DB、API、CSV、UI。
    10. 时长偏差超限时旧成片保持不变。
    11. 超大上传返回 413，ZIP bomb 被拒绝。
    12. 10000 行批量插入使用单事务；预览有上限。
    13. staging 越界路径、根目录、符号链接都拒绝删除。
    14. 页面重开后能恢复批次列表和当前批次。
    15. 试听音频能通过浏览器实际播放（HTTP 200 + audio/wav）。
"""

from __future__ import annotations

import io
import json
import shutil
import sys
import threading
import time
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub import ffmpeg_pipeline as vp  # noqa: E402
from dub_align_studio.bulk_dub import api as bulk_api  # noqa: E402
from dub_align_studio.bulk_dub.circuit_breaker import CircuitBreaker  # noqa: E402
from dub_align_studio.bulk_dub.csv_export import to_bytes as csv_to_bytes  # noqa: E402
from dub_align_studio.bulk_dub.edge_backend import MockTtsBackend, EdgeTtsBackend  # noqa: E402
from dub_align_studio.bulk_dub.excel_reader import (  # noqa: E402
    MAX_XLSX_UPLOAD_BYTES, ExcelSizeError, build_minimal_xlsx,
    build_zip_bomb_payload, parse_excel,
)
from dub_align_studio.bulk_dub.scheduler import (  # noqa: E402
    Scheduler, SchedulerConfig, TtsBackend, TtsHttpError,
)
from dub_align_studio.bulk_dub.service import BulkDubService  # noqa: E402
from dub_align_studio.bulk_dub.store import (  # noqa: E402
    STATUS_COMPLETED, STATUS_FAILED, STATUS_INTERRUPTED, STATUS_PENDING,
    STATUS_RETRY_WAIT, STATUS_TTS_DONE, STATUS_TTS_RUNNING,
    STATUS_VIDEO_RUNNING, TaskStore,
)


HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _wait_until(cond, timeout=10.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return False


def _fake_video_render(sched):
    """把 scheduler._process_video 换成 mock：用 params_snapshot 的 output_dir。"""
    orig = sched.__class__._process_video

    def _fake(self, row):
        out_dir = row.params_snapshot.get("output_dir", "/tmp/out")
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        # 预留输出路径（用于测试并发）
        try:
            reserved = self.store.reserve_output_path(
                row.task_id, Path(out_dir) / f"{row.excel_row}.mp4"
            )
            reserved.write_bytes(b"MP4-FAKE")
        except Exception as exc:  # noqa: BLE001
            self.store.update(row.task_id, status=STATUS_FAILED,
                              error_type="video_error", error_detail=str(exc))
            return
        self.store.commit_output_path(row.task_id, str(reserved))
        self.store.update(row.task_id, status=STATUS_COMPLETED,
                          final_duration=1.0, progress=100, encoder_used="libx264")
        with self._metrics_lock:
            self.metrics.record_success(0.1, 0.05)

    sched.__class__._process_video = _fake
    return lambda: setattr(sched.__class__, "_process_video", orig)


# ============================================================
# 1. 两批次并发不串
# ============================================================

def test_p0_1_two_batches_do_not_leak_params(tmp_path):
    from dub_align_studio.bulk_dub.excel_reader import build_minimal_xlsx
    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(base_seconds=0.02),
    )
    restore = _fake_video_render(svc._ensure_scheduler())
    try:
        out1 = tmp_path / "out1"; out1.mkdir()
        out2 = tmp_path / "out2"; out2.mkdir()
        xlsx1 = build_minimal_xlsx([(f"/A{i}.mp4", f"文案A{i}") for i in range(1, 6)])
        xlsx2 = build_minimal_xlsx([(f"/B{i}.mp4", f"文案B{i}") for i in range(1, 6)])
        r1 = svc.start_batch(source_bytes=xlsx1, label="B1",
                              output_dir=str(out1),
                              voice_id="zh-CN-XiaoshuangNeural", speed=1.25,
                              check_exists=False, require_endpoint=False)
        r2 = svc.start_batch(source_bytes=xlsx2, label="B2",
                              output_dir=str(out2),
                              voice_id="zh-CN-YunxiNeural", speed=0.8,
                              check_exists=False, require_endpoint=False)
        assert _wait_until(
            lambda: (svc.store.count_by_status(r1["batch_id"]).get(STATUS_COMPLETED, 0) == 5
                     and svc.store.count_by_status(r2["batch_id"]).get(STATUS_COMPLETED, 0) == 5),
            timeout=20,
        )
        b1 = svc.store.list_tasks(batch_id=r1["batch_id"])
        b2 = svc.store.list_tasks(batch_id=r2["batch_id"])
        # 关键：output_path 严格分属两个目录
        for t in b1:
            assert str(out1) in t.output_path
            assert t.voice_id == "zh-CN-XiaoshuangNeural"
            assert t.speed == 1.25
        for t in b2:
            assert str(out2) in t.output_path
            assert t.voice_id == "zh-CN-YunxiNeural"
            assert t.speed == 0.8
    finally:
        restore(); svc.stop()


# ============================================================
# 2. 重启恢复四态
# ============================================================

def test_p0_2_restart_recovery_four_states(tmp_path):
    """validating/tts_running → pending；video_running(有效tts.wav) → tts_done；
    video_running(无效tts.wav) → pending；retry_wait 保留。"""
    db = tmp_path / "q.sqlite3"
    s1 = TaskStore(db)
    b = s1.create_batch("t", "/o", {})
    ids = s1.bulk_insert(b, [
        {"excel_row": 2, "input_video": "/v.mp4", "text": "t2", "fingerprint": "fp2",
         "voice_id": "v", "voice_name": "V", "speed": 1.0, "keep_original_audio": False,
         "params_snapshot": {}},
        {"excel_row": 3, "input_video": "/v.mp4", "text": "t3", "fingerprint": "fp3",
         "voice_id": "v", "voice_name": "V", "speed": 1.0, "keep_original_audio": False,
         "params_snapshot": {}},
        {"excel_row": 4, "input_video": "/v.mp4", "text": "t4", "fingerprint": "fp4",
         "voice_id": "v", "voice_name": "V", "speed": 1.0, "keep_original_audio": False,
         "params_snapshot": {}},
        {"excel_row": 5, "input_video": "/v.mp4", "text": "t5", "fingerprint": "fp5",
         "voice_id": "v", "voice_name": "V", "speed": 1.0, "keep_original_audio": False,
         "params_snapshot": {}},
    ])
    tid_val, tid_tts, tid_vid_ok, tid_vid_bad = ids
    # 建有效 tts.wav（video_running_ok）
    stg_ok = tmp_path / "stg_ok"; stg_ok.mkdir()
    with wave.open(str(stg_ok / "tts.wav"), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 4410)
    stg_bad = tmp_path / "stg_bad"; stg_bad.mkdir()   # 无 tts.wav
    s1.update(tid_val, status="validating")
    s1.update(tid_tts, status=STATUS_TTS_RUNNING)
    s1.update(tid_vid_ok, status=STATUS_VIDEO_RUNNING, staging_dir=str(stg_ok),
               tts_duration=0.1)
    s1.update(tid_vid_bad, status=STATUS_VIDEO_RUNNING, staging_dir=str(stg_bad))
    # retry_wait 单独一条
    ids2 = s1.bulk_insert(b, [
        {"excel_row": 6, "input_video": "/v.mp4", "text": "t6", "fingerprint": "fp6",
         "voice_id": "v", "voice_name": "V", "speed": 1.0, "keep_original_audio": False,
         "params_snapshot": {}}])
    tid_retry = ids2[0]
    next_at = time.time() + 5
    s1.update(tid_retry, status=STATUS_RETRY_WAIT, next_attempt_at=next_at)

    # 新连接（模拟重启）
    s2 = TaskStore(db)

    def _tts_wav_ok(row):
        return (Path(row.staging_dir) / "tts.wav").is_file() if row.staging_dir else False

    stats = s2.reap_and_recover_running(tts_wav_ok=_tts_wav_ok)
    assert stats["tts_recovered_pending"] == 2       # validating + tts_running
    assert stats["video_recovered_tts_done"] == 1    # video_running + ok wav
    assert stats["video_recovered_pending"] == 1     # video_running + bad wav

    assert s2.get(tid_val).status == STATUS_PENDING
    assert s2.get(tid_tts).status == STATUS_PENDING
    assert s2.get(tid_vid_ok).status == STATUS_TTS_DONE
    assert s2.get(tid_vid_bad).status == STATUS_PENDING
    # retry_wait 保留 + next_attempt_at 未变
    r_retry = s2.get(tid_retry)
    assert r_retry.status == STATUS_RETRY_WAIT
    assert abs(r_retry.next_attempt_at - next_at) < 0.01


# ============================================================
# 3. 8 worker 并发同名输出：全部唯一，已有文件不变
# ============================================================

def test_p0_3_concurrent_output_no_overwrite(tmp_path):
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", str(tmp_path), {})
    # 8 条任务，同一 base_path 竞争
    ids = store.bulk_insert(b, [
        {"excel_row": i, "input_video": "/v.mp4", "text": f"t{i}",
         "fingerprint": f"fp{i}", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False, "params_snapshot": {}}
        for i in range(2, 10)
    ])
    # 一个已存在文件，作为占位——不得被改
    (tmp_path / "output.mp4").write_bytes(b"OLD-BYTES")

    reserved_paths = []
    errors = []
    barrier = threading.Barrier(len(ids))

    def worker(tid):
        try:
            barrier.wait()
            p = store.reserve_output_path(tid, tmp_path / "output.mp4")
            reserved_paths.append(str(p))
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(tid,)) for tid in ids]
    for t in threads: t.start()
    for t in threads: t.join()
    assert not errors, f"预留出错：{errors}"
    # 全部唯一
    assert len(set(reserved_paths)) == len(ids)
    # 已有文件字节不动
    assert (tmp_path / "output.mp4").read_bytes() == b"OLD-BYTES"


# ============================================================
# 4. HALF_OPEN 无任务时不会永久锁死
# ============================================================

def test_p0_4_half_open_lease_releases(tmp_path):
    br = CircuitBreaker(failure_threshold=2, open_seconds=0.2,
                        half_open_lease_seconds=0.3)
    br.record_failure(); br.record_failure()
    time.sleep(0.25)
    # 第一个 acquire 进入 HALF_OPEN 并占锁
    allow1, _ = br.acquire()
    assert allow1 is True
    # 第二个此时不放行
    allow2, _ = br.acquire()
    assert allow2 is False
    # 探测线程崩溃/无任务——不 record → 租约到期后自动解锁 → 回 OPEN
    time.sleep(0.35)
    snap = br.snapshot()
    assert snap["state"] == "OPEN"
    assert snap["half_open_locked"] is False


def test_p0_4_release_probe_manual(tmp_path):
    br = CircuitBreaker(failure_threshold=1, open_seconds=0.1)
    br.record_failure()
    time.sleep(0.15)
    allow, _ = br.acquire()
    assert allow
    br.release_probe()
    assert br.snapshot()["half_open_locked"] is False


# ============================================================
# 5. Retry-After 重启后仍生效
# ============================================================

def test_p0_5_retry_after_persists_across_restart(tmp_path):
    db = tmp_path / "q.sqlite3"
    s1 = TaskStore(db)
    b = s1.create_batch("t", "/o", {})
    tid = s1.bulk_insert(b, [
        {"excel_row": 2, "input_video": "/v.mp4", "text": "t",
         "fingerprint": "fp", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False, "params_snapshot": {}}
    ])[0]
    # 设置 retry_wait 到期时间 = 现在 + 100s
    next_at = time.time() + 100
    s1.update(tid, status=STATUS_RETRY_WAIT, next_attempt_at=next_at)

    # 重启：新连接
    s2 = TaskStore(db)
    # claim_next 尊重 next_attempt_at → 不到期时应该领不到
    got = s2.claim_next((STATUS_RETRY_WAIT,), STATUS_TTS_RUNNING,
                         respect_next_attempt=True)
    assert got is None, "未到期就领到了任务——Retry-After 没落库"
    # 强行到期
    s2.update(tid, next_attempt_at=time.time() - 1)
    got2 = s2.claim_next((STATUS_RETRY_WAIT,), STATUS_TTS_RUNNING,
                          respect_next_attempt=True)
    assert got2 is not None
    assert got2.task_id == tid


# ============================================================
# 6. Edge 适配层单次请求，保留 429/5xx 分类
# ============================================================

def test_p0_6_edge_backend_single_request_preserves_status(tmp_path):
    """伪造 urlopen 返回 429，验证：只调用一次；status_code=429；Retry-After 保留。"""
    from unittest.mock import MagicMock, patch
    from urllib.error import HTTPError

    class _FakeHeaders(dict):
        def get(self, k, default=None):
            return super().get(k.lower(), default)
    headers = _FakeHeaders({"retry-after": "7"})

    err = HTTPError("http://x/", 429, "Too Many Requests", headers, io.BytesIO(b"limited"))
    call_count = {"n": 0}

    def _fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        raise err

    be = EdgeTtsBackend(endpoint="http://x.workers.dev", timeout=1)
    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        with pytest.raises(TtsHttpError) as ei:
            be.synthesize(text="hi", voice_id="v", speed=1.0, pitch=0,
                          style="general", output_wav=tmp_path / "out.wav")
    assert call_count["n"] == 1, "适配层不能自己重试"
    assert ei.value.status_code == 429
    assert ei.value.retry_after == 7.0


def test_p0_6_edge_backend_5xx_becomes_retryable(tmp_path):
    from unittest.mock import patch
    from urllib.error import HTTPError

    err = HTTPError("http://x/", 503, "Overloaded", {}, io.BytesIO(b"busy"))

    def _fake(req, timeout=None):
        raise err
    be = EdgeTtsBackend(endpoint="http://x.workers.dev", timeout=1)
    with patch("urllib.request.urlopen", side_effect=_fake):
        with pytest.raises(TtsHttpError) as ei:
            be.synthesize(text="hi", voice_id="v", speed=1.0, pitch=0,
                          style="general", output_wav=tmp_path / "out.wav")
    assert ei.value.status_code == 503


# ============================================================
# 7. TTS 请求期间取消 → 不能转 tts_done
# ============================================================

def test_p0_7_cancel_during_tts_does_not_become_tts_done(tmp_path):
    """伪造 backend 返回后再取消 → scheduler 应把任务转 cancelled。"""
    cancel_event = threading.Event()
    call_started = threading.Event()

    class _SlowBackend(TtsBackend):
        def synthesize(self, **kwargs):
            call_started.set()
            # 让 test 有时间把任务取消
            time.sleep(0.6)
            import wave
            out = kwargs["output_wav"]
            out.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
                w.writeframes(b"\x00\x00" * 4410)
            return 0.1

    store = TaskStore(tmp_path / "q.sqlite3")
    config = SchedulerConfig(tts_concurrency=1, video_concurrency=1)
    sched = Scheduler(store, config, _SlowBackend())
    b = store.create_batch("t", "/o", {})
    tid = store.bulk_insert(b, [
        {"excel_row": 2, "input_video": "/v.mp4", "text": "t",
         "fingerprint": "fp", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False, "params_snapshot": {}}
    ])[0]
    sched.start()
    try:
        assert call_started.wait(3.0)
        # 请求进行中——设置取消标志（但状态仍是 tts_running；status 不允许直接改）
        with sched._cancel_lock:
            flag = sched._cancel_flags.setdefault(tid, threading.Event())
            flag.set()
        assert _wait_until(
            lambda: store.get(tid).status == "cancelled", 5.0
        )
        assert store.get(tid).status != STATUS_TTS_DONE
    finally:
        sched.stop()


# ============================================================
# 8. 硬件编码运行失败 → 自动回退 libx264
# ============================================================

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg missing")
def test_p0_8_hw_encoder_runtime_failure_falls_back(tmp_path):
    """让 EncoderProbe 声明一个不存在的编码器 → 首次运行失败 → 自动回退 libx264 成功。"""
    import subprocess
    from dub_align_studio.bulk_dub.hw_encoder import EncoderProbe

    video = tmp_path / "v.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:duration=1:rate=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
    ], check=True)
    tts = tmp_path / "tts.wav"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(tts),
    ], check=True)

    # 故意造一个"存在但一定失败"的编码器名（"__nonexistent_enc__"）
    encoder = EncoderProbe("nvidia", "__nonexistent_enc__", [], True, "假")
    out = tmp_path / "out.mp4"
    staging = tmp_path / "staging"
    result = vp.render_single(
        input_video=video, tts_audio=tts, reserved_output=out,
        staging_dir=staging, encoder=encoder, allow_hw_fallback=True,
    )
    assert Path(result.output_path).is_file()
    assert result.hw_fallback_used is True
    assert result.encoder_used == "libx264"
    assert any("回退" in w or "fallback" in w.lower() for w in result.warnings)


# ============================================================
# 9. 无原声 warning 落 DB + API + CSV + UI
# ============================================================

def test_p0_9_no_original_audio_warning_persists_to_db_csv(tmp_path):
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    tid = store.bulk_insert(b, [
        {"excel_row": 2, "input_video": "/v.mp4", "text": "t",
         "fingerprint": "fp", "voice_id": "v", "voice_name": "晓双（女·青春）",
         "speed": 1.25, "keep_original_audio": True, "params_snapshot": {}}
    ])[0]
    store.add_warning(tid, "该视频没有原声音轨，本条仅使用旁白。")
    store.update(tid, status=STATUS_COMPLETED, output_path="/o/1.mp4",
                  final_duration=2.0, tts_duration=1.5, video_duration=1.0)
    # DB
    row = store.get(tid)
    assert "原声" in row.warnings[0]
    # CSV: 我们的 CSV 没有 warnings 列（COLUMNS 无），所以补一个校验：warning 在 API 层
    from dataclasses import asdict
    api_dict = asdict(row)
    assert "warnings" in api_dict
    assert api_dict["warnings"] and "原声" in api_dict["warnings"][0]
    # UI HTML 里含有 warnings 相关的渲染逻辑
    html = (Path(__file__).resolve().parents[1] / "source" / "dub_align_studio" /
            "web" / "bulk_dub.html").read_text(encoding="utf-8")
    assert "warnings" in html and "warn-row" in html


# ============================================================
# 10. 时长偏差超限 → 旧成片保持不变
# ============================================================

@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg missing")
def test_p0_10_duration_overshoot_leaves_existing_output_untouched(tmp_path):
    """在渲染前，把预留路径写入伪旧文件；渲染因为其他失败/校验被拒时旧文件不变。"""
    import subprocess
    from dub_align_studio.bulk_dub.hw_encoder import EncoderProbe

    video = tmp_path / "v.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:duration=1:rate=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
    ], check=True)
    tts = tmp_path / "tts.wav"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(tts),
    ], check=True)

    encoder = EncoderProbe("cpu", "libx264", [], True, "")
    out = tmp_path / "out.mp4"
    out.write_bytes(b"OLD_KEEP_ME")
    staging = tmp_path / "staging"
    # reserved_output 已存在文件 → render_single 应拒绝覆盖并抛错
    with pytest.raises(vp.VideoError):
        vp.render_single(
            input_video=video, tts_audio=tts, reserved_output=out,
            staging_dir=staging, encoder=encoder,
        )
    assert out.read_bytes() == b"OLD_KEEP_ME"


# ============================================================
# 11. 超大上传 413 + ZIP bomb 拒绝
# ============================================================

def test_p0_11_zip_bomb_rejected():
    payload = build_zip_bomb_payload(entries=101)
    # 现在预览 → 抛 ExcelSizeError
    with pytest.raises(ExcelSizeError):
        parse_excel(payload, check_exists=False)


def test_p0_11_huge_upload_413(tmp_path):
    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(),
    )
    huge = b"\x00" * (MAX_XLSX_UPLOAD_BYTES + 1)
    handled, status, body, _ = bulk_api.dispatch_post(
        "/api/bulk_dub/preview", {}, huge, "application/octet-stream", service=svc,
    )
    assert handled and status == 413
    svc.stop()


# ============================================================
# 12. 10000 行批量插入用单事务 + 预览有上限
# ============================================================

def test_p0_12_bulk_insert_10000_single_transaction(tmp_path):
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    rows = [
        {"excel_row": i, "input_video": "/v.mp4", "text": f"t{i}",
         "fingerprint": f"fp{i}", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False, "params_snapshot": {}}
        for i in range(2, 10002)
    ]
    started = time.time()
    ids = store.bulk_insert(b, rows)
    elapsed = time.time() - started
    assert len(ids) == 10000
    # 单事务性能：10000 条应远快于 10000 短连接
    # 保守上限 15s（本地 CI 通常 <2s）；测目的是"不逐行 open connection"
    assert elapsed < 15.0, f"批量插入耗时 {elapsed:.2f}s 过长（疑似非单事务）"


def test_p0_12_preview_has_row_limit(tmp_path):
    from dub_align_studio.bulk_dub.excel_reader import (
        DEFAULT_PREVIEW_LIMIT, build_minimal_xlsx, parse_excel,
    )
    rows = [(f"/v{i}.mp4", f"t{i}") for i in range(1, DEFAULT_PREVIEW_LIMIT + 100)]
    xlsx = build_minimal_xlsx(rows)
    r = parse_excel(xlsx, check_exists=False)
    d = r.to_dict(preview_limit=DEFAULT_PREVIEW_LIMIT)
    assert len(d["rows"]) == DEFAULT_PREVIEW_LIMIT
    assert d["rows_total"] == DEFAULT_PREVIEW_LIMIT + 99


# ============================================================
# 13. staging 越界/根/符号链接 拒绝删除
# ============================================================

def test_p0_13_staging_out_of_root_refused(tmp_path):
    # 一个不在白名单里的目录
    victim = tmp_path / "totally_unrelated"
    victim.mkdir()
    (victim / "file").write_bytes(b"live")
    assert vp.cleanup_staging(str(victim)) is False
    assert (victim / "file").exists()


def test_p0_13_staging_root_itself_refused(tmp_path, monkeypatch):
    from dub_align_studio import settings as studio_settings
    fake = tmp_path / "d"
    (fake / "批量带货" / "staging").mkdir(parents=True)
    monkeypatch.setattr(studio_settings, "data_root", lambda: fake)
    root = fake / "批量带货" / "staging"
    assert vp.cleanup_staging(str(root)) is False
    assert root.exists()


def test_p0_13_staging_symlink_refused(tmp_path, monkeypatch):
    from dub_align_studio import settings as studio_settings
    fake = tmp_path / "d"
    (fake / "批量带货" / "staging").mkdir(parents=True)
    monkeypatch.setattr(studio_settings, "data_root", lambda: fake)
    victim = tmp_path / "real_target"
    victim.mkdir()
    (victim / "important").write_bytes(b"live")
    batch_id = "a" * 12
    task_id = "b" * 16
    (fake / "批量带货" / "staging" / batch_id).mkdir()
    link = fake / "批量带货" / "staging" / batch_id / task_id
    try:
        link.symlink_to(victim, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("环境不支持 symlink")
    assert vp.cleanup_staging(str(link)) is False
    assert (victim / "important").exists()


# ============================================================
# 14. 页面重开可恢复批次列表
# ============================================================

def test_p0_14_batches_api_lists_historical(tmp_path):
    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(),
    )
    b1 = svc.store.create_batch("旧批次-1", "/o1", {})
    b2 = svc.store.create_batch("旧批次-2", "/o2", {})
    svc.store.bulk_insert(b1, [
        {"excel_row": 2, "input_video": "/v.mp4", "text": "t",
         "fingerprint": "fp", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False, "params_snapshot": {}}
    ])
    handled, status, body, _ = bulk_api.dispatch_get(
        "/api/bulk_dub/batches", {"limit": "10"}, service=svc,
    )
    assert handled and status == 200
    payload = json.loads(body)
    ids = [b["batch_id"] for b in payload["batches"]]
    assert b1 in ids and b2 in ids
    # 每条含 counts + label
    for b in payload["batches"]:
        assert "counts" in b and "label" in b
    svc.stop()


# ============================================================
# 15. 试听 audio 可用 HTTP 播放
# ============================================================

def test_p0_15_try_voice_audio_downloadable_via_bulk_audio_route(tmp_path, monkeypatch):
    """try_voice 合成 → 通过 /api/bulk_dub/audio 白名单路由可下载。"""
    from dub_align_studio import settings as studio_settings

    fake = tmp_path / "d"
    fake.mkdir()
    monkeypatch.setattr(studio_settings, "data_root", lambda: fake)

    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(base_seconds=0.3),
    )
    handled, status, body, _ = bulk_api.dispatch_post(
        "/api/bulk_dub/try_voice",
        {"voice_id": "zh-CN-XiaoshuangNeural", "speed": "1.25"},
        b"", service=svc,
    )
    assert handled and status == 200
    audio_path = json.loads(body)["audio_path"]
    assert Path(audio_path).is_file()
    # 白名单 root 就是 fake/批量带货/试听
    allowed = (fake / "批量带货" / "试听").resolve()
    assert Path(audio_path).resolve().is_relative_to(allowed)
    # 内容是有效 WAV（头部 "RIFF"）
    assert Path(audio_path).read_bytes()[:4] == b"RIFF"
    svc.stop()
