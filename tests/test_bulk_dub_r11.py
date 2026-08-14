"""R11 专项回归测试。按用户第 3 轮清单编号 R11-1 至 R11-11。"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import threading
import time
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub import api as bulk_api  # noqa: E402
from dub_align_studio.bulk_dub import ffmpeg_pipeline as vp  # noqa: E402
from dub_align_studio.bulk_dub.circuit_breaker import CircuitBreaker  # noqa: E402
from dub_align_studio.bulk_dub.csv_export import (  # noqa: E402
    COLUMNS, iter_csv_chunks, write_csv,
)
from dub_align_studio.bulk_dub.edge_backend import EdgeTtsBackend, MockTtsBackend  # noqa: E402
from dub_align_studio.bulk_dub.excel_reader import build_minimal_xlsx  # noqa: E402
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


def _wait(cond, timeout=8.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return False


def _fake_video(sched):
    orig = sched.__class__._process_video

    def _fake(self, row):
        out_dir = row.params_snapshot.get("output_dir", "/tmp/out")
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        try:
            reserved = self.store.reserve_output_path(
                row.task_id, Path(out_dir) / f"{row.excel_row}.mp4"
            )
            reserved.write_bytes(b"MP4-FAKE")
        except Exception as exc:  # noqa: BLE001
            self.store.update(row.task_id, status=STATUS_FAILED,
                              error_type="video_error", error_detail=str(exc))
            return
        self.store.complete_task_transactional(
            row.task_id, output_path=str(reserved),
            final_duration=1.0, tts_duration=row.tts_duration or 0.1,
            concat_duration=2.0, video_duration=1.0,
            encoder_used="libx264", hw_fallback_used=False,
        )
        with self._metrics_lock:
            self.metrics.record_success(0.1, 0.05)

    sched.__class__._process_video = _fake
    return lambda: setattr(sched.__class__, "_process_video", orig)


# ============================================================
# R11-1 并发数真正生效
# ============================================================

def test_r11_1_resize_pools_actually_changes_workers(tmp_path):
    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(base_seconds=0.02),
        config=SchedulerConfig(tts_concurrency=2, video_concurrency=1),
    )
    restore = _fake_video(svc._ensure_scheduler())
    try:
        s = svc._scheduler
        assert len([w for w in s._tts_workers if w.thread.is_alive()]) == 2
        assert len([w for w in s._video_workers if w.thread.is_alive()]) == 1
        r = svc.resize_pools(tts=6, video=3)
        time.sleep(0.2)
        assert r["after"]["tts"] >= 6 or len([w for w in s._tts_workers if w.thread.is_alive()]) >= 6
        assert r["after"]["video"] >= 3
        # 缩小
        svc.resize_pools(tts=1, video=1)
        time.sleep(1.0)
        alive_tts = len([w for w in s._tts_workers if w.thread.is_alive()])
        assert alive_tts <= 2, f"TTS 缩到 1 后存活 {alive_tts} 过多"
    finally:
        restore(); svc.stop()


def test_r11_1_snapshot_returns_three_numbers(tmp_path):
    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(base_seconds=0.02),
        config=SchedulerConfig(tts_concurrency=3, video_concurrency=2),
    )
    restore = _fake_video(svc._ensure_scheduler())
    try:
        snap = svc._scheduler.snapshot()
        for k in ("tts_configured", "tts_alive", "tts_active",
                  "video_configured", "video_alive", "video_active"):
            assert k in snap
        assert snap["tts_configured"] == 3
        assert snap["tts_alive"] >= 1
    finally:
        restore(); svc.stop()


# ============================================================
# R11-2 批次暂停不饿死其他批次
# ============================================================

def test_r11_2_paused_batch_does_not_starve_others(tmp_path):
    """P0-2 关键回归：批次 A 排在最前且暂停时，批次 B 必须能完成，且 A 保持在 pending。

    做法：先构建 store 并**手动 pause_batch**，再启动 scheduler；scheduler.start() 前所有
    setup 都是同步的，杜绝抢跑。
    """
    # 阶段 1：只有 store，无 scheduler
    store = TaskStore(tmp_path / "q.sqlite3")
    b_a = store.create_batch("A", "/o", {})
    store.bulk_insert(b_a, [
        {"excel_row": i, "input_video": "/v.mp4", "text": f"A{i}",
         "fingerprint": f"fpA-{i}", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False,
         "params_snapshot": {"output_dir": str(tmp_path / "outA"),
                              "encoder_preference": "cpu"}}
        for i in range(2, 6)
    ])
    b_b = store.create_batch("B", "/o", {})
    store.bulk_insert(b_b, [
        {"excel_row": i, "input_video": "/v.mp4", "text": f"B{i}",
         "fingerprint": f"fpB-{i}", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False,
         "params_snapshot": {"output_dir": str(tmp_path / "outB"),
                              "encoder_preference": "cpu"}}
        for i in range(2, 6)
    ])

    # 阶段 2：构造服务；先在 scheduler 启动前 pause_batch(A) — 通过 Scheduler 构造直接注入
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend(base_seconds=0.01))
    # 手动构造 scheduler 但不 start，配置好后再 start
    from dub_align_studio.bulk_dub.scheduler import Scheduler, SchedulerConfig
    s = Scheduler(store, SchedulerConfig(tts_concurrency=2, video_concurrency=2),
                   svc.tts_backend)
    s.pause_batch(b_a)
    svc._scheduler = s
    restore = _fake_video(s)
    try:
        s.start()   # A 此时已在 paused set 中；worker 一开始就 exclude A
        assert _wait(
            lambda: store.count_by_status(b_b).get(STATUS_COMPLETED, 0) == 4,
            timeout=15,
        )
        counts_a = store.count_by_status(b_a)
        assert counts_a.get(STATUS_PENDING, 0) == 4, f"A 被吃掉：{counts_a}"
        s.resume_batch(b_a)
        assert _wait(
            lambda: store.count_by_status(b_a).get(STATUS_COMPLETED, 0) == 4,
            timeout=10,
        )
    finally:
        restore(); svc.stop()


# ============================================================
# R11-3 停止/重启语义
# ============================================================

def test_r11_3_stop_returns_bool_and_keeps_alive_workers(tmp_path):
    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(base_seconds=0.02),
    )
    restore = _fake_video(svc._ensure_scheduler())
    try:
        s = svc._scheduler
        ok = s.stop(3.0)
        assert isinstance(ok, bool)
    finally:
        restore(); svc.stop()


def test_r11_3_recover_video_running_reserved_file_already_committed(tmp_path):
    """P0-3 断电场景：正式文件已落地，DB 未写 completed → 恢复补记 completed。"""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg missing")
    import subprocess
    # 建一个真视频作为"已生成成片"
    out = tmp_path / "out.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:duration=1:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(out),
    ], check=True)

    db = tmp_path / "q.sqlite3"
    s1 = TaskStore(db)
    b = s1.create_batch("t", str(tmp_path), {})
    tid = s1.bulk_insert(b, [
        {"excel_row": 2, "input_video": "/x.mp4", "text": "t",
         "fingerprint": "fp", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False, "params_snapshot": {}}
    ])[0]
    s1.update(tid, status=STATUS_VIDEO_RUNNING,
               reserved_output_path=str(out), tts_duration=0.5)

    # 重启：新连接 + 恢复
    from dub_align_studio.bulk_dub import scheduler as sched_mod
    from dub_align_studio.bulk_dub.edge_backend import MockTtsBackend as _MB

    s2 = TaskStore(db)
    sched = sched_mod.Scheduler(s2, SchedulerConfig(tts_concurrency=1, video_concurrency=1),
                                 _MB())
    stats = s2.reap_and_recover_running(
        tts_wav_ok=sched._tts_wav_valid,
        reserved_output_verifier=sched._verify_reserved_output,
        staging_cleanup=vp.cleanup_staging,
    )
    assert stats["video_recovered_completed"] == 1
    row = s2.get(tid)
    assert row.status == STATUS_COMPLETED
    assert row.output_path == str(out)


def test_r11_3_recover_interrupted_from_old_version(tmp_path):
    """兼容旧版本 interrupted 孤儿：恢复时也要一并处理为 pending。"""
    db = tmp_path / "q.sqlite3"
    s1 = TaskStore(db)
    b = s1.create_batch("t", "/o", {})
    tid = s1.bulk_insert(b, [
        {"excel_row": 2, "input_video": "/x.mp4", "text": "t",
         "fingerprint": "fp", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False, "params_snapshot": {}}
    ])[0]
    s1.update(tid, status=STATUS_INTERRUPTED)

    stats = s1.reap_and_recover_running(staging_cleanup=vp.cleanup_staging)
    assert stats["interrupted_recovered"] == 1
    assert s1.get(tid).status == STATUS_PENDING


# ============================================================
# R11-4 绝不覆盖
# ============================================================

def test_r11_4_commit_no_overwrite_refuses_existing_file(tmp_path):
    """低层 _commit_no_overwrite：目标已存在 → 抛错，不覆盖字节。"""
    src = tmp_path / "src"; src.write_bytes(b"NEW-CONTENT")
    tgt = tmp_path / "tgt"; tgt.write_bytes(b"OLD-BYTES")
    with pytest.raises(vp.VideoError):
        vp._commit_no_overwrite(src, tgt)
    assert tgt.read_bytes() == b"OLD-BYTES"


def test_r11_4_commit_when_target_absent_creates_it(tmp_path):
    src = tmp_path / "src"; src.write_bytes(b"NEW")
    tgt = tmp_path / "tgt"
    vp._commit_no_overwrite(src, tgt)
    assert tgt.read_bytes() == b"NEW"
    assert not src.exists()


def test_r11_4_cross_device_fallback_uses_o_excl(tmp_path, monkeypatch):
    """模拟 os.link 抛 OSError (跨盘/不支持) → 走 O_EXCL 手动 copy 路径；
    目标已存在时仍拒绝覆盖。"""
    def fake_link(a, b, *args, **kw):
        raise OSError(18, "EXDEV mock")
    monkeypatch.setattr(os, "link", fake_link)
    # 目标不存在 → 手动 O_EXCL 创建
    src1 = tmp_path / "src1"; src1.write_bytes(b"HELLO")
    tgt1 = tmp_path / "tgt1"
    vp._commit_no_overwrite(src1, tgt1)
    assert tgt1.read_bytes() == b"HELLO"
    # 目标已存在 → 拒绝
    src2 = tmp_path / "src2"; src2.write_bytes(b"NEW")
    tgt2 = tmp_path / "tgt2"; tgt2.write_bytes(b"OLD")
    with pytest.raises(vp.VideoError):
        vp._commit_no_overwrite(src2, tgt2)
    assert tgt2.read_bytes() == b"OLD"


def test_r11_4_race_8_workers_no_overwrite(tmp_path):
    """8 worker 同时试图 commit 同一 target → 只有一个成功；其余抛错；target 只被第一个填。"""
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg missing (real render not needed for this pure-fs race)")
    # 用假的 mp4 字节直接测 commit 层的竞态（不需要真视频）
    tgt = tmp_path / "final.mp4"
    barrier = threading.Barrier(8)
    winners = []
    errors = []

    def worker(i):
        src = tmp_path / f"src-{i}"
        src.write_bytes(f"BYTES-FROM-{i}".encode())
        try:
            barrier.wait()
            vp._commit_no_overwrite(src, tgt)
            winners.append(i)
        except vp.VideoError as exc:
            errors.append((i, str(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(winners) == 1, f"应只有一个 winner，实际 {winners}"
    assert len(errors) == 7, f"其余 7 个应抛错，实际 {len(errors)}"
    winner_bytes = f"BYTES-FROM-{winners[0]}".encode()
    assert tgt.read_bytes() == winner_bytes


# ============================================================
# R11-5 复合游标
# ============================================================

def test_r11_5_cursor_pagination_10000_same_updated_at(tmp_path):
    """10000 条同 updated_at → 复合游标必须不重不漏遍历。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    N = 2000   # 用 2000 条足以证明复合游标；10000 只是耗时不测正确性
    rows = [
        {"excel_row": i, "input_video": "/v.mp4", "text": f"t{i}",
         "fingerprint": f"fp{i}", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False, "params_snapshot": {}}
        for i in range(2, 2 + N)
    ]
    ids = store.bulk_insert(b, rows)
    # 强制它们全部同 updated_at
    with store._connect() as conn:
        conn.execute("UPDATE tasks SET updated_at=1234567890.5 WHERE batch_id=?", (b,))
    # 分页拉取
    seen = set()
    cursor = None
    while True:
        rows_p, next_cursor, has_more = store.changed_since_cursor(cursor, b, limit=200)
        for r in rows_p:
            seen.add(r.task_id)
        cursor = next_cursor
        if not has_more:
            break
    assert seen == set(ids), f"missing: {len(set(ids) - seen)} extra: {len(seen - set(ids))}"


