"""LP.3: pure loop evaluation: loop.<L>.* roots, combinators, foreach_items, state/carry/until."""

from __future__ import annotations

import copy
import pickle
import random

import pytest

from eap_schemas.workflows import ForeachStep, LoopStep
from eap_worker.workflow_eval import (
    MISSING,
    IterationExit,
    LoopItemsError,
    RefMissing,
    build_context,
    evaluate,
    evaluate3,
    foreach_items,
    initial_state,
    iteration_exit,
    json_copy,
    outcome,
    resolve_ref,
    while_holds,
    while_outcome,
    with_loop_frame,
)


def ref(path):
    return {"$ref": path}


def cond(op, left, right=None):
    return {"op": op, "left": left, "right": right}


BODY = {"start": "work", "steps": [{"id": "work", "type": "tool", "tool_id": "noop"}]}


def foreach(**kw):
    return ForeachStep.model_validate(
        {"id": "each", "type": "foreach", "items": ref("input.list"), "max_items": 3, "body": BODY, **kw}
    )


def loop(**kw):
    data = {"id": "lp", "type": "loop", "max_iterations": 5, "body": BODY, **kw}
    if "while" not in data and "until" not in data:
        data["until"] = cond("eq", ref("loop.lp.state.n"), 2)
    return LoopStep.model_validate(data)


def test_loop_free_context_keeps_todays_shape():
    assert build_context({"a": 1}, {"s": 2}) == {"input": {"a": 1}, "steps": {"s": {"output": 2}}}


def test_loop_roots_resolve_in_scope_and_are_missing_outside():
    base = build_context({"q": 1}, {"fetch": {"x": [1]}})
    assert resolve_ref(base, "loop.each.index") is MISSING
    ctx = with_loop_frame(base, "each", 2, item={"id": "t2"})
    assert resolve_ref(ctx, "loop.each.index") == 2
    assert resolve_ref(ctx, "loop.each.item.id") == "t2"
    assert resolve_ref(ctx, "loop.each.item") == {"id": "t2"}
    assert resolve_ref(ctx, "loop.each.state") is MISSING  # foreach has no state
    assert resolve_ref(ctx, "loop.each.index.x") is MISSING
    assert resolve_ref(ctx, "loop.other.index") is MISSING
    assert resolve_ref(ctx, "steps.fetch.output.x.0") == 1
    assert "loop" not in base  # not modified


def test_nested_scopes_read_both_loops():
    ctx = with_loop_frame(build_context({}, {}), "outer", 1, item="a")
    ctx = with_loop_frame(ctx, "inner", 0, state={"n": 4})
    assert resolve_ref(ctx, "loop.outer.item") == "a"
    assert resolve_ref(ctx, "loop.inner.state.n") == 4
    assert resolve_ref(ctx, "loop.inner.item") is MISSING
    assert evaluate({"all": [cond("eq", ref("loop.outer.index"), 1), cond("eq", ref("loop.inner.index"), 0)]}, ctx)


def test_item_null_is_present_not_missing():
    ctx = with_loop_frame(build_context({}, {}), "each", 0, item=None)
    assert resolve_ref(ctx, "loop.each.item") is None
    assert not evaluate(cond("exists", ref("loop.each.item")), ctx)


@pytest.mark.parametrize("bad", [-1, True, 1.0, "0"])
def test_loop_index_must_be_a_non_negative_int(bad):
    with pytest.raises(ValueError):
        with_loop_frame({}, "each", bad)


T = cond("eq", 1, 1)
F = cond("eq", 1, 2)
M = cond("eq", ref("input.nope"), 1)  # MISSING operand: UNKNOWN


@pytest.mark.parametrize(
    ("expr", "three", "expected"),
    [
        (M, None, False),  # loop-free parity: a bare MISSING comparison is false, as before
        ({"all": [T, T]}, True, True),
        ({"all": [T, F]}, False, False),
        ({"all": [T, M]}, None, False),
        ({"all": [F, M]}, False, False),
        ({"any": [F, T]}, True, True),
        ({"any": [M, T]}, True, True),
        ({"any": [F, M]}, None, False),
        ({"not": T}, False, False),
        ({"not": F}, True, True),
        ({"not": M}, None, False),  # Kleene: not UNKNOWN is UNKNOWN, never satisfied
        ({"not": {"not": M}}, None, False),
        ({"any": [M, {"not": M}]}, None, False),
        ({"not": {"all": [cond("exists", ref("input.nope")), M]}}, True, True),  # exists is two-valued
        ({"any": [{"not": {"any": [F, M]}}, F]}, None, False),
        (T, True, True),
    ],
)
def test_kleene_truth_tables(expr, three, expected):
    ctx = build_context({}, {})
    assert evaluate3(expr, ctx) is three
    assert evaluate(expr, ctx) is expected
    assert outcome(expr, ctx) == {True: "true", False: "false", None: "unknown"}[three]


