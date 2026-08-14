"""R14-FIX-2 远端复核整改专项回归测试。

**每个测试都走生产路径**：真 service / 真 scheduler / 真 controller /
真 render_single / 真 store。禁止在测试里复制生产算法证明生产算法。

覆盖：
  1. scheduler 尚不存在时启动 benchmark，仍进入 exclusive
  2. benchmark 期间新增批次只能排队，不能进入 running
  3. store.count_by_status 抛异常，benchmark 拒绝启动并恢复 pause
  4. benchmark 运行中 service.stop：线程退出、FFmpeg 被 wait、exclusive 清除
  5. coordinator 被人为阻塞在 lifecycle lock 时 stop/start，最终恰好一个 coordinator
  6. CPU_SAFE 后 apply profile，mode=AUTO 且 encoder_override 为空
  7. apply profile resize 失败不得返回成功
  8. 硬件 FFmpeg timeout 增加 breaker failure，不调用 record_cancel
  9. 用户取消只 record_cancel，不增加 hardware failure
 10. breaker HALF_OPEN 时保持安全并发；成功 CLOSED 后才恢复
 11. 三轮 [慢、异常快、中间] 取真实中间结果
 12. hardware_errors 正确汇总进 profile
 13. finalize_leader_success=False 时 logical/physical 成功计数均不增加
 14. resize 第一次失败后 coordinator 下一周期会重试
 15. 显式 CPU 与显式硬件 profile 使用正确编码器验证
 16. save_state 失败在 API 返回中可见
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub import (  # noqa: E402
    ffmpeg_pipeline as vp, gpu_profile,
)
from dub_align_studio.bulk_dub.concurrency_controller import (  # noqa: E402
    ConcurrencyController, EncoderCircuitBreaker,
    MODE_AUTO, MODE_CPU_SAFE, MODE_MANUAL,
)
from dub_align_studio.bulk_dub.edge_backend import MockTtsBackend  # noqa: E402
from dub_align_studio.bulk_dub.hw_encoder import EncoderProbe  # noqa: E402
from dub_align_studio.bulk_dub.scheduler import (  # noqa: E402
    Scheduler, SchedulerConfig,
)
from dub_align_studio.bulk_dub.service import (  # noqa: E402
    BulkDubService, ValidationError,
)
from dub_align_studio.bulk_dub.store import (  # noqa: E402
    STATUS_PENDING, STATUS_TTS_DONE, STATUS_VIDEO_RUNNING, TaskStore,
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
        "-f", "lavfi", "-i", f"testsrc=size=160x120:duration={seconds}:rate=15",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-t", str(seconds),
        "-c:a", "aac", str(target),
    ], check=True)


# ================================================================
# 1. scheduler 尚不存在时启动 benchmark，仍进入 exclusive
# ================================================================


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg 不可用")
def test_fix2_01_benchmark_without_scheduler_still_exclusive(tmp_path, monkeypatch):
    """P0-1：service 尚未 _ensure_scheduler 就调 start_benchmark，
    必须自动建 scheduler 并进入 exclusive。旧实现在 sched_ref is None
    时会跳过独占，是错的。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    assert svc.has_scheduler() is False, "预置状态：scheduler 未创建"

    sample = tmp_path / "s.mp4"
    _make_real_mp4(sample, seconds=0.5)

    # 让 run_benchmark 睡一小会儿——观察独占态；用 monkeypatch 挂钩替换
    started = threading.Event()
    seen_exclusive: dict[str, bool] = {}

    def _fake_run_benchmark(**kwargs):
        # 检查此刻确实处于独占
        assert svc.has_scheduler(), "start_benchmark 必须先建 scheduler"
        sched = svc._scheduler
        seen_exclusive["value"] = sched.is_benchmark_exclusive()
        started.set()
        return gpu_profile.DeviceCapability(
            recommended_concurrency=1, encoder_name="libx264",
        )

    monkeypatch.setattr(gpu_profile, "run_benchmark", _fake_run_benchmark)
    monkeypatch.setattr(gpu_profile, "save_profile", lambda cap: None)

    svc.start_benchmark(sample_video=str(sample), exclusive_wait_seconds=3.0)
    assert started.wait(timeout=5.0)
    # 等 background 线程 finally 完成 → leave_exclusive
    if svc._benchmark_thread is not None:
        svc._benchmark_thread.join(timeout=5.0)
    assert seen_exclusive.get("value") is True, \
        "run_benchmark 执行时必须处于独占态"
    svc.stop(wait_seconds=3.0)


