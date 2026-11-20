"""Loop step kinds, combinators, scoping and bounds (workflow loops milestone, LP.1)."""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest
from eap_schemas.workflows import (
    AllCondition,
    EvaluateStep,
    ForeachStep,
    LoopStep,
    NotCondition,
    WorkflowSpec,
    body_reads,
    canonical_workflow_hash,
    iter_refs,
    loop_exit_refs,
    parse_ref,
    run_concurrency,
    static_execution_bound,
    walk_steps,
)
from pydantic import ValidationError

A = "00000000-0000-0000-0000-0000000000a1"


def agent(sid, nxt=None, **inp):
    return {"id": sid, "type": "agent", "agent_id": A, "input": inp, "next": nxt}


def tool(sid, nxt=None, **inp):
    return {"id": sid, "type": "tool", "tool_id": "clock.now", "input": inp, "next": nxt}


def ref(path):
    return {"$ref": path}


def wf(*steps, start=None, **extra):
    return {"name": "W", "input_schema": {"type": "object"}, "steps": list(steps),
            "start": start or steps[0]["id"], **extra}


def foreach(sid, body, nxt=None, items="steps.fetch.output.tickets", **kw):
    return {"id": sid, "type": "foreach", "items": ref(items), "max_items": 50,
            "body": {"start": body[0]["id"], "steps": body}, "next": nxt, **kw}


def loop(sid, body, nxt=None, **kw):
    kw.setdefault("max_iterations", 10)
    if "while" not in kw and "until" not in kw:
        kw["until"] = {"op": "eq", "left": ref(f"steps.{body[-1]['id']}.output.done"), "right": True}
    return {"id": sid, "type": "loop", "body": {"start": body[0]["id"], "steps": body}, "next": nxt, **kw}


def ok(spec):
    return WorkflowSpec.model_validate(spec)


def bad(spec, pattern):
    with pytest.raises(ValidationError, match=pattern):
        WorkflowSpec.model_validate(spec)


FOREACH = wf(
    tool("fetch", "each"),
    foreach("each", [agent("triage", "update", t=ref("loop.each.item"), i=ref("loop.each.index")),
                     tool("update", None, r=ref("steps.triage.output"), src=ref("steps.fetch.output"))],
            nxt="report", concurrency=4),
    tool("report", None, all=ref("steps.each.output")),
)

UNTIL = wf(
    loop("poll", [tool("check", None, s=ref("loop.poll.state.n"))],
         state={"n": 0}, carry={"n": ref("steps.check.output.n")}),
)


# --- valid specs -----------------------------------------------------------------------------

def test_foreach_valid_and_round_trips():
    spec = ok(FOREACH)
    each = spec.steps[1]
    assert isinstance(each, ForeachStep) and each.concurrency == 4 and each.on_item_error == "fail"
    again = WorkflowSpec.model_validate(spec.model_dump(mode="json"))
    assert canonical_workflow_hash(again) == canonical_workflow_hash(spec)


def test_loop_until_and_while_dump_with_json_keys():
    spec = ok(UNTIL)
    dumped = spec.model_dump(mode="json")["steps"][0]
    assert "until" in dumped and "while" not in dumped and "while_" not in dumped
    w = ok(wf(loop("w", [tool("x")], **{"while": {"op": "lt", "left": ref("loop.w.index"), "right": 3}})))
    d = w.model_dump(mode="json")["steps"][0]
    assert d["while"] == {"op": "lt", "left": {"$ref": "loop.w.index"}, "right": 3, }
    assert "until" not in d and "state" not in d and "carry" not in d
    assert canonical_workflow_hash(ok(w.model_dump(mode="json"))) == canonical_workflow_hash(w)


def test_while_reads_state_and_index():
    ok(wf(loop("w", [tool("x")], state={"go": True},
               **{"while": {"all": [{"op": "eq", "left": ref("loop.w.state.go"), "right": True},
                                    {"op": "lt", "left": ref("loop.w.index"), "right": 5}]}})))


