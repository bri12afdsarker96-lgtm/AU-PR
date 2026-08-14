"""R14-FIX 生产集成回归测试。

所有测试都调用**生产控制路径**：Scheduler coordinator、_process_video、
render_single、apply_gpu_profile、benchmark 服务、真实 API Handler。
禁止只验证 controller 的孤立返回值。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub import (  # noqa: E402
    api as bulk_api, ffmpeg_pipeline as vp, gpu_profile,
)
from dub_align_studio.bulk_dub.concurrency_controller import (  # noqa: E402
    ALL_MODES, MAX_VIDEO_CONCURRENCY, MODE_AUTO, MODE_CPU_SAFE, MODE_MANUAL,
    CpuFallbackGate, EncoderCircuitBreaker, GateCancelled,
    RESIZE_MIN_INTERVAL_S,
)
from dub_align_studio.bulk_dub.edge_backend import MockTtsBackend  # noqa: E402
from dub_align_studio.bulk_dub.hw_encoder import EncoderProbe  # noqa: E402
from dub_align_studio.bulk_dub.scheduler import (  # noqa: E402
    Scheduler, SchedulerConfig,
)
from dub_align_studio.bulk_dub.service import (  # noqa: E402
    BulkDubService, ValidationError,
)
from dub_align_studio.bulk_dub.store import TaskStore  # noqa: E402
from dub_align_studio.bulk_dub.store import (  # noqa: E402
    STATUS_TTS_DONE,
)


def _has_ffmpeg() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=True, timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


def _make_real_mp4(target: Path, seconds: float = 1.0) -> None:
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"testsrc=size=320x240:duration={seconds}:rate=15",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-t", str(seconds),
        "-c:a", "aac", str(target),
    ], check=True)


# ================================================================
# P0-1 生产 coordinator：AUTO 真 resize、无泄漏、stop/start
# ================================================================


def test_r14fix_p0_1_coordinator_scales_up_with_profile_and_backlog(tmp_path):
    """profile 推荐 6 + tts_done backlog>0 → coordinator 真把视频池扩到 6。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched.controller.set_profile_recommended(6)
    sched.controller.set_mode(MODE_AUTO)
    # 构造 tts_done backlog
    b = store.create_batch("t", str(tmp_path), {})
    for i in range(5):
        tid = store.add_task(batch_id=b, excel_row=2 + i,
                              input_video="/v", text=f"t{i}", fingerprint=f"fp{i}",
                              voice_id="v", voice_name="V", speed=1.0,
                              keep_original_audio=False, params_snapshot={})
        store.update(tid, status=STATUS_TTS_DONE, tts_duration=1.0,
                       staging_dir=str(tmp_path / f"s{i}"))
    sched._coordinator_interval = 0.2
    sched.start()
    try:
        # 等 coordinator 决策周期
        deadline = time.time() + 8
        while time.time() < deadline:
            snap = sched.snapshot()
            if snap["video_alive"] >= 6:
                break
            time.sleep(0.2)
        snap = sched.snapshot()
        assert snap["video_alive"] == 6, \
            f"coordinator 应真把视频池扩到 6，实际 {snap['video_alive']}"
        # coordinator 事件历史里有此次 resize
        evs = sched.coordinator_events()
        assert any(e.get("target") == 6 for e in evs), \
            f"coordinator 事件必须记录 target=6，实际 {evs}"
    finally:
        sched.stop(wait_seconds=3)


def test_r14fix_p0_1_coordinator_no_backlog_no_scaleup(tmp_path):
    """profile=6 但无 backlog → coordinator 不扩容（保守）。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched.controller.set_profile_recommended(6)
    sched.controller.set_mode(MODE_AUTO)
    sched._coordinator_interval = 0.2
    sched.start()
    try:
        time.sleep(2.0)
        snap = sched.snapshot()
        # 无 backlog → 保持 1
        assert snap["video_alive"] <= 1, \
            f"无 backlog 时不应扩容，实际 {snap['video_alive']}"
    finally:
        sched.stop(wait_seconds=3)


def test_r14fix_p0_1_coordinator_disk_low_blocks_scaleup(tmp_path, monkeypatch):
    """磁盘紧张 → coordinator 不扩容。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched.controller.set_profile_recommended(6)
    sched.controller.set_mode(MODE_AUTO)
    # 构造 backlog + 假装最活跃 output_dir 磁盘紧张
    b = store.create_batch("t", str(tmp_path), {})
    for i in range(3):
        tid = store.add_task(batch_id=b, excel_row=2 + i,
                              input_video="/v", text=f"t{i}", fingerprint=f"fp{i}",
                              voice_id="v", voice_name="V", speed=1.0,
                              keep_original_audio=False, params_snapshot={})
        store.update(tid, status=STATUS_TTS_DONE, tts_duration=1.0)
    # monkeypatch 磁盘查询返回 0.5 GB
    monkeypatch.setattr(sched, "_min_output_free_gb", lambda: 0.5)
    sched._coordinator_interval = 0.2
    # 先 resize 到 2 让 last_effective_concurrency=2；等冷却过后 backlog 大也扩不了
    sched.start()
    try:
        # 让 coordinator 跑几轮
        time.sleep(2.5)
        snap = sched.snapshot()
        assert snap["video_alive"] <= 2, \
            f"磁盘紧张不应扩容，实际 {snap['video_alive']}"
    finally:
        sched.stop(wait_seconds=3)


