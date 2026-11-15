"""LP.7 unit checks (no database): error mapping, recorded-limit defaults, run bounds."""

from __future__ import annotations

import pickle

import pytest

from eap_schemas.workflow_scope import LOOP_ERROR_MESSAGES
from eap_schemas.workflows import WorkflowSpec
from eap_worker.errors import WORKFLOW_FAILURES, WorkflowStepError, classify
from eap_worker.workflow_eval import LoopItemsError
from eap_worker.workflow_run import _check_run_bounds, loop_limit, read_limits


@pytest.mark.parametrize("code", ["loop_items_not_list", "loop_items_exceeded"])
def test_loop_items_error_is_a_step_failure_with_its_code(code):
    exc = LoopItemsError(code, "each", 3, 2)
    failure = classify(exc)
    assert failure.code == code and failure.status == "failed"
    assert failure.message == LOOP_ERROR_MESSAGES[code]
    # Through DBOS's recorded (pickled) exception too.
    assert classify(pickle.loads(pickle.dumps(exc))).code == code


def test_every_loop_code_is_a_workflow_failure():
    for code, message in LOOP_ERROR_MESSAGES.items():
        assert WORKFLOW_FAILURES[code] == ("failed", message)
        assert classify(WorkflowStepError(code, "lp")).message == message


def test_loop_limit_falls_back_for_old_checkpoints(monkeypatch):
    old = {"max_step_output_bytes": 1, "max_context_bytes": 2, "max_run_seconds": 3}
    assert loop_limit(old, "max_loop_state_bytes") == 16 * 1024
    assert loop_limit(old, "max_loop_output_bytes") == 256 * 1024
    assert loop_limit(old, "max_step_executions") == 2000
    monkeypatch.setenv("EAP_WORKFLOW_MAX_LOOP_STATE_BYTES", "99")
    assert loop_limit(read_limits(), "max_loop_state_bytes") == 99


def _spec(max_iterations: int, body_steps: int = 1) -> WorkflowSpec:
    steps = [{"id": f"s{i}", "type": "tool", "tool_id": "clock.now", "input": {},
              "next": f"s{i + 1}" if i + 1 < body_steps else None} for i in range(body_steps)]
    return WorkflowSpec.model_validate({
        "name": "b", "input_schema": {"type": "object"}, "start": "lp",
        "steps": [{"id": "lp", "type": "loop", "max_iterations": max_iterations,
                   "until": {"op": "exists", "left": {"$ref": "loop.lp.index"}},
                   "body": {"start": "s0", "steps": steps}}],
    })


def test_run_bounds_guard_only_loop_specs():
    _check_run_bounds(_spec(100, 2), {})  # 1 + 100 * 2 <= 2000 (defaults)
    with pytest.raises(WorkflowStepError) as err:
        _check_run_bounds(_spec(10, 3), {"max_step_executions": 30})
    assert err.value.code == "workflow_too_many_executions"
    plain = WorkflowSpec.model_validate({
        "name": "p", "input_schema": {"type": "object"}, "start": "t",
        "steps": [{"id": "t", "type": "tool", "tool_id": "clock.now", "input": {}}],
    })
    _check_run_bounds(plain, {"max_step_executions": 0})  # never checked without loops


def _operational():
    from sqlalchemy.exc import OperationalError

    return OperationalError("SELECT 1", {}, Exception("server closed the connection unexpectedly"))


def test_immutable_read_retries_transient_db_errors(monkeypatch):
    import asyncio

    import eap_worker.workflow_run as wr

    monkeypatch.setattr(wr, "IMMUTABLE_READ_DELAYS", (0.0, 0.0, 0.0))
    calls = []

    def flaky(x):
        calls.append(x)
        if len(calls) < 3:
            raise _operational()
        return {"ok": x}

    assert asyncio.run(wr.immutable_read(flaky, 7)) == {"ok": 7} and len(calls) == 3


def test_immutable_read_gives_up_and_never_retries_other_errors(monkeypatch):
    import asyncio

    import eap_worker.workflow_run as wr

    monkeypatch.setattr(wr, "IMMUTABLE_READ_DELAYS", (0.0, 0.0))
    calls = []

    def down():
        calls.append(1)
        raise _operational()

    from sqlalchemy.exc import OperationalError

    with pytest.raises(OperationalError):
        asyncio.run(wr.immutable_read(down))
    assert len(calls) == 3  # two retries, then the error is raised

    calls.clear()

    def broken():
        calls.append(1)
        raise KeyError("not transient")

    with pytest.raises(KeyError):
        asyncio.run(wr.immutable_read(broken))
    assert len(calls) == 1
