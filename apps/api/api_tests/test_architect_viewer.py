"""A viewer may chat with the built-in Architect and nothing else (docs/iam.md, "Viewers and the Architect").

The real `OpenFgaPolicy` over `ModelServer` (admin ann, editor eve, viewer vic; zed is no member) against the real API on
Postgres, with a fake DBOS client. The tool list of such a run is the worker's (worker_tests/test_architect_read_only.py);
the same rules against a real OpenFGA are in test_architect_viewer_openfga_real.py."""

from __future__ import annotations

import threading
import time
import uuid

import pytest
from eap_run_store import EventLog, RunStore
from sqlalchemy import text

from .test_iam_p2b_wave1 import API, World
from .test_iam_p2b_wave1 import world as _wave1_world  # noqa: F401
from .test_workflow_runs_api import AGENT_SPEC


@pytest.fixture
def world(_wave1_world):  # noqa: F811
    _wave1_world.use(_wave1_world.engine)
    return _wave1_world


def make_agent(w: World, name: str, *, slug: str | None = None, user: str = "ann") -> str:
    """A published agent made by `user`; with `slug`, the row is made built in (as the Architect seed writes it)."""
    made = w.client.post(f"{API}/agents", json={"spec": {**AGENT_SPEC, "name": name}}, headers=w.h(user))
    assert made.status_code == 201, made.text
    agent_id = made.json()["id"]
    assert w.client.post(f"{API}/agents/{agent_id}/versions", headers=w.h(user)).status_code == 201
    if slug is not None:
        with w.app.state.database_engine.begin() as conn:
            conn.execute(text("UPDATE agents SET builtin = true, slug = :s WHERE id = :a"), {"s": slug, "a": agent_id})
    return agent_id


def converse(w: World, user: str, agent_id: str):
    """Create a conversation: its response."""
    return w.client.post(f"{API}/agents/{agent_id}/conversations", json={}, headers=w.h(user))


def turn(w: World, user: str, conversation_id: str, run_id: str | None = None):
    return w.client.post(
        f"{API}/conversations/{conversation_id}/runs", json={"input": "list agents", "run_id": run_id or str(uuid.uuid4())},
        headers=w.h(user),
    )


def end_run(w: World, run_id: str) -> None:
    """What the worker would do: the terminal event, then the terminal status (an open stream ends)."""
    EventLog(w.app.state.database_engine).append(w.tenant, uuid.UUID(run_id), "end", "run_completed", {"output": "ok"})
    finish(w, run_id)


def agui(w: World, user: str, agent_id: str, thread: str, *, end: bool = True):
    body = {"threadId": thread, "runId": uuid.uuid4().hex, "tools": [], "context": [], "state": {}, "forwardedProps": {},
            "messages": [{"id": uuid.uuid4().hex, "role": "user", "content": "list agents"}]}
    # No worker runs here, so a thread plays it: it ends the turn this request records, which ends the stream.
    done = threading.Event()

    def play_worker() -> None:
        deadline = time.monotonic() + 30
        while not done.is_set() and time.monotonic() < deadline:
            with w.app.state.database_engine.connect() as conn:
                found = conn.execute(
                    text("SELECT run_id FROM ag_ui_runs WHERE tenant_id = :t AND external_run_id = :r"),
                    {"t": w.tenant, "r": body["runId"]},
                ).scalar_one_or_none()
            if found is not None:
                return end_run(w, str(found))
            time.sleep(0.05)

    helper = threading.Thread(target=play_worker) if end else None
    if helper is not None:
        helper.start()
    try:
        return w.client.post(f"{API}/ag-ui/agents/{agent_id}", json=body, headers=w.h(user)).status_code
    finally:
        done.set()
        if helper is not None:
            helper.join(timeout=40)


def finish(w: World, run_id: str) -> None:
    with w.app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'completed' WHERE id = :r"), {"r": run_id})


def actor(w: World, run_id: str):
    return RunStore(w.app.state.database_engine).get_run_actor(w.tenant, uuid.UUID(run_id))


def test_a_viewer_starts_and_continues_an_architect_conversation(world: World):
    architect = make_agent(world, "Architect", slug="architect")
    made = converse(world, "vic", architect)
    assert made.status_code == 201, made.text
    conversation = made.json()["id"]
    first = turn(world, "vic", conversation)
    assert first.status_code == 201, first.text
    finish(world, first.json()["run_id"])
    second = turn(world, "vic", conversation)
    assert second.status_code == 201, second.text
    # Each turn records the person it acts for, which the Architect's run token and the worker read.
    for run in (first.json()["run_id"], second.json()["run_id"]):
        who = actor(world, run)
        assert (who.kind, who.on_behalf_of) == ("user", "vic") and who.roles is not None


def test_a_viewer_chats_with_the_architect_over_ag_ui(world: World):
    architect = make_agent(world, "Architect", slug="architect")
    thread = uuid.uuid4().hex
    assert agui(world, "vic", architect, thread) == 200
    assert world.count("runs") == 1


@pytest.mark.parametrize("slug", [None, "architect-drafter", "architect-workflow-drafter", "scorer-suggester",
                                  "workflow-explainer"])
