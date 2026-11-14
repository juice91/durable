"""AC.8 acceptance tests: the approvals inbox and decisions beyond the implementer's tests.

Isolation, a concurrent approve+deny race, cursor paging with ties, filters, the decided list,
child/root linking, ended runs, workflow step rows and failed notifies.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import text

from .run_helpers import requires_pg
from .test_approvals_api import (  # noqa: F401 - fixtures
    _no_hang,
    add_tool_wait,
    api,
    fake,
    tool_url,
    waiting_step,
    waiting_tool_call,
)

pytestmark = requires_pg


def _items(client, headers, query=""):
    r = client.get(f"/api/v1/approvals?{query}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _set_times(app, tenant, run_id, key, requested_at=None, decided_at=None):
    with app.state.database_engine.begin() as conn:
        if requested_at:
            conn.execute(text("UPDATE pending_approvals SET requested_at = :t WHERE tenant_id=:n AND run_id=:r"
                              " AND approval_key=:k"), {"t": requested_at, "n": tenant, "r": str(run_id), "k": key})
        if decided_at:
            conn.execute(text("UPDATE pending_approvals SET decided_at = :t WHERE tenant_id=:n AND run_id=:r"
                              " AND approval_key=:k"), {"t": decided_at, "n": tenant, "r": str(run_id), "k": key})


# ------------------------------------------------------------------------------ isolation


def test_tenants_see_only_their_own_inbox_and_cannot_decide_each_others(api, fake):
    client, app, tenant_of = api
    ta, ha = tenant_of("alice")
    tb, hb = tenant_of("bob")
    run_a, _ = waiting_tool_call(client, app, ta, ha, call_id="call_a")
    run_b, _ = waiting_tool_call(client, app, tb, hb, call_id="call_b")
    assert [i["run_id"] for i in _items(client, ha)] == [str(run_a)]
    assert [i["run_id"] for i in _items(client, hb)] == [str(run_b)]
    for action in ("approve", "deny"):
        r = client.post(tool_url(run_a, "call_a", action), headers=hb)
        assert r.status_code == 404 and r.json()["detail"] == "Run not found"
    assert fake.sent == []
    assert [i["status"] for i in _items(client, ha)] == ["pending"]
    assert _items(client, hb, "status=decided") == []
    # A decided row of A never shows in B's decided list either.
    assert client.post(tool_url(run_a, "call_a", "approve"), headers=ha).status_code == 200
    assert _items(client, hb, "status=decided") == []
    assert len(_items(client, ha, "status=decided")) == 1


# ------------------------------------------------------------------------------ races


def test_concurrent_approve_and_deny_exactly_one_wins(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    for round_no in range(5):
        call = f"call_race{round_no}"
        run_id, _ = waiting_tool_call(client, app, tenant, headers, call_id=call)
        barrier = threading.Barrier(2)
        out: dict[str, object] = {}

        def go(action: str) -> None:
            barrier.wait()
            out[action] = client.post(tool_url(run_id, call, action), headers=headers)

        threads = [threading.Thread(target=go, args=(a,)) for a in ("approve", "deny")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        codes = sorted(r.status_code for r in out.values())
        assert codes == [200, 409], codes
        winner = next(a for a, r in out.items() if r.status_code == 200)
        loser = next(a for a, r in out.items() if r.status_code == 409)
        assert out[loser].json()["detail"] == "tool call is already decided"
        _, event_log = app.state.run_stores
        event = event_log.get_event(tenant, run_id, f"tool:{call}:approval:decision")
        assert event.type == ("tool_approved" if winner == "approve" else "tool_denied")
        sent = [m for m in fake.sent if m[1]["tool_call_id"] == call]
        assert len(sent) == 1 and sent[0][1]["decision"] == ("approved" if winner == "approve" else "denied")
        (row,) = [i for i in _items(client, headers, "status=decided") if i["run_id"] == str(run_id)]
        assert row["decision"] == ("approved" if winner == "approve" else "denied")


# ------------------------------------------------------------------------------ paging, filters


def test_cursor_paging_is_stable_with_equal_timestamps(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    runs = [waiting_tool_call(client, app, tenant, headers, call_id=f"call_{i}")[0] for i in range(5)]
    same = "2030-01-01T00:00:00+00:00"
    for i, run in enumerate(runs):
        _set_times(app, tenant, run, f"call_{i}", requested_at=same)
    full = [(i["run_id"], i["approval_key"]) for i in _items(client, headers, "limit=200")]
    assert len(full) == 5 and len(set(full)) == 5
    seen: list[tuple[str, str]] = []
    cursor = None
    for _ in range(10):
        q = "limit=2" + (f"&cursor={cursor}" if cursor else "")
        body = client.get(f"/api/v1/approvals?{q}", headers=headers).json()
        assert len(body["items"]) <= 2
        seen += [(i["run_id"], i["approval_key"]) for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert seen == full  # no row twice, none skipped, same order as one big page
    # A row added mid-paging (newest) does not disturb the remaining pages.
    first = client.get("/api/v1/approvals?limit=2", headers=headers).json()
    new_run, _ = waiting_tool_call(client, app, tenant, headers, call_id="call_new")
    _set_times(app, tenant, new_run, "call_new", requested_at="2031-01-01T00:00:00+00:00")
    rest = client.get(f"/api/v1/approvals?limit=200&cursor={first['next_cursor']}", headers=headers).json()
    assert [(i["run_id"], i["approval_key"]) for i in rest["items"]] == full[2:]


def test_decided_list_is_newest_decision_first_and_paged(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    runs = [waiting_tool_call(client, app, tenant, headers, call_id=f"call_{i}")[0] for i in range(3)]
    for i, run in enumerate(runs):
        action = "deny" if i == 1 else "approve"
        assert client.post(tool_url(run, f"call_{i}", action), headers=headers).status_code == 200
    # Force a known decision order: call_1 newest, then call_2, then call_0.
    for key, run, at in (("call_0", runs[0], "2031-01-01T00:00:00+00:00"),
                         ("call_2", runs[2], "2031-01-02T00:00:00+00:00"),
                         ("call_1", runs[1], "2031-01-03T00:00:00+00:00")):
        _set_times(app, tenant, run, key, decided_at=at)
    decided = _items(client, headers, "status=decided")
    assert [(i["approval_key"], i["decision"]) for i in decided] == [
        ("call_1", "denied"), ("call_2", "approved"), ("call_0", "approved")]
    assert all(i["status"] == "decided" and i["decided_by"] == "alice" for i in decided)
    assert _items(client, headers) == []  # nothing pending
    p1 = client.get("/api/v1/approvals?status=decided&limit=2", headers=headers).json()
    p2 = client.get(f"/api/v1/approvals?status=decided&limit=2&cursor={p1['next_cursor']}", headers=headers).json()
    assert [i["approval_key"] for i in p1["items"] + p2["items"]] == ["call_1", "call_2", "call_0"]
    assert p2["next_cursor"] is None
    # The kind filter applies to the decided list too.
    assert _items(client, headers, "status=decided&kind=step") == []

    # A cursor names its list: a decided token is a 422 on the pending list, and a pending token
    # a 422 on the decided list (their timestamps mean different things).
    r = client.get(f"/api/v1/approvals?cursor={p1['next_cursor']}", headers=headers)
    assert r.status_code == 422 and "cursor" in r.json()["detail"]
    for i in (3, 4):
        waiting_tool_call(client, app, tenant, headers, call_id=f"call_{i}")
    pending = client.get("/api/v1/approvals?limit=1", headers=headers).json()
    assert len(pending["items"]) == 1 and pending["next_cursor"]
    r = client.get(f"/api/v1/approvals?status=decided&cursor={pending['next_cursor']}", headers=headers)
    assert r.status_code == 422 and "cursor" in r.json()["detail"]
    # Each token still works on its own list.
    assert client.get(f"/api/v1/approvals?cursor={pending['next_cursor']}", headers=headers).status_code == 200


def test_pending_is_newest_request_first_and_filters_combine(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    r1, agent1 = waiting_tool_call(client, app, tenant, headers, call_id="c1", tool_name="t1")
    r2, agent2 = waiting_tool_call(client, app, tenant, headers, call_id="c2", tool_name="t2")
    step = waiting_step(client, app, tenant, headers)
    _set_times(app, tenant, r1, "c1", requested_at="2030-01-01T00:00:00+00:00")
    _set_times(app, tenant, r2, "c2", requested_at="2030-01-03T00:00:00+00:00")
    _set_times(app, tenant, step, "approve", requested_at="2030-01-02T00:00:00+00:00")
    assert [i["approval_key"] for i in _items(client, headers)] == ["c2", "approve", "c1"]
    assert [i["approval_key"] for i in _items(client, headers, "kind=tool")] == ["c2", "c1"]
    assert [i["approval_key"] for i in _items(client, headers, f"agent_id={agent1}")] == ["c1"]
    assert [i["approval_key"] for i in _items(client, headers, f"agent_id={agent2}&kind=tool")] == ["c2"]
    assert _items(client, headers, f"agent_id={agent2}&kind=step") == []
    assert [i["approval_key"] for i in _items(client, headers, "status=pending&kind=step")] == ["approve"]
    assert len(_items(client, headers, "limit=200")) == 3


# ------------------------------------------------------------------------------ linking


def test_child_agent_run_of_a_workflow_links_to_its_root_and_wakes_its_own_workflow(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    wf_run = waiting_step(client, app, tenant, headers)
    _, agent_id = waiting_tool_call(client, app, tenant, headers, call_id="unrelated")
    run_store, event_log = app.state.run_stores
    child = uuid.uuid4()
    run_store.create_child_agent_run(tenant, agent_id, 1, "x", child, wf_run, "ask")
    assert run_store.mark_running(tenant, child)
    child_wf = f"{wf_run}:ask"
    event_log.append_approval_waiting(
        tenant, child, "tool", "call_c",
        {"tool_call_id": "call_c", "tool_name": "send_email", "args": {}, "deadline": "2099-01-01T00:00:00+00:00",
         "workflow_id": child_wf},
    )
    item = next(i for i in _items(client, headers) if i["run_id"] == str(child))
    assert item["root_run_id"] == str(wf_run) and item["run_id"] != item["root_run_id"]
    step = next(i for i in _items(client, headers) if i["kind"] == "step")
    assert step["root_run_id"] == str(wf_run) and item["workflow_id"] == step["workflow_id"]
    assert item["agent_id"] == agent_id
    r = client.post(tool_url(child, "call_c", "approve"), headers=headers)
    assert r.status_code == 200 and r.json()["notified"] is True
    # The send goes to the workflow named in the waiting event, with the topic of the child run.
    assert fake.sent == [(child_wf, {"tool_call_id": "call_c", "decision": "approved"}, f"tool_approval:{child}")]
    # The step row of the root run is untouched.
    assert [i["kind"] for i in _items(client, headers) if i["run_id"] == str(wf_run)] == ["step"]


def test_wake_up_defaults_to_the_run_id_without_a_workflow_id(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers, call_id="call_1")
    _, event_log = app.state.run_stores
    event_log.append_approval_waiting(
        tenant, run_id, "tool", "call_nowf",
        {"tool_call_id": "call_nowf", "tool_name": "t", "args": {}, "deadline": "2099-01-01T00:00:00+00:00"})
    assert client.post(tool_url(run_id, "call_nowf", "deny"), headers=headers).status_code == 200
    assert fake.sent == [(str(run_id), {"tool_call_id": "call_nowf", "decision": "denied"},
                          f"tool_approval:{run_id}")]


# ------------------------------------------------------------------------------ ended runs


@pytest.mark.parametrize("terminal", ["failed", "completed", "needs_attention"])
def test_terminal_run_is_hidden_from_pending_and_cannot_be_decided(api, fake, terminal):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers)
    live, _ = waiting_tool_call(client, app, tenant, headers, call_id="call_live")
    with app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = :s WHERE id = :r"), {"s": terminal, "r": str(run_id)})
    assert [i["run_id"] for i in _items(client, headers)] == [str(live)]
    assert _items(client, headers, "status=decided") == []
    for action in ("approve", "deny"):
        r = client.post(tool_url(run_id, "call_1", action), headers=headers)
        assert r.status_code == 409 and r.json()["detail"] == "tool call is not waiting for approval"
    assert fake.sent == []


# ------------------------------------------------------------------------------ steps in the inbox


def test_step_decided_through_the_workflow_route_updates_its_inbox_row(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of("carol")
    run_id = waiting_step(client, app, tenant, headers)
    (row,) = _items(client, headers)
    assert (row["kind"], row["step_id"], row["status"], row["decision"], row["run_status"]) == (
        "step", "approve", "pending", None, "running")
    r = client.post(f"/api/v1/workflows/runs/{run_id}/steps/approve/approve", json={"comment": "go"}, headers=headers)
    assert r.status_code == 200
    assert _items(client, headers) == []
    (done,) = _items(client, headers, "status=decided")
    assert (done["kind"], done["decision"], done["decided_by"], done["status"]) == ("step", "approved", "carol", "decided")
    assert done["decided_at"] and done["prompt"] == "OK?"
    # The workflow route stays closed afterwards, and a rejected step reads as denied.
    assert client.post(f"/api/v1/workflows/runs/{run_id}/steps/approve/reject", headers=headers).status_code == 409
    run2 = waiting_step(client, app, tenant, headers)
    assert client.post(f"/api/v1/workflows/runs/{run2}/steps/approve/reject", headers=headers).status_code == 200
    rows = {i["run_id"]: i for i in _items(client, headers, "status=decided&kind=step")}
    assert rows[str(run2)]["decision"] == "denied" and rows[str(run_id)]["decision"] == "approved"


def test_failed_notify_on_a_step_still_closes_the_row(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id = waiting_step(client, app, tenant, headers)
    fake.fail_send = True
    r = client.post(f"/api/v1/workflows/runs/{run_id}/steps/approve/approve", headers=headers)
    assert r.status_code == 200 and r.json()["notified"] is False
    assert [i["decision"] for i in _items(client, headers, "status=decided")] == ["approved"]


def test_failed_notify_on_a_tool_call_keeps_the_decided_row_and_event(api, fake):
    client, app, tenant_of = api
    tenant, headers = tenant_of()
    run_id, _ = waiting_tool_call(client, app, tenant, headers)
    fake.fail_send = True
    r = client.post(tool_url(run_id, "call_1", "deny"), json={"comment": "no"}, headers=headers)
    assert r.status_code == 200 and r.json()["notified"] is False and r.json()["decision"] == "denied"
    (row,) = _items(client, headers, "status=decided")
    assert (row["decision"], row["decided_by"]) == ("denied", "alice")
    _, event_log = app.state.run_stores
    ev = event_log.get_event(tenant, run_id, "tool:call_1:approval:decision")
    assert ev.type == "tool_denied" and ev.data["comment"] == "no"
    fake.fail_send = False
    assert client.post(tool_url(run_id, "call_1", "approve"), headers=headers).status_code == 409
    assert fake.sent == []
