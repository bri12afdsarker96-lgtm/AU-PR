"""限流/退避/熔断测试。覆盖点 38-42。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "source"))

from dub_align_studio.bulk_dub.circuit_breaker import (  # noqa: E402
    CircuitBreaker, classify_http_error, compute_backoff,
)


def test_38_classify_429():
    assert classify_http_error(429) == "retryable_429"


def test_39_classify_5xx():
    assert classify_http_error(500) == "retryable_5xx"
    assert classify_http_error(502) == "retryable_5xx"
    assert classify_http_error(599) == "retryable_5xx"


def test_40_classify_client_4xx_not_retried():
    assert classify_http_error(400) == "client_4xx"
    assert classify_http_error(401) == "client_4xx"
    assert classify_http_error(404) == "client_4xx"


def test_backoff_grows_exponentially():
    a = compute_backoff(1, base=1.0, jitter=0)
    b = compute_backoff(2, base=1.0, jitter=0)
    c = compute_backoff(5, base=1.0, jitter=0)
    assert a <= b <= c
    assert compute_backoff(20, base=1.0, jitter=0) <= 30


def test_breaker_closes_and_records_success():
    br = CircuitBreaker(failure_threshold=3, open_seconds=1)
    for _ in range(2):
        allow, _ = br.acquire()
        assert allow
        br.record_success()


def test_41_breaker_opens_after_threshold_failures_no_thundering_herd():
    br = CircuitBreaker(failure_threshold=3, open_seconds=0.3)
    for _ in range(3):
        allow, _ = br.acquire()
        assert allow
        br.record_failure()
    allow, wait = br.acquire()
    assert not allow
    assert wait > 0
    time.sleep(0.35)
    allow1, _ = br.acquire()
    allow2, _ = br.acquire()
    assert allow1 is True
    assert allow2 is False, "半开状态只放一个探测请求"


def test_42_breaker_half_open_then_close_on_success():
    br = CircuitBreaker(failure_threshold=2, open_seconds=0.2)
    br.record_failure(); br.record_failure()
    time.sleep(0.25)
    allow, _ = br.acquire()
    assert allow
    br.record_success()
    snap = br.snapshot()
    assert snap["state"] == "CLOSED"


def test_42_breaker_half_open_probe_fails_reopens():
    br = CircuitBreaker(failure_threshold=2, open_seconds=0.2)
    br.record_failure(); br.record_failure()
    time.sleep(0.25)
    allow, _ = br.acquire()
    assert allow
    br.record_failure()
    snap = br.snapshot()
    assert snap["state"] == "OPEN"


def test_38_breaker_honors_retry_after():
    br = CircuitBreaker(failure_threshold=1, open_seconds=1.0)
    br.record_failure(retry_after_seconds=0.3)
    allow, wait = br.acquire()
    assert not allow
    assert wait <= 0.35
