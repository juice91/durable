"""A per-caller token bucket for the OTLP trace endpoint (one bucket per API key, or per tenant for
the dev bearer). In-memory and per API process: with several replicas the effective rate is the
limit times the replica count, which is the intent (a guard against a runaway exporter, not billing).

`EAP_OTLP_RATE_LIMIT_PER_MINUTE` (default 600 requests, also the burst); 0 turns the limit off.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable

DEFAULT_RATE_PER_MINUTE = 600
MAX_BUCKETS = 10_000


def rate_limit_from_env() -> int:
    raw = os.environ.get("EAP_OTLP_RATE_LIMIT_PER_MINUTE", "").strip()
    try:
        return max(int(raw), 0) if raw else DEFAULT_RATE_PER_MINUTE
    except ValueError:
        return DEFAULT_RATE_PER_MINUTE


class TokenBucketLimiter:
    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.capacity = per_minute
        self.rate = per_minute / 60.0
        self._clock = clock
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last)

    def acquire(self, key: str) -> float | None:
        """None when the request may go ahead, else the seconds until a token is available."""
        if self.capacity <= 0:
            return None
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(self.capacity), now))
            tokens = min(float(self.capacity), tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                if len(self._buckets) > MAX_BUCKETS:
                    self._evict(now)
                return None
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / self.rate

    def _evict(self, now: float) -> None:
        full_after = self.capacity / self.rate
        for key in [k for k, (_, last) in self._buckets.items() if now - last > full_after]:
            del self._buckets[key]
