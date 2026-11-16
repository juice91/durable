"""LP.1 acceptance: hash stability against pre-LP.1 goldens, validation messages, scoping, bounds, combinators."""

from __future__ import annotations

import copy
import json

import pytest
from eap_schemas.workflows import (
    MAX_LOOP_ITERATIONS,
    WorkflowSpec,
    canonical_workflow_hash,
    run_concurrency,
    static_execution_bound,
)
from pydantic import ValidationError

A = "00000000-0000-0000-0000-0000000000a1"
DONE = {"op": "eq", "left": {"$ref": "input.a"}, "right": 1}


def agent(sid, nxt=None, **inp):
    return {"id": sid, "type": "agent", "agent_id": A, "input": inp, "next": nxt}


def tool(sid, nxt=None, **inp):
    return {"id": sid, "type": "tool", "tool_id": "clock.now", "input": inp, "next": nxt}


def ref(path):
    return {"$ref": path}


def wf(*steps, start=None, **extra):
    return {"name": "W", "input_schema": {"type": "object"}, "steps": list(steps),
            "start": start or steps[0]["id"], **extra}


def body(steps):
    return {"start": steps[0]["id"], "steps": steps}


def foreach(sid, steps, nxt=None, items="input.xs", max_items=5, **kw):
    return {"id": sid, "type": "foreach", "items": ref(items), "max_items": max_items,
            "body": body(steps), "next": nxt, **kw}


def loop(sid, steps, nxt=None, max_iterations=3, **kw):
    if "while" not in kw and "until" not in kw:
        kw["until"] = DONE
    return {"id": sid, "type": "loop", "max_iterations": max_iterations, "body": body(steps),
            "next": nxt, **kw}


def ok(spec):
    return WorkflowSpec.model_validate(spec)


def bad(spec, pattern):
    with pytest.raises(ValidationError, match=pattern):
        WorkflowSpec.model_validate(spec)


# --- hash stability: inline goldens from the pre-LP.1 code ------------------------------------
# Hash and exact model_dump_json of each shape, computed with workflows.py at commit 7960175 (before
# LP.1). Inline, so the test keeps guarding after LP.1 is committed and needs no git at test time.

OLD_SHAPES = {
    "minimal": wf(tool("t")),
    "agent_chain": wf(agent("a", "t", q=ref("input.q")), tool("t", None, r=ref("steps.a.output")),
                      description="d", output_schema={"type": "object"}),
    "agent_version": wf({"id": "a", "type": "agent", "agent_id": A, "version": 3}),
    "branch": wf(
        {"id": "r", "type": "branch", "cases": [
            {"when": {"op": "eq", "left": ref("input.x"), "right": 1}, "next": "a"},
            {"when": {"op": "exists", "left": ref("input.y")}, "next": "b"}], "default": "a"},
        tool("a"), tool("b")),
    "approval": wf(tool("t", "g"), {"id": "g", "type": "approval", "prompt": "Go?", "timeout_hours": 2}),
    "parallel": wf({"id": "f", "type": "parallel", "branches": ["a", "b"], "join": "j"},
                   tool("a", "j"), agent("b"),
                   tool("j", None, a=ref("steps.a.output"), b=ref("steps.b.output"))),
    "unicode": wf(tool("t", None, s="é中 {0} %s", n=None, f=1.5)),
}


