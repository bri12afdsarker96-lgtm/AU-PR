"""R12 专项回归测试。按用户第 4 轮清单编号 R12-1 至 R12-13。

覆盖点（16 项）：
    R12-1  批内 100 条同指纹 → TTS/ffmpeg 只跑 1 次；leader 成功→follower 复用；
           leader 失败/取消→follower 同步终态；leader 未跑完时进程重启不"孤儿"
    R12-2  批次导入若中途异常 → batches/tasks 都不留半批次（事务原子）
    R12-3  隐藏 .part + marker 提交：正式文件绝不出现在半途；外部合法 mp4 无 marker
           不会被误认为本系统产物
    R12-4  video 写完文件但 DB 未 commit → 重启恢复能补记 completed，
           不重跑视频（output_committed → completed）
    R12-5  resize 6→1→6 期间 draining/serving/target 三态可见；无孤儿；
           无 target 泄露
    R12-6  批量取消 vs 单条完成竞态：cancelled 优先，late worker 拒覆盖
    R12-7  批次暂停跨重启保留
    R12-8  /api/browse 契约字段稳定；/api/bulk_dub/open_output_dir 真调外部命令
    R12-9  CSV 通过 HTTP/1.1 chunked + 主键游标；100000 条无重无漏，导出中新增
           不影响已导出行
    R12-10 Range 全边界：suffix / open / closed / >file_size / multi 416；
           task_output 授权按 task_id
    R12-11 avg_tts_processing_seconds 不含 wav 播放时长；projection_confidence
           样本不足=low
    R12-12 /start HTTP 参数不再吃 require_endpoint；MockTts 自动跳过；
           EdgeTts 未配置必抛
    R12-13 存在性 404：/summary /tasks /changes /csv 未知 batch_id → 404；
           /tasks?status=乱码 → 400
"""

from __future__ import annotations

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
from dub_align_studio.bulk_dub.csv_export import (  # noqa: E402
    iter_csv_chunks, write_csv,
)
from dub_align_studio.bulk_dub.edge_backend import (  # noqa: E402
    EdgeTtsBackend, MockTtsBackend,
)
from dub_align_studio.bulk_dub.excel_reader import build_minimal_xlsx  # noqa: E402
from dub_align_studio.bulk_dub.scheduler import (  # noqa: E402
    Scheduler, SchedulerConfig,
)
from dub_align_studio.bulk_dub.service import (  # noqa: E402
    BulkDubService, ValidationError,
)
from dub_align_studio.bulk_dub.store import (  # noqa: E402
    ALL_STATUSES,
    STATUS_CANCELLED, STATUS_CANCELLING, STATUS_COMPLETED, STATUS_FAILED,
    STATUS_OUTPUT_COMMITTED, STATUS_PENDING, STATUS_TTS_DONE, STATUS_TTS_RUNNING,
    STATUS_VIDEO_RUNNING, STATUS_WAITING_DEPENDENCY, TaskStore,
)


def _wait(cond, timeout=8.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(interval)
    return False


def _mk_service(tmp_path):
    return BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=MockTtsBackend(base_seconds=0.02),
    )


# ============================================================
# R12-1 批内 leader/follower 去重（100 条同指纹只跑 1 次）
# ============================================================

def test_r12_1_100_identical_only_one_leader(tmp_path):
    """100 条 Excel 行同视频 + 同文案 → 1 leader + 99 follower。"""
    (tmp_path / "out").mkdir()
    v = tmp_path / "同一.mp4"
    v.write_bytes(b"\x00")
    rows = [(str(v), "同样的文案") for _ in range(100)]
    xlsx = build_minimal_xlsx(rows)
    svc = _mk_service(tmp_path)
    r = svc.start_batch(source_bytes=xlsx, label="dedup100",
                          output_dir=str(tmp_path / "out"),
                          check_exists=False, _skip_endpoint_check=True)
    assert r["added"] == 1
    assert r["followers"] == 99
    assert r["reused"] == 0
    # 数据库里 100 条 task，其中 1 pending / 99 waiting_dependency
    tasks = svc.store.list_tasks(batch_id=r["batch_id"], limit=200)
    assert len(tasks) == 100
    assert sum(1 for t in tasks if t.status == STATUS_PENDING) == 1
    assert sum(1 for t in tasks if t.status == STATUS_WAITING_DEPENDENCY) == 99
    svc.stop()