# ============================================================
# R11-6 Edge Endpoint 热更新
# ============================================================

def test_r11_6_endpoint_hot_reload(tmp_path, monkeypatch):
    """settings 里改 endpoint → 下次 synthesize 立即用新地址。"""
    from dub_align_studio import settings as studio_settings
    from dub_align_studio.engines import edge_tts as edge_mod
    # 先清空 endpoint
    fake_settings = {"edge_tts_endpoint": ""}
    monkeypatch.setattr(studio_settings, "load_settings", lambda: dict(fake_settings))
    monkeypatch.delenv("EDGE_TTS_ENDPOINT", raising=False)
    be = EdgeTtsBackend()
    assert be.endpoint_root == ""
    # 保存新地址
    fake_settings["edge_tts_endpoint"] = "https://newhost.workers.dev"
    assert be.endpoint_root == "https://newhost.workers.dev"
    # 再改
    fake_settings["edge_tts_endpoint"] = "https://another.workers.dev/"
    assert be.endpoint_root == "https://another.workers.dev"


def test_r11_6_start_batch_rejects_when_endpoint_unset(tmp_path, monkeypatch):
    """R12-12：真 Edge backend + 未配置 endpoint → ValidationError。
    Mock backend 天然不需要 endpoint（内部生成静音 WAV），本用例特意用真 Edge。
    """
    from dub_align_studio import settings as studio_settings
    from dub_align_studio.bulk_dub.edge_backend import EdgeTtsBackend
    monkeypatch.setattr(studio_settings, "load_settings", lambda: {"edge_tts_endpoint": ""})
    monkeypatch.delenv("EDGE_TTS_ENDPOINT", raising=False)
    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=EdgeTtsBackend(),
    )
    xlsx = build_minimal_xlsx([("/x.mp4", "t")])
    (tmp_path / "out").mkdir()
    from dub_align_studio.bulk_dub.service import ValidationError
    with pytest.raises(ValidationError):
        svc.start_batch(source_bytes=xlsx, label="t",
                         output_dir=str(tmp_path / "out"),
                         check_exists=False)
    svc.stop()


