"""AGUI.8 acceptance: AG-UI workflow endpoints, approvals as interrupts, resume, one run per thread.

Real Postgres, a real in-process worker (DBOS launched in this process on a private system schema)
and a DBOSClient behind the API's launcher. The implementer's tests use a scripted client stand-in;
these drive the real interpreter (agent steps with gated tool calls, an approval step, parallel
branches) and the real decide path (`eap_api.approvals.decide`).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from eap_api.ag_ui.conformance import events_of, lifecycle_errors, parse_frames, schema_errors
from eap_api.app import create_app
from eap_api.launcher import DBOSRunLauncher, LauncherSettings, get_run_launcher
from eap_api.routes.ag_ui import AgUiRouteSettings, get_agui_settings
from eap_api.routes.ag_ui_workflows import derive_workflow_run_id
from eap_api.sse import StreamSettings, get_delta_source, get_stream_settings, no_deltas

from .agui4_harness import SCHEMA
from .test_agui8_workflow_endpoints import comparable, suffix_after
from .conftest import get_auth_header
from .run_helpers import PG_URL, cleanup_tenant, hard_timeout, requires_pg

pytestmark = requires_pg

DBOS_URL = os.environ.get("EAP_TEST_DBOS_URL", "postgresql://eap_dbos:eap_dbos_password@localhost:54329/eap_dbos")
APP_VERSION = "agui8-acc"
TICKET = {"title": "Laptop broken", "body": "It will not boot."}
GATED_PROMPT = json.dumps({"tool": "tickets.create", "args": TICKET})


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@pytest.fixture(autouse=True)
def _no_hang():
    with hard_timeout(150):
        yield


@pytest.fixture(scope="module")
def worker():
    """An in-process worker on a private DBOS schema; the API's client uses the same schema."""
    os.environ["EAP_QUEUE_POLL_SECONDS"] = "0.2"
    os.environ["EAP_APPROVAL_POLL_SECONDS"] = "1"
    os.environ.setdefault("EAP_AUTH_MODE", "dev")
    from dbos import DBOS, DBOSClient

    from eap_worker.app import launch
    from eap_worker.config import WorkerSettings
    from eap_worker.runtime import get_runtime

    schema = f"eap_test_agui8_{uuid.uuid4().hex[:10]}"
    settings = WorkerSettings(
        app_database_url=PG_URL, dbos_system_database_url=DBOS_URL, dbos_system_schema=schema,
        app_version=APP_VERSION, executor_id=f"agui8-{uuid.uuid4().hex[:8]}", allowed_tools=None,
        litellm_base_url=None, litellm_api_key=None,
    )
    launch(settings)
    client = DBOSClient(system_database_url=DBOS_URL, dbos_system_schema=schema)
    try:
        yield get_runtime(), client, schema
    finally:
        client.destroy()
        DBOS.destroy(destroy_registry=False)
        eng = create_engine(DBOS_URL.replace("postgresql://", "postgresql+psycopg://", 1))
        with eng.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        eng.dispose()


@pytest.fixture
def api(worker, monkeypatch):
    monkeypatch.setenv("EAP_AUTH_MODE", "dev")
    monkeypatch.setenv("EAP_ALLOWED_MODELS", "test:*")
    _, dbos, schema = worker
    app = create_app(database_url=PG_URL)
    launcher = DBOSRunLauncher(LauncherSettings(DBOS_URL, schema, APP_VERSION), client=dbos)
    app.dependency_overrides[get_run_launcher] = lambda: launcher
    app.dependency_overrides[get_stream_settings] = lambda: StreamSettings(poll_interval=0.05, heartbeat_interval=1.0)
    app.dependency_overrides[get_delta_source] = lambda: no_deltas
    app.dependency_overrides[get_agui_settings] = lambda: AgUiRouteSettings(previous_turn_wait_s=5.0)
    tenants: list[str] = []
    app.state.test_tenants = tenants
    with TestClient(app) as client:
        yield client, app
    engine = app.state.database_engine
    for t in tenants:
        cleanup_tenant(engine, t)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_versions WHERE tenant_id = :t"), {"t": t})
            conn.execute(text("DELETE FROM workflows WHERE tenant_id = :t"), {"t": t})
    engine.dispose()


@pytest.fixture
def tenant(api):
    t = f"t{uuid.uuid4().hex[:10]}"
    api[1].state.test_tenants.append(t)
    return t


# --------------------------------------------------------------------------- helpers


def wait_for(predicate, timeout: float = 60.0, what: str = "") -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


