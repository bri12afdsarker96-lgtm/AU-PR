"""R14-FIX-2 + R14-FIX-3 远端复核整改专项回归测试。

**每个测试都走生产路径**：真 service / 真 scheduler / 真 controller /
真 render_single / 真 store。禁止在测试里复制生产算法证明生产算法。

R14-FIX-3 更新：
  * test_fix2_08 契约变为「硬件 timeout 只经 hw_failure_cb 计一次」
  * test_fix2_10 走真实 Scheduler + coordinator（不再纯 controller 单元）
  * test_fix2_13 走真实 TaskStore + Scheduler + _process_video
  * test_fix2_14 走真实 coordinator + monkeypatch resize_pools
  * test_fix2_15 走真实 apply_gpu_profile + 捕获 detect_capability_metadata
  * 新增 test_fix3_* 系列覆盖并发/进程隔离/DB+active/ladder 400 等
"""

from __future__ import annotations

import functools
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
    api as bulk_api, ffmpeg_pipeline as vp, gpu_profile,
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


@functools.lru_cache(maxsize=1)
def _has_ffmpeg() -> bool:
    """R14-FIX-4b：探测一次全程复用；timeout 放宽到 15s 让慢机也能通过，
    避免"探测超时 → skip → 复核看到 1 skipped"的伪失败。"""
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=True, timeout=15)
        return True
    except Exception:  # noqa: BLE001
        return False


# R14-FIX-4：慢机鲁棒——统一 poll + 宽 deadline 的等待助手。
# 禁止在断言前裸 `time.sleep(固定值)` —— 慢机 CI 下会漂移。
def _wait_until(cond, timeout: float = 15.0, interval: float = 0.05) -> bool:
    """轮询 cond() 直到返回真值或超时。返回是否满足条件。"""
    end = time.time() + max(0.05, float(timeout))
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    try:
        return bool(cond())
    except Exception:  # noqa: BLE001
        return False


def _stays_true(cond, duration: float = 3.0, interval: float = 0.05) -> bool:
    """要求 cond() 在整段窗口内**持续**为真——用于"什么坏事都不该发生"
    的负向断言。任一次探测为假立即返回 False。"""
    end = time.time() + max(0.1, float(duration))
    while time.time() < end:
        try:
            if not cond():
                return False
        except Exception:  # noqa: BLE001
            return False
        time.sleep(interval)
    return True


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
    必须自动建 scheduler 并进入 exclusive。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    assert svc.has_scheduler() is False, "预置状态：scheduler 未创建"

    sample = tmp_path / "s.mp4"
    _make_real_mp4(sample, seconds=0.5)

    started = threading.Event()
    seen_exclusive: dict[str, bool] = {}

    def _fake_run_benchmark(**kwargs):
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
        assert sched.enter_benchmark_exclusive(wait_seconds=15.0) is True
        assert sched.is_paused() is True, "benchmark exclusive 必须 pause 生产池"
        b = store.create_batch("t", str(tmp_path), {})
        tid = store.add_task(batch_id=b, excel_row=2,
                              input_video="/v", text="t", fingerprint="fp",
                              voice_id="v", voice_name="V", speed=1.0,
                              keep_original_audio=False, params_snapshot={})
        # R14-FIX-4：**持续**验证 3 秒内 status 永远不离开 PENDING（负向断言）
        assert _stays_true(
            lambda: store.get(tid).status == STATUS_PENDING,
            duration=3.0, interval=0.05,
        ), (
            f"benchmark 期间 pending 任务不能被领取，"
            f"实际 status={store.get(tid).status}"
        )
    finally:
        sched.leave_benchmark_exclusive()
        sched.stop(15.0)


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
        monkeypatch.setattr(store, "count_by_status",
                            mock.Mock(side_effect=RuntimeError("db 故障")))
        with pytest.raises(RuntimeError, match="db 故障"):
            sched.enter_benchmark_exclusive(wait_seconds=1.0)
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
    ok = svc.stop(wait_seconds=5.0)
    assert ok is True, "stop 应等 benchmark 收敛后再返回 True"
    assert (svc._benchmark_thread is None
             or not svc._benchmark_thread.is_alive())


# ================================================================
# 5. coordinator stop/start，最终恰好一个 coordinator（P0-5：无锁内 join）
# ================================================================


