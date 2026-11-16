"""POST /conversations/{id}/clone: the thread's terminal turns copied into a new conversation."""

from __future__ import annotations

import uuid

from sqlalchemy import text

from eap_run_store import EventLog, RunStore

from .run_helpers import requires_pg
from .test_conversations import finish, new_conversation, turn
from .test_runs import (  # noqa: F401 - fixtures
    FakeDBOSClient,
    _no_hang,
    deltas,
    fake_dbos,
    make_agent,
    pg,
    start,
    stream_settings,
    tenant_of,
)

pytestmark = requires_pg

MESSAGES = [{"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "hi"}]}]


def clone(client, headers, conversation_id):
    return client.post(f"/api/v1/conversations/{conversation_id}/clone", headers=headers)


def complete(app, tenant, run_id, output):
    RunStore(app.state.database_engine).finish_with_end(
        tenant, uuid.UUID(run_id), "completed", output=output, messages=MESSAGES
    )


def test_clone_copies_history_in_order_and_leaves_the_source(pg, tenant_of, fake_dbos):
    client, app = pg
    tenant, h = tenant_of()
    agent_id = make_agent(client, h)
    conv_id = new_conversation(client, h, agent_id, title="Printer")
    run1, run2, run3 = (str(uuid.uuid4()) for _ in range(3))
    turn(client, h, conv_id, run1, input="one")
    complete(app, tenant, run1, "first")
    turn(client, h, conv_id, run2, input="two")
    finish(app, run2, "failed")
    turn(client, h, conv_id, run3, input="three")
    complete(app, tenant, run3, "third")
    before = client.get(f"/api/v1/conversations/{conv_id}", headers=h).json()

    r = clone(client, h, conv_id)
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["id"] != conv_id and new["agent_id"] == agent_id
    assert new["title"] == "Printer (copy)" and new["turn_count"] == 3
    assert new["resource_id"] == before["resource_id"]

    body = client.get(f"/api/v1/conversations/{new['id']}", headers=h).json()
    assert [(x["turn"], x["input"], x["status"]) for x in body["runs"]] == [
        (1, "one", "completed"), (2, "two", "failed"), (3, "three", "completed"),
    ]
    assert [x["output"] for x in body["runs"]] == ["first", None, "third"]
    assert not {x["run_id"] for x in body["runs"]} & {run1, run2, run3}
    log = EventLog(app.state.database_engine)
    for copy, source in zip(body["runs"], (run1, run2, run3)):
        got = [(e.seq, e.type) for e in log.list_events(tenant, uuid.UUID(copy["run_id"]))]
        assert got and got == [(e.seq, e.type) for e in log.list_events(tenant, uuid.UUID(source))]
    with app.state.database_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT messages FROM runs WHERE conversation_id = :c AND turn = 1"), {"c": new["id"]}
        ).scalar_one()
    assert rows == MESSAGES

    # The source is unchanged.
    assert client.get(f"/api/v1/conversations/{conv_id}", headers=h).json() == before


def test_clone_skips_queued_and_running_turns(pg, tenant_of, fake_dbos):
    client, app = pg
    tenant, h = tenant_of()
    agent_id = make_agent(client, h)
    done = new_conversation(client, h, agent_id, title="Busy")
    run1 = turn(client, h, done, input="done").json()["run_id"]
    complete(app, tenant, run1, "ok")
    for state in ("queued", "running"):
        conv_id = new_conversation(client, h, agent_id, title=state)
        first = turn(client, h, conv_id, input="done").json()["run_id"]
        complete(app, tenant, first, "ok")
        busy = turn(client, h, conv_id, input="in flight").json()["run_id"]
        if state == "running":
            finish(app, busy, "running")
        r = clone(client, h, conv_id)
        assert r.status_code == 201, r.text
        body = client.get(f"/api/v1/conversations/{r.json()['id']}", headers=h).json()
        assert [x["input"] for x in body["runs"]] == ["done"]
        assert body["turn_count"] == 1 and body["last_turn_status"] == "completed"
        # The source still has its in-flight turn.
        assert client.get(f"/api/v1/conversations/{conv_id}", headers=h).json()["turn_count"] == 2


def test_clone_can_take_a_new_turn(pg, tenant_of, fake_dbos):
    client, app = pg
    tenant, h = tenant_of()
    conv_id = new_conversation(client, h, make_agent(client, h))
    complete(app, tenant, turn(client, h, conv_id, input="one").json()["run_id"], "first")
    new_id = clone(client, h, conv_id).json()["id"]
    assert clone(client, h, conv_id).json()["id"] != new_id  # each clone is its own thread
    r = turn(client, h, new_id, input="next")
    assert r.status_code == 201 and r.json()["turn"] == 2 and r.json()["conversation_id"] == new_id
    # The history the worker builds comes from the copied turn.
    history = RunStore(app.state.database_engine).completed_turns_before(tenant, new_id, 2)
    assert [(t["turn"], t["input"], t["output"], t["messages"]) for t in history] == [(1, "one", "first", MESSAGES)]
    assert client.get(f"/api/v1/conversations/{conv_id}", headers=h).json()["turn_count"] == 1


def test_clone_of_an_empty_untitled_thread(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    conv_id = new_conversation(client, h, make_agent(client, h))
    r = clone(client, h, conv_id)
    assert r.status_code == 201
    assert r.json()["title"] == "Untitled conversation (copy)" and r.json()["turn_count"] == 0


def test_clone_is_404_across_tenants_unknown_and_malformed(pg, tenant_of):
    client, _ = pg
    _, h1 = tenant_of()
    _, h2 = tenant_of()
    conv_id = new_conversation(client, h1, make_agent(client, h1))
    assert clone(client, h2, conv_id).status_code == 404
    assert clone(client, h1, str(uuid.uuid4())).status_code == 404
    assert clone(client, h1, "not-a-uuid").status_code == 404
    assert [c["id"] for c in client.get("/api/v1/conversations", headers=h2).json()["items"]] == []


def test_clone_of_a_deleted_conversation_is_404(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    conv_id = new_conversation(client, h, make_agent(client, h))
    assert client.delete(f"/api/v1/conversations/{conv_id}", headers=h).status_code == 204
    assert clone(client, h, conv_id).status_code == 404
