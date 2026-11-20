"""The default `clock.sleep` implementation: bounds, a monkeypatched sleep, the result shape."""

import asyncio
from datetime import datetime

import pytest
from pydantic import ValidationError

from eap_runtime import default_registry
from eap_schemas.tools import ClockSleepInput


def test_the_bounds_are_the_declared_input_model_bounds():
    for ok in (1, 60, 120):
        assert ClockSleepInput(seconds=ok).seconds == ok
    for bad in (0, -1, 121):
        with pytest.raises(ValidationError):
            ClockSleepInput(seconds=bad)


def test_it_sleeps_the_requested_seconds_and_reports_when_it_finished(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    result = asyncio.run(default_registry().get("clock.sleep")(None, seconds=7))
    assert slept == [7]
    assert result["slept_seconds"] == 7
    assert datetime.fromisoformat(result["finished_at"]).utcoffset().total_seconds() == 0
    assert set(result) == {"slept_seconds", "finished_at"}