def test_fix2_05_stop_start_no_double_coordinator(tmp_path):
    """P0-5：stop/start 之间不能产生两个 coordinator；锁内绝不 join。

    R14-FIX-4：慢机鲁棒——**只核对本 scheduler 的 coord 身份**，
    不再枚举全局 threading.enumerate()——上一版会误报其他并行/前序测试
    正在收敛的 coord 线程（本轮 P0-5 已保证任意时刻本实例内最多一个活
    coord，跨实例的 in-flight 收敛不是本用例证明的目标）。
    """
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched._coordinator_interval = 0.1
    sched.start()
    coord1 = sched._coordinator_thread
    assert coord1 is not None and coord1.is_alive()
    assert sched.stop(wait_seconds=15.0) is True
    # stop 之后 coord1 应当已死；即使 OS 调度慢，最多也是 finalization pending
    assert _wait_until(lambda: not coord1.is_alive(), timeout=10.0), \
        "coord1 stop 后必须最终死亡"
    sched.start()
    coord2 = sched._coordinator_thread
    assert coord2 is not None and coord2.is_alive()
    assert coord2 is not coord1, "start 后必须新起 coord，不复用旧引用"
    # 本 scheduler 只允许 coord2 活着（coord1 已死）
    mine_alive = [t for t in (coord1, coord2) if t.is_alive()]
    assert mine_alive == [coord2], \
        f"本 scheduler 应恰只有 coord2 活着，实际 {mine_alive}"
    sched.stop(15.0)


# ================================================================
# 6. CPU_SAFE 后 apply profile，mode=AUTO 且 encoder_override 为空
# ================================================================