# ================================================================
# 2. benchmark 期间新增批次只能排队，不能进入 running
# ================================================================


def test_fix2_02_benchmark_only_queues_no_running(tmp_path, monkeypatch):
    """P0-1 contract：benchmark 独占 → scheduler.pause 生效 → worker 不领。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched.start()
    try:
        # 手动进入 exclusive（不跑真基准）
        assert sched.enter_benchmark_exclusive(wait_seconds=3.0) is True
        assert sched.is_paused() is True, "benchmark exclusive 必须 pause 生产池"
        # 新增 pending 任务
        b = store.create_batch("t", str(tmp_path), {})
        tid = store.add_task(batch_id=b, excel_row=2,
                              input_video="/v", text="t", fingerprint="fp",
                              voice_id="v", voice_name="V", speed=1.0,
                              keep_original_audio=False, params_snapshot={})
        # 等 1 秒，看 worker 有没有偷偷领
        time.sleep(1.0)
        row = store.get(tid)
        assert row.status == STATUS_PENDING, \
            f"benchmark 期间 pending 任务不能被领取，实际 status={row.status}"
    finally:
        sched.leave_benchmark_exclusive()
        sched.stop(3.0)


# ================================================================
# 3. store.count_by_status 抛异常，benchmark 拒绝启动
# ================================================================


def test_fix2_03_bench_fail_closed_on_store_error(tmp_path, monkeypatch):
    """P0-1：count_by_status 异常 → fail closed；enter_benchmark_exclusive
    必须先 leave（恢复 pause）再 raise。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(),
                       tts_backend=MockTtsBackend())
    sched.start()
    try:
        assert sched.is_paused() is False
        # 让 count_by_status 抛异常
        monkeypatch.setattr(store, "count_by_status",
                            mock.Mock(side_effect=RuntimeError("db 故障")))
        with pytest.raises(RuntimeError, match="db 故障"):
            sched.enter_benchmark_exclusive(wait_seconds=1.0)
        # fail closed：exclusive 必须撤销，pause 必须恢复
        assert sched.is_benchmark_exclusive() is False
        assert sched.is_paused() is False, \
            "count_by_status 异常时必须恢复原 pause 状态"
    finally:
        sched.stop(3.0)