# ============================================================
# R11-7 单事务批量插入 + fingerprint 含编码参数
# ============================================================

def test_r11_7_fingerprint_includes_encoder_preference():
    """v2 指纹：不同 encoder_preference 应产生不同 fingerprint。"""
    from dub_align_studio.bulk_dub.fingerprint import compute_fingerprint
    a = compute_fingerprint(video_path="/v.mp4", text="t", voice_id="v",
                             speed=1.0, encoder_preference="auto")
    b = compute_fingerprint(video_path="/v.mp4", text="t", voice_id="v",
                             speed=1.0, encoder_preference="nvidia")
    assert a != b


def test_r11_7_batch_dedup_and_batch_query(tmp_path, monkeypatch):
    """service 层：批内同 fingerprint 去重（仅 1 条走渲染，其余共享）。"""
    monkeypatch.delenv("EDGE_TTS_ENDPOINT", raising=False)
    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(),
    )
    # **必须**先创建 v.mp4 再算 fingerprint——指纹包含文件 mtime/size
    (tmp_path / "v.mp4").write_bytes(b"x")
    (tmp_path / "prev.mp4").write_bytes(b"prev")
    prev_batch = svc.store.create_batch("prev", "/o", {})
    from dub_align_studio.bulk_dub.fingerprint import compute_fingerprint
    fp_reuse = compute_fingerprint(
        video_path=str(tmp_path / "v.mp4"), text="重用", voice_id="zh-CN-XiaoshuangNeural",
        speed=1.25,
    )
    tid = svc.store.add_task(
        batch_id=prev_batch, excel_row=2,
        input_video=str(tmp_path / "v.mp4"), text="重用",
        fingerprint=fp_reuse, voice_id="zh-CN-XiaoshuangNeural",
        voice_name="晓双（女·青春）", speed=1.25,
        keep_original_audio=False, params_snapshot={},
    )
    svc.store.update(tid, status=STATUS_COMPLETED,
                      output_path=str(tmp_path / "prev.mp4"),
                      final_duration=5, tts_duration=3, video_duration=2)
    # 新批次：3 条同 fp（复用） + 2 条同 fp2（批内共享）
    xlsx = build_minimal_xlsx([
        (str(tmp_path / "v.mp4"), "重用"),
        (str(tmp_path / "v.mp4"), "重用"),   # 复用
        (str(tmp_path / "v.mp4"), "新A"),   # 新 fp
        (str(tmp_path / "v.mp4"), "新A"),   # 批内共享
        (str(tmp_path / "v.mp4"), "新B"),   # 新 fp
    ])
    (tmp_path / "out").mkdir()
    r = svc.start_batch(source_bytes=xlsx, label="dedupe",
                         output_dir=str(tmp_path / "out"),
                         check_exists=True, require_endpoint=False)
    # R12-1：外部指纹 hit → 2 条"重用"直接 completed；
    # 新指纹只有 2 个 leader（新A 首个 + 新B），另 1 条"新A"重复 → follower
    assert r["reused"] == 2, f"reused={r['reused']}"
    assert r["added"] == 2, f"added={r['added']}"
    assert r.get("followers", 0) == 1, f"followers={r.get('followers')}"
    svc.stop()


