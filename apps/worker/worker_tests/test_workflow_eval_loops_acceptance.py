"""LP.3 acceptance: loop-free parity, nested frames, MISSING semantics, combinators, foreach_items,
loop state, determinism, pickle safety, purity of the module."""

from __future__ import annotations

import ast
import copy
import pickle
from pathlib import Path

import pytest
from pydantic import ValidationError

import eap_worker.workflow_eval as we
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
    resolve_ref,
    resolve_template,
    while_holds,
    with_loop_frame,
)

BODY = {"start": "w", "steps": [{"id": "w", "type": "tool", "tool_id": "noop"}]}


def ref(path):
    return {"$ref": path}


def cmp(op, left, right=None):
    return {"op": op, "left": left, "right": right}


def foreach(**kw):
    return ForeachStep.model_validate(
        {"id": "each", "type": "foreach", "items": ref("input.list"), "max_items": 3, "body": BODY, **kw}
    )


def loop(**kw):
    data = {"id": "lp", "type": "loop", "max_iterations": 5, "body": BODY, **kw}
    if "while" not in data and "until" not in data:
        data["until"] = cmp("eq", ref("loop.lp.state.n"), 2)
    return LoopStep.model_validate(data)


MISS = cmp("eq", ref("input.nope"), 1)  # MISSING operand -> false
TRUE = cmp("eq", 1, 1)
FALSE = cmp("eq", 1, 2)


# ---------------------------------------------------------------- loop-free parity
LOOP_FREE_CASES = [
    (cmp("eq", ref("input.a"), 1), True),
    (cmp("gt", ref("steps.s.output.n"), 2), True),
    (cmp("contains", ref("steps.s.output.tags"), "x"), True),
    (cmp("exists", ref("input.nope")), False),
    (cmp("eq", ref("input.a"), "1"), False),
    (cmp("lt", ref("input.a"), True), False),
]


@pytest.mark.parametrize(("cond", "expected"), LOOP_FREE_CASES)
def test_loop_free_context_evaluates_as_before(cond, expected):
    ctx = {"input": {"a": 1}, "steps": {"s": {"output": {"n": 3, "tags": ["x"]}}}}
    assert evaluate(cond, ctx) is expected
    assert evaluate(cond, build_context({"a": 1}, {"s": {"n": 3, "tags": ["x"]}})) is expected
    assert "loop" not in build_context({"a": 1}, {})
    assert "loop" not in build_context({"a": 1}, {}, loops={})


def test_loop_free_resolution_and_templates_unchanged():
    ctx = build_context({"a": {"b": [10, 20]}}, {"s": {"k": None}})
    assert resolve_ref(ctx, "input.a.b.1") == 20
    assert resolve_ref(ctx, "steps.s.output.k") is None
    assert resolve_ref(ctx, "steps.s.output.zz") is MISSING
    assert resolve_ref(ctx, "loop.x.index") is MISSING
    assert resolve_template({"x": ref("input.a.b.0"), "y": 5}, ctx) == {"x": 10, "y": 5}
    with pytest.raises(RefMissing):
        resolve_template({"x": ref("loop.x.state.n")}, ctx)


def test_loop_roots_with_bad_shapes_are_missing_not_errors():
    ctx = {"input": {}, "steps": {}, "loop": {"l": {"index": 1, "item": [1, 2]}}}
    for path in ("loop", "loop.l", "loop.l.bogus", "loop.l.index.x", "loop.l.state", "loop.zz.item"):
        assert resolve_ref(ctx, path) is MISSING
    assert resolve_ref(ctx, "loop.l.item.1") == 2
    assert resolve_ref({"input": {}, "steps": {}, "loop": None}, "loop.l.index") is MISSING


# ---------------------------------------------------------------- nested frames
def test_inner_refs_see_outer_index_and_item_and_own_frame():
    base = build_context({"q": 1}, {})
    outer = with_loop_frame(base, "o", 4, item={"id": "A"})
    inner = with_loop_frame(outer, "i", 1, item="b", state={"n": 7})
    assert resolve_ref(inner, "loop.o.index") == 4
    assert resolve_ref(inner, "loop.o.item.id") == "A"
    assert resolve_ref(inner, "loop.i.index") == 1
    assert resolve_ref(inner, "loop.i.item") == "b"
    assert resolve_ref(inner, "loop.i.state.n") == 7
    assert resolve_ref(outer, "loop.i.index") is MISSING  # inner frame not in the outer scope
    assert evaluate({"all": [cmp("eq", ref("loop.o.index"), 4), cmp("eq", ref("loop.i.item"), "b")]}, inner)