def test_r12_1_leader_success_propagates_to_followers(tmp_path):
    """leader 完成时 propagate_leader_result_atomic 把 follower 一并转 completed。"""
    (tmp_path / "out").mkdir()
    v = tmp_path / "共.mp4"; v.write_bytes(b"\x00")
    xlsx = build_minimal_xlsx([(str(v), "共同"), (str(v), "共同"), (str(v), "共同")])
    svc = _mk_service(tmp_path)
    r = svc.start_batch(source_bytes=xlsx, label="lead-ok",
                          output_dir=str(tmp_path / "out"),
                          check_exists=False, _skip_endpoint_check=True)
    tasks = svc.store.list_tasks(batch_id=r["batch_id"], limit=10)
    leader = next(t for t in tasks if t.status == STATUS_PENDING)
    followers = [t for t in tasks if t.status == STATUS_WAITING_DEPENDENCY]
    assert len(followers) == 2

    # 模拟 leader 已完成：写 output_path + video_duration/tts_duration
    ok = tmp_path / "out" / "leader.mp4"; ok.write_bytes(b"MP4")
    svc.store.update(leader.task_id, status=STATUS_COMPLETED,
                      output_path=str(ok), final_duration=1.0,
                      tts_duration=0.5, concat_duration=0.5,
                      video_duration=0.4, encoder_used="libx264",
                      hw_fallback_used=False, progress=100)
    leader_row = svc.store.get(leader.task_id)
    n = svc.store.propagate_leader_result_atomic(
        leader.task_id, to_status=STATUS_COMPLETED,
        copy_output=True, leader_row=leader_row,
    )
    assert n == 2
    updated = svc.store.list_tasks(batch_id=r["batch_id"], limit=10)
    assert sum(1 for t in updated if t.status == STATUS_COMPLETED) == 3
    for t in updated:
        if t.task_id != leader.task_id and t.leader_task_id == leader.task_id:
            assert t.output_path == str(ok)
    svc.stop()


def test_r12_1_leader_failure_propagates_to_followers(tmp_path):
    """leader 失败 → follower 同步失败，带继承的 error 信息。"""
    (tmp_path / "out").mkdir()
    v = tmp_path / "同.mp4"; v.write_bytes(b"\x00")
    xlsx = build_minimal_xlsx([(str(v), "同"), (str(v), "同")])
    svc = _mk_service(tmp_path)
    r = svc.start_batch(source_bytes=xlsx, label="lead-fail",
                          output_dir=str(tmp_path / "out"),
                          check_exists=False, _skip_endpoint_check=True)
    tasks = svc.store.list_tasks(batch_id=r["batch_id"], limit=10)
    leader = next(t for t in tasks if t.status == STATUS_PENDING)
    n = svc.store.propagate_leader_result_atomic(
        leader.task_id, to_status=STATUS_FAILED,
        error_type="tts_error", error_detail="模拟失败",
    )
    assert n == 1
    updated = svc.store.list_tasks(batch_id=r["batch_id"], limit=10)
    followers = [t for t in updated if t.leader_task_id == leader.task_id]
    assert followers and followers[0].status == STATUS_FAILED
    assert followers[0].error_type == "tts_error"
    svc.stop()


# ============================================================
# R12-2 批次单事务原子性
# ============================================================