def test_fix2_06_cpu_safe_then_apply_profile_clears_override(tmp_path, monkeypatch):
    """P0-4：切 CPU_SAFE 设 encoder_override=libx264；再 apply_gpu_profile
    进入 AUTO 后必须**清 encoder_override**。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    prof = gpu_profile.DeviceCapability(
        recommended_concurrency=2, encoder_name="libx264",
        encoder_preference="auto",
    )
    prof.device_fingerprint = "fake_fp_abc"
    svc._gpu_profile = prof
    fake = gpu_profile.DeviceCapability(device_fingerprint="fake_fp_abc")
    monkeypatch.setattr(gpu_profile, "detect_capability_metadata",
                         lambda *a, **kw: fake)
    monkeypatch.setattr(gpu_profile, "save_state", lambda payload: None)

    r1 = svc.set_concurrency_mode(MODE_CPU_SAFE)
    assert r1["encoder_override"] == "libx264", \
        f"CPU_SAFE 必须打开 libx264 override，got {r1['encoder_override']}"
    r2 = svc.apply_gpu_profile()
    assert r2["applied"]["mode"] == MODE_AUTO
    assert r2["encoder_override"] == "", \
        f"apply 到 AUTO 必须清 encoder_override，got {r2['encoder_override']}"
    svc.stop(3.0)


# ================================================================
# 7. apply profile resize 失败必须不宣称成功，且回滚快照
# ================================================================


def test_fix2_07_apply_profile_resize_failure_visible(tmp_path, monkeypatch):
    """P0-4 / P1-3：resize 抛异常 → resize_ok=False + resize_error 非空 +
    applied=False + persisted=False。回滚到应用前 mode/override 快照。"""
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
    # 先切到 CPU_SAFE 建立"应用前"快照
    svc.set_concurrency_mode(MODE_CPU_SAFE)
    pre_override = sched.encoder_override()
    monkeypatch.setattr(sched, "resize_pools",
                         mock.Mock(side_effect=RuntimeError("resize boom")))
    r = svc.apply_gpu_profile()
    assert r["resize_ok"] is False, "resize 抛异常必须体现 resize_ok=False"
    assert "resize boom" in r["resize_error"]
    # P1-3：apply=False；persisted=False；requested_video 与 actual_video 分离
    assert r["applied"]["applied"] is False
    assert r["persisted"] is False, "resize 失败必须 persisted=False"
    assert r["requested_video"] == 3
    # 回滚：encoder_override 恢复到 CPU_SAFE 时的 libx264
    assert sched.encoder_override() == pre_override
    svc.stop(3.0)


# ================================================================
# 8. 硬件 FFmpeg timeout 增加 breaker failure 恰好一次
# ================================================================


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg 不可用")
def test_fix2_08_hw_timeout_records_breaker_failure(tmp_path, monkeypatch):
    """R14-FIX-3 P0-6：硬件 encoder timeout → 单一所有权：**只经 callback**。
    render_single 不再直接调 breaker.record_failure；controller callback
    负责 recent failure + breaker（真控制器路径）。
    不能再是 +2。
    """
    class _FakeBreaker:
        state = EncoderCircuitBreaker.STATE_CLOSED
        calls: list[str] = []
        def allow(self): return True
        def record_success(self): self.calls.append("success")
        def record_failure(self): self.calls.append("failure")
        def record_cancel(self): self.calls.append("cancel")

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
    breaker = _FakeBreaker()
    breaker.calls = []
    with pytest.raises(vp.VideoError):
        vp.render_single(
            input_video=inp, tts_audio=tts,
            reserved_output=outp, staging_dir=stg,
            encoder=hw_encoder, allow_hw_fallback=False,
            timeout=1.0, breaker=breaker,
            hw_failure_cb=_hw_cb,
            cpu_fallback_failure_cb=_cpu_cb,
        )
    # R14-FIX-3 P0-6：**callback 存在** → render_single 不再直接调 breaker
    assert breaker.calls == [], \
        f"render_single 有 callback 时不能双记 breaker，实际 {breaker.calls}"
    assert hw_calls == ["h264_nvenc"], \
        f"必须触发 hw_failure_cb，实际 {hw_calls}"
    assert cpu_cb_calls == [], "非 CPU 路径不应触发 cpu_fallback_failure_cb"


# ================================================================
# 9. 用户取消只 record_cancel
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
# 10. breaker HALF_OPEN — 走真实 Scheduler + coordinator（P0-6）
# ================================================================


def test_fix2_10_half_open_keeps_safe_concurrency(tmp_path, monkeypatch):
    """R14-FIX-3 P0-6：真实 Scheduler coordinator 下——
    breaker 进入 HALF_OPEN → coordinator 强制 video_alive=1；
    record_success CLOSED 后才可放大。真实 resize_pools 运行。"""
    import dub_align_studio.bulk_dub.concurrency_controller as cc
    monkeypatch.setattr(cc, "RESIZE_MIN_INTERVAL_S", 0.1)
    store = TaskStore(tmp_path / "q.sqlite3")
    cfg = SchedulerConfig(tts_concurrency=1, video_concurrency=4)
    sched = Scheduler(store=store, config=cfg, tts_backend=MockTtsBackend())
    sched._coordinator_interval = 0.05
    sched.controller.set_profile_recommended(4)
    # 造 backlog：coordinator 才会 ramp-up
    b = store.create_batch("bl", str(tmp_path),
                             {"output_dir": str(tmp_path)})
    for i in range(5):
        tid = store.add_task(
            batch_id=b, excel_row=i + 2,
            input_video=str(tmp_path / f"v{i}.mp4"), text="t",
            fingerprint=f"fp{i}", voice_id="v", voice_name="V",
            speed=1.0, keep_original_audio=False,
            params_snapshot={"output_dir": str(tmp_path)},
        )
        store.try_advance_status(tid, from_status=STATUS_PENDING,
                                  to_status=STATUS_TTS_DONE)
    sched.pause()  # worker 不领；resize_pools 真跑（spawn threads only）
    sched.start()
    try:
        # 等 coordinator 稳定到 4 个视频 worker
        deadline = time.time() + 8.0
        while time.time() < deadline:
            snap = sched.snapshot()
            if int(snap.get("video_alive") or 0) >= 4:
                break
            time.sleep(0.05)
        assert int(sched.snapshot().get("video_alive") or 0) >= 4, \
            "coord 应先稳定到 4 video worker"
        # 触发 breaker OPEN → HALF_OPEN
        br = sched.controller.breaker_for("h264_nvenc")
        for _ in range(3):
            br.record_failure()
        br._opened_at = time.time() - 100
        assert br.state == EncoderCircuitBreaker.STATE_HALF_OPEN
        assert sched.controller.is_any_hw_breaker_degraded() is True
        # 等 coordinator 降到 1
        deadline = time.time() + 8.0
        while time.time() < deadline:
            snap = sched.snapshot()
            if int(snap.get("video_alive") or 0) == 1:
                break
            time.sleep(0.05)
        assert int(sched.snapshot().get("video_alive") or 0) == 1, \
            "HALF_OPEN 必须降到 video_alive=1"
        # record_success → CLOSED → 允许恢复
        br.record_success()
        assert sched.controller.is_any_hw_breaker_degraded() is False
        deadline = time.time() + 8.0
        while time.time() < deadline:
            snap = sched.snapshot()
            if int(snap.get("video_alive") or 0) >= 4:
                break
            time.sleep(0.05)
        assert int(sched.snapshot().get("video_alive") or 0) >= 4, \
            "CLOSED 后 coordinator 应能恢复到 profile_recommended"
    finally:
        sched.stop(3.0)


# ================================================================
# 11. 三轮 [慢、异常快、中间] 取真实中间结果
# ================================================================


def test_fix2_11_median_of_three_rounds(tmp_path, monkeypatch):
    """P1-1：warmup 1 + 3 轮测量 [slow, extra-fast, middle]，中位数 = middle。
    生产走 gpu_profile.run_benchmark。"""
    from dub_align_studio.bulk_dub.gpu_profile import (
        LadderRunResult, run_benchmark,
    )
    seq = [
        LadderRunResult(1, 1, 0, 0, 0, 0, 0, 1.0, 1.0, 1.0, 100.0, 2400.0, True),
        LadderRunResult(1, 1, 0, 0, 0, 0, 0, 2.0, 1.0, 1.0, 100.0, 2400.0, True),
        LadderRunResult(1, 1, 0, 0, 0, 0, 0, 0.5, 1.0, 1.0, 900.0, 21600.0, True),
        LadderRunResult(1, 1, 0, 0, 0, 0, 0, 1.0, 1.0, 1.0, 300.0, 7200.0, True),
    ]
    call_idx = [0]
    def _fake(ctx, concurrency, *, cancel_event, per_job_timeout,
              warmup=False, expected_seconds=None, duration_tolerance=0.4,
              registry=None):
        r = seq[call_idx[0]]
        call_idx[0] += 1
        return r

    monkeypatch.setattr("dub_align_studio.bulk_dub.gpu_profile.run_one_ladder_step",
                         _fake)
    sample = tmp_path / "s.mp4"
    if _has_ffmpeg():
        _make_real_mp4(sample, seconds=0.3)
    else:
        pytest.skip("ffmpeg 缺失")
    tts = tmp_path / "t.wav"
    gpu_profile.make_silent_wav(tts, seconds=1.0)
    cap = run_benchmark(
        ffmpeg="ffmpeg", sample_video=sample, sample_tts_audio=tts,
        ladder=[1], warmup_rounds=1, measured_rounds=3,
    )
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
              warmup=False, expected_seconds=None, duration_tolerance=0.4,
              registry=None):
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
    assert cap.hw_failures_total == 6, \
        f"hw_failures_total 应等于 hardware_errors 累加，实际 {cap.hw_failures_total}"


# ================================================================
# 13. finalize_leader_success=False — 走真实 _process_video
# ================================================================


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg 不可用")
def test_fix2_13_no_success_count_on_finalize_fail(tmp_path, monkeypatch):
    """R14-FIX-3 P1-3：真实 TaskStore + Scheduler；mock render_single 成功；
    mock finalize_leader_success 返回 (False, 0)；调用真实 _process_video；
    断言 logical/physical 不增加，commit_failed 增加。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    # 不 start scheduler；直接调 _process_video
    inp = tmp_path / "v.mp4"; _make_real_mp4(inp, 0.3)
    b = store.create_batch("t", str(tmp_path),
                             {"output_dir": str(tmp_path)})
    tid = store.add_task(
        batch_id=b, excel_row=2,
        input_video=str(inp), text="t", fingerprint="fp",
        voice_id="v", voice_name="V", speed=1.0,
        keep_original_audio=False,
        params_snapshot={"output_dir": str(tmp_path),
                          "encoder_preference": "auto",
                          "zoom_percent": 130,
                          "crf": 20, "preset": "medium"},
    )
    # 把 status 直接推到 video_running（模拟 TTS 已完成）
    store.try_advance_status(tid, from_status=STATUS_PENDING,
                              to_status=STATUS_TTS_DONE,
                              tts_duration=1.0)
    store.try_advance_status(tid, from_status=STATUS_TTS_DONE,
                              to_status=STATUS_VIDEO_RUNNING)
    # 建 staging
    row = store.get(tid)
    staging = tmp_path / "stg"; staging.mkdir()
    store.update(tid, staging_dir=str(staging))
    (staging / "tts.wav").write_bytes(b"x" * 128)

    # mock render_single 成功
    fake_out = tmp_path / "out.mp4"
    _make_real_mp4(fake_out, 0.3)
    fake_result = vp.RenderResult(
        output_path=str(fake_out), final_duration=0.3,
        concat_duration=0.6, tts_duration=1.0,
        warnings=[], encoder_used="libx264", hw_fallback_used=False,
    )
    monkeypatch.setattr(vp, "render_single", lambda **kw: fake_result)
    # 让 mark_output_committed 成功但 finalize_leader_success 返 (False, 0)
    monkeypatch.setattr(store, "mark_output_committed",
                         lambda *a, **kw: True)
    monkeypatch.setattr(store, "finalize_leader_success",
                         lambda *a, **kw: (False, 0))
    # 触发真实 _process_video
    row_fresh = store.get(tid)
    sched._process_video(row_fresh)
    # 断言：logical/physical 均不增加；commit_failed +1
    assert len(sched.metrics.finished_times) == 0, \
        "ok=False 不能增加 logical finished_times"
    assert len(sched.metrics.physical_video_finished_times) == 0, \
        "ok=False 不能增加 physical_video_finished_times"
    assert sched.metrics.physical_render_succeeded_commit_failed == 1