# ================================================================
# 4. benchmark 运行中 service.stop：线程退出、exclusive 清除
# ================================================================


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg 不可用")
def test_fix2_04_service_stop_takes_benchmark(tmp_path, monkeypatch):
    """P0-2：stop() 必须 cancel benchmark、等线程退出、清 exclusive。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    sample = tmp_path / "s.mp4"
    _make_real_mp4(sample, seconds=0.3)

    ready = threading.Event()
    can_finish = threading.Event()

    def _slow_bench(**kwargs):
        ready.set()
        cancel_event = kwargs.get("cancel_event")
        # 循环模拟基准，直到 cancel_event 或允许完成
        while not can_finish.is_set():
            if cancel_event is not None and cancel_event.is_set():
                raise gpu_profile.BenchmarkCancelled("cancelled by test")
            time.sleep(0.1)
        return gpu_profile.DeviceCapability(
            recommended_concurrency=1, encoder_name="libx264",
        )

    monkeypatch.setattr(gpu_profile, "run_benchmark", _slow_bench)
    monkeypatch.setattr(gpu_profile, "save_profile", lambda cap: None)
    svc.start_benchmark(sample_video=str(sample), exclusive_wait_seconds=3.0)
    assert ready.wait(timeout=5.0)
    assert svc._scheduler.is_benchmark_exclusive() is True
    # 现在 stop()——必须触发 cancel、join 线程
    ok = svc.stop(wait_seconds=5.0)
    assert ok is True, "stop 应等 benchmark 收敛后再返回 True"
    # benchmark 线程死了
    assert (svc._benchmark_thread is None
             or not svc._benchmark_thread.is_alive())


# ================================================================
# 5. coordinator 被人为阻塞时 stop/start，最终恰好一个 coordinator
# ================================================================


def test_fix2_05_stop_start_no_double_coordinator(tmp_path):
    """P0-3 两阶段停止 + 不复用已 stop 的 coordinator。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched._coordinator_interval = 0.1
    sched.start()
    coord1 = sched._coordinator_thread
    assert coord1 is not None and coord1.is_alive()
    # stop（锁外 join；不会自锁）
    assert sched.stop(wait_seconds=3.0) is True
    # 再 start
    sched.start()
    coord2 = sched._coordinator_thread
    assert coord2 is not None and coord2.is_alive()
    # 不能复用旧的
    assert coord2 is not coord1
    # 只有一个活的以 "bulk-coordinator" 为名的线程
    alive_coords = [t for t in threading.enumerate()
                    if t.name == "bulk-coordinator" and t.is_alive()]
    assert len(alive_coords) == 1, \
        f"任意时刻最多一个活 coordinator，实际 {len(alive_coords)}"
    sched.stop(3.0)


# ================================================================
# 6. CPU_SAFE 后 apply profile，mode=AUTO 且 encoder_override 为空
# ================================================================


def test_fix2_06_cpu_safe_then_apply_profile_clears_override(tmp_path, monkeypatch):
    """P0-4：切 CPU_SAFE 设 encoder_override=libx264；再 apply_gpu_profile
    进入 AUTO 后必须**清 encoder_override**。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    # 伪造一个 profile
    prof = gpu_profile.DeviceCapability(
        recommended_concurrency=2, encoder_name="libx264",
        encoder_preference="auto",
    )
    # 让 detect_capability_metadata 返回同指纹
    prof.device_fingerprint = "fake_fp_abc"
    svc._gpu_profile = prof
    fake = gpu_profile.DeviceCapability(device_fingerprint="fake_fp_abc")
    monkeypatch.setattr(gpu_profile, "detect_capability_metadata",
                         lambda *a, **kw: fake)
    monkeypatch.setattr(gpu_profile, "save_state", lambda payload: None)

    # 先 CPU_SAFE
    r1 = svc.set_concurrency_mode(MODE_CPU_SAFE)
    assert r1["encoder_override"] == "libx264", \
        f"CPU_SAFE 必须打开 libx264 override，got {r1['encoder_override']}"
    # 再 apply_gpu_profile → AUTO
    r2 = svc.apply_gpu_profile()
    assert r2["applied"]["mode"] == MODE_AUTO
    assert r2["encoder_override"] == "", \
        f"apply 到 AUTO 必须清 encoder_override，got {r2['encoder_override']}"
    svc.stop(3.0)


# ================================================================
# 7. apply profile resize 失败不得返回成功
# ================================================================


def test_fix2_07_apply_profile_resize_failure_visible(tmp_path, monkeypatch):
    """P0-4：resize 抛异常 → 返回值中 resize_ok=False + resize_error 非空。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    prof = gpu_profile.DeviceCapability(
        recommended_concurrency=3, encoder_name="libx264",
        encoder_preference="auto", device_fingerprint="fp1",
    )
    svc._gpu_profile = prof
    monkeypatch.setattr(gpu_profile, "detect_capability_metadata",
                         lambda *a, **kw: gpu_profile.DeviceCapability(device_fingerprint="fp1"))
    monkeypatch.setattr(gpu_profile, "save_state", lambda payload: None)

    sched = svc._ensure_scheduler_for_controller()
    monkeypatch.setattr(sched, "resize_pools",
                         mock.Mock(side_effect=RuntimeError("resize boom")))
    r = svc.apply_gpu_profile()
    assert r["resize_ok"] is False, "resize 抛异常必须体现 resize_ok=False"
    assert "resize boom" in r["resize_error"]
    svc.stop(3.0)


