"""The five functional tools of the kb source sync: kb.source.get, settings.connection.check, kb.source.sync,
workflow.run.wait and workflow.run.result (design: Public tool contracts).

The tools are called the way a tool step calls them (a RunContext over the worker's tool deps); the DBOS enqueue
of the sync is replaced by a recorder and the adapter is the fake catalogue."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic_ai import RunContext
from pydantic_ai.usage import RunUsage
from sqlalchemy import text

from eap_kb_sources import ConnectionCheck
from eap_kb_sources.fake import FakeCatalogAdapter
from eap_kb_sources.registry import register_adapter
from eap_run_store import KnowledgeStore, RunStore
from eap_run_store.kb_source_launch import tool_run_id
from eap_run_store.kb_sources import ConnectionStore, SourceStore, SyncStore
from eap_schemas.tools import TOOL_DECLARATIONS
from eap_secrets import load_keyring
from eap_worker import kb_source_tools as tools
from worker_tests.kb_ingest_helpers import cleanup_knowledge, create_kb

IDS = ("kb.source.get", "settings.connection.check", "kb.source.sync", "workflow.run.wait", "workflow.run.result")
SENTINEL = "sentinel-client-secret-VALUE-0002"
REFS = {"client_id": "secret:ti-id", "client_secret": "secret:ti-secret"}


class Adapter(FakeCatalogAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.result = ConnectionCheck(True)

    async def check_connection(self, connection, secrets):
        self.secret_values.append({k: secrets.resolve(r) for k, r in connection.secret_refs.items()})
        return self.result


@pytest.fixture
def env(worker, tenant, mcp_env, monkeypatch):
    adapter = Adapter()
    register_adapter("product_catalog", "fake", lambda: adapter)
    launched: list[dict[str, Any]] = []
    monkeypatch.setattr(tools, "_launch_kb_source_sync", lambda **kw: launched.append(kw))
    monkeypatch.setattr(tools, "POLL_SECONDS", 0.05)
    kb = create_kb(KnowledgeStore(worker.engine), tenant)
    conn = ConnectionStore(worker.engine).create(
        tenant, name="c", kind="oauth2_client_credentials", base_url="https://x.example", config={"provider": "fake"},
        credential_refs=REFS)
    source = SourceStore(worker.engine).create(
        tenant, knowledge_base_id=kb, connection_id=conn.id, name="parts", kind="product_catalog", provider="fake")
    other = f"t_{uuid.uuid4().hex[:10]}"
    yield SimpleNamespace(rt=worker, tenant=tenant, other=other, kb=kb, conn=conn, source=source, adapter=adapter,
                          launched=launched, runs=RunStore(worker.engine), syncs=SyncStore(worker.engine))
    cleanup_knowledge(worker.engine, tenant)
    with worker.engine.begin() as c:
        for t in (tenant, other):
            c.execute(text("DELETE FROM settings_connections WHERE tenant_id = :t"), {"t": t})
            c.execute(text("DELETE FROM tenant_secrets WHERE tenant_id = :t"), {"t": t})
            c.execute(text("DELETE FROM execution_events WHERE tenant_id = :t"), {"t": t})
            c.execute(text("DELETE FROM runs WHERE tenant_id = :t"), {"t": t})


def ctx(env, *, tenant: str | None = None, run_id: uuid.UUID | None = None, call: str = "call-1") -> RunContext:
    deps = env.rt.tool_deps_for(str(run_id or uuid.uuid4()), tenant or env.tenant)
    return RunContext(deps=deps, model=None, usage=RunUsage(), tool_call_id=call, tool_name="x")  # type: ignore[arg-type]


def call(name: str, c: RunContext, **kw: Any) -> Any:
    return asyncio.run(env_registry_get(c, name)(c, **kw))


def env_registry_get(c: RunContext, name: str):
    from eap_worker.runtime import get_runtime

    return get_runtime().registry.get(name)


def code(result: dict) -> str:
    return result["error"]["code"]


def mk_run(env, *, tenant=None, mode="full", parts=None, parent=None, status=None, report=None) -> uuid.UUID:
    tenant = tenant or env.tenant
    rid = uuid.uuid4()
    request = {"mode": mode, "part_numbers": parts or []}
    env.runs.create_kb_source_sync_run(tenant, rid, env.source.id, request)
    env.syncs.create(tenant, str(uuid.uuid4()), env.source.id, str(rid), mode, "h" * 64,
                     parent_run_id=str(parent) if parent else None)
    if status in ("completed", "failed", "needs_attention"):
        env.runs.mark_running(tenant, rid)
        env.runs.finish_with_end(tenant, rid, status, json.dumps(report or {}), None, end_data={"report": report or {}})
    elif status == "running":
        env.runs.mark_running(tenant, rid)
    return rid


# --------------------------------------------------------------------------------------- declarations


def test_the_five_tools_are_declared_and_registered(env):
    from eap_worker.runtime import get_runtime

    reg = get_runtime().registry
    for tool_id in IDS:
        assert tool_id in TOOL_DECLARATIONS and tool_id in reg.tool_ids()
    flags = {t: TOOL_DECLARATIONS[t].side_effect for t in IDS}
    assert flags == {"kb.source.get": False, "settings.connection.check": False, "kb.source.sync": True,
                     "workflow.run.wait": False, "workflow.run.result": False}
    assert reg.summary_for("kb.source.sync", {"source_id": "abc", "mode": "targeted"}) == \
        "Start a targeted sync of source 'abc'"


def test_input_models(env):
    from pydantic import ValidationError

    wait = TOOL_DECLARATIONS["workflow.run.wait"].input_model
    assert wait(run_id="r", wait_seconds=300).wait_seconds == 300
    for bad in (0, 301):
        with pytest.raises(ValidationError):
            wait(run_id="r", wait_seconds=bad)
    sync = TOOL_DECLARATIONS["kb.source.sync"].input_model
    assert sync(source_id="s", mode="full").part_numbers == []
    with pytest.raises(ValidationError):
        sync(source_id="s", mode="weekly")
    assert TOOL_DECLARATIONS["workflow.run.result"].input_model(run_id="r").require_success is False


# ---------------------------------------------------------------------------------------- kb.source.get


def test_source_get_is_tenant_scoped(env):
    got = call("kb.source.get", ctx(env), source_id=env.source.id)
    assert got["id"] == env.source.id and got["provider"] == "fake" and got["config"] == {}
    assert got["last_successful_sync_at"] is None and got["last_checkpoint_summary"] == {"segments": 0, "has_cursor": False}
    assert set(got) >= {"knowledge_base_id", "connection_id", "kind", "name", "enabled"}
    assert code(call("kb.source.get", ctx(env, tenant=env.other), source_id=env.source.id)) == "source_not_found"
    assert code(call("kb.source.get", ctx(env), source_id="not-a-uuid")) == "source_not_found"


# ------------------------------------------------------------------------------ settings.connection.check


def test_connection_check_codes_and_no_leak(env):
    from eap_run_store.secrets import SecretStore

    check = lambda cid=None, tenant=None: call("settings.connection.check", ctx(env, tenant=tenant),  # noqa: E731
                                               connection_id=cid or env.conn.id)
    assert check()["error_code"] == "secret_missing"
    store = SecretStore(env.rt.engine, load_keyring())
    store.put(env.tenant, "ti-id", "id-value", "t")
    store.put(env.tenant, "ti-secret", SENTINEL, "t")
    ok = check()
    assert ok["ok"] is True and ok["error_code"] is None and ok["checked_at"]
    assert SENTINEL not in json.dumps(ok) and env.adapter.secret_values[-1]["client_secret"] == SENTINEL
    env.adapter.result = ConnectionCheck(False, "connection_auth_failed")
    assert check()["error_code"] == "connection_auth_failed"
    stored = ConnectionStore(env.rt.engine).get(env.tenant, env.conn.id)
    assert stored.last_status == "error" and stored.last_error_code == "connection_auth_failed"
    assert code(check(tenant=env.other)) == "connection_not_found"
    assert code(check(cid=str(uuid.uuid4()))) == "connection_not_found"
    ConnectionStore(env.rt.engine).update(env.tenant, env.conn.id, enabled=False)
    assert check()["error_code"] == "connection_disabled"


def test_connection_check_require_ok_returns_an_error_object_when_not_ok(env):
    from eap_run_store.secrets import SecretStore

    run = lambda **kw: call("settings.connection.check", ctx(env), connection_id=env.conn.id, **kw)  # noqa: E731
    plain = run()
    assert plain["ok"] is False and plain["error_code"] == "secret_missing"  # without require_ok: unchanged
    assert run(require_ok=False)["error_code"] == "secret_missing"
    assert code(run(require_ok=True)) == "secret_missing"
    store = SecretStore(env.rt.engine, load_keyring())
    store.put(env.tenant, "ti-id", "id-value", "t")
    store.put(env.tenant, "ti-secret", SENTINEL, "t")
    ok = run(require_ok=True)
    assert ok["ok"] is True and "error" not in ok
    env.adapter.result = ConnectionCheck(False, "connection_auth_failed")
    failed = run(require_ok=True)
    assert set(failed) == {"error"} and failed["error"]["code"] == "connection_auth_failed"
    assert failed["error"]["message"] and SENTINEL not in json.dumps(failed)


# ------------------------------------------------------------------------------------------ kb.source.sync


def test_sync_starts_once_per_tool_call_with_a_deterministic_run_id(env):
    parent = uuid.uuid4()
    want = tool_run_id(env.tenant, env.source.id, str(parent), "call-1")
    c = ctx(env, run_id=parent)
    out = call("kb.source.sync", c, source_id=env.source.id, mode="targeted", part_numbers=["LM317"])
    assert out == {"run_id": str(want), "status": "queued"}
    again = call("kb.source.sync", ctx(env, run_id=parent), source_id=env.source.id, mode="targeted",
                 part_numbers=["LM317"])
    assert again == out and len(env.launched) == 1  # exactly once per (run, tool call)
    sync = env.syncs.get_by_run(env.tenant, str(want))
    assert sync.parent_run_id == str(parent) and sync.mode == "targeted"
    assert json.loads(env.launched[0]["request_json"]) == {"mode": "targeted", "part_numbers": ["LM317"]}
    other_call = call("kb.source.sync", ctx(env, run_id=parent, call="call-2"), source_id=env.source.id, mode="full")
    assert other_call["run_id"] != out["run_id"] and len(env.launched) == 2


def test_sync_restart_is_part_of_the_request_for_a_walk_only(env):
    assert TOOL_DECLARATIONS["kb.source.sync"].input_model(source_id="s", mode="full", restart=True).restart is True
    assert TOOL_DECLARATIONS["kb.source.sync"].input_model(source_id="s", mode="full").restart is False
    out = call("kb.source.sync", ctx(env, call="r1"), source_id=env.source.id, mode="full", restart=True)
    assert out["status"] == "queued"
    assert json.loads(env.launched[-1]["request_json"]) == {"mode": "full", "part_numbers": [], "restart": True}
    call("kb.source.sync", ctx(env, call="r2"), source_id=env.source.id, mode="full")
    assert "restart" not in json.loads(env.launched[-1]["request_json"])
    n = len(env.launched)
    refused = call("kb.source.sync", ctx(env, call="r3"), source_id=env.source.id, mode="targeted",
                   part_numbers=["LM317"], restart=True)
    assert code(refused) == "invalid_request" and len(env.launched) == n


def test_sync_refusals_are_stable_codes_and_start_nothing(env):
    sync = lambda mode="full", parts=(), tenant=None, source=None: call(  # noqa: E731
        "kb.source.sync", ctx(env, tenant=tenant, call=uuid.uuid4().hex), source_id=source or env.source.id, mode=mode,
        part_numbers=list(parts))
    assert code(sync(tenant=env.other)) == "source_not_found"
    assert code(sync(mode="full", parts=["x"])) == "part_numbers_not_allowed"
    assert code(sync(mode="targeted", parts=["bad part"])) == "invalid_request"
    assert code(sync(mode="targeted", parts=[f"P{i}" for i in range(101)])) == "invalid_request"
    SourceStore(env.rt.engine).update(env.tenant, env.source.id, enabled=False)
    assert code(sync()) == "source_disabled"
    assert env.launched == []


def test_sync_enqueue_failure_is_retryable(env, monkeypatch):
    parent = uuid.uuid4()
    n = {"calls": 0}

    def flaky(**kw):
        n["calls"] += 1
        if n["calls"] == 1:
            raise RuntimeError("postgresql://u:pw@db is down")
        env.launched.append(kw)

    monkeypatch.setattr(tools, "_launch_kb_source_sync", flaky)
    first = call("kb.source.sync", ctx(env, run_id=parent), source_id=env.source.id, mode="full")
    assert code(first) == "enqueue_failed" and "pw@db" not in json.dumps(first)
    second = call("kb.source.sync", ctx(env, run_id=parent), source_id=env.source.id, mode="full")
    assert second["status"] == "queued" and len(env.launched) == 1  # the same deterministic run, re-enqueued


# ------------------------------------------------------------------------------------------ workflow.run.wait


def test_wait_returns_when_the_run_ends(env):
    rid = mk_run(env, status="running")
    started = asyncio.new_event_loop()

    async def go():
        async def finish():
            await asyncio.sleep(0.2)
            await asyncio.to_thread(env.runs.finish_with_end, env.tenant, rid, "completed", "{}", None, end_data={"report": {}})
        task = asyncio.create_task(finish())
        c = ctx(env)
        out = await env_registry_get(c, "workflow.run.wait")(c, run_id=str(rid), wait_seconds=5)
        await task
        return out

    out = started.run_until_complete(go())
    assert out["terminal"] is True and out["status"] == "completed" and out["updated_at"]


def test_wait_times_out_with_the_current_status(env):
    rid = mk_run(env, status="running")
    t0 = datetime.now(timezone.utc)
    out = call("workflow.run.wait", ctx(env), run_id=str(rid), wait_seconds=1)
    assert out["terminal"] is False and out["status"] == "running"
    assert 0.9 <= (datetime.now(timezone.utc) - t0).total_seconds() < 4


def test_wait_returns_at_once_for_a_finished_run_and_hides_other_tenants(env):
    rid = mk_run(env, status="completed", report={"status": "completed"})
    assert call("workflow.run.wait", ctx(env), run_id=str(rid), wait_seconds=60)["terminal"] is True
    assert code(call("workflow.run.wait", ctx(env, tenant=env.other), run_id=str(rid), wait_seconds=1)) == "run_not_found"
    assert code(call("workflow.run.wait", ctx(env), run_id="nope", wait_seconds=1)) == "run_not_found"
    assert code(call("workflow.run.wait", ctx(env), run_id=str(uuid.uuid4()), wait_seconds=1)) == "run_not_found"


def cancel_parent(env, parent: uuid.UUID) -> None:
    """What POST /runs/{parent}/cancel records first: the parent's `cancel_requested` event."""
    env.rt.event_log.append(env.tenant, parent, "cancel", "cancel_requested", {"requested_by": "alice"})