def body(thread: str, run: str, *, resume=None, messages=(), **pydanticos: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "threadId": thread, "runId": run, "messages": list(messages), "tools": [], "context": [], "state": None,
        "forwardedProps": {"pydanticos": pydanticos} if pydanticos else {},
    }
    if resume is not None:
        out["resume"] = resume
    return out


def post(client, url: str, payload: dict[str, Any], headers: dict[str, str], extra: dict[str, str] | None = None):
    with client.stream("POST", url, json=payload, headers={**headers, **(extra or {})}) as response:
        raw = response.read()
        if response.status_code != 200:
            return response.status_code, json.loads(raw), []
        return 200, None, parse_frames([raw.decode()])


def attach(client, url: str, headers: dict[str, str], last_event_id: str | None = None):
    extra = {"Last-Event-ID": last_event_id} if last_event_id else {}
    with client.stream("GET", url, headers={**headers, **extra}) as response:
        raw = response.read()
        if response.status_code != 200:
            return response.status_code, json.loads(raw), []
        return 200, None, parse_frames([raw.decode()])


def check(frames, **kw: Any) -> list[dict[str, Any]]:
    events = events_of(frames)
    assert schema_errors(events, SCHEMA) == []
    assert lifecycle_errors(frames, **kw) == []
    return events


def types(frames) -> list[str]:
    return [f"{f.event['type']}:{f.event['name']}" if f.event["type"] == "CUSTOM" else f.event["type"]
            for f in frames if f.event is not None]


def finished(frames) -> dict[str, Any]:
    return next(f.event for f in reversed(frames) if f.event and f.event["type"] == "RUN_FINISHED")


def interrupt_ids(frames) -> list[str]:
    outcome = finished(frames).get("outcome") or {}
    return [i["id"] for i in outcome.get("interrupts", [])] if outcome.get("type") == "interrupt" else []


def user_msg(mid: str, text_: str = "any news?") -> dict[str, Any]:
    return {"id": mid, "role": "user", "content": text_}


def approve(ids, approved: bool = True, **extra: Any) -> list[dict[str, Any]]:
    return [{"interruptId": i, "status": "resolved", "payload": {"approved": approved, **extra}} for i in ids]


def wf_url(workflow_id: str) -> str:
    return f"/api/v1/ag-ui/workflows/{workflow_id}"


def open_rows(app, tenant: str) -> list[tuple[str, str]]:
    with app.state.database_engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT kind, approval_key FROM pending_approvals WHERE tenant_id = :t AND decided_at IS NULL "
            "ORDER BY requested_at"), {"t": tenant})]


def wait_open(app, tenant: str, count: int) -> list[tuple[str, str]]:
    return wait_for(lambda: (r := open_rows(app, tenant)) if len(open_rows(app, tenant)) >= count else None,
                    what=f"{count} open approvals")


def run_row(app, run_id: str) -> dict[str, Any]:
    with app.state.database_engine.connect() as conn:
        return dict(conn.execute(text("SELECT status, error FROM runs WHERE id = :r"), {"r": run_id}).mappings().one())


def wait_status(app, run_id: str, wanted: set[str]) -> dict[str, Any]:
    return wait_for(lambda: (r := run_row(app, run_id)) if run_row(app, run_id)["status"] in wanted else None,
                    what=f"run {run_id} in {wanted}")


def events_db(app, run_id: str) -> list[dict[str, Any]]:
    with app.state.database_engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(
            "SELECT seq, event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq"), {"r": run_id}).mappings()]


# --------------------------------------------------------------------------- publishing

ECHO = {"name": "Echo", "instructions": "Help.", "model": {"provider": "test", "name": "echo"}}
GATED = {"name": "Gated", "instructions": "Help.", "model": {"provider": "test", "name": "approval"},
         "tools": ["tickets.create"], "approvals": {"tools": ["tickets.create"]}}
SCHEMA_IN = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"], "additionalProperties": False}


def publish_agent(client, headers, spec: dict[str, Any]) -> str:
    r = client.post("/api/v1/agents", json={"spec": spec}, headers=headers)
    assert r.status_code == 201, r.text
    agent_id = r.json()["id"]
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=headers).status_code == 201
    return agent_id


def publish_workflow(client, headers, spec: dict[str, Any]) -> str:
    r = client.post("/api/v1/workflows", json={"spec": {"input_schema": SCHEMA_IN, **spec}}, headers=headers)
    assert r.status_code == 201, r.text
    workflow_id = r.json()["id"]
    assert client.post(f"/api/v1/workflows/{workflow_id}/versions", headers=headers).status_code == 201
    return workflow_id