def test_with_loop_frame_never_mutates_and_replaces_same_loop():
    base = build_context({"q": [1]}, {"s": 1})
    snap = copy.deepcopy(base)
    a = with_loop_frame(base, "o", 0, item=1)
    assert base == snap and "loop" not in base
    a_snap = copy.deepcopy(a)
    b = with_loop_frame(a, "i", 2, state={"n": 1})
    c = with_loop_frame(a, "o", 9, item=2)
    assert a == a_snap
    assert resolve_ref(c, "loop.o.index") == 9 and resolve_ref(c, "loop.o.item") == 2
    assert resolve_ref(a, "loop.o.index") == 0
    assert set(b["loop"]) == {"o", "i"} and set(a["loop"]) == {"o"}
    assert b["loop"] is not a["loop"]


# ---------------------------------------------------------------- MISSING semantics
@pytest.mark.parametrize("op", ["eq", "ne", "gt", "lt", "contains"])
def test_comparison_with_missing_operand_is_unknown_and_not_stays_unknown(op):
    ctx = build_context({"a": 1}, {})
    for left, right in ((ref("input.nope"), 1), (1, ref("input.nope")), (ref("input.nope"), ref("input.nada"))):
        c = cmp(op, left, right)
        assert evaluate(c, ctx) is False  # loop-free parity: even ne, MISSING never compares
        assert evaluate3(c, ctx) is None  # UNKNOWN
        assert evaluate3({"not": c}, ctx) is None  # Kleene (LP.3 review 2026-11-15)
        assert evaluate({"not": c}, ctx) is False


def test_exists_missing_vs_null_and_none_is_present():
    ctx = build_context({"n": None}, {"s": None})
    assert evaluate(cmp("exists", ref("input.n")), ctx) is False  # plan: exists = value other than null
    assert evaluate(cmp("eq", ref("input.n"), None), ctx) is True  # null is present, not MISSING
    assert evaluate(cmp("eq", ref("steps.s.output"), None), ctx) is True
    assert evaluate({"not": cmp("eq", ref("input.n"), None)}, ctx) is False
    assert evaluate(cmp("eq", ref("input.nope"), None), ctx) is False
    with_none = with_loop_frame(ctx, "l", 0, item=None, state={"k": None})
    assert evaluate(cmp("eq", ref("loop.l.item"), None), with_none) is True
    assert evaluate(cmp("eq", ref("loop.l.state.k"), None), with_none) is True
    assert resolve_ref(with_none, "loop.l.item") is None


def test_module_documents_kleene_logic():
    doc = we.__doc__ or ""
    assert "Kleene" in doc and "UNKNOWN" in doc


# ---------------------------------------------------------------- combinators
def ev(expr, ctx=None):
    return evaluate(expr, ctx or build_context({"a": 1}, {}))


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ({"all": [TRUE, TRUE, TRUE]}, True),
        ({"all": [TRUE, FALSE, TRUE]}, False),
        ({"any": [FALSE, FALSE, TRUE]}, True),
        ({"any": [FALSE, MISS]}, False),
        ({"all": [TRUE, MISS]}, False),
        ({"all": [TRUE, {"not": MISS}]}, False),
        ({"any": [MISS, {"not": MISS}]}, False),
        ({"not": {"not": TRUE}}, True),
        ({"not": {"not": MISS}}, False),
        ({"not": {"all": [TRUE, MISS]}}, False),
        ({"not": {"all": [FALSE, MISS]}}, True),
        ({"not": {"any": [TRUE, MISS]}}, False),
        ({"not": {"any": [FALSE, FALSE]}}, True),
        ({"all": [{"any": [FALSE, TRUE]}, {"not": {"all": [TRUE, FALSE]}}]}, True),
        ({"any": [{"all": [TRUE, FALSE]}, {"all": [{"not": FALSE}, {"any": [FALSE, TRUE]}]}]}, True),
    ],
)
def test_nested_combinator_truth_tables(expr, expected):
    assert ev(expr) is expected


def test_empty_combinators_are_rejected_by_the_schema_not_evaluated():
    for expr in ({"all": []}, {"any": []}):
        with pytest.raises(ValidationError):
            evaluate(expr, build_context({}, {}))


def test_limits_operands_and_depth_enforced_when_evaluating_json():
    ok = {"all": [TRUE] * 8}
    assert ev(ok) is True
    with pytest.raises(ValidationError):
        ev({"all": [TRUE] * 9})
    deep4 = {"not": {"not": {"not": {"not": TRUE}}}}
    assert ev(deep4) is True
    with pytest.raises(ValidationError):
        ev({"not": deep4})  # 5 levels
    with pytest.raises(ValidationError):
        ev({"all": [TRUE], "any": [TRUE]})


def test_evaluate_rejects_non_conditions_and_accepts_models():
    with pytest.raises(TypeError):
        evaluate(object(), build_context({}, {}))
    with pytest.raises(ValidationError):
        evaluate({"op": "bogus", "left": 1}, build_context({}, {}))
    model = loop(until={"all": [TRUE, {"not": FALSE}]}).until
    assert evaluate(model, build_context({}, {})) is True