def test_wait_returns_cancelled_at_once_when_the_parent_run_is_cancelled(env):
    parent = uuid.uuid4()
    env.rt.run_store.create_workflow_run(env.tenant, uuid.uuid4(), 1, "{}", parent)
    child = mk_run(env, status="running", parent=parent)
    cancel_parent(env, parent)
    t0 = datetime.now(timezone.utc)
    out = call("workflow.run.wait", ctx(env, run_id=parent), run_id=str(child), wait_seconds=60)
    assert (datetime.now(timezone.utc) - t0).total_seconds() < 2  # promptly, not after 60 s
    assert out["status"] == "cancelled" and out["terminal"] is True and out["updated_at"]


def test_wait_sees_a_cancel_that_arrives_while_it_waits(env):
    parent = uuid.uuid4()
    env.rt.run_store.create_workflow_run(env.tenant, uuid.uuid4(), 1, "{}", parent)
    child = mk_run(env, status="running", parent=parent)

    async def go():
        async def cancel():
            await asyncio.sleep(0.3)
            await asyncio.to_thread(cancel_parent, env, parent)
        task = asyncio.create_task(cancel())
        c = ctx(env, run_id=parent)
        out = await env_registry_get(c, "workflow.run.wait")(c, run_id=str(child), wait_seconds=30)
        await task
        return out

    t0 = datetime.now(timezone.utc)
    out = asyncio.new_event_loop().run_until_complete(go())
    assert out["status"] == "cancelled" and out["terminal"] is True
    assert (datetime.now(timezone.utc) - t0).total_seconds() < 3