def agent_step(step_id: str, agent_id: str, prompt_ref: str, nxt: str | None = None) -> dict[str, Any]:
    step: dict[str, Any] = {"id": step_id, "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": prompt_ref}}}
    return {**step, "next": nxt} if nxt else step


def mixed_workflow(client, headers) -> str:
    """ask (echo) -> parallel [gated agent step (tool approval) | approval step] -> done (echo)."""
    echo, gated = publish_agent(client, headers, ECHO), publish_agent(client, headers, GATED)
    return publish_workflow(client, headers, {"name": "Mixed", "start": "ask", "steps": [
        agent_step("ask", echo, "input.q", "fork"),
        {"id": "fork", "type": "parallel", "branches": ["tool_step", "appr"], "join": "done"},
        agent_step("tool_step", gated, "input.q"),
        {"id": "appr", "type": "approval", "prompt": "Escalate?", "timeout_hours": 1},
        agent_step("done", echo, "steps.ask.output"),
    ]})


def step_workflow(client, headers, *, hours: int = 1) -> str:
    """ask (echo) -> approval step."""
    echo = publish_agent(client, headers, ECHO)
    return publish_workflow(client, headers, {"name": "Steps", "start": "ask", "steps": [
        agent_step("ask", echo, "input.q", "appr"),
        {"id": "appr", "type": "approval", "prompt": "Escalate?", "timeout_hours": hours},
    ]})


def child_workflow(client, headers) -> str:
    """one gated agent step."""
    return publish_workflow(client, headers, {"name": "Child", "start": "tool_step", "steps": [
        agent_step("tool_step", publish_agent(client, headers, GATED), "input.q"),
    ]})


# --------------------------------------------------------------------------- a mixed workflow


def start_mixed(client, app, tenant, thread="th", run="r1"):
    h = get_auth_header(tenant, "alice")
    wf = mixed_workflow(client, h)
    status, detail, frames = post(client, wf_url(wf), body(thread, run, input={"q": GATED_PROMPT}), h)
    assert status == 200, detail
    return h, wf, frames


def test_mixed_workflow_streams_conformant_with_steps_state_and_all_interrupts(api, tenant):
    client, app = api
    h, wf, first = start_mixed(client, app, tenant)
    check(first)
    t = types(first)
    assert {"STEP_STARTED", "STEP_FINISHED", "STATE_SNAPSHOT", "STATE_DELTA"} <= set(t)
    assert t[0] == "RUN_STARTED" and t[-1] == "RUN_FINISHED"
    started = [f.event["stepName"] for f in first if f.event and f.event["type"] == "STEP_STARTED"]
    assert "ask" in started
    first_ids = interrupt_ids(first)
    assert first_ids, "the stream must stop on an interrupt"
    run_id = derive_workflow_run_id(tenant, wf, "th")
    rows = wait_open(app, tenant, 2)
    assert {k for k, _ in rows} == {"tool", "step"}
    # Q5: a new message re-sends every open interrupt, of both kinds, in the order requested.
    s2, d2, again = post(client, wf_url(wf), body("th", "r2", messages=[user_msg("u2")]), h)
    assert s2 == 200, d2
    check(again)
    ids = interrupt_ids(again)
    assert [i.rsplit(":", 1)[-1] for i in ids] == [k for _, k in rows]  # the order requested
    assert {i.split(":")[2] == "tool" for i in ids} == {True, False}  # both kinds
    assert set(first_ids) <= set(ids)
    assert open_rows(app, tenant) == rows  # nothing recorded
    assert [e for e in events_db(app, run_id) if e["type"] in ("step_approved", "tool_approved")] == []
    assert all(f.id is None for f in again)  # a re-send carries no frame id


def q5_ids(client, app, tenant, h, wf, count=2, thread="th", run="rq"):
    wait_open(app, tenant, count)
    status, detail, frames = post(client, wf_url(wf), body(thread, run, messages=[user_msg(run + "-u")]), h)
    assert status == 200, detail
    ids = interrupt_ids(frames)
    assert len(ids) == count
    return ids


def same_frames(live, replay) -> bool:
    """Frames with a workflow seq replay identically (keep-alive comments aside); child frames
    (no id) interleave by timing, so only their number must match."""
    live, replay = [f for f in live if f.event], [f for f in replay if f.event]
    keyed = lambda fs: [(f.id, f.event["type"]) for f in fs if f.id]  # noqa: E731
    return keyed(live) == keyed(replay) and len(live) == len(replay)


def err_types(detail) -> list[str]:
    return [d["type"] for d in detail["detail"]]