# ================================================================
# 14. coordinator resize 失败下一周期重试 — 走真实 coordinator
# ================================================================


def test_fix2_14_coordinator_retries_after_resize_failure(tmp_path, monkeypatch):
    """R14-FIX-3 P1-4：真实 coordinator——第一次 resize_pools 抛错；
    下一周期立即重试（note_resize_failed 清零 last_resize_at）并成功。"""
    import dub_align_studio.bulk_dub.concurrency_controller as cc
    monkeypatch.setattr(cc, "RESIZE_MIN_INTERVAL_S", 0.1)
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched._coordinator_interval = 0.05
    sched.controller.set_profile_recommended(4)
    # 造 backlog：coordinator 才会 ramp-up
    b = store.create_batch("bl", str(tmp_path),
                             {"output_dir": str(tmp_path)})
    for i in range(5):
        tid = store.add_task(
            batch_id=b, excel_row=i + 2,
            input_video=str(tmp_path / f"v{i}.mp4"), text="t",
            fingerprint=f"fp{i}", voice_id="v", voice_name="V",
            speed=1.0, keep_original_audio=False,
            params_snapshot={"output_dir": str(tmp_path)},
        )
        store.try_advance_status(tid, from_status=STATUS_PENDING,
                                  to_status=STATUS_TTS_DONE)
    resize_calls: list[int] = []
    real_resize = sched.resize_pools

    def _flaky_resize(*, tts=None, video=None, wait_seconds=3.0):
        resize_calls.append(int(video or 0))
        if len(resize_calls) == 1:
            raise RuntimeError("first-attempt boom")
        return real_resize(tts=tts, video=video, wait_seconds=wait_seconds)

    monkeypatch.setattr(sched, "resize_pools", _flaky_resize)
    sched.pause()
    sched.start()
    try:
        # 等 coordinator 至少 2 次尝试并成功
        deadline = time.time() + 8.0
        while time.time() < deadline:
            snap = sched.snapshot()
            if len(resize_calls) >= 2 and int(snap.get("video_alive") or 0) >= 4:
                break
            time.sleep(0.05)
        assert len(resize_calls) >= 2, \
            f"resize 失败后必须至少重试一次，实际 {len(resize_calls)}"
        assert int(sched.snapshot().get("video_alive") or 0) >= 4, \
            f"重试后 video_alive 应达到 4，实际 " \
            f"{sched.snapshot().get('video_alive')}"
    finally:
        sched.stop(3.0)