def test_nested_depth_two_reads_both_loops():
    inner = loop("inner", [tool("leaf", None, it=ref("loop.outer.item"), st=ref("loop.inner.state.k"),
                                 oi=ref("loop.outer.index"))], state={"k": ref("loop.outer.item")},
                 carry={"k": ref("steps.leaf.output")})
    ok(wf(tool("fetch", "outer"), foreach("outer", [inner])))


def test_evaluate_valid_top_level_and_in_body():
    judge = {"id": "judge", "type": "evaluate", "value": ref("steps.write.output.draft"),
             "evaluators": [{"kind": "llm_judge", "rubric": "good?", "model": {"provider": "test", "name": "judge"}},
                            {"kind": "schema_valid", "json_schema": {"type": "object"}}]}
    spec = ok(wf(loop("refine", [agent("write", "judge", fb=ref("loop.refine.state.feedback")), judge],
                      state={"feedback": ""}, carry={"feedback": ref("steps.judge.output.reason")},
                      until={"op": "eq", "left": ref("steps.judge.output.passed"), "right": True}, max_iterations=3)))
    step = spec.steps[0].body.steps[1]
    assert isinstance(step, EvaluateStep) and step.pass_ == "all"
    assert spec.model_dump(mode="json")["steps"][0]["body"]["steps"][1]["pass"] == "all"
    top = ok(wf(tool("t", "e"), {"id": "e", "type": "evaluate", "value": ref("steps.t.output"), "expected": 3,
                                 "evaluators": [{"kind": "equals_expected"}], "pass": "any"}))
    assert top.steps[1].pass_ == "any" and iter_refs(top.steps[1]) == ["steps.t.output"]


def test_combinators_in_a_branch_and_bare_comparison_json_unchanged():
    when = {"any": [{"not": {"op": "exists", "left": ref("input.a")}},
                    {"all": [{"op": "eq", "left": ref("input.b"), "right": 1}]}]}
    spec = ok(wf({"id": "b", "type": "branch", "cases": [{"when": when, "next": "x"},
                                                         {"when": {"op": "eq", "left": ref("input.c"), "right": 2},
                                                          "next": "x"}], "default": "x"}, tool("x")))
    cases = spec.model_dump(mode="json")["steps"][0]["cases"]
    when["any"][0]["not"]["right"] = None  # a comparison dumps right: null, exactly as before
    assert cases[0]["when"] == when
    assert cases[1]["when"] == {"op": "eq", "left": {"$ref": "input.c"}, "right": 2}
    assert spec.steps[0].cases[0].when.refs() == ["input.a", "input.b"]
    assert isinstance(spec.steps[0].cases[0].when.any[0], NotCondition)


def test_limits_excluded_while_empty():
    base = ok(wf(tool("t")))
    assert "limits" not in base.model_dump(mode="json")
    empty = ok(wf(tool("t"), limits={}))
    assert canonical_workflow_hash(empty) == canonical_workflow_hash(base)
    capped = ok(wf(tool("t"), limits={"max_cost_usd": "0.50"}))
    assert capped.limits.max_cost_usd == Decimal("0.5")
    assert capped.model_dump(mode="json")["limits"] == {"max_cost_usd": "0.5"}
    assert canonical_workflow_hash(capped) != canonical_workflow_hash(base)
    for value in ("0", "1000.1", "0.00001"):
        bad(wf(tool("t"), limits={"max_cost_usd": value}), "max_cost_usd")


def test_existing_shapes_keep_null_fields():
    dumped = ok(wf(agent("a", "t"), tool("t"))).model_dump(mode="json")
    assert dumped["steps"][0]["version"] is None and dumped["steps"][1]["next"] is None
    assert dumped["output_schema"] is None


# --- rejections ------------------------------------------------------------------------------

@pytest.mark.parametrize("value, msg", [(None, "Field required"), (0, "greater than or equal to 1"),
                                        (1001, "less than or equal to 1000")])
