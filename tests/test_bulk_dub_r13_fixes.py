"""R13 复审修复回归测试（本窗口新增）。

覆盖三条本窗口发现的 R13 缺陷：
    R13-FIX-P0-A  leader 在 worker 早期检测到 cancel_flag 时必须同事务广播
                    follower cancelled；不能靠 scheduler 重启对账兜底
    R13-FIX-P0-B  _process_video 早期 cancel 与 VideoCancelled 同样广播
    R13-FIX-P1-A  marker 从 target 旁边 sidecar 迁到目标目录内受控子目录
                    `.bulk_dub_markers/<name>.marker.json`；恢复兼容旧 sidecar
    R13-FIX-P1-B  跨批次 fingerprint 复用 hardlink 到本目录时，同步写 marker
                    以补齐 ownership metadata
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub import ffmpeg_pipeline as vp  # noqa: E402
from dub_align_studio.bulk_dub.edge_backend import MockTtsBackend  # noqa: E402
from dub_align_studio.bulk_dub.excel_reader import build_minimal_xlsx  # noqa: E402
from dub_align_studio.bulk_dub.scheduler import (  # noqa: E402
    Scheduler, SchedulerConfig,
)
from dub_align_studio.bulk_dub.service import BulkDubService  # noqa: E402
from dub_align_studio.bulk_dub.store import (  # noqa: E402
    STATUS_CANCELLED, STATUS_CANCELLING, STATUS_COMPLETED, STATUS_PENDING,
    STATUS_TTS_RUNNING, STATUS_VIDEO_RUNNING, STATUS_WAITING_DEPENDENCY,
    TaskStore,
)


def _mk_store(tmp_path):
    return TaskStore(tmp_path / "q.sqlite3")


def _mk_leader_and_followers(store, batch_id, fp="fpX"):
    lid = store.add_task(batch_id=batch_id, excel_row=2, input_video="/v.mp4",
                          text="t", fingerprint=fp, voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False, params_snapshot={})
    fid1 = store.add_task(batch_id=batch_id, excel_row=3, input_video="/v.mp4",
                           text="t", fingerprint=fp, voice_id="v",
                           voice_name="V", speed=1.0,
                           keep_original_audio=False, params_snapshot={})
    fid2 = store.add_task(batch_id=batch_id, excel_row=4, input_video="/v.mp4",
                           text="t", fingerprint=fp, voice_id="v",
                           voice_name="V", speed=1.0,
                           keep_original_audio=False, params_snapshot={})
    store.update(fid1, status=STATUS_WAITING_DEPENDENCY, leader_task_id=lid)
    store.update(fid2, status=STATUS_WAITING_DEPENDENCY, leader_task_id=lid)
    return lid, fid1, fid2


# ============================================================
# R13-FIX-P0-C  finalize_leader_cancel 参数占位符数量必须匹配
# ============================================================

def test_r13_fix_finalize_leader_cancel_actually_updates(tmp_path):
    """原 R13 代码 IN (?,?,?,?,?,?,?,?) 8 个占位符对 7 个状态值 —— sqlite
    抛 ProgrammingError，被上层静默吞。本 fix 后必须真正把 leader 从
    cancelling 收敛到 cancelled，同事务广播 follower。"""
    store = _mk_store(tmp_path)
    b = store.create_batch("t", str(tmp_path), {})
    lid, fid1, fid2 = _mk_leader_and_followers(store, b)
    store.update(lid, status=STATUS_CANCELLING)
    ok, aff = store.finalize_leader_cancel(lid, error_type="cancelled",
                                             error_detail="test")
    assert ok is True, "finalize_leader_cancel 应真正命中 leader"
    assert store.get(lid).status == STATUS_CANCELLED
    assert aff == 2
    assert store.get(fid1).status == STATUS_CANCELLED
    assert store.get(fid2).status == STATUS_CANCELLED


# ============================================================
# R13-FIX-P0-A  _process_tts 早期 cancel 必须广播 follower
# ============================================================

def test_r13_fix_tts_early_cancel_broadcasts_follower(tmp_path):
    """leader 在 TTS worker 里检测到 cancel_flag 时，必须**同事务**把
    waiting_dependency follower 也收敛到 cancelled；不能留给下次 reap 对账。"""
    store = _mk_store(tmp_path)
    b = store.create_batch("t", str(tmp_path), {})
    lid, fid1, fid2 = _mk_leader_and_followers(store, b)
    # 模拟用户在 cancel_task 里推 running → cancelling
    store.update(lid, status=STATUS_TTS_RUNNING, staging_dir=str(tmp_path / "s"))
    (tmp_path / "s").mkdir(exist_ok=True)
    store.update(lid, status=STATUS_CANCELLING)
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    # 触发 cancel_flag（cancel_task 会做，这里直接置位）
    sched._ensure_cancel_flag(lid).set()
    row = store.get(lid)
    # 直接调用 _process_tts —— worker 进入时看到 flag，走早期 cancel 路径
    sched._process_tts(row)
    assert store.get(lid).status == STATUS_CANCELLED, \
        "leader 应被 worker 收敛到 cancelled"
    assert store.get(fid1).status == STATUS_CANCELLED, \
        "follower1 必须**立即**被广播为 cancelled"
    assert store.get(fid2).status == STATUS_CANCELLED, \
        "follower2 必须**立即**被广播为 cancelled"


# ============================================================
# R13-FIX-P0-B  _process_video 早期 cancel 与渲染中 cancel 广播 follower
# ============================================================

def test_r13_fix_video_early_cancel_broadcasts_follower(tmp_path):
    store = _mk_store(tmp_path)
    b = store.create_batch("t", str(tmp_path), {})
    lid, fid1, fid2 = _mk_leader_and_followers(store, b)
    store.update(lid, status=STATUS_VIDEO_RUNNING,
                  staging_dir=str(tmp_path / "s"),
                  params_snapshot={"output_dir": str(tmp_path / "out")})
    (tmp_path / "s").mkdir(exist_ok=True)
    (tmp_path / "out").mkdir(exist_ok=True)
    store.update(lid, status=STATUS_CANCELLING)
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched._ensure_cancel_flag(lid).set()
    row = store.get(lid)
    sched._process_video(row)
    assert store.get(lid).status == STATUS_CANCELLED
    assert store.get(fid1).status == STATUS_CANCELLED, \
        "视频 worker 早期 cancel 必须广播 follower"
    assert store.get(fid2).status == STATUS_CANCELLED


# ============================================================
# R13-FIX-P1-A  marker 目录化 —— 目标目录下 `.bulk_dub_markers/<name>.marker.json`
# ============================================================

def test_r13_fix_marker_lives_in_controlled_subdir(tmp_path):
    """marker 不再作为可见 sidecar 散落在目标 mp4 旁边，而是放进
    受控子目录 `<output_dir>/.bulk_dub_markers/`。"""
    target = tmp_path / "video.mp4"
    marker = vp.marker_path_for(target)
    # 新契约：marker 位于目标目录的 `.bulk_dub_markers/` 子目录内
    assert marker.parent.name == ".bulk_dub_markers", \
        f"marker 必须在 `.bulk_dub_markers` 子目录内，实际 parent={marker.parent}"
    assert marker.parent.parent == target.parent, \
        "marker 子目录必须位于 target 所在目录下"
    assert marker.name == "video.mp4.marker.json"
    # 断言 target 目录里不再有形如 `.video.mp4.bulk_dub.marker.json` 的可见 sidecar
    # （即使写入 marker 之后，也不应产生该文件）
    marker.parent.mkdir(parents=True, exist_ok=True)
    vp._write_marker(marker, task_id="T", batch_id="B", fingerprint="fp",
                      target_final_seconds=1.0, file_size=10,
                      output_name=target.name, encoder_used="libx264",
                      hw_fallback_used=False)
    stray = target.parent / f".{target.name}.bulk_dub.marker.json"
    assert not stray.exists(), f"不允许产生旧式 sidecar：{stray}"


def test_r13_fix_read_marker_falls_back_to_legacy_sidecar(tmp_path):
    """R13-FIX-P1-A 兼容：老批次成片旁边还有旧 sidecar `.name.bulk_dub.marker.json`
    时，恢复逻辑必须能读到，避免误清理历史成片。"""
    target = tmp_path / "old.mp4"; target.write_bytes(b"\x00" * 2048)
    # 手工写入旧式 sidecar（模拟老版本产物）
    legacy = target.parent / f".{target.name}.bulk_dub.marker.json"
    vp._write_marker(legacy, task_id="T-legacy", batch_id="B",
                      fingerprint="fp", target_final_seconds=1.0,
                      file_size=target.stat().st_size,
                      output_name=target.name,
                      encoder_used="libx264", hw_fallback_used=False)
    got = vp.read_marker_for_target(target)
    assert got is not None, "必须能读到旧 sidecar 作为兼容"
    assert got.get("task_id") == "T-legacy"


# ============================================================
# R13-FIX-P1-B  跨批次 fingerprint 复用 hardlink 时同步写 marker
# ============================================================

def _ffprobe_ok() -> bool:
    try:
        subprocess.run(["ffprobe", "-version"], check=True,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def test_r13_fix_cross_dir_reuse_writes_marker_for_new_task(tmp_path):
    """R13-P1-6 hardlink 到新 output_dir 后必须写 marker，让新 task 也有
    ownership metadata（否则未来的恢复/校验找不到归属）。"""
    old_dir = tmp_path / "old"; old_dir.mkdir()
    new_dir = tmp_path / "new"; new_dir.mkdir()
    # 造一个"已完成"的老成片和对应 marker（老批次）
    old_mp4 = old_dir / "vid_A.mp4"; old_mp4.write_bytes(b"\x00" * 2048)
    old_marker = vp.marker_path_for(old_mp4)
    old_marker.parent.mkdir(parents=True, exist_ok=True)
    vp._write_marker(old_marker, task_id="T-old", batch_id="B-old",
                      fingerprint="fp1",
                      target_final_seconds=1.0, file_size=2048,
                      output_name=old_mp4.name, encoder_used="libx264",
                      hw_fallback_used=False)
    store = _mk_store(tmp_path)
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    # 老批次先入库一条 completed（提供 fingerprint 复用源）
    b_old = store.create_batch("old", str(old_dir), {})
    tid_old = store.add_task(batch_id=b_old, excel_row=2, input_video="/v",
                              text="t", fingerprint="fp1", voice_id="v",
                              voice_name="V", speed=1.0,
                              keep_original_audio=False, params_snapshot={})
    store.update(tid_old, status=STATUS_COMPLETED,
                  output_path=str(old_mp4),
                  video_duration=1.0, tts_duration=1.0,
                  concat_duration=1.0, final_duration=1.0,
                  encoder_used="libx264", hw_fallback_used=0)
    # 新批次：同一 fingerprint 但指定新 output_dir
    v = tmp_path / "input.mp4"; v.write_bytes(b"\x00")
    xlsx = build_minimal_xlsx([(str(v), "t")])
    # 手工设 fingerprint —— 不能靠 mock，因为 fingerprint 依赖于文件+文本。
    # 我们通过直接在 store 上 create + reused 路径断言 marker 写入。
    # 用 service.start_batch 走完整流程，check_exists=False，
    # 让复用逻辑基于 preview.rows fingerprint == 老 fp1（我们在这里靠 monkeypatch）
    import dub_align_studio.bulk_dub.service as svc_mod
    real_cf = svc_mod.compute_fingerprint
    svc_mod.compute_fingerprint = lambda **kw: "fp1"
    try:
        r = svc.start_batch(source_bytes=xlsx, label="new",
                             output_dir=str(new_dir),
                             check_exists=False)
    finally:
        svc_mod.compute_fingerprint = real_cf
    # 复用命中 → 新批次的 dest 必须是 new_dir/vid_A.mp4，且有 marker
    dest = new_dir / old_mp4.name
    assert dest.exists(), "hardlink 未落地到新 output_dir"
    new_marker = vp.marker_path_for(dest)
    assert new_marker.exists(), \
        "跨目录复用 hardlink 后必须写 marker 携带新任务 ownership"
    got = vp.read_marker(new_marker)
    assert got is not None
    assert got.get("batch_id") == r["batch_id"], \
        "复用 marker 的 batch_id 应指向新批次"
    svc.stop()