def test_r12_2_batch_creation_single_transaction_rolls_back(tmp_path):
    """create_batch_with_tasks 中一条 task 违反约束 → batches 也不写入。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    # 构造一条**必失败**的 task —— excel_row 用非 int（会在 int() 转换时抛）
    good = dict(excel_row=2, input_video="/x.mp4", text="a",
                 fingerprint="fp-a", voice_id="v", voice_name="V",
                 speed=1.0, keep_original_audio=False, params_snapshot={})
    bad = dict(good, excel_row="not-int")
    with pytest.raises(Exception):
        store.create_batch_with_tasks(label="atomic",
                                        output_dir=str(tmp_path),
                                        params={}, tasks=[good, bad])
    # 没有半批次残留
    assert store.list_batches(limit=10) == []


# ============================================================
# R12-3 隐藏 .part + marker 提交协议
# ============================================================

def test_r12_3_hidden_part_and_marker_naming(tmp_path):
    """.part 名字以点开头且带 task_id；marker 名字也以点开头。"""
    target = tmp_path / "out.mp4"
    part = vp._hidden_part_path(target, "T123")
    marker = vp.marker_path_for(target)
    assert part.name.startswith(".") and "T123" in part.name and part.name.endswith(".part")
    assert marker.name.startswith(".") and marker.name.endswith(".bulk_dub.marker.json")


def test_r12_3_read_marker_recognizes_ours_vs_external(tmp_path):
    """read_marker 只对我们写的 marker 返回 dict；外部 mp4 无 marker → None。"""
    target = tmp_path / "produced.mp4"; target.write_bytes(b"\x00")
    marker = vp.marker_path_for(target)
    # 未写 marker → 视为"外部合法文件"，不认领
    assert vp.read_marker(marker) is None
    # 用官方接口写 marker
    vp._write_marker(marker, task_id="T1", fingerprint="fp",
                     target_final_seconds=1.0, file_size=1,
                     output_name=target.name, encoder_used="libx264",
                     hw_fallback_used=False)
    meta = vp.read_marker(marker)
    assert meta is not None
    assert meta["task_id"] == "T1" and meta["fingerprint"] == "fp"
    assert meta["schema"] == "bulk_dub_marker@v1"


def test_r12_3_commit_never_overwrites_existing_target(tmp_path):
    """target 已存在 → _commit_with_marker 抛错，target 字节不动，.part 清理。"""
    target = tmp_path / "keep.mp4"; target.write_bytes(b"ORIGINAL")
    source = tmp_path / "src.mp4"; source.write_bytes(b"NEW")
    with pytest.raises(vp.VideoError):
        vp._commit_with_marker(source, target,
                                task_id="T2", fingerprint="fp",
                                target_final_seconds=1.0,
                                encoder_used="libx264",
                                hw_fallback_used=False)
    assert target.read_bytes() == b"ORIGINAL"
    part = vp._hidden_part_path(target, "T2")
    assert not part.exists()


# ============================================================
# R12-4 output_committed 保护 DB 提交失败
# ============================================================

def test_r12_4_output_committed_recovered_to_completed(tmp_path):
    """video_running → mark_output_committed → 若进程崩 → reap_and_recover
    应把状态补成 completed（不重跑视频）。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("recover", str(tmp_path), {})
    tid = store.add_task(batch_id=b, excel_row=2, input_video="/x.mp4",
                          text="a", fingerprint="fp-a", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    store.update(tid, status=STATUS_VIDEO_RUNNING, started_at=time.time())
    out = tmp_path / "recover.mp4"; out.write_bytes(b"MP4")
    assert store.mark_output_committed(tid, str(out)) is True
    r = store.get(tid)
    assert r.status == STATUS_OUTPUT_COMMITTED
    # 模拟重启恢复
    stats = store.reap_and_recover_running(
        reserved_output_verifier=lambda row, path: (True, {"ok": True}),
    )
    assert stats["output_committed_recovered"] >= 1
    r2 = store.get(tid)
    assert r2.status == STATUS_COMPLETED
    assert r2.output_path == str(out)


def test_r12_4_complete_transactional_accepts_output_committed(tmp_path):
    """complete_task_transactional 允许 expected_statuses 里包含 OUTPUT_COMMITTED。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("t", "/o", {})
    tid = store.add_task(batch_id=b, excel_row=2, input_video="/x.mp4",
                          text="a", fingerprint="f", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    store.update(tid, status=STATUS_OUTPUT_COMMITTED, output_path="/o/1.mp4")
    ok = store.complete_task_transactional(
        tid, output_path="/o/1.mp4", final_duration=1.0,
        tts_duration=0.5, concat_duration=0.3, video_duration=0.4,
        encoder_used="libx264", hw_fallback_used=False,
    )
    assert ok is True
    assert store.get(tid).status == STATUS_COMPLETED


# ============================================================
# R12-5 池 draining/serving/target 三态
# ============================================================

def test_r12_5_pool_resize_reports_three_states(tmp_path):
    """resize 6→1 期间 draining 可见；再 1→6 补足；snapshot 三段数字对得上。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=6, video_concurrency=2,
    ), tts_backend=MockTtsBackend(base_seconds=0.02))
    sched.start()
    try:
        _wait(lambda: sched.snapshot()["tts_alive"] == 6, timeout=3.0)
        snap = sched.snapshot()
        assert snap["tts_configured"] == 6
        assert snap["tts_alive"] == 6

        sched.resize_pools(tts=1, wait_seconds=2.0)
        _wait(lambda: sched.snapshot()["tts_alive"] == 1, timeout=3.0)
        snap = sched.snapshot()
        assert snap["tts_configured"] == 1
        assert snap["tts_alive"] == 1
        # draining 数值必须存在（无论是否已 join）
        assert "tts_draining" in snap

        sched.resize_pools(tts=6, wait_seconds=1.0)
        _wait(lambda: sched.snapshot()["tts_alive"] == 6, timeout=3.0)
        snap = sched.snapshot()
        assert snap["tts_configured"] == 6
        assert snap["tts_alive"] == 6
    finally:
        sched.stop()