def test_a_cancel_of_another_run_does_not_cancel_the_wait(env):
    parent, other_parent = uuid.uuid4(), uuid.uuid4()
    for wf in (parent, other_parent):
        env.rt.run_store.create_workflow_run(env.tenant, uuid.uuid4(), 1, "{}", wf)
    child = mk_run(env, status="running", parent=parent)
    cancel_parent(env, other_parent)
    out = call("workflow.run.wait", ctx(env, run_id=parent), run_id=str(child), wait_seconds=1)
    assert out["status"] == "running" and out["terminal"] is False


def test_result_reports_run_cancelled_when_the_parent_run_is_cancelled(env):
    parent = uuid.uuid4()
    env.rt.run_store.create_workflow_run(env.tenant, uuid.uuid4(), 1, "{}", parent)
    child = mk_run(env, status="running", parent=parent)
    cancel_parent(env, parent)
    # the child has not ended yet (the cancel is still being applied): the parent's cancel answers
    out = call("workflow.run.result", ctx(env, run_id=parent), run_id=str(child), require_success=True)
    assert code(out) == "run_cancelled"
    plain = call("workflow.run.result", ctx(env, run_id=parent), run_id=str(child))
    assert plain["status"] == "cancelled"
    # once the cancel ended the child (`end` run_failed run_cancelled, as cancel_run_tree writes it) it is the same
    env.rt.run_store.cancel_run_tree(env.tenant, child)
    assert code(call("workflow.run.result", ctx(env, run_id=parent), run_id=str(child), require_success=True)) == "run_cancelled"
    assert call("workflow.run.result", ctx(env, run_id=parent), run_id=str(child))["status"] == "cancelled"


