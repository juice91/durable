"""Model settings (W1.1) in the worker: per-run overrides merged over the spec, max_steps folded
into the request cap, max_retries as the agent's retry budgets."""

from __future__ import annotations

from eap_schemas import AgentSpec
from eap_worker.capabilities import retries_for
from eap_worker.workflow import apply_run_model_settings, usage_limits_for

BASE = {"name": "A", "model": {"provider": "test", "name": "echo"}}


def spec(**fields) -> AgentSpec:
    return AgentSpec.model_validate({**BASE, **fields})


def test_no_override_returns_the_spec_itself():
    s = spec()
    assert apply_run_model_settings(s, None) is s
    assert apply_run_model_settings(s, {}) is s


def test_override_wins_only_where_set():
    s = spec(model_settings={"temperature": 0.2, "top_p": 0.9})
    merged = apply_run_model_settings(s, {"top_p": 0.1, "seed": 5})
    assert merged.model_settings.set_values() == {"temperature": 0.2, "top_p": 0.1, "seed": 5}
    assert s.model_settings.top_p == 0.9  # the spec is not changed


def test_max_steps_is_the_request_limit():
    assert usage_limits_for(spec()) is None
    s = apply_run_model_settings(spec(), {"max_steps": 3})
    assert usage_limits_for(s).request_limit == 3
    assert s.limits.max_requests == 3


def test_max_steps_never_raises_an_existing_request_cap():
    s = spec(limits={"max_requests": 2, "max_tool_calls": 7}, model_settings={"max_steps": 9})
    effective = apply_run_model_settings(s, None)
    assert (effective.limits.max_requests, effective.limits.max_tool_calls) == (2, 7)
    lower = apply_run_model_settings(s, {"max_steps": 1})
    assert lower.limits.max_requests == 1 and lower.limits.max_tool_calls == 7


def test_max_retries_budgets():
    assert retries_for(spec()) is None
    assert retries_for(spec(model_settings={"max_retries": 4})) == {"tools": 4, "output": 4}
    assert retries_for(spec(model_settings={"max_retries": 0})) == {"tools": 0, "output": 0}
    structured = spec(model_settings={"max_retries": 4}, output={"json_schema": {"type": "object"}, "max_retries": 2})
    assert retries_for(structured) == {"tools": 4, "output": 2}