PRE_LP1_GOLDENS = {
    "agent_chain": (
        "c8db396074c1e803eed964aa63a2baef7164540a1ac43178095debfb829cd1b6",
        '{"spec_version":1,"name":"W","description":"d","input_schema":{"type":"object"},"output_schema":{"type":"object"},"steps":[{"id":"a","next":"t","input":{"q":{"$ref":"input.q"}},"type":"agent","agent_id":"00000000-0000-0000-0000-0000000000a1","version":null},{"id":"t","next":null,"input":{"r":{"$ref":"steps.a.output"}},"type":"tool","tool_id":"clock.now"}],"start":"a"}',
    ),
    "agent_version": (
        "b721831f1f8948d10116ac753cecfdf83fa2b07d3ad515bd17fa91177a553e33",
        '{"spec_version":1,"name":"W","description":"","input_schema":{"type":"object"},"output_schema":null,"steps":[{"id":"a","next":null,"input":{},"type":"agent","agent_id":"00000000-0000-0000-0000-0000000000a1","version":3}],"start":"a"}',
    ),
    "approval": (
        "070ec00b61e75130432256fafc856a07c3ecb06733eddb7cdb1e1f3e53bf98d3",
        '{"spec_version":1,"name":"W","description":"","input_schema":{"type":"object"},"output_schema":null,"steps":[{"id":"t","next":"g","input":{},"type":"tool","tool_id":"clock.now"},{"id":"g","next":null,"type":"approval","prompt":"Go?","timeout_hours":2}],"start":"t"}',
    ),
    "branch": (
        "4d294d9ce5b3358409aee04e06acc6edc7ce1a4fb9639ba2162db8aedaa14214",
        '{"spec_version":1,"name":"W","description":"","input_schema":{"type":"object"},"output_schema":null,"steps":[{"id":"r","type":"branch","cases":[{"when":{"op":"eq","left":{"$ref":"input.x"},"right":1},"next":"a"},{"when":{"op":"exists","left":{"$ref":"input.y"},"right":null},"next":"b"}],"default":"a"},{"id":"a","next":null,"input":{},"type":"tool","tool_id":"clock.now"},{"id":"b","next":null,"input":{},"type":"tool","tool_id":"clock.now"}],"start":"r"}',
    ),
    "minimal": (
        "2609a8534d48c27cfe3ce527adbb5ec1c41d2bcb70d7d7498e7ef93e1e2fc82f",
        '{"spec_version":1,"name":"W","description":"","input_schema":{"type":"object"},"output_schema":null,"steps":[{"id":"t","next":null,"input":{},"type":"tool","tool_id":"clock.now"}],"start":"t"}',
    ),
    "parallel": (
        "4757bf32783d0ffced4376fa4e168a1c468517df71426357c9ce12ca72ac751a",
        '{"spec_version":1,"name":"W","description":"","input_schema":{"type":"object"},"output_schema":null,"steps":[{"id":"f","type":"parallel","branches":["a","b"],"join":"j"},{"id":"a","next":"j","input":{},"type":"tool","tool_id":"clock.now"},{"id":"b","next":null,"input":{},"type":"agent","agent_id":"00000000-0000-0000-0000-0000000000a1","version":null},{"id":"j","next":null,"input":{"a":{"$ref":"steps.a.output"},"b":{"$ref":"steps.b.output"}},"type":"tool","tool_id":"clock.now"}],"start":"f"}',
    ),
    "unicode": (
        "a0b571682dbbeb6cd9eae956f00a5747f2d8f939bc8c1150de643c3b63c43061",
        '{"spec_version":1,"name":"W","description":"","input_schema":{"type":"object"},"output_schema":null,"steps":[{"id":"t","next":null,"input":{"s":"é中 {0} %s","n":null,"f":1.5},"type":"tool","tool_id":"clock.now"}],"start":"t"}',
    ),
}


def test_goldens_cover_every_old_shape():
    assert sorted(PRE_LP1_GOLDENS) == sorted(OLD_SHAPES)


@pytest.mark.parametrize("name", sorted(OLD_SHAPES))
def test_old_specs_keep_pre_lp1_hash_and_exact_json(name):
    golden_hash, golden_json = PRE_LP1_GOLDENS[name]
    new = ok(OLD_SHAPES[name])
    assert canonical_workflow_hash(new) == golden_hash
    assert new.model_dump_json() == golden_json
    assert new.model_dump(mode="json") == json.loads(golden_json)
    assert "limits" not in new.model_dump(mode="json")