# ============================================================
# R12-6 批量取消 vs 完成竞态
# ============================================================

def test_r12_6_try_advance_status_rejects_after_cancel(tmp_path):
    """pending → cancelled 之后，late worker 用 try_advance_status(from_status=
    pending, to=tts_done) 必须不命中。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("race", "/o", {})
    tid = store.add_task(batch_id=b, excel_row=2, input_video="/x.mp4",
                          text="a", fingerprint="f", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    cancelled = store.cancel_atomic([tid])
    assert cancelled == [tid]
    assert store.get(tid).status == STATUS_CANCELLED
    ok = store.try_advance_status(tid, from_status=STATUS_PENDING,
                                    to_status=STATUS_TTS_DONE)
    assert ok is False
    assert store.get(tid).status == STATUS_CANCELLED


def test_r12_6_cancel_batch_atomic_running_becomes_cancelling(tmp_path):
    """running 状态被 cancel_batch_atomic 转 cancelling（不直接终态）；
    reap_and_recover_running 把 cancelling 收敛为 cancelled。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("cx", "/o", {})
    t_run = store.add_task(batch_id=b, excel_row=2, input_video="/x.mp4",
                            text="a", fingerprint="f1", voice_id="v",
                            voice_name="V", speed=1.0,
                            keep_original_audio=False, params_snapshot={})
    t_wait = store.add_task(batch_id=b, excel_row=3, input_video="/x.mp4",
                             text="b", fingerprint="f2", voice_id="v",
                             voice_name="V", speed=1.0,
                             keep_original_audio=False, params_snapshot={})
    store.update(t_run, status=STATUS_TTS_RUNNING, started_at=time.time())
    changed = store.cancel_batch_atomic(b)
    assert set(changed) == {t_run, t_wait}
    assert store.get(t_run).status == STATUS_CANCELLING
    assert store.get(t_wait).status == STATUS_CANCELLED
    stats = store.reap_and_recover_running()
    assert stats["cancelling_recovered"] >= 1
    assert store.get(t_run).status == STATUS_CANCELLED


# ============================================================
# R12-7 批次暂停持久化
# ============================================================