def test_type_mismatch_is_false_not_unknown():
    ctx = build_context({"s": "a"}, {})
    assert evaluate3(cond("gt", ref("input.s"), 1), ctx) is False
    assert evaluate3({"not": cond("gt", ref("input.s"), 1)}, ctx) is True


def test_combinators_accept_validated_models():
    step = loop(until={"any": [F, cond("eq", ref("loop.lp.index"), 0)]})
    assert evaluate(step.until, with_loop_frame(build_context({}, {}), "lp", 0, state={})) is True


def test_foreach_items_ok_and_errors():
    assert foreach_items(foreach(), build_context({"list": [1, 2, 3]}, {})) == [1, 2, 3]
    assert foreach_items(foreach(), build_context({"list": []}, {})) == []
    with pytest.raises(LoopItemsError) as exc:
        foreach_items(foreach(), build_context({"list": [1, 2, 3, 4]}, {}))
    assert (exc.value.code, exc.value.count, exc.value.limit) == ("loop_items_exceeded", 4, 3)
    with pytest.raises(LoopItemsError) as exc:
        foreach_items(foreach(), build_context({"list": {"a": 1}}, {}))
    assert exc.value.code == "loop_items_not_list"
    with pytest.raises(RefMissing):
        foreach_items(foreach(), build_context({}, {}))


def test_loop_items_error_is_pickle_safe():
    err = LoopItemsError("loop_items_exceeded", "each", 4, 3)
    back = pickle.loads(pickle.dumps(err))
    assert (back.code, back.step_id, back.count, back.limit, str(back)) == (
        "loop_items_exceeded",
        "each",
        4,
        3,
        str(err),
    )


def test_initial_state_and_while():
    step = loop(**{"while": cond("lt", ref("loop.lp.state.n"), 2), "state": {"n": ref("input.start"), "k": "x"}})
    ctx = build_context({"start": 0}, {})
    state = initial_state(step, ctx)
    assert state == {"n": 0, "k": "x"}
    assert while_holds(step, ctx, 0, state) is True
    assert while_holds(step, ctx, 1, {"n": 2}) is False
    assert while_outcome(step, ctx, 1, {}) == "unknown"  # MISSING state field: the while stops
    assert while_holds(step, ctx, 1, {}) is False
    assert initial_state(loop(), ctx) == {}
    assert while_holds(loop(), ctx, 0, {}) is True  # until loop
    with pytest.raises(RefMissing):
        initial_state(loop(state={"n": ref("input.nope")}), ctx)


def test_carry_before_until_until_sees_updated_state():
    step = loop(
        state={"n": 0},
        carry={"n": ref("steps.work.output.next"), "prev": ref("loop.lp.state.n")},
        until=cond("eq", ref("loop.lp.state.n"), 2),
    )
    ctx = with_loop_frame(build_context({}, {"work": {"next": 2}}), "lp", 1, state={"n": 1})
    out = iteration_exit(step, ctx)
    assert out == IterationExit(state={"n": 2, "prev": 1}, done=True, until="true")  # carry read the old state
    # without carry the state is kept and until reads it
    assert iteration_exit(loop(state={"n": 2}), with_loop_frame(ctx, "lp", 1, state={"n": 2})).done is True
    # while loops never report done and have no until outcome
    w = loop(**{"while": T})
    assert iteration_exit(w, with_loop_frame(ctx, "lp", 0, state={})) == IterationExit({}, False, None)
    with pytest.raises(ValueError):
        iteration_exit(step, build_context({}, {}))
    with pytest.raises(ValueError):  # a loop frame without a state is a wiring error, not {}
        iteration_exit(step, with_loop_frame(ctx, "lp", 1))
    with pytest.raises(RefMissing):
        iteration_exit(step, with_loop_frame(build_context({}, {}), "lp", 0, state={"n": 0}))


def test_until_over_a_missing_field_is_unknown_and_not_done():
    # the refine hazard: "stop unless the critic says revise" with a drifted output field
    step = loop(state={}, until={"not": cond("eq", ref("steps.work.output.verdict"), "revise")})
    ctx = with_loop_frame(build_context({}, {"work": {"Verdict": "revise"}}), "lp", 0, state={})
    assert iteration_exit(step, ctx) == IterationExit(state={}, done=False, until="unknown")