def test_r14fix_p0_1_coordinator_stop_start_no_leak(tmp_path):
    """stop → 无 coordinator 泄漏；再 start → 只有一个 coordinator。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched.start()
    t1 = sched._coordinator_thread
    assert t1 is not None and t1.is_alive()
    ok = sched.stop(wait_seconds=3)
    assert ok
    # coordinator 也必须真死
    time.sleep(0.2)
    assert sched._coordinator_thread is None or not sched._coordinator_thread.is_alive()
    # 再 start → 复用/新建单个 coordinator
    sched.start()
    t2 = sched._coordinator_thread
    assert t2 is not None and t2.is_alive()
    assert t2 is not t1
    sched.stop(wait_seconds=3)


def test_r14fix_p0_1_hardware_failures_trigger_ramp_down_via_coordinator(tmp_path):
    """连续硬件失败 → coordinator 真调 resize 降档。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=4,
    ), tts_backend=MockTtsBackend())
    sched.controller.set_profile_recommended(4)
    sched.controller.set_mode(MODE_AUTO)
    sched._coordinator_interval = 0.2
    sched.start()
    try:
        # 起始 serving=4
        deadline = time.time() + 3
        while time.time() < deadline and sched.snapshot()["video_alive"] != 4:
            time.sleep(0.1)
        # 记入 3 次硬件失败
        for _ in range(3):
            sched.controller.record_hardware_failure("h264_nvenc")
        # 等 coordinator 冷却 8s 过后触发降档
        deadline = time.time() + 12
        while time.time() < deadline:
            snap = sched.snapshot()
            if snap["video_alive"] < 4:
                break
            time.sleep(0.3)
        snap = sched.snapshot()
        assert snap["video_alive"] < 4, \
            f"硬件失败必须触发降档，实际仍是 {snap['video_alive']}"
    finally:
        sched.stop(wait_seconds=3)


# ================================================================
# P0-2 CPU_SAFE 强制 libx264 — 断言传给 render_single 的 encoder
# ================================================================


def test_r14fix_p0_2_cpu_safe_forces_libx264_encoder(tmp_path, monkeypatch):
    """CPU_SAFE 下 _process_video 传给 render_single 的 encoder 必须是 libx264。"""
    captured: list[str] = []

    def _fake_render_single(**kw):
        captured.append(kw["encoder"].encoder)
        raise vp.VideoError("fake render abort")

    monkeypatch.setattr(vp, "render_single", _fake_render_single)

    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched.set_encoder_override("libx264")

    # 构造 tts_done + staging + tts wav（假 wav）+ 让 _process_video 进入到 render_single
    b = store.create_batch("t", str(tmp_path), {})
    v = tmp_path / "in.mp4"; _make_real_mp4(v, seconds=1.0) if _has_ffmpeg() else v.write_bytes(b"\x00" * 4096)
    if not _has_ffmpeg():
        pytest.skip("需要 ffprobe")
    outdir = tmp_path / "out"; outdir.mkdir()
    staging = tmp_path / "s"; staging.mkdir()
    tts_wav = staging / "tts.wav"
    gpu_profile.make_silent_wav(tts_wav, seconds=1.0)

    tid = store.add_task(batch_id=b, excel_row=2, input_video=str(v),
                          text="t", fingerprint="fp", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False,
                          params_snapshot={
                              "output_dir": str(outdir),
                              "encoder_preference": "auto",   # 用户偏好 auto
                              "zoom_percent": 130,
                              "preset": "ultrafast",
                              "crf": 28,
                              "ffmpeg_timeout": 30.0,
                          })
    from dub_align_studio.bulk_dub.store import STATUS_VIDEO_RUNNING
    store.update(tid, status=STATUS_VIDEO_RUNNING,
                  staging_dir=str(staging), tts_duration=1.0)
    row = store.get(tid)
    sched._process_video(row)
    assert captured == ["libx264"], \
        f"CPU_SAFE override 时必须强制 libx264，实际 {captured}"


