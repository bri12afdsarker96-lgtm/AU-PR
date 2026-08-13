"""Edge TTS 熔断器 + 429 分类 + 指数退避 + 抖动。

R5 修复：
    - HALF_OPEN 探测租约带超时（half_open_lease_seconds），任何原因（无任务/异常/线程退出）
      导致探测未回执，超时后自动释放锁，避免熔断器永久锁住；
    - 通用网络异常也应计入熔断（由 record_failure 触发）——本模块不感知异常类型，
      调用方对所有可重试失败都调用 record_failure() 即可。
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass


@dataclass
class BreakerState:
    state: str = "CLOSED"
    consecutive_failures: int = 0
    open_until: float = 0.0
    half_open_locked: bool = False
    half_open_lease_expires: float = 0.0


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int = 5,
                 open_seconds: float = 30.0,
                 half_open_lease_seconds: float = 20.0) -> None:
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.half_open_lease_seconds = half_open_lease_seconds
        self._state = BreakerState()
        self._lock = threading.Lock()

    def acquire(self) -> tuple[bool, float]:
        now = time.time()
        with self._lock:
            # 探测租约到期即自动释放（防死锁）
            if (self._state.half_open_locked
                    and self._state.half_open_lease_expires
                    and now >= self._state.half_open_lease_expires):
                self._state.half_open_locked = False
                self._state.half_open_lease_expires = 0.0
                # 租约到期视为一次隐性失败——重新回到 OPEN
                self._state.state = "OPEN"
                self._state.open_until = now + self.open_seconds
                return False, self.open_seconds

            if self._state.state == "CLOSED":
                return True, 0.0
            if self._state.state == "OPEN":
                if now >= self._state.open_until:
                    self._state.state = "HALF_OPEN"
                    self._state.half_open_locked = True
                    self._state.half_open_lease_expires = now + self.half_open_lease_seconds
                    return True, 0.0
                return False, self._state.open_until - now
            # HALF_OPEN
            if self._state.half_open_locked:
                return False, min(self._state.half_open_lease_expires - now, 1.5) if self._state.half_open_lease_expires else 1.5
            self._state.half_open_locked = True
            self._state.half_open_lease_expires = now + self.half_open_lease_seconds
            return True, 0.0

    def record_success(self) -> None:
        with self._lock:
            self._state.state = "CLOSED"
            self._state.consecutive_failures = 0
            self._state.open_until = 0.0
            self._state.half_open_locked = False
            self._state.half_open_lease_expires = 0.0

    def record_failure(self, *, retry_after_seconds: float | None = None) -> None:
        with self._lock:
            self._state.consecutive_failures += 1
            self._state.half_open_locked = False
            self._state.half_open_lease_expires = 0.0
            if self._state.state == "HALF_OPEN":
                self._state.state = "OPEN"
                self._state.open_until = time.time() + (
                    retry_after_seconds if retry_after_seconds else self.open_seconds
                )
                return
            if self._state.consecutive_failures >= self.failure_threshold:
                self._state.state = "OPEN"
                self._state.open_until = time.time() + (
                    retry_after_seconds if retry_after_seconds else self.open_seconds
                )

    def release_probe(self) -> None:
        """线程异常/无任务领取时主动释放 HALF_OPEN 探测锁。"""
        with self._lock:
            if self._state.state == "HALF_OPEN":
                self._state.half_open_locked = False
                self._state.half_open_lease_expires = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            # 如果 HALF_OPEN 探测租约已到期，snapshot 里也要反映"已回 OPEN"——
            # 否则外部只调 snapshot 不调 acquire 时看到的状态会陈旧。
            if (self._state.half_open_locked
                    and self._state.half_open_lease_expires
                    and now >= self._state.half_open_lease_expires):
                self._state.state = "OPEN"
                self._state.half_open_locked = False
                self._state.half_open_lease_expires = 0.0
                self._state.open_until = now + self.open_seconds
            remaining = max(0.0, self._state.open_until - now) if self._state.state == "OPEN" else 0.0
            return {
                "state": self._state.state,
                "consecutive_failures": self._state.consecutive_failures,
                "open_seconds_left": round(remaining, 1),
                "half_open_locked": self._state.half_open_locked,
            }


def compute_backoff(attempt: int, *, base: float = 1.0, cap: float = 30.0,
                    jitter: float = 0.2) -> float:
    delay = min(cap, base * (2 ** (attempt - 1)))
    j = delay * jitter
    return max(0.1, delay + random.uniform(-j, j))


def classify_http_error(status_code: int) -> str:
    if status_code == 429:
        return "retryable_429"
    if 500 <= status_code < 600:
        return "retryable_5xx"
    if 400 <= status_code < 500:
        return "client_4xx"
    return "other"