@pytest.mark.parametrize("name", sorted(OLD_SHAPES))
def test_old_specs_round_trip_with_explicit_empty_limits(name):
    expected = PRE_LP1_GOLDENS[name][0]
    assert canonical_workflow_hash(ok({**OLD_SHAPES[name], "limits": {}})) == expected
    assert canonical_workflow_hash(ok({**OLD_SHAPES[name], "limits": None})) == expected
    assert canonical_workflow_hash(ok({**OLD_SHAPES[name], "limits": {"max_cost_usd": None}})) == expected


def test_null_fields_still_dump_as_null():
    d = ok(OLD_SHAPES["agent_chain"]).model_dump(mode="json")
    assert d["steps"][0]["version"] is None and d["steps"][1]["next"] is None
    assert d["output_schema"] == {"type": "object"} and "limits" not in d
    cond = ok(OLD_SHAPES["branch"]).model_dump(mode="json")["steps"][0]["cases"][1]["when"]
    assert cond == {"op": "exists", "left": {"$ref": "input.y"}, "right": None}


# --- validation: every error names the step -----------------------------------------------------

def test_valid_shapes_in_one_spec():
    spec = ok(wf(
        tool("seed", "each"),
        foreach("each", [
            loop("inner", [tool("w", None, x=ref("loop.each.item"), n=ref("loop.inner.index"))],
                 state={"n": 0}, carry={"n": ref("steps.w.output.n")},
                 until={"all": [DONE, {"not": {"op": "exists", "left": ref("loop.inner.state.n")}}]}),
        ], nxt="chk", items="steps.seed.output"),
        {"id": "chk", "type": "branch", "cases": [{"when": {"any": [DONE, DONE]}, "next": "end"}],
         "default": "end"},
        tool("end"),
    ))
    assert ok(spec.model_dump(mode="json")).model_dump(mode="json") == spec.model_dump(mode="json")


def test_missing_and_oversized_bounds_name_the_step():
    f = foreach("each", [tool("x")])
    del f["max_items"]
    bad(wf(f), r"step 'each': max_items")
    bad(wf(foreach("each", [tool("x")], max_items=0)), r"step 'each': max_items")
    bad(wf(foreach("each", [tool("x")], max_items=1001)), r"step 'each': max_items")
    lp = loop("lp", [tool("x")])
    del lp["max_iterations"]
    bad(wf(lp), r"step 'lp': max_iterations")
    bad(wf(loop("lp", [tool("x")], max_iterations=0)), r"step 'lp': max_iterations")
    bad(wf(loop("lp", [tool("x")], max_iterations=1001)), r"step 'lp': max_iterations")


def test_while_xor_until_and_concurrency():
    both = loop("lp", [tool("x")], **{"while": DONE})
    both["until"] = DONE
    bad(wf(both), "step 'lp': a loop needs exactly one")
    bad(wf({**loop("lp", [tool("x")]), "until": None}), "step 'lp': a loop needs exactly one")
    bad(wf(foreach("each", [tool("x")], concurrency=9)), "step 'each': concurrency")
    bad(wf(foreach("each", [tool("x")], concurrency=0)), "step 'each': concurrency")
    ok(wf(foreach("each", [tool("x")], concurrency=8)))


def test_nesting_caps():
    l3 = loop("l3", [tool("x")])
    l2 = loop("l2", [l3])
    l1 = loop("l1", [l2])
    bad(wf(l1), "step 'l3'.*2 deep")
    f3 = foreach("f3", [tool("x")])
    bad(wf(foreach("f1", [foreach("f2", [f3])])), "step 'f3'.*2 deep")
    bad(wf(loop("l1", [foreach("f2", [loop("l3", [tool("x")])])])), "step 'l3'.*2 deep")
    ok(wf(foreach("f1", [loop("l2", [tool("x")])])))