def decision_events(app, run_ids) -> list[tuple[str, str]]:
    out = []
    for r in run_ids:
        out += [(e["type"], e["event_key"]) for e in events_db(app, r)
                if e["type"] in ("step_approved", "step_rejected", "tool_approved", "tool_denied")]
    return out


def all_runs(app, tenant) -> list[str]:
    with app.state.database_engine.connect() as conn:
        return [str(r[0]) for r in conn.execute(text("SELECT id FROM runs WHERE tenant_id = :t"), {"t": tenant})]


def test_resume_refuses_partial_and_unknown_then_approves_both_kinds(api, tenant):
    client, app = api
    h, wf, _ = start_mixed(client, app, tenant)
    run_id = derive_workflow_run_id(tenant, wf, "th")
    ids = q5_ids(client, app, tenant, h, wf)
    # Partial coverage: refused before anything is recorded.
    for one in ids:
        s, d, _ = post(client, wf_url(wf), body("th", "r3", resume=approve([one])), h)
        assert (s, err_types(d)) == (422, ["resume_missing_interrupt"]), d
    # Unknown ids: another run's, another tenant's shape, garbage, and a mixed list with a stray.
    stray = f"approval:{uuid.uuid4()}:appr"
    cases = {"stray+all": approve(ids + [stray]), "garbage": approve(["nope"]),
             "other-run-tool": approve(ids + [f"approval:{uuid.uuid4()}:tool:call-1"])}
    for name, payload in cases.items():
        s, d, _ = post(client, wf_url(wf), body("th", "r3", resume=payload), h)
        assert s == 422 and err_types(d)[0] == "resume_unknown_interrupt", (name, s, d)
    assert decision_events(app, all_runs(app, tenant)) == []
    assert len(open_rows(app, tenant)) == 2
    # Both answered through the real decide path.
    s, d, frames = post(client, wf_url(wf), body("th", "r4", resume=approve(ids, reason="ok")), h)
    assert s == 200, d
    check(frames)
    wait_status(app, run_id, {"completed"})
    assert finished(frames)["outcome"]["type"] == "success"
    decided = decision_events(app, all_runs(app, tenant))
    assert sorted(t for t, _ in decided) == ["step_approved", "tool_approved"]
    assert open_rows(app, tenant) == []
    t = types(frames)
    assert "CUSTOM:pydanticos.approval_decided" in t and "TOOL_CALL_RESULT" in t
    # A second POST of the same resume decides nothing new and shows the recorded decisions.
    s, d, again = post(client, wf_url(wf), body("th", "r4", resume=approve(ids, reason="ok")), h)
    assert s == 200, d  # the same runId: a D7 replay, not a second decision
    check(again)
    # Frames with a workflow seq replay identically; child frames (no id) interleave by timing.
    assert same_frames(frames, again)
    assert sorted(t for t, _ in decision_events(app, all_runs(app, tenant))) == ["step_approved", "tool_approved"]


def test_deny_and_cancel_end_the_branches_and_record_once(api, tenant):
    client, app = api
    h, wf, _ = start_mixed(client, app, tenant)
    run_id = derive_workflow_run_id(tenant, wf, "th")
    ids = q5_ids(client, app, tenant, h, wf)
    tool_id = next(i for i in ids if ":tool:" in i)
    step_id = next(i for i in ids if ":tool:" not in i)
    resume = [
        {"interruptId": tool_id, "status": "resolved", "payload": {"approved": False, "reason": "not today"}},
        {"interruptId": step_id, "status": "cancelled"},
    ]
    s, d, frames = post(client, wf_url(wf), body("th", "r3", resume=resume), h)
    assert s == 200, d
    check(frames)
    wait_status(app, run_id, {"failed", "completed"})
    runs = all_runs(app, tenant)
    assert sorted(t for t, _ in decision_events(app, runs)) == ["step_rejected", "tool_denied"]
    with app.state.database_engine.connect() as conn:
        comments = {r[0]: (r[1] or {}).get("comment") for r in conn.execute(text(
            "SELECT type, data FROM execution_events WHERE run_id = ANY(:r) AND type IN ('step_rejected','tool_denied')"),
            {"r": runs})}
    assert comments == {"tool_denied": "not today", "step_rejected": "Cancelled by user."}
    run = run_row(app, run_id)
    assert run["status"] == "failed"
    last = [e for e in events_db(app, run_id) if e["type"] == "run_failed"]
    assert len(last) == 1 and last[0]["data"]["error_code"] == "approval_rejected"
    assert (frames[-1].event["type"], frames[-1].event["code"]) == ("RUN_ERROR", "approval_rejected")
    # A late resume of the decided interrupts is accepted; the recorded decisions win; nothing new.
    s, d, _ = post(client, wf_url(wf), body("th", "r5", resume=approve(ids)), h)
    assert s == 200, d
    assert sorted(t for t, _ in decision_events(app, runs)) == ["step_rejected", "tool_denied"]


