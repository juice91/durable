"""AGUI.2: the AG-UI run mapping (`ag_ui_runs`), `conversations.external_thread_id`, the
`runs.client_tools` / `client_tool_results` columns and `pending_client_tool_calls`.
Implementer's focused tests; the acceptance suite adds more."""

from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import threading

from eap_run_store import (
    AgUiRunWrite,
    AgUiScopeMismatch,
    AgUiStore,
    ConversationIdConflict,
    PendingClientToolCall,
    RunNotFound,
)


def _fp(seed: str = "x") -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


@pytest.fixture
def tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ag_ui_runs WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t AND parent_run_id IS NOT NULL"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.fixture
def other_tenant(engine):
    tenant_id = f"test_tenant_{uuid4().hex[:8]}"
    yield tenant_id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ag_ui_runs WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": tenant_id})
        conn.execute(text("DELETE FROM conversations WHERE tenant_id = :t"), {"t": tenant_id})


@pytest.fixture
def ag_ui(engine):
    return AgUiStore(engine)


def _turn(run_store, tenant, conv_id, *, status="completed", **kwargs):
    run_id = uuid4()
    run_store.create_turn(tenant, conv_id, 1, "hi", run_id, **kwargs)
    if status != "queued":
        run_store.set_status(tenant, run_id, "running")
        if status != "running":
            run_store.set_status(tenant, run_id, status)
    return str(run_id)


def _record(ag_ui, tenant, run_id, **overrides):
    values = dict(
        scope_kind="agent",
        scope_id="agent-1",
        external_thread_id="thread-1",
        external_run_id="run-1",
        run_id=run_id,
        fingerprint=_fp(),
    )
    values.update(overrides)
    return ag_ui.record_ag_ui_run(tenant, **values)


# ---------------------------------------------------------------------- record / get


def test_record_is_idempotent_and_first_writer_wins(run_store, ag_ui, tenant):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    run_id = _turn(run_store, tenant, conv["id"], status="queued")

    rec, created = _record(ag_ui, tenant, run_id, input_message_ids=["u1", "u2"])
    assert created is True
    assert rec.run_id == run_id and rec.from_seq == -1 and rec.fingerprint == _fp()
    assert rec.input_message_ids == ("u1", "u2")
    assert rec.created_at is not None

    again, created = _record(ag_ui, tenant, run_id, fingerprint=_fp("other"), from_seq=5, input_message_ids=["z"])
    assert created is False
    assert again == rec  # unchanged: the caller compares fingerprints

    got = ag_ui.get_ag_ui_run(
        tenant, scope_kind="agent", scope_id="agent-1", external_thread_id="thread-1", external_run_id="run-1"
    )
    assert got == rec
    assert ag_ui.get_ag_ui_run(
        tenant, scope_kind="agent", scope_id="agent-1", external_thread_id="thread-1", external_run_id="run-2"
    ) is None


def test_same_external_ids_are_distinct_per_tenant_scope_and_kind(run_store, ag_ui, tenant, other_tenant):
    wf_run = uuid4()
    run_store.create_workflow_run(tenant, "agent-1", 1, "{}", wf_run)
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    agent_run = _turn(run_store, tenant, conv["id"], status="queued")
    other_conv, _ = run_store.create_conversation(other_tenant, "agent-1")
    other_run = _turn(run_store, other_tenant, other_conv["id"], status="queued")

    conv2, _ = run_store.create_conversation(tenant, "agent-2")
    agent2_run = _turn(run_store, tenant, conv2["id"], status="queued")
    a, _ = _record(ag_ui, tenant, agent_run)
    b, created_b = _record(ag_ui, tenant, agent2_run, scope_id="agent-2")
    c, created_c = _record(ag_ui, tenant, str(wf_run), scope_kind="workflow", from_seq=3)
    d, created_d = _record(ag_ui, other_tenant, other_run)
    assert created_b and created_c and created_d
    assert {a.scope_id, b.scope_id} == {"agent-1", "agent-2"}
    assert c.scope_kind == "workflow" and c.from_seq == 3 and c.run_id == str(wf_run)
    assert d.tenant_id == other_tenant and d.run_id == other_run

    # Tenant-scoped read: the other tenant's row is never returned for this tenant.
    key = dict(scope_kind="agent", scope_id="agent-1", external_thread_id="thread-1", external_run_id="run-1")
    assert ag_ui.get_ag_ui_run(tenant, **key).run_id == agent_run
    assert ag_ui.get_ag_ui_run(other_tenant, **key).run_id == other_run
    assert ag_ui.get_ag_ui_run("nobody", **key) is None


