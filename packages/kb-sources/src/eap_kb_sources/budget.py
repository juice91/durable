"""Rate limit and daily quota. `TokenBucket` paces calls (for example 5 per second); `DailyBudget` counts
calls per bucket per UTC day and lives in the source checkpoint (`checkpoint["daily_budget"]`), so a
sync that spends the day's quota stops with a usable checkpoint and the next run (the next day, or a
raised limit) continues. `Budget` is what an adapter is handed: spend before every provider call."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

from eap_kb_sources.errors import BudgetExhausted


class TokenBucket:
    """Refills `rate` tokens per second up to `capacity` (default `rate`); `acquire` waits for a token."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else rate)
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity
        self._at = clock()
        self._lock: asyncio.Lock | None = None

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._at) * self.rate)
        self._at = now

    async def acquire(self, n: float = 1.0) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            self._refill()
            if self._tokens < n:
                await self._sleep((n - self._tokens) / self.rate)
                self._refill()
            self._tokens = max(0.0, self._tokens - n)


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


class DailyBudget:
    """Calls used today per bucket against `limits`. A state from another day starts at zero."""

    def __init__(self, limits: Mapping[str, int], day: date, used: Mapping[str, int] | None = None) -> None:
        self.limits = {str(k): int(v) for k, v in limits.items()}
        self.day = day
        self.used: dict[str, int] = {k: int(v) for k, v in (used or {}).items() if int(v) > 0}

    @classmethod
    def from_checkpoint(cls, state: Any, limits: Mapping[str, int], today: date | None = None) -> "DailyBudget":
        today = today or utc_today()
        if isinstance(state, Mapping) and state.get("date") == today.isoformat():
            used = state.get("used")
            if isinstance(used, Mapping) and all(
                isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in used.values()
            ):
                return cls(limits, today, used)
        return cls(limits, today)

    def remaining(self, bucket: str) -> int:
        return max(0, self.limits.get(bucket, 0) - self.used.get(bucket, 0))

    def try_consume(self, bucket: str, n: int = 1) -> bool:
        if self.remaining(bucket) < n:
            return False
        self.used[bucket] = self.used.get(bucket, 0) + n
        return True

    def consume(self, bucket: str, n: int = 1) -> None:
        if not self.try_consume(bucket, n):
            raise BudgetExhausted(bucket)

    def as_state(self) -> dict[str, Any]:
        return {"date": self.day.isoformat(), "used": dict(sorted(self.used.items()))}

    def snapshot(self) -> dict[str, dict[str, int]]:
        buckets = sorted(self.limits)
        return {
            "used": {b: self.used.get(b, 0) for b in buckets},
            "remaining": {b: self.remaining(b) for b in buckets},
        }


@dataclass
class Budget:
    """The daily quota plus an optional rate limiter, spent before each provider call."""

    daily: DailyBudget
    limiter: TokenBucket | None = None

    async def spend(self, bucket: str, n: int = 1) -> None:
        self.daily.consume(bucket, n)  # BudgetExhausted before any token is taken
        if self.limiter is not None:
            await self.limiter.acquire(n)

    def remaining(self, bucket: str) -> int:
        return self.daily.remaining(bucket)

    def snapshot(self) -> dict[str, dict[str, int]]:
        return self.daily.snapshot()
