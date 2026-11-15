"""LP.2 acceptance: the workflow scope contract (eap_schemas.workflow_scope), tester's file."""

import hashlib
import itertools
import pickle
import random
import re
import uuid
from typing import get_args

import pytest

from eap_schemas import RunEventType
from eap_schemas import workflow_scope as ws
from eap_schemas.workflow_scope import (
    EMPTY_SCOPE,
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
NAMES = ["a", "outer", "inner", "each", "x_1", "loop_a", "z" * 64]


def random_scope(rng: random.Random, max_depth: int = 5):
    return tuple(
        (rng.choice(NAMES), rng.choice([0, 1, 2, 9, 10, 11, 99, 100, 12345]))
        for _ in range(rng.randint(0, max_depth))
    )


# --------------------------------------------------------------------------- round trips


def test_string_and_data_round_trip_property():
    rng = random.Random(1234)
    for _ in range(500):
        scope = make_scope(random_scope(rng))
        assert parse_scope(scope_string(scope)) == scope
        assert scope_from_data(scope_to_data(scope)) == scope
        assert loop_path(scope) == tuple(f.loop for f in scope)


def test_round_trip_every_depth_and_shape():
    assert parse_scope("") == EMPTY_SCOPE and scope_string(EMPTY_SCOPE) == ""
    assert scope_string(child_scope(child_scope(EMPTY_SCOPE, "outer", 2), "inner", 0)) == "outer.2/inner.0"
    assert scope_to_data(parse_scope("outer.2/inner.0")) == [
        {"loop": "outer", "index": 2},
        {"loop": "inner", "index": 0},
    ]
    assert scope_from_data(None) == EMPTY_SCOPE and scope_from_data([]) == EMPTY_SCOPE


@pytest.mark.parametrize(
    "bad",
    [
        ".0", "a.", "a", "a.-1", "a.01", "a.00", "a.1.2", "a/b.0", "a.0/", "/a.0", "a.0//b.0",
        "a b.0", "a~b.0", "A.0", "1a.0", "_a.0", "a-b.0", "é.0", "a.٣", "a.0\n", " a.0", "a.0 ",
        "a.+1", "a.1e3", "a.0/ ", "a:b.0", "a" * 65 + ".0",
    ],
)
def test_parse_scope_rejects(bad):
    with pytest.raises(ScopeInvalid):
        parse_scope(bad)


@pytest.mark.parametrize(
    "loop,index",
    [("", 0), ("a/b", 0), ("a.b", 0), ("a~b", 0), ("é", 0), ("a", -1), ("a", True), ("a", 1.0), ("a", "1"),
     ("a", None), (None, 0), (1, 0), ("A", 0), ("a b", 0), ("a\n", 0)],
)
def test_make_scope_and_child_scope_reject(loop, index):
    with pytest.raises(ScopeInvalid):
        make_scope([(loop, index)])
    with pytest.raises(ScopeInvalid):
        child_scope(EMPTY_SCOPE, loop, index)


@pytest.mark.parametrize(
    "data",
    [[{"loop": "a"}], [{"loop": "a", "index": 0, "x": 1}], ["a.0"], [{"loop": "a", "index": -1}],
     [{"loop": "", "index": 0}], [{"loop": "a", "index": "0"}], [{"loop": "a", "index": True}]],
)
def test_scope_from_data_rejects(data):
    with pytest.raises(ScopeInvalid):
        scope_from_data(data)


def test_scope_invalid_is_value_error():
    assert issubclass(ScopeInvalid, ValueError)


def test_instance_token_round_trip_and_single_segment():
    rng = random.Random(99)
    for _ in range(500):
        scope = make_scope(random_scope(rng))
        step = rng.choice(NAMES)
        token = instance_token(scope, step)
        assert "/" not in token
        assert parse_instance_token(token) == (scope, step)
    assert instance_token(EMPTY_SCOPE, "triage") == "triage"
    assert instance_token(parse_scope("each.3"), "triage") == "each.3~triage"
    assert instance_token(parse_scope("outer.2/inner.0"), "triage") == "outer.2~inner.0~triage"


@pytest.mark.parametrize("bad", ["", "~", "a~", "~a", "A", "a/b", "a.0~", "a.0~B", "a.x~s"])
def test_parse_instance_token_rejects(bad):
    with pytest.raises(ScopeInvalid):
        parse_instance_token(bad)


@pytest.mark.parametrize("bad", ["", "A", "a b", "a/b", "a~b", "é", "a.b", "1a"])
def test_bad_step_ids_rejected_everywhere(bad):
    s = parse_scope("l.0")
    for call in (
        lambda: scoped_event_key(s, bad, "started"),
        lambda: scoped_child_run_id(RUN, s, bad),
        lambda: scoped_parent_step_id(s, bad),
        lambda: scoped_guard_key(RUN, s, bad),
        lambda: scoped_tool_call_id(s, bad),
        lambda: scoped_judge_key(s, bad, 0),
        lambda: instance_token(s, bad),
    ):
        with pytest.raises(ScopeInvalid):
            call()


def test_iteration_keys_need_nonempty_scope_and_valid_phase():
    with pytest.raises(ScopeInvalid):
        iteration_event_key(EMPTY_SCOPE, "started")
    with pytest.raises(ScopeInvalid):
        iteration_workflow_id(RUN, EMPTY_SCOPE)
    with pytest.raises(ScopeInvalid):
        iteration_event_key(parse_scope("a.0"), "waiting")
    assert iteration_event_key(parse_scope("a.0/b.1"), "failed") == "loop:a.0/b.1:failed"
    assert iteration_workflow_id(RUN, parse_scope("a.0/b.1")) == f"{RUN}:loop:a.0/b.1"
    with pytest.raises(ScopeInvalid):
        scoped_judge_key(EMPTY_SCOPE, "s", -1)
    with pytest.raises(ScopeInvalid):
        scoped_event_key(EMPTY_SCOPE, "s", "Bad Phase")


# --------------------------------------------------------------------------- uniqueness


def _all_scopes(depth: int):
    """Every scope up to `depth` frames over two loop names and indexes 0, 1, 10, 11."""
    frames = [(n, i) for n in ("a", "b") for i in (0, 1, 10, 11)]
    out = [EMPTY_SCOPE]
    for d in range(1, depth + 1):
        out += [make_scope(c) for c in itertools.product(frames, repeat=d)]
    return out


SCOPES = _all_scopes(2)
STEPS = ["s", "t", "a", "b"]


def _assert_injective(fn, pairs):
    seen = {}
    for scope, step in pairs:
        value = fn(scope, step)
        key = (scope, step)
        assert seen.setdefault(value, key) == key, f"collision on {value!r}: {seen[value]} vs {key}"


PAIRS = [(s, step) for s in SCOPES for step in STEPS]


@pytest.mark.parametrize("phase", ["started", "completed", "waiting"])
def test_event_keys_injective(phase):
    _assert_injective(lambda s, step: scoped_event_key(s, step, phase), PAIRS)


def test_child_run_ids_injective_and_valid_uuids():
    _assert_injective(lambda s, step: scoped_child_run_id(RUN, s, step), PAIRS)
    for s, step in PAIRS[:50]:
        uuid.UUID(scoped_child_run_id(RUN, s, step))


def test_guard_and_parent_step_ids_injective():
    _assert_injective(lambda s, step: scoped_guard_key(RUN, s, step), PAIRS)
    _assert_injective(scoped_parent_step_id, PAIRS)


def test_instance_tokens_injective():
    _assert_injective(instance_token, PAIRS)


def test_judge_keys_injective():
    seen = {}
    for s, step in PAIRS:
        for k in (0, 1, 12):
            v = scoped_judge_key(s, step, k)
            assert seen.setdefault(v, (s, step, k)) == (s, step, k)


def test_iteration_workflow_ids_and_keys_injective():
    scopes = [s for s in SCOPES if s]
    assert len({iteration_workflow_id(RUN, s) for s in scopes}) == len(scopes)
    for phase in ("started", "completed", "failed"):
        assert len({iteration_event_key(s, phase) for s in scopes}) == len(scopes)


def test_event_key_namespaces_do_not_collide_across_kinds():
    keys = {scoped_event_key(s, st, "started") for s, st in PAIRS}
    keys |= {iteration_event_key(s, "started") for s in SCOPES if s}
    assert len(keys) == len({scoped_event_key(s, st, "started") for s, st in PAIRS}) + len(
        [s for s in SCOPES if s]
    )


def test_tool_call_ids_injective_for_one_step_across_scopes():
    # Within a run a step's chain of enclosing loops is fixed (D4), so only indexes vary.
    for chain in (("a",), ("a", "b"), ("a", "b", "a")):
        seen = {}
        for idx in itertools.product((0, 1, 10, 11, 100), repeat=len(chain)):
            scope = make_scope(zip(chain, idx))
            v = scoped_tool_call_id(scope, "s")
            assert seen.setdefault(v, scope) == scope


def test_mapping_collisions_dot_slash_underscore():
    """Tool call ids are `wf_` + the canonical instance token, so pairs that the old `.`/`/` -> `_`
    mapping merged now stay distinct."""
    # Loop names with underscores: `a_b.0` + step `c` vs `a.0` + step `b_c`.
    a = scoped_tool_call_id(parse_scope("a_b.0"), "c")
    b = scoped_tool_call_id(parse_scope("a.0"), "b_c")
    assert a == "wf_a_b.0~c" and b == "wf_a.0~b_c" and a != b
    # A top-level step id that looks like a scoped one: `~` never appears in a top-level id.
    assert scoped_tool_call_id(EMPTY_SCOPE, "a_1_s") == "wf_a_1_s"
    assert scoped_tool_call_id(parse_scope("a.1"), "s") == "wf_a.1~s"
    # Different depth, same characters once `.`/`/`/`~` are ignored.
    c1 = scoped_tool_call_id(parse_scope("a.1"), "b_0_s")
    c2 = scoped_tool_call_id(parse_scope("a.1/b.0"), "s")
    assert c1 == "wf_a.1~b_0_s" and c2 == "wf_a.1~b.0~s"
    assert c1 != c2, "tool call id collision: loop chain a/b step s vs loop a step b_0_s"
    # Index digits are not merged with the next name: `a.1` + `0_s` is not a legal step id, and
    # `a.1` vs `a.10` differ.
    assert scoped_tool_call_id(parse_scope("a.1"), "z0_s") != scoped_tool_call_id(parse_scope("a.10"), "z0_s")


def test_tool_call_id_collision_across_distinct_pairs_exhaustive():
    names = ["a", "b", "b_0", "a_1"]
    frames = [(n, i) for n in ("a", "b") for i in (0, 1)]
    seen = {}
    for d in range(0, 3):
        for combo in itertools.product(frames, repeat=d):
            for step in names + ["s", "b_0_s", "a_1_s", "1_s"]:
                try:
                    v = scoped_tool_call_id(make_scope(combo), step)
                except ScopeInvalid:
                    continue
                key = (combo, step)
                if seen.setdefault(v, key) != key:
                    pytest.fail(f"tool call id collision {v!r}: {seen[v]} vs {key}")


# --------------------------------------------------------------------------- compatibility


# Recorded from eap_worker.workflow_run (child_run_id, side_effect_key, tool_call_id_for) on
# 2026-11-15; the live cross-check lives in apps/worker/worker_tests/test_workflow_scope_parity.py.
_GOLDEN_RUN = "11111111-2222-3333-4444-555555555555"
_GOLDEN_EMPTY_SCOPE = {
    "fetch": ("6c7a2b5e-e2d8-58cc-a38c-f5d78494ebb1", "wf_fetch"),
    "a_1_s": ("840f7ea9-cc0e-56ec-bb00-38543eed3403", "wf_a_1_s"),
    "z" * 64: ("846f78d8-2edd-5f58-a84c-9b6b123efe86", "wf_" + "z" * 64),
}


def test_empty_scope_matches_recorded_worker_identifiers():
    assert ws.CHILD_RUN_NAMESPACE == uuid.UUID("6f2b8e0a-5f37-4c1e-9d0b-8f8e2c7a1d42")
    for step, (child, tool_call) in _GOLDEN_EMPTY_SCOPE.items():
        assert scoped_child_run_id(_GOLDEN_RUN, EMPTY_SCOPE, step) == child
        assert scoped_guard_key(_GOLDEN_RUN, EMPTY_SCOPE, step) == f"{_GOLDEN_RUN}:{step}"
        assert scoped_tool_call_id(EMPTY_SCOPE, step) == tool_call
    rng = random.Random(7)
    for _ in range(200):
        run = str(uuid.UUID(int=rng.getrandbits(128)))
        step = rng.choice(NAMES)
        assert scoped_child_run_id(run, EMPTY_SCOPE, step) == str(uuid.uuid5(ws.CHILD_RUN_NAMESPACE, f"{run}:{step}"))
        assert scoped_guard_key(run, EMPTY_SCOPE, step) == f"{run}:{step}"
        assert scoped_tool_call_id(EMPTY_SCOPE, step) == f"wf_{step}"
        assert scoped_event_key(EMPTY_SCOPE, step, "started") == f"step:{step}:started"
        assert scoped_event_key(EMPTY_SCOPE, step, "decision") == f"step:{step}:decision"
        assert scoped_parent_step_id(EMPTY_SCOPE, step) == step
        assert instance_token(EMPTY_SCOPE, step) == step
        assert scoped_judge_key(EMPTY_SCOPE, step, 3) == f"judge:{step}:3"


def test_nonempty_scope_formats():
    s = parse_scope("outer.2/inner.0")
    assert scoped_event_key(s, "t", "completed") == "loop:outer.2/inner.0:step:t:completed"
    assert scoped_parent_step_id(s, "t") == "outer.2/inner.0/t"
    assert scoped_guard_key(RUN, s, "t") == f"{RUN}:outer.2/inner.0:t"
    assert scoped_child_run_id(RUN, s, "t") == str(uuid.uuid5(ws.CHILD_RUN_NAMESPACE, f"{RUN}:outer.2/inner.0:t"))
    assert scoped_tool_call_id(s, "t") == "wf_outer.2~inner.0~t"
    assert scoped_judge_key(s, "t", 1) == "judge:outer.2/inner.0:t:1"


# --------------------------------------------------------------------------- tool call ids


def test_tool_call_id_length_deterministic_and_decide_route_safe():
    rule = re.compile(r"^[\x21-\x7e]+$")  # AC.8 TOOL_CALL_ID_PATTERN, max 256
    rng = random.Random(5)
    for _ in range(300):
        scope = make_scope(
            ("z" * 64 if rng.random() < 0.5 else rng.choice(NAMES), rng.choice([0, 7, 10**9, 10**30]))
            for _ in range(rng.randint(0, 8))
        )
        step = rng.choice(NAMES)
        tid = scoped_tool_call_id(scope, step)
        assert 1 <= len(tid) <= ws.MAX_TOOL_CALL_ID_LENGTH == 128
        assert len(tid) <= 256 and rule.fullmatch(tid)
        assert tid == scoped_tool_call_id(scope, step)


def test_tool_call_id_max_depth_long_ids_distinct_and_hashed():
    deep = make_scope(("l" * 64, 5) for _ in range(10))
    other = make_scope([*deep[:-1], ("l" * 64, 6)])
    a, b = scoped_tool_call_id(deep, "s" * 64), scoped_tool_call_id(other, "s" * 64)
    assert len(a) == len(b) == 128 and a != b
    assert re.fullmatch(r"wf_.*_[0-9a-f]{16}", a)
    short = scoped_tool_call_id(parse_scope("a.0"), "s")
    assert short == "wf_a.0~s"
    assert len(scoped_tool_call_id(make_scope([("a" * 55, 1)]), "b")) <= 128


def test_tool_call_id_exactly_at_limit_is_not_hashed():
    # "wf_" + "a" * 60 + ".0~" + step = 128 when step has 128-3-60-3 = 62 chars.
    tid = scoped_tool_call_id(make_scope([("a" * 60, 0)]), "s" * 62)
    assert len(tid) == 128 and tid == "wf_" + "a" * 60 + ".0~" + "s" * 62
    full = "wf_" + "a" * 60 + ".0~" + "s" * 63  # 129 characters: one over, so it is digested
    over = scoped_tool_call_id(make_scope([("a" * 60, 0)]), "s" * 63)
    digest = hashlib.sha256(full.encode()).hexdigest()[:16]
    assert len(full) == 129 and len(over) == 128
    assert over == full[:111] + "_" + digest


# --------------------------------------------------------------------------- status, errors, events


def test_status_for_end():
    assert status_for_end("run_completed") == "completed"
    assert status_for_end("run_completed", {"status": "failed"}) == "completed"
    assert status_for_end("run_failed", {"status": "failed"}) == "failed"
    assert status_for_end("run_failed", {"status": "needs_attention"}) == "needs_attention"
    assert status_for_end("run_failed", {}) == "failed"
    assert status_for_end("run_failed") == "failed"
    assert status_for_end("run_failed", {"status": "completed"}) == "failed"
    assert status_for_end("run_failed", {"status": None}) == "failed"
    for other in ("run_cancelled", "step_completed", "cancel_requested", "", "RUN_COMPLETED"):
        with pytest.raises(ValueError):
            status_for_end(other, {})


def test_loop_failures_match_worker_shape():
    """(default status, message) tuples, as eap_worker.errors.WORKFLOW_FAILURES; the live
    cross-check against the worker table is in apps/worker/worker_tests."""
    assert ws.LOOP_WORKFLOW_FAILURES.keys() == set(ws.LOOP_ERROR_CODES) == ws.LOOP_ERROR_MESSAGES.keys()
    assert len(ws.LOOP_ERROR_CODES) == len(set(ws.LOOP_ERROR_CODES))
    for code, value in ws.LOOP_WORKFLOW_FAILURES.items():
        assert type(value) is tuple and len(value) == 2
        assert value == ("failed", ws.LOOP_ERROR_MESSAGES[code])
        assert all(isinstance(x, str) and x for x in value)
    assert set(get_args(ws.LoopErrorCode)) == set(ws.LOOP_ERROR_CODES)


# The codes eap_worker.errors.WORKFLOW_FAILURES had on 2026-11-15; none may be redefined.
_RECORDED_WORKER_CODES = {
    "approval_rejected", "approval_timeout", "agent_not_found", "input_invalid", "input_ref_missing",
    "step_output_too_large", "step_output_not_json", "workflow_context_too_large", "output_invalid",
    "workflow_spec_invalid", "workflow_version_not_found", "step_limit_exceeded",
    "workflow_deadline_exceeded",
}


def test_loop_error_codes_do_not_clash_with_existing_codes():
    assert not set(ws.LOOP_ERROR_CODES) & _RECORDED_WORKER_CODES
    assert "run_budget_exceeded" not in ws.LOOP_ERROR_CODES  # AC.5's code is reused (D11)


# RunEventType before the loops milestone (recorded from HEAD 7960175), in order.
_RECORDED_EVENT_TYPES = [
    "run_started", "text_delta", "model_message", "tool_call", "tool_result", "run_completed",
    "run_failed", "processor_result", "step_started", "step_waiting", "step_approved",
    "step_rejected", "step_timed_out", "step_completed", "step_failed", "ingest_progress",
    "embedding", "memory_observe_failed", "memory_updated", "output_retry",
    "tool_approval_waiting", "tool_approved", "tool_denied", "tool_approval_timed_out",
    "eval_case_result",
]
_LOOP_EVENT_TYPES = ["loop_iteration_started", "loop_iteration_completed", "loop_iteration_failed", "cancel_requested"]


def test_run_event_types_additive_and_ordered():
    """The old values keep their order; the four loop values follow them as one block. Later
    milestones may append more after it."""
    args = list(get_args(RunEventType))
    assert len(args) == len(set(args))
    old = _RECORDED_EVENT_TYPES
    assert args[: len(old)] == old, "existing RunEventType values changed or were reordered"
    assert args[len(old) : len(old) + len(_LOOP_EVENT_TYPES)] == _LOOP_EVENT_TYPES

def test_errors_pickle_round_trip():
    exc = ScopeInvalid("bad scope: 'x'")
    back = pickle.loads(pickle.dumps(exc))
    assert type(back) is ScopeInvalid and back.args == exc.args and str(back) == str(exc)
    scope = parse_scope("a.1/b.2")
    assert pickle.loads(pickle.dumps(scope)) == scope
    assert type(pickle.loads(pickle.dumps(scope[0]))) is ws.ScopeFrame
    with pytest.raises(ScopeInvalid) as info:
        parse_scope("nope")
    assert pickle.loads(pickle.dumps(info.value)).args == info.value.args