def test_bounds(value, msg):
    f = copy.deepcopy(FOREACH)
    if value is None:
        del f["steps"][1]["max_items"]
    else:
        f["steps"][1]["max_items"] = value
    bad(f, rf"step 'each': max_items: .*{msg}")
    lp = copy.deepcopy(UNTIL)
    if value is None:
        del lp["steps"][0]["max_iterations"]
    else:
        lp["steps"][0]["max_iterations"] = value
    bad(lp, rf"step 'poll': max_iterations: .*{msg}")


def test_while_and_until_exactly_one():
    both = loop("l", [tool("x")], **{"while": {"op": "exists", "left": ref("input.a")}},
                until={"op": "exists", "left": ref("input.a")})
    bad(wf(both), "step 'l': a loop needs exactly one of 'while' and 'until'")
    neither = loop("l", [tool("x")], until=None)
    bad(wf(neither), "step 'l': a loop needs exactly one of 'while' and 'until'")


def test_concurrency_nine():
    f = copy.deepcopy(FOREACH)
    f["steps"][1]["concurrency"] = 9
    bad(f, "step 'each': concurrency: .*less than or equal to 8")


def test_depth_three():
    l3 = loop("l3", [tool("x")])
    l2 = loop("l2", [l3], until={"op": "exists", "left": ref("input.a")})
    l1 = loop("l1", [l2], until={"op": "exists", "left": ref("input.a")})
    bad(wf(l1), "step 'l3': loops nest at most 2 deep")


@pytest.mark.parametrize("step", [
    {"id": "gate", "type": "approval", "prompt": "ok?", "timeout_hours": 1},
    {"id": "gate", "type": "parallel", "branches": ["p1", "p2"], "join": "j"},
])
def test_approval_or_parallel_in_body(step):
    body = [step] if step["type"] == "approval" else [step, tool("p1", "j"), tool("p2", "j"), tool("j")]
    bad(wf(loop("l", body, until={"op": "exists", "left": ref("input.a")})),
        rf"step 'l': body step 'gate' is a {step['type']} step.*\(loop_body_step_not_allowed\)")


def test_loop_inside_parallel_branch():
    bad(wf({"id": "p", "type": "parallel", "branches": ["l", "t"], "join": "j"},
           loop("l", [tool("x")], nxt="j"), tool("t", "j"), tool("j")),
        r"parallel step 'p': step 'l' in branch 'l' is a loop step.*\(loop_in_parallel_not_allowed\)")


def test_loop_at_join_is_fine():
    ok(wf({"id": "p", "type": "parallel", "branches": ["a", "b"], "join": "l"}, tool("a", "l"), tool("b", "l"),
          loop("l", [tool("x")], until={"op": "exists", "left": ref("steps.a.output")})))


def test_duplicate_ids_across_depths():
    bad(wf(tool("x", "l"), loop("l", [tool("x")])), "duplicate step id 'x'")


def test_cycle_and_unreachable_in_body():
    bad(wf(loop("l", [tool("a", "b"), tool("b", "a")])), r"step 'l': body: body graph has a cycle: a -> b -> a")
    bad(wf(loop("l", [tool("a"), tool("b")], until={"op": "exists", "left": ref("input.x")})),
        r"step 'l': body: step 'b' is not reachable from body start step 'a'")


def test_body_target_outside_body():
    bad(wf(loop("l", [tool("a", "after")], nxt="after"), tool("after")),
        r"step 'l': body: step 'a': next target 'after' does not exist in this body")


def test_body_step_read_outside_its_loop():
    f = copy.deepcopy(FOREACH)
    f["steps"][2]["input"] = {"x": ref("steps.triage.output")}
    bad(f, r"step 'report': \$ref 'steps.triage.output' refers to step 'triage' inside the body of loop 'each'")


def test_item_on_loop_and_state_on_foreach():
    bad(wf(loop("l", [tool("x", None, i=ref("loop.l.item"))])), r"step 'x': .*'.item' exists only in a foreach")
    f = copy.deepcopy(FOREACH)
    f["steps"][1]["body"]["steps"][0]["input"] = {"s": ref("loop.each.state")}
    bad(f, r"step 'triage': .*'.state' exists only in a loop step")


