"""clock.now: the bare ISO string by default; `offset_minutes` shifts it; `parts: true` returns structured fields."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from eap_runtime import _default_registry as registry
from eap_schemas.tools import ClockNowInput

FROZEN = datetime(2026, 11, 20, 6, 15, 0, 489394, tzinfo=timezone.utc)


class _Frozen(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D401 - datetime API
        return FROZEN if tz is None else FROZEN.astimezone(tz)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(registry, "datetime", _Frozen)


def test_default_is_the_bare_iso_string_with_microseconds(frozen):
    assert registry._clock_now(None) == "2026-11-20T06:15:00.489394+00:00"


def test_offset_minutes_shifts_the_bare_string(frozen):
    assert registry._clock_now(None, offset_minutes=-60) == "2026-11-20T05:15:00.489394+00:00"
    assert registry._clock_now(None, offset_minutes=90) == "2026-11-20T07:45:00.489394+00:00"


def test_parts_returns_the_structured_fields_rounded_to_seconds(frozen):
    assert registry._clock_now(None, parts=True, offset_minutes=-60) == {
        "iso": "2026-11-20T05:15:00+00:00",
        "date": "2026-11-20",
        "hour": "05",
        "minute": "15",
        "hour_prefix": "2026-11-20T05:",
        "weekday": "fri",
        "epoch_s": 1795151700,
    }


def test_parts_weekday_is_locale_independent_and_crosses_midnight(frozen):
    parts = registry._clock_now(None, parts=True, offset_minutes=-7 * 60)
    assert parts["date"] == "2026-11-19" and parts["hour_prefix"] == "2026-11-19T23:" and parts["weekday"] == "thu"


def test_input_bounds():
    assert ClockNowInput().model_dump() == {"offset_minutes": 0, "parts": False}
    assert ClockNowInput(offset_minutes=10080).offset_minutes == 10080
    with pytest.raises(ValidationError):
        ClockNowInput(offset_minutes=10081)
    with pytest.raises(ValidationError):
        ClockNowInput(offset_minutes=-10081)
    with pytest.raises(ValidationError):
        ClockNowInput(parts="yes please")  # type: ignore[arg-type]
