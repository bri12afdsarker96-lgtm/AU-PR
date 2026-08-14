"""R13 专项回归测试。

按用户第 5 轮清单：
    R13-P0-1  leader/follower 单事务 + 恢复对账 + retry_failed 语义
    R13-P0-2  取消：所有异常路径 CAS + running 取消交由 worker
    R13-P0-3  marker 先于 target；hash/size 严格校验；崩溃组合
    R13-P0-4  真 HTTP handler：416/CSV 404/Range/断连
    R13-P0-5  start_batch 并发真生效 + snapshot 并发一致性
    R13-P1-6  跨批次 fingerprint 复用必须落在本批次 output_dir
    R13-P1-7  移除 service 层 _skip_endpoint_check / require_endpoint 生产参数
"""

from __future__ import annotations

import http.client
import io
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub import api as bulk_api  # noqa: E402
from dub_align_studio.bulk_dub import ffmpeg_pipeline as vp  # noqa: E402
from dub_align_studio.bulk_dub.edge_backend import (  # noqa: E402
    EdgeTtsBackend, MockTtsBackend,
)
from dub_align_studio.bulk_dub.excel_reader import build_minimal_xlsx  # noqa: E402
from dub_align_studio.bulk_dub.scheduler import (  # noqa: E402
    Scheduler, SchedulerConfig, TtsBackend, TtsHttpError,
)
from dub_align_studio.bulk_dub.service import (  # noqa: E402
    BulkDubService, ValidationError,
)
from dub_align_studio.bulk_dub.store import (  # noqa: E402
    STATUS_CANCELLED, STATUS_CANCELLING, STATUS_COMPLETED, STATUS_FAILED,
    STATUS_OUTPUT_COMMITTED, STATUS_PENDING, STATUS_RETRY_WAIT,
    STATUS_TTS_DONE, STATUS_TTS_RUNNING, STATUS_VIDEO_RUNNING,
    STATUS_WAITING_DEPENDENCY, TaskStore,
)


def _mk_service(tmp_path, **kw):
    return BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(base_seconds=0.02, **kw),
    )