# ================================================================
# 15. profile encoder_preference 走真实 apply_gpu_profile
# ================================================================


def test_fix2_15_profile_encoder_preference_persisted_and_used(tmp_path, monkeypatch):
    """R14-FIX-3 P1-5：走真实 apply_gpu_profile；捕获
    detect_capability_metadata 的 encoder_preference 参数，断言使用 profile
    保存的值而非硬编码 'auto'。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    prof = gpu_profile.DeviceCapability(
        recommended_concurrency=1, encoder_name="h264_nvenc",
        encoder_preference="nvidia", device_fingerprint="fp_nv",
    )
    svc._gpu_profile = prof
    captured_prefs: list[str] = []

    def _capture(ffmpeg, encoder_preference="auto", **kw):
        captured_prefs.append(encoder_preference)
        return gpu_profile.DeviceCapability(device_fingerprint="fp_nv")

    monkeypatch.setattr(gpu_profile, "detect_capability_metadata", _capture)
    monkeypatch.setattr(gpu_profile, "save_state", lambda payload: None)

    svc.apply_gpu_profile()
    assert "nvidia" in captured_prefs, \
        f"apply 必须用 profile 保存的 encoder_preference 探测，实际 {captured_prefs}"

    # 同时验证指纹算法把 encoder_preference 纳入
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
    assert fp_cpu != fp_auto, "显式 cpu 与 auto 必须落在不同指纹"
    svc.stop(3.0)


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


# ================================================================
# R14-FIX-3 新增：并发/异常恢复/进程隔离/DB+active/timeout+1/ladder 400
# ================================================================


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg 不可用")
def test_fix3_a_concurrent_start_benchmark_atomic_cas(tmp_path, monkeypatch):
    """新增 5：两个线程并发 start_benchmark，只允许一个进入；样本和
    cancel event 不串。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    sample_a = tmp_path / "a.mp4"; _make_real_mp4(sample_a, 0.3)
    sample_b = tmp_path / "b.mp4"; _make_real_mp4(sample_b, 0.3)

    started = threading.Event()
    proceed = threading.Event()

    def _slow(**kwargs):
        started.set()
        cancel_event = kwargs.get("cancel_event")
        while not proceed.is_set():
            if cancel_event is not None and cancel_event.is_set():
                raise gpu_profile.BenchmarkCancelled("cancelled")
            time.sleep(0.05)
        return gpu_profile.DeviceCapability(
            recommended_concurrency=1, encoder_name="libx264",
        )

    monkeypatch.setattr(gpu_profile, "run_benchmark", _slow)
    monkeypatch.setattr(gpu_profile, "save_profile", lambda cap: None)

    results: dict[str, Exception | dict] = {}

    def _try_start(name: str, sample: Path) -> None:
        try:
            r = svc.start_benchmark(sample_video=str(sample),
                                     exclusive_wait_seconds=3.0)
            results[name] = r
        except Exception as exc:  # noqa: BLE001
            results[name] = exc

    t1 = threading.Thread(target=_try_start, args=("A", sample_a))
    t2 = threading.Thread(target=_try_start, args=("B", sample_b))
    t1.start()
    # R14-FIX-4：慢机鲁棒——**poll** 直到 t1 真的进入 preparing/running，
    # 而不是裸 sleep(0.05) 押注 CPU 调度
    assert _wait_until(
        lambda: svc.benchmark_status().get("state") in ("preparing", "running")
                 or ("A" in results),
        timeout=10.0, interval=0.02,
    )
    t2.start()
    t1.join(timeout=15.0)
    t2.join(timeout=15.0)
    # 有且仅有一个成功
    success = [k for k, v in results.items() if isinstance(v, dict)]
    fail = [k for k, v in results.items() if isinstance(v, Exception)]
    assert len(success) == 1 and len(fail) == 1, \
        f"并发只允许一个成功，实际 success={success} fail={fail}"
    # 生成号唯一
    winner = results[success[0]]
    assert winner["state"] == "running"
    assert winner["generation"] >= 1
    proceed.set()
    if svc._benchmark_thread is not None:
        svc._benchmark_thread.join(timeout=15.0)
    svc.stop(15.0)


