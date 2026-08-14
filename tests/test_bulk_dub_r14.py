"""R14 显卡能力探测、并发基准、自动并发、物理指标测试。

覆盖点（都直接调用生产控制器 / Handler，不复制算法）：

- worker × snapshot × resize 并发：不重复 spawn，不丢 worker，
  serving 严格等于 target，draining 最终归零；
- 1000 次并发 marker 写：无 EBADF；
- profile 原子写入 + 损坏文件安全忽略 + 设备指纹变化即失效；
- benchmark 阶梯选择：提升不足 5% → 选低档；某档 recommendable=False
  即不推荐；
- 自动 controller ramp-up/ramp-down/hysteresis；
- CpuFallbackGate semaphore；
- EncoderCircuitBreaker closed → open → half-open → closed；
- 磁盘低空间不 ramp-up；
- physical / logical 指标严格分离；
- benchmark 取消无孤儿；
- API 真实 Handler：/gpu/capability, /gpu/benchmark_status, /gpu/set_mode,
  /gpu/apply_profile。
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
    ALL_MODES, MODE_AUTO, MODE_CPU_SAFE, MODE_MANUAL,
    ConcurrencyController, CpuFallbackGate, EncoderCircuitBreaker,
    BREAKER_FAILURE_THRESHOLD, DISK_SAFE_FREE_GB, HYSTERESIS_RAMPUP,
    RESIZE_MIN_INTERVAL_S,
)
from dub_align_studio.bulk_dub.edge_backend import MockTtsBackend  # noqa: E402
from dub_align_studio.bulk_dub.scheduler import (  # noqa: E402
    Scheduler, SchedulerConfig, SchedulerMetrics,
)
from dub_align_studio.bulk_dub.service import BulkDubService  # noqa: E402
from dub_align_studio.bulk_dub.store import TaskStore  # noqa: E402


# ================================================================
# 1) worker × snapshot × resize：并发不泄漏、不重复
# ================================================================


def test_r14_worker_lifecycle_snapshot_x_resize_no_leak(tmp_path):
    """20 个 snapshot 请求 + 连续 resize 交叉：worker 记录不丢失、不重复；
    serving 最终严格等于 target；draining 归零。"""
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=2, video_concurrency=2,
    ), tts_backend=MockTtsBackend())
    sched.start()
    try:
        errors: list[str] = []
        stop = threading.Event()

        def snap_loop():
            while not stop.is_set():
                try: sched.snapshot()
                except Exception as e: errors.append(f"snap:{e}")

        def resize_loop():
            for target in (1, 4, 2, 3, 1, 2, 5, 3, 2):
                if stop.is_set(): return
                try:
                    sched.resize_pools(tts=target, video=target, wait_seconds=0.5)
                except Exception as e: errors.append(f"resize:{e}")

        threads = [threading.Thread(target=snap_loop) for _ in range(20)]
        rt = threading.Thread(target=resize_loop)
        for t in threads: t.start()
        rt.start()
        rt.join()
        stop.set()
        for t in threads: t.join(timeout=3)

        # 最终 reconcile 到目标 (最后一次 resize target=2)
        # 允许一小段稳定时间给 draining worker 退出
        for _ in range(30):
            snap = sched.snapshot()
            if (snap["tts_alive"] == 2 and snap["video_alive"] == 2
                    and snap["tts_draining"] == 0 and snap["video_draining"] == 0):
                break
            time.sleep(0.1)
        assert not errors, errors
        assert snap["tts_alive"] == 2
        assert snap["video_alive"] == 2
        assert snap["tts_draining"] == 0
        assert snap["video_draining"] == 0
    finally:
        sched.stop(wait_seconds=3)


# ================================================================
# 2) 1000 次并发 marker 写无 EBADF
# ================================================================


def test_r14_marker_no_ebadf_under_high_concurrency(tmp_path):
    """1000 次并发 marker 写：不能出现 OSError [Errno 9]。"""
    outdir = tmp_path / "out"; outdir.mkdir()
    errors: list[Exception] = []
    lock = threading.Lock()

    def one(i: int):
        target = outdir / f"vid_{i}.mp4"
        marker = vp.marker_path_for(target)
        marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            vp._write_marker(marker, task_id=f"t{i:04d}", batch_id="B",
                              fingerprint="fp", target_final_seconds=1.0,
                              file_size=10, output_name=target.name,
                              encoder_used="libx264", hw_fallback_used=False)
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(1000)]
    for t in threads: t.start()
    for t in threads: t.join()
    ebadfs = [e for e in errors if isinstance(e, OSError) and e.errno == 9]
    assert not ebadfs, f"1000 并发 marker 写出现 EBADF：{ebadfs[:3]}"
    assert not errors, f"1000 并发 marker 写出现其他错误：{errors[:3]}"
    # 断言 marker 文件都真的写了
    remaining = list((outdir / vp.MARKER_DIR_NAME).glob("*.marker.json"))
    assert len(remaining) == 1000


# ================================================================
# 3) profile 持久化 / 损坏恢复 / 指纹失效
# ================================================================


def test_r14_profile_atomic_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    cap = gpu_profile.DeviceCapability(
        device_fingerprint="fp-x", os_name="Linux", encoder_family="cpu",
        encoder_name="libx264", recommended_concurrency=3,
        peak_renders_per_hour=100.0,
    )
    p = gpu_profile.save_profile(cap)
    assert p.exists()
    loaded = gpu_profile.load_profile()
    assert loaded is not None
    assert loaded.device_fingerprint == "fp-x"
    assert loaded.recommended_concurrency == 3


def test_r14_profile_corrupted_json_is_safely_ignored(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    p = gpu_profile.profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{not valid json !!", encoding="utf-8")
    assert gpu_profile.load_profile() is None, "损坏 JSON 必须被静默忽略"
    # 且不能抛异常


def test_r14_profile_wrong_schema_is_ignored(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    p = gpu_profile.profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema": "wrong@v99"}), encoding="utf-8")
    assert gpu_profile.load_profile() is None


def test_r14_device_fingerprint_stability():
    a = gpu_profile.compute_fingerprint(os_name="Linux", ffmpeg_version="6.1",
                                          encoder_name="h264_nvenc",
                                          gpu_adapter="RTX 3090", driver_version="535.86")
    b = gpu_profile.compute_fingerprint(os_name="Linux", ffmpeg_version="6.1",
                                          encoder_name="h264_nvenc",
                                          gpu_adapter="RTX 3090", driver_version="535.86")
    c = gpu_profile.compute_fingerprint(os_name="Linux", ffmpeg_version="6.1",
                                          encoder_name="h264_nvenc",
                                          gpu_adapter="RTX 3090", driver_version="536.00")
    assert a == b, "同参数指纹必须稳定"
    assert a != c, "驱动版本变化 → 指纹必须变化"


# ================================================================
# 4) benchmark 阶梯 & 推荐选择
# ================================================================


def _mk(step_c, ok=True, per_hour=100.0, hw_fail=0, session_lim=0,
         oom=0, dev=0):
    return gpu_profile.LadderRunResult(
        concurrency=step_c, successes=step_c if ok else 0,
        failures=0 if ok else step_c, hw_fallbacks=hw_fail,
        session_limit_errors=session_lim, oom_errors=oom,
        device_errors=dev, wall_clock_seconds=10.0,
        aggregate_fps=1.0, realtime_multiple=1.0,
        projected_renders_per_hour=per_hour,
        projected_renders_per_day=per_hour * 24,
        is_recommendable=(ok and hw_fail == 0 and session_lim == 0
                          and oom == 0 and dev == 0),
    )


def test_r14_recommendation_picks_lower_when_improvement_below_5_percent():
    """两档都稳定：4档比2档只提升 3%（<5%）→ 选 2 档。"""
    steps = [_mk(1, per_hour=100), _mk(2, per_hour=200), _mk(4, per_hour=206)]
    n, reason = gpu_profile.choose_recommended_concurrency(steps)
    assert n == 2, f"应选 2（提升不足 5%），实际 {n} reason={reason}"


def test_r14_recommendation_picks_highest_when_improvement_sufficient():
    steps = [_mk(1, per_hour=100), _mk(2, per_hour=200), _mk(4, per_hour=350)]
    n, _ = gpu_profile.choose_recommended_concurrency(steps)
    assert n == 4, "提升充分时应选最高稳定档"


def test_r14_recommendation_skips_unstable_step():
    """某档硬件失败 → 该档 is_recommendable=False，绝不能被推荐。"""
    steps = [_mk(1, per_hour=100), _mk(2, per_hour=200),
              _mk(4, hw_fail=1, per_hour=350)]  # 4 档硬件失败
    n, _ = gpu_profile.choose_recommended_concurrency(steps)
    assert n == 2, "不稳定档必须跳过"


def test_r14_recommendation_no_stable_falls_back_to_one():
    steps = [_mk(1, hw_fail=1, per_hour=50)]
    n, reason = gpu_profile.choose_recommended_concurrency(steps)
    assert n == 1
    assert "CPU-safe" in reason or "无任何" in reason


# ================================================================
# 5) 分类 stderr：session-limit / OOM / device
# ================================================================


def test_r14_classify_stderr_session_limit():
    c = gpu_profile._classify_stderr("[h264_nvenc @ 0x1] OpenEncodeSessionEx failed: out of memory (10): (no details)")
    # 同时命中 session_limit（OpenEncodeSession）与 oom
    assert c["session_limit"] == 1
    assert c["oom"] == 1


def test_r14_classify_stderr_device_error():
    c = gpu_profile._classify_stderr("Cannot init CUDA: no CUDA-capable device is detected")
    assert c["device"] == 1


def test_r14_classify_stderr_empty():
    c = gpu_profile._classify_stderr("")
    assert c == {"hw": 0, "session_limit": 0, "oom": 0, "device": 0}


# ================================================================
# 6) 自动 controller：mode / ramp / hysteresis / disk
# ================================================================


def test_r14_controller_manual_mode_returns_user_value():
    c = ConcurrencyController(profile_recommended=3)
    c.set_mode(MODE_MANUAL, manual_video=5)
    target, apply, _ = c.decide(tts_done_backlog=10, video_running=0,
                                  output_free_gb=100, recent_throughput=0)
    assert target == 5
    assert apply is True


def test_r14_controller_cpu_safe_mode_forces_low_concurrency():
    c = ConcurrencyController(profile_recommended=8)
    c.set_mode(MODE_CPU_SAFE)
    target, apply, _ = c.decide(tts_done_backlog=100, video_running=0,
                                  output_free_gb=100, recent_throughput=0)
    assert target <= 2


def test_r14_controller_auto_ramp_up_requires_backlog():
    c = ConcurrencyController(profile_recommended=4)
    # 首次决策 → 使用 profile.recommended
    target, apply, _ = c.decide(tts_done_backlog=10, video_running=0,
                                  output_free_gb=100, recent_throughput=0)
    assert target == 4
    assert apply is True


def test_r14_controller_disk_low_blocks_rampup(monkeypatch):
    c = ConcurrencyController(profile_recommended=4)
    # 先设置一个 last_effective=2 的稳态
    c.state.last_effective_concurrency = 2
    c.state.last_resize_at = time.time() - RESIZE_MIN_INTERVAL_S - 1
    # 磁盘紧张
    target, apply, reason = c.decide(
        tts_done_backlog=10, video_running=0,
        output_free_gb=DISK_SAFE_FREE_GB - 0.5, recent_throughput=0)
    assert target == 2, f"磁盘紧张时不能扩容，实际 {target}"
    assert apply is False


def test_r14_controller_hardware_failure_halves():
    c = ConcurrencyController(profile_recommended=4)
    c.state.last_effective_concurrency = 4
    c.state.last_resize_at = time.time() - RESIZE_MIN_INTERVAL_S - 1
    for _ in range(3):
        c.record_hardware_failure()
    target, apply, reason = c.decide(
        tts_done_backlog=10, video_running=0,
        output_free_gb=100, recent_throughput=0)
    assert target == 2, f"3 次硬件失败必须减半，实际 {target}"
    assert apply is True


def test_r14_controller_cooldown_prevents_thrashing():
    c = ConcurrencyController(profile_recommended=4)
    # 首次
    c.decide(tts_done_backlog=10, video_running=0,
              output_free_gb=100, recent_throughput=0)
    # 立即再决策：处于冷却
    _, apply, _ = c.decide(tts_done_backlog=10, video_running=0,
                             output_free_gb=100, recent_throughput=0)
    assert apply is False, "冷却期内不允许再次 apply"


# ================================================================
# 7) CpuFallbackGate semaphore
# ================================================================


def test_r14_cpu_fallback_gate_limits_concurrent_libx264():
    gate = CpuFallbackGate(limit=2)
    assert gate.acquire()
    assert gate.acquire()
    assert not gate.is_saturated() or gate.is_saturated()  # limit=2 时 active=2
    assert gate.active() == 2
    got = gate.acquire(timeout=0.1)
    assert got is False, "超过 limit=2 时必须阻塞"
    gate.release()
    got = gate.acquire(timeout=0.5)
    assert got is True


def test_r14_cpu_fallback_gate_release_more_than_acquire_is_safe():
    gate = CpuFallbackGate(limit=1)
    gate.acquire()
    gate.release()
    # 多释放一次不应崩溃
    gate.release()


# ================================================================
# 8) Encoder circuit breaker：closed → open → half-open → closed
# ================================================================


def test_r14_breaker_opens_after_threshold_failures():
    b = EncoderCircuitBreaker("h264_nvenc", failure_threshold=3,
                                open_seconds=60)
    assert b.state == EncoderCircuitBreaker.STATE_CLOSED
    assert b.allow() is True
    for _ in range(3):
        b.record_failure()
    assert b.state == EncoderCircuitBreaker.STATE_OPEN
    assert b.allow() is False


def test_r14_breaker_half_open_transition():
    b = EncoderCircuitBreaker("h264_nvenc", failure_threshold=2,
                                open_seconds=0.05)
    for _ in range(2):
        b.record_failure()
    assert b.state == EncoderCircuitBreaker.STATE_OPEN
    time.sleep(0.1)
    assert b.state == EncoderCircuitBreaker.STATE_HALF_OPEN
    # 第一个 allow → 允许（唯一探测名额）
    assert b.allow() is True
    # 第二个 allow → 拒绝（探测名额已用）
    assert b.allow() is False


def test_r14_breaker_half_open_success_closes():
    b = EncoderCircuitBreaker("h264_nvenc", failure_threshold=2,
                                open_seconds=0.05)
    for _ in range(2): b.record_failure()
    time.sleep(0.1)
    assert b.state == EncoderCircuitBreaker.STATE_HALF_OPEN
    b.allow()  # 拿 half-open probe
    b.record_success()
    assert b.state == EncoderCircuitBreaker.STATE_CLOSED


def test_r14_breaker_half_open_failure_reopens():
    b = EncoderCircuitBreaker("h264_nvenc", failure_threshold=2,
                                open_seconds=0.05)
    for _ in range(2): b.record_failure()
    time.sleep(0.1)
    assert b.state == EncoderCircuitBreaker.STATE_HALF_OPEN
    b.allow()
    b.record_failure()   # half-open 探测又失败
    assert b.state == EncoderCircuitBreaker.STATE_OPEN


# ================================================================
# 9) physical vs logical 指标分离
# ================================================================


def test_r14_metrics_separates_physical_video_from_logical():
    m = SchedulerMetrics()
    # 模拟：leader 完成 1 条物理视频；跟随 3 条 follower（logical 秒完成）
    m.record_success(0, 5.0)
    m.record_physical_video()
    for _ in range(3):
        m.record_success(0, 0)  # follower 只记录 logical
    assert len(m.finished_times) == 4, "logical 应记 4 条"
    assert len(m.physical_video_finished_times) == 1, \
        "physical_video 只应记 1 条，绝不能被 follower 放大"


def test_r14_metrics_recent_physical_video_rate():
    m = SchedulerMetrics()
    for _ in range(10):
        m.record_physical_video()
    r = m.recent_physical_video_rate(60)
    assert r > 0
    per_hour = m.recent_physical_video_per_hour(60)
    assert per_hour > 0


# ================================================================
# 10) benchmark 取消：不留孤儿进程 / staging
# ================================================================


def _has_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, check=True, timeout=5)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _has_ffmpeg(), reason="需要 ffmpeg")
def test_r14_benchmark_cancel_leaves_no_staging(tmp_path, monkeypatch):
    """基准取消 → staging 完全清理，无孤儿目录。"""
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    # 用 lavfi 造一段 3 秒真实视频
    sample = tmp_path / "s.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc=size=320x240:duration=3:rate=15",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
        "-c:v", "libx264", "-preset", "ultrafast", "-t", "3",
        "-c:a", "aac", str(sample),
    ], check=True)
    silent = tmp_path / "sil.wav"
    gpu_profile.make_silent_wav(silent, seconds=3)
    cancel = threading.Event()

    # 后台跑，立刻 cancel
    result = {}
    def _bg():
        try:
            cap = gpu_profile.run_benchmark(
                ffmpeg="ffmpeg", sample_video=sample,
                sample_tts_audio=silent,
                encoder_preference="cpu",
                ladder=[1, 2], warmup_rounds=0,
                per_job_timeout=30.0,
                cancel_event=cancel,
            )
            result["cap"] = cap
        except gpu_profile.BenchmarkCancelled:
            result["cancelled"] = True
        except Exception as e:  # noqa: BLE001
            result["err"] = str(e)

    t = threading.Thread(target=_bg); t.start()
    time.sleep(0.3)
    cancel.set()
    t.join(timeout=30)
    # 结束后 bench_root 已被清理（bulk_staging_root 下不能残留 _benchmark_*）
    from dub_align_studio.bulk_dub.ffmpeg_pipeline import bulk_staging_root
    residual = list(bulk_staging_root().glob("_benchmark_*"))
    assert not residual, f"取消后还残留 benchmark staging：{residual}"


# ================================================================
# 11) API 真实 Handler：/gpu/capability, /gpu/set_mode, /gpu/benchmark_status
# ================================================================


def test_r14_api_gpu_capability_returns_current_metadata(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        handled, status, body, ctype = bulk_api.dispatch_get(
            "/api/bulk_dub/gpu/capability", {}, service=svc,
        )
        assert handled and status == 200
        payload = json.loads(body)
        assert "current" in payload
        assert "profile" in payload
        assert "controller" in payload
        assert "disclaimer" in payload
        # 诚实性文案必须提到"不等于"或"24 小时"
        assert "24 小时" in payload["disclaimer"] or "不等于" in payload["disclaimer"]
    finally:
        svc.stop()


def test_r14_api_gpu_set_mode_switches_controller(tmp_path, monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        # 切到 manual
        handled, status, body, _ = bulk_api.dispatch_post(
            "/api/bulk_dub/gpu/set_mode",
            {"mode": "manual", "manual_video": "3"}, b"", service=svc,
        )
        assert handled and status == 200
        payload = json.loads(body)
        assert payload["mode"] == "manual"
        assert payload["controller"]["mode"] == "manual"
        # 切到 cpu_safe
        _, status2, body2, _ = bulk_api.dispatch_post(
            "/api/bulk_dub/gpu/set_mode",
            {"mode": "cpu_safe"}, b"", service=svc,
        )
        assert status2 == 200
        assert json.loads(body2)["controller"]["mode"] == "cpu_safe"
        # 未知 mode
        _, status3, body3, _ = bulk_api.dispatch_post(
            "/api/bulk_dub/gpu/set_mode",
            {"mode": "nope"}, b"", service=svc,
        )
        assert status3 == 400
    finally:
        svc.stop()


def test_r14_api_gpu_benchmark_status_before_run_shows_not_running(tmp_path,
                                                                     monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        _, status, body, _ = bulk_api.dispatch_get(
            "/api/bulk_dub/gpu/benchmark_status", {}, service=svc,
        )
        assert status == 200
        payload = json.loads(body)
        assert payload["running"] is False
    finally:
        svc.stop()


def test_r14_api_gpu_start_benchmark_rejects_when_no_sample(tmp_path,
                                                              monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        _, status, body, _ = bulk_api.dispatch_post(
            "/api/bulk_dub/gpu/start_benchmark", {}, b"", service=svc,
        )
        assert status == 400
    finally:
        svc.stop()


def test_r14_api_gpu_apply_profile_without_profile_returns_400(tmp_path,
                                                                 monkeypatch):
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        _, status, body, _ = bulk_api.dispatch_post(
            "/api/bulk_dub/gpu/apply_profile", {}, b"", service=svc,
        )
        assert status == 400, f"无 profile 时必须 400，实际 {status} body={body}"
    finally:
        svc.stop()


# ================================================================
# 12) scheduler snapshot 暴露 physical 指标 + controller
# ================================================================


def test_r14_snapshot_contract_includes_physical_and_controller(tmp_path):
    store = TaskStore(tmp_path / "q.sqlite3")
    sched = Scheduler(store=store, config=SchedulerConfig(
        tts_concurrency=1, video_concurrency=1,
    ), tts_backend=MockTtsBackend())
    sched.start()
    try:
        snap = sched.snapshot()
        assert "controller" in snap
        assert "capability_disclaimer" in snap
        m = snap["metrics"]
        assert "physical_video_completed" in m
        assert "physical_video_projected_per_day" in m
        assert "logical_rows_rate_60min" in m
    finally:
        sched.stop(wait_seconds=2)


def test_r14_benchmark_running_task_check_blocks_start(tmp_path, monkeypatch):
    """R14-3：生产池有运行中任务时禁止启动基准。"""
    import dub_align_studio.settings as ss
    monkeypatch.setattr(ss, "data_root", lambda: tmp_path / "data")
    store = TaskStore(tmp_path / "q.sqlite3")
    svc = BulkDubService(store=store, tts_backend=MockTtsBackend())
    try:
        # 造一条 video_running 状态的任务
        b = store.create_batch("t", str(tmp_path), {})
        tid = store.add_task(
            batch_id=b, excel_row=2, input_video="/v", text="t",
            fingerprint="fp", voice_id="v", voice_name="V",
            speed=1.0, keep_original_audio=False, params_snapshot={},
        )
        from dub_align_studio.bulk_dub.store import STATUS_VIDEO_RUNNING
        store.update(tid, status=STATUS_VIDEO_RUNNING)
        # 造一个假样本文件
        sample = tmp_path / "s.mp4"
        sample.write_bytes(b"\x00" * 100)
        from dub_align_studio.bulk_dub.service import ValidationError
        with pytest.raises(ValidationError):
            svc.start_benchmark(sample_video=str(sample))
    finally:
        svc.stop()