def test_all_any_short_circuit_never_masks_result_with_missing():
    ctx = build_context({}, {})
    assert evaluate({"any": [TRUE, MISS]}, ctx) is True
    assert evaluate({"all": [FALSE, MISS]}, ctx) is False


# ---------------------------------------------------------------- foreach_items
def test_foreach_items_returns_full_list_never_truncated():
    step = foreach(max_items=3)
    for items in ([], [1], [1, 2, 3]):
        assert foreach_items(step, build_context({"list": items}, {})) == items
    exact = [{"k": i} for i in range(3)]
    assert foreach_items(step, build_context({"list": exact}, {})) == exact


@pytest.mark.parametrize("value", ["abc", {"a": 1}, 5, 1.5, True, None])
def test_foreach_items_non_list_errors(value):
    with pytest.raises(LoopItemsError) as e:
        foreach_items(foreach(), build_context({"list": value}, {}))
    assert (e.value.code, e.value.step_id, e.value.count, e.value.limit) == ("loop_items_not_list", "each", None, None)


def test_foreach_items_over_max_errors_with_count_and_limit():
    with pytest.raises(LoopItemsError) as e:
        foreach_items(foreach(max_items=3), build_context({"list": [0, 1, 2, 3]}, {}))
    assert (e.value.code, e.value.count, e.value.limit) == ("loop_items_exceeded", 4, 3)
    assert "4" in str(e.value) and "3" in str(e.value)


def test_foreach_items_unresolved_ref_raises_ref_missing_with_path():
    for ctx in (build_context({}, {}), build_context({"other": 1}, {})):
        with pytest.raises(RefMissing) as e:
            foreach_items(foreach(), ctx)
        assert "input.list" in str(e.value)
    step = foreach(items=ref("steps.fetch.output.rows"))
    assert foreach_items(step, build_context({}, {"fetch": {"rows": [1]}})) == [1]
    with pytest.raises(RefMissing):
        foreach_items(step, build_context({}, {}))


def test_foreach_items_reads_outer_loop_frame_and_does_not_copy_input():
    step = foreach(items=ref("loop.o.item.rows"))
    ctx = with_loop_frame(build_context({}, {}), "o", 0, item={"rows": [1, 2]})
    assert foreach_items(step, ctx) == [1, 2]
    with pytest.raises(LoopItemsError):
        foreach_items(step, with_loop_frame(ctx, "o", 1, item={"rows": "x"}))


# ---------------------------------------------------------------- loop state
def test_state_without_template_is_empty_dict_and_fresh_each_call():
    step = loop(until=cmp("eq", ref("loop.lp.index"), 1))
    ctx = build_context({}, {})
    a, b = initial_state(step, ctx), initial_state(step, ctx)
    assert a == {} and b == {} and a is not b


def test_initial_state_resolves_over_outer_scope_and_missing_raises():
    step = loop(state={"n": 0, "src": ref("input.seed")})
    assert initial_state(step, build_context({"seed": [1]}, {})) == {"n": 0, "src": [1]}
    with pytest.raises(RefMissing):
        initial_state(step, build_context({}, {}))
    with pytest.raises(RefMissing):  # the loop's own frame is not in scope at loop start
        initial_state(loop(state={"n": ref("loop.lp.index")}), build_context({}, {}))


def test_carry_replaces_state_it_does_not_merge():
    step = loop(carry={"n": ref("steps.w.output.n")})
    ctx = with_loop_frame(build_context({}, {"w": {"n": 9}}), "lp", 0, state={"n": 1, "old": "x"})
    out = iteration_exit(step, ctx)
    assert out == IterationExit(state={"n": 9}, done=False, until="false")
    assert "old" not in out.state


def test_carry_reads_state_before_update_until_reads_after():
    step = loop(
        carry={"n": ref("loop.lp.state.n"), "prev": ref("loop.lp.state.n"), "x": ref("steps.w.output.x")},
        until=cmp("eq", ref("loop.lp.state.x"), 5),
    )
    ctx = with_loop_frame(build_context({}, {"w": {"x": 5}}), "lp", 2, state={"n": 3})
    out = iteration_exit(step, ctx)
    assert out.state == {"n": 3, "prev": 3, "x": 5} and out.done is True
    ctx2 = with_loop_frame(build_context({}, {"w": {"x": 4}}), "lp", 2, state={"n": 3, "x": 5})
    assert iteration_exit(step, ctx2).done is False  # old state x=5 is ignored; updated x=4


def test_no_carry_keeps_state_and_until_sees_it():
    step = loop(until=cmp("eq", ref("loop.lp.state.n"), 2))
    ctx = with_loop_frame(build_context({}, {}), "lp", 0, state={"n": 2})
    assert iteration_exit(step, ctx) == IterationExit(state={"n": 2}, done=True, until="true")
    assert iteration_exit(step, with_loop_frame(ctx, "lp", 0, state={"n": 1})).done is False