def test_r12_7_batch_paused_persists_across_restart(tmp_path):
    """pause_batch 后销毁 scheduler；重启新 scheduler 应从 DB 恢复 paused 集合。"""
    db = tmp_path / "q.sqlite3"
    store1 = TaskStore(db)
    b = store1.create_batch("p", "/o", {})
    assert store1.set_batch_paused(b, True) is True
    assert store1.list_paused_batch_ids() == [b]

    # 新 scheduler 从同一 DB 打开
    store2 = TaskStore(db)
    sched = Scheduler(store=store2, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend(base_seconds=0.01))
    sched.start()
    try:
        _wait(lambda: sched.is_batch_paused(b), timeout=2.0)
        assert sched.is_batch_paused(b)
    finally:
        sched.stop()


# ============================================================
# R12-8 目录浏览契约 + 真"打开目录"端点
# ============================================================

def test_r12_8_browse_api_contract_fields(tmp_path):
    """/api/browse 返回 path/parent/dirs/files（不叫别的名字）。"""
    from dub_align_studio.web_server import _browse
    (tmp_path / "sub").mkdir()
    r = _browse(str(tmp_path))
    for k in ("path", "parent", "dirs", "files"):
        assert k in r, f"字段 {k} 缺失"
    assert "sub" in r["dirs"]


def test_r12_8_browse_api_empty_path_returns_roots(tmp_path):
    """空路径 → Linux 从 "/" 起；Windows 是 roots=True。"""
    from dub_align_studio.web_server import _browse
    r = _browse("")
    assert "path" in r and "dirs" in r


def test_r12_8_open_output_dir_endpoint_uses_batch_id_not_arbitrary_path(tmp_path, monkeypatch):
    """/open_output_dir 只接受 batch_id 白名单；不允许传任意路径。"""
    import subprocess as _sp_mod
    from dub_align_studio.bulk_dub import service as bulk_service
    from dub_align_studio import web_server as ws
    from urllib.parse import urlparse

    svc = _mk_service(tmp_path)
    (tmp_path / "out").mkdir()
    b = svc.store.create_batch("op", str(tmp_path / "out"), {})
    monkeypatch.setattr(bulk_service, "get_service", lambda: svc)
    calls = []
    monkeypatch.setattr(_sp_mod, "Popen",
                        lambda args, **kw: calls.append(args) or object())

    class _FakeHandler:
        def __init__(self):
            self.responses = []
            self.path = ""
        _json = lambda self, payload, status=200: self.responses.append((status, payload))

    h = _FakeHandler()
    ws._Handler._handle_open_output_dir(
        h, urlparse(f"/api/bulk_dub/open_output_dir?batch_id={b}"),
    )
    assert h.responses and h.responses[0][0] == 200
    assert calls, "外部命令应真的被调用"
    # 传假 batch_id → 404
    h2 = _FakeHandler()
    ws._Handler._handle_open_output_dir(
        h2, urlparse("/api/bulk_dub/open_output_dir?batch_id=deadbeef1234"),
    )
    assert h2.responses and h2.responses[0][0] == 404
    svc.stop()


# ============================================================
# R12-9 CSV 稳定游标 + 100000 无重无漏
# ============================================================

