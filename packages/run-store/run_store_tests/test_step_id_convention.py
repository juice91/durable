"""The publish-time `step_id_convention` warning: branch steps `<subject>_gate`, workflow.return steps
`ret_<reason>`, every id at most 40 characters. Pure checks over a spec; no database."""

from __future__ import annotations

import pytest
from eap_run_store.workflow_checks import WARNING_STEP_ID_CONVENTION, step_id_convention_warnings
from eap_schemas.workflows import WorkflowSpec

EXISTS = {"op": "exists", "left": {"$ref": "input.a"}}


def _ret(sid: str) -> dict:
    return {"id": sid, "type": "tool", "tool_id": "workflow.return", "input": {"outcome": "completed"}}


def _gate(sid: str, nxt: str = "ret_end") -> dict:
    return {"id": sid, "type": "branch", "cases": [{"when": EXISTS, "next": nxt}], "default": nxt}


def _tool(sid: str) -> dict:
    return {"id": sid, "type": "tool", "tool_id": "json.query", "input": {}}


def _spec(*steps: dict) -> WorkflowSpec:
    """A valid spec holding `steps`: a conforming hub branch reaches each one, and every branch leads to ret_end."""
    ids = [st["id"] for st in steps if st["id"] != "ret_end"]
    hub = {"id": "route_gate", "type": "branch", "default": "ret_end",
           "cases": [{"when": EXISTS, "next": sid} for sid in ids]}
    end = [] if any(st["id"] == "ret_end" for st in steps) else [_ret("ret_end")]
    return WorkflowSpec.model_validate({
        "name": "t", "input_schema": {"type": "object"}, "start": "route_gate", "steps": [hub, *steps, *end]})


def _suggestions(spec: WorkflowSpec) -> dict[str, str]:
    out: dict[str, str] = {}
    for w in step_id_convention_warnings(spec):
        assert w["code"] == WARNING_STEP_ID_CONVENTION
        step_id, suggested = w["message"].split("'")[1], w["message"].rsplit("'", 2)[1]
        out[step_id] = suggested
    return out


@pytest.mark.parametrize(
    ("branch_id", "suggested"),
    [("gate_check", "check_gate"), ("check", "check_gate"), ("gate", "check_gate"), ("login_check", "login_check_gate"),
     ("_".join(["word"] * 12), None)],
)
def test_branch_ids_end_in_gate(branch_id, suggested):
    got = _suggestions(_spec(_gate(branch_id)))
    if suggested is None:  # too long: shortened, still ends in _gate, within 40
        [(_, s)] = got.items()
        assert s.endswith("_gate") and len(s) <= 40
    else:
        assert got == {branch_id: suggested}


@pytest.mark.parametrize(
    ("ret_id", "suggested"),
    [("r_posted", "ret_posted"), ("return_dry", "ret_dry"), ("posted_ret", "ret_posted"), ("posted", "ret_posted"),
     ("ret", "ret_done"), ("done", "ret_done")],
)
def test_return_ids_start_with_ret(ret_id, suggested):
    assert _suggestions(_spec(_tool("fetch"), _ret(ret_id))) == {ret_id: suggested}


def test_other_steps_only_have_a_length_limit():
    long_id = "fetch_" + "x" * 40
    got = _suggestions(_spec(_tool("fetch_rss"), _tool("post_thumb"), _tool("pick"), _tool(long_id), _ret("ret_ok")))
    assert list(got) == [long_id] and len(got[long_id]) <= 40
    # a non-branch step may be named like a gate or a return: the rules bind branch and return steps only
    assert _suggestions(_spec(_tool("check_gate"), _tool("ret_x"), _ret("ret_y"))) == {}


def test_a_conforming_spec_gets_no_warning():
    spec = _spec(_tool("fetch_rss"), _gate("check_gate"), _ret("ret_posted"), _gate("richtext_gate"),
                 _ret("ret_check_failed"))
    assert step_id_convention_warnings(spec) == []


def test_each_warning_names_the_path_and_the_suggestion():
    spec = _spec(_tool("fetch"), _gate("gate_check"), _ret("r_posted"))
    warnings = step_id_convention_warnings(spec)
    assert [w["path"] for w in warnings] == ["steps[2]", "steps[3]"]
    assert "step 'gate_check'" in warnings[0]["message"] and "suggested name 'check_gate'" in warnings[0]["message"]
    assert "suggested name 'ret_posted'" in warnings[1]["message"]


def test_loop_bodies_are_included():
    body = {"start": "gate_x", "steps": [_gate("gate_x", "r_stop"), _ret("r_stop")]}
    loop = {"id": "each", "type": "loop", "max_iterations": 2, "until": EXISTS, "body": body}
    spec = _spec(loop)
    warnings = step_id_convention_warnings(spec)
    assert [w["path"] for w in warnings] == ["steps[1].body.steps[0]", "steps[1].body.steps[1]"]
    assert _suggestions(spec) == {"gate_x": "x_gate", "r_stop": "ret_stop"}


def test_parallel_branch_steps_are_included():
    def agent(sid: str, nxt: str | None = None) -> dict:
        step = {"id": sid, "type": "agent", "agent_id": "00000000-0000-0000-0000-000000000001", "input": {}}
        return step | ({"next": nxt} if nxt else {})

    par = {"id": "fan", "type": "parallel", "branches": ["a", "b"], "join": "gate_join"}
    spec = WorkflowSpec.model_validate({
        "name": "t", "input_schema": {"type": "object"}, "start": "fan",
        "steps": [par, agent("a", "gate_join"), agent("b", "gate_join"), _gate("gate_join"), _ret("ret_end")]})
    assert _suggestions(spec) == {"gate_join": "join_gate"}