def test_r14fix_p0_2_no_override_uses_user_preference(tmp_path, monkeypatch):
    """无 override 时使用任务冻结的 encoder_preference（→ resolve_encoder 结果）。"""
    if not _has_ffmpeg():
        pytest.skip("需要 ffprobe")
    captured: list[str] = []

    def _fake_render_single(**kw):
        captured.append(kw["encoder"].encoder)
        raise vp.VideoError("fake")

    monkeypatch.setattr(vp, "render_single", _fake_render_single)

    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    # 没有 override → 走 _get_encoder("cpu") → libx264
    b = store.create_batch("t", str(tmp_path), {})
    v = tmp_path / "in.mp4"; _make_real_mp4(v)
    outdir = tmp_path / "out"; outdir.mkdir()
    staging = tmp_path / "s"; staging.mkdir()
    gpu_profile.make_silent_wav(staging / "tts.wav", seconds=1.0)
    tid = store.add_task(batch_id=b, excel_row=2, input_video=str(v),
                          text="t", fingerprint="fp", voice_id="v",
                          voice_name="V", speed=1.0,
                          keep_original_audio=False,
                          params_snapshot={
                              "output_dir": str(outdir),
                              "encoder_preference": "cpu",
                              "zoom_percent": 130,
                              "preset": "ultrafast",
                              "crf": 28,
                              "ffmpeg_timeout": 30.0,
                          })
    from dub_align_studio.bulk_dub.store import STATUS_VIDEO_RUNNING
    store.update(tid, status=STATUS_VIDEO_RUNNING,
                  staging_dir=str(staging), tts_duration=1.0)
    sched._process_video(store.get(tid))
    assert captured and captured[0] == "libx264"


# ================================================================
# P0-3 gate/breaker：cancel during acquire, breaker cancel record
# ================================================================


def test_r14fix_p0_3_gate_acquire_cancels_via_predicate():
    gate = CpuFallbackGate(limit=1)
    gate.acquire()   # 占用唯一 permit
    # 后台线程尝试 acquire，5ms 后触发 cancel
    cancel = threading.Event()
    got: list[bool] = []
    err: list[Exception] = []

    def _try():
        try:
            got.append(gate.acquire(is_cancelled=lambda: cancel.is_set(),
                                      poll_interval=0.05))
        except GateCancelled as e:
            err.append(e)
        except Exception as e:  # noqa: BLE001
            err.append(e)

    t = threading.Thread(target=_try, daemon=True); t.start()
    time.sleep(0.15)
    cancel.set()
    t.join(timeout=2)
    assert not got, "cancel 之后不应该获得 permit"
    assert err and isinstance(err[0], GateCancelled), \
        f"cancel 必须抛 GateCancelled，实际 {err}"


def test_r14fix_p0_3_breaker_cancel_releases_halfopen_probe():
    """half-open 探测被取消 → probe 名额归还，其他任务可以进来。"""
    b = EncoderCircuitBreaker("h264_nvenc", failure_threshold=2,
                                open_seconds=0.05)
    b.record_failure(); b.record_failure()
    assert b.is_open()
    time.sleep(0.1)
    # 现在半开
    assert b.state == EncoderCircuitBreaker.STATE_HALF_OPEN
    assert b.allow() is True         # 拿到 probe
    assert b.allow() is False        # 无第二个 probe
    b.record_cancel()               # 归还
    assert b.allow() is True         # 又能拿了


def test_r14fix_p0_3_cpu_fallback_failure_counted_separately():
    """CPU fallback 失败**不**被硬件 success 清零。"""
    from dub_align_studio.bulk_dub.concurrency_controller import ConcurrencyController
    c = ConcurrencyController(profile_recommended=2)
    c.record_cpu_fallback_failure()
    c.record_cpu_fallback_failure()
    assert c.cpu_fallback_failure_count() == 2
    # 硬件成功 → 不能清零 CPU fallback 失败
    c.record_hardware_success("h264_nvenc")
    assert c.cpu_fallback_failure_count() == 2