def test_payload_rules_are_422_before_anything_is_recorded(api, tenant):
    client, app = api
    h, wf, _ = start_mixed(client, app, tenant)
    ids = q5_ids(client, app, tenant, h, wf)
    one, other = ids
    good = approve([other])
    bads = {
        "editedArgs": ("edited_args_unsupported", approve([one], editedArgs={"x": 1})),
        "non-bool": ("resume_payload_invalid", approve([one], True)[:0] + [{"interruptId": one, "status": "resolved", "payload": {"approved": "yes"}}]),
        "no payload": ("resume_payload_invalid", [{"interruptId": one, "status": "resolved"}]),
        "long reason": ("resume_payload_invalid", approve([one], reason="x" * 501)),
    }
    for name, (code, entry) in bads.items():
        s, d, _ = post(client, wf_url(wf), body("th", "r3", resume=entry + good), h)
        assert (s, err_types(d)[0]) == (422, code), (name, d)
    assert decision_events(app, all_runs(app, tenant)) == []
    assert len(open_rows(app, tenant)) == 2
    s, d, _ = post(client, wf_url(wf), body("th", "r3", resume=approve(ids), input={"q": "again"}), h)
    assert (s, err_types(d)[0]) == (422, "resume_with_input")


# --------------------------------------------------------------------------- D7


def seqs(frames) -> list[int]:
    return [int(f.id.rsplit(":", 1)[1]) for f in frames if f.id]


def starts(frames) -> list[str]:
    return [f.event.get("messageId") or f.event.get("toolCallId") for f in frames
            if f.event and f.event["type"] in ("TEXT_MESSAGE_START", "TOOL_CALL_START")]


def snapshot_ids(frames) -> set[str]:
    out: set[str] = set()
    for f in frames:
        if f.event and f.event["type"] == "MESSAGES_SNAPSHOT":
            for m in f.event["messages"]:
                out.add(m["id"])
                out.update(c["id"] for c in m.get("toolCalls") or [])
    return out


def finished_mixed(client, app, tenant):
    """A mixed run answered through resume[] and completed; returns (headers, workflow, run id)."""
    h, wf, first = start_mixed(client, app, tenant)
    run_id = derive_workflow_run_id(tenant, wf, "th")
    ids = q5_ids(client, app, tenant, h, wf)
    s, d, _ = post(client, wf_url(wf), body("th", "r3", resume=approve(ids)), h)
    assert s == 200, d
    wait_status(app, run_id, {"completed"})
    return h, wf, run_id, first, ids