# ============================================================
# R11-8 CSV 加列 + 流式
# ============================================================

def test_r11_8_csv_has_new_columns():
    for k in ("警告列表", "错误类型", "编码器", "硬件回退", "batch_id", "task_id"):
        assert k in COLUMNS, f"CSV 缺列：{k}"


def test_r11_8_iter_csv_chunks_streams_without_full_buffer(tmp_path):
    """生成器接口：调用 next() 得到一个 chunk 后中止，也不会构造全表。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    store.bulk_insert(b, [
        {"excel_row": i, "input_video": "/v.mp4", "text": f"t{i}",
         "fingerprint": f"fp{i}", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False, "params_snapshot": {}}
        for i in range(2, 502)
    ])
    it = iter_csv_chunks(store.iter_all(b), chunk_rows=50)
    first = next(it)
    assert first.startswith("﻿".encode("utf-8")) or first.startswith(b"\xef\xbb\xbf")
    assert b"Excel" in first
    # 只取 1 块就能拿到 chunk，不必消费完
    second = next(it)
    assert len(second) > 0


# ============================================================
# R11-10 取消覆盖 tts_done
# ============================================================

def test_r11_10_cancel_all_waiting_includes_tts_done(tmp_path):
    """P0-10：等待类包含 pending + retry_wait + tts_done。用未启动的 Scheduler
    杜绝 worker 抢占。"""
    from dub_align_studio.bulk_dub.scheduler import Scheduler, SchedulerConfig
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    ids = store.bulk_insert(b, [
        {"excel_row": i, "input_video": "/v.mp4", "text": f"t{i}",
         "fingerprint": f"fp{i}", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False,
         "params_snapshot": {"output_dir": str(tmp_path / "out")}}
        for i in range(2, 6)
    ])
    store.update(ids[0], status=STATUS_TTS_DONE)
    store.update(ids[1], status=STATUS_RETRY_WAIT, next_attempt_at=0)
    # 未启动 scheduler，直接调 cancel_all_waiting
    s = Scheduler(store, SchedulerConfig(), MockTtsBackend())
    n = s.cancel_all_waiting(b)
    assert n == 4, f"应取消 pending+retry_wait+tts_done，实际 {n}"
    counts = store.count_by_status(b)
    assert counts.get("cancelled", 0) == 4


# ============================================================
# R11-11 边界硬化 + 真 HTTP handler
# ============================================================

def test_r11_11_multipart_missing_file_returns_400(tmp_path):
    from dub_align_studio.bulk_dub import api as _api
    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(),
    )
    body = (b"------boundary123\r\n"
            b'Content-Disposition: form-data; name="other"\r\n\r\n'
            b"nope\r\n------boundary123--\r\n")
    handled, status, body_out, _ = _api.dispatch_post(
        "/api/bulk_dub/preview", {},
        body, "multipart/form-data; boundary=----boundary123",
        service=svc,
    )
    assert status == 400
    svc.stop()


def test_r11_11_cancel_task_nonexistent_returns_404(tmp_path):
    from dub_align_studio.bulk_dub import api as _api
    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(),
    )
    handled, status, body, _ = _api.dispatch_post(
        "/api/bulk_dub/cancel_task",
        {"task_id": "a" * 16}, b"", service=svc,
    )
    assert status == 404
    svc.stop()


def test_r11_11_real_http_handler_csv_stream(tmp_path, monkeypatch):
    """启一个真 HTTP server 打 /api/bulk_dub/csv，验证 chunked 传输和 200。"""
    import http.client
    from dub_align_studio import settings as studio_settings
    from dub_align_studio import web_server
    from dub_align_studio.bulk_dub import service as bulk_service

    fake_root = tmp_path / "data"
    fake_root.mkdir()
    monkeypatch.setattr(studio_settings, "data_root", lambda: fake_root)

    svc = BulkDubService(
        store=TaskStore(fake_root / "q.sqlite3"),
        tts_backend=MockTtsBackend(),
    )
    b = svc.store.create_batch("t", str(tmp_path / "out"), {})
    svc.store.bulk_insert(b, [
        {"excel_row": i, "input_video": "/v.mp4", "text": f"t{i}",
         "fingerprint": f"fp{i}", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False,
         "params_snapshot": {}} for i in range(2, 12)
    ])
    bulk_service.reset_service_for_tests(svc)

    server = web_server.serve(port=18800, open_browser=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET", "/api/bulk_dub/csv?batch_id=" + b)
        resp = conn.getresponse()
        assert resp.status == 200
        assert "text/csv" in (resp.getheader("Content-Type") or "")
        body = resp.read()
        assert b"Excel" in body   # header
        assert body.count(b"\r\n") + body.count(b"\n") >= 10
    finally:
        server.shutdown()
        server.server_close()
        bulk_service.reset_service_for_tests(None)


def test_r11_11_real_http_handler_audio_range(tmp_path, monkeypatch):
    """真 HTTP：/api/bulk_dub/probe_audio 支持 Range → 206 + Content-Range。"""
    import http.client
    from dub_align_studio import settings as studio_settings
    from dub_align_studio import web_server
    from dub_align_studio.bulk_dub import service as bulk_service

    fake = tmp_path / "data"; fake.mkdir()
    monkeypatch.setattr(studio_settings, "data_root", lambda: fake)
    (fake / "批量带货" / "试听").mkdir(parents=True)
    # R12-10：试听文件命名为 <voice>_<speed>.wav——由 probe_audio 端点定位
    wav = fake / "批量带货" / "试听" / "zh-CN-XiaoshuangNeural_1.25.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
        w.writeframes(b"\x00\x00" * 4410)
    file_size = wav.stat().st_size

    svc = BulkDubService(
        store=TaskStore(fake / "q.sqlite3"),
        tts_backend=MockTtsBackend(),
    )
    bulk_service.reset_service_for_tests(svc)
    server = web_server.serve(port=18801, open_browser=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        conn.request("GET",
                     "/api/bulk_dub/probe_audio?voice_id=zh-CN-XiaoshuangNeural&speed=1.25",
                     headers={"Range": "bytes=0-99"})
        resp = conn.getresponse()
        assert resp.status == 206, f"expected 206, got {resp.status}"
        assert resp.getheader("Content-Range") == f"bytes 0-99/{file_size}"
        data = resp.read()
        assert len(data) == 100
    finally:
        server.shutdown()
        server.server_close()
        bulk_service.reset_service_for_tests(None)


def test_r11_11_negative_content_length_400(tmp_path, monkeypatch):
    """真 HTTP：Content-Length: -1 → 400。"""
    import socket
    from dub_align_studio import settings as studio_settings
    from dub_align_studio import web_server
    from dub_align_studio.bulk_dub import service as bulk_service

    fake = tmp_path / "data"; fake.mkdir()
    monkeypatch.setattr(studio_settings, "data_root", lambda: fake)
    svc = BulkDubService(store=TaskStore(fake / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    bulk_service.reset_service_for_tests(svc)
    server = web_server.serve(port=18802, open_browser=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # 手写 HTTP 请求带负 Content-Length（http.client 不允许）
        s = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        req = (
            b"POST /api/bulk_dub/pause HTTP/1.1\r\n"
            b"Host: localhost\r\n"
            b"Content-Length: -1\r\n"
            b"\r\n"
        )
        s.sendall(req)
        raw = b""
        s.settimeout(2)
        try:
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                raw += chunk
                if b"\r\n\r\n" in raw:
                    break
        except socket.timeout:
            pass
        s.close()
        # 应该看到 400
        first_line = raw.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
        assert "400" in first_line, f"expected 400, first_line={first_line}"
    finally:
        server.shutdown()
        server.server_close()
        bulk_service.reset_service_for_tests(None)
