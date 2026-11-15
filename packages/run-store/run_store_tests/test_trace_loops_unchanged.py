"""LP.9 tester: a loop-free workflow trace keeps its pre-loop shape (plus truncated.iterations / the limit)."""

from __future__ import annotations

from eap_schemas.workflow_scope import EMPTY_SCOPE

from eap_run_store.trace import RunData, TraceInput, build_trace

from .loop_trace_fixtures import RUN, Log, body_agent, workflow_run


def _fixture():
    log, children = Log(), []
    log.add("start", "run_started", {"kind": "workflow", "input": {}})
    log.step_started(EMPTY_SCOPE, "a", "tool", tool_id="t", input={"x": 1})
    log.step_completed(EMPTY_SCOPE, "a", {"y": 2})
    body_agent(log, EMPTY_SCOPE, "b", children, output="out")
    log.add("end", "run_completed", {"output": "out"})
    return build_trace(TraceInput(root=RunData(run=workflow_run(), events=log.events, requests={}), children=children))


def test_loop_free_trace_shape():
    t = _fixture()
    assert [(s["id"], s["kind"], s["parent_id"]) for s in t["spans"] if s["kind"] != "model_request"] == [
        (f"run:{RUN}", "run", None),
        (f"{RUN}:step:a", "workflow_step", f"run:{RUN}"),
        (f"{RUN}:step:b", "workflow_step", f"run:{RUN}"),
        (t["spans"][3]["id"], "child_run", f"{RUN}:step:b"),
    ]
    for s in t["spans"][1:3]:
        assert "scope" not in s["attributes"] and not any(k.startswith("iterations") for k in s["attributes"])
    assert t["spans"][1]["attributes"] == {"step_id": "a", "step_type": "tool", "tool_id": "t"}
    assert not any(s["kind"] in ("loop", "loop_iteration") for s in t["spans"])


def test_only_new_truncation_and_limit_keys_are_added():
    t = _fixture()
    assert t["truncated"] == {
        "spans": False, "events": False, "child_runs": False, "payload_fields": 0, "iterations": 0,
    }
    assert t["limits"]["max_loop_iteration_spans"] == 100
    assert set(t["limits"]) == {
        "max_spans", "max_events_per_run", "max_trace_events", "max_child_runs", "field_max_bytes",
        "payload_budget_bytes", "max_loop_iteration_spans", "max_loop_trace_events",
    }
