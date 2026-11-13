"""$ref availability around parallel blocks, and non-finite numbers (task 3.7 review fixes)."""

import json
import uuid

import pytest
from pydantic import ValidationError

from eap_schemas.workflows import WorkflowSpec

NO_OUTPUT = "which has no output"


def agent(i, nxt=None, **refs):
    inp = {k: {"$ref": v} for k, v in refs.items()}
    return {"id": i, "type": "agent", "agent_id": str(uuid.uuid4()), "next": nxt, "input": inp}


def par(i, branches, join):
    return {"id": i, "type": "parallel", "branches": branches, "join": join}


def spec(steps, start):
    return {"name": "W", "input_schema": {"type": "object"}, "steps": steps, "start": start}


def ok(steps, start="s0"):
    return WorkflowSpec.model_validate(spec(steps, start))


def bad(steps, match, start="s0"):
    with pytest.raises(ValidationError, match=match):
        ok(steps, start)


def test_branch_root_cannot_read_its_own_parallel_step():
    bad([agent("s0", "p"), par("p", ["a", "b"], "j"), agent("a", x="steps.p.output"), agent("b"), agent("j")],
        r"step 'a': \$ref 'steps\.p\.output' refers to parallel step 'p', " + NO_OUTPUT)


def test_join_and_later_steps_cannot_read_the_parallel_step_either():
    bad([par("s0", ["a", "b"], "j"), agent("a"), agent("b"), agent("j", "k", x="steps.s0.output"), agent("k")],
        r"step 'j': .* refers to parallel step 's0', " + NO_OUTPUT)
    bad([par("s0", ["a", "b"], "j"), agent("a"), agent("b"), agent("j", "k"), agent("k", x="steps.s0.output.a")],
        r"step 'k': .* refers to parallel step 's0', " + NO_OUTPUT)


def _nested(**refs):
    # outer (x | y) -> oj ; x is the inner parallel (q1 | q2) -> qj
    return [
        par("outer", ["x", "y"], "oj"),
        par("x", ["q1", "q2"], "qj"),
        agent("q1", **refs), agent("q2"), agent("qj"), agent("y"), agent("oj"),
    ]


def test_nested_branch_cannot_read_the_outer_or_inner_parallel_step():
    bad(_nested(v="steps.outer.output"), r"step 'q1': .* refers to parallel step 'outer', " + NO_OUTPUT, "outer")
    bad(_nested(v="steps.x.output"), r"step 'q1': .* refers to parallel step 'x', " + NO_OUTPUT, "outer")


def test_branch_root_reads_a_step_before_the_parallel():
    ok([agent("s0", "p"), par("p", ["a", "b"], "j"), agent("a", x="steps.s0.output"), agent("b"), agent("j")])
    nested = [agent("s0", "outer")] + _nested(v="steps.s0.output")
    ok(nested)


def test_join_reads_branch_steps_but_siblings_and_skipped_arms_stay_rejected():
    ok([par("s0", ["a", "b"], "j"), agent("a", "a2"), agent("a2"), agent("b"),
        agent("j", x="steps.a.output", y="steps.a2.output", z="steps.b.output")])
    bad([par("s0", ["a", "b"], "j"), agent("a"), agent("b", x="steps.a.output"), agent("j")],
        "refers to step 'a', which does not run before 'b' on every path")
    arm = {"id": "s0", "type": "branch", "default": "d",
           "cases": [{"when": {"op": "exists", "left": {"$ref": "input.z"}}, "next": "c"}]}
    bad([arm, agent("c", "d"), agent("d", x="steps.c.output")],
        "refers to step 'c', which does not run before 'd' on every path")


def test_inner_branch_targeting_the_outer_join_is_rejected():
    steps = [par("outer", ["x", "y"], "oj"), par("x", ["q1", "q2"], "qj"),
             agent("q1", "oj"), agent("q2"), agent("qj"), agent("y"), agent("oj")]
    bad(steps, "parallel step", "outer")


@pytest.mark.parametrize("number", ["1e309", "-1e309", "NaN", "Infinity"])
@pytest.mark.parametrize("where", ["template", "condition", "input_schema", "output_schema"])
def test_non_finite_numbers_are_rejected(number, where):
    lit = '{"lit": %s}' % number
    steps = '[{"id":"s0","type":"agent","agent_id":"%s","input":%s,"next":"b"},' % (
        uuid.uuid4(), lit if where == "template" else "{}")
    right = number if where == "condition" else "1"
    steps += '{"id":"b","type":"branch","default":"e","cases":[{"when":{"op":"eq","left":{"$ref":"input.q"},"right":%s},"next":"e"}]},' % right
    steps += '{"id":"e","type":"approval","prompt":"p","timeout_hours":1}]'
    schema = '{"type":"object","maximum":%s}' % (number if where == "input_schema" else "1")
    out = ',"output_schema":{"maximum":%s}' % number if where == "output_schema" else ""
    raw = json.loads('{"name":"W","input_schema":%s%s,"start":"s0","steps":%s}' % (schema, out, steps))
    with pytest.raises(ValidationError, match="non-finite number"):
        WorkflowSpec.model_validate(raw)
