import asyncio
from datetime import date

import pytest

from eap_kb_sources.budget import Budget, DailyBudget, TokenBucket
from eap_kb_sources.errors import BudgetExhausted


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_token_bucket_allows_a_burst_then_paces():
    clock = Clock()
    bucket = TokenBucket(rate=5, capacity=5, clock=clock, sleep=clock.sleep)

    async def run():
        for _ in range(5):
            await bucket.acquire()
        assert clock.slept == []  # the burst of 5 is free
        await bucket.acquire()  # the sixth waits for one token: 1/5 s
        await bucket.acquire()

    asyncio.run(run())
    assert clock.slept == [pytest.approx(0.2), pytest.approx(0.2)]


def test_token_bucket_refills_with_time():
    clock = Clock()
    bucket = TokenBucket(rate=2, capacity=2, clock=clock, sleep=clock.sleep)

    async def run():
        await bucket.acquire()
        await bucket.acquire()
        clock.now += 10  # long idle: refilled to capacity, not beyond
        await bucket.acquire()
        await bucket.acquire()
        assert clock.slept == []
        await bucket.acquire()

    asyncio.run(run())
    assert clock.slept == [pytest.approx(0.5)]


def test_daily_budget_counts_per_bucket_and_refuses_at_the_limit():
    daily = DailyBudget.from_checkpoint(None, {"product": 3, "extended": 1}, today=date(2026, 11, 18))
    assert daily.remaining("product") == 3
    daily.consume("product", 2)
    assert daily.remaining("product") == 1
    daily.consume("product")
    with pytest.raises(BudgetExhausted) as info:
        daily.consume("product")
    assert info.value.bucket == "product"
    assert daily.remaining("product") == 0 and daily.remaining("extended") == 1
    assert not daily.try_consume("product") and daily.try_consume("extended")


def test_daily_budget_survives_a_checkpoint_round_trip_and_rolls_over_at_midnight():
    today = date(2026, 11, 18)
    daily = DailyBudget.from_checkpoint(None, {"product": 10}, today=today)
    daily.consume("product", 4)
    state = daily.as_state()
    assert state == {"date": "2026-11-18", "used": {"product": 4}}
    same_day = DailyBudget.from_checkpoint(state, {"product": 10}, today=today)
    assert same_day.remaining("product") == 6
    next_day = DailyBudget.from_checkpoint(state, {"product": 10}, today=date(2026, 11, 19))
    assert next_day.remaining("product") == 10 and next_day.as_state()["date"] == "2026-11-19"
    # a limit raised or lowered between runs applies at once
    lowered = DailyBudget.from_checkpoint(state, {"product": 3}, today=today)
    assert lowered.remaining("product") == 0


def test_daily_budget_ignores_a_malformed_state():
    for bad in ("x", 3, {"date": 5, "used": "no"}, {"date": "2026-11-18", "used": {"product": "many"}}):
        daily = DailyBudget.from_checkpoint(bad, {"product": 5}, today=date(2026, 11, 18))
        assert daily.remaining("product") == 5


def test_budget_spend_takes_the_quota_then_the_rate_limit():
    clock = Clock()
    budget = Budget(
        DailyBudget.from_checkpoint(None, {"product": 2}, today=date(2026, 11, 18)),
        TokenBucket(rate=1, capacity=1, clock=clock, sleep=clock.sleep),
    )

    async def run():
        await budget.spend("product")
        await budget.spend("product")
        with pytest.raises(BudgetExhausted):
            await budget.spend("product")

    asyncio.run(run())
    assert clock.slept == [pytest.approx(1.0)]  # the refused call took no token
    assert budget.snapshot() == {"used": {"product": 2}, "remaining": {"product": 0}}
