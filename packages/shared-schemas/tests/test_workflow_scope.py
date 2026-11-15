"""LP.2: the workflow scope contract (eap_schemas.workflow_scope)."""

import ast
import pickle
import uuid
from pathlib import Path
from typing import get_args

import pytest

import eap_schemas
from eap_schemas import RunEvent, RunEventType
from eap_schemas.workflow_scope import (
    CHILD_RUN_NAMESPACE,
    EMPTY_SCOPE,
    LOOP_ERROR_CODES,
    LOOP_ERROR_MESSAGES,
    LOOP_WORKFLOW_FAILURES,
    MAX_TOOL_CALL_ID_LENGTH,
    ScopeFrame,
    ScopeInvalid,
    child_scope,
    instance_token,
    iteration_event_key,
    iteration_workflow_id,
    scoped_judge_key,
    loop_path,
    make_scope,
    parse_instance_token,
    parse_scope,
    scope_from_data,
    scope_string,
    scope_to_data,
    scoped_child_run_id,
    scoped_event_key,
    scoped_guard_key,
    scoped_parent_step_id,
    scoped_tool_call_id,
    status_for_end,
)

RUN = "11111111-2222-3333-4444-555555555555"
ONE = (ScopeFrame("each", 3),)
TWO = (ScopeFrame("outer", 2), ScopeFrame("inner", 0))


def test_scope_strings():
    assert scope_string(EMPTY_SCOPE) == ""
    assert scope_string(ONE) == "each.3"
    assert scope_string(TWO) == "outer.2/inner.0"
    assert child_scope(child_scope(EMPTY_SCOPE, "outer", 2), "inner", 0) == TWO
    assert parse_scope("outer.2/inner.0") == TWO
    assert parse_scope("") == EMPTY_SCOPE
    assert loop_path(TWO) == ("outer", "inner")
    assert make_scope([("each", 3)]) == ONE


@pytest.mark.parametrize("bad", ["each", "each.-1", "each.01", "Each.1", "a.1//b.2", "a.1/", "1a.0"])
def test_parse_scope_rejects(bad):
    with pytest.raises(ScopeInvalid):
        parse_scope(bad)


@pytest.mark.parametrize("frame", [("Bad", 0), ("ok", -1), ("ok", True), ("ok", "1"), ("", 0)])
def test_frames_validated(frame):
    with pytest.raises(ScopeInvalid):
        make_scope([frame])


def test_scope_data_round_trip():
    assert scope_to_data(TWO) == [{"loop": "outer", "index": 2}, {"loop": "inner", "index": 0}]
    assert scope_from_data(scope_to_data(TWO)) == TWO
    assert scope_from_data(None) == EMPTY_SCOPE
    assert scope_from_data([]) == EMPTY_SCOPE
    with pytest.raises(ScopeInvalid):
        scope_from_data([{"loop": "a", "index": 0, "x": 1}])


def test_empty_scope_keeps_todays_identifiers():
    assert scoped_event_key(EMPTY_SCOPE, "fetch", "started") == "step:fetch:started"
    assert scoped_event_key(EMPTY_SCOPE, "fetch", "completed") == "step:fetch:completed"
    assert scoped_event_key(EMPTY_SCOPE, "fetch", "failed") == "step:fetch:failed"
    # The value eap_worker.workflow_run.child_run_id has always produced.
    assert CHILD_RUN_NAMESPACE == uuid.UUID("6f2b8e0a-5f37-4c1e-9d0b-8f8e2c7a1d42")
    assert scoped_child_run_id(RUN, EMPTY_SCOPE, "fetch") == str(uuid.uuid5(CHILD_RUN_NAMESPACE, f"{RUN}:fetch"))
    assert scoped_parent_step_id(EMPTY_SCOPE, "fetch") == "fetch"
    assert scoped_guard_key(RUN, EMPTY_SCOPE, "fetch") == f"{RUN}:fetch"
    assert scoped_tool_call_id(EMPTY_SCOPE, "fetch") == "wf_fetch"
    assert instance_token(EMPTY_SCOPE, "fetch") == "fetch"
    assert scoped_judge_key(EMPTY_SCOPE, "judge", 0) == "judge:judge:0"


def test_scoped_formats():
    assert scoped_event_key(TWO, "triage", "completed") == "loop:outer.2/inner.0:step:triage:completed"
    assert iteration_event_key(ONE, "started") == "loop:each.3:started"
    assert iteration_event_key(TWO, "failed") == "loop:outer.2/inner.0:failed"
    assert scoped_child_run_id(RUN, TWO, "triage") == str(
        uuid.uuid5(CHILD_RUN_NAMESPACE, f"{RUN}:outer.2/inner.0:triage")
    )
    assert scoped_parent_step_id(TWO, "triage") == "outer.2/inner.0/triage"
    assert scoped_guard_key(RUN, ONE, "update") == f"{RUN}:each.3:update"
    assert scoped_tool_call_id(ONE, "update") == "wf_each.3~update"
    assert scoped_tool_call_id(TWO, "update") == "wf_outer.2~inner.0~update"
    assert iteration_workflow_id(RUN, ONE) == f"{RUN}:loop:each.3"
    assert iteration_workflow_id(RUN, TWO) == f"{RUN}:loop:outer.2/inner.0"
    assert scoped_judge_key(ONE, "judge", 2) == "judge:each.3:judge:2"
    assert instance_token(ONE, "triage") == "each.3~triage"
    assert instance_token(TWO, "triage") == "outer.2~inner.0~triage"