def test_several_ag_ui_runs_may_name_one_workflow_run(run_store, ag_ui, tenant):
    wf_run = str(uuid4())
    run_store.create_workflow_run(tenant, "wf", 1, "{}", wf_run)
    _record(ag_ui, tenant, wf_run, scope_kind="workflow", scope_id="wf", external_run_id="r1")
    rec, created = _record(ag_ui, tenant, wf_run, scope_kind="workflow", scope_id="wf", external_run_id="r2", from_seq=7)
    assert created and rec.from_seq == 7


def test_record_refuses_another_tenants_run(run_store, ag_ui, tenant, other_tenant):
    conv, _ = run_store.create_conversation(other_tenant, "agent-1")
    run_id = _turn(run_store, other_tenant, conv["id"], status="queued")
    with pytest.raises(RunNotFound):
        _record(ag_ui, tenant, run_id)
    with pytest.raises(RunNotFound):
        _record(ag_ui, tenant, str(uuid4()))


@pytest.mark.parametrize(
    "override",
    [
        {"scope_kind": "tool"},
        {"scope_id": ""},
        {"external_thread_id": ""},
        {"external_run_id": ""},
        {"from_seq": -2},
        {"from_seq": True},
        {"fingerprint": "abc"},
        {"fingerprint": _fp().upper()},
        # `$` would accept a trailing newline; the DB check does not (never a RunNotFound).
        {"fingerprint": _fp() + "\n"},
        {"input_message_ids": [1]},
    ],
)
def test_record_validates_arguments(ag_ui, tenant, override):
    with pytest.raises(ValueError):
        _record(ag_ui, tenant, "any-run", **override)


def test_checks_hold_for_direct_writes(engine, run_store, tenant):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    run_id = _turn(run_store, tenant, conv["id"], status="queued")
    insert = (
        "INSERT INTO ag_ui_runs (tenant_id, scope_kind, scope_id, external_thread_id, external_run_id, run_id,"
        " from_seq, fingerprint, input_message_ids)"
        " VALUES (:t, :k, 'a', 'th', :r, :run, :s, :f, CAST(:m AS jsonb))"
    )
    good = dict(t=tenant, k="agent", r="r", run=run_id, s=-1, f=_fp(), m="[]")
    bad = [
        (dict(good, r="r1", k="tool"), "ck_ag_ui_runs_scope_kind"),
        (dict(good, r="r2", s=-2), "ck_ag_ui_runs_from_seq"),
        (dict(good, r="r3", f="nothex"), "ck_ag_ui_runs_fingerprint"),
        (dict(good, r="r4", m="{}"), "ck_ag_ui_runs_input_message_ids"),
        (dict(good, r="r5", t="other-tenant"), "fk_ag_ui_runs_tenant_run"),
    ]
    for params, constraint in bad:
        with pytest.raises(IntegrityError, match=constraint):
            with engine.begin() as conn:
                conn.execute(text(insert), params)
    for sql, constraint in [
        ("UPDATE runs SET client_tools = '{}'::jsonb WHERE id = :run", "ck_runs_client_tools"),
        ("UPDATE runs SET client_tool_results = '\"x\"'::jsonb WHERE id = :run", "ck_runs_client_tool_results"),
    ]:
        with pytest.raises(IntegrityError, match=constraint):
            with engine.begin() as conn:
                conn.execute(text(sql), {"run": run_id})


def test_rows_cascade_with_the_run(engine, run_store, ag_ui, tenant):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    run_id = _turn(run_store, tenant, conv["id"], status="queued")
    _record(ag_ui, tenant, run_id)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM runs WHERE id = :r"), {"r": run_id})
    assert ag_ui.get_ag_ui_run(
        tenant, scope_kind="agent", scope_id="agent-1", external_thread_id="thread-1", external_run_id="run-1"
    ) is None


# ---------------------------------------------------------------------- conversations.external_thread_id