@pytest.mark.parametrize("step", [
    {"id": "g", "type": "approval", "prompt": "Go?", "timeout_hours": 1},
    {"id": "g", "type": "parallel", "branches": ["a", "b"], "join": "j"},
])
def test_approval_or_parallel_in_a_body_names_loop_and_step(step):
    bad(wf(loop("lp", [step, tool("a")])), r"step 'lp': body step 'g'.*loop_body_step_not_allowed")
    bad(wf(foreach("fe", [step, tool("a")])), r"step 'fe': body step 'g'.*loop_body_step_not_allowed")


@pytest.mark.parametrize("kind", ["loop", "foreach"])
def test_loop_in_a_parallel_branch_is_rejected(kind):
    inner = loop("blk", [tool("x")], "j") if kind == "loop" else foreach("blk", [tool("x")], "j")
    spec = wf({"id": "p", "type": "parallel", "branches": ["blk", "b"], "join": "j"},
              inner, tool("b", "j"), tool("j"))
    bad(spec, r"parallel step 'p': step 'blk' in branch 'blk'.*loop_in_parallel_not_allowed")


def test_duplicate_ids_across_nested_bodies():
    bad(wf(tool("x", "lp"), loop("lp", [tool("x")])), r"duplicate step id 'x'")
    bad(wf(loop("a", [tool("x")], "b"), loop("b", [tool("x")])), r"duplicate step id 'x'")
    bad(wf(foreach("f", [loop("f", [tool("x")])])), r"duplicate step id 'f'")
    bad(wf(foreach("f", [loop("g", [tool("x"), ])]), tool("x")), r"duplicate step id 'x'")


def test_body_graph_errors_name_the_loop_and_step():
    cyc = [tool("a", "b"), tool("b", "a")]
    bad(wf(loop("lp", cyc)), r"step 'lp': body: .*cycle: a -> b -> a")
    bad(wf(loop("lp", [tool("a"), tool("orphan")])), r"step 'lp': body: step 'orphan' is not reachable")
    bad(wf(loop("lp", [tool("a", "outside")], "outside"), tool("outside")),
        r"step 'lp': body: step 'a': next target")
    bad(wf(loop("lp", [tool("a")]) | {"body": {"start": "nope", "steps": [tool("a")]}}),
        r"step 'lp': body: start step 'nope' does not exist in the body")


def test_more_than_fifty_steps_counts_bodies():
    inner = [tool(f"b{i}", f"b{i + 1}" if i < 44 else None) for i in range(45)]
    bad(wf(loop("lp", inner), *[tool(f"t{i}") for i in range(6)], start="lp"),
        r"at most 50 steps in all.*got 52")
    ok(wf(loop("lp", [*inner[:39], tool("b39")])))


# --- scoping (D3) -------------------------------------------------------------------------------

def test_loop_refs_outside_their_loop_are_rejected():
    spec = wf(loop("lp", [tool("x")], "after"), tool("after", None, i=ref("loop.lp.index")))
    bad(spec, r"step 'after'.*loop 'lp'.*does not enclose")
    bad(wf(tool("t", None, i=ref("loop.ghost.index"))), r"step 't'.*unknown loop 'ghost'")
    bad(wf(foreach("a", [tool("x")], "b"), foreach("b", [tool("y", None, i=ref("loop.a.item"))])),
        r"step 'y'.*loop 'a'.*does not enclose")
    bad(wf(loop("lp", [tool("x", None, i=ref("loop.lp.index.deeper"))])), r"step 'x'")


def test_item_and_state_only_on_the_right_kind():
    bad(wf(loop("lp", [tool("x", None, i=ref("loop.lp.item"))])), r"step 'x'.*\.item.*foreach")
    bad(wf(foreach("fe", [tool("x", None, i=ref("loop.fe.state"))])), r"step 'x'.*\.state.*loop")
    ok(wf(foreach("fe", [tool("x", None, i=ref("loop.fe.item.name"), j=ref("loop.fe.index"))])))