def test_a_viewer_may_not_run_any_other_agent(world: World, slug: str | None):
    agent_id = make_agent(world, "Other", slug=slug)
    refused = converse(world, "vic", agent_id)
    assert refused.status_code == 403, refused.text
    assert refused.json()["refused"] == {"action": "run", "kind": "agent", "id": agent_id}
    assert agui(world, "vic", agent_id, uuid.uuid4().hex) == 403
    conversation = converse(world, "eve", agent_id).json()["id"]
    assert turn(world, "vic", conversation).status_code == 403
    assert world.count("runs") == 0


def test_a_viewer_gets_nothing_else_of_run_on_the_architect(world: World):
    architect = make_agent(world, "Architect", slug="architect")
    conversation = converse(world, "vic", architect).json()["id"]
    c, h = world.client, world.h("vic")
    for method, url, body in [
        ("post", f"{API}/agents/{architect}/runs", {"input": "hi", "run_id": str(uuid.uuid4())}),
        ("patch", f"{API}/conversations/{conversation}", {"title": "x"}),
        ("post", f"{API}/conversations/{conversation}/clone", None),
        ("delete", f"{API}/conversations/{conversation}", None),
        ("patch", f"{API}/agents/{architect}", {"spec": AGENT_SPEC}),
        ("post", f"{API}/agents/{architect}/versions", None),
    ]:
        r = getattr(c, method)(url, headers=h, **({"json": body} if body is not None else {}))
        assert r.status_code == 403, (method, url, r.status_code, r.text)


def test_a_viewer_continues_only_their_own_conversations(world: World):
    """A viewer's text must not reach a thread whose next turn runs with someone else's write tools."""
    architect = make_agent(world, "Architect", slug="architect")
    thread = uuid.uuid4().hex
    assert agui(world, "eve", architect, thread) == 200
    theirs = converse(world, "eve", architect).json()["id"]
    first = turn(world, "eve", theirs)
    finish(world, first.json()["run_id"])
    with world.app.state.database_engine.begin() as conn:
        conn.execute(text("UPDATE runs SET status = 'completed' WHERE tenant_id = :t"), {"t": world.tenant})
    before = world.count("runs")
    refused = turn(world, "vic", theirs)
    assert refused.status_code == 403 and "that you started" in refused.json()["detail"], refused.text
    assert agui(world, "vic", architect, thread) == 403
    assert world.count("runs") == before
    # They still read it (unchanged), and an editor continues a viewer's thread as before.
    assert world.client.get(f"{API}/conversations/{theirs}", headers=world.h("vic")).status_code == 200
    mine = converse(world, "vic", architect).json()["id"]
    run = turn(world, "vic", mine).json()["run_id"]
    finish(world, run)
    assert turn(world, "eve", mine).status_code == 201


def test_editors_and_admins_are_unchanged_and_a_non_member_is_still_refused(world: World):
    architect = make_agent(world, "Architect", slug="architect")
    plain = make_agent(world, "Plain")
    for user in ("ann", "eve"):
        for agent_id in (architect, plain):
            conversation = converse(world, user, agent_id).json()["id"]
            assert turn(world, user, conversation).status_code == 201
    assert converse(world, "zed", architect).status_code == 404
    assert agui(world, "zed", architect, uuid.uuid4().hex) == 404


def test_another_tenants_architect_is_not_found(world: World):
    make_agent(world, "Architect", slug="architect")
    assert converse(world, "vic", str(uuid.uuid4())).status_code == 404


def test_a_turn_recorded_after_waiting_for_the_previous_one_still_names_the_viewer(world: World):
    """The AG-UI route records such a turn from inside its stream: losing the actor there would run it with write tools."""
    architect = make_agent(world, "Architect", slug="architect")
    thread = uuid.uuid4().hex
    assert agui(world, "vic", architect, thread) == 200  # ends at once: the helper finishes the turn
    engine = world.app.state.database_engine
    with engine.begin() as conn:  # the first turn is running again, so the next one has to wait for it
        conn.execute(text("UPDATE runs SET status = 'running' WHERE tenant_id = :t"), {"t": world.tenant})
        (first,) = conn.execute(text("SELECT id FROM runs WHERE tenant_id = :t"), {"t": world.tenant}).scalars().all()
    seen: list[str] = []

    def worker() -> None:
        time.sleep(0.5)
        finish(world, str(first))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with engine.connect() as conn:
                second = conn.execute(
                    text("SELECT id FROM runs WHERE tenant_id = :t AND turn = 2"), {"t": world.tenant}
                ).scalar_one_or_none()
            if second is not None:
                seen.append(str(second))
                end_run(world, str(second))
                return
            time.sleep(0.05)

    helper = threading.Thread(target=worker)
    helper.start()
    status = agui(world, "vic", architect, thread, end=False)
    helper.join(timeout=40)
    assert status == 200 and len(seen) == 1
    who = actor(world, seen[0])
    assert (who.kind, who.on_behalf_of) == ("user", "vic") and who.roles is not None