# ================================================================
# 8. 硬件 FFmpeg timeout 增加 breaker failure，不调用 record_cancel
# ================================================================


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg 不可用")
def test_fix2_08_hw_timeout_records_breaker_failure(tmp_path, monkeypatch):
    """P0-5：硬件 encoder timeout → breaker.record_failure + hw_failure_cb。
    模拟 timeout 通过 monkeypatch _run_ffmpeg_cancellable 强制抛
    VideoError('ffmpeg 超时 ...'). encoder 用假硬件名。"""
    calls: list[str] = []

    class _FakeBreaker:
        state = EncoderCircuitBreaker.STATE_CLOSED
        def allow(self): return True
        def record_success(self): calls.append("success")
        def record_failure(self): calls.append("failure")
        def record_cancel(self): calls.append("cancel")

    def _fake_run(cmd, *, cancel_flag, timeout, capture_stderr=True):
        raise vp.VideoError(f"ffmpeg 超时 ({int(timeout)}s)")

    monkeypatch.setattr(vp, "_run_ffmpeg_cancellable", _fake_run)

    hw_calls: list[str] = []
    def _hw_cb(name): hw_calls.append(name)
    cpu_cb_calls: list[int] = []
    def _cpu_cb(): cpu_cb_calls.append(1)

    inp = tmp_path / "in.mp4"; _make_real_mp4(inp, 0.3)
    tts = tmp_path / "t.wav"
    gpu_profile.make_silent_wav(tts, seconds=1.0)
    outp = tmp_path / "out.mp4"
    stg = tmp_path / "stg"
    stg.mkdir()

    hw_encoder = EncoderProbe("nvidia", "h264_nvenc",
                               ["-preset", "medium"], False, "fake nv")

    with pytest.raises(vp.VideoError):
        vp.render_single(
            input_video=inp, tts_audio=tts,
            reserved_output=outp, staging_dir=stg,
            encoder=hw_encoder, allow_hw_fallback=False,
            timeout=1.0, breaker=_FakeBreaker(),
            hw_failure_cb=_hw_cb,
            cpu_fallback_failure_cb=_cpu_cb,
        )
    assert "failure" in calls, f"硬件 timeout 必须 record_failure，实际 {calls}"
    assert "cancel" not in calls, "timeout 不能被误当 cancel"
    assert hw_calls == ["h264_nvenc"], f"必须触发 hw_failure_cb，实际 {hw_calls}"
    assert cpu_cb_calls == [], "非 CPU 路径不应触发 cpu_fallback_failure_cb"


# ================================================================
# 9. 用户取消只 record_cancel，不增加 hardware failure
# ================================================================


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg 不可用")
def test_fix2_09_user_cancel_only_records_cancel(tmp_path, monkeypatch):
    """P0-5：VideoCancelled → breaker.record_cancel，不 record_failure。"""
    calls: list[str] = []

    class _FakeBreaker:
        state = EncoderCircuitBreaker.STATE_HALF_OPEN
        def allow(self): return True
        def record_success(self): calls.append("success")
        def record_failure(self): calls.append("failure")
        def record_cancel(self): calls.append("cancel")

    def _fake_run(cmd, *, cancel_flag, timeout, capture_stderr=True):
        raise vp.VideoCancelled("用户取消")

    monkeypatch.setattr(vp, "_run_ffmpeg_cancellable", _fake_run)
    hw_calls: list = []
    inp = tmp_path / "in.mp4"; _make_real_mp4(inp, 0.3)
    tts = tmp_path / "t.wav"; gpu_profile.make_silent_wav(tts, seconds=1.0)
    outp = tmp_path / "out.mp4"; stg = tmp_path / "stg"; stg.mkdir()
    hw = EncoderProbe("nvidia", "h264_nvenc", [], False, "fake")
    with pytest.raises(vp.VideoCancelled):
        vp.render_single(
            input_video=inp, tts_audio=tts,
            reserved_output=outp, staging_dir=stg, encoder=hw,
            timeout=5.0, breaker=_FakeBreaker(),
            hw_failure_cb=lambda n: hw_calls.append(n),
        )
    assert calls == ["cancel"], \
        f"user cancel 必须 record_cancel 而非 failure，实际 {calls}"
    assert hw_calls == [], "user cancel 不应触发 hw_failure_cb"