def test_fix3_b_silent_wav_error_restores_exclusive(tmp_path, monkeypatch):
    """新增 6：make_silent_wav 抛错后 exclusive/pause/running 全恢复。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    sample = tmp_path / "s.mp4"
    if _has_ffmpeg():
        _make_real_mp4(sample, 0.3)
    else:
        pytest.skip("ffmpeg 缺失")

    def _boom(target, seconds=10.0, sample_rate=16000):
        raise OSError("silent wav 落盘失败")

    monkeypatch.setattr(gpu_profile, "make_silent_wav", _boom)
    monkeypatch.setattr(gpu_profile, "save_profile", lambda cap: None)

    # 用 slow run_benchmark 保证不会真跑
    def _no_run(**kw):
        raise AssertionError("silent 写失败后 run_benchmark 不应被调")

    monkeypatch.setattr(gpu_profile, "run_benchmark", _no_run)
    svc.start_benchmark(sample_video=str(sample), exclusive_wait_seconds=3.0)
    if svc._benchmark_thread is not None:
        svc._benchmark_thread.join(timeout=5.0)
    st = svc.benchmark_status()
    assert st["state"] == "error", f"silent wav 失败必须落 error，实际 {st}"
    assert st["running"] is False
    assert svc._scheduler.is_benchmark_exclusive() is False, \
        "exclusive 必须恢复"
    assert svc._scheduler.is_paused() is False, "pause 必须恢复到 False"
    svc.stop(3.0)


def test_fix3_c_thread_start_error_restores_exclusive(tmp_path, monkeypatch):
    """新增 7：Thread.start() 抛错后 exclusive/pause/running 全恢复。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    sample = tmp_path / "s.mp4"
    if _has_ffmpeg():
        _make_real_mp4(sample, 0.3)
    else:
        pytest.skip("ffmpeg 缺失")
    # 先让 scheduler 启动完成（worker 线程已 spawn）
    svc._ensure_scheduler()

    real_thread = threading.Thread

    class _BadBenchThread(real_thread):
        def start(self):
            # 只有 benchmark 线程会命中——名字前缀 "gpu-benchmark"
            if self.name.startswith("gpu-benchmark"):
                raise RuntimeError("cannot start thread")
            return super().start()

    monkeypatch.setattr("dub_align_studio.bulk_dub.service.threading.Thread",
                         _BadBenchThread)
    with pytest.raises(ValidationError, match="benchmark 线程启动失败"):
        svc.start_benchmark(sample_video=str(sample),
                             exclusive_wait_seconds=3.0)
    st = svc.benchmark_status()
    assert st["state"] == "error"
    assert st["running"] is False
    assert svc._scheduler.is_benchmark_exclusive() is False
    assert svc._scheduler.is_paused() is False
    svc.stop(3.0)


