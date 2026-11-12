"""Conversation API (milestone 2, task 3): create/list/get, turns as runs, 409 while a turn runs,
idempotent turn start on the client run_id (including the insert-then-crash retry), tenant 404s.

Uses the same Postgres app + fake DBOSClient as test_runs.py, so the exact enqueue call of the
hand-off contract is checked without DBOS.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from eap_run_store import RunStore

from .run_helpers import requires_pg
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


def create_conversation(client: TestClient, headers: dict[str, str], agent_id: str, **body: Any):
    return client.post(f"/api/v1/agents/{agent_id}/conversations", json=body, headers=headers)


def new_conversation(client: TestClient, headers: dict[str, str], agent_id: str, title: str | None = None) -> str:
    r = create_conversation(client, headers, agent_id, **({"title": title} if title else {}))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def turn(client: TestClient, headers: dict[str, str], conversation_id: str, run_id: str | None = None,
         input: str = "hello", **body: Any):
    payload = {"run_id": run_id or str(uuid.uuid4()), "input": input, **body}
    return client.post(f"/api/v1/conversations/{conversation_id}/runs", json=payload, headers=headers)


def finish(app, run_id: str, status: str = "completed", output: str | None = "ok") -> None:
    store = RunStore(app.state.database_engine)
    with app.state.database_engine.connect() as conn:
        tenant = conn.execute(text("SELECT tenant_id FROM runs WHERE id = :r"), {"r": run_id}).scalar_one()
    store.set_status(tenant, uuid.UUID(run_id), status, output=output if status == "completed" else None,
                     error=None if status == "completed" else "boom")


class TestConversationCrud:
    def test_create_list_get(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        r = create_conversation(client, h, agent_id, title="Printer")
        assert r.status_code == 201, r.text
        conv = r.json()
        assert conv["agent_id"] == agent_id and conv["title"] == "Printer" and conv["turn_count"] == 0
        assert {"id", "created_at", "updated_at"} <= set(conv)

        listed = client.get("/api/v1/conversations", headers=h)
        assert listed.status_code == 200
        assert [c["id"] for c in listed.json()] == [conv["id"]]

        got = client.get(f"/api/v1/conversations/{conv['id']}", headers=h)
        assert got.status_code == 200
        assert got.json()["id"] == conv["id"] and got.json()["runs"] == []

    def test_list_filters_by_agent_and_newest_first(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        a1, a2 = make_agent(client, h), make_agent(client, h)
        c1 = new_conversation(client, h, a1)
        c2 = new_conversation(client, h, a2)
        c3 = new_conversation(client, h, a1)
        assert [c["id"] for c in client.get("/api/v1/conversations", headers=h).json()] == [c3, c2, c1]
        assert [c["id"] for c in client.get(f"/api/v1/conversations?agent_id={a1}", headers=h).json()] == [c3, c1]

    def test_client_conversation_id_is_idempotent(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        _, other = tenant_of()
        agent_id = make_agent(client, h)
        conv_id = str(uuid.uuid4())
        assert create_conversation(client, h, agent_id, conversation_id=conv_id).status_code == 201
        again = create_conversation(client, h, agent_id, conversation_id=conv_id)
        assert again.status_code == 200 and again.json()["id"] == conv_id
        assert create_conversation(client, other, make_agent(client, other), conversation_id=conv_id).status_code == 409

    def test_unknown_or_other_tenants_agent_is_404(self, pg, tenant_of):
        client, _ = pg
        _, h1 = tenant_of()
        _, h2 = tenant_of()
        agent_id = make_agent(client, h1)
        assert create_conversation(client, h2, agent_id).status_code == 404
        assert create_conversation(client, h1, str(uuid.uuid4())).status_code == 404


class TestTurns:
    def test_turns_enqueue_per_contract_and_are_listed_in_order(self, pg, tenant_of, fake_dbos):
        client, app = pg
        tenant, h = tenant_of()
        agent_id = make_agent(client, h)
        conv_id = new_conversation(client, h, agent_id)

        run1 = str(uuid.uuid4())
        r = turn(client, h, conv_id, run1, input="My name is Ada")
        assert r.status_code == 201, r.text
        assert r.json() == {
            "run_id": run1, "status": "queued", "display_status": "queued", "agent_id": agent_id, "version": 1,
            "conversation_id": conv_id, "turn": 1,
        }
        # The hand-off contract's six positional args are unchanged; the worker reads the
        # conversation link from the run row.
        options, args = fake_dbos.calls[-1]
        assert options["workflow_id"] == run1 and options["workflow_name"] == "eap.agent_run"
        assert len(args) == 6 and args[:4] == (run1, tenant, agent_id, 1) and args[5] == "My name is Ada"

        finish(app, run1)
        run2 = str(uuid.uuid4())
        r2 = turn(client, h, conv_id, run2, input="What is my name?")
        assert r2.status_code == 201 and r2.json()["turn"] == 2

        body = client.get(f"/api/v1/conversations/{conv_id}", headers=h).json()
        assert [(r["run_id"], r["turn"], r["input"]) for r in body["runs"]] == [
            (run1, 1, "My name is Ada"), (run2, 2, "What is my name?"),
        ]
        assert body["runs"][0]["status"] == "completed" and body["runs"][0]["output"] == "ok"
        assert body["turn_count"] == 2
        run_view = client.get(f"/api/v1/runs/{run2}", headers=h).json()
        assert run_view["conversation_id"] == conv_id and run_view["turn"] == 2

    @pytest.mark.parametrize("previous", ["queued", "running"])
    def test_next_turn_is_409_while_the_previous_turn_is_not_terminal(self, pg, tenant_of, fake_dbos, previous):
        client, app = pg
        _, h = tenant_of()
        conv_id = new_conversation(client, h, make_agent(client, h))
        run1 = turn(client, h, conv_id).json()["run_id"]
        if previous == "running":
            finish(app, run1, "running")
        calls = len(fake_dbos.calls)
        busy = turn(client, h, conv_id, input="too soon")
        assert busy.status_code == 409
        assert run1 in busy.json()["detail"]
        assert len(fake_dbos.calls) == calls  # nothing enqueued
        assert len(client.get(f"/api/v1/conversations/{conv_id}", headers=h).json()["runs"]) == 1

    @pytest.mark.parametrize("terminal", ["completed", "failed", "needs_attention"])
    def test_any_terminal_previous_turn_allows_the_next(self, pg, tenant_of, terminal):
        client, app = pg
        _, h = tenant_of()
        conv_id = new_conversation(client, h, make_agent(client, h))
        finish(app, turn(client, h, conv_id).json()["run_id"], terminal)
        assert turn(client, h, conv_id).status_code == 201

    def test_retry_same_run_id_is_200_one_row_and_reenqueued(self, pg, tenant_of, fake_dbos):
        client, app = pg
        _, h = tenant_of()
        conv_id = new_conversation(client, h, make_agent(client, h))
        run_id = str(uuid.uuid4())
        assert turn(client, h, conv_id, run_id).status_code == 201
        finish(app, run_id, "running")  # still the in-flight latest turn: a retry is not "the next turn"
        again = turn(client, h, conv_id, run_id)
        assert again.status_code == 200 and again.json()["turn"] == 1
        with app.state.database_engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM runs WHERE id = :r"), {"r": run_id}).scalar_one() == 1
        assert [c[0]["workflow_id"] for c in fake_dbos.calls] == [run_id, run_id]
        assert fake_dbos.calls[0][1] == fake_dbos.calls[1][1]

    @pytest.mark.parametrize("fake_dbos", [FakeDBOSClient(fail_times=1)])
    def test_enqueue_failure_is_503_and_retry_heals(self, pg, tenant_of, fake_dbos):
        client, _ = pg
        _, h = tenant_of()
        conv_id = new_conversation(client, h, make_agent(client, h))
        run_id = str(uuid.uuid4())
        assert turn(client, h, conv_id, run_id).status_code == 503
        healed = turn(client, h, conv_id, run_id)
        assert healed.status_code == 200 and healed.json()["turn"] == 1
        assert [c[0]["workflow_id"] for c in fake_dbos.calls] == [run_id]

    def test_crash_between_insert_and_enqueue_heals_on_retry(self, pg, tenant_of, fake_dbos):
        """The API died after committing the turn's row (no enqueue happened at all)."""
        client, app = pg
        tenant, h = tenant_of()
        agent_id = make_agent(client, h)
        conv_id = new_conversation(client, h, agent_id)
        run_id = uuid.uuid4()
        RunStore(app.state.database_engine).create_turn(tenant, conv_id, 1, "hello", run_id)
        assert fake_dbos.calls == []
        # Another client cannot start a new turn over the orphan ...
        assert turn(client, h, conv_id, input="other").status_code == 409
        # ... but the original client's retry of the same request enqueues it.
        retry = turn(client, h, conv_id, str(run_id), input="hello")
        assert retry.status_code == 200 and retry.json()["turn"] == 1
        assert [c[0]["workflow_id"] for c in fake_dbos.calls] == [str(run_id)]
        assert fake_dbos.calls[0][1][:4] == (str(run_id), tenant, agent_id, 1)

    def test_same_run_id_for_a_different_request_is_409(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        conv_a = new_conversation(client, h, agent_id)
        conv_b = new_conversation(client, h, agent_id)
        run_id = str(uuid.uuid4())
        assert turn(client, h, conv_a, run_id, input="hello").status_code == 201
        assert turn(client, h, conv_a, run_id, input="changed").status_code == 409
        assert turn(client, h, conv_b, run_id, input="hello").status_code == 409
        # A turn's run_id is not a standalone run either, and vice versa.
        assert start(client, h, agent_id, run_id, input="hello").status_code == 409
        standalone = str(uuid.uuid4())
        assert start(client, h, agent_id, standalone).status_code == 201
        assert turn(client, h, conv_b, standalone, input="printer is on fire").status_code == 409

    def test_agent_without_published_version_is_409(self, pg, tenant_of, fake_dbos):
        client, _ = pg
        _, h = tenant_of()
        conv_id = new_conversation(client, h, make_agent(client, h, publish=False))
        assert turn(client, h, conv_id).status_code == 409
        assert fake_dbos.calls == []

    def test_standalone_runs_still_work_without_a_conversation(self, pg, tenant_of):
        client, _ = pg
        _, h = tenant_of()
        agent_id = make_agent(client, h)
        r = start(client, h, agent_id)
        assert r.status_code == 201
        run = client.get(f"/api/v1/runs/{r.json()['run_id']}", headers=h).json()
        assert run["conversation_id"] is None and run["turn"] is None


class TestTenantIsolation:
    def test_other_tenant_gets_404_everywhere(self, pg, tenant_of, fake_dbos):
        client, _ = pg
        _, h1 = tenant_of()
        _, h2 = tenant_of()
        conv_id = new_conversation(client, h1, make_agent(client, h1))
        turn(client, h1, conv_id)
        calls = len(fake_dbos.calls)
        assert client.get(f"/api/v1/conversations/{conv_id}", headers=h2).status_code == 404
        assert turn(client, h2, conv_id).status_code == 404
        assert client.get("/api/v1/conversations", headers=h2).json() == []
        assert client.get(f"/api/v1/conversations/{uuid.uuid4()}", headers=h1).status_code == 404
        assert turn(client, h1, str(uuid.uuid4())).status_code == 404
        assert len(fake_dbos.calls) == calls