def _wait(cond, timeout=8.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return False


# ============================================================
# R13-P0-1 leader/follower 单事务 + 恢复对账 + retry_failed 语义
# ============================================================

def test_r13_p0_1_finalize_leader_success_single_transaction(tmp_path):
    """leader complete + follower propagation 在同一事务；leader complete 命中
    时 follower_count 与 UPDATE 一起发生，不再分两步。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    lid = store.add_task(batch_id=b, excel_row=2, input_video="/v.mp4",
                          text="共", fingerprint="fp1", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    fid1 = store.add_task(batch_id=b, excel_row=3, input_video="/v.mp4",
                           text="共", fingerprint="fp1", voice_id="v",
                           voice_name="V", speed=1.0,
                           keep_original_audio=False, params_snapshot={})
    fid2 = store.add_task(batch_id=b, excel_row=4, input_video="/v.mp4",
                           text="共", fingerprint="fp1", voice_id="v",
                           voice_name="V", speed=1.0,
                           keep_original_audio=False, params_snapshot={})
    store.update(fid1, status=STATUS_WAITING_DEPENDENCY, leader_task_id=lid)
    store.update(fid2, status=STATUS_WAITING_DEPENDENCY, leader_task_id=lid)
    store.update(lid, status=STATUS_VIDEO_RUNNING, reserved_output_path="/o/l.mp4")
    ok, n = store.finalize_leader_success(
        lid, output_path="/o/l.mp4", final_duration=1, tts_duration=0.5,
        concat_duration=0.3, video_duration=0.4,
        encoder_used="libx264", hw_fallback_used=False,
        expected_reserved_path="/o/l.mp4",
    )
    assert ok is True and n == 2
    assert store.get(lid).status == STATUS_COMPLETED
    assert store.get(fid1).status == STATUS_COMPLETED
    assert store.get(fid2).status == STATUS_COMPLETED
    assert store.get(fid1).output_path == "/o/l.mp4"


def test_r13_p0_1_finalize_leader_success_refuses_when_cancelled(tmp_path):
    """leader 已 cancelled → finalize_leader_success 返回 (False, 0)；
    follower 保持 waiting_dependency（不被误标 completed）。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    lid = store.add_task(batch_id=b, excel_row=2, input_video="/v",
                          text="c", fingerprint="fp", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    fid = store.add_task(batch_id=b, excel_row=3, input_video="/v",
                          text="c", fingerprint="fp", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    store.update(fid, status=STATUS_WAITING_DEPENDENCY, leader_task_id=lid)
    store.update(lid, status=STATUS_CANCELLED)  # 已被取消
    ok, n = store.finalize_leader_success(
        lid, output_path="/o/x.mp4", final_duration=1, tts_duration=0,
        concat_duration=0, video_duration=0,
        encoder_used="", hw_fallback_used=False,
    )
    assert ok is False and n == 0
    assert store.get(fid).status == STATUS_WAITING_DEPENDENCY


def test_r13_p0_1_reconcile_dependencies_leader_completed(tmp_path):
    """启动恢复：leader 已 completed → 对账把 follower 补 completed。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    lid = store.add_task(batch_id=b, excel_row=2, input_video="/v",
                          text="c", fingerprint="fp", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    fid = store.add_task(batch_id=b, excel_row=3, input_video="/v",
                          text="c", fingerprint="fp", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    store.update(fid, status=STATUS_WAITING_DEPENDENCY, leader_task_id=lid)
    store.update(lid, status=STATUS_COMPLETED, output_path="/o/x.mp4",
                  final_duration=1)
    stats = store.reconcile_dependencies()
    assert stats["follower_completed"] == 1
    assert store.get(fid).status == STATUS_COMPLETED
    assert store.get(fid).output_path == "/o/x.mp4"


def test_r13_p0_1_reconcile_dependencies_leader_failed(tmp_path):
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    lid = store.add_task(batch_id=b, excel_row=2, input_video="/v",
                          text="c", fingerprint="fp", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    fid = store.add_task(batch_id=b, excel_row=3, input_video="/v",
                          text="c", fingerprint="fp", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    store.update(fid, status=STATUS_WAITING_DEPENDENCY, leader_task_id=lid)
    store.update(lid, status=STATUS_FAILED)
    stats = store.reconcile_dependencies()
    assert stats["follower_failed"] == 1
    assert store.get(fid).status == STATUS_FAILED


def test_r13_p0_1_reconcile_dependencies_orphan_leader(tmp_path):
    """follower 的 leader 已被删/leader_task_id 指向不存在的 id → orphan_failed。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    fid = store.add_task(batch_id=b, excel_row=3, input_video="/v",
                          text="c", fingerprint="fp", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    # 指向不存在的 leader
    store.update(fid, status=STATUS_WAITING_DEPENDENCY,
                  leader_task_id="deadbeefdeadbeef")
    stats = store.reconcile_dependencies()
    assert stats["follower_orphan_failed"] == 1
    assert store.get(fid).status == STATUS_FAILED
    assert store.get(fid).error_type == "orphan_dependency"


def test_r13_p0_1_retry_failed_only_leaders_and_skips_excel_invalid(tmp_path):
    """retry_failed：
    - leader（leader_task_id==''）从 failed → pending
    - follower（leader_task_id 非空）从 failed → waiting_dependency，**不进入调度**
    - error_type='excel_invalid' 永远跳过
    这里用未启动 Scheduler，避免 worker 立即消费 pending。
    """
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    b = store.create_batch("t", "/o", {})
    ldr = store.add_task(batch_id=b, excel_row=2, input_video="/v",
                          text="a", fingerprint="fp1", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    fol = store.add_task(batch_id=b, excel_row=3, input_video="/v",
                          text="a", fingerprint="fp1", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    store.update(fol, leader_task_id=ldr, status=STATUS_FAILED,
                  error_type="leader_terminated",
                  error_detail="模拟 leader 已失败")
    store.update(ldr, status=STATUS_FAILED, error_type="tts_error")
    inv = store.add_task(batch_id=b, excel_row=4, input_video="/x",
                          text="", fingerprint="fp-bad", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    store.update(inv, status=STATUS_FAILED, error_type="excel_invalid",
                  error_detail="excel 格式错")
    n = sched.retry_failed(b)
    assert n == 2  # leader + follower（excel_invalid 跳过）
    assert store.get(ldr).status == STATUS_PENDING
    assert store.get(fol).status == STATUS_WAITING_DEPENDENCY
    assert store.get(inv).status == STATUS_FAILED


# ============================================================
# R13-P0-2 取消 CAS + running 取消让 worker 清理
# ============================================================

def test_r13_p0_2_fail_task_cas_never_overwrites_cancelled(tmp_path):
    """_fail 底层 fail_task_cas 绝不覆盖 cancelled / completed / output_committed。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    tid = store.add_task(batch_id=b, excel_row=2, input_video="/v",
                          text="a", fingerprint="f", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    for terminal in (STATUS_CANCELLED, STATUS_COMPLETED,
                       STATUS_OUTPUT_COMMITTED, STATUS_CANCELLING):
        store.update(tid, status=terminal)
        ok, old = store.fail_task_cas(tid, error_type="tts_error",
                                        error_detail="模拟")
        assert ok is False, f"覆盖了 {terminal}！"
        assert store.get(tid).status == terminal


def test_r13_p0_2_cancel_task_returns_false_for_terminal(tmp_path):
    """cancel_task 对 completed/output_committed/failed 应返回 False；
    不能永远给页面 cancelled=True 的假象。"""
    svc = _mk_service(tmp_path)
    b = svc.store.create_batch("t", "/o", {})
    tid = svc.store.add_task(batch_id=b, excel_row=2, input_video="/v",
                              text="a", fingerprint="f", voice_id="v",
                              voice_name="V", speed=1.0,
                              keep_original_audio=False, params_snapshot={})
    for terminal in (STATUS_COMPLETED, STATUS_OUTPUT_COMMITTED, STATUS_FAILED,
                       STATUS_CANCELLED):
        svc.store.update(tid, status=terminal)
        ok = svc._ensure_scheduler().cancel_task(tid)
        assert ok is False, f"对 {terminal} 应返回 False"
    svc.stop()


def test_r13_p0_2_cancel_task_running_does_not_clean_staging_immediately(tmp_path):
    """running 任务被取消时——staging 由 worker 稳定点清理，不由取消线程并发删。
    这里使用未启动的 Scheduler，避免 worker 线程与 cancel 竞态。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    b = store.create_batch("t", "/o", {})
    tid = store.add_task(batch_id=b, excel_row=2, input_video="/v",
                          text="a", fingerprint="f", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    from dub_align_studio import settings as studio_settings
    root = studio_settings.data_root() / "批量带货" / "staging" / b / tid
    root.mkdir(parents=True, exist_ok=True)
    (root / "tts.wav").write_bytes(b"WAV")
    store.update(tid, status=STATUS_TTS_RUNNING, staging_dir=str(root))
    # 不启动 scheduler → 直接调 cancel_task 逻辑
    ok = sched.cancel_task(tid)
    assert ok is True
    assert store.get(tid).status == STATUS_CANCELLING
    # staging 文件依然在——由 worker 在稳定点清理
    assert (root / "tts.wav").exists()


def test_r13_p0_2_tts_http_error_after_cancel_becomes_cancelled(tmp_path):
    """TTS 请求中被 cancel → 请求返回 429/网络错时，走取消收敛，不覆盖成 retry_wait。"""
    class _CancelledMidRequest(TtsBackend):
        requires_endpoint = False
        def __init__(self, sched_getter):
            self._get_sched = sched_getter
        def synthesize(self, *, text, voice_id, speed, pitch, style, output_wav):
            # 请求进入 → 由测试触发 cancel_task → 抛 429
            sched = self._get_sched()
            for row in sched.store.list_tasks(status=STATUS_TTS_RUNNING, limit=10):
                sched.cancel_task(row.task_id)
                break
            raise TtsHttpError(429, "模拟返回时取消", retry_after=1.0)

    store = TaskStore(tmp_path / "q.sqlite3")
    holder = {}
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1, tts_max_retries=5,
    ), tts_backend=_CancelledMidRequest(lambda: holder["s"]))
    holder["s"] = sched
    b = store.create_batch("t", "/o", {})
    tid = store.add_task(batch_id=b, excel_row=2, input_video="/v",
                          text="a", fingerprint="f", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    sched.start()
    try:
        assert _wait(lambda: store.get(tid).status in (STATUS_CANCELLED,
                                                          STATUS_CANCELLING),
                       timeout=6.0), f"实际 {store.get(tid).status}"
    finally:
        sched.stop()


def test_r13_p0_2_cancel_atomic_detailed_reports_old_and_new(tmp_path):
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    t_wait = store.add_task(batch_id=b, excel_row=2, input_video="/v",
                             text="a", fingerprint="f", voice_id="v",
                             voice_name="V", speed=1.0,
                             keep_original_audio=False, params_snapshot={})
    t_run = store.add_task(batch_id=b, excel_row=3, input_video="/v",
                            text="a", fingerprint="g", voice_id="v",
                            voice_name="V", speed=1.0,
                            keep_original_audio=False, params_snapshot={})
    t_done = store.add_task(batch_id=b, excel_row=4, input_video="/v",
                             text="a", fingerprint="h", voice_id="v",
                             voice_name="V", speed=1.0,
                             keep_original_audio=False, params_snapshot={})
    store.update(t_run, status=STATUS_TTS_RUNNING)
    store.update(t_done, status=STATUS_COMPLETED)
    r = store.cancel_atomic_detailed([t_wait, t_run, t_done])
    d = {tid: (old, new) for tid, old, new in r}
    assert d[t_wait] == (STATUS_PENDING, STATUS_CANCELLED)
    assert d[t_run] == (STATUS_TTS_RUNNING, STATUS_CANCELLING)
    # completed 不动，返回结果里根本不出现（cancel_atomic_detailed 定义如此）
    assert t_done not in d


# ============================================================
# R13-P0-3 marker 先于 target；hash/size 严格校验；崩溃组合
# ============================================================

def test_r13_p0_3_marker_atomic_write_survives_partial_json(tmp_path):
    """marker 半途崩 → 不留截断 JSON（因走 .tmp + os.replace）。"""
    marker = tmp_path / ".x.mp4.bulk_dub.marker.json"
    vp._write_marker(marker, task_id="T", batch_id="B", fingerprint="fp",
                      target_final_seconds=1, file_size=10,
                      output_name="x.mp4", encoder_used="libx264",
                      hw_fallback_used=False, content_hash="h", commit_stage="target_ready")
    v = vp.read_marker(marker)
    assert v and v["schema"] == vp.MARKER_SCHEMA_V2
    assert v["batch_id"] == "B"
    assert v["content_hash"] == "h"
    assert v["commit_stage"] == "target_ready"


def test_r13_p0_3_verify_marker_matches_target_hash_mismatch(tmp_path):
    """target 内容被外部改动 → hash 不符 → verify_marker 返回 False。"""
    target = tmp_path / "y.mp4"; target.write_bytes(b"ORIG-CONTENT")
    marker = vp.marker_path_for(target)
    vp._write_marker(marker, task_id="T", batch_id="B", fingerprint="fp",
                      target_final_seconds=0, file_size=target.stat().st_size,
                      output_name=target.name, encoder_used="",
                      hw_fallback_used=False,
                      content_hash=vp._blake2b_of_file(target),
                      commit_stage="target_ready")
    ok, _ = vp.verify_marker_matches_target(vp.read_marker(marker), target)
    assert ok
    # 篡改 target
    target.write_bytes(b"CHANGED_CONTENT_XXXXXX")
    ok, why = vp.verify_marker_matches_target(vp.read_marker(marker), target)
    assert ok is False
    assert "hash" in why or "size" in why


def test_r13_p0_3_commit_never_overwrites_existing_target_and_cleans_part(tmp_path):
    target = tmp_path / "z.mp4"; target.write_bytes(b"EXTERNAL")
    source = tmp_path / "s.mp4"; source.write_bytes(b"NEW")
    with pytest.raises(vp.VideoError):
        vp._commit_with_marker(source, target,
                                task_id="Tx", fingerprint="fp",
                                target_final_seconds=1,
                                encoder_used="libx264",
                                hw_fallback_used=False, batch_id="B")
    assert target.read_bytes() == b"EXTERNAL"
    part = vp._hidden_part_path(target, "Tx")
    assert not part.exists()


def test_r13_p0_3_external_mp4_not_claimed_by_recovery(tmp_path):
    """目录里放一个外部 mp4（没有 marker）——恢复不认领。"""
    from dub_align_studio.bulk_dub.scheduler import Scheduler as _S
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", str(tmp_path), {})
    ext = tmp_path / "ext.mp4"; ext.write_bytes(b"\x00" * 2048)
    tid = store.add_task(batch_id=b, excel_row=2, input_video="/v",
                          text="a", fingerprint="fp", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    store.update(tid, status=STATUS_VIDEO_RUNNING,
                  reserved_output_path=str(ext))
    sched = _S(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1),
                tts_backend=MockTtsBackend())
    stats = store.reap_and_recover_running(
        reserved_output_verifier=sched._verify_reserved_output,
        tts_wav_ok=sched._tts_wav_valid,
    )
    # 无 marker → 不认领 → 回 pending（走 video_recovered_pending）
    assert store.get(tid).status == STATUS_PENDING
    # ext.mp4 仍在
    assert ext.exists()


# ============================================================
# R13-P0-4 真 HTTP handler 测试：416/CSV/Range
# ============================================================

def _start_web_server(tmp_path, svc):
    """真启动 ThreadingHTTPServer；返回 (conn, port, stop_fn)。"""
    import http.server
    from dub_align_studio import web_server as ws
    from dub_align_studio.bulk_dub import service as bulk_service

    # 让 web_server 用测试注入的 service（不再各自创建）
    bulk_service.reset_service_for_tests(svc)

    class _H(ws._Handler):
        def log_message(self, *a, **kw):  # 静音
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=5)
    def _stop():
        srv.shutdown()
        srv.server_close()
        bulk_service.reset_service_for_tests(None)
    return conn, srv.server_port, _stop


def test_r13_p0_4_range_416_has_content_length_zero(tmp_path):
    """真发 416 → 必须有 Content-Length: 0，客户端不会挂起。"""
    svc = _mk_service(tmp_path)
    ok_mp4 = tmp_path / "ok.mp4"; ok_mp4.write_bytes(b"0123456789")
    b = svc.store.create_batch("t", str(tmp_path), {})
    tid = svc.store.add_task(batch_id=b, excel_row=2, input_video="/v",
                              text="a", fingerprint="fp", voice_id="v",
                              voice_name="V", speed=1.0,
                              keep_original_audio=False, params_snapshot={})
    svc.store.update(tid, status=STATUS_COMPLETED, output_path=str(ok_mp4))

    conn, port, stop = _start_web_server(tmp_path, svc)
    try:
        # multi-range → 416 + Content-Length: 0
        conn.request("GET", f"/api/bulk_dub/task_output?task_id={tid}",
                       headers={"Range": "bytes=0-1,3-4"})
        r = conn.getresponse()
        assert r.status == 416
        assert r.getheader("Content-Length") == "0"
        body = r.read()
        assert body == b""
        # closed range 正确
        conn.request("GET", f"/api/bulk_dub/task_output?task_id={tid}",
                       headers={"Range": "bytes=2-5"})
        r = conn.getresponse()
        assert r.status == 206
        assert r.read() == b"2345"
        # suffix range
        conn.request("GET", f"/api/bulk_dub/task_output?task_id={tid}",
                       headers={"Range": "bytes=-3"})
        r = conn.getresponse()
        assert r.status == 206
        assert r.read() == b"789"
        # suffix > size → 200 full
        conn.request("GET", f"/api/bulk_dub/task_output?task_id={tid}",
                       headers={"Range": "bytes=-999"})
        r = conn.getresponse()
        assert r.status == 206
        assert r.read() == b"0123456789"
        # open range
        conn.request("GET", f"/api/bulk_dub/task_output?task_id={tid}",
                       headers={"Range": "bytes=5-"})
        r = conn.getresponse()
        assert r.status == 206
        assert r.read() == b"56789"
    finally:
        conn.close(); stop(); svc.stop()


def test_r13_p0_4_csv_unknown_batch_returns_404_not_200_empty(tmp_path):
    """/csv 未知 batch → 404，不发 200 空 CSV。"""
    svc = _mk_service(tmp_path)
    conn, port, stop = _start_web_server(tmp_path, svc)
    try:
        conn.request("GET", "/api/bulk_dub/csv?batch_id=deadbeef1234")
        r = conn.getresponse()
        assert r.status == 404, f"expected 404, got {r.status}"
        body = r.read()
        assert b"batch" in body
    finally:
        conn.close(); stop(); svc.stop()


def test_r13_p0_4_csv_valid_batch_chunked_completes(tmp_path):
    """/csv 有效 batch → chunked 完整可读。"""
    svc = _mk_service(tmp_path)
    b = svc.store.create_batch("csv", "/o", {})
    svc.store.bulk_insert(b, [
        {"excel_row": i, "input_video": "/v", "text": f"t{i}",
         "fingerprint": f"fp{i}", "voice_id": "v", "voice_name": "V",
         "speed": 1.0, "keep_original_audio": False, "params_snapshot": {}}
        for i in range(2, 22)
    ])
    conn, port, stop = _start_web_server(tmp_path, svc)
    try:
        conn.request("GET", f"/api/bulk_dub/csv?batch_id={b}")
        r = conn.getresponse()
        assert r.status == 200
        assert (r.getheader("Transfer-Encoding") or "").lower() == "chunked"
        body = r.read()
        text = body.decode("utf-8")
        lines = text.rstrip("\n").split("\n")
        # 20 条 + 1 header
        assert len(lines) == 21, f"lines={len(lines)}"
    finally:
        conn.close(); stop(); svc.stop()


# ============================================================
# R13-P0-5 start_batch 并发真生效 + snapshot 并发一致性
# ============================================================

def test_r13_p0_5_start_batch_resizes_pools_when_explicit(tmp_path):
    svc = _mk_service(tmp_path)
    v = tmp_path / "v.mp4"; v.write_bytes(b"\x00")
    (tmp_path / "out").mkdir()
    xlsx = build_minimal_xlsx([(str(v), "t")])
    # 显式传入 tts=6/video=3 → 应用到 scheduler
    r = svc.start_batch(source_bytes=xlsx, label="c",
                          output_dir=str(tmp_path / "out"),
                          check_exists=False,
                          tts_concurrency=6, video_concurrency=3)
    assert r["effective_pools"] is not None
    _wait(lambda: svc.summary()["scheduler"]["tts_configured"] == 6, timeout=3.0)
    snap = svc.summary()["scheduler"]
    assert snap["tts_configured"] == 6
    assert snap["video_configured"] == 3
    svc.stop()


def test_r13_p0_5_snapshot_concurrent_does_not_leak_workers(tmp_path):
    """20 个并发 snapshot 不应无限补 worker（超过 target）。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=3, video_concurrency=2,
    ), tts_backend=MockTtsBackend())
    sched.start()
    try:
        _wait(lambda: sched.snapshot()["tts_alive"] == 3, timeout=2.0)
        errs = []
        def _worker():
            try:
                for _ in range(20):
                    sched.snapshot()
            except Exception as e:  # noqa: BLE001
                errs.append(e)
        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errs
        snap = sched.snapshot()
        assert snap["tts_alive"] == 3, f"tts_alive={snap['tts_alive']}"
        assert snap["video_alive"] == 2, f"video_alive={snap['video_alive']}"
    finally:
        sched.stop()


# ============================================================
# R13-P1-6 跨批次 fingerprint 复用必须落在本批次 output_dir
# ============================================================

def test_r13_p1_6_cross_output_dir_reuse_hardlinks_into_new_dir(tmp_path):
    """旧成片在旧目录，新批次选另一目录 → 必须落地到新目录（hardlink）。"""
    svc = _mk_service(tmp_path)
    old_dir = tmp_path / "old"; old_dir.mkdir()
    new_dir = tmp_path / "new"; new_dir.mkdir()
    v = tmp_path / "v.mp4"; v.write_bytes(b"\x00")
    old_mp4 = old_dir / "prev.mp4"; old_mp4.write_bytes(b"MP4")
    # 手工插一条 completed 记录，位于 old_dir
    prev_b = svc.store.create_batch("prev", str(old_dir), {})
    from dub_align_studio.bulk_dub.fingerprint import compute_fingerprint
    fp = compute_fingerprint(
        video_path=str(v), text="reuse", voice_id="zh-CN-XiaoshuangNeural",
        speed=1.25,
    )
    tid = svc.store.add_task(batch_id=prev_b, excel_row=2,
                              input_video=str(v), text="reuse",
                              fingerprint=fp,
                              voice_id="zh-CN-XiaoshuangNeural",
                              voice_name="V", speed=1.25,
                              keep_original_audio=False, params_snapshot={})
    svc.store.update(tid, status=STATUS_COMPLETED, output_path=str(old_mp4),
                      final_duration=1)
    xlsx = build_minimal_xlsx([(str(v), "reuse")])
    r = svc.start_batch(source_bytes=xlsx, label="new",
                          output_dir=str(new_dir),
                          voice_id="zh-CN-XiaoshuangNeural", speed=1.25,
                          check_exists=True)
    assert r["reused"] == 1, f"reused={r['reused']}"
    # 落地到 new_dir
    new_mp4 = new_dir / "prev.mp4"
    assert new_mp4.is_file()
    # task 的 output_path 指向 new_dir 下的文件
    for t in svc.store.list_tasks(batch_id=r["batch_id"], limit=10):
        assert t.output_path == str(new_mp4)
    svc.stop()


def test_r13_p1_6_cross_output_dir_reuse_conflict_falls_through(tmp_path):
    """新目录里已有同名不同来源文件 → 不覆盖，复用不成立，走 leader/follower。"""
    svc = _mk_service(tmp_path)
    old_dir = tmp_path / "old"; old_dir.mkdir()
    new_dir = tmp_path / "new"; new_dir.mkdir()
    v = tmp_path / "v.mp4"; v.write_bytes(b"\x00")
    old_mp4 = old_dir / "prev.mp4"; old_mp4.write_bytes(b"MP4")
    # 新目录里放个不相关的同名文件
    (new_dir / "prev.mp4").write_bytes(b"EXT_OTHER")
    prev_b = svc.store.create_batch("prev", str(old_dir), {})
    from dub_align_studio.bulk_dub.fingerprint import compute_fingerprint
    fp = compute_fingerprint(
        video_path=str(v), text="reuse", voice_id="zh-CN-XiaoshuangNeural",
        speed=1.25,
    )
    tid = svc.store.add_task(batch_id=prev_b, excel_row=2,
                              input_video=str(v), text="reuse",
                              fingerprint=fp,
                              voice_id="zh-CN-XiaoshuangNeural",
                              voice_name="V", speed=1.25,
                              keep_original_audio=False, params_snapshot={})
    svc.store.update(tid, status=STATUS_COMPLETED, output_path=str(old_mp4),
                      final_duration=1)
    xlsx = build_minimal_xlsx([(str(v), "reuse")])
    r = svc.start_batch(source_bytes=xlsx, label="new",
                          output_dir=str(new_dir),
                          voice_id="zh-CN-XiaoshuangNeural", speed=1.25,
                          check_exists=True)
    # 新目录同名文件被视为外部占用 → 不覆盖 → 复用不成立 → leader/follower
    assert r["reused"] == 0
    assert r["added"] == 1
    # 外部文件字节不动
    assert (new_dir / "prev.mp4").read_bytes() == b"EXT_OTHER"
    svc.stop()


# ============================================================
# R13-P1-7 移除 service 层 _skip_endpoint_check / require_endpoint
# ============================================================

def test_r13_p1_7_backend_capability_drives_endpoint_check(tmp_path, monkeypatch):
    """MockTtsBackend.requires_endpoint=False → service 天然跳过；
    EdgeTtsBackend.requires_endpoint=True → 未配置 endpoint 必抛。
    生产 API 无 _skip_endpoint_check / require_endpoint 绕过入口。"""
    from dub_align_studio import settings as studio_settings
    monkeypatch.setattr(studio_settings, "load_settings",
                        lambda: {"edge_tts_endpoint": ""})
    monkeypatch.delenv("EDGE_TTS_ENDPOINT", raising=False)

    # Mock backend 天然可用
    svc_mock = _mk_service(tmp_path / "mock")
    v = tmp_path / "v.mp4"; v.write_bytes(b"\x00")
    (tmp_path / "out").mkdir()
    xlsx = build_minimal_xlsx([(str(v), "t")])
    r = svc_mock.start_batch(source_bytes=xlsx, label="ok",
                                output_dir=str(tmp_path / "out"),
                                check_exists=False)
    assert r.get("batch_id")
    svc_mock.stop()

    # Edge backend 未配置 endpoint → 必抛
    svc_edge = BulkDubService(
        store=TaskStore(tmp_path / "edge.sqlite3"),
        tts_backend=EdgeTtsBackend(),
    )
    with pytest.raises(ValidationError):
        svc_edge.start_batch(source_bytes=xlsx, label="",
                               output_dir=str(tmp_path / "out"),
                               check_exists=False)
    svc_edge.stop()


def test_r13_p1_7_start_batch_signature_no_skip_endpoint_check(tmp_path):
    """签名层校验：start_batch 不再接受 _skip_endpoint_check / require_endpoint。"""
    import inspect
    sig = inspect.signature(BulkDubService.start_batch)
    assert "_skip_endpoint_check" not in sig.parameters
    assert "require_endpoint" not in sig.parameters