def test_fix3_d_stop_does_not_kill_non_benchmark_procs(tmp_path, monkeypatch):
    """新增 8：service.stop 时构造一个"不属于 benchmark"的活动子进程，
    断言它没有被终止。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    # 显式启动一个"外部"子进程（通过 popen_silent 登记进全局 _ACTIVE）
    from integrated_workbench.proc import popen_silent
    external = popen_silent(["sleep", "5"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    try:
        # 服务尚未启动 benchmark；stop() 现在不应误杀 external
        svc.stop(wait_seconds=1.0)
        # R14-FIX-4：慢机鲁棒——**持续**验证 2 秒内 external 存活
        assert _stays_true(
            lambda: external.poll() is None,
            duration=2.0, interval=0.05,
        ), "service.stop 不能杀死不属于 benchmark 的子进程"
    finally:
        try:
            external.terminate()
            external.wait(timeout=3)
        except Exception:  # noqa: BLE001
            pass


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg 不可用")
def test_fix3_e_stop_kills_benchmark_own_procs(tmp_path, monkeypatch):
    """新增 9：benchmark 自己的子进程会被终止并 wait。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    sample = tmp_path / "s.mp4"; _make_real_mp4(sample, 0.3)

    started = threading.Event()
    proc_holder: dict = {}

    def _bench_that_spawns(**kw):
        registry = kw.get("registry")
        # 通过 registry 走同一 popen 路径
        from dub_align_studio.bulk_dub.gpu_profile import _popen_bench
        proc = _popen_bench(["sleep", "30"], registry,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
        proc_holder["p"] = proc
        started.set()
        # 等 cancel
        while not kw["cancel_event"].is_set():
            time.sleep(0.05)
        raise gpu_profile.BenchmarkCancelled("cancelled")

    monkeypatch.setattr(gpu_profile, "run_benchmark", _bench_that_spawns)
    monkeypatch.setattr(gpu_profile, "save_profile", lambda cap: None)
    svc.start_benchmark(sample_video=str(sample), exclusive_wait_seconds=15.0)
    assert started.wait(timeout=15.0)
    p = proc_holder["p"]
    assert p.poll() is None, "benchmark 子进程应当在跑"
    ok = svc.stop(wait_seconds=15.0)
    assert ok is True
    # R14-FIX-4：慢机鲁棒——poll 直到 p 真的死；不再裸 sleep(0.2)
    assert _wait_until(lambda: p.poll() is not None,
                         timeout=10.0, interval=0.05), \
        "stop 后 benchmark 自己的子进程必须被收尸"


def test_fix3_f_benchmark_waits_for_scheduler_active(tmp_path, monkeypatch):
    """新增 10：DB 状态为 cancelling / 已提交但 scheduler active_video=1 时，
    benchmark 不得进入。用 active_counts() fake 返回非 0。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(),
                       tts_backend=MockTtsBackend())
    # 关键：让 DB 报 running=0，但 active_counts 报 video=1
    monkeypatch.setattr(store, "count_by_status",
                         lambda batch=None: {"tts_running": 0,
                                              "video_running": 0})
    monkeypatch.setattr(sched, "active_counts", lambda: (0, 1))
    ok = sched.enter_benchmark_exclusive(wait_seconds=0.5)
    assert ok is False, \
        "scheduler active_video=1 时 benchmark 不得进入 exclusive"
    assert sched.is_benchmark_exclusive() is False


@pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg 不可用")
def test_fix3_g_single_hw_timeout_counts_once(tmp_path, monkeypatch):
    """新增 11：单次硬件 timeout consecutive failure 精确 +1，不是 +2。"""
    from dub_align_studio.bulk_dub.circuit_breaker import CircuitBreaker  # noqa
    # 用真的 EncoderCircuitBreaker
    br = EncoderCircuitBreaker("h264_nvenc", failure_threshold=10)

    def _fake_run(cmd, *, cancel_flag, timeout, capture_stderr=True):
        raise vp.VideoError(f"ffmpeg 超时 ({int(timeout)}s)")

    monkeypatch.setattr(vp, "_run_ffmpeg_cancellable", _fake_run)

    ctrl = ConcurrencyController(profile_recommended=1)

    def _hw_cb(name: str) -> None:
        # 模拟生产 callback：走 controller.record_hardware_failure
        ctrl.record_hardware_failure(name)

    inp = tmp_path / "in.mp4"; _make_real_mp4(inp, 0.3)
    tts = tmp_path / "t.wav"; gpu_profile.make_silent_wav(tts, 1.0)
    outp = tmp_path / "out.mp4"; stg = tmp_path / "stg"; stg.mkdir()
    hw = EncoderProbe("nvidia", "h264_nvenc", [], False, "fake")
    # 让 breaker 从 controller 中取——callback 路径会 record 一次
    ctrl_br = ctrl.breaker_for("h264_nvenc")
    assert ctrl_br is not br
    with pytest.raises(vp.VideoError):
        vp.render_single(
            input_video=inp, tts_audio=tts,
            reserved_output=outp, staging_dir=stg,
            encoder=hw, allow_hw_fallback=False,
            timeout=1.0, breaker=ctrl_br,
            hw_failure_cb=_hw_cb,
        )
    # 单次 timeout → ctrl.breakers["h264_nvenc"] consec_failures 应为 1（不是 2）
    assert ctrl_br._consec_failures == 1, \
        f"单次 hw timeout 必须精确 +1，实际 {ctrl_br._consec_failures}"


def test_fix3_h_api_rejects_bad_ladder(tmp_path, monkeypatch):
    """新增 12：ladder [1000]/负数/超长/空 → 真实 API 返回 400。"""
    monkeypatch.setenv("DUB_ALIGN_STUDIO_DATA_ROOT", str(tmp_path))
    svc = BulkDubService(store=TaskStore(tmp_path / "q.sqlite3"),
                          tts_backend=MockTtsBackend())
    import json as _json
    sample = tmp_path / "s.mp4"
    if _has_ffmpeg():
        _make_real_mp4(sample, 0.3)
    else:
        sample.write_bytes(b"x" * 2048)

    def _post(body: dict) -> int:
        ok, status, body_bytes, ctype = bulk_api.dispatch_post(
            "/api/bulk_dub/gpu/start_benchmark", {},
            _json.dumps(body).encode(), "application/json",
            service=svc,
        )
        return status

    # 超上限
    assert _post({"sample_video": str(sample), "ladder": [1000]}) == 400
    # 负数
    assert _post({"sample_video": str(sample), "ladder": [1, -2, 3]}) == 400
    # 空
    assert _post({"sample_video": str(sample), "ladder": []}) == 400
    # 超长（>16）
    assert _post({"sample_video": str(sample),
                    "ladder": list(range(1, 20))}) == 400
    # 非数组
    assert _post({"sample_video": str(sample), "ladder": "1,2,3"}) == 400
    svc.stop(3.0)