def test_external_thread_id_is_stored_once_and_unique_per_tenant_and_agent(run_store, tenant, other_tenant):
    conv_id = str(uuid4())
    conv, created = run_store.create_conversation(tenant, "agent-1", conversation_id=conv_id, external_thread_id="th")
    assert created and conv["external_thread_id"] == "th"
    again, created = run_store.create_conversation(tenant, "agent-1", conversation_id=conv_id, external_thread_id="th")
    assert not created and again["external_thread_id"] == "th"
    assert run_store.get_conversation(tenant, conv_id)["external_thread_id"] == "th"
    # A re-create naming another (or no) thread id is a conflict: it is never changed.
    with pytest.raises(ConversationIdConflict):
        run_store.create_conversation(tenant, "agent-1", conversation_id=conv_id, external_thread_id="other")
    with pytest.raises(ConversationIdConflict):
        run_store.create_conversation(tenant, "agent-1", conversation_id=conv_id)
    # Another conversation of the same tenant and agent cannot hold the thread.
    with pytest.raises(ConversationIdConflict):
        run_store.create_conversation(tenant, "agent-1", external_thread_id="th")
    # Another agent or another tenant can.
    run_store.create_conversation(tenant, "agent-2", external_thread_id="th")
    run_store.create_conversation(other_tenant, "agent-1", external_thread_id="th")
    with pytest.raises(ValueError):
        run_store.create_conversation(tenant, "agent-1", external_thread_id="")


def test_old_rows_read_the_new_columns_as_null(run_store, tenant):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    assert conv["external_thread_id"] is None
    run_id = _turn(run_store, tenant, conv["id"], status="queued")
    run = run_store.get_run(tenant, run_id)
    assert run["client_tools"] is None and run["client_tool_results"] is None
    standalone = uuid4()
    run_store.create_run(tenant, "agent-1", 1, "hi", standalone)
    assert run_store.get_run(tenant, standalone)["client_tools"] is None


def test_turn_stores_client_tools_and_results(engine, run_store, tenant):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    tools = [{"name": "pick_color", "description": "Pick", "parameters": {"type": "object"}}]
    results = [{"tool_call_id": "c1", "content": "red"}]
    run_id = _turn(run_store, tenant, conv["id"], status="completed", client_tools=tools, client_tool_results=results)
    _turn(run_store, tenant, conv["id"], status="queued")  # without client tools
    run = run_store.get_run(tenant, run_id)
    assert run["client_tools"] == tools and run["client_tool_results"] == results
    with engine.connect() as conn:
        # SQL NULL, not the JSON value null, when absent.
        assert conn.execute(text(
            "SELECT count(*) FROM runs WHERE tenant_id = :t AND client_tools = 'null'::jsonb"
        ), {"t": tenant}).scalar_one() == 0


# ---------------------------------------------------------------------- pending_client_tool_calls


def _complete_with_pending(event_log, tenant, run_id, calls, pending):
    for call_id, name, args in calls:
        event_log.append(
            tenant, run_id, f"tool:{call_id}:call", "tool_call", {"tool_call_id": call_id, "tool_name": name, "args": args}
        )
    event_log.append(tenant, run_id, "end", "run_completed", {"output": None, "pending_tool_call_ids": pending})


def test_pending_calls_come_from_the_latest_turn_only(run_store, event_log, ag_ui, tenant):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    first = _turn(run_store, tenant, conv["id"], status="running")
    _complete_with_pending(event_log, tenant, first, [("c1", "pick", {"a": 1})], ["c1"])
    run_store.set_status(tenant, first, "completed")

    assert ag_ui.pending_client_tool_calls(tenant, conv["id"]) == [
        PendingClientToolCall(run_id=first, turn=1, tool_call_id="c1", tool_name="pick", args={"a": 1})
    ]

    # The next turn (running, then completed with nothing pending) hides turn 1's calls.
    second = _turn(run_store, tenant, conv["id"], status="running")
    assert ag_ui.pending_client_tool_calls(tenant, conv["id"]) == []
    event_log.append(tenant, second, "end", "run_completed", {"output": "done"})
    run_store.set_status(tenant, second, "completed")
    assert ag_ui.pending_client_tool_calls(tenant, conv["id"]) == []

    # A third turn pending two calls, in the run_completed order; a server call is not listed.
    third = _turn(run_store, tenant, conv["id"], status="running")
    _complete_with_pending(
        event_log,
        tenant,
        third,
        [("s1", "server_tool", {}), ("c3", "b", {"y": 2}), ("c2", "a", {"x": 1})],
        ["c2", "c3"],
    )
    run_store.set_status(tenant, third, "completed")
    pending = ag_ui.pending_client_tool_calls(tenant, conv["id"])
    assert [(p.tool_call_id, p.tool_name, p.args, p.turn) for p in pending] == [
        ("c2", "a", {"x": 1}, 3),
        ("c3", "b", {"y": 2}, 3),
    ]


