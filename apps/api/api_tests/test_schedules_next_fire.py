"""next_fire: computed from `after` in the schedule's timezone, returned in UTC, across DST changes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from eap_api.schedules import next_fire

UTC = timezone.utc


def test_spring_forward_gap_does_not_raise_and_is_strictly_after():
    # Europe/Berlin skips 02:00-03:00 on 2026-03-29, so 02:30 does not exist that day.
    after = datetime(2026, 3, 28, 12, tzinfo=UTC)
    nxt = next_fire("30 2 * * *", "Europe/Berlin", after)
    assert nxt.tzinfo is not None and nxt.utcoffset() == timedelta(0)
    assert nxt > after
    assert nxt < after + timedelta(days=3)
    # croniter resolves the absent 02:30 CET to 01:00 UTC on 2026-03-29 (the instant of the jump).
    assert nxt == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)


def test_autumn_fall_back_returns_one_value_after_the_anchor():
    # 02:30 happens twice on 2026-10-25 in Europe/Berlin; one answer, strictly after `after`.
    after = datetime(2026, 10, 24, 12, tzinfo=UTC)
    nxt = next_fire("30 2 * * *", "Europe/Berlin", after)
    assert nxt > after and nxt.utcoffset() == timedelta(0)
    assert nxt.date() == datetime(2026, 10, 25).date() and nxt.minute == 30
    again = next_fire("30 2 * * *", "Europe/Berlin", after)
    assert again == nxt


def test_result_depends_only_on_after_not_on_a_stored_next_fire_at():
    first = next_fire("0 9 * * *", "UTC", datetime(2026, 5, 1, 0, tzinfo=UTC))
    later = next_fire("0 9 * * *", "UTC", datetime(2026, 5, 10, 9, 0, tzinfo=UTC))
    assert first == datetime(2026, 5, 1, 9, tzinfo=UTC)
    assert later == datetime(2026, 5, 11, 9, tzinfo=UTC)  # strictly after, not equal to `after`
    # a naive `after` is read as UTC
    assert next_fire("0 9 * * *", "UTC", datetime(2026, 5, 1, 0)) == first


def test_update_body_rejects_null_input_even_when_a_schema_would_allow_null():
    import pytest
    from pydantic import ValidationError

    from eap_schemas.workflows import WorkflowScheduleUpdate

    with pytest.raises(ValidationError):
        WorkflowScheduleUpdate.model_validate({"input": None})
    assert WorkflowScheduleUpdate.model_validate({"input": {}}).input == {}
