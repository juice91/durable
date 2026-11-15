"""AGUI.4 acceptance on REAL run event logs: agent turns run by the in-process worker with the
scripted test models (plain, tool, structured, failure, needs_attention, retrieve with citations,
memory). For each log: native SSE equals the frozen old engine from every Last-Event-ID offset;
the AG-UI translation passes the 1.0 schema and ordering invariants, fresh and from every offset
(no duplicates, no gaps)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "api"))

from api_tests.agui4_harness import agui_chunks, assert_resume_is_a_suffix, conformance, legacy_chunks, native_chunks  # noqa: E402
from api_tests.test_agui4_translate_acceptance import invariants  # noqa: E402
from eap_api.ag_ui.conformance import parse_frames  # noqa: E402
from eap_schemas import RunEvent  # noqa: E402
from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal  # noqa: E402
from worker_tests.test_agent_run import _unsafe_side_effect  # noqa: E402
from worker_tests.test_working_memory import conversation, mem_tenant, memory_spec, turn  # noqa: E402,F401 - fixture
from worker_tests.test_retrieve import PRINTER, ingest, kb_tenant, make_kb, rag_spec  # noqa: E402,F401 - fixture

TS = datetime(2026, 11, 15, tzinfo=timezone.utc)


def collect(worker, client, tenant, spec: dict[str, Any], prompt: str) -> tuple[str, list[RunEvent], dict[str, Any]]:
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec, input_text=prompt,
                         app_version=APP_VERSION)
    row = wait_terminal(worker.run_store, tenant, run_id)
    rows = event_rows(worker.engine, run_id)
    return str(run_id), [RunEvent(run_id=run_id, seq=s, type=t, data=d, ts=TS) for s, _k, t, d in rows], row


def check_log(run_id: str, events: list[RunEvent], expect: str) -> list[dict[str, Any]]:
    """Native byte identity from every offset; AG-UI conformance fresh and from every offset."""
    end = next(e.seq for e in events if e.type in {"run_completed", "run_failed"})
    for after in range(-1, end + 2):
        assert native_chunks(run_id, events, after) == legacy_chunks(run_id, events, after), f"native after {after}"
    full = agui_chunks(run_id, events)
    out = conformance(full)
    invariants(out)
    assert out[-1]["type"] == expect
    ids = [f.id for f in parse_frames(full) if f.id]
    assert len(ids) == len(set(ids))
    for n in range(-1, end):
        tools = [e.data["tool_call_id"] for e in events if e.seq <= n and "tool_call_id" in e.data]
        conformance(agui_chunks(run_id, events, resume_after=n), continuation=True, run_id="agui-run",
                    known_tool_calls=tools)
        assert_resume_is_a_suffix(run_id, events, full, n)
    return out


def test_plain_chat(worker, client, tenant):
    run_id, events, row = collect(worker, client, tenant, ticket_spec(tools=()), "hello corpus")
    out = check_log(run_id, events, "RUN_FINISHED")
    assert any(e["type"] == "TEXT_MESSAGE_CONTENT" and "Echo: hello corpus" in e["delta"] for e in out)


def test_tool_call_run(worker, client, tenant):
    run_id, events, row = collect(worker, client, tenant, ticket_spec(), "printer broken")
    out = check_log(run_id, events, "RUN_FINISHED")
    assert {"TOOL_CALL_START", "TOOL_CALL_RESULT"} <= {e["type"] for e in out}


def test_structured_output_run(worker, client, tenant):
    from worker_tests.test_step_layout_ac import SCHEMA

    spec = ticket_spec(tools=(), model="structured")
    spec["output"] = {"json_schema": SCHEMA, "mode": "tool"}
    run_id, events, row = collect(worker, client, tenant, spec, "rate this")
    out = check_log(run_id, events, "RUN_FINISHED")
    completed = next(e for e in events if e.type == "run_completed")
    assert out[-1]["result"] == completed.data["output_json"] and "final_result" not in str(out)


@pytest.mark.parametrize("model", ["fail-503", "fail-400"])
def test_failed_runs(worker, client, tenant, model):
    run_id, events, row = collect(worker, client, tenant, ticket_spec(tools=(), model=model), "go")
    assert row["status"] == "failed"
    out = check_log(run_id, events, "RUN_ERROR")
    assert out[-1]["message"] and "scripted failure" not in out[-1]["message"]


def test_needs_attention_run(worker, client, tenant, monkeypatch):
    if "test.unsafe" not in worker.registry.tool_ids():
        worker.registry.register("test.unsafe", _unsafe_side_effect, side_effect=True, external_idempotent=False)
    monkeypatch.setitem(worker.__dict__, "allowed_tools", frozenset({"test.unsafe"}))
    run_id, events, row = collect(worker, client, tenant, ticket_spec(tools=("test.unsafe",), model="first-tool"), "go")
    assert row["status"] == "needs_attention"
    out = check_log(run_id, events, "RUN_ERROR")
    assert out[-1]["code"] == "needs_attention"


def test_retrieve_with_citations(worker, client, kb_tenant):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "manual.md", PRINTER)
    run_id, events, row = collect(worker, client, kb_tenant, rag_spec({"id": kb["id"]}), "How do I fix paper jams in tray two?")
    out = check_log(run_id, events, "RUN_FINISHED")
    assert any(e["type"] == "TOOL_CALL_START" and e["toolCallName"] == "retrieve" for e in out)
    # The native sources metadata (and embeddings) must never be forwarded as a key on any AG-UI event;
    # the retrieve tool's own result text may mention the word "sources" (D9: plain TOOL_CALL_RESULT).
    def keys(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield k
                yield from keys(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from keys(v)
    all_keys = set(keys(out))
    assert "sources" not in all_keys and "embedding" not in all_keys and "vector" not in all_keys


def test_memory_turn(worker, client, mem_tenant):
    conv = conversation(worker, mem_tenant)
    run_id, row = turn(worker, client, mem_tenant, conv, "remember name=Ana", memory_spec(working={}))
    assert row["status"] == "completed"
    events = [RunEvent(run_id=run_id, seq=s, type=t, data=d, ts=TS) for s, _k, t, d in event_rows(worker.engine, run_id)]
    out = check_log(str(run_id), events, "RUN_FINISHED")
    assert "memory_updated" not in str(out)