# ---------------------------------------------------------------------------------------- workflow.run.result

REPORT = {"source_id": "s", "mode": "full", "status": "completed", "discovered": 5, "inserted": 3, "updated": 1,
          "unchanged": 1, "documents_discovered": 4, "documents_queued": 4, "failed": 0,
          "checkpoint": {"segments": 2}, "errors": [{"code": "not_found", "message": "m"}] * 60,
          "complete": True, "stop_reason": "complete", "calls_used": 9, "started_at": "x", "completed_at": "y"}


def test_result_is_the_bounded_report(env):
    rid = mk_run(env, status="completed", report=REPORT)
    out = call("workflow.run.result", ctx(env), run_id=str(rid))
    assert out["run_id"] == str(rid) and out["status"] == "completed" and out["source_id"] == env.source.id
    assert (out["discovered"], out["inserted"], out["updated"], out["unchanged"], out["documents_queued"],
            out["failed"]) == (5, 3, 1, 1, 4, 0)
    assert out["checkpoint"] == {"segments": 2} and len(out["errors"]) == 50 and out["mode"] == "full"
    assert len(json.dumps(out)) < 32 * 1024
    assert call("workflow.run.result", ctx(env), run_id=str(rid), require_success=True)["status"] == "completed"


def test_result_of_a_running_run_reports_progress(env):
    rid = mk_run(env, status="running")
    env.syncs.update_progress(env.tenant, str(rid), checkpoint={}, counters={"discovered": 2, "inserted": 2},
                              bounded_errors=[])
    out = call("workflow.run.result", ctx(env), run_id=str(rid))
    assert out["status"] == "running" and out["discovered"] == 2 and out["errors"] == []
    assert code(call("workflow.run.result", ctx(env), run_id=str(rid), require_success=True)) == "sync_not_finished"


