"""Agent archive (follow-up item 7): POST /agents/{id}/archive and /unarchive, GET /agents?archived,
and the refusals of eap_api.agent_archive (new runs, conversations, turns, publishes, restores)."""

from __future__ import annotations

import uuid

from eap_api.agent_archive import AGENT_ARCHIVED

from .run_helpers import requires_pg
from .test_conversations import create_conversation, new_conversation, turn
from .test_runs import (  # noqa: F401 - fixtures
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


def archive(client, headers, agent_id, action="archive"):
    return client.post(f"/api/v1/agents/{agent_id}/{action}", headers=headers)


def listed(client, headers, **params):
    r = client.get("/api/v1/agents", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return {a["id"]: a for a in r.json()}


def test_archive_and_unarchive_are_idempotent_and_move_the_agent_between_lists(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    other = make_agent(client, h, publish=False)

    assert client.get(f"/api/v1/agents/{agent_id}", headers=h).json()["archived"] is False
    assert listed(client, h)[agent_id]["archived"] is False

    for _ in range(2):  # idempotent
        r = archive(client, h, agent_id)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["id"] == agent_id and body["archived"] is True and body["latest_version"] == 1

    assert set(listed(client, h)) == {other}
    assert set(listed(client, h, archived="true")) == {agent_id}
    # Still readable by id.
    got = client.get(f"/api/v1/agents/{agent_id}", headers=h)
    assert got.status_code == 200 and got.json()["archived"] is True

    for _ in range(2):
        r = archive(client, h, agent_id, "unarchive")
        assert r.status_code == 200 and r.json()["archived"] is False
    assert set(listed(client, h)) == {agent_id, other}
    assert listed(client, h, archived="true") == {}


def test_archive_is_tenant_scoped(pg, tenant_of):
    client, _ = pg
    _, h1 = tenant_of()
    _, h2 = tenant_of()
    agent_id = make_agent(client, h1, publish=False)
    for action in ("archive", "unarchive"):
        r = archive(client, h2, agent_id, action)
        assert r.status_code == 404 and r.json()["detail"] == "Agent not found"
        assert archive(client, h1, str(uuid.uuid4()), action).status_code == 404
    assert archive(client, h1, "not-a-uuid").status_code == 422
    assert client.get(f"/api/v1/agents/{agent_id}", headers=h1).json()["archived"] is False
    assert listed(client, h2, archived="true") == {}


def test_archived_agent_refuses_new_runs_conversations_turns_and_publishes(pg, tenant_of):
    client, _ = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    recorded_run = str(uuid.uuid4())
    assert start(client, h, agent_id, run_id=recorded_run).status_code == 201
    conv_id = new_conversation(client, h, agent_id)
    assert archive(client, h, agent_id).status_code == 200

    def refused(r):
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == AGENT_ARCHIVED

    refused(start(client, h, agent_id))
    refused(start(client, h, agent_id, version="draft"))
    refused(create_conversation(client, h, agent_id))
    refused(create_conversation(client, h, agent_id, conversation_id=str(uuid.uuid4())))
    refused(turn(client, h, conv_id))
    client.patch(  # a draft edit is allowed, and makes the draft publishable apart from the archive
        f"/api/v1/agents/{agent_id}",
        json={"spec": {"name": "Renamed", "model": {"provider": "test", "name": "ticket"}}},
        headers=h,
    ).raise_for_status()
    refused(client.post(f"/api/v1/agents/{agent_id}/versions", headers=h))
    refused(client.post(f"/api/v1/agents/{agent_id}/versions/1/restore", json={"discard_draft": True}, headers=h))

    # Existing data stays readable, and a retry of a recorded run or conversation still answers.
    assert client.get(f"/api/v1/runs/{recorded_run}", headers=h).status_code == 200
    assert start(client, h, agent_id, run_id=recorded_run).status_code == 200
    assert create_conversation(client, h, agent_id, conversation_id=conv_id).status_code == 200
    assert client.get(f"/api/v1/conversations/{conv_id}", headers=h).status_code == 200
    assert client.get(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 200

    # Unarchived: work starts again.
    assert archive(client, h, agent_id, "unarchive").status_code == 200
    assert start(client, h, agent_id).status_code == 201
    assert create_conversation(client, h, agent_id).status_code == 201
    assert client.post(f"/api/v1/agents/{agent_id}/versions", headers=h).status_code == 201


def _while_archiving(app, agent_id, request):
    """Run `request` (in a thread) while another transaction holds the agent row as archive does
    (FOR NO KEY UPDATE), then commit archived = true. Returns (finished before the commit, response)."""
    import threading

    from sqlalchemy import text

    result: dict = {}
    thread = threading.Thread(target=lambda: result.setdefault("r", request()))
    with app.state.database_engine.connect() as conn:
        tx = conn.begin()
        conn.execute(text("SELECT 1 FROM agents WHERE id = :a FOR NO KEY UPDATE"), {"a": agent_id})
        thread.start()
        thread.join(0.7)
        early = not thread.is_alive()
        conn.execute(text("UPDATE agents SET archived = true WHERE id = :a"), {"a": agent_id})
        tx.commit()
    thread.join(10)
    return early, result["r"]


def test_new_runs_turns_and_conversations_wait_for_an_archive_in_progress(pg, tenant_of):
    """Review minor 6: the archive check locks the agent row FOR SHARE (as publish locks it), so a
    request racing an archive either finishes first or sees the archived flag: never a run created
    on an agent whose archive committed in between."""
    client, app = pg
    _, h = tenant_of()
    agent_id = make_agent(client, h)
    early, r = _while_archiving(app, agent_id, lambda: start(client, h, agent_id))
    assert not early and r.status_code == 409 and r.json()["detail"] == AGENT_ARCHIVED

    assert archive(client, h, agent_id, "unarchive").status_code == 200
    cid = new_conversation(client, h, agent_id)
    early, r = _while_archiving(app, agent_id, lambda: turn(client, h, cid))
    assert not early and r.status_code == 409

    assert archive(client, h, agent_id, "unarchive").status_code == 200
    early, r = _while_archiving(app, agent_id, lambda: create_conversation(client, h, agent_id))
    assert not early and r.status_code == 409