def test_until_can_read_index_and_body_outputs_and_missing_is_not_done():
    step = loop(until={"any": [cmp("gt", ref("loop.lp.index"), 1), cmp("eq", ref("steps.w.output.ok"), True)]})
    ctx = with_loop_frame(build_context({}, {"w": {"ok": False}}), "lp", 1, state={})
    assert iteration_exit(step, ctx).done is False
    assert iteration_exit(step, with_loop_frame(ctx, "lp", 2, state={})).done is True
    step2 = loop(until=cmp("eq", ref("steps.w.output.nope"), 1))
    missing = iteration_exit(step2, with_loop_frame(ctx, "lp", 0, state={}))
    assert missing.done is False and missing.until == "unknown"


def test_while_loop_is_never_done_by_until_logic():
    step = loop(**{"while": cmp("lt", ref("loop.lp.index"), 3)}, carry={"n": 1})
    ctx = with_loop_frame(build_context({}, {}), "lp", 9, state={"n": 0})
    assert iteration_exit(step, ctx) == IterationExit(state={"n": 1}, done=False)


def test_while_holds_uses_index_and_carried_state_and_ignores_body_steps():
    step = loop(**{"while": {"all": [cmp("lt", ref("loop.lp.index"), 2), cmp("lt", ref("loop.lp.state.n"), 5)]}})
    ctx = build_context({}, {"w": {"n": 99}})
    assert while_holds(step, ctx, 0, {"n": 0}) is True
    assert while_holds(step, ctx, 2, {"n": 0}) is False
    assert while_holds(step, ctx, 1, {"n": 5}) is False
    stepb = loop(**{"while": cmp("eq", ref("steps.w.output.n"), 99)})
    assert while_holds(stepb, build_context({}, {}), 0, {}) is False  # body step not readable -> MISSING
    assert while_holds(loop(), ctx, 7, {"n": 1}) is True  # until loop: always holds


def test_iteration_exit_requires_own_frame_and_carry_missing_raises():
    step = loop(carry={"n": ref("steps.w.output.n")})
    for ctx in (
        build_context({}, {}),
        with_loop_frame(build_context({}, {}), "other", 0, state={}),
        with_loop_frame(build_context({}, {}), "lp", 0),  # own frame without a state
    ):
        with pytest.raises(ValueError):
            iteration_exit(step, ctx)
    with pytest.raises(RefMissing):
        iteration_exit(step, with_loop_frame(build_context({}, {}), "lp", 0, state={}))
    assert IterationExit(state={}, done=False).__dataclass_params__.frozen is True


# ---------------------------------------------------------------- determinism, pickle, purity
def test_same_inputs_same_results_no_shared_state_and_no_mutation():
    step = loop(carry={"n": ref("steps.w.output.n"), "l": ref("steps.w.output.l")})
    base = build_context({"i": 1}, {"w": {"n": 1, "l": [1, 2]}})
    ctx = with_loop_frame(base, "lp", 3, state={"n": 0})
    snap = copy.deepcopy(ctx)
    results = [iteration_exit(step, ctx) for _ in range(5)]
    assert all(r == results[0] for r in results) and ctx == snap
    # the result is a copy: mutating it corrupts neither the context nor a later call's result,
    # and the evaluator itself holds no cache.
    results[0].state["l"].append(3)
    assert ctx == snap and ctx["steps"]["w"]["output"]["l"] == [1, 2]
    assert results[1].state["l"] == [1, 2]
    assert iteration_exit(step, ctx).state["l"] == [1, 2]
    expr = {"all": [cmp("gt", ref("steps.w.output.n"), 0), {"not": FALSE}]}
    assert [evaluate(expr, snap) for _ in range(3)] == [True] * 3 and expr["all"][1] == {"not": FALSE}


def test_loop_items_error_pickles_all_codes():
    for err in (
        LoopItemsError("loop_items_not_list", "s1"),
        LoopItemsError("loop_items_exceeded", "s2", 10, 3),
    ):
        back = pickle.loads(pickle.dumps(err))
        assert type(back) is LoopItemsError
        assert (back.code, back.step_id, back.count, back.limit) == (err.code, err.step_id, err.count, err.limit)
        assert str(back) == str(err) and back.args == err.args


def test_module_has_no_io_clock_or_random_imports():
    tree = ast.parse(Path(we.__file__).read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    banned = {"os", "sys", "time", "datetime", "random", "uuid", "socket", "subprocess", "pathlib", "sqlalchemy",
              "asyncio", "httpx", "requests", "logging", "secrets", "threading"}
    assert not (mods & banned), mods & banned
