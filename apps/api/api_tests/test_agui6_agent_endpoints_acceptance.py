"""AGUI.6 acceptance: first turns and continuations, M1 dedupe, D7 resume at every offset, the
previous-turn wait, fingerprints, API keys (revocation with time control), isolation, leaks,
and AGUI.4 conformance on every stream. Reuses the implementer's worker fixtures."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

import pytest

from eap_api.ag_ui.conformance import Frame as CFrame, lifecycle_errors, schema_errors
from eap_api.ag_ui.input import derive_agent_run_id
from eap_api.routes.ag_ui import AgUiRouteSettings
from eap_api.app import create_app  # noqa: F401  (fixtures build the app)

from .agui4_harness import SCHEMA
from .conftest import get_auth_header
from .run_helpers import iter_frames
from .test_agui6_agent_endpoints import (  # noqa: F401  (fixtures and helpers)
    ScriptedWorker, _key, _no_hang, ag, agui_settings, body, complete, post, pytestmark, runs_of,
    tenant, types, user, worker,
)
from .test_runs import make_agent

BASE = "/api/v1/ag-ui/agents"


def check(frames, *, continuation: bool = False, run_id: str | None = None) -> None:
    """AGUI.4 conformance: schema + lifecycle of the data frames of one stream."""
    cframes = [CFrame(f.data, f.id) for f in frames if f.data is not None]
    assert schema_errors([f.event for f in cframes], SCHEMA) == []
    assert lifecycle_errors(cframes, continuation=continuation, run_id=run_id) == []


def ok(client, agent_id, payload, headers, *, continuation=False, run_id=None):
    status, detail, frames = post(client, agent_id, payload, headers)
    assert status == 200, detail
    check(frames, continuation=continuation, run_id=run_id or payload["runId"])
    return frames


def attach(client, agent_id, thread, run, headers, *, params: str = ""):
    url = f"{BASE}/{agent_id}/threads/{thread}/runs/{run}/stream{params}"
    with client.stream("GET", url, headers=headers) as r:
        if r.status_code != 200:
            r.read()
            return r.status_code, r.json(), []
        return 200, None, list(iter_frames(r, deadline_s=30))


def snapshot(client, agent_id, thread, headers) -> dict[str, Any]:
    r = client.get(f"{BASE}/{agent_id}/threads/{thread}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def assistant(mid: str, text: str) -> dict[str, Any]:
    return {"id": mid, "role": "assistant", "content": text}


def setup(ag, tenant, user_id="alice"):
    client, app = ag
    h = get_auth_header(tenant, user_id)
    return client, app, h, make_agent(client, h)


# --------------------------------------------------------------------------- first turns, M1


def test_first_turn_then_continuation_with_full_history(ag, tenant, worker):
    client, app, h, agent = setup(ag, tenant)
    ok(client, agent, body("t", "r1", user("u1", "hello")), h)
    frames = ok(client, agent, body("t", "r2", user("u1", "hello"), assistant("r1-m0", "echo: hello"),
                                    user("u2", "more")), h)
    assert types(frames)[-1] == "RUN_FINISHED"
    assert [r["input"] for r in runs_of(app, tenant)] == ["hello", "more"]
    assert [c[1] for c in worker.calls] == ["hello", "more"]
    snap = snapshot(client, agent, "t", h)
    assert [m["id"] for m in snap["messages"]] == ["u1", "r1-m0", "u2", "r2-m0"]
    assert [r["runId"] for r in snap["runs"]] == ["r1", "r2"] and snap["activeRunId"] is None


def test_m1_lost_stream_then_repost_with_new_run_id_has_no_duplicate_user_message(ag, tenant, worker):
    client, app, h, agent = setup(ag, tenant)
    with client.stream("POST", f"{BASE}/{agent}", json=body("t", "r1", user("u1", "hello")), headers=h) as r:
        assert r.status_code == 200  # the client lost the stream after RUN_STARTED
    # The client never saw the answer, so its history still lacks the assistant message.
    ok(client, agent, body("t", "r2", user("u1", "hello"), user("u2", "again")), h)
    assert [r["input"] for r in runs_of(app, tenant)] == ["hello", "again"]
    ids = [m["id"] for m in snapshot(client, agent, "t", h)["messages"]]
    assert ids == ["u1", "r1-m0", "u2", "r2-m0"] and len(ids) == len(set(ids))


def test_m1_lost_stream_then_same_run_id_repost_is_one_run(ag, tenant, worker):
    client, app, h, agent = setup(ag, tenant)
    payload = body("t", "r1", user("u1", "hello"))
    with client.stream("POST", f"{BASE}/{agent}", json=payload, headers=h) as r:
        assert r.status_code == 200
    ok(client, agent, payload, h)
    assert [r["input"] for r in runs_of(app, tenant)] == ["hello"]
    ids = [m["id"] for m in snapshot(client, agent, "t", h)["messages"]]
    assert ids == ["u1", "r1-m0"]


def test_m1_regenerate_keeps_a_single_user_message_id_in_the_thread(ag, tenant):
    client, app, h, agent = setup(ag, tenant)
    ok(client, agent, body("t", "r1", user("u1", "hello")), h)
    ok(client, agent, body("t", "r2", user("u1", "hello")), h)  # regenerate: same tail, new runId
    assert [r["input"] for r in runs_of(app, tenant)] == ["hello", "hello"]
    ids = [m["id"] for m in snapshot(client, agent, "t", h)["messages"]]
    users = [i for i in ids if i.startswith("u")]
    assert len(users) == len(set(users)), ids  # AG-UI message ids are unique
    assert ids.count("u1") == 1


def test_m1_lost_stream_then_regenerate_after_a_two_message_tail(ag, tenant):
    client, app, h, agent = setup(ag, tenant)
    ok(client, agent, body("t", "r1", user("u1", "a")), h)
    ok(client, agent, body("t", "r2", user("u1", "a"), assistant("r1-m0", "echo: a"), user("u2", "b")), h)
    # regenerate turn 2 after a lost stream: the client resends [u1, a, u2] under a new runId
    ok(client, agent, body("t", "r3", user("u1", "a"), assistant("r1-m0", "echo: a"), user("u2", "b")), h)
    assert [r["input"] for r in runs_of(app, tenant)] == ["a", "b", "b"]
    ids = [m["id"] for m in snapshot(client, agent, "t", h)["messages"] if m["role"] == "user"]
    assert len(ids) == len(set(ids)), ids


# --------------------------------------------------------------------------- D7 resume


def test_resume_at_every_offset_is_a_suffix_of_the_full_stream(ag, tenant):
    client, app, h, agent = setup(ag, tenant)
    payload = body("t", "r1", user("u1", "hello"))
    full = ok(client, agent, payload, h)
    full_ids = [f.id for f in full if f.id]
    assert full_ids == ["r1:0", "r1:1", "r1:2"] or full_ids[-1] == "r1:2"
    for n in range(-1, 3):
        frames = ok(client, agent, payload, {**h, "Last-Event-ID": f"r1:{n}"}, continuation=True)
        t = types(frames)
        assert "RUN_STARTED" not in t and t[:2] == ["CUSTOM:pydanticos.run", "MESSAGES_SNAPSHOT"], (n, t)
        seqs = [int(f.id.rsplit(":", 1)[1]) for f in frames if f.id]
        # frames after N, then the terminal event (re-sent when N is already the terminal seq)
        assert seqs == [s for s in (1, 2) if s > n or s == 2]  # seq 0 has no frame, (n, seqs)
        assert t[-1] == "RUN_FINISHED"
        # GET attach with the header and with ?after behaves the same
        for kw in ({"headers": {**h, "Last-Event-ID": f"r1:{n}"}}, {"headers": h, "params": f"?after={n}"}):
            s, _, got = attach(client, agent, "t", "r1", kw["headers"], params=kw.get("params", ""))
            assert s == 200 and types(got) == t, (n, kw)
            check(got, continuation=True, run_id="r1")


def test_cursor_past_end_and_malformed_cursors_are_400_on_post_and_get(ag, tenant):
    client, app, h, agent = setup(ag, tenant)
    payload = body("t", "r1", user("u1", "hello"))
    ok(client, agent, payload, h)
    want = {"detail": "Last-Event-ID must be <runId>:<seq> for this run"}
    for bad in ("r1:3", "r1:99", "r1:-2", "r1:abc", "r1", "other:0", ":0", "r1:", "r1:1:2"):
        s, d, _ = post(client, agent, payload, {**h, "Last-Event-ID": bad})
        assert (s, d) == (400, want), bad
        s, d, _ = attach(client, agent, "t", "r1", {**h, "Last-Event-ID": bad})
        assert (s, d) == (400, want), bad
    for bad in ("3", "-2"):
        s, d, _ = attach(client, agent, "t", "r1", h, params=f"?after={bad}")
        assert s == 400, bad
    assert len(runs_of(app, tenant)) == 1


def test_attach_header_wins_over_after_param_and_unknown_run_is_404(ag, tenant):
    client, app, h, agent = setup(ag, tenant)
    ok(client, agent, body("t", "r1", user("u1", "hello")), h)
    s, _, a = attach(client, agent, "t", "r1", {**h, "Last-Event-ID": "r1:2"}, params="?after=-1")
    assert s == 200
    s, _, b = attach(client, agent, "t", "r1", h, params="?after=2")
    assert types(a) == types(b)
    assert attach(client, agent, "t", "nope", h)[:2] == (404, {"detail": "Unknown runId"})
    assert attach(client, agent, "other-thread", "r1", h)[0] == 404
    assert len(runs_of(app, tenant)) == 1


# --------------------------------------------------------------------------- previous-turn wait


def test_second_post_while_first_runs_waits_with_keepalives_then_runs(ag, tenant, worker):
    client, app, h, agent = setup(ag, tenant)
    worker.hold = True
    with client.stream("POST", f"{BASE}/{agent}", json=body("t", "r1", user("u1", "first")), headers=h) as r:
        assert r.status_code == 200
    first = derive_agent_run_id(tenant, agent, "t", "r1")
    worker.hold = False
    threading.Timer(0.6, complete, args=(app, tenant, first, "first answer")).start()
    status, _, frames = post(client, agent, body("t", "r2", user("u1", "first"), user("u2", "second")), h)
    assert status == 200
    check(frames, run_id="r2")
    t = types(frames)
    assert t[:2] == ["RUN_STARTED", "CUSTOM:pydanticos.waiting"] and t.count("RUN_STARTED") == 1
    assert frames[1].data["value"] == {"run_id": first}
    assert any("keep-alive" in c for f in frames for c in f.comments)
    assert t.index("CUSTOM:pydanticos.run") < t.index("MESSAGES_SNAPSHOT") and t[-1] == "RUN_FINISHED"
    assert [r["input"] for r in runs_of(app, tenant)] == ["first", "second"]
    assert snapshot(client, agent, "t", h)["activeRunId"] is None


def test_wait_timeout_then_resend_after_the_first_finishes(ag, tenant, worker, agui_settings):
    agui_settings[0] = AgUiRouteSettings(previous_turn_wait_s=0.3)
    client, app, h, agent = setup(ag, tenant)
    worker.hold = True
    with client.stream("POST", f"{BASE}/{agent}", json=body("t", "r1", user("u1", "a")), headers=h):
        pass
    status, _, frames = post(client, agent, body("t", "r2", user("u2", "b")), h)
    assert status == 200 and frames[-1].data["code"] == "previous_turn_running"
    check(frames, run_id="r2")
    assert len(runs_of(app, tenant)) == 1  # nothing recorded for the refused turn
    complete(app, tenant, derive_agent_run_id(tenant, agent, "t", "r1"), "a answer")
    worker.hold = False
    ok(client, agent, body("t", "r2", user("u2", "b")), h)
    assert [r["input"] for r in runs_of(app, tenant)] == ["a", "b"]


def test_client_disconnect_does_not_cancel_the_run(ag, tenant, worker):
    client, app, h, agent = setup(ag, tenant)
    worker.hold = True
    with client.stream("POST", f"{BASE}/{agent}", json=body("t", "r1", user("u1", "a")), headers=h) as r:
        next(r.iter_lines())  # read a frame, then drop the connection
    time.sleep(0.3)
    (row,) = runs_of(app, tenant)
    from eap_run_store import RunStore
    run = RunStore(app.state.database_engine).get_run(tenant, row["id"])
    assert run["status"] not in ("cancelled", "canceled", "failed")
    assert snapshot(client, agent, "t", h)["activeRunId"] == "r1"


# --------------------------------------------------------------------------- fingerprints


def test_same_run_id_different_body_is_refused_and_same_body_is_idempotent(ag, tenant, worker):
    client, app, h, agent = setup(ag, tenant)
    payload = body("t", "r1", user("u1", "hello"))
    for _ in range(3):
        ok(client, agent, payload, h)
    assert len(runs_of(app, tenant)) == 1
    want = {"detail": "runId was already used with a different input"}
    tool = [{"name": "pick", "description": "d", "parameters": {"type": "object", "properties": {}}}]
    variants = [
        body("t", "r1", user("u1", "hello!")),
        body("t", "r1", user("u1", "hello"), tools=tool),
        body("t", "r1", user("u1", "hello"), forwardedProps={"pydanticos": {"resourceId": "x"}}),
    ]
    for v in variants:
        s, d, _ = post(client, agent, v, h)
        assert (s, d) == (409, want), v
    assert len(runs_of(app, tenant)) == 1 and len(worker.calls) == 3
    # By design (D4) ids and context are not fingerprinted: the same input attaches again.
    ok(client, agent, body("t", "r1", user("u9", "hello"), context=[{"description": "d", "value": "v"}]), h)
    assert len(runs_of(app, tenant)) == 1
    ok(client, agent, body("t2", "r1", user("u1", "hello")), h)  # same runId, other thread: a new run
    assert len(runs_of(app, tenant)) == 2


# --------------------------------------------------------------------------- keys


def test_agent_only_key_and_dev_bearer(ag, tenant):
    client, app, h, agent = setup(ag, tenant)
    other = make_agent(client, h)
    _, key = _key(client, tenant, agent_ids=[agent])
    kh = {"Authorization": f"Bearer {key}"}
    ok(client, agent, body("t", "r1", user("u1", "hi")), kh)
    for target in (other, str(uuid.uuid4())):
        s, d, _ = post(client, target, body("t", "r1", user("u1", "hi")), kh)
        assert (s, d) == (404, {"detail": "Agent not found"})
    for url in (f"{BASE}/{other}/threads/t", f"{BASE}/{other}/threads/t/runs/r1/stream"):
        assert client.get(url, headers=kh).status_code == 404
    assert client.get(f"{BASE}/{agent}/threads/t", headers=kh).status_code == 200
    assert attach(client, agent, "t", "r1", kh)[0] == 200
    ok(client, other, body("t", "r1", user("u1", "hi")), h)  # the dev bearer reaches both
    assert client.post(f"{BASE}/{agent}", json={}, headers={"Authorization": "Bearer pos_nope"}).status_code == 401
    assert client.post(f"{BASE}/{agent}", json={}).status_code == 401


def test_revoked_key_is_refused_at_the_next_request_on_every_route(ag, tenant):
    client, app, h, agent = setup(ag, tenant)
    key_id, key = _key(client, tenant)
    kh = {"Authorization": f"Bearer {key}"}
    ok(client, agent, body("t", "r1", user("u1", "hi")), kh)
    assert client.delete(f"/api/v1/api-keys/{key_id}", headers=h).status_code == 204
    assert post(client, agent, body("t", "r2", user("u1", "hi")), kh)[0] == 401
    assert client.get(f"{BASE}/{agent}/threads/t", headers=kh).status_code == 401
    assert attach(client, agent, "t", "r1", kh)[0] == 401
    assert len(runs_of(app, tenant)) == 1


def test_revoked_key_ends_an_open_stream_within_the_interval_with_time_control(ag, tenant, worker, monkeypatch):
    client, app, h, agent = setup(ag, tenant)  # default key_recheck_s = 60
    key_id, key = _key(client, tenant)
    kh = {"Authorization": f"Bearer {key}"}
    worker.hold = True
    real, jump = time.monotonic, {"by": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: real() + jump["by"])
    seen = []
    with client.stream("POST", f"{BASE}/{agent}", json=body("t", "r1", user("u1", "a")), headers=kh) as r:
        assert r.status_code == 200
        for frame in iter_frames(r, deadline_s=600):
            seen.append(frame)
            if len(seen) == 3:
                assert client.delete(f"/api/v1/api-keys/{key_id}", headers=h).status_code == 204
            if len(seen) == 6:
                assert all(f.data is None or f.data["type"] != "RUN_ERROR" for f in seen)  # still streaming inside the 60 s window
                jump["by"] = 61.0
    last = [f for f in seen if f.data is not None][-1].data
    assert last["type"] == "RUN_ERROR" and last["code"] == "unauthorized"
    check(seen, run_id="r1")
    monkeypatch.undo()
    from eap_run_store import RunStore
    (row,) = runs_of(app, tenant)
    assert RunStore(app.state.database_engine).get_run(tenant, row["id"])["status"] not in ("failed", "cancelled")


# --------------------------------------------------------------------------- isolation


def test_cross_tenant_requests_are_404_on_every_route_even_for_known_ids(ag, tenant, worker):
    client, app, h, agent = setup(ag, tenant)
    ok(client, agent, body("t", "r1", user("u1", "hello")), h)
    other = f"t{uuid.uuid4().hex[:10]}"
    app.state.test_tenants.append(other)
    oh = get_auth_header(other, "bob")
    _, okey = _key(client, other)
    for headers in (oh, {"Authorization": f"Bearer {okey}"}):
        s, d, _ = post(client, agent, body("t", "r1", user("u1", "hello")), headers)
        assert (s, d) == (404, {"detail": "Agent not found"})
        s, d, _ = post(client, agent, body("t", "r1", user("u1", "x")), {**headers, "Last-Event-ID": "r1:0"})
        assert s == 404 and d == {"detail": "Agent not found"}
        assert client.get(f"{BASE}/{agent}/threads/t", headers=headers).json() == {"detail": "Agent not found"}
        s, d, _ = attach(client, agent, "t", "r1", headers)
        assert (s, d) == (404, {"detail": "Agent not found"})
    assert len(runs_of(app, tenant)) == 1 and len(worker.calls) == 1
    # Another tenant's key never sees this tenant's thread through its own agent either.
    mine = make_agent(client, oh)
    assert snapshot(client, mine, "t", oh)["messages"] == []
    assert attach(client, mine, "t", "r1", oh)[:2] == (404, {"detail": "Unknown runId"})
    ok(client, mine, body("t", "r1", user("u1", "bob's")), oh)  # same ids, independent run
    assert [m["content"] for m in snapshot(client, agent, "t", h)["messages"]][0] == "hello"


# --------------------------------------------------------------------------- leaks

SECRET = "SECRET-CONTENT-7c1e9"


def test_no_secret_content_or_key_in_errors_or_logs(ag, tenant, worker, caplog, agui_settings):
    caplog.set_level(logging.DEBUG)
    agui_settings[0] = AgUiRouteSettings(previous_turn_wait_s=0.2)
    client, app, h, agent = setup(ag, tenant)
    key_id, key = _key(client, tenant)
    kh = {"Authorization": f"Bearer {key}"}
    texts: list[str] = []
    ok(client, agent, body("t", "r1", user("u1", SECRET)), kh)
    for payload in (body("t", "r1", user("u1", SECRET + "x")), body("t", "r9", user("u1", "a\u0000" + SECRET)),
                    body("t", "r8", user("u1", SECRET), {"id": "tm", "role": "tool", "toolCallId": SECRET,
                                                         "content": SECRET}),
                    body("t", "r7", user("u1", SECRET), forwardedProps={"pydanticos": {"model": {"name": SECRET}}})):
        s, d, _ = post(client, agent, payload, kh)
        assert s >= 400
        texts.append(str(d))
    for raw in (('{"threadId": "t", "runId": "q", "messages": [' + SECRET).encode(), b"\xff\xfe" + SECRET.encode()):
        r = client.post(f"{BASE}/{agent}", content=raw, headers={**kh, "Content-Type": "application/json"})
        assert r.status_code >= 400
        texts.append(r.text)
    worker.fail_times = 1
    s, d, _ = post(client, agent, body("t", "r2", user("u2", SECRET)), kh)
    assert s == 503
    texts.append(str(d))
    worker.hold = True
    with client.stream("POST", f"{BASE}/{agent}", json=body("t", "r3", user("u3", "z")), headers=kh):
        pass
    s, _, frames = post(client, agent, body("t", "r4", user("u4", SECRET)), kh)
    texts.append(" ".join(str(f.data) for f in frames))
    assert client.delete(f"/api/v1/api-keys/{key_id}", headers=h).status_code == 204
    texts.append(client.post(f"{BASE}/{agent}", json=body("t", "r5", user("u5", SECRET)), headers=kh).text)
    blob = "\n".join(texts) + "\n" + caplog.text
    assert SECRET not in blob and key not in blob and key.split("_", 1)[-1] not in blob
