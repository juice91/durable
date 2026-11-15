"""LP.13 acceptance: the AG-UI mapping for workflow loops (D17).

Decision on the open question (state shape): D17 says the loop step's own path `/steps/{L}`
carries `{status, type, iterations_started, iterations_done, iterations_failed}`, and AGUI.4's D11
puts a completed step's result under `steps.{id}.output`. The loop's `last`/`until`-style result
(`iterations, exited, last, state`, or an error code) is that step OUTPUT, so it lives inside
`/steps/{L}/output`, not as top-level keys. D17 is the source of truth; the LP.13 bullet
(counters only) agrees. These tests enforce that shape.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from eap_schemas.workflow_scope import instance_token, make_scope

from eap_api.streams.native import ChildScope, DurableEvent

from .test_agui4_translate import Log, check, drive, types, wf_ctx, workflow_log


def sc(*frames: tuple[str, int]) -> list[dict[str, Any]]:
    return [{"loop": loop, "index": i} for loop, i in frames]


def cid(tag: str) -> str:
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"lp13:{tag}"))


def deltas(events):
    return [op for e in events if e["type"] == "STATE_DELTA" for op in e["delta"]]


def foreach_run(n: int, *, fail: set[int] = frozenset(), tool: bool = False, end: str = "run_completed"):
    """A top-level foreach with an agent body (child run per iteration)."""
    log = Log()
    items: list[Any] = [log.add("run_started", kind="workflow", workflow_id="w", input={}),
                        log.add("step_started", step_id="each", step_type="foreach")]
    for i in range(n):
        child = Log(cid(f"b{i}"))
        scope = ChildScope("body", child.run_id)
        items += [log.add("loop_iteration_started", step_id="each", scope=[], index=i),
                  log.add("step_started", step_id="body", step_type="agent", child_run_id=child.run_id,
                          scope=sc(("each", i))),
                  DurableEvent(child.add("run_started", input="x").event, scope)]
        if tool:
            call = f"call_{i}"
            msg = {"kind": "response", "parts": [{"part_kind": "tool-call", "tool_name": "t",
                                                  "tool_call_id": call, "args": {}}]}
            items += [DurableEvent(child.add("model_message", model_request=0, text="", message=msg).event, scope),
                      DurableEvent(child.add("tool_call", tool_call_id=call, tool_name="t", args={}).event, scope),
                      DurableEvent(child.add("tool_result", tool_call_id=call, tool_name="t", result="ok").event, scope),
                      DurableEvent(child.add("model_message", model_request=1, text=f"r{i}", message={}).event, scope)]
        else:
            items.append(DurableEvent(child.add("model_message", model_request=0, text=f"r{i}", message={}).event, scope))
        if i in fail:
            items += [DurableEvent(child.add("run_failed", status="failed", error="x", error_code="tool_error").event, scope),
                      log.add("step_failed", step_id="body", status="failed", error="x", error_code="tool_error",
                              scope=sc(("each", i))),
                      log.add("loop_iteration_failed", step_id="each", scope=[], index=i, status="failed")]
        else:
            items += [DurableEvent(child.add("run_completed", output=f"r{i}").event, scope),
                      log.add("step_completed", step_id="body", output=f"r{i}", scope=sc(("each", i))),
                      log.add("loop_iteration_completed", step_id="each", scope=[], index=i)]
    items.append(log.add("step_completed", step_id="each", output=[f"r{i}" for i in range(n)]))
    if end == "run_completed":
        items.append(log.add("run_completed", output=None))
    return items, log


# --------------------------------------------------------------------------- instance names


def test_one_level_names_match_instance_token_and_top_level_unchanged():
    items, _ = foreach_run(3)
    ev = check(drive(wf_ctx(), items)[0])
    starts = [e["stepName"] for e in ev if e["type"] == "STEP_STARTED"]
    expected = ["each"] + [instance_token(make_scope([("each", i)]), "body") for i in range(3)]
    assert starts == expected
    assert [e["stepName"] for e in ev if e["type"] == "STEP_FINISHED"] == expected[1:] + ["each"]


def test_two_level_names_match_instance_token_for_every_event():
    log = Log()
    leaf = cid("leaf")
    s2 = sc(("outer", 1), ("inner", 2))
    items = [
        log.add("run_started", kind="workflow", workflow_id="w", input={}),
        log.add("step_started", step_id="outer", step_type="foreach"),
        log.add("step_started", step_id="inner", step_type="loop", scope=sc(("outer", 1))),
        log.add("step_started", step_id="leaf", step_type="agent", child_run_id=leaf, scope=s2),
        log.add("step_failed", step_id="leaf", status="failed", error="e", error_code="x", scope=s2),
        log.add("step_completed", step_id="inner", output={}, scope=sc(("outer", 1))),
        log.add("step_completed", step_id="outer", output=[]),
        log.add("run_completed", output=None),
    ]
    ev = check(drive(wf_ctx(), items)[0])
    tok_inner = instance_token(make_scope([("outer", 1)]), "inner")
    tok_leaf = instance_token(make_scope([("outer", 1), ("inner", 2)]), "leaf")
    assert tok_leaf == "outer.1~inner.2~leaf"
    assert [e["stepName"] for e in ev if e["type"] == "STEP_STARTED"] == ["outer", tok_inner, tok_leaf]
    assert [e["stepName"] for e in ev if e["type"] == "STEP_FINISHED"] == [tok_leaf, tok_inner, "outer"]


def test_loop_free_step_names_unchanged():
    items, _, _ = workflow_log()
    ev = check(drive(wf_ctx(), items)[0])
    assert [e["stepName"] for e in ev if e["type"] == "STEP_STARTED"] == ["draft", "lookup", "route", "fan", "a/b", "ok"]


# --------------------------------------------------------------------------- message ids


def test_three_iterations_have_distinct_message_ids_and_tool_scheme():
    items, _ = foreach_run(3, tool=True)
    ev = check(drive(wf_ctx(), items)[0])
    ids = [e["messageId"] for e in ev if e["type"] == "TEXT_MESSAGE_START"]
    want = [f"wrun-1-{instance_token(make_scope([('each', i)]), 'body')}-m{n}" for i in range(3) for n in (0, 1)]
    assert ids == want and len(set(ids)) == 6
    calls = [e["toolCallId"] for e in ev if e["type"] == "TOOL_CALL_START"]
    assert calls == ["call_0", "call_1", "call_2"]
    results = [e["messageId"] for e in ev if e["type"] == "TOOL_CALL_RESULT"]
    assert results == [f"wrun-1-each.{i}~body-t-call_{i}" for i in range(3)]
    assert len(set(results)) == 3


def test_ids_never_collide_across_iterations_and_kinds():
    items, _ = foreach_run(3, tool=True)
    ev = check(drive(wf_ctx(), items)[0])
    ids = [e["messageId"] for e in ev if e["type"] in ("TEXT_MESSAGE_START", "TOOL_CALL_RESULT")]
    assert len(ids) == 9 and len(set(ids)) == 9


# --------------------------------------------------------------------------- state scope


def test_every_state_delta_touches_only_the_loop_step_or_status():
    items, _ = foreach_run(3, fail={1}, tool=True)
    ev = check(drive(wf_ctx(), items)[0])
    paths = [op["path"] for op in deltas(ev)]
    assert all(p == "/status" or p == "/steps/each" or p.startswith("/steps/each/") for p in paths), paths
    assert not any("body" in p or "~" in p for p in paths)


def test_body_step_events_emit_no_state_delta():
    items, _ = foreach_run(2)
    out, _f = drive(wf_ctx(), items)
    ev = check(out)
    # Between a body STEP_STARTED and its STEP_FINISHED no STATE_DELTA appears.
    inside = False
    for e in ev:
        if e["type"] == "STEP_STARTED" and "~" in e["stepName"]:
            inside = True
        elif e["type"] == "STEP_FINISHED" and "~" in e["stepName"]:
            inside = False
            continue
        assert not (inside and e["type"] == "STATE_DELTA"), e


def test_nested_inner_counters_do_not_reach_state():
    log = Log()
    o = sc(("outer", 0))
    items = [
        log.add("run_started", kind="workflow", workflow_id="w", input={}),
        log.add("step_started", step_id="outer", step_type="foreach"),
        log.add("loop_iteration_started", step_id="outer", scope=[], index=0),
        log.add("step_started", step_id="inner", step_type="foreach", scope=o),
        log.add("loop_iteration_started", step_id="inner", scope=o, index=0),
        log.add("loop_iteration_completed", step_id="inner", scope=o, index=0),
        log.add("loop_iteration_started", step_id="inner", scope=o, index=1),
        log.add("loop_iteration_failed", step_id="inner", scope=o, index=1, status="failed"),
        log.add("step_completed", step_id="inner", output=[], scope=o),
        log.add("loop_iteration_completed", step_id="outer", scope=[], index=0),
        log.add("step_completed", step_id="outer", output=[]),
        log.add("run_completed", output=None),
    ]
    ev = check(drive(wf_ctx(), items)[0])
    ops = deltas(ev)
    assert not any("inner" in op["path"] for op in ops)
    counters = {op["path"].rsplit("/", 1)[1]: op["value"] for op in ops if "/iterations_" in op["path"]}
    assert counters == {"iterations_started": 1, "iterations_done": 1, "iterations_failed": 0} or \
        counters == {"iterations_started": 1, "iterations_done": 1}
    # but the inner loop's frames are still streamed
    frames = [e["value"] for e in ev if e.get("name") == "pydanticos.loop_iteration"]
    assert sum(f["step_id"] == "inner" for f in frames) == 4


def _final_counters(ev):
    state: dict[str, Any] = {}
    for op in deltas(ev):
        if op["path"].startswith("/steps/each/iterations_"):
            state[op["path"].rsplit("/", 1)[1]] = op["value"]
    return state


def test_counters_across_completed_failed_iterations():
    items, _ = foreach_run(4, fail={1, 3})
    ev = check(drive(wf_ctx(), items)[0])
    seed = next(op for op in deltas(ev) if op["path"] == "/steps/each")["value"]
    assert (seed["iterations_started"], seed["iterations_done"], seed["iterations_failed"]) == (0, 0, 0)
    assert _final_counters(ev) == {"iterations_started": 4, "iterations_done": 2, "iterations_failed": 2}


def test_failed_then_cancelled_run_counters_match_last_event():
    items, log = foreach_run(2, fail={1}, end="none")
    items.pop()  # drop `step_completed each`: the loop was cut short by the cancel
    items += [log.add("cancel_requested", requested_by="u"),
              log.add("step_failed", step_id="each", status="failed", error="c", error_code="run_cancelled"),
              log.add("run_failed", status="failed", error="c", error_code="run_cancelled")]
    ev = check(drive(wf_ctx(), items)[0])
    assert _final_counters(ev) == {"iterations_started": 2, "iterations_done": 1, "iterations_failed": 1}
    assert ev[-1]["type"] == "RUN_FINISHED" and ev[-1]["outcome"] == {"type": "cancelled"}
    last = [op for op in deltas(ev) if op["path"].startswith("/steps/each")][-1]
    assert last["path"] == "/steps/each/status" and last["value"] == "failed"


def test_cancelled_mid_iteration_leaves_started_ahead_of_done():
    log = Log()
    items = [log.add("run_started", kind="workflow", workflow_id="w", input={}),
             log.add("step_started", step_id="each", step_type="foreach"),
             log.add("loop_iteration_started", step_id="each", scope=[], index=0),
             log.add("loop_iteration_completed", step_id="each", scope=[], index=0),
             log.add("loop_iteration_started", step_id="each", scope=[], index=1),
             log.add("run_failed", status="failed", error="c", error_code="run_cancelled")]
    ev = check(drive(wf_ctx(), items)[0])
    assert _final_counters(ev) == {"iterations_started": 2, "iterations_done": 1}
    assert ev[-1]["outcome"] == {"type": "cancelled"}


# --------------------------------------------------------------------------- custom frames


def test_loop_iteration_frames_shape_status_and_schema():
    items, _ = foreach_run(3, fail={2})
    ev = check(drive(wf_ctx(), items)[0])  # check() validates against the AG-UI 1.0 schema
    frames = [e for e in ev if e["type"] == "CUSTOM" and e["name"] == "pydanticos.loop_iteration"]
    assert len(frames) == 6
    for f in frames:
        assert set(f) <= {"type", "name", "value", "timestamp", "rawEvent"}
        assert set(f["value"]) == {"step_id", "scope", "index", "status"}
        assert f["value"]["status"] in {"started", "completed", "failed"}
        assert f["value"]["step_id"] == "each" and f["value"]["scope"] == []
        assert isinstance(f["value"]["index"], int)
    assert [f["value"]["status"] for f in frames][-2:] == ["started", "failed"]


def test_nested_frame_carries_outer_scope():
    log = Log()
    o = sc(("outer", 3))
    items = [log.add("run_started", kind="workflow", workflow_id="w", input={}),
             log.add("step_started", step_id="outer", step_type="foreach"),
             log.add("step_started", step_id="inner", step_type="loop", scope=o),
             log.add("loop_iteration_started", step_id="inner", scope=o, index=5),
             log.add("run_completed", output=None)]
    ev = check(drive(wf_ctx(), items)[0])
    f = next(e for e in ev if e.get("name") == "pydanticos.loop_iteration")
    assert f["value"] == {"step_id": "inner", "scope": o, "index": 5, "status": "started"}


# --------------------------------------------------------------------------- cancel + silent


@pytest.mark.parametrize("code", ["tool_error", "run_budget_exceeded", "loop_max_iterations", "internal_error"])
def test_non_cancel_codes_still_render_run_error(code):
    log = Log()
    items = [log.add("run_started", kind="workflow", workflow_id="w", input={}),
             log.add("step_started", step_id="each", step_type="foreach"),
             log.add("loop_iteration_started", step_id="each", scope=[], index=0),
             log.add("run_failed", status="failed", error="x", error_code=code)]
    ev = check(drive(wf_ctx(), items)[0])
    assert ev[-1]["type"] == "RUN_ERROR" and ev[-1]["code"] == code
    assert "RUN_FINISHED" not in types(ev)


def test_cancel_outcome_after_loop_and_cancel_requested_is_silent():
    items, log = foreach_run(2, end="none")
    items.pop()
    items += [log.add("cancel_requested", requested_by="someone"),
              log.add("run_failed", status="failed", error="c", error_code="run_cancelled")]
    chunks, _ = drive(wf_ctx(), items)
    ev = check(chunks)
    assert "cancel_requested" not in "".join(chunks) and "someone" not in "".join(chunks)
    assert [e["type"] for e in ev].count("RUN_FINISHED") == 1
    assert ev[-1]["outcome"] == {"type": "cancelled"}
    assert not any(e["type"] == "RUN_ERROR" for e in ev)


def test_cancel_before_any_step():
    log = Log()
    ev = check(drive(wf_ctx(), [log.add("run_started", kind="workflow", workflow_id="w", input={}),
                                log.add("cancel_requested", requested_by="u"),
                                log.add("run_failed", status="failed", error="c", error_code="run_cancelled")])[0])
    assert ev[-1]["outcome"] == {"type": "cancelled"} and "STEP_STARTED" not in types(ev)


# --------------------------------------------------------------------------- state shape (decision)


def test_loop_result_lives_inside_step_output_not_top_level_keys():
    log = Log()
    out = {"iterations": 2, "exited": "condition", "last": {"v": 1}, "state": {"n": 2}}
    items = [log.add("run_started", kind="workflow", workflow_id="w", input={}),
             log.add("step_started", step_id="lp", step_type="loop"),
             log.add("step_completed", step_id="lp", output=out),
             log.add("run_completed", output=None)]
    ev = check(drive(wf_ctx(), items)[0])
    ops = [op for op in deltas(ev) if op["path"].startswith("/steps/lp")]
    assert {"op": "replace", "path": "/steps/lp/output", "value": out} in [
        {k: op[k] for k in ("op", "path", "value")} for op in ops] or \
        {"op": "add", "path": "/steps/lp/output", "value": out} in [
            {k: op[k] for k in ("op", "path", "value")} for op in ops]
    assert not any(op["path"].rsplit("/", 1)[-1] in {"last", "until", "error_code"} for op in ops)


# --------------------------------------------------------------------------- byte-identical


def test_loop_free_workflow_is_byte_identical_to_the_pre_lp13_recording():
    from pathlib import Path
    recorded = json.loads((Path(__file__).parent / "fixtures" / "agui_loop_free_workflow_pre_lp13.json").read_text())
    items, _, _ = workflow_log()
    chunks, _ = drive(wf_ctx(), items)
    assert chunks == recorded
    check(chunks)