def test_attach_at_every_offset_has_no_duplicates_and_no_gaps(api, tenant):
    client, app = api
    h, wf, run_id, first, ids = finished_mixed(client, app, tenant)
    url = f"{wf_url(wf)}/threads/th/runs/r3/stream"
    s, d, full = attach(client, url, h)
    assert s == 200, d
    check(full)
    full_seqs = seqs(full)
    assert full_seqs == sorted(set(full_seqs)) and len(full_seqs) >= 5
    assert sorted(starts(full)) == sorted(set(starts(full)))  # no duplicated message in a full attach
    offsets = [-1] + full_seqs
    for n in offsets:
        s, d, part = attach(client, url, h, None if n < 0 else f"r3:{n}")
        assert s == 200, (n, d)
        got = seqs(part)
        want = [q for q in full_seqs if q > n]
        # Past the end the attach is just the closing frame (the run's end seq).
        assert got == (want or full_seqs[-1:]), (n, got)  # no gap, no duplicate workflow frame
        if n >= 0 and want:  # the whole frame sequence (child frames, results, CUSTOM extras), not only ids
            assert comparable(part) == comparable(suffix_after(full, f"r3:{n}")), n
        assert starts(part) == sorted(set(starts(part)), key=starts(part).index), n
        # Messages started before the offset (in the full stream) are not started again.
        cut = max((i for i, f in enumerate(full) if f.id and int(f.id.rsplit(":", 1)[1]) <= n), default=-1)
        assert not set(starts(full[: cut + 1])) & set(starts(part)), (n, set(starts(full[: cut + 1])) & set(starts(part)))
        mine = set(starts(part))
        assert not mine & snapshot_ids(part), (n, mine & snapshot_ids(part))  # a snapshot-named message never restarts
        lost = set(starts(full)) - set(starts(full[: cut + 1])) - mine - snapshot_ids(part)
        assert not lost, (n, lost)  # nothing lost
        assert schema_errors(events_of(part), SCHEMA) == []
    # Re-POSTing the run with Last-Event-ID at every offset behaves the same as the attach.
    for n in full_seqs[:: max(1, len(full_seqs) // 4)]:
        s, d, part = post(client, wf_url(wf), body("th", "r3", resume=approve(ids)), h, {"Last-Event-ID": f"r3:{n}"})
        assert s == 200, (n, d)
        assert seqs(part) == ([q for q in full_seqs if q > n] or full_seqs[-1:]), n


def test_attach_of_the_first_run_at_every_offset_is_a_suffix_of_its_frames(api, tenant):
    client, app = api
    h, wf, run_id, first, ids = finished_mixed(client, app, tenant)
    # The earlier AG-UI run (r1) stopped at the interrupt: its attach at every offset is a clean
    # suffix of its own frames, and it never shows what only the resume run streamed.
    url1 = f"{wf_url(wf)}/threads/th/runs/r1/stream"
    first_seqs = seqs(first)
    s, d, r1_full = attach(client, url1, h)
    assert s == 200 and seqs(r1_full) == first_seqs, (d, seqs(r1_full), first_seqs)
    assert comparable(r1_full) == comparable(first)  # the replay is the live stream, frame for frame
    for n in first_seqs:
        s, d, part = attach(client, url1, h, f"r1:{n}")
        assert s == 200, (n, d)
        if n != first_seqs[-1]:
            assert comparable(part) == comparable(suffix_after(first, f"r1:{n}")), n
        assert seqs(part) == ([q for q in first_seqs if q > n] or first_seqs[-1:]), n  # a client that saw r1:4 and reconnects
        assert not set(starts(part)) & snapshot_ids(part), n


# --------------------------------------------------------------------------- one run per thread


def test_one_workflow_run_per_thread_409_with_guidance_and_a_new_thread_starts_a_new_run(api, tenant):
    client, app = api
    h, wf, _ = start_mixed(client, app, tenant)
    run_id = derive_workflow_run_id(tenant, wf, "th")
    wait_open(app, tenant, 2)
    # New input on a thread whose run waits: 409, even though it is waiting; nothing started.
    s, d, _ = post(client, wf_url(wf), body("th", "r2", input={"q": "second"}), h)
    assert s == 409 and d["detail"][0]["type"] == "workflow_thread_running", d
    assert d["detail"][0]["loc"] == ["body", "forwardedProps", "pydanticos", "input"]
    assert "new threadId" in d["detail"][0]["msg"]
    assert all_runs_of_workflow(app, tenant) == 3  # the workflow run and its two child runs, no second run
    # A new threadId starts a new run with its own derived id.
    s, d, frames = post(client, wf_url(wf), body("th-2", "r1", input={"q": GATED_PROMPT}), h)
    assert s == 200, d
    other = derive_workflow_run_id(tenant, wf, "th-2")
    assert other != run_id and run_row(app, other)["status"] in ("queued", "running")
    # Finish thread 1, then new input on it is 409 workflow_thread_finished.
    ids = q5_ids(client, app, tenant, h, wf, count=2, thread="th", run="rq")
    s, d, _ = post(client, wf_url(wf), body("th", "r3", resume=approve(ids)), h)
    assert s == 200, d
    wait_status(app, run_id, {"completed"})
    s, d, _ = post(client, wf_url(wf), body("th", "r9", input={"q": "third"}), h)
    assert s == 409 and d["detail"][0]["type"] == "workflow_thread_finished", d
    s, d, _ = post(client, wf_url(wf), body("th", "r10", messages=[user_msg("u10")]), h)
    assert s == 409 and d["detail"][0]["type"] == "workflow_thread_finished" and d["detail"][0]["loc"][-1] == "threadId", d
    # The thread GET names the run, its AG-UI runs and the active one.
    t = client.get(f"{wf_url(wf)}/threads/th", headers=h).json()
    assert t["run_id"] == run_id and t["status"] == "completed"
    assert [r["runId"] for r in t["runs"]][0] == "r1" and "r3" in [r["runId"] for r in t["runs"]]
    assert t["state"]["steps"]["appr"]["status"] == "completed"
    # Clean up the second thread's waiting run so the worker does not hold it.
    ids2 = q5_ids(client, app, tenant, h, wf, count=2, thread="th-2", run="rq2")
    s, d, _ = post(client, wf_url(wf), body("th-2", "r3", resume=approve(ids2, False)), h)
    assert s == 200, d
    wait_status(app, other, {"failed"})


def all_runs_of_workflow(app, tenant) -> int:
    return len(all_runs(app, tenant))


def test_409_guidance_names_the_attach_route_untruncated(api, tenant):
    client, app = api
    h, wf, _ = start_mixed(client, app, tenant)
    wait_open(app, tenant, 2)
    s, d, _ = post(client, wf_url(wf), body("th", "r2", input={"q": "second"}), h)
    msg = d["detail"][0]["msg"]
    assert s == 409 and "new threadId" in msg
    assert "/threads/{threadId}/runs/{activeRunId}/stream" in msg, msg  # the plan's guidance, whole


# --------------------------------------------------------------------------- keys


def make_key(client, tenant: str, **extra: Any) -> tuple[str, str]:
    r = client.post("/api/v1/api-keys", json={"name": "k", **extra}, headers=get_auth_header(tenant, "alice"))
    assert r.status_code == 201, r.text
    return r.json()["id"], r.json()["key"]


def test_keys_workflow_scoped_works_agent_only_and_cross_tenant_get_404(api, tenant):
    client, app = api
    h = get_auth_header(tenant, "alice")
    wf = step_workflow(client, h)
    agent = publish_agent(client, h, ECHO)
    key_id, key = make_key(client, tenant, workflow_ids=[wf])
    _, agent_only = make_key(client, tenant, agent_ids=[agent])
    other = f"o{uuid.uuid4().hex[:8]}"
    app.state.test_tenants.append(other)
    _, foreign = make_key(client, other)
    bearer = lambda k: {"Authorization": f"Bearer {k}"}  # noqa: E731
    thread, run = f"{wf_url(wf)}/threads/th", derive_workflow_run_id(tenant, wf, "th")
    s, d, frames = post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), bearer(key))
    assert s == 200, d
    check(frames)
    (iid,) = interrupt_ids(frames)
    assert iid == f"approval:{run}:appr"
    for bad in (agent_only, foreign):
        for call in (lambda k: post(client, wf_url(wf), body("th2", "r1", input={"q": "hi"}), bearer(k)),
                     lambda k: (client.get(thread, headers=bearer(k)).status_code, None, None),
                     lambda k: (client.get(thread + "/runs/r1/stream", headers=bearer(k)).status_code, None, None)):
            assert call(bad)[0] == 404, bad
        s, d, _ = post(client, wf_url(wf), body("th", "r2", resume=approve([iid])), bearer(bad))
        assert s == 404, d
    assert open_rows(app, tenant) == [("step", "appr")]  # nothing was decided by the refused keys
    s, d, frames = post(client, wf_url(wf), body("th", "r3", resume=approve([iid], reason="by key")), bearer(key))
    assert s == 200, d
    wait_status(app, str(run), {"completed"})
    with app.state.database_engine.connect() as conn:
        decided = conn.execute(text("SELECT data FROM execution_events WHERE run_id = :r AND type = 'step_approved'"),
                               {"r": str(run)}).scalar_one()
    assert decided["decided_by"] == f"apikey:{key_id}" and decided["comment"] == "by key"
    # A key outside the allowlist for a second workflow gets the same 404 as an unknown one.
    wf2 = step_workflow(client, h)
    assert post(client, wf_url(wf2), body("t", "r1", input={"q": "x"}), bearer(key))[0] == 404
    assert post(client, wf_url(str(uuid.uuid4())), body("t", "r1", input={"q": "x"}), bearer(key))[0] == 404