def test_pending_calls_are_tenant_scoped_and_need_a_completed_turn(run_store, event_log, ag_ui, tenant, other_tenant):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    assert ag_ui.pending_client_tool_calls(tenant, conv["id"]) == []  # no turns
    run_id = _turn(run_store, tenant, conv["id"], status="running")
    _complete_with_pending(event_log, tenant, run_id, [("c1", "pick", {})], ["c1"])
    assert ag_ui.pending_client_tool_calls(tenant, conv["id"]) == []  # not completed yet
    run_store.set_status(tenant, run_id, "completed")
    assert len(ag_ui.pending_client_tool_calls(tenant, conv["id"])) == 1
    assert ag_ui.pending_client_tool_calls(other_tenant, conv["id"]) == []
    assert ag_ui.pending_client_tool_calls(tenant, str(uuid4())) == []


def test_failed_latest_turn_has_no_pending_calls(run_store, event_log, ag_ui, tenant):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    run_id = _turn(run_store, tenant, conv["id"], status="running")
    _complete_with_pending(event_log, tenant, run_id, [("c1", "pick", {})], ["c1"])
    run_store.set_status(tenant, run_id, "failed")
    assert ag_ui.pending_client_tool_calls(tenant, conv["id"]) == []


def test_pending_before_run_id_reads_the_turn_the_new_turn_answers(run_store, event_log, ag_ui, tenant):
    """The crash window: turn 4 (answering turn 3's client call) is created, then the API dies
    before the AG-UI row is written. The identical re-POST must still find turn 3's pending call."""
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    for _ in range(2):
        r = _turn(run_store, tenant, conv["id"], status="running")
        event_log.append(tenant, r, "end", "run_completed", {"output": "ok"})
        run_store.set_status(tenant, r, "completed")
    third = _turn(run_store, tenant, conv["id"], status="running")
    _complete_with_pending(event_log, tenant, third, [("c1", "pick", {"a": 1})], ["c1"])
    run_store.set_status(tenant, third, "completed")

    fourth = uuid4()
    # First POST: the turn does not exist yet; before_run_id unknown -> the latest turn (3).
    assert [p.tool_call_id for p in ag_ui.pending_client_tool_calls(tenant, conv["id"], before_run_id=str(fourth))] == ["c1"]
    run_store.create_turn(tenant, conv["id"], 1, "", fourth, client_tool_results=[{"tool_call_id": "c1"}])
    # ...crash: no ag_ui_runs row. The re-POST finds no AG-UI row and validates again.
    assert ag_ui.get_ag_ui_run(
        tenant, scope_kind="agent", scope_id="agent-1", external_thread_id="th", external_run_id="R2"
    ) is None
    assert ag_ui.pending_client_tool_calls(tenant, conv["id"]) == []  # the latest turn is now 4
    healed = ag_ui.pending_client_tool_calls(tenant, conv["id"], before_run_id=str(fourth))
    assert [(p.tool_call_id, p.turn, p.run_id) for p in healed] == [("c1", 3, third)]
    # Then the heal records the row; create_turn returned the existing turn.
    assert run_store.create_turn(tenant, conv["id"], 1, "", fourth) == (fourth, False)
    rec, created = _record(ag_ui, tenant, str(fourth), external_thread_id="th", external_run_id="R2")
    assert created and rec.run_id == str(fourth)
    # A run of another conversation (or another tenant) as before_run_id reads the latest turn.
    other, _ = run_store.create_conversation(tenant, "agent-1")
    other_run = _turn(run_store, tenant, other["id"], status="queued")
    assert ag_ui.pending_client_tool_calls(tenant, conv["id"], before_run_id=other_run) == []
    # before_run_id = turn 1: nothing before it.
    first_run = run_store.list_turns(tenant, conv["id"])[0]["id"]
    assert ag_ui.pending_client_tool_calls(tenant, conv["id"], before_run_id=first_run) == []


def _write(fp_seed="x", **overrides):
    values = dict(
        scope_kind="agent",
        scope_id="agent-1",
        external_thread_id="th",
        external_run_id="R1",
        fingerprint=_fp(fp_seed),
        input_message_ids=("u1",),
    )
    values.update(overrides)
    return AgUiRunWrite(**values)