def test_r14fix_p0_3_breaker_open_forces_coordinator_downscale(tmp_path):
    """有 encoder breaker 打开时，coordinator 必须降到 1。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=4,
    ), tts_backend=MockTtsBackend())
    sched.controller.set_profile_recommended(4)
    sched.controller.set_mode(MODE_AUTO)
    # 打开 h264_nvenc breaker
    b = sched.controller.breaker_for("h264_nvenc")
    for _ in range(5):
        b.record_failure()
    assert b.is_open()
    sched._coordinator_interval = 0.2
    sched.start()
    try:
        deadline = time.time() + 12
        while time.time() < deadline:
            snap = sched.snapshot()
            if snap["video_alive"] <= 1:
                break
            time.sleep(0.3)
        assert sched.snapshot()["video_alive"] == 1, \
            "breaker 打开 → coordinator 必须降到 1"
    finally:
        sched.stop(wait_seconds=3)


# ================================================================
# P0-4 上限统一到 16
# ================================================================


def test_r14fix_p0_4_service_resize_pools_accepts_video_16(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        r = svc.resize_pools(video=16)
        assert r["after"]["video_alive"] == 16
        # 17 → 拒绝
        with pytest.raises(ValidationError):
            svc.resize_pools(video=17)
    finally:
        svc.stop()


def test_r14fix_p0_4_benchmark_ladder_top_matches_max(tmp_path):
    from dub_align_studio.bulk_dub.gpu_profile import DEFAULT_LADDER
    assert max(DEFAULT_LADDER) == MAX_VIDEO_CONCURRENCY, \
        "benchmark ladder 顶点必须等于全局并发上限，避免推荐 16 实际 8"


# ================================================================
# P0-5 profile 应用需指纹校验；持久化恢复
# ================================================================


def test_r14fix_p0_5_apply_rejects_when_fingerprint_mismatch(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        # 写一个"陈旧" profile，指纹与当前设备不匹配
        stale = gpu_profile.DeviceCapability(
            device_fingerprint="stale-fp-1234",
            encoder_family="cpu", encoder_name="libx264",
            recommended_concurrency=4,
        )
        gpu_profile.save_profile(stale)
        svc._gpu_profile = stale
        with pytest.raises(ValidationError) as ei:
            svc.apply_gpu_profile()
        assert "指纹" in str(ei.value) or "已失效" in str(ei.value)
    finally:
        svc.stop()


def test_r14fix_p0_5_apply_succeeds_when_fingerprint_matches(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        ffmpeg = ss.ffmpeg_tool("ffmpeg")
        fresh = gpu_profile.detect_capability_metadata(ffmpeg)
        # profile 指纹 = 当前设备指纹
        prof = gpu_profile.DeviceCapability(
            device_fingerprint=fresh.device_fingerprint,
            encoder_family=fresh.encoder_family,
            encoder_name=fresh.encoder_name,
            recommended_concurrency=3,
        )
        gpu_profile.save_profile(prof)
        svc._gpu_profile = prof
        r = svc.apply_gpu_profile()
        assert r["applied_video_concurrency"] == 3
    finally:
        svc.stop()


def test_r14fix_p0_5_state_persistence_survives_restart(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    # 第一次：切到 CPU_SAFE + user_max=3
    store1 = TaskStore(tmp_path / "q.sqlite3")
    svc1 = BulkDubService(store=store1, tts_backend=MockTtsBackend())
    try:
        svc1.set_concurrency_mode(MODE_CPU_SAFE, user_max=3)
    finally:
        svc1.stop()
    # 第二次：新 svc → 应该从 gpu_state.json 恢复 mode/user_max
    store2 = TaskStore(tmp_path / "q.sqlite3")
    svc2 = BulkDubService(store=store2, tts_backend=MockTtsBackend())
    try:
        # 触发 _ensure_scheduler_for_controller → 恢复状态
        r = svc2.gpu_capability()
        ctl = svc2._ensure_scheduler_for_controller().controller.snapshot()
        assert ctl["mode"] == MODE_CPU_SAFE
        assert ctl["user_max"] == 3
    finally:
        svc2.stop()


# ================================================================
# P0-6 benchmark 与生产池互斥
# ================================================================


def test_r14fix_p0_6_benchmark_blocks_resize_and_apply(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        sched = svc._ensure_scheduler_for_controller()
        # 手动进入独占态
        ok = sched.enter_benchmark_exclusive(wait_seconds=2.0)
        assert ok
        # resize / apply / set_mode / resume 全部被拒
        with pytest.raises(ValidationError):
            svc.resize_pools(video=2)
        with pytest.raises(ValidationError):
            svc.set_concurrency_mode(MODE_MANUAL, manual_video=2)
        with pytest.raises(ValidationError):
            svc.resume()
        # apply_profile 需要有 profile → 不管有没有都得拒
        # 造个假 profile
        svc._gpu_profile = gpu_profile.DeviceCapability(
            device_fingerprint="x", recommended_concurrency=2,
        )
        with pytest.raises(ValidationError):
            svc.apply_gpu_profile()
        # 退出后恢复
        sched.leave_benchmark_exclusive()
        # 现在应该可以 resize
        svc.resize_pools(video=2)
    finally:
        svc.stop()


def test_r14fix_p0_6_benchmark_restores_prev_pause_state(tmp_path, monkeypatch):
    """用户原本已 pause → benchmark 结束后不能被误 resume。"""
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        sched = svc._ensure_scheduler_for_controller()
        sched.pause()                    # 用户先 pause
        assert sched.is_paused()
        sched.enter_benchmark_exclusive(wait_seconds=2.0)
        sched.leave_benchmark_exclusive()
        assert sched.is_paused(), "用户原本 pause → benchmark 后不能误 resume"
    finally:
        svc.stop()


def test_r14fix_p0_6_benchmark_restores_non_paused(tmp_path, monkeypatch):
    """用户原本运行中 → benchmark 结束后恢复运行（清 pause）。"""
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        sched = svc._ensure_scheduler_for_controller()
        assert not sched.is_paused()
        sched.enter_benchmark_exclusive(wait_seconds=2.0)
        assert sched.is_paused(), "benchmark 期间必然 pause"
        sched.leave_benchmark_exclusive()
        assert not sched.is_paused(), "用户原本非 pause → benchmark 后恢复"
    finally:
        svc.stop()


def test_r14fix_p0_6_second_benchmark_rejected(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        sched = svc._ensure_scheduler_for_controller()
        assert sched.enter_benchmark_exclusive(wait_seconds=1.0)
        # 第二个 → False（不能同时 2 个）
        assert sched.enter_benchmark_exclusive(wait_seconds=1.0) is False
        sched.leave_benchmark_exclusive()
    finally:
        svc.stop()


# ================================================================
# P1-1 基准真实性：真 FFmpeg + 校验产物 + 时长容差
# ================================================================


@pytest.mark.skipif(not _has_ffmpeg(), reason="需要 ffmpeg")
def test_r14fix_p1_1_benchmark_uses_20s_when_video_is_10s(tmp_path, monkeypatch):
    """代表视频 5s → benchmark 应生成 10s 音频（2×video），产物长度约 10s。"""
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        sample = tmp_path / "s.mp4"
        _make_real_mp4(sample, seconds=5.0)
        r = svc.start_benchmark(sample_video=str(sample),
                                  encoder_preference="cpu",
                                  ladder=[1], preset="ultrafast", crf=28,
                                  exclusive_wait_seconds=1.0)
        # 等 benchmark 完成
        deadline = time.time() + 60
        while time.time() < deadline:
            st = svc.benchmark_status()
            if not st.get("running"):
                break
            time.sleep(0.3)
        st = svc.benchmark_status()
        assert st.get("phase") == "done", f"basline should finish; got {st}"
        # R14-FIX P1-1：audio_seconds 记录为 2×video.duration（生产同款：
        # 视频经镜像拼接后是 2×原视频长度，最终 min(2×video, tts) 决定成片长度）
        assert st.get("audio_seconds") == 10.0, \
            f"audio_seconds 应等于 2×video (10.0)，实际 {st.get('audio_seconds')}"
        prof = st.get("profile") or {}
        results = prof.get("ladder_results") or []
        assert results, "至少一档"
        r0 = results[0]
        # 至少 1 个成功、is_recommendable=True（说明 20s 完整链已通过校验）
        assert r0.get("successes", 0) >= 1
        assert r0.get("is_recommendable") is True, \
            f"完整 20s 成片应通过 duration_ok/streams_ok，实际 {r0}"
    finally:
        svc.stop()


@pytest.mark.skipif(not _has_ffmpeg(), reason="需要 ffmpeg")
def test_r14fix_p1_1_invalid_output_not_counted_success(tmp_path):
    """产物流不完整（无音频） → is_recommendable=False。"""
    # 直接用 _run_one 简化：伪造一个无音频的 ffmpeg cmd 来测校验分支
    # 用 _run_one 传入 expected_seconds 让时长校验触发失败
    from dub_align_studio.bulk_dub.gpu_profile import (
        _run_one, _BenchmarkContext, _build_render_cmd,
    )
    sample = tmp_path / "s.mp4"; _make_real_mp4(sample, 1.0)
    tts = tmp_path / "sil.wav"; gpu_profile.make_silent_wav(tts, seconds=1.0)
    from dub_align_studio.bulk_dub.hw_encoder import resolve_encoder
    enc = resolve_encoder("ffmpeg", preference="cpu")
    from dub_align_studio.bulk_dub.gpu_profile import _prepare_bench_context
    ctx = _prepare_bench_context(
        ffmpeg="ffmpeg", input_video=sample, tts_audio=tts,
        staging_root=tmp_path / "bench", encoder=enc,
        zoom_percent=130, keep_original_audio=False,
        preset="ultrafast", crf=28,
    )
    results: list = []
    cancel = threading.Event()
    # expected_seconds 故意设一个不可能达到的值 → duration_ok=False → ok=False
    _run_one(ctx, 0, cancel, tmp_path / "slot", results,
              threading.Lock(), 30.0,
              expected_seconds=999.0, duration_tolerance=0.4)
    assert results and results[0]["ok"] is False
    assert results[0]["duration_ok"] is False


# ================================================================
# P1-2 物理指标：commit-failed 单列，不算成功；tts vs video 分开
# ================================================================


def test_r14fix_p1_2_metric_separation():
    from dub_align_studio.bulk_dub.scheduler import SchedulerMetrics
    m = SchedulerMetrics()
    # 3 次物理 video 完整成功
    m.record_success(0, 5.0); m.record_physical_video()
    m.record_success(0, 5.0); m.record_physical_video()
    m.record_success(0, 5.0); m.record_physical_video()
    # 1 次 commit 失败
    m.record_render_committed_failure()
    # follower 秒完成
    m.record_success(0, 0)
    # 物理 tts 2 次
    m.record_physical_tts(); m.record_physical_tts()
    assert len(m.physical_video_finished_times) == 3
    assert m.physical_render_succeeded_commit_failed == 1
    assert len(m.physical_tts_finished_times) == 2
    # video vs tts rate 用各自的方法（不再互相 aliasing）
    assert m.recent_physical_video_rate(60) != m.recent_physical_tts_rate(60) \
        or (m.recent_physical_video_rate(60) == 3 and m.recent_physical_tts_rate(60) == 2)


def test_r14fix_p1_2_snapshot_shows_commit_failed_and_split_rates(tmp_path):
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched.start()
    try:
        sched.metrics.record_render_committed_failure()
        sched.metrics.record_physical_tts()
        snap = sched.snapshot()
        m = snap["metrics"]
        assert m["physical_render_succeeded_commit_failed"] == 1
        assert "physical_tts_rate_1min" in m
        assert "physical_tts_per_hour" in m
    finally:
        sched.stop(wait_seconds=2)


# ================================================================
# 综合 API：/gpu/apply_profile 拒失效指纹；/gpu/set_mode 持久化
# ================================================================


def test_r14fix_api_apply_profile_stale_returns_400(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        svc._gpu_profile = gpu_profile.DeviceCapability(
            device_fingerprint="stale-xxxx",
            recommended_concurrency=4,
        )
        _, status, body, _ = bulk_api.dispatch_post(
            "/api/bulk_dub/gpu/apply_profile", {}, b"", service=svc,
        )
        assert status == 400
        payload = json.loads(body)
        assert "失效" in payload["error"] or "指纹" in payload["error"]
    finally:
        svc.stop()


def test_r14fix_api_capability_shows_profile_valid_flag(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        svc._gpu_profile = gpu_profile.DeviceCapability(
            device_fingerprint="stale-xxxx", recommended_concurrency=4,
        )
        _, status, body, _ = bulk_api.dispatch_get(
            "/api/bulk_dub/gpu/capability", {}, service=svc,
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["profile_valid"] is False, \
            "陈旧 profile 必须显示 profile_valid=False"
    finally:
        svc.stop()


def test_r14fix_api_snapshot_shows_coordinator_events_and_flags(tmp_path):
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched.start()
    try:
        snap = sched.snapshot()
        assert "coordinator_recent_events" in snap
        assert "benchmark_exclusive" in snap
        assert "encoder_override" in snap
    finally:
        sched.stop(wait_seconds=2)