def test_non_enclosing_loop():
    bad(wf(loop("l", [tool("x")], nxt="after"), tool("after", None, i=ref("loop.l.index"))),
        r"step 'after': \$ref 'loop.l.index' reads loop 'l', which does not enclose 'after'")
    bad(wf(tool("t", None, i=ref("loop.nope.index"))), "refers to unknown loop 'nope'")


def test_index_has_no_fields():
    bad(wf(loop("l", [tool("x", None, i=ref("loop.l.index.x"))])), "loop.<loop_id>.index is a number")


def test_own_output_inside_body():
    bad(wf(loop("l", [tool("x", None, o=ref("steps.l.output"))])),
        r"step 'x': \$ref 'steps.l.output' refers to loop step 'l' from inside its own body")


def test_while_reading_body_step():
    bad(wf(loop("l", [tool("x")], **{"while": {"op": "exists", "left": ref("steps.x.output")}})),
        r"step 'l': while: \$ref 'steps.x.output' reads body step 'x'.*cannot read body steps")


def _branchy_body():
    return [{"id": "b", "type": "branch", "cases": [{"when": {"op": "exists", "left": ref("input.a")}, "next": "c"}],
             "default": "d"}, tool("c", "d"), tool("d")]


def test_until_or_carry_reading_a_skipped_body_step():
    bad(wf(loop("l", _branchy_body(), until={"op": "exists", "left": ref("steps.c.output")})),
        r"step 'l': until: .*'c'.*\(loop_ref_not_on_every_path\)")
    bad(wf(loop("l", _branchy_body(), until={"op": "exists", "left": ref("steps.d.output")},
                state={"v": 0}, carry={"v": ref("steps.c.output")})),
        r"step 'l': carry: .*'c'.*\(loop_ref_not_on_every_path\)")
    ok(wf(loop("l", _branchy_body(), until={"op": "exists", "left": ref("steps.d.output")})))


def test_more_than_fifty_steps_in_all():
    body = [tool(f"s{i}", f"s{i + 1}" if i < 46 else None) for i in range(47)]
    bad(wf(tool("a", "b"), tool("b", "l"), loop("l", body, nxt="c"), tool("c"), start="a"),
        r"at most 50 steps in all \(loop bodies included\), got 51")
    ok(wf(tool("a", "l"), loop("l", body)))  # 1 + 1 + 47 = 49


def test_combinator_limits():
    leaf = {"op": "exists", "left": ref("input.a")}
    bad(wf({"id": "b", "type": "branch", "cases": [{"when": {"all": [leaf] * 9}, "next": "x"}], "default": "x"},
           tool("x")), "'all' takes 1 to 8 conditions, got 9")
    bad(wf({"id": "b", "type": "branch", "cases": [{"when": {"any": []}, "next": "x"}], "default": "x"}, tool("x")),
        "'any' takes 1 to 8 conditions, got 0")
    deep = leaf
    for _ in range(5):
        deep = {"not": deep}
    bad(wf({"id": "b", "type": "branch", "cases": [{"when": deep, "next": "x"}], "default": "x"}, tool("x")),
        "nests all/any/not more than 4 levels deep")
    four = leaf
    for _ in range(4):
        four = {"not": four}
    ok(wf({"id": "b", "type": "branch", "cases": [{"when": four, "next": "x"}], "default": "x"}, tool("x")))
    bad(wf({"id": "b", "type": "branch", "cases": [{"when": {"xor": []}, "next": "x"}], "default": "x"}, tool("x")),
        "a condition must be a comparison")


def _nested(depth):
    value = 1
    for _ in range(depth):
        value = [value]
    return value


def test_raw_checks_recurse_into_bodies():
    bad(wf(loop("l", [tool("x", None, v=_nested(40))])), r"step 'x': input: value of 'v' is nested more than 32")
    bad(wf(loop("l", [tool("x")], until={"op": "eq", "left": ref("input.a"), "right": _nested(40)})),
        r"step 'l': until.right: operand is nested more than 32")
    bad(wf(loop("l", [tool("x", None, v="a\x00b")])), r"NUL character .*\(step 'x'\)")