@pytest.mark.parametrize("status,report,want", [
    ("failed", {**REPORT, "status": "failed", "error_code": "internal_error"}, "sync_failed"),
    ("needs_attention", {**REPORT, "status": "needs_attention", "error_code": "connection_auth_failed"}, "sync_needs_attention"),
    ("failed", {**REPORT, "status": "failed", "error_code": "run_cancelled"}, "run_cancelled"),
])
def test_require_success_maps_the_outcome_to_stable_codes(env, status, report, want):
    rid = mk_run(env, status=status, report=report)
    assert code(call("workflow.run.result", ctx(env), run_id=str(rid), require_success=True)) == want
    plain = call("workflow.run.result", ctx(env), run_id=str(rid))
    assert "error" not in plain and plain["status"] in ("failed", "needs_attention", "cancelled")


def test_result_is_tenant_scoped_and_limited_to_the_calling_workflows_children(env):
    rid = mk_run(env, status="completed", report=REPORT)
    assert code(call("workflow.run.result", ctx(env, tenant=env.other), run_id=str(rid))) == "run_not_found"
    assert code(call("workflow.run.result", ctx(env), run_id="nope")) == "run_not_found"
    # an agent run's id: not a sync run
    assert code(call("workflow.run.result", ctx(env), run_id=str(uuid.uuid4()))) == "run_not_found"

    wf_a, wf_b = uuid.uuid4(), uuid.uuid4()
    for wf in (wf_a, wf_b):
        env.rt.run_store.create_workflow_run(env.tenant, uuid.uuid4(), 1, "{}", wf)
    child = mk_run(env, status="completed", report=REPORT, parent=wf_a)
    mine = call("workflow.run.result", ctx(env, run_id=wf_a), run_id=str(child))
    assert mine["status"] == "completed"
    assert code(call("workflow.run.result", ctx(env, run_id=wf_b), run_id=str(child))) == "run_not_found"
    assert code(call("workflow.run.wait", ctx(env, run_id=wf_b), run_id=str(child), wait_seconds=1)) == "run_not_found"
    # a run started outside any workflow (no parent) is refused to a calling workflow, readable to an agent
    assert code(call("workflow.run.result", ctx(env, run_id=wf_a), run_id=str(rid))) == "run_not_found"
    assert call("workflow.run.result", ctx(env), run_id=str(rid))["status"] == "completed"


def test_the_report_carries_removed_budget_and_source_revision(env):
    rep = {**REPORT, "removed": 2, "budget": {"date": "2026-11-18", "used": 5}, "source_revision": "r9"}
    rid = mk_run(env, status="completed", report=rep)
    out = call("workflow.run.result", ctx(env), run_id=str(rid))
    assert (out["removed"], out["budget"], out["source_revision"]) == (2, {"date": "2026-11-18", "used": 5}, "r9")
    plain = call("workflow.run.result", ctx(env), run_id=str(mk_run(env, status="completed", report=REPORT)))
    assert "removed" not in plain and "budget" not in plain


def test_the_approval_summary_names_the_source(env):
    args = {"source_id": env.source.id, "mode": "incremental"}
    assert tools.approval_summary_text(env.rt.engine, env.tenant, args) == "Start a incremental sync of source 'parts'"
    assert tools.approval_summary_text(env.rt.engine, env.other, args) is None  # another tenant's source
    assert tools.approval_summary_text(env.rt.engine, env.tenant, {"source_id": "x", "mode": "full"}) is None