def test_ids_distinct_per_iteration():
    scopes = [(ScopeFrame("each", i),) for i in range(5)]
    for fn in (
        lambda s: scoped_child_run_id(RUN, s, "x"),
        lambda s: scoped_guard_key(RUN, s, "x"),
        lambda s: scoped_tool_call_id(s, "x"),
        lambda s: instance_token(s, "x"),
        lambda s: scoped_event_key(s, "x", "started"),
        lambda s: iteration_workflow_id(RUN, s),
    ):
        assert len({fn(s) for s in scopes}) == len(scopes)


def test_iteration_identifiers_need_a_scope():
    with pytest.raises(ScopeInvalid):
        iteration_event_key(EMPTY_SCOPE, "started")
    with pytest.raises(ScopeInvalid):
        iteration_workflow_id(RUN, EMPTY_SCOPE)
    with pytest.raises(ScopeInvalid):
        iteration_event_key(ONE, "waiting")  # type: ignore[arg-type]


def test_instance_token_is_one_segment_and_round_trips():
    for scope in (EMPTY_SCOPE, ONE, TWO):
        token = instance_token(scope, "triage")
        assert "/" not in token
        assert parse_instance_token(token) == (scope, "triage")


def test_tool_call_id_bounded_at_max_depth_and_lengths():
    loop_a, loop_b, step = "a" * 64, "b" * 64, "c" * 64
    scope = (ScopeFrame(loop_a, 999), ScopeFrame(loop_b, 999))
    tcid = scoped_tool_call_id(scope, step)
    assert len(tcid) <= MAX_TOOL_CALL_ID_LENGTH
    assert tcid.startswith("wf_a")
    assert tcid == scoped_tool_call_id(scope, step)  # deterministic
    other = scoped_tool_call_id((ScopeFrame(loop_a, 999), ScopeFrame(loop_b, 998)), step)
    assert len(other) <= MAX_TOOL_CALL_ID_LENGTH and other != tcid
    # Ids that fit are never digested.
    short = scoped_tool_call_id((ScopeFrame("x", 1), ScopeFrame("y", 2)), "z")
    assert short == "wf_x.1~y.2~z"


def test_tool_call_id_injective_across_scope_and_step():
    # The old `_` mapping collided on all of these pairs.
    assert scoped_tool_call_id(EMPTY_SCOPE, "a_1_s") != scoped_tool_call_id((ScopeFrame("a", 1),), "s")
    assert scoped_tool_call_id((ScopeFrame("a", 1),), "b_0_s") != scoped_tool_call_id(
        (ScopeFrame("a", 1), ScopeFrame("b", 0)), "s"
    )


@pytest.mark.parametrize("bad", ["~a", "~", "a~", "a.0~", "~a.0~s", "a.0~~s", "a.01~s"])
def test_parse_instance_token_rejects_non_canonical(bad):
    with pytest.raises(ScopeInvalid):
        parse_instance_token(bad)


def test_invalid_step_ids_rejected():
    for fn in (
        lambda: scoped_event_key(ONE, "Bad", "started"),
        lambda: scoped_event_key(ONE, "ok", "Bad:phase"),
        lambda: scoped_tool_call_id(ONE, "a/b"),
        lambda: instance_token(ONE, "a~b"),
        lambda: scoped_judge_key(ONE, "j", -1),
        lambda: scoped_judge_key(ONE, "j", True),
    ):
        with pytest.raises(ScopeInvalid):
            fn()


def test_status_for_end():
    assert status_for_end("run_completed", {"output": "x"}) == "completed"
    assert status_for_end("run_completed") == "completed"
    assert status_for_end("run_failed", {"status": "failed", "error_code": "run_cancelled"}) == "failed"
    assert status_for_end("run_failed", {"status": "needs_attention"}) == "needs_attention"
    assert status_for_end("run_failed", {}) == "failed"
    assert status_for_end("run_failed", {"status": "running"}) == "failed"
    with pytest.raises(ValueError):
        status_for_end("step_completed", {})


def test_new_event_types_registered():
    args = get_args(RunEventType)
    for name in ("loop_iteration_started", "loop_iteration_completed", "loop_iteration_failed", "cancel_requested"):
        assert name in args
        RunEvent(run_id=uuid.uuid4(), seq=0, type=name, ts="2026-11-15T00:00:00Z")


def test_error_codes_registered_with_messages():
    expected = {
        "loop_items_exceeded",
        "loop_items_not_list",
        "loop_max_iterations",
        "loop_state_too_large",
        "loop_body_step_not_allowed",
        "loop_in_parallel_not_allowed",
        "loop_ref_not_on_every_path",
        "workflow_too_many_executions",
        "workflow_concurrency_exceeded",
        "run_cancelled",
        "evaluate_failed",
    }
    assert set(LOOP_ERROR_CODES) == expected == set(LOOP_ERROR_MESSAGES) == set(LOOP_WORKFLOW_FAILURES)
    assert all(msg and msg == msg.strip() for msg in LOOP_ERROR_MESSAGES.values())
    assert LOOP_WORKFLOW_FAILURES["run_cancelled"] == ("failed", "the run was cancelled")
    assert "run_budget_exceeded" not in LOOP_ERROR_MESSAGES  # AC.5's code is reused, not redefined


def test_scope_invalid_pickles():
    exc = pickle.loads(pickle.dumps(ScopeInvalid("bad scope")))
    assert isinstance(exc, ScopeInvalid) and str(exc) == "bad scope"


def test_package_exports():
    for name in (
        "status_for_end", "scoped_event_key", "scoped_guard_key", "scoped_tool_call_id",
        "iteration_workflow_id", "instance_token", "scoped_judge_key", "LOOP_WORKFLOW_FAILURES",
    ):
        assert name in eap_schemas.__all__
        assert getattr(eap_schemas, name) is not None


def test_no_io_imports():
    source = Path(eap_schemas.__file__).with_name("workflow_scope.py").read_text()
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "hashlib", "re", "uuid", "collections", "typing"}