def test_create_turn_writes_the_ag_ui_row_in_its_transaction(run_store, ag_ui, tenant):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    run_id = uuid4()
    assert run_store.create_turn(tenant, conv["id"], 1, "hi", run_id, ag_ui_run=_write("A")) == (run_id, True)
    rec = ag_ui.get_ag_ui_run(tenant, scope_kind="agent", scope_id="agent-1", external_thread_id="th", external_run_id="R1")
    assert rec.run_id == str(run_id) and rec.fingerprint == _fp("A") and rec.input_message_ids == ("u1",)
    # The same runId with another body: the turn exists, nothing is written, the creator's row stands.
    assert run_store.create_turn(tenant, conv["id"], 1, "other", run_id, ag_ui_run=_write("B")) == (run_id, False)
    again, created = _record(ag_ui, tenant, str(run_id), external_thread_id="th", external_run_id="R1", fingerprint=_fp("B"))
    assert not created and again.fingerprint == _fp("A")


@pytest.mark.parametrize(
    "write, error",
    [
        (lambda: _write(scope_id="agent-2"), AgUiScopeMismatch),
        (lambda: _write(scope_kind="workflow"), AgUiScopeMismatch),
        (lambda: _write(fingerprint="nothex"), ValueError),
        (lambda: _write(from_seq=-5), ValueError),
    ],
)
def test_create_turn_refuses_a_bad_ag_ui_run_and_writes_nothing(engine, run_store, tenant, write, error):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    with pytest.raises(error):
        run_store.create_turn(tenant, conv["id"], 1, "hi", uuid4(), ag_ui_run=write())
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM runs WHERE tenant_id = :t"), {"t": tenant}).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM ag_ui_runs WHERE tenant_id = :t"), {"t": tenant}).scalar_one() == 0


