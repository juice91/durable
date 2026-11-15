"""AC.5 acceptance (tester): usage limits at their boundaries, the cost cap, fallback attribution,
interactions (structured output, memory), golden step lists. Crash recovery is in
test_limits_fallback_crash.py. Builds on the implementer's helpers in test_limits_fallback.py."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import pytest

from eap_schemas.limits import RunBudgetExceeded
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, wait_terminal
from worker_tests.test_conversation import step_names
from worker_tests.test_limits_fallback import events_by_key, fallbacks, run, spec_with, usage_rows

# test:priced: 1000 input + 500 output tokens and USD 0.02 per request.
IN, OUT, TOTAL = 1000, 500, 1500


def _end(worker, run_id) -> dict[str, Any]:
    return events_by_key(worker.engine, run_id)["end"][1]


# --------------------------------------------------------------------------- native limits


@pytest.mark.parametrize(
    ("field", "used"),
    [("max_input_tokens", IN), ("max_output_tokens", OUT), ("max_total_tokens", TOTAL)],
)
def test_token_limit_boundary_and_one_past(worker, client, tenant, field, used):
    """A limit equal to the usage passes; one below fails with {limit, value}."""
    ok_id, ok = run(worker, client, tenant, spec_with(model="priced", limits={field: used}), "hi")
    assert ok["status"] == "completed", ok
    assert _end(worker, ok_id).get("error_code") is None
    bad_id, bad = run(worker, client, tenant, spec_with(model="priced", limits={field: used - 1}), "hi")
    assert bad["status"] == "failed" and bad["error"] == "UsageLimitExceeded: the run exceeded a usage limit", bad
    end = _end(worker, bad_id)
    assert end["error_code"] == "usage_limit_exceeded"
    assert (end["limit"], end["value"]) == (field, used - 1)


def test_request_limit_boundary_and_one_past(worker, client, tenant):
    """rounds=2 makes 3 requests: max_requests=3 passes, 2 fails before the third request."""
    prompt = '{"rounds": 2}'
    spec = spec_with(model="priced", tools=("clock.now",))
    ok_id, ok = run(worker, client, tenant, {**spec, "limits": {"max_requests": 3}}, prompt)
    assert ok["status"] == "completed", ok
    assert [r["request_key"] for r in usage_rows(worker.engine, ok_id)] == ["model:0", "model:1", "model:2"]
    bad_id, bad = run(worker, client, tenant, {**spec, "limits": {"max_requests": 2}}, prompt)
    assert bad["status"] == "failed"
    end = _end(worker, bad_id)
    assert (end["error_code"], end["limit"], end["value"]) == ("usage_limit_exceeded", "max_requests", 2)
    assert [r["request_key"] for r in usage_rows(worker.engine, bad_id)] == ["model:0", "model:1"]


def test_tool_call_limit_boundary_and_one_past(worker, client, tenant):
    prompt = '{"rounds": 2}'  # two tool calls, then the answer
    spec = spec_with(model="priced", tools=("clock.now",))
    _, ok = run(worker, client, tenant, {**spec, "limits": {"max_tool_calls": 2}}, prompt)
    assert ok["status"] == "completed", ok
    bad_id, bad = run(worker, client, tenant, {**spec, "limits": {"max_tool_calls": 1}}, prompt)
    assert bad["status"] == "failed"
    end = _end(worker, bad_id)
    assert (end["error_code"], end["limit"], end["value"]) == ("usage_limit_exceeded", "max_tool_calls", 1)


def test_no_limits_keeps_the_default_fifty_request_cap(worker, client, tenant):
    run_id, row = run(worker, client, tenant, spec_with(model="loop", tools=("clock.now",)))
    assert row["status"] == "failed"
    end = _end(worker, run_id)
    assert (end["error_code"], end["limit"], end["value"]) == ("usage_limit_exceeded", "max_requests", 50)
    assert len(usage_rows(worker.engine, run_id)) == 50


# --------------------------------------------------------------------------- the cost cap


def _priced_spec(cap: str, **extra: Any) -> dict[str, Any]:
    return spec_with(model="priced", tools=("clock.now",), limits={"max_cost_usd": cap}, **extra)


def test_cost_cap_boundary_at_exactly_the_spend(worker, client, tenant):
    """rounds=2 is three requests at USD 0.02. Before request 2 the spend is 0.04."""
    prompt = '{"rounds": 2}'
    run_id, row = run(worker, client, tenant, _priced_spec("0.04"), prompt)  # sum >= cap raises
    assert row["status"] == "failed" and row["error"] == "RunBudgetExceeded: the run reached its cost limit"
    end = _end(worker, run_id)
    assert (end["error_code"], end["limit"], end["value"]) == ("run_budget_exceeded", "max_cost_usd", "0.04")
    assert Decimal(end["spent_usd"]) == Decimal("0.04")
    assert [r["request_key"] for r in usage_rows(worker.engine, run_id)] == ["model:0", "model:1"]
    run_id, row = run(worker, client, tenant, _priced_spec("0.0401"), prompt)  # one cent-fraction above
    assert row["status"] == "completed", row
    assert [r["request_key"] for r in usage_rows(worker.engine, run_id)] == ["model:0", "model:1", "model:2"]


def test_cost_cap_is_not_checked_before_request_zero_or_without_a_cap(worker, client, tenant):
    # The smallest cap (0.0001) still lets request 0 run; the answer comes in one request.
    run_id, row = run(worker, client, tenant, spec_with(model="priced", limits={"max_cost_usd": "0.0001"}), "hi")
    assert row["status"] == "completed", row
    assert "eap.check_budget" not in step_names(worker.settings.dbos_system_schema, run_id)
    # And without a cap a long priced run has no check_budget step at all.
    run_id, row = run(worker, client, tenant, spec_with(model="priced", tools=("clock.now",)), '{"rounds": 3}')
    assert row["status"] == "completed"
    assert "eap.check_budget" not in step_names(worker.settings.dbos_system_schema, run_id)


def test_cost_cap_counts_input_moderation(worker, client, tenant):
    mod = {"type": "moderation", "config": {"model": {"provider": "test", "name": "moderation"}}}
    probe_id, probe = run(worker, client, tenant, spec_with(model="priced", input_processors=[mod]), '{"rounds": 0}')
    assert probe["status"] == "completed", probe
    rows = {r["request_key"]: r["cost_usd"] for r in usage_rows(worker.engine, probe_id)}
    moderation_cost = next(c for k, c in rows.items() if k.startswith("moderation:"))
    assert moderation_cost is not None and moderation_cost > 0
    # The cap's 4-decimal floor is above a moderation call's cost, so assert what check_budget reads:
    # the run's recorded sum covers the chat row and the real moderation row.
    assert worker.run_store.recorded_cost_usd(tenant, probe_id) == Decimal("0.02") + Decimal(moderation_cost)


def _usage(key: str, cost: str | None, model: str = "test:echo") -> dict[str, Any]:
    return {"request_key": key, "model_id": model, "provider_model_name": None, "input_tokens": 1,
            "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0, "usage_measured": True,
            "cost_usd": None if cost is None else Decimal(cost), "price_source": None if cost is None else "override"}


def test_check_budget_sums_chat_failed_embedding_and_moderation_rows_and_counts_unpriced_as_zero(worker, tenant):
    from eap_worker.workflow import check_budget_step

    run_id = uuid.uuid4()
    worker.run_store.create_run(tenant, uuid.uuid4(), 1, "hi", run_id)
    rows = [("model:0", "0.010"), ("model:0:failed:0", "0.005"), ("embed:retrieve:c1", "0.002"),
            ("embed:recall:1", "0.001"), ("moderation:input:1", "0.003"), ("model:1", None)]
    for i, (key, cost) in enumerate(rows):
        worker.event_log.append(tenant, run_id, f"k{i}", "model_message", {}, _usage(key, cost))
    total = Decimal("0.021")  # the unpriced model:1 adds nothing
    assert worker.run_store.recorded_cost_usd(tenant, run_id) == total

    def check(cap: str) -> str:
        return asyncio.run(check_budget_step(str(run_id), tenant, 1, cap))

    assert Decimal(check("0.0211")) == total
    with pytest.raises(RunBudgetExceeded) as raised:
        check("0.021")  # at the cap: refused
    assert raised.value.spent_usd == total
    with pytest.raises(RunBudgetExceeded):
        check("0.02")


# --------------------------------------------------------------------------- fallback


def _answer(worker, run_id, key="model:0:final") -> dict[str, Any]:
    return events_by_key(worker.engine, run_id)[key][1]


def test_503_falls_through_to_the_next_member_and_a_400_does_too(worker, client, tenant):
    run_id, row = run(worker, client, tenant, spec_with(model="fail-503", fallback_models=fallbacks("echo")), "hi")
    assert row["status"] == "completed" and row["output"] == "Echo: hi"
    # FallbackModel's default fallback_on is ModelAPIError, and ModelHTTPError(400) is one (models.py doc).
    run_id, row = run(worker, client, tenant, spec_with(model="fail-400", fallback_models=fallbacks("echo")), "hi")
    assert row["status"] == "completed" and _answer(worker, run_id)["fallback_index"] == 1, row


def test_chain_of_three_answered_by_the_last_member(worker, client, tenant):
    spec = spec_with(model="fail-503", fallback_models=fallbacks("fail-400", "priced"))
    run_id, row = run(worker, client, tenant, spec, "hi")
    assert row["status"] == "completed" and row["output"] == "Echo: hi", row
    data = _answer(worker, run_id)
    assert data["model_id"] == "test:priced" and data["requested_model_id"] == "test:fail-503"
    assert data["fallback_index"] == 2
    assert [(a["model_id"], a["outcome"]) for a in data["failed_attempts"]] == [
        ("test:fail-503", "error"), ("test:fail-400", "error")]
    # Priced as the answering model: test:priced's 1000/500 tokens are USD 0.02, not the test:* rate.
    [usage] = usage_rows(worker.engine, run_id)
    assert (usage["request_key"], usage["model_id"], usage["cost_usd"]) == ("model:0", "test:priced", Decimal("0.02"))


def test_the_event_text_never_carries_the_provider_error(worker, client, tenant):
    """Plan: failed_attempts carries no error text, and exception text never leaves the worker."""
    run_id, _ = run(worker, client, tenant, spec_with(model="fail-503", fallback_models=fallbacks("echo")), "hi")
    data = _answer(worker, run_id)
    assert "status_code" not in repr(data["failed_attempts"])
    for _seq, key, _type, event in event_rows(worker.engine, run_id):
        blob = repr(event)
        assert "status_code" not in blob and "scripted failure" not in blob, (key, blob[:300])


def test_fallback_exhausted_writes_no_usage_rows(worker, client, tenant):
    spec = spec_with(model="fail-400", fallback_models=fallbacks("fail-503"))
    run_id, row = run(worker, client, tenant, spec)
    assert row["status"] == "failed" and row["error"].startswith("FallbackExceptionGroup")
    end = _end(worker, run_id)
    assert (end["error_code"], end["cause"]) == ("fallback_exhausted", "model_http_error")
    assert usage_rows(worker.engine, run_id) == []


def test_each_member_is_checked_against_the_allowlist(worker, client, tenant):
    # The second of two fallbacks is off the allowlist: the whole run fails closed before any request.
    bad = [{"provider": "test", "name": "echo"}, {"provider": "litellm", "name": "not-allowed-here"}]
    run_id, row = run(worker, client, tenant, spec_with(model="fail-503", fallback_models=bad))
    assert row["status"] == "failed" and "ModelNotAllowed" in row["error"], row
    assert [k for _s, k, _t, _d in event_rows(worker.engine, run_id)] == ["start", "end"]
    assert usage_rows(worker.engine, run_id) == []


def test_override_to_the_fallback_model_runs_it_bare(worker, client, tenant):
    spec = spec_with(model="fail-503", fallback_models=fallbacks("echo"))
    run_id, agent_id = uuid.uuid4(), uuid.uuid4()
    worker.run_store.create_run(tenant, agent_id, 1, "hi", run_id, model_id="test:echo", model_overridden=True)
    run_id, row = run(worker, client, tenant, spec, run_id=run_id, agent_id=agent_id, create_row=False)
    assert row["status"] == "completed", row
    assert events_by_key(worker.engine, run_id)["start"][1]["fallback"] == "disabled_by_override"
    assert "fallback_index" not in _answer(worker, run_id)  # a bare model: no chain fields
    assert [(r["request_key"], r["model_id"]) for r in usage_rows(worker.engine, run_id)] == [("model:0", "test:echo")]


def test_no_fallback_field_on_start_when_not_overridden(worker, client, tenant):
    run_id, _ = run(worker, client, tenant, spec_with(model="fail-503", fallback_models=fallbacks("echo")), "hi")
    assert "fallback" not in events_by_key(worker.engine, run_id)["start"][1]
    run_id, _ = run(worker, client, tenant, spec_with(), "hi")
    assert "fallback" not in events_by_key(worker.engine, run_id)["start"][1]


# --------------------------------------------------------------------------- golden step lists


def test_native_limits_alone_and_fallback_alone_keep_the_golden_steps(worker, client, tenant):
    from worker_tests.test_step_layout_ac import GOLDEN

    schema = worker.settings.dbos_system_schema
    base = spec_with(model="echo")
    for extra in ({"limits": {"max_requests": 9, "max_input_tokens": 900_000}},
                  {"fallback_models": fallbacks("history")}):
        run_id, row = run(worker, client, tenant, {**base, **extra}, "hi")
        assert row["status"] == "completed", row
        assert step_names(schema, run_id) == GOLDEN["plain"], extra
    # A fallback that actually answers still adds no step: the chain lives in the model step.
    run_id, row = run(worker, client, tenant, spec_with(model="fail-503", fallback_models=fallbacks("echo")), "hi")
    assert row["status"] == "completed" and step_names(schema, run_id) == GOLDEN["plain"]


def test_a_spec_without_limits_or_fallback_has_no_new_event_fields(worker, client, tenant):
    run_id, row = run(worker, client, tenant, spec_with(model="echo"), "hi")
    assert row["status"] == "completed"
    message = _answer(worker, run_id)
    assert not {"requested_model_id", "fallback_index", "failed_attempts"} & set(message)
    assert "fallback" not in events_by_key(worker.engine, run_id)["start"][1]


# --------------------------------------------------------------------------- interactions


def test_fallback_with_structured_output(worker, client, tenant):
    from worker_tests.test_step_layout_ac import SCHEMA

    spec = spec_with(model="fail-503", fallback_models=fallbacks("structured"), output={"json_schema": SCHEMA})
    run_id, row = run(worker, client, tenant, spec, "hi")
    assert row["status"] == "completed", row
    assert row["output"] == '{"answer":"x","score":1}' and row["output_format"] == "json"
    data = _answer(worker, run_id)
    assert data["model_id"] == "test:structured" and data["requested_model_id"] == "test:fail-503"
    assert data["fallback_index"] == 1 and data["output_candidate"] is True
    assert [(r["request_key"], r["model_id"]) for r in usage_rows(worker.engine, run_id)] == [
        ("model:0", "test:structured")]
    done = [d for _s, _k, t, d in event_rows(worker.engine, run_id) if t == "run_completed"]
    assert len(done) == 1 and done[0]["output_json"] == {"answer": "x", "score": 1}


def test_fallback_with_memory_history_answers_the_second_turn(worker, client, tenant):
    from worker_tests.test_working_memory import conversation, memory_spec, turn

    conv = conversation(worker, tenant)
    spec = {**memory_spec(model="fail-503", history={"last_turns": 5}), "fallback_models": fallbacks("history")}
    _, first = turn(worker, client, tenant, conv, "my name is Ada", spec)
    assert first["status"] == "completed", first
    run_id, second = turn(worker, client, tenant, conv, "what did I say?", spec)
    assert second["status"] == "completed" and "my name is Ada" in second["output"], second
    assert _answer(worker, run_id)["model_id"] == "test:history"
    assert [(r["request_key"], r["model_id"]) for r in usage_rows(worker.engine, run_id)] == [("model:0", "test:history")]


def test_the_observer_has_no_fallback(worker, client, worker_settings, tenant):
    from worker_tests.test_observational_memory import by_key, fact, obs_spec, obs_turn
    from worker_tests.test_working_memory import conversation

    conv = conversation(worker, tenant)
    spec = {**obs_spec(observer={"provider": "test", "name": "fail-503"}, keep=0),
            "fallback_models": fallbacks("history")}
    run_id, row = obs_turn(worker, client, worker_settings, tenant, conv, fact(1, 600), spec)
    assert row["status"] == "completed" and row["output"], row
    [(etype, data)] = by_key(event_rows(worker.engine, run_id), "memory:observe:1:failed")
    assert etype == "memory_observe_failed" and data["error_code"] == "model_http_error"
    assert "fallback_index" not in data and "failed_attempts" not in data
    assert [r["request_key"] for r in usage_rows(worker.engine, run_id)] == ["model:0"]
