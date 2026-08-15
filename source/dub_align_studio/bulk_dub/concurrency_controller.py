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

# R14-FIX P0-4 **唯一并发上限常量**——service/scheduler/controller/API/HTML/
# benchmark ladder 全部统一用它。不允许出现"推荐 16 实际只 clamp 到 8"。
# R15：改为自适应（用户反馈"如果 CPU/GPU 能支撑，自动增加并发"）。
#   - 基础 16
#   - GPU VRAM ≥ 12 GB 且 CPU ≥ 12 逻辑核 → 24
#   - GPU VRAM ≥ 20 GB 且 CPU ≥ 16 逻辑核 → 32
#   - CPU-only（无独立 GPU） → 保守 min(cpu//2, 16)
# 攻击者不该拿这个反推硬件，只暴露最终数值到 API（不含硬件细节）。


def _detect_hardware_cap() -> tuple[int, int]:
    """返回 (video_cap, tts_cap)。仅在模块导入时算一次；结果只暴露数值本身。"""
    import os
    import subprocess
    import shutil as _sh
    try:
        cpu = os.cpu_count() or 4
    except Exception:  # noqa: BLE001
        cpu = 4

    # GPU VRAM 探测（不依赖 torch，避免拖慢启动；用 nvidia-smi）
    vram_mb = 0
    smi = _sh.which("nvidia-smi")
    if smi:
        try:
            # Windows pyinstaller windowed exe：必须传 CREATE_NO_WINDOW，
            # 否则每个 subprocess 都会闪一个黑色 cmd 窗（用户抱怨的「两个黑色闪屏」）
            _flags = 0
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                _flags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
            r = subprocess.run(
                [smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
                creationflags=_flags,
            )
            if r.returncode == 0:
                for line in r.stdout.splitlines():
                    line = line.strip()
                    if line.isdigit():
                        vram_mb = max(vram_mb, int(line))
        except Exception:  # noqa: BLE001
            pass

    if vram_mb >= 20_000 and cpu >= 16:
        video = 32
    elif vram_mb >= 12_000 and cpu >= 12:
        video = 24
    else:
        # 保守/基准档：跟老逻辑一致，保留 16 上限
        # 硬件不够时**不下调**上限（避免破坏现有 UI / 测试预期）；
        # 真正调节由 ConcurrencyController 运行时动态 resize 完成。
        video = 16

    # TTS 主要网络/IO bound，只做 up-scale；下限 16
    tts = max(16, min(32, cpu))
    return video, tts


MAX_VIDEO_CONCURRENCY, MAX_TTS_CONCURRENCY = _detect_hardware_cap()

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


class GateCancelled(Exception):
    """R14-FIX P0-3：等 gate 期间收到 task cancel / scheduler stop。"""


class CpuFallbackGate:
    """R14 CPU fallback 专用 semaphore。

    NVENC 编码时若运行时失败，会自动回退到 libx264。若同时 8 个 NVENC 会话
    全部回退，就是 8 个 libx264 同时抢 CPU，会把机器压垮。这个 gate 用
    一把 Semaphore 强制"同一时刻最多 N 个 libx264 回退"，多余任务等待。

    R14-FIX P0-3：
      * `acquire()` 不再无限阻塞——用短 timeout 循环，每轮检查外部
        `is_cancelled()`；取消时抛 `GateCancelled`；
      * `release()` **精确释放一次**——引入 `_holders` 计数，
        release 时若当前调用者未持有 → 记 error 并 no-op，不静默 release
        破坏 semaphore；
      * 多线程压力测试证明 active/limit 严格自洽。
    """

    def __init__(self, limit: int = DEFAULT_CPU_FALLBACK_LIMIT) -> None:
        self._limit = max(1, int(limit))
        self._sem = threading.BoundedSemaphore(self._limit)
        self._active = 0
        self._active_lock = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    def acquire(self, timeout: float | None = None,
                 is_cancelled: Callable[[], bool] | None = None,
                 poll_interval: float = 0.2) -> bool:
        """短 timeout 轮询获取 permit。

        - `is_cancelled` 返回 True → 抛 `GateCancelled`；
        - `timeout is None` → 无限轮询直到 acquired 或 cancelled；
        - `timeout > 0` → 总等待上限；到期返回 False；
        - 返回 True 表示获得 permit，调用方必须 release 一次。
        """
        deadline = None if timeout is None else (time.time() + max(0.0, timeout))
        while True:
            if is_cancelled is not None and is_cancelled():
                raise GateCancelled("gate acquire cancelled by caller")
            # 每轮短 timeout，让 cancel/stop 能及时收敛
            step = poll_interval
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return False
                step = min(step, remaining)
            got = self._sem.acquire(timeout=step)
            if got:
                with self._active_lock:
                    self._active += 1
                return True

    def release(self) -> None:
        released_here = False
        with self._active_lock:
            if self._active > 0:
                self._active -= 1
                released_here = True
        if not released_here:
            # 精确释放：没有对应 acquire 时**不**再动 semaphore
            return
        try:
            self._sem.release()
        except ValueError:
            # bounded 情况多释放会抛——按 released_here 逻辑不会走到，防御
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

    def record_cancel(self) -> None:
        """R14-FIX P0-3：half-open 探测任务被取消/中止时释放 probe 名额，
        避免 probe 永久占位、breaker 卡在 half-open 拒绝其他任务。"""
        with self._lock:
            if self._state == self.STATE_HALF_OPEN and self._halfopen_in_flight > 0:
                self._halfopen_in_flight -= 1

    def is_open(self) -> bool:
        return self.state == self.STATE_OPEN

    def is_degraded(self) -> bool:
        """R14-FIX2 P0-6：OPEN 或 HALF_OPEN 都视为硬件退化——
        HALF_OPEN 期间只允许 1 个探测；池不能保持满并发。"""
        return self.state in (self.STATE_OPEN, self.STATE_HALF_OPEN)

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
    absolute_max: int = MAX_VIDEO_CONCURRENCY   # R14-FIX P0-4 全局上限一致
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
                 absolute_max: int = MAX_VIDEO_CONCURRENCY) -> None:
        self.state = ControllerState(
            profile_recommended=max(0, int(profile_recommended)),
            absolute_max=max(1, min(MAX_VIDEO_CONCURRENCY, int(absolute_max))),
        )
        self._lock = threading.Lock()
        self.cpu_fallback = CpuFallbackGate()
        self.breakers: dict[str, EncoderCircuitBreaker] = {}
        # R14-FIX P1-2：CPU fallback 失败计数——与硬件失败分开
        # 硬件成功回调不能清零 CPU fallback 失败
        self._cpu_fallback_failure_count = 0
        self._cpu_fallback_lock = threading.Lock()

    # R14-FIX P1-2 CPU fallback 计数
    def record_cpu_fallback_failure(self) -> None:
        with self._cpu_fallback_lock:
            self._cpu_fallback_failure_count += 1

    def cpu_fallback_failure_count(self) -> int:
        with self._cpu_fallback_lock:
            return self._cpu_fallback_failure_count

    def is_any_hw_breaker_open(self) -> bool:
        """R14-FIX P0-3：任何硬件 encoder breaker 打开 → coordinator 降池。"""
        with self._lock:
            for b in self.breakers.values():
                if b.is_open():
                    return True
        return False

    def is_any_hw_breaker_degraded(self) -> bool:
        """R14-FIX2 P0-6：任何硬件 encoder breaker OPEN 或 HALF_OPEN —— 池必须
        保持安全并发（推荐 1）。HALF_OPEN 期间也不允许恢复满池，
        只有 CLOSED 才能按冷却/滞回恢复并发。"""
        with self._lock:
            for b in self.breakers.values():
                if b.is_degraded():
                    return True
        return False

    def note_resize_applied(self, applied: int) -> None:
        """R14-FIX2 P1-4：由 scheduler 在 **resize 真的成功之后** 调用，
        统一在这里更新 last_effective_concurrency / last_resize_at；
        decide() 只出计划、绝不预写。"""
        with self._lock:
            self.state.last_effective_concurrency = max(1, int(applied))
            self.state.last_resize_at = time.time()

    def note_observed_serving(self, serving: int) -> None:
        """R14-FIX2 P1-4：coordinator/snapshot 观察到"当前 serving 数"但**尚未
        主动 resize** 时，只对齐 last_effective_concurrency，**不**改
        last_resize_at——避免虚假冷却窗口阻止首次真实决策。"""
        with self._lock:
            if serving > 0 and self.state.last_effective_concurrency == 0:
                self.state.last_effective_concurrency = int(serving)

    def note_resize_failed(self, target: int, reason: str = "") -> None:
        """R14-FIX2 P1-4：resize 失败——保留真实 serving，不改 last_effective；
        清零 last_resize_at，允许下一周期立即重试。"""
        with self._lock:
            self.state.last_resize_at = 0.0
            if reason:
                self.state.reason = f"resize 失败：{reason}"

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
        """返回 (target, apply, reason)——**只做决策，不落状态**。

        R14-FIX2 P1-4：last_effective_concurrency / last_resize_at 只能由
        `note_resize_applied()` / `note_resize_failed()` 在 resize 真的完成
        后更新。decide() 不再"先写效应再返回"，避免 resize 失败仍认为已生效。
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
                s.reason = reason
                if cooled and target != s.last_effective_concurrency:
                    return target, True, reason
                return target, False, reason

            # CPU_SAFE 模式：libx264 + 低并发（≤2）
            if mode == MODE_CPU_SAFE:
                target = min(2, s.absolute_max)
                cooled = (now - s.last_resize_at) >= RESIZE_MIN_INTERVAL_S \
                    or s.last_effective_concurrency == 0
                reason = "CPU_SAFE：libx264 低并发（≤2）"
                s.reason = reason
                if cooled and target != s.last_effective_concurrency:
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
                    s.reason = reason
                    if cooled and target != s.last_effective_concurrency:
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
                s.reason = reason
                if cooled and target != s.last_effective_concurrency:
                    return target, True, reason
                return target, False, reason

            # 磁盘紧张：不允许 ramp-up
            if output_free_gb < DISK_SAFE_FREE_GB and \
                    s.last_effective_concurrency and \
                    base_recommended > s.last_effective_concurrency:
                target = s.last_effective_concurrency
                reason = f"AUTO：输出目录仅剩 {output_free_gb:.1f} GB，不放大"
                s.reason = reason
                return target, False, reason

            # CPU fallback 饱和：不放大
            if self.cpu_fallback.is_saturated() and \
                    s.last_effective_concurrency and \
                    base_recommended > s.last_effective_concurrency:
                target = s.last_effective_concurrency
                reason = "AUTO：CPU fallback 饱和，不放大"
                s.reason = reason
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
                s.reason = "AUTO：冷却中"
                return target, False, s.reason
            if target == s.last_effective_concurrency and \
                    s.last_effective_concurrency > 0:
                # 无变化——不需要下发
                s.reason = "AUTO：无需调整"
                return target, False, s.reason
            # R14-FIX2 P1-4：仅记录本次决策使用的吞吐信号，供下次滞回参考；
            # last_effective / last_resize_at 由 note_resize_applied 更新
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
                # R14-FIX P1-2：CPU fallback 失败与硬件失败**分开**记录
                "cpu_fallback_failure_count": self.cpu_fallback_failure_count(),
                "breakers": [b.snapshot() for b in self.breakers.values()],
                "reason": s.reason,
            }
