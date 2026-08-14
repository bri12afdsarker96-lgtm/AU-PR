"""R14 视频池自动并发控制 + 背压 + 硬件故障保护。

分离于 Scheduler：Scheduler 关心"一个 worker 怎么跑一个任务"；这里关心
"整个视频池到底该有几个 worker"。用一个显式的 controller 决定放缩，避免
把这些跨阶段策略埋进 worker loop。

模式：
    - MODE_AUTO：按 profile.recommended_concurrency 起始；只在 TTS_DONE
        队列有 backlog 时才继续 ramp-up；连续硬件失败 → 立刻降档；
    - MODE_MANUAL：用户给固定 N；不自动调整；
    - MODE_CPU_SAFE：强制 libx264 单/双并发，避免 CPU fallback 风暴。

硬件保护：
    - CpuFallbackGate：全局 semaphore，同一时刻最多 N 个 libx264 回退跑；
        阻止 8 个 NVENC 全部回退成 8 个 libx264 把机器压垮；
    - EncoderCircuitBreaker：同一硬件编码器连续失败 N 次 → 打开 breaker，
        暂停硬件路径；一段时间后半开只放 1 个探测任务；成功恢复。

滞回与冷却：
    - RESIZE_MIN_INTERVAL_S：两次 resize 间最短间隔；
    - HYSTERESIS：吞吐必须变化 > 阈值才触发 ramp-up；避免抖动。

背压：
    - 磁盘空间低 → 拒绝 ramp-up；
    - CPU fallback 活跃数达到上限 → 不再放大；
    - 硬件失败率过高 → 降档并触发 breaker。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MODE_AUTO = "auto"
MODE_MANUAL = "manual"
MODE_CPU_SAFE = "cpu_safe"
ALL_MODES = frozenset({MODE_AUTO, MODE_MANUAL, MODE_CPU_SAFE})

# 两次自动 resize 之间的最短间隔（秒）——避免抖动
RESIZE_MIN_INTERVAL_S = 8.0

# ramp-up 时吞吐相对前一稳定值的最小提升，否则不放大
HYSTERESIS_RAMPUP = 0.05

# ramp-down 触发条件：单位时间硬件失败次数
FAILURE_WINDOW_S = 60.0

# 磁盘空间安全阈值——低于此不再 ramp-up（GB）
DISK_SAFE_FREE_GB = 2.0

# CPU fallback 全局并发上限
DEFAULT_CPU_FALLBACK_LIMIT = 2

# encoder breaker 阈值
BREAKER_FAILURE_THRESHOLD = 3
BREAKER_OPEN_SECONDS = 60.0
BREAKER_HALFOPEN_PROBE = 1


class CpuFallbackGate:
    """R14 CPU fallback 专用 semaphore。

    NVENC 编码时若运行时失败，会自动回退到 libx264。若同时 8 个 NVENC 会话
    全部回退，就是 8 个 libx264 同时抢 CPU，会把机器压垮。这个 gate 用
    一把 Semaphore 强制"同一时刻最多 N 个 libx264 回退"，多余任务等待。
    """

    def __init__(self, limit: int = DEFAULT_CPU_FALLBACK_LIMIT) -> None:
        self._limit = max(1, int(limit))
        self._sem = threading.BoundedSemaphore(self._limit)
        self._active = 0
        self._active_lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def acquire(self, timeout: float | None = None) -> bool:
        got = self._sem.acquire(timeout=timeout) if timeout \
            else self._sem.acquire()
        if got:
            with self._active_lock:
                self._active += 1
        return got

    def release(self) -> None:
        with self._active_lock:
            if self._active > 0:
                self._active -= 1
        try:
            self._sem.release()
        except ValueError:
            # bounded 情况：多释放一次会抛；忽略以保持 idempotent
            pass

    def active(self) -> int:
        with self._active_lock:
            return self._active

    def is_saturated(self) -> bool:
        return self.active() >= self._limit


class EncoderCircuitBreaker:
    """R14 硬件编码器断路器：连续失败达到阈值 → open，暂停硬件任务或降级到
    CPU-safe 并发；一段时间后 half-open，只放 1 个探测任务；成功 → closed。

    与 CpuFallbackGate 配合使用：breaker 打开时 controller 应把有效并发直接
    降到 1，并只走 libx264 路径。
    """

    STATE_CLOSED = "closed"
    STATE_OPEN = "open"
    STATE_HALF_OPEN = "half_open"

    def __init__(self, encoder_name: str,
                 failure_threshold: int = BREAKER_FAILURE_THRESHOLD,
                 open_seconds: float = BREAKER_OPEN_SECONDS) -> None:
        self.encoder_name = encoder_name
        self._threshold = max(1, int(failure_threshold))
        self._open_seconds = float(open_seconds)
        self._state = self.STATE_CLOSED
        self._consec_failures = 0
        self._opened_at = 0.0
        self._halfopen_in_flight = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            self._maybe_transition_to_halfopen_locked()
            return self._state

    def _maybe_transition_to_halfopen_locked(self) -> None:
        if self._state == self.STATE_OPEN and \
                (time.time() - self._opened_at) >= self._open_seconds:
            self._state = self.STATE_HALF_OPEN
            self._halfopen_in_flight = 0

    def allow(self) -> bool:
        """尝试占一个"允许硬件运行"的名额。返回 False 表示 breaker 打开
        且不允许新任务走硬件路径。"""
        with self._lock:
            self._maybe_transition_to_halfopen_locked()
            if self._state == self.STATE_CLOSED:
                return True
            if self._state == self.STATE_OPEN:
                return False
            # half-open：只放 BREAKER_HALFOPEN_PROBE 个探测
            if self._halfopen_in_flight >= BREAKER_HALFOPEN_PROBE:
                return False
            self._halfopen_in_flight += 1
            return True

    def record_success(self) -> None:
        with self._lock:
            self._consec_failures = 0
            if self._state != self.STATE_CLOSED:
                self._state = self.STATE_CLOSED
                self._halfopen_in_flight = 0

    def record_failure(self) -> None:
        with self._lock:
            if self._state == self.STATE_HALF_OPEN:
                # half-open 探测又失败 → 立即回到 open 并重新等 open_seconds
                self._state = self.STATE_OPEN
                self._opened_at = time.time()
                self._halfopen_in_flight = 0
                return
            self._consec_failures += 1
            if self._consec_failures >= self._threshold:
                self._state = self.STATE_OPEN
                self._opened_at = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            self._maybe_transition_to_halfopen_locked()
            return {
                "encoder": self.encoder_name,
                "state": self._state,
                "consec_failures": self._consec_failures,
                "opened_at": self._opened_at,
                "remaining_open_seconds": max(0.0,
                    self._open_seconds - (time.time() - self._opened_at))
                    if self._state == self.STATE_OPEN else 0.0,
            }


# --------------------------------------------------------------------------
# controller
# --------------------------------------------------------------------------


@dataclass
class ControllerState:
    mode: str = MODE_AUTO
    manual_video_concurrency: int = 0  # only meaningful in MODE_MANUAL
    absolute_max: int = 8              # 系统安全上限
    profile_recommended: int = 0
    user_max: int = 0                  # 用户手动上限（0 表示无覆盖）
    last_resize_at: float = 0.0
    last_effective_concurrency: int = 0
    last_throughput_signal: float = 0.0
    recent_hw_failures: list[float] = field(default_factory=list)  # timestamps
    reason: str = ""


class ConcurrencyController:
    """自动并发决策器——**只做决策，不直接开 worker**。

    调用方（scheduler / service）在每次事件后调用 `decide()`；
    controller 返回 (target_video_workers, apply_now: bool, reason)。
    """

    def __init__(self,
                 *, profile_recommended: int = 0,
                 absolute_max: int = 8) -> None:
        self.state = ControllerState(
            profile_recommended=max(0, int(profile_recommended)),
            absolute_max=max(1, int(absolute_max)),
        )
        self._lock = threading.Lock()
        self.cpu_fallback = CpuFallbackGate()
        self.breakers: dict[str, EncoderCircuitBreaker] = {}

    def breaker_for(self, encoder_name: str) -> EncoderCircuitBreaker:
        with self._lock:
            b = self.breakers.get(encoder_name)
            if b is None:
                b = EncoderCircuitBreaker(encoder_name)
                self.breakers[encoder_name] = b
            return b

    def set_mode(self, mode: str, *, manual_video: int | None = None) -> None:
        if mode not in ALL_MODES:
            raise ValueError(f"未知模式：{mode}")
        with self._lock:
            self.state.mode = mode
            if manual_video is not None:
                self.state.manual_video_concurrency = max(1, int(manual_video))

    def set_profile_recommended(self, n: int) -> None:
        with self._lock:
            self.state.profile_recommended = max(0, int(n))

    def set_user_max(self, n: int) -> None:
        with self._lock:
            self.state.user_max = max(0, int(n))

    def record_hardware_failure(self, encoder_name: str = "") -> None:
        now = time.time()
        with self._lock:
            self.state.recent_hw_failures.append(now)
            cutoff = now - FAILURE_WINDOW_S
            self.state.recent_hw_failures = [
                t for t in self.state.recent_hw_failures if t >= cutoff
            ]
        if encoder_name:
            self.breaker_for(encoder_name).record_failure()

    def record_hardware_success(self, encoder_name: str = "") -> None:
        if encoder_name:
            self.breaker_for(encoder_name).record_success()

    def recent_hw_failure_count(self) -> int:
        now = time.time()
        with self._lock:
            cutoff = now - FAILURE_WINDOW_S
            self.state.recent_hw_failures = [
                t for t in self.state.recent_hw_failures if t >= cutoff
            ]
            return len(self.state.recent_hw_failures)

    def decide(self, *, tts_done_backlog: int,
                 video_running: int,
                 output_free_gb: float,
                 recent_throughput: float,
                 encoder_name: str = "") -> tuple[int, bool, str]:
        """返回 (target, apply, reason)。

        - target: 建议的视频池 worker 数；
        - apply: 是否满足冷却时间可立即应用（False → 调用方应等一下再问）；
        - reason: 决策理由（供 UI/日志）。
        """
        now = time.time()
        with self._lock:
            s = self.state
            mode = s.mode
            # MANUAL 模式：直接返回用户设定
            if mode == MODE_MANUAL:
                target = min(s.absolute_max,
                              max(1, s.manual_video_concurrency))
                if s.user_max:
                    target = min(target, s.user_max)
                cooled = (now - s.last_resize_at) >= RESIZE_MIN_INTERVAL_S \
                    or s.last_effective_concurrency == 0
                reason = f"MANUAL：{target}"
                if cooled and target != s.last_effective_concurrency:
                    s.last_resize_at = now
                    s.last_effective_concurrency = target
                    s.reason = reason
                    return target, True, reason
                return target, False, reason

            # CPU_SAFE 模式：libx264 + 低并发（≤2）
            if mode == MODE_CPU_SAFE:
                target = min(2, s.absolute_max)
                cooled = (now - s.last_resize_at) >= RESIZE_MIN_INTERVAL_S \
                    or s.last_effective_concurrency == 0
                reason = "CPU_SAFE：libx264 低并发（≤2）"
                if cooled and target != s.last_effective_concurrency:
                    s.last_resize_at = now
                    s.last_effective_concurrency = target
                    s.reason = reason
                    return target, True, reason
                return target, False, reason

            # AUTO 模式
            base_recommended = s.profile_recommended or 1
            hard_cap = s.absolute_max
            if s.user_max:
                hard_cap = min(hard_cap, s.user_max)
            base_recommended = min(base_recommended, hard_cap)

            # breaker 打开 → 降到 1（并让上层切 libx264）
            if encoder_name:
                b = self.breakers.get(encoder_name)
                if b and b.state == EncoderCircuitBreaker.STATE_OPEN:
                    target = 1
                    cooled = (now - s.last_resize_at) >= RESIZE_MIN_INTERVAL_S
                    reason = f"AUTO：{encoder_name} breaker 打开，降到 1"
                    if cooled and target != s.last_effective_concurrency:
                        s.last_resize_at = now
                        s.last_effective_concurrency = target
                        s.reason = reason
                        return target, True, reason
                    return target, False, reason

            # 硬件失败率过高 → 减半
            cutoff = now - FAILURE_WINDOW_S
            s.recent_hw_failures = [t for t in s.recent_hw_failures if t >= cutoff]
            hw_fail_rate = len(s.recent_hw_failures)
            if hw_fail_rate >= 3:
                target = max(1, s.last_effective_concurrency // 2 or 1)
                cooled = (now - s.last_resize_at) >= RESIZE_MIN_INTERVAL_S
                reason = f"AUTO：近 {int(FAILURE_WINDOW_S)}s 硬件失败 {hw_fail_rate} 次，减半"
                if cooled and target != s.last_effective_concurrency:
                    s.last_resize_at = now
                    s.last_effective_concurrency = target
                    s.reason = reason
                    return target, True, reason
                return target, False, reason

            # 磁盘紧张：不允许 ramp-up
            if output_free_gb < DISK_SAFE_FREE_GB and \
                    s.last_effective_concurrency and \
                    base_recommended > s.last_effective_concurrency:
                target = s.last_effective_concurrency
                reason = f"AUTO：输出目录仅剩 {output_free_gb:.1f} GB，不放大"
                return target, False, reason

            # CPU fallback 饱和：不放大
            if self.cpu_fallback.is_saturated() and \
                    s.last_effective_concurrency and \
                    base_recommended > s.last_effective_concurrency:
                target = s.last_effective_concurrency
                reason = "AUTO：CPU fallback 饱和，不放大"
                return target, False, reason

            # 无 backlog → 不主动放大（保守）
            desired = base_recommended
            if tts_done_backlog <= 0 and s.last_effective_concurrency:
                desired = min(desired, max(1, s.last_effective_concurrency))
            # 有 backlog & 上一次吞吐信号相对之前提升不足 → 不放大
            if s.last_effective_concurrency and \
                    desired > s.last_effective_concurrency:
                if s.last_throughput_signal > 0 and recent_throughput > 0:
                    gain = (recent_throughput - s.last_throughput_signal) \
                        / max(1e-6, s.last_throughput_signal)
                    if gain < HYSTERESIS_RAMPUP:
                        desired = s.last_effective_concurrency
            target = min(hard_cap, max(1, desired))
            cooled = (now - s.last_resize_at) >= RESIZE_MIN_INTERVAL_S \
                or s.last_effective_concurrency == 0
            if not cooled:
                return target, False, "AUTO：冷却中"
            if target == s.last_effective_concurrency and \
                    s.last_effective_concurrency > 0:
                # 无变化——不需要下发
                return target, False, "AUTO：无需调整"
            s.last_resize_at = now
            s.last_effective_concurrency = target
            s.last_throughput_signal = recent_throughput
            reason = (f"AUTO：backlog={tts_done_backlog}，"
                       f"recommended={base_recommended}，选 {target}")
            s.reason = reason
            return target, True, reason

    def snapshot(self) -> dict:
        with self._lock:
            s = self.state
            return {
                "mode": s.mode,
                "profile_recommended": s.profile_recommended,
                "manual_video_concurrency": s.manual_video_concurrency,
                "absolute_max": s.absolute_max,
                "user_max": s.user_max,
                "last_effective_concurrency": s.last_effective_concurrency,
                "last_resize_at": s.last_resize_at,
                "recent_hw_failures": len(s.recent_hw_failures),
                "cpu_fallback_active": self.cpu_fallback.active(),
                "cpu_fallback_limit": self.cpu_fallback.limit,
                "breakers": [b.snapshot() for b in self.breakers.values()],
                "reason": s.reason,
            }