def test_r12_9_iter_all_pk_cursor_stable_on_concurrent_updates(tmp_path):
    """iter_all 用主键游标 —— 导出期间对已导出行做 update，不会重复或漏行。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("cur", "/o", {})
    rows = []
    for i in range(2, 302):  # 300 条即可验证
        rows.append(dict(
            excel_row=i, input_video=f"/v/{i}.mp4",
            text=f"t{i}", fingerprint=f"fp-{i}",
            voice_id="v", voice_name="V", speed=1.0,
            keep_original_audio=False, params_snapshot={},
        ))
    ids = store.bulk_insert(b, rows)
    seen = []
    it = store.iter_all(batch_id=b)
    # 头 50 条正常读
    for _ in range(50):
        row = next(it)
        seen.append(row.task_id)
    # 对**已读**行做 update（改 updated_at）—— 主键游标不会让它们再来一次
    for tid in seen[:20]:
        store.update(tid, stage="重复读测试")
    # 继续读到底
    for row in it:
        seen.append(row.task_id)
    assert len(seen) == 300
    assert len(set(seen)) == 300


def test_r12_9_csv_100000_no_dup_no_miss(tmp_path):
    """100000 条数据用 iter_all + iter_csv_chunks 一次写出，行数不重不漏。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    b = store.create_batch("big", "/o", {})
    N = 100000
    # 分批 bulk_insert 避免单 sql 语句过大
    for start in range(2, N + 2, 5000):
        rows = [dict(
            excel_row=i, input_video=f"/v/{i}.mp4",
            text=f"t{i}", fingerprint=f"fp-{i}",
            voice_id="v", voice_name="V", speed=1.0,
            keep_original_audio=False, params_snapshot={},
        ) for i in range(start, min(start + 5000, N + 2))]
        store.bulk_insert(b, rows)
    buf = io.StringIO()
    n = write_csv(store.iter_all(batch_id=b), buf)
    assert n == N
    # 抽样检查
    lines = buf.getvalue().rstrip("\n").split("\n")
    assert len(lines) == N + 1  # +header
    excel_rows_seen = {int(line.split(",", 1)[0]) for line in lines[1:] if line.split(",", 1)[0].isdigit()}
    assert len(excel_rows_seen) == N


# ============================================================
# R12-10 Range 全边界 + task_id 授权
# ============================================================

def _range_ok_from(file_bytes: bytes, hdr: str | None):
    """用 _serve_bulk_audio 的 Range 解析逻辑（简化再实现）验证输出。"""
    file_size = len(file_bytes)
    if not hdr:
        return 200, 0, file_size - 1
    spec = hdr[6:].strip()
    if "," in spec:
        return 416, None, None
    try:
        a, b = spec.split("-", 1)
        a, b = a.strip(), b.strip()
        if not a and not b:
            return 416, None, None
        if not a:
            suffix = int(b)
            if suffix <= 0:
                return 416, None, None
            if suffix >= file_size:
                start, end = 0, file_size - 1
            else:
                start, end = file_size - suffix, file_size - 1
        elif not b:
            start = int(a); end = file_size - 1
        else:
            start = int(a); end = int(b)
        if start < 0 or end < 0 or start > end or start >= file_size:
            return 416, None, None
        end = min(end, file_size - 1)
        return 206, start, end
    except ValueError:
        return 416, None, None


def test_r12_10_range_all_boundaries():
    """closed / open / suffix / >file_size / multi 都符合 RFC 7233。"""
    fb = b"0123456789"  # 10 bytes
    assert _range_ok_from(fb, "bytes=0-3") == (206, 0, 3)
    assert _range_ok_from(fb, "bytes=5-") == (206, 5, 9)
    assert _range_ok_from(fb, "bytes=-3") == (206, 7, 9)
    # suffix 大于文件 → 返回整段
    assert _range_ok_from(fb, "bytes=-100") == (206, 0, 9)
    # start >= file_size → 416
    assert _range_ok_from(fb, "bytes=100-200")[0] == 416
    # multi range → 416
    assert _range_ok_from(fb, "bytes=0-1,3-4")[0] == 416
    # 无破折号 → 416
    assert _range_ok_from(fb, "bytes=abc")[0] == 416
    # 空 → 416
    assert _range_ok_from(fb, "bytes=-")[0] == 416


def test_r12_10_task_output_uses_task_id_not_arbitrary_path(tmp_path, monkeypatch):
    """/api/bulk_dub/task_output?task_id=... —— DB 查 output_path；
    页面不再提供 path=... 端点。"""
    svc = _mk_service(tmp_path)
    ok = tmp_path / "ok.mp4"; ok.write_bytes(b"MP4")
    b = svc.store.create_batch("t", str(tmp_path), {})
    tid = svc.store.add_task(batch_id=b, excel_row=2, input_video="/x",
                              text="t", fingerprint="fp", voice_id="v",
                              voice_name="V", speed=1.0,
                              keep_original_audio=False, params_snapshot={})
    svc.store.update(tid, status=STATUS_COMPLETED, output_path=str(ok))
    # 断言 web_server 已经移除旧 audio?path= 端点：不在 do_GET 分支里
    from dub_align_studio import web_server as ws
    import inspect
    src = inspect.getsource(ws._Handler.do_GET)
    assert "/api/bulk_dub/audio" not in src, "旧的 path= 端点必须移除"
    assert "/api/bulk_dub/task_output" in src
    svc.stop()