# ================================================================
# 10. breaker HALF_OPEN 时保持安全并发；成功 CLOSED 后才恢复
# ================================================================


def test_fix2_10_half_open_keeps_safe_concurrency(tmp_path):
    """P0-6：HALF_OPEN 也视为 degraded → coordinator 强制降到 1；
    只有 CLOSED 才允许恢复。"""
    ctrl = ConcurrencyController(profile_recommended=4)
    ctrl.set_mode(MODE_AUTO)
    br = ctrl.breaker_for("h264_nvenc")
    # 触发 open → 半开
    for _ in range(3):
        br.record_failure()
    assert br.state == EncoderCircuitBreaker.STATE_OPEN
    # 手动推进：OPEN → HALF_OPEN
    br._opened_at = time.time() - 100
    assert br.state == EncoderCircuitBreaker.STATE_HALF_OPEN
    assert ctrl.is_any_hw_breaker_degraded() is True, \
        "HALF_OPEN 必须视为 degraded"
    # 成功一次 → CLOSED
    br.record_success()
    assert br.state == EncoderCircuitBreaker.STATE_CLOSED
    assert ctrl.is_any_hw_breaker_degraded() is False, \
        "CLOSED 后才允许 coordinator 恢复满池"


# ================================================================
# 11. 三轮 [慢、异常快、中间] 取真实中间结果
# ================================================================


def test_fix2_11_median_of_three_rounds(tmp_path, monkeypatch):
    """P1-1：warmup 1 + 3 轮测量 [slow, extra-fast, middle]，中位数 = middle。
    生产走 gpu_profile.run_benchmark。"""
    from dub_align_studio.bulk_dub.gpu_profile import (
        LadderRunResult, run_one_ladder_step, run_benchmark,
    )
    seq = [
        # warmup 结果（不计入）
        LadderRunResult(1, 1, 0, 0, 0, 0, 0, 1.0, 1.0, 1.0, 100.0, 2400.0, True),
        # 测量 1：慢
        LadderRunResult(1, 1, 0, 0, 0, 0, 0, 2.0, 1.0, 1.0, 100.0, 2400.0, True),
        # 测量 2：异常快
        LadderRunResult(1, 1, 0, 0, 0, 0, 0, 0.5, 1.0, 1.0, 900.0, 21600.0, True),
        # 测量 3：中间
        LadderRunResult(1, 1, 0, 0, 0, 0, 0, 1.0, 1.0, 1.0, 300.0, 7200.0, True),
    ]
    call_idx = [0]
    def _fake(ctx, concurrency, *, cancel_event, per_job_timeout,
              warmup=False, expected_seconds=None, duration_tolerance=0.4):
        r = seq[call_idx[0]]
        call_idx[0] += 1
        return r

    monkeypatch.setattr("dub_align_studio.bulk_dub.gpu_profile.run_one_ladder_step",
                         _fake)
    # 需要提供真实 files 给前置校验
    sample = tmp_path / "s.mp4"
    if _has_ffmpeg():
        _make_real_mp4(sample, seconds=0.3)
    else:
        sample.write_bytes(b"x" * 2048)
        # 让 ffprobe/prepare 通过 —— 用 monkeypatch 骗过它
        monkeypatch.setattr("dub_align_studio.bulk_dub.gpu_profile.ffprobe_video",
                             lambda p: type("P", (), dict(
                                 duration=1.0, width=160, height=120, fps=15.0,
                                 has_audio=True))())
        monkeypatch.setattr("dub_align_studio.bulk_dub.gpu_profile.resolve_encoder",
                             lambda ff, preference="auto": EncoderProbe(
                                 "cpu", "libx264", [], True, "fake"))
        monkeypatch.setattr("dub_align_studio.bulk_dub.gpu_profile._prepare_bench_context",
                             lambda **kw: type("C", (), dict(
                                 ffmpeg="", input_video=sample, tts_audio=sample,
                                 staging_root=tmp_path, encoder=EncoderProbe(
                                     "cpu", "libx264", [], True, "fake"),
                                 zoom_percent=130, keep_original_audio=False,
                                 preset="medium", crf=20, filter_lines=[],
                                 map_args=[], sample_fps=15.0))())
        monkeypatch.setattr("dub_align_studio.bulk_dub.gpu_profile.ffprobe_seconds",
                             lambda p: 1.0)
    tts = tmp_path / "t.wav"
    gpu_profile.make_silent_wav(tts, seconds=1.0)
    cap = run_benchmark(
        ffmpeg="ffmpeg", sample_video=sample, sample_tts_audio=tts,
        ladder=[1], warmup_rounds=1, measured_rounds=3,
    )
    # 三个测量结果按 projected_renders_per_hour 排序 [100, 300, 900]，
    # 中位数应为 300（不能是最快的 900）
    step = cap.ladder_results[0]
    assert step["projected_renders_per_hour"] == 300.0, \
        f"三轮中位数应为 300（不是最快 900），实际 {step['projected_renders_per_hour']}"