# --------------------------------------------------------------------------- native routes unchanged


def test_native_workflow_routes_are_unchanged_and_rest_approval_shows_on_attach(api, tenant):
    client, app = api
    h = get_auth_header(tenant, "alice")
    wf = step_workflow(client, h)
    run = str(uuid.uuid4())
    r = client.post(f"/api/v1/workflows/{wf}/runs", json={"input": {"q": "hi"}, "run_id": run}, headers=h)
    assert r.status_code == 202, r.text
    assert set(r.json()) == {"run_id", "kind", "workflow_id", "version", "status", "display_status", "trace_id"}
    wait_for(lambda: any(e["type"] == "step_waiting" for e in events_db(app, run)), what="step_waiting")
    a = client.post(f"/api/v1/workflows/runs/{run}/steps/appr/approve", json={"comment": "rest"}, headers=h)
    assert a.status_code == 200 and a.json()["decision"] == "approved" and a.json()["decided_by"] == "alice"
    again = client.post(f"/api/v1/workflows/runs/{run}/steps/appr/approve", headers=h)
    assert again.status_code == 409 and again.json()["detail"] == "step is not waiting for approval"
    wait_status(app, run, {"completed"})
    types_ = [e["type"] for e in events_db(app, run)]
    assert types_[0] == "run_started" and types_[-1] == "run_completed" and types_.count("step_approved") == 1
    got = client.get(f"/api/v1/runs/{run}/events", headers=h).json()
    assert [e["type"] for e in got] == types_
    # The AG-UI client of another thread: REST approval while it waits, then attach shows the continuation.
    s, d, first = post(client, wf_url(wf), body("th", "r1", input={"q": "hi"}), h)
    assert s == 200, d
    ag_run = str(derive_workflow_run_id(tenant, wf, "th"))
    assert interrupt_ids(first)
    assert client.post(f"/api/v1/workflows/runs/{ag_run}/steps/appr/approve", headers=h).status_code == 200
    wait_status(app, ag_run, {"completed"})
    # r1 is closed at its interrupt; the continuation is the resume run (answered late: accepted,
    # the recorded decision wins), on the POST and on a later attach.
    s, d, late = post(client, wf_url(wf), body("th", "r2", resume=approve(interrupt_ids(first), False)), h)
    assert s == 200, d
    check(late)
    t = types(late)
    assert "CUSTOM:pydanticos.approval_decided" in t and (finished(late).get("outcome") or {}).get("type") == "success", t
    decided = next(f.event for f in late if f.event and f.event["type"] == "CUSTOM" and f.event["name"] == "pydanticos.approval_decided")
    assert decided["value"]["decision"] == "approved", decided
    s, d, part = attach(client, f"{wf_url(wf)}/threads/th/runs/r2/stream", h)
    assert s == 200 and same_frames(late, part), d
    assert [e["type"] for e in events_db(app, ag_run)].count("step_approved") == 1
    assert "step_rejected" not in [e["type"] for e in events_db(app, ag_run)]