def test_evaluate_rejections():
    base = {"id": "e", "type": "evaluate", "value": ref("input.a")}
    bad(wf({**base, "evaluators": []}), "step 'e': evaluators")
    bad(wf({**base, "evaluators": [{"kind": "max_cost", "usd": 1}]}), "not available in an evaluate step")
    bad(wf({**base, "evaluators": [{"kind": "equals_expected"}]}), "needs the step's 'expected'")
    bad(wf({**base, "evaluators": [{"kind": "schema_valid"}]}), "needs a json_schema")
    bad(wf({**base, "value": 3, "evaluators": [{"kind": "equals_expected"}]}), "value must be a \\$ref")
    bad(wf({**base, "evaluators": [{"kind": "regex", "pattern": "a", "name": "n"},
                                   {"kind": "regex", "pattern": "b", "name": "n"}]}), "duplicate evaluator name")


def test_foreach_items_must_be_a_ref():
    f = copy.deepcopy(FOREACH)
    f["steps"][1]["items"] = [1, 2]
    bad(f, r"step 'each': items: .*items must be a \$ref")


# --- helpers ---------------------------------------------------------------------------------

def test_parse_ref():
    assert parse_ref("input.a") == ("input", "a", None)
    assert parse_ref("input") == ("input", None, None)
    assert parse_ref("steps.x.output.y") == ("steps", "x", None)
    assert parse_ref("loop.l.state.k") == ("loop", "l", "state")


def test_body_reads_static_bound_and_run_concurrency():
    spec = ok(FOREACH)
    assert body_reads(spec.steps[1]) == ("fetch",)
    assert static_execution_bound(spec) == 1 + (1 + 50 * 2) + 1
    assert run_concurrency(spec) == 4
    inner = foreach("inner", [tool("leaf", None, a=ref("steps.pre.output"), b=ref("steps.mid.output"))],
                    items="steps.fetch.output", concurrency=2)
    inner["max_items"] = 5
    outer = foreach("outer", [tool("mid", "inner"), inner], concurrency=3)
    outer["max_items"] = 10
    nested = ok(wf(tool("fetch", "pre"), tool("pre", "outer"), outer))
    assert body_reads(nested.steps[2]) == ("fetch", "pre")  # inner's items read fetch inside an iteration
    assert body_reads(nested.steps[2].body.steps[1]) == ("mid", "pre")  # mid runs in outer's body, outside inner's
    assert static_execution_bound(nested) == 2 + 1 + 10 * (1 + 1 + 5 * 1)
    assert run_concurrency(nested) == 6
    assert run_concurrency(ok(UNTIL)) == 1 and run_concurrency(ok(wf(tool("t")))) == 1
    lp = ok(UNTIL).steps[0]
    assert isinstance(lp, LoopStep)
    assert body_reads(lp) == () and loop_exit_refs(lp) == ["steps.check.output.n", "steps.check.output.done"]
    assert [(c, s.id) for c, s in walk_steps(nested.steps)][-1] == (("outer", "inner"), "leaf")


def test_loop_carry_reads_outer_step_in_body_reads():
    spec = ok(wf(tool("pre", "l"), loop("l", [tool("x")], state={"v": 0}, carry={"v": ref("steps.pre.output")})))
    assert body_reads(spec.steps[1]) == ("pre",)


def test_combinator_objects():
    c = AllCondition.model_validate({"all": [{"op": "exists", "left": ref("input.a")}]})
    assert c.model_dump(mode="json") == {"all": [{"op": "exists", "left": {"$ref": "input.a"}, "right": None}]}


# --- hash goldens (computed on the code before LP.1, commit 7960175) -------------------------