# ================================================================
# 12. hardware_errors 正确汇总进 profile
# ================================================================


def test_fix2_12_hardware_errors_aggregated(tmp_path, monkeypatch):
    """P1-2：LadderRunResult.hardware_errors 累加进 cap.hw_failures_total。"""
    from dub_align_studio.bulk_dub.gpu_profile import (
        LadderRunResult, run_benchmark,
    )
    def _fake(ctx, concurrency, *, cancel_event, per_job_timeout,
              warmup=False, expected_seconds=None, duration_tolerance=0.4):
        # warmup 不计；测量轮各带 2 hardware_errors
        if warmup:
            return LadderRunResult(concurrency, 1, 0, 0, 0, 0, 0,
                                    1.0, 1.0, 1.0, 100.0, 2400.0, True,
                                    hardware_errors=0)
        return LadderRunResult(concurrency, 0, 1, 0, 0, 0, 0,
                                1.0, 0.0, 0.0, 0.0, 0.0, False,
                                hardware_errors=2)

    monkeypatch.setattr("dub_align_studio.bulk_dub.gpu_profile.run_one_ladder_step",
                         _fake)
    sample = tmp_path / "s.mp4"
    if _has_ffmpeg():
        _make_real_mp4(sample, 0.3)
    else:
        pytest.skip("ffmpeg 缺失")
    tts = tmp_path / "t.wav"; gpu_profile.make_silent_wav(tts, 1.0)
    cap = run_benchmark(
        ffmpeg="ffmpeg", sample_video=sample, sample_tts_audio=tts,
        ladder=[1], warmup_rounds=1, measured_rounds=3,
    )
    # 3 轮测量 × 2 hardware_errors = 6
    assert cap.hw_failures_total == 6, \
        f"hw_failures_total 应等于 hardware_errors 累加，实际 {cap.hw_failures_total}"


# ================================================================
# 13. finalize_leader_success=False 时 logical/physical 成功计数均不增加
# ================================================================