def test_body_steps_are_not_readable_outside_the_loop():
    spec = wf(loop("lp", [tool("x")], "after"), tool("after", None, r=ref("steps.x.output")))
    bad(spec, r"step 'after'.*'steps.x.output'.*inside the body of loop 'lp'")
    spec = wf(foreach("fe", [tool("x")], "after"), tool("after", None, r=ref("steps.x.output")))
    bad(spec, r"step 'after'.*inside the body of loop 'fe'")
    ok(wf(loop("lp", [tool("x")], "after"), tool("after", None, r=ref("steps.lp.output.state"))))
    bad(wf(loop("lp", [tool("x")], "after", state={"a": ref("steps.x.output")}), tool("after")),
        r"step 'lp'.*steps.x.output")


def test_own_loop_output_is_unreadable_inside_its_body():
    bad(wf(loop("lp", [tool("x", None, r=ref("steps.lp.output"))])), r"step 'x'.*loop step 'lp'.*own body")


def test_while_scope():
    ok(wf(loop("lp", [tool("x")], **{"while": {"op": "lt", "left": ref("loop.lp.index"), "right": 3}},
               state={"n": 0}, carry={"n": ref("steps.x.output")})))
    ok(wf(loop("lp", [tool("x")], **{"while": {"op": "gt", "left": ref("loop.lp.state.n"), "right": 0}},
               state={"n": 1})))
    bad(wf(loop("lp", [tool("x")], **{"while": {"op": "eq", "left": ref("steps.x.output"), "right": 1}})),
        r"step 'lp': while.*'steps.x.output'.*body step 'x'.*cannot read body steps")
    mixed = {"all": [DONE, {"op": "eq", "left": ref("steps.x.output"), "right": 1}]}
    bad(wf(loop("lp", [tool("x")], **{"while": mixed})), r"step 'lp': while")


def test_until_and_carry_read_body_steps_only_on_every_path():
    branchy = [
        {"id": "br", "type": "branch", "cases": [{"when": DONE, "next": "left"}], "default": "right"},
        tool("left", "tail"), tool("right", "tail"), tool("tail"),
    ]
    ok(wf(loop("lp", branchy, until={"op": "exists", "left": ref("steps.tail.output")})))
    ok(wf(loop("lp", branchy, until={"op": "exists", "left": ref("steps.br.output")})))
    bad(wf(loop("lp", branchy, until={"op": "exists", "left": ref("steps.left.output")})),
        r"step 'lp': until.*body step 'left'.*loop_ref_not_on_every_path")
    bad(wf(loop("lp", branchy, state={"a": 0}, carry={"a": ref("steps.right.output")})),
        r"step 'lp': carry.*body step 'right'.*loop_ref_not_on_every_path")
    bad(wf(loop("lp", branchy, until={"not": {"op": "exists", "left": ref("steps.left.output")}})),
        r"loop_ref_not_on_every_path")


def test_carry_and_until_may_read_loop_state():
    ok(wf(loop("lp", [tool("x", None, s=ref("loop.lp.state.n"))], state={"n": 0},
               carry={"n": ref("loop.lp.state.n")},
               until={"op": "gt", "left": ref("loop.lp.state.n"), "right": 1})))


def test_inner_loop_reads_enclosing_roots_but_not_unrelated_loops():
    inner = loop("in", [tool("x", None, o=ref("loop.out.index"), i=ref("loop.in.index"))])
    ok(wf(loop("out", [inner])))
    bad(wf(loop("out", [loop("in", [tool("x")])], "n"),
           loop("n", [tool("y", None, r=ref("loop.in.index"))])), r"step 'y'")


# --- bounds -------------------------------------------------------------------------------------

