"""AC.5 re-review: crash on the token-cap path right after the breaching response's event and usage
row are committed (before the append_event step's checkpoint), then recover in a fresh process:
exactly one usage row, one usage_limit_exceeded end, gap-free events. Real subprocesses, always
stopped in `finally` (crash_and_recover)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from worker_tests.test_crash_recovery import crash_schema  # noqa: F401 - fixture
from worker_tests.test_limits_fallback import events_by_key, spec_with, usage_rows
from worker_tests.test_limits_fallback_crash import assert_clean, crash_and_recover
from worker_tests.test_processors_runtime import PII, echo_spec


@pytest.mark.parametrize(
    "spec",
    [
        pytest.param(spec_with(model="priced", limits={"max_output_tokens": 499}), id="plain"),
        pytest.param({**echo_spec(output_processors=[PII], model="priced"), "limits": {"max_output_tokens": 499}},
                     id="output-processors"),
    ],
)
def test_token_breach_recovers_with_one_row_and_one_failure(worker, tenant, crash_schema, tmp_path, spec):
    crash = {"EAP_CRASH_AT": "after_event_write", "EAP_CRASH_EVENT_KEY": "model:0:final"}
    run_id, row = crash_and_recover(worker, tenant, crash_schema, tmp_path, spec, "hi", crash)
    assert row["status"] == "failed", (row["error"], row["output"])
    assert row["error"] == "UsageLimitExceeded: the run exceeded a usage limit"
    events = assert_clean(worker, run_id, completed=0)
    assert [e[1] for e in events] == ["start", "model:0:final", "end"]
    assert sum(1 for e in events if e[2] == "run_failed") == 1
    end = events_by_key(worker.engine, run_id)["end"][1]
    assert (end["error_code"], end["limit"], end["value"]) == ("usage_limit_exceeded", "max_output_tokens", 499)
    assert [(r["request_key"], r["cost_usd"]) for r in usage_rows(worker.engine, run_id)] == [
        ("model:0", Decimal("0.0200000000"))
    ]
    assert row["cost_usd"] == Decimal("0.0200000000")