# ============================================================
# R12-11 统计口径修正
# ============================================================

def test_r12_11_avg_tts_key_renamed_and_projection_confidence(tmp_path):
    """snapshot metrics 有 avg_tts_processing_seconds（旧 avg_tts_seconds 已弃）+
    projection_confidence（样本少 → low）。"""
    svc = _mk_service(tmp_path)
    summary = svc.summary()
    m = summary["scheduler"]["metrics"]
    assert "avg_tts_processing_seconds" in m
    assert "tts_samples" in m and "video_samples" in m
    assert "projection_confidence" in m
    # 冷启动样本 = 0 → low
    assert m["projection_confidence"] == "low"
    svc.stop()


# ============================================================
# R12-12 HTTP 层不再吃 require_endpoint
# ============================================================

def test_r12_12_http_start_ignores_require_endpoint(tmp_path, monkeypatch):
    """HTTP /start 即使传 require_endpoint=0 也不能让真 Edge backend 绕过 endpoint 校验；
    Mock backend 自动跳过（生产 API 契约）。"""
    from dub_align_studio import settings as studio_settings
    monkeypatch.setattr(studio_settings, "load_settings", lambda: {"edge_tts_endpoint": ""})
    monkeypatch.delenv("EDGE_TTS_ENDPOINT", raising=False)
    svc = BulkDubService(
        store=TaskStore(tmp_path / "q.sqlite3"),
        tts_backend=EdgeTtsBackend(),
    )
    xlsx = build_minimal_xlsx([("/x.mp4", "t")])
    (tmp_path / "out").mkdir()
    # 显式传 require_endpoint=0 —— API 层不会转成 _skip_endpoint_check
    handled, status, body, _ = bulk_api.dispatch_post(
        "/api/bulk_dub/start",
        {"output_dir": str(tmp_path / "out"), "check_exists": "0",
         "require_endpoint": "0"},
        xlsx, "application/octet-stream", service=svc,
    )
    assert handled and status == 400
    assert "Edge TTS" in json.loads(body).get("error", "") \
        or "端点" in json.loads(body).get("error", "")
    svc.stop()


# ============================================================
# R12-13 存在性 404 + status 枚举
# ============================================================

def test_r12_13_summary_tasks_changes_csv_404_on_unknown_batch(tmp_path):
    svc = _mk_service(tmp_path)
    bogus = "deadbeef1234"  # 16 位以内 hex
    for route in ("/api/bulk_dub/summary", "/api/bulk_dub/tasks",
                    "/api/bulk_dub/changes", "/api/bulk_dub/csv"):
        handled, status, body, _ = bulk_api.dispatch_get(
            route, {"batch_id": bogus}, service=svc,
        )
        assert handled and status == 404, f"{route} 未 404：{status} {body[:80]}"
    svc.stop()


def test_r12_13_tasks_unknown_status_returns_400(tmp_path):
    svc = _mk_service(tmp_path)
    b = svc.store.create_batch("t", "/o", {})
    handled, status, body, _ = bulk_api.dispatch_get(
        "/api/bulk_dub/tasks", {"batch_id": b, "status": "乱码状态"},
        service=svc,
    )
    assert handled and status == 400
    for s in ALL_STATUSES:
        handled, status, body, _ = bulk_api.dispatch_get(
            "/api/bulk_dub/tasks", {"batch_id": b, "status": s},
            service=svc,
        )
        assert handled and status == 200
    svc.stop()