def test_static_execution_bound_products():
    assert static_execution_bound(ok(wf(tool("t")))) == 1
    assert static_execution_bound(ok(wf(foreach("f", [tool("a")], max_items=7)))) == 1 + 7 * 1
    # body counts every step: a -> b gives 2 per iteration
    assert static_execution_bound(ok(wf(foreach("f", [tool("a", "b"), tool("b")], max_items=7)))) == 1 + 7 * 2
    nested = wf(loop("o", [foreach("i", [tool("x")], max_items=4)], max_iterations=5))
    assert static_execution_bound(ok(nested)) == 1 + 5 * (1 + 4 * 1)
    mixed = wf(tool("p", "o"), loop("o", [tool("m", "i"), loop("i", [tool("x")], max_iterations=6)],
                                    max_iterations=3, ), )
    assert static_execution_bound(ok(mixed)) == 1 + (1 + 3 * (1 + (1 + 6 * 1)))
    two = wf(foreach("a", [tool("x")], "b", max_items=10), foreach("b", [tool("y")], max_items=20))
    assert static_execution_bound(ok(two)) == (1 + 10) + (1 + 20)


def test_static_execution_bound_can_exceed_the_publish_cap():
    big = wf(loop("o", [loop("i", [tool("x")], max_iterations=100)], max_iterations=100))
    assert static_execution_bound(ok(big)) == 1 + 100 * (1 + 100 * 1) > 2000
    ceiling = wf(foreach("o", [foreach("i", [tool("x")], max_items=1000)], max_items=1000))
    assert static_execution_bound(ok(ceiling)) == 1 + 1000 * (1 + 1000)


def test_run_concurrency_is_the_product_along_a_chain_and_the_max_across_chains():
    ch = wf(foreach("a", [foreach("b", [tool("x")], concurrency=3)], "c", concurrency=2),
            foreach("c", [tool("y")], concurrency=5))
    assert run_concurrency(ok(ch)) == 6
    wide = wf(foreach("a", [foreach("b", [tool("x")], concurrency=8)], concurrency=8))
    assert run_concurrency(ok(wide)) == 64 > 8
    via_loop = wf(foreach("a", [loop("l", [foreach("b", [tool("x")], concurrency=4)])], concurrency=3))
    with pytest.raises(ValidationError):
        ok(via_loop)  # a foreach in a loop in a foreach is depth 3
    via_loop = wf(loop("l", [foreach("b", [tool("x")], concurrency=4)]))
    assert run_concurrency(ok(via_loop)) == 4
    assert run_concurrency(ok(wf(loop("l", [tool("x")])))) == 1


def test_mandatory_bound_and_schema_ceiling():
    assert MAX_LOOP_ITERATIONS == 1000
    ok(wf(loop("lp", [tool("x")], max_iterations=100)))  # the platform default cap is allowed
    ok(wf(loop("lp", [tool("x")], max_iterations=1000)))  # schema ceiling
    ok(wf(foreach("fe", [tool("x")], max_items=100)))
    bad(wf(loop("lp", [tool("x")], max_iterations=1001)), r"step 'lp': max_iterations")


# --- combinators --------------------------------------------------------------------------------

def _case(when):
    return wf({"id": "r", "type": "branch", "cases": [{"when": when, "next": "a"}], "default": "a"}, tool("a"))


def test_combinators_nest_and_round_trip():
    when = {"all": [DONE, {"any": [{"not": DONE}, {"all": [DONE]}]}]}
    spec = ok(_case(when))
    dumped = spec.model_dump(mode="json")["steps"][0]["cases"][0]["when"]
    assert dumped["all"][1]["any"][0] == {"not": {"op": "eq", "left": {"$ref": "input.a"}, "right": 1}}
    assert canonical_workflow_hash(ok(spec.model_dump(mode="json"))) == canonical_workflow_hash(spec)
    assert ok(_case(DONE)).model_dump(mode="json")["steps"][0]["cases"][0]["when"] == {
        "op": "eq", "left": {"$ref": "input.a"}, "right": 1}