def test_results_share_nothing_with_spec_or_context():
    step = loop(state={"hist": [], "n": ref("input.start")}, until=F)
    ctx = build_context({"start": {"deep": [1]}, "list": [{"a": 1}]}, {"work": {"next": [1]}})
    s0 = initial_state(step, ctx)
    s0["hist"].append(1)
    s0["n"]["deep"].append(2)
    assert step.state["hist"] == [] and ctx["input"]["start"] == {"deep": [1]}
    items = foreach_items(foreach(), ctx)
    items[0]["a"] = 2
    assert ctx["input"]["list"] == [{"a": 1}]
    frame_state = {"keep": [1]}
    kept = iteration_exit(step, with_loop_frame(ctx, "lp", 0, state=frame_state))
    kept.state["keep"].append(2)
    assert frame_state == {"keep": [1]}
    carried = iteration_exit(
        loop(state={}, carry={"x": ref("steps.work.output.next")}, until=F),
        with_loop_frame(ctx, "lp", 0, state={}),
    )
    carried.state["x"].append(2)
    assert ctx["steps"]["work"]["output"] == {"next": [1]}


def test_json_copy_is_iterative_and_deep():
    deep: list = []
    node = deep
    for _ in range(5000):
        node.append([])
        node = node[0]
    out = json_copy(deep)
    assert out == deep and out is not deep
    assert json_copy({"a": [1, {"b": None}]}) == {"a": [1, {"b": None}]}


def _random_value(rng: random.Random, depth: int = 0):
    kind = rng.randrange(7 if depth < 3 else 5)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.randint(-3, 3)
    if kind == 3:
        return rng.choice(["", "a", "ab", "b"])
    if kind == 4:
        return rng.choice([0.5, 1.0, -2.0])
    if kind == 5:
        return [_random_value(rng, depth + 1) for _ in range(rng.randrange(3))]
    return {rng.choice("abc"): _random_value(rng, depth + 1) for _ in range(rng.randrange(3))}


_PATHS = [
    "input.a",
    "input.b",
    "input.nope",
    "steps.work.output",
    "steps.work.output.a",
    "loop.lp.index",
    "loop.lp.state.a",
    "loop.lp.state.zz",
]


def _random_operand(rng: random.Random):
    return ref(rng.choice(_PATHS)) if rng.random() < 0.6 else _random_value(rng, 2)


def _random_expr(rng: random.Random, depth: int = 0):
    pick = rng.randrange(4 if depth < 3 else 1)
    if pick == 0:
        op = rng.choice(["eq", "ne", "gt", "lt", "contains", "exists"])
        if op == "exists":
            return {"op": op, "left": ref(rng.choice(_PATHS)), "right": None}
        return cond(op, _random_operand(rng), _random_operand(rng))
    if pick == 3:
        return {"not": _random_expr(rng, depth + 1)}
    return {["all", "any"][pick - 1]: [_random_expr(rng, depth + 1) for _ in range(rng.randint(1, 3))]}


def _kleene_oracle(expr, ctx):
    """An independent model: evaluate leaves two ways and check the Kleene laws structurally."""
    if "all" in expr:
        rs = [_kleene_oracle(e, ctx) for e in expr["all"]]
        return False if False in rs else (None if None in rs else True)
    if "any" in expr:
        rs = [_kleene_oracle(e, ctx) for e in expr["any"]]
        return True if True in rs else (None if None in rs else False)
    if "not" in expr:
        r = _kleene_oracle(expr["not"], ctx)
        return None if r is None else not r
    return evaluate3(expr, ctx)


def test_property_seeded_random_contexts_and_expressions():
    """Property-style (seeded; hypothesis is not installed): for random JSON contexts and random
    expressions, evaluation and iteration_exit are deterministic, follow the Kleene laws, map
    UNKNOWN to False at the top level, agree with De Morgan, and never mutate their inputs."""
    rng = random.Random(20261115)
    for _ in range(400):
        ctx = with_loop_frame(
            build_context({"a": _random_value(rng), "b": _random_value(rng)}, {"work": _random_value(rng)}),
            "lp",
            rng.randrange(3),
            state={"a": _random_value(rng)},
        )
        expr = _random_expr(rng)
        snap_ctx, snap_expr = copy.deepcopy(ctx), copy.deepcopy(expr)
        three = evaluate3(expr, ctx)
        assert three is _kleene_oracle(expr, ctx)
        assert evaluate(expr, ctx) is (three is True)
        assert evaluate3(expr, ctx) is three  # deterministic
        negated = evaluate3({"not": expr}, ctx)
        assert negated is (None if three is None else not three)
        if "all" in expr:  # De Morgan: not all(xs) == any(not x)
            dual = evaluate3({"any": [{"not": e} for e in expr["all"]]}, ctx)
            assert dual is negated
        step = loop(carry={"a": ref("loop.lp.state.a")}, until=expr)
        exits = [iteration_exit(step, ctx) for _ in range(2)]
        assert exits[0] == exits[1] and exits[0].state is not ctx["loop"]["lp"]["state"]
        assert exits[0].until == outcome(expr, ctx)  # carry keeps the same state here
        assert exits[0].done is (three is True)
        assert ctx == snap_ctx and expr == snap_expr