def test_concurrent_same_run_id_with_different_bodies_the_creator_wins(run_store, ag_ui, tenant):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    run_id = uuid4()
    barrier = threading.Barrier(6)
    results: list[tuple[int, bool]] = []
    lock = threading.Lock()

    def post(i: int) -> None:
        barrier.wait()
        _, created = run_store.create_turn(tenant, conv["id"], 1, f"body {i}", run_id, ag_ui_run=_write(str(i)))
        with lock:
            results.append((i, created))

    threads = [threading.Thread(target=post, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    creators = [i for i, created in results if created]
    assert len(results) == 6 and len(creators) == 1
    winner = creators[0]
    rec = ag_ui.get_ag_ui_run(tenant, scope_kind="agent", scope_id="agent-1", external_thread_id="th", external_run_id="R1")
    # Deterministic: the row is the creator's, and its run's input is the creator's body; every
    # loser sees a different fingerprint (the 409).
    assert rec.fingerprint == _fp(str(winner))
    assert run_store.get_run(tenant, run_id)["input"] == f"body {winner}"


def test_record_refuses_a_run_of_another_scope(run_store, ag_ui, tenant):
    conv, _ = run_store.create_conversation(tenant, "agent-1")
    agent_run = _turn(run_store, tenant, conv["id"], status="queued")
    wf_run = str(uuid4())
    run_store.create_workflow_run(tenant, "wf", 1, "{}", wf_run)
    for run_id, overrides in [
        (agent_run, {"scope_id": "agent-2"}),
        (agent_run, {"scope_kind": "workflow", "scope_id": "agent-1"}),
        (wf_run, {"scope_kind": "agent", "scope_id": "wf"}),
        (wf_run, {"scope_kind": "workflow", "scope_id": "other-wf"}),
    ]:
        with pytest.raises(AgUiScopeMismatch) as info:
            _record(ag_ui, tenant, run_id, **overrides)
        assert run_id in str(info.value)
    _record(ag_ui, tenant, wf_run, scope_kind="workflow", scope_id="wf")


def test_scope_mismatch_pickles():
    import pickle

    exc = pickle.loads(pickle.dumps(AgUiScopeMismatch("t", "r", "agent", "a")))
    assert str(exc) == "run r is not a run of agent a" and isinstance(exc, ValueError)


def test_concurrent_identical_conversation_creates_all_succeed(run_store, tenant):
    conv_id = str(uuid4())
    barrier = threading.Barrier(8)
    results: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def create() -> None:
        barrier.wait()
        try:
            _, created = run_store.create_conversation(
                tenant, "agent-1", conversation_id=conv_id, external_thread_id="th", resource_id="u1"
            )
            with lock:
                results.append(created)
        except BaseException as exc:  # pragma: no cover - the failure being guarded against
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and sorted(results) == [False] * 7 + [True]


# ---------------------------------------------------------------------- cross-tenant isolation

_SNAPSHOT = {
    "ag_ui_runs": "SELECT to_jsonb(x) FROM ag_ui_runs x WHERE tenant_id = :t ORDER BY scope_kind, scope_id, external_thread_id, external_run_id",
    "conversations": "SELECT to_jsonb(x) FROM conversations x WHERE tenant_id = :t ORDER BY id",
    "runs": "SELECT to_jsonb(x) FROM runs x WHERE tenant_id = :t ORDER BY id",
    "execution_events": "SELECT to_jsonb(x) FROM execution_events x WHERE tenant_id = :t ORDER BY run_id, seq",
}


def _snapshot(engine, *tenants):
    with engine.connect() as conn:
        return {t: {k: list(conn.execute(text(q), {"t": t}).scalars()) for k, q in _SNAPSHOT.items()} for t in tenants}


def _seed_world(run_store, event_log, tenant):
    """The identical shape in each tenant: agent-1's thread `th` with a turn pending call c1,
    recorded as AG-UI run R1."""
    conv, _ = run_store.create_conversation(tenant, "agent-1", external_thread_id="th")
    run_id = uuid4()
    run_store.create_turn(tenant, conv["id"], 1, "hi", run_id, ag_ui_run=_write())
    run_store.set_status(tenant, run_id, "running")
    _complete_with_pending(event_log, tenant, str(run_id), [("c1", "pick", {})], ["c1"])
    run_store.set_status(tenant, run_id, "completed")
    return conv["id"], str(run_id)


def test_tenant_b_calling_every_ag_ui_method_with_tenant_a_ids_sees_and_changes_nothing(
    engine, run_store, event_log, ag_ui, tenant, other_tenant
):
    a_conv, a_run = _seed_world(run_store, event_log, tenant)
    _seed_world(run_store, event_log, other_tenant)
    key = dict(scope_kind="agent", scope_id="agent-1", external_thread_id="th", external_run_id="R1")
    calls = {
        "get_ag_ui_run": lambda: ag_ui.get_ag_ui_run(other_tenant, **dict(key, external_run_id="R-a-only")),
        "record_ag_ui_run": lambda: ag_ui.record_ag_ui_run(
            other_tenant, **dict(key, external_run_id="R9"), run_id=a_run, fingerprint=_fp()
        ),
        "pending_client_tool_calls": lambda: ag_ui.pending_client_tool_calls(other_tenant, a_conv, before_run_id=a_run),
        "set_end_order": lambda: ag_ui.set_end_order(other_tenant, **dict(key, external_run_id="R-a-only"), end_order=5),
    }
    public = {n for n in dir(AgUiStore) if not n.startswith("_") and callable(getattr(AgUiStore, n))}
    assert public == set(calls)  # a new method must be added here

    # A-only key so B's get cannot hit B's own identical row.
    ag_ui.record_ag_ui_run(tenant, **dict(key, external_run_id="R-a-only"), run_id=a_run, fingerprint=_fp())
    before = _snapshot(engine, tenant, other_tenant)
    assert calls["get_ag_ui_run"]() is None
    with pytest.raises(RunNotFound):
        calls["record_ag_ui_run"]()
    assert calls["pending_client_tool_calls"]() == []
    from sqlalchemy.exc import NoResultFound

    with pytest.raises(NoResultFound):  # B cannot record an end on A's AG-UI run
        calls["set_end_order"]()
    # create_turn's AG-UI write: B cannot put a turn into A's conversation.
    from eap_run_store import ConversationNotFound

    with pytest.raises(ConversationNotFound):
        run_store.create_turn(other_tenant, a_conv, 1, "x", uuid4(), ag_ui_run=_write(external_run_id="R8"))
    assert _snapshot(engine, tenant, other_tenant) == before
    # Each tenant reads only its own identical row.
    assert ag_ui.get_ag_ui_run(tenant, **key).run_id == a_run
    assert ag_ui.get_ag_ui_run(other_tenant, **key).run_id != a_run