@pytest.mark.parametrize("key", ["all", "any"])
def test_empty_and_oversized_combinators(key):
    bad(_case({key: []}), rf"'{key}' takes 1 to 8")
    bad(_case({key: [DONE] * 9}), rf"'{key}' takes 1 to 8 conditions, got 9")
    ok(_case({key: [DONE] * 8}))


def test_not_shapes_and_depth():
    bad(_case({"not": []}), r"condition")
    bad(_case({"not": None}), r"condition")
    deep = DONE
    for _ in range(4):
        deep = {"not": deep}
    ok(_case(deep))
    bad(_case({"not": deep}), r"nests all/any/not more than 4 levels deep")
    bad(_case({"all": [{"any": [{"all": [{"any": [{"all": [DONE]}]}]}]}]}), r"more than 4 levels")


def test_combinator_refs_are_scope_checked():
    bad(_case({"all": [DONE, {"not": {"op": "exists", "left": ref("steps.ghost.output")}}]}), r"step 'r'")
    bad(_case({"any": [DONE], "all": [DONE]}), r"step 'r'.*(Extra inputs|condition)")
    bad(_case({"op": "eq", "left": ref("input.a"), "right": 1, "all": [DONE]}), r"step 'r'.*(Extra|condition)")


# --- round trips, unknown fields, raw checks ----------------------------------------------------

def _big():
    return wf(
        tool("seed", "fe"),
        foreach("fe", [loop("lp", [agent("w", None, x=ref("loop.fe.item"))], state={"n": 0},
                            carry={"n": ref("loop.lp.index")},
                            until={"not": {"op": "exists", "left": ref("steps.w.output")}})],
                "ev", items="steps.seed.output", concurrency=2, on_item_error="collect"),
        {"id": "ev", "type": "evaluate", "value": ref("steps.fe.output"), "expected": [1],
         "evaluators": [{"kind": "equals_expected", "name": "eq"}], "pass": "any"},
        limits={"max_cost_usd": "1.5"},
    )


def test_json_round_trip_and_hash_are_stable():
    spec = ok(_big())
    first = spec.model_dump(mode="json")
    again = ok(json.loads(spec.model_dump_json()))
    assert again.model_dump(mode="json") == first
    assert canonical_workflow_hash(again) == canonical_workflow_hash(spec)
    assert canonical_workflow_hash(ok(copy.deepcopy(_big()))) == canonical_workflow_hash(spec)
    assert first["steps"][1]["body"]["steps"][0]["until"] == {
        "not": {"op": "exists", "left": {"$ref": "steps.w.output"}, "right": None}}
    assert first["limits"] == {"max_cost_usd": "1.5"}


def test_unknown_fields_are_rejected_everywhere():
    for path in ("top", "foreach", "loop", "evaluate", "body", "limits"):
        spec = _big()
        target = {"top": spec, "foreach": spec["steps"][1], "loop": spec["steps"][1]["body"]["steps"][0],
                  "evaluate": spec["steps"][2], "body": spec["steps"][1]["body"], "limits": spec["limits"]}[path]
        target["surprise"] = 1
        with pytest.raises(ValidationError, match="surprise|Extra"):
            ok(spec)


def test_raw_checks_recurse_into_bodies():
    deep: object = 1
    for _ in range(40):
        deep = {"k": deep}
    bad(wf(loop("lp", [tool("x", None, v=deep)])), r"step 'x'.*nested more than 32 levels")
    bad(wf(foreach("fe", [loop("lp", [tool("x", None, v=deep)])])), r"step 'x'.*nested more than 32")
    bad(wf(loop("lp", [tool("x", None, v="a\x00b")])), r"NUL character.*step 'x'")
    bad(wf(loop("lp", [tool("x")], state={"v": deep})), r"step 'lp'.*nested more than 32")
    bad(wf(loop("lp", [tool("x")], until={"op": "eq", "left": ref("input.a"), "right": deep})),
        r"step 'lp'.*nested")