GOLDEN_FIXTURES = {
    "agent_tool": {
        "name": "Triage", "input_schema": {"type": "object"},
        "steps": [
            {"id": "classify", "type": "agent", "agent_id": A, "input": {"text": {"$ref": "input.text"}}, "next": "save"},
            {"id": "save", "type": "tool", "tool_id": "tickets.create", "input": {"title": {"$ref": "steps.classify.output.title"}, "n": 3}},
        ],
        "start": "classify",
    },
    "branch_approval": {
        "name": "Branchy", "description": "d", "input_schema": {"type": "object"}, "output_schema": {"type": "object"},
        "steps": [
            {"id": "route", "type": "branch", "cases": [
                {"when": {"op": "eq", "left": {"$ref": "input.x"}, "right": 1}, "next": "ok"},
                {"when": {"op": "exists", "left": {"$ref": "input.y"}}, "next": "gate"}], "default": "ok"},
            {"id": "gate", "type": "approval", "prompt": "Go?", "timeout_hours": 2, "next": "ok"},
            {"id": "ok", "type": "agent", "agent_id": A, "version": 2},
        ],
        "start": "route",
    },
    "parallel": {
        "name": "Par", "input_schema": {"type": "object"},
        "steps": [
            {"id": "fork", "type": "parallel", "branches": ["a", "b"], "join": "j"},
            {"id": "a", "type": "tool", "tool_id": "clock.now", "next": "j"},
            {"id": "b", "type": "agent", "agent_id": A},
            {"id": "j", "type": "tool", "tool_id": "clock.now", "input": {"a": {"$ref": "steps.a.output"}, "b": {"$ref": "steps.b.output"}}},
        ],
        "start": "fork",
    },
}

GOLDEN_HASHES = {
    "agent_tool": "fb22915c1b254c157b5c67d4d3b064235337b6d4df63ea771b825239f3d5b987",
    "branch_approval": "6af65eb0cb78a5c40476552a62db4c6d6130b273a64a9b51d3fcc3f035b37d2b",
    "parallel": "dc84324619b61af0d139883c327330d067323afc9ecfa4029bccb6721cdd0bca",
}


@pytest.mark.parametrize("name", sorted(GOLDEN_HASHES))
def test_pre_loop_specs_keep_their_hash(name):
    spec = WorkflowSpec.model_validate(GOLDEN_FIXTURES[name])
    assert canonical_workflow_hash(spec) == GOLDEN_HASHES[name]
    assert "limits" not in spec.model_dump(mode="json")


# --- review fixes ----------------------------------------------------------------------------

def test_storable_message_ignores_step_lookalikes_inside_literals():
    fake = {"steps": [{"id": "impostor", "x": "a\x00"}]}
    with pytest.raises(ValidationError) as exc:
        WorkflowSpec.model_validate(wf(tool("t", None, v=fake)))
    assert "at steps.0.input.v.steps.0.x (step 't')" in str(exc.value) and "impostor" not in str(exc.value)
    bad(wf(loop("l", [tool("inner", None, v="a\x00")])), r"at steps\.0\.body\.steps\.0\.input\.v \(step 'inner'\)")


def test_error_paths_leave_out_union_tags():
    with pytest.raises(ValidationError) as exc:
        WorkflowSpec.model_validate(wf(loop("l", [tool("x", None, q=ref("bogus.path"))])))
    text = str(exc.value)
    assert "body.steps.0: step 'x': input: invalid $ref" in text and ".tool:" not in text
    when = {"all": [{"op": "eq", "left": {"x": ref("input.a")}, "right": 1}]}
    bad(wf({"id": "b", "type": "branch", "cases": [{"when": when, "next": "x"}], "default": "x"}, tool("x")),
        r"step 'b': cases\.0\.when\.all\.0\.left: \$ref is only allowed as a whole value")


def test_items_or_initial_state_reading_their_own_loop():
    bad(wf(foreach("f", [tool("x")], items="loop.f.item")),
        r"step 'f': items: \$ref 'loop.f.item' reads loop 'f' itself, but items is evaluated before the loop starts")
    bad(wf(loop("l", [tool("x")], state={"a": ref("loop.l.state")})),
        r"step 'l': state: .*reads loop 'l' itself, but state is evaluated before the loop starts")