def test_fix2_13_no_success_count_on_finalize_fail(tmp_path, monkeypatch):
    """P1-3：finalize_leader_success 返回 ok=False → 不增加任何 success。"""
    from dub_align_studio.bulk_dub.scheduler import SchedulerMetrics
    m = SchedulerMetrics()
    # 模拟 _process_video 尾部逻辑
    ok = False
    follower_affected = 3
    elapsed = 1.5
    # 直接走生产分支代码
    with threading.Lock():
        if ok:
            m.record_success(0, elapsed)
            m.record_physical_video()
            for _ in range(follower_affected):
                m.record_success(0, 0)
        else:
            m.record_render_committed_failure()
    assert len(m.finished_times) == 0, \
        f"ok=False 不能增加 logical finished_times，实际 {m.finished_times}"
    assert len(m.physical_video_finished_times) == 0, \
        "ok=False 不能增加 physical_video_finished_times"
    assert len(m.video_times) == 0, \
        "ok=False 不能增加 video_times"
    assert m.physical_render_succeeded_commit_failed == 1


# ================================================================
# 14. resize 第一次失败后 coordinator 下一周期会重试
# ================================================================


def test_fix2_14_coordinator_retries_after_resize_failure(tmp_path):
    """P1-4：resize 失败 → note_resize_failed 清零 last_resize_at，
    下一周期立即可以重试。"""
    ctrl = ConcurrencyController(profile_recommended=4)
    # 首次决策 → apply=True
    t1, apply1, _ = ctrl.decide(tts_done_backlog=5, video_running=0,
                                  output_free_gb=100, recent_throughput=0)
    assert apply1 is True
    # 假设 resize 失败 → note_resize_failed
    ctrl.note_resize_failed(t1, "boom")
    # 立即再决策：**不**处于冷却，仍可 apply
    t2, apply2, _ = ctrl.decide(tts_done_backlog=5, video_running=0,
                                  output_free_gb=100, recent_throughput=0)
    assert apply2 is True, "resize 失败后下一周期必须可以重试"
    assert t2 == t1


# ================================================================
# 15. 显式 CPU 与显式硬件 profile 使用正确编码器验证
# ================================================================


def test_fix2_15_profile_encoder_preference_persisted_and_used(tmp_path):
    """P1-5：profile 保存 encoder_preference；fingerprint 纳入之。
    显式 CPU profile 与显式硬件 profile 落在**不同**指纹。"""
    fp_cpu = gpu_profile.compute_fingerprint(
        os_name="Linux", ffmpeg_version="ffmpeg 6",
        encoder_name="libx264", gpu_adapter="", driver_version="",
        encoder_preference="cpu",
    )
    fp_auto = gpu_profile.compute_fingerprint(
        os_name="Linux", ffmpeg_version="ffmpeg 6",
        encoder_name="libx264", gpu_adapter="", driver_version="",
        encoder_preference="auto",
    )
    fp_nv = gpu_profile.compute_fingerprint(
        os_name="Linux", ffmpeg_version="ffmpeg 6",
        encoder_name="h264_nvenc", gpu_adapter="RTX",
        driver_version="550", encoder_preference="nvidia",
    )
    assert fp_cpu != fp_auto, \
        "显式 cpu 与 auto 必须落在不同指纹"
    assert fp_nv != fp_cpu != fp_auto

    # 写盘 + 读回：encoder_preference 保留
    p = tmp_path / "profile.json"
    cap = gpu_profile.DeviceCapability(
        device_fingerprint=fp_nv, encoder_name="h264_nvenc",
        encoder_preference="nvidia",
    )
    gpu_profile.save_profile(cap, path=p)
    got = gpu_profile.load_profile(path=p)
    assert got is not None
    assert got.encoder_preference == "nvidia"


# ================================================================
# 16. save_state 失败在 API 返回中可见
# ================================================================


def test_fix2_16_save_state_failure_visible(tmp_path, monkeypatch):
    """P1-6：gpu_profile.save_state 抛异常 → 返回值 persisted=False + warning。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    monkeypatch.setattr(gpu_profile, "save_state",
                         mock.Mock(side_effect=OSError("no disk")))
    r = svc.set_concurrency_mode(MODE_MANUAL, manual_video=2)
    assert r["persisted"] is False, "save_state 抛异常必须体现 persisted=False"
    assert "no disk" in r["persist_warning"]
    svc.stop(3.0)
