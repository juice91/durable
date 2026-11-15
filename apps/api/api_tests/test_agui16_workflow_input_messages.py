"""AGUI.16 follow-up: a workflow thread's input user messages in its snapshots and thread GET.

The user messages the thread's first AG-UI run carried (`messages[]`, not the
`forwardedProps.pydanticos.input` object) are stored on its starter row
(`ag_ui_runs.input_messages`) and prefix every MESSAGES_SNAPSHOT of the thread, exactly once, so
`@ag-ui/client`'s merge (which drops held messages a snapshot lacks) keeps them. D7 unchanged:
live, attach (from the start and as a continuation), re-POST and the thread GET agree.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import text

from eap_api.routes.ag_ui_workflows import derive_workflow_run_id
from eap_api.ag_ui.conformance import parse_frames

from .conftest import get_auth_header
from .test_agui8_workflow_endpoints import (  # noqa: F401  (fixtures used by name)
    _no_hang,
    ag,
    body,
    check,
    finished,
    post,
    publish,
    script,
    tenant,
    types,
    wf_url,
)

INPUT_TEXT = '{"q":  "refund order 42"}'  # as the user typed it (two spaces): stored verbatim
USER = {"id": "u-input", "role": "user", "content": INPUT_TEXT}


def snapshots(frames) -> list[list[dict[str, Any]]]:
    return [f.event["messages"] for f in frames if f.event and f.event["type"] == "MESSAGES_SNAPSHOT"]


def assert_once_first(messages: list[dict[str, Any]], user: dict[str, Any] = USER) -> None:
    ids = [m["id"] for m in messages]
    assert ids.count(user["id"]) == 1, ids
    assert messages[0] == {"id": user["id"], "role": "user", "content": user["content"]}
    assert len(ids) == len(set(ids)), ids


def attach(client, url: str, headers: dict[str, str], extra: dict[str, str] | None = None):
    with client.stream("GET", url, headers={**headers, **(extra or {})}) as r:
        assert r.status_code == 200, r.read()
        return parse_frames([r.read().decode()])


def starter_rows(app, tenant: str) -> list[tuple[str, int, Any]]:
    with app.state.database_engine.connect() as conn:
        return [tuple(r) for r in conn.execute(text(
            "SELECT external_run_id, from_seq, input_messages FROM ag_ui_runs WHERE tenant_id = :t "
            "ORDER BY from_seq, created_at"), {"t": tenant})]


def test_input_message_is_in_every_snapshot_and_the_thread_get_once(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    url = wf_url(wf)
    first_body = body("th", "r1", messages=[USER])  # the input is the last user message, as JSON

    # Live, first AG-UI run: the client holds its own message; no snapshot repeats it.
    status, detail, first = post(client, url, first_body, h)
    assert status == 200, detail
    check(first)
    assert snapshots(first) == []
    assert finished(first)["outcome"]["type"] == "interrupt"
    iid = finished(first)["outcome"]["interrupts"][0]["id"]
    waiting_seq = int(first[-1].id.rsplit(":", 1)[1])

    # Stored on the starter row only, verbatim.
    assert starter_rows(app, tenant) == [("r1", -1, [{"id": "u-input", "content": INPUT_TEXT}])]

    # The thread GET: the message first, once.
    thread = client.get(f"{url}/threads/th", headers=h).json()
    assert_once_first(thread["messages"])
    assert [m["id"] for m in thread["messages"]] == ["u-input", "r1-ask-m0"]

    # Attach r1 from the start, and as a continuation: the snapshot carries it once.
    full = attach(client, f"{url}/threads/th/runs/r1/stream", h)
    check(full)
    (snap,) = snapshots(full)
    assert_once_first(snap)
    cont = attach(client, f"{url}/threads/th/runs/r1/stream", h, {"Last-Event-ID": f"r1:{waiting_seq}"})
    # A continuation sends no snapshot: the client already holds the message and everything
    # before the cut. From the final frame nothing new comes but the close; from inside the run
    # the rest of it, the agent step's text included, and still no snapshot.
    assert snapshots(cont) == [] and types(cont)[-1] == "RUN_FINISHED"
    mid = attach(client, f"{url}/threads/th/runs/r1/stream", h, {"Last-Event-ID": "r1:0"})
    check(mid, continuation=True, run_id="r1")
    assert snapshots(mid) == [] and "TEXT_MESSAGE_CONTENT" in types(mid) and types(mid)[-1] == "RUN_FINISHED"

    # Re-POST of r1 (same body): an attach from the start, the same snapshot.
    status, _, again = post(client, url, first_body, h)
    assert status == 200 and snapshots(again) == [snap]

    # Q5: a new runId without resume re-sends the interrupt; its snapshot carries the message once.
    status, _, q5 = post(client, url, body("th", "r-q5", messages=[USER]), h)
    assert status == 200 and finished(q5)["outcome"]["type"] == "interrupt"
    (q5_snap,) = snapshots(q5)
    assert_once_first(q5_snap)

    # Live resume run (CopilotKit sends the whole history again): once, first.
    resume = [{"interruptId": iid, "status": "resolved", "payload": {"approved": True}}]
    history = [USER, {"id": "r1-ask-m0", "role": "assistant", "content": "Echo"}]
    resume_body = body("th", "r2", resume=resume, messages=history)
    status, detail, live = post(client, url, resume_body, h)
    assert status == 200, detail
    check(live)
    assert "MESSAGES_SNAPSHOT" in types(live) and finished(live)["outcome"]["type"] == "success"
    for s in snapshots(live):
        assert_once_first(s)
    # The resume row stores no messages (the starter row holds the thread's input).
    rows = starter_rows(app, tenant)
    assert [(r[0], r[2]) for r in rows] == [("r1", [{"id": "u-input", "content": INPUT_TEXT}]), ("r2", None)]

    # Re-POST of r2 and its attach: the same snapshots as live.
    status, _, r2_again = post(client, url, resume_body, h)
    assert status == 200 and snapshots(r2_again) == snapshots(live)
    assert snapshots(attach(client, f"{url}/threads/th/runs/r2/stream", h)) == snapshots(live)
    r2_first = next(f.id for f in live if f.id)
    r2_cont = attach(client, f"{url}/threads/th/runs/r2/stream", h, {"Last-Event-ID": r2_first})
    check(r2_cont, continuation=True, run_id="r2", known_open_steps=["approve"])
    # The cut is past RUN_STARTED, before the step's end: the seeded agent step's messages come
    # again as one snapshot, the input message first, nothing twice, nothing missing.
    whole = [{"id": "u-input", "role": "user", "content": INPUT_TEXT},
             {"id": "r1-ask-m0", "role": "assistant", "content": "answer"}]
    assert snapshots(r2_cont) == [whole]
    assert snapshots(live) == [whole]

    # The thread GET after the run ended: still once, first.
    final = client.get(f"{url}/threads/th", headers=h).json()
    assert final["status"] == "completed"
    assert_once_first(final["messages"])


def test_user_messages_beside_a_forwarded_input_object_are_shown_too(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    note = {"id": "u-note", "role": "user", "content": "Please escalate this one."}
    status, detail, _ = post(client, wf_url(wf), body("th2", "r1", messages=[note], input={"q": "hi"}), h)
    assert status == 200, detail
    thread = client.get(f"{wf_url(wf)}/threads/th2", headers=h).json()
    assert_once_first(thread["messages"], note)
    full = attach(client, f"{wf_url(wf)}/threads/th2/runs/r1/stream", h)
    (snap,) = snapshots(full)
    assert_once_first(snap, note)


def test_a_thread_started_without_user_messages_shows_none(ag, tenant, script):
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    status, _, _ = post(client, wf_url(wf), body("th3", "r1", input={"q": "hi"}), h)
    assert status == 200
    assert starter_rows(app, tenant) == [("r1", -1, None)]
    run_id = derive_workflow_run_id(tenant, wf, "th3")
    thread = client.get(f"{wf_url(wf)}/threads/th3", headers=h).json()
    assert thread["run_id"] == run_id and [m["id"] for m in thread["messages"]] == ["r1-ask-m0"]
    assert json.dumps(thread).count('"role": "user"') == 0


def test_a_re_post_under_other_message_ids_keeps_the_stored_ones(ag, tenant, script):
    """MINOR-5 decision: the fingerprint covers the input text, never ids (D4, fingerprint_payload),
    and the starter row is written once (first writer wins). A re-POST of r1 with the same text
    under a new id, or split across two messages that join to the same text, attaches; the
    snapshot keeps the stored id and message, so the thread holds exactly one copy."""
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    url = wf_url(wf)
    stored = [{"id": "u-input", "content": INPUT_TEXT}]
    status, detail, _ = post(client, url, body("th", "r1", messages=[USER]), h)
    assert status == 200, detail
    (snap,) = snapshots(attach(client, f"{url}/threads/th/runs/r1/stream", h))
    assert snap[0] == {"id": "u-input", "role": "user", "content": INPUT_TEXT}

    renamed = {**USER, "id": "u-renamed"}
    status, detail, again = post(client, url, body("th", "r1", messages=[renamed]), h)
    assert status == 200, detail
    assert snapshots(again) == [snap]
    assert starter_rows(app, tenant) == [("r1", -1, stored)]
    thread = client.get(f"{url}/threads/th", headers=h).json()
    assert [m["id"] for m in thread["messages"]] == ["u-input", "r1-ask-m0"]

    # Other text under the same runId is another request: 409, nothing stored.
    other = {**USER, "content": '{"q": "something else"}'}
    status, detail, _ = post(client, url, body("th", "r1", messages=[other]), h)
    assert status == 409, detail
    assert starter_rows(app, tenant) == [("r1", -1, stored)]


def test_the_crash_heal_path_stores_the_input_messages(ag, tenant, script):
    """A crash between the run's insert and its AG-UI row: the re-POST records the starter row
    (`_prepare_post`'s heal branch) with the messages, and every view shows them once."""
    client, app = ag
    h = get_auth_header(tenant, "alice")
    wf = publish(client, h, script)
    url = wf_url(wf)
    first_body = body("th", "r1", messages=[USER])
    status, detail, _ = post(client, url, first_body, h)
    assert status == 200, detail
    with app.state.database_engine.begin() as conn:  # as if the AG-UI row was never written
        conn.execute(text("DELETE FROM ag_ui_runs WHERE tenant_id = :t"), {"t": tenant})
    assert starter_rows(app, tenant) == []

    status, detail, healed = post(client, url, first_body, h)
    assert status == 200, detail
    check(healed)
    assert starter_rows(app, tenant) == [("r1", -1, [{"id": "u-input", "content": INPUT_TEXT}])]
    (snap,) = snapshots(healed)  # healed as an attach from the start
    assert_once_first(snap)
    thread = client.get(f"{url}/threads/th", headers=h).json()
    assert_once_first(thread["messages"])


def test_input_messages_stay_in_their_tenant_and_workflow(ag, tenant, script):
    """The same threadId on another workflow of the tenant, and on another tenant's workflow:
    each thread GET and snapshot shows only its own input message."""
    client, app = ag
    other_tenant = f"t{uuid.uuid4().hex[:10]}"
    app.state.test_tenants.append(other_tenant)
    h = get_auth_header(tenant, "alice")
    h_other = get_auth_header(other_tenant, "bob")
    wf_a = publish(client, h, script)
    wf_b = publish(client, h, script)
    wf_c = publish(client, h_other, script)
    msg = {
        wf_a: {"id": "u-a", "role": "user", "content": '{"q": "a"}'},
        wf_b: {"id": "u-b", "role": "user", "content": '{"q": "b"}'},
        wf_c: {"id": "u-c", "role": "user", "content": '{"q": "c"}'},
    }
    owner = {wf_a: h, wf_b: h, wf_c: h_other}
    for wf, m in msg.items():
        status, detail, _ = post(client, wf_url(wf), body("th", "r1", messages=[m]), owner[wf])
        assert status == 200, detail
    for wf, m in msg.items():
        thread = client.get(f"{wf_url(wf)}/threads/th", headers=owner[wf]).json()
        users = [x for x in thread["messages"] if x["role"] == "user"]
        assert users == [m], (wf, thread["messages"])
        (snap,) = snapshots(attach(client, f"{wf_url(wf)}/threads/th/runs/r1/stream", owner[wf]))
        assert [x for x in snap if x["role"] == "user"] == [m]
    # Across tenants the workflow itself is unknown: no messages leak through the thread GET.
    assert client.get(f"{wf_url(wf_c)}/threads/th", headers=h).status_code == 404
    assert client.get(f"{wf_url(wf_a)}/threads/th", headers=h_other).status_code == 404