# --------------------------------------------------------------------------- agent tool approvals


def agent_url(agent_id: str) -> str:
    return f"/api/v1/ag-ui/agents/{agent_id}"


def start_agent(client, app, tenant, thread="t", run="a1"):
    h = get_auth_header(tenant, "alice")
    agent = publish_agent(client, h, GATED)
    s, d, frames = post(client, agent_url(agent), body(thread, run, messages=[user_msg("u1", GATED_PROMPT)]), h)
    assert s == 200, d
    return h, agent, frames


@pytest.mark.parametrize("approved", [True, False])
def test_agent_tool_approval_is_an_interrupt_and_resume_decides_through_decide(api, tenant, approved):
    client, app = api
    h, agent, first = start_agent(client, app, tenant)
    check(first)
    (iid,) = interrupt_ids(first)
    assert ":tool:" in iid
    # Q5: a new message while the call waits re-sends the interrupt; nothing is recorded.
    s, d, again = post(client, agent_url(agent), body("t", "a2", messages=[user_msg("u1", GATED_PROMPT), user_msg("u2")]), h)
    assert s == 200, d
    check(again)
    assert interrupt_ids(again) == [iid]
    assert decision_events(app, all_runs(app, tenant)) == []
    # Unknown id, partial (none) coverage and editedArgs are refused before anything is recorded.
    assert post(client, agent_url(agent), body("t", "a3", resume=approve([f"approval:{uuid.uuid4()}:tool:x"])), h)[0] == 422
    s, d, _ = post(client, agent_url(agent), body("t", "a3", resume=approve([iid], editedArgs={})), h)
    assert (s, err_types(d)[0]) == (422, "edited_args_unsupported")
    assert decision_events(app, all_runs(app, tenant)) == []
    s, d, frames = post(client, agent_url(agent), body("t", "a3", resume=approve([iid], approved, reason="because")), h)
    assert s == 200, d
    check(frames)
    (run_id,) = all_runs(app, tenant)
    wait_status(app, run_id, {"completed"})
    assert decision_events(app, [run_id]) == [("tool_approved" if approved else "tool_denied", f"tool:{iid.rsplit(':', 1)[1]}:approval:decision")]
    result = next(f.event for f in frames if f.event and f.event["type"] == "TOOL_CALL_RESULT")
    if approved:
        assert json.loads(result["content"])["created"] is True
    else:
        assert result["content"] == "The user denied this tool call. Comment: because"
    assert finished(frames)["outcome"]["type"] == "success"
    assert len(all_runs(app, tenant)) == 1  # the same durable run continued; no new turn
