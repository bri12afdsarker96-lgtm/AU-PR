"""Edge TTS 熔断器 + 429 分类 + 指数退避 + 抖动。"""

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


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int = 5,
                 open_seconds: float = 30.0,
                 half_open_probe_timeout: float = 60.0) -> None:
        self.failure_threshold = failure_threshold
        self.open_seconds = open_seconds
        self.half_open_probe_timeout = half_open_probe_timeout
        self._state = BreakerState()
        self._lock = threading.Lock()

    def acquire(self) -> tuple[bool, float]:
        now = time.time()
        with self._lock:
            if self._state.state == "CLOSED":
                return True, 0.0
            if self._state.state == "OPEN":
                if now >= self._state.open_until:
                    self._state.state = "HALF_OPEN"
                    self._state.half_open_locked = True
                    return True, 0.0
                return False, self._state.open_until - now
            if self._state.half_open_locked:
                return False, 1.5
            self._state.half_open_locked = True
            return True, 0.0

    def record_success(self) -> None:
        with self._lock:
            self._state.state = "CLOSED"
            self._state.consecutive_failures = 0
            self._state.open_until = 0.0
            self._state.half_open_locked = False

    def record_failure(self, *, retry_after_seconds: float | None = None) -> None:
        with self._lock:
            self._state.consecutive_failures += 1
            self._state.half_open_locked = False
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

    def snapshot(self) -> dict:
        with self._lock:
            now = time.time()
            remaining = max(0.0, self._state.open_until - now) if self._state.state == "OPEN" else 0.0
            return {
                "state": self._state.state,
                "consecutive_failures": self._state.consecutive_failures,
                "open_seconds_left": round(remaining, 1),
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
