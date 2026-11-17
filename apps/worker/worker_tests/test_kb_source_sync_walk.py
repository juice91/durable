"""Segmented full and incremental catalogue sync across runs (the fake catalogue: 12 products, pages of 5).

Resume from the stored cursor, `restart: true`, budgets carried between runs, the removal sweep after a
COMPLETE full walk (by `observed_at >= walk start`, no key list in the checkpoint), incremental never
removing, and the replay of the steps that decide those."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date

import pytest

from eap_worker import kb_source_sync as sync
from worker_tests.test_kb_source_sync import _ctx, env, fake, keys, launched, report, scalar, start  # noqa: F401

DAY = date(2026, 11, 18)


def cursor_of(env):
    return env.sources.get(env.tenant, env.source.id).last_checkpoint["cursor"]


def status_of(env, key: str, record_type: str = "product") -> str:
    return env.records.get(env.tenant, env.source.id, key, record_type).status


def fetched(fake) -> list[str]:
    return [k for m, k in fake.calls if m == "fetch_record"]


# ------------------------------------------------------------------------------------ resume and restart


def test_a_three_page_catalogue_is_walked_over_two_runs_the_second_resumes_the_cursor(env, fake):
    _, first = start(env, {"mode": "full", "page_budget": 1, "max_segments": 2})
    rep = report(first)
    assert (rep["complete"], rep["stop_reason"], rep["discovered"], rep["removed"]) == (False, "max_segments", 10, 0)
    cursor = cursor_of(env)
    assert cursor["cursor"] == 10 and cursor["walk_mode"] == "full" and cursor["walk_started_at"]
    assert env.sources.get(env.tenant, env.source.id).last_successful_sync_at is None

    fake.calls.clear()
    _, second = start(env, {"mode": "full", "page_budget": 1})
    rep = report(second)
    assert rep["complete"] is True and rep["discovered"] == 2 and rep["stop_reason"] is None
    assert fetched(fake) == keys(2, 11)  # the two products of page three, nothing before
    done = env.sources.get(env.tenant, env.source.id)
    assert done.last_successful_sync_at is not None
    assert "walk_started_at" not in done.last_checkpoint["cursor"] and "walk_mode" not in done.last_checkpoint["cursor"]


def test_restart_true_walks_from_the_first_page_again(env, fake):
    start(env, {"mode": "full", "page_budget": 1, "max_segments": 2})
    fake.calls.clear()
    _, result = start(env, {"mode": "full", "restart": True})
    rep = report(result)
    assert rep["complete"] is True and rep["discovered"] == 12 and fetched(fake) == keys(12)


@pytest.mark.parametrize("value", ["yes", 1, None, []])
def test_restart_must_be_a_boolean(env, value):
    _, result = start(env, {"mode": "full", "restart": value})
    assert result["error_code"] == "invalid_request"


def test_restart_is_refused_for_a_targeted_sync(env):
    _, result = start(env, {"mode": "targeted", "part_numbers": ["FAKE0001"], "restart": True})
    assert result["error_code"] == "invalid_request"


def test_a_walk_of_the_other_mode_is_not_resumed_it_starts_over(env, fake):
    start(env, {"mode": "full", "page_budget": 1, "max_segments": 1})  # a full walk stopped after page one
    fake.calls.clear()
    _, result = start(env, {"mode": "incremental"})
    assert report(result)["complete"] is True and fetched(fake)[0] == "FAKE0001"


# ------------------------------------------------------------------------------------ the removal sweep


def test_a_complete_full_walk_marks_a_vanished_product_removed_and_keeps_the_others(env, fake):
    start(env, {"mode": "full"})
    assert all(status_of(env, k) == "active" for k in keys(12))
    fake.remove("FAKE0005")
    _, result = start(env, {"mode": "full"})
    rep = report(result)
    assert rep["complete"] is True and rep["removed"] == 3  # its three raw records
    assert {status_of(env, "FAKE0005", t) for t in ("product", "parametrics", "quality")} == {"removed"}
    assert all(status_of(env, k) == "active" for k in keys(12) if k != "FAKE0005")
    # a walk that sees it again brings it back
    fake.restore("FAKE0005")
    _, back = start(env, {"mode": "full"})
    assert report(back)["removed"] == 0 and status_of(env, "FAKE0005") == "active"


def test_the_sweep_uses_this_walks_observations_not_a_stored_key_list(env, fake):
    start(env, {"mode": "full", "page_budget": 1, "max_segments": 2})
    assert len(json.dumps(cursor_of(env))) < 400  # no key list in the checkpoint
    fake.remove("FAKE0002")  # already walked in this walk, then vanished from the catalogue: not seen later
    _, result = start(env, {"mode": "full"})
    # FAKE0002 was observed earlier in THIS walk (before the resume), so this walk does not remove it
    assert report(result)["complete"] is True and status_of(env, "FAKE0002") == "active"
    _, next_walk = start(env, {"mode": "full"})
    assert report(next_walk)["removed"] == 3 and status_of(env, "FAKE0002") == "removed"


def test_an_incomplete_full_walk_never_removes(env, fake):
    start(env, {"mode": "full"})
    fake.remove("FAKE0003")
    _, result = start(env, {"mode": "full", "page_budget": 1, "max_segments": 1})
    rep = report(result)
    assert rep["complete"] is False and rep["removed"] == 0 and status_of(env, "FAKE0003") == "active"


def test_incremental_never_removes(env, fake):
    start(env, {"mode": "full"})
    fake.remove("FAKE0004")
    fake.bump("FAKE0001")
    _, result = start(env, {"mode": "incremental"})
    rep = report(result)
    assert rep["complete"] is True and rep["removed"] == 0 and status_of(env, "FAKE0004") == "active"


def test_an_adapter_veto_keeps_the_records(env, fake):
    start(env, {"mode": "full"})
    fake.remove("FAKE0006")
    fake.sweep_ok = False
    _, result = start(env, {"mode": "full"})
    rep = report(result)
    assert rep["complete"] is True and rep["removed"] == 0 and status_of(env, "FAKE0006") == "active"


def test_the_finalize_sweep_is_idempotent_on_replay(env, fake):
    start(env, {"mode": "full"})
    fake.remove("FAKE0007")
    run_id, result = start(env, {"mode": "full"})
    state = {"source_id": env.source.id, "mode": "full", "counters": {n: 0 for n in sync.SegmentCounters.FIELDS},
             "errors": [], "cursor": {}, "segments": 3, "complete": True, "stop_reason": None, "calls_used": 0,
             "started_at": "2026-11-18T00:00:00+00:00", "walk_started_at": "2026-11-18T00:00:00+00:00",
             "sweep_ok": True, "removed": 0, "budget": None, "source_revision": None}
    before = scalar(env, "SELECT count(*) FROM kb_source_records WHERE tenant_id = :t AND source_id = :s AND status = 'removed'")
    asyncio.run(sync.finalize_step(str(run_id), env.tenant, env.source.id, state))
    assert scalar(env, "SELECT count(*) FROM kb_source_records WHERE tenant_id = :t AND source_id = :s AND status = 'removed'") == before == 3


# ------------------------------------------------------------------------------------ revision and budget


def test_a_complete_walk_stores_the_source_revision_and_the_report_carries_the_budget(env, fake):
    fake.today = DAY
    fake.daily_limit = 100
    _, result = start(env, {"mode": "full"})
    rep = report(result)
    assert rep["source_revision"] == "12:rev1" and rep["budget"] == {"used": {"product": 12}, "remaining": {"product": 88}}
    assert env.sources.get(env.tenant, env.source.id).last_source_revision == "12:rev1"


def test_a_run_that_cannot_make_a_single_call_stops_on_budget_and_leaves_the_cursor_unchanged(env, fake):
    fake.today = DAY
    start(env, {"mode": "full", "page_budget": 1, "max_segments": 1})
    before = cursor_of(env)
    fake.daily_limit = 5  # the five calls of the first segment spent it
    calls = fake.count("fetch_record")
    _, result = start(env, {"mode": "full", "page_budget": 1})
    rep = report(result)
    assert rep["stop_reason"] == "budget" and rep["complete"] is False and rep["discovered"] == 0 and rep["calls_used"] == 0
    assert rep["budget"]["remaining"] == {"product": 0}
    assert cursor_of(env) == before and fake.count("fetch_record") == calls + 1  # one refused attempt, no call made


def test_the_walk_continues_the_next_day_from_the_stored_cursor(env, fake):
    fake.today = DAY
    fake.daily_limit = 7
    _, first = start(env, {"mode": "full", "page_budget": 1})
    assert report(first)["stop_reason"] == "budget" and cursor_of(env)["daily_budget"]["date"] == DAY.isoformat()
    fake.today = date(2026, 11, 19)  # UTC midnight passed: the day's counts start again
    fake.calls.clear()
    _, second = start(env, {"mode": "full", "page_budget": 1})
    rep = report(second)
    assert rep["complete"] is True and fetched(fake) == keys(7, 6)
    assert rep["budget"]["used"] == {"product": 7}


# ------------------------------------------------------------------------------------ replay


def test_a_replayed_segment_does_not_double_count_the_budget_or_the_cursor(env, fake):
    fake.today = DAY
    fake.daily_limit = 100
    run_id = str(uuid.uuid4())
    env.rt.run_store.create_kb_source_sync_run(env.tenant, uuid.UUID(run_id), env.source.id, {"mode": "full"})
    cursor = {"cursor": 5, "walk_started_at": "2026-11-18T00:00:00+00:00", "walk_mode": "full",
              "daily_budget": {"date": DAY.isoformat(), "used": {"product": 5}}}
    args = (run_id, env.tenant, env.source.id, 2, _ctx(env), "full", cursor, [], 1, 100)
    one = asyncio.run(sync.segment_step(*args))
    two = asyncio.run(sync.segment_step(*args))  # the crash replay: same input, nothing recorded yet
    assert one["next_cursor"]["daily_budget"] == two["next_cursor"]["daily_budget"] == {"date": DAY.isoformat(), "used": {"product": 10}}
    assert one["budget"] == two["budget"] and one["calls_used"] == two["calls_used"] == 5
    assert (two["counters"]["inserted"], two["counters"]["updated"]) == (0, 0)


def test_a_checkpoint_with_a_1486_page_cursor_fits_the_8_kib_cap(env):
    cursor = {
        "page": 1485, "item": 99, "walk_seen": 148_500, "walk_digest": "f" * 16, "walk_stamp": "2026-11-18T00:00:00Z",
        "walk_started_at": "2026-11-18T00:00:00.123456+00:00", "walk_mode": "incremental",
        "daily_budget": {"date": "2026-11-18", "used": {"product": 2999, "orchestrated": 1500}},
    }
    checkpoint = {"cursor": cursor, "segments": 400}
    assert len(json.dumps(checkpoint, separators=(",", ":"))) < 600
    assert env.sources.set_checkpoint(env.tenant, env.source.id, checkpoint, completed=False) is True
    assert env.sources.get(env.tenant, env.source.id).last_checkpoint == checkpoint
