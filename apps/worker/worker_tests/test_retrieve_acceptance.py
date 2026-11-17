"""RM.11 acceptance tests of the retrieval runtime: what test_retrieve.py does not cover.

Isolation, spec-only settings, usage across a crash, caps and gone knowledge bases, the exact step
layout, no retrieved text outside the tool result, and a workflow's child agent run. In-process
worker (plus real worker subprocesses for the crash case); knowledge is seeded with test:hash.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from typing import Any

import pytest
from dbos import DBOSClient
from pydantic_ai import RunContext, ToolFailed
from pydantic_ai.usage import RunUsage
from sqlalchemy import text

from eap_run_store import KnowledgeStore
from eap_schemas import AgentSpec
from eap_schemas.knowledge import MAX_RETRIEVE_RESULT_BYTES
from eap_worker.models import hash_embedding
from eap_worker.platform_tools import (
    KB_GONE_ALL,
    KB_GONE_ONE,
    RETRIEVE_INSTRUCTION,
    RetrieveTool,
)
from worker_tests.conftest import (
    APP_VERSION,
    DBOS_DB_URL,
    enqueue_run,
    event_rows,
    ticket_spec,
    wait_terminal,
)
from worker_tests.test_conversation import start_turn, step_names
from worker_tests.test_crash_recovery import APP_VERSION as CRASH_APP_VERSION
from worker_tests.test_crash_recovery import WorkerProcess, _env, crash_schema  # noqa: F401 - fixture
from worker_tests.test_processors_runtime import BASELINE
from worker_tests.test_retrieve import (
    PRINTER,
    PRINTER_URL,
    by_type,
    ingest,
    kb_tenant,  # noqa: F401 - fixture
    make_kb,
    rag_spec,
    run,
    usage_rows,
)
from worker_tests.test_telemetry import _run_spans, _wait_for_run_span, telemetry  # noqa: F401 - fixture
from worker_tests.workflow_run_helpers import (
    agent_spec,
    cleanup_definitions,
    enqueue_workflow_run,
    publish_agent,
    publish_workflow,
)


@pytest.fixture(autouse=True)
def _env_embeddings(monkeypatch):
    monkeypatch.setenv("EAP_ALLOWED_EMBEDDING_MODELS", "test:*")


def tool_result(worker, run_id) -> dict[str, Any]:
    [result] = by_type(event_rows(worker.engine, run_id), "tool_result")
    return result


def drop_tenant_kbs(worker, tenant: str) -> None:
    with worker.engine.begin() as conn:
        conn.execute(text("DELETE FROM knowledge_bases WHERE tenant_id = :t"), {"t": tenant})


# --------------------------------------------------------------------------- isolation


def test_a_foreign_kb_id_in_a_spec_never_yields_the_other_tenants_chunks(worker, client, kb_tenant):
    other = f"t_{uuid.uuid4().hex[:10]}"
    try:
        theirs = make_kb(worker, other, name="Their manual")
        ingest(worker, other, theirs, "their.md", [("Paper jams", "ALPHASECRET paper jams tray two")])
        mine = make_kb(worker, kb_tenant, name="Our manual")
        ingest(worker, kb_tenant, mine, "ours.md", [("Paper jams", "Pull the jammed paper from tray two.")])
        # The forced spec carries both ids: only our chunks come back, and the definition names only ours.
        run_id, row = run(worker, client, kb_tenant, rag_spec({"id": theirs["id"]}, {"id": mine["id"]}),
                          "paper jams tray two")
        assert row["status"] == "completed" and row["output"].startswith("According to [1] Paper jams:")
        result = tool_result(worker, run_id)
        assert {s["kb_id"] for s in result["sources"]} == {mine["id"]}
        assert "ALPHASECRET" not in json.dumps(event_rows(worker.engine, run_id), default=str)
        # Naming their knowledge base (by name or by id) is outside the enum: refused before any search.
        for value in ("Their manual", theirs["id"], mine["id"]):
            prompt = json.dumps({"search_query": "paper jams", "knowledge_base": value})
            run_id, row = run(worker, client, kb_tenant, rag_spec({"id": mine["id"]}), prompt)
            assert row["status"] == "completed" and row["output"].startswith("Retrieve error:"), value
            assert "'Our manual'" in row["output"] and "ALPHASECRET" not in row["output"]
            assert tool_result(worker, run_id)["status"] == "error"
            assert by_type(event_rows(worker.engine, run_id), "embedding") == []
    finally:
        drop_tenant_kbs(worker, other)


def test_tenant_b_agent_with_tenant_a_kb_only_gets_kb_gone(worker, client, kb_tenant):
    other = f"t_{uuid.uuid4().hex[:10]}"
    try:
        theirs = make_kb(worker, other)
        ingest(worker, other, theirs, "printer.md", PRINTER)
        run_id, row = run(worker, client, kb_tenant, rag_spec({"id": theirs["id"]}), "toner cartridge")
        assert row["status"] == "completed" and row["output"] == f"Retrieve error: {KB_GONE_ALL}"
        result = tool_result(worker, run_id)
        assert result["status"] == "error" and result["content"] == KB_GONE_ALL and "sources" not in result
        assert by_type(event_rows(worker.engine, run_id), "embedding") == []
        assert [k for k, _ in usage_rows(worker.engine, run_id)] == ["model:0", "model:1"]
    finally:
        drop_tenant_kbs(worker, other)


def test_the_model_visible_schema_has_no_filter_tenant_or_top_k(worker, client, kb_tenant):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", PRINTER)
    spec = AgentSpec.model_validate(rag_spec({"id": kb["id"], "filter": {"lang": "zzfilter"}, "top_k": 2}))
    run_id = uuid.uuid4()
    worker.run_store.create_run(kb_tenant, uuid.uuid4(), 1, "q", run_id)
    ctx = RunContext(deps=worker.deps_for(str(run_id), kb_tenant, spec), model=None, usage=RunUsage(),
                     tool_call_id="c1")  # type: ignore[arg-type]
    tool = RetrieveTool(spec.knowledge_bases, lambda: KnowledgeStore(worker.engine))
    definition = asyncio.run(tool.definition(ctx))
    schema = definition.parameters_json_schema
    assert set(schema["properties"]) == {"search_query", "knowledge_base"}
    assert schema["additionalProperties"] is False and schema["required"] == ["search_query"]
    assert "zzfilter" not in json.dumps(dataclasses.asdict(definition), default=str)
    # The tool refuses an argument problem as a model-visible error naming the choices.
    with pytest.raises(ToolFailed, match="must be one of 'Printer manual'"):
        asyncio.run(tool.call({"search_query": "x", "knowledge_base": "Other"}, ctx, definition))
    with pytest.raises(ToolFailed, match="must not be empty"):
        asyncio.run(tool.call({"search_query": "  "}, ctx, definition))
    assert usage_rows(worker.engine, run_id) == []


# --------------------------------------------------------------------------- filters and settings


def _scores(worker, tenant, kb_id, query):
    vector = hash_embedding(query, 1536)
    hits = KnowledgeStore(worker.engine).search(tenant, [kb_id], vector, embedding_model="test:hash", top_k=10)
    return [h.score for h in hits]


@pytest.mark.parametrize("extra", [{"filter": {"lang": "de"}}, {"top_k": 50}, {"tenant_id": "other"},
                                   {"min_score": 0.0}, {"knowledge_base_id": "x"}])
def test_the_model_cannot_pass_filters_or_settings(worker, client, kb_tenant, extra):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "en.md", PRINTER, metadata={"lang": "en"})
    prompt = json.dumps({"search_query": "paper jams", **extra})
    run_id, row = run(worker, client, kb_tenant, rag_spec({"id": kb["id"]}), prompt)
    assert row["status"] == "completed" and row["output"].startswith("Retrieve error:")
    assert by_type(event_rows(worker.engine, run_id), "embedding") == []  # nothing was searched
    assert tool_result(worker, run_id)["status"] == "error" and "sources" not in tool_result(worker, run_id)


def test_the_spec_filter_top_k_and_min_score_apply(worker, client, kb_tenant):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "en.md", PRINTER, metadata={"lang": "en"})
    ingest(worker, kb_tenant, kb, "de.md", [("Papierstau", "Paper jams tray two Fach zwei")],
           metadata={"lang": "de"}, source_url="https://docs.example.test/de")
    ingest(worker, kb_tenant, kb, "fr.md", [("Bourrage", "Paper jams tray two bac deux")],
           metadata={"lang": "fr"}, source_url="https://docs.example.test/fr")
    query = "paper jams tray two"

    def sources(ref):
        run_id, row = run(worker, client, kb_tenant, rag_spec({"id": kb["id"], **ref}), query)
        assert row["status"] == "completed", row
        return tool_result(worker, run_id).get("sources", [])

    both = sources({"filter": {"lang": ["de", "fr"]}})  # a list is "any of"
    assert {s["title"] for s in both} == {"Papierstau", "Bourrage"}
    assert [s["title"] for s in sources({"filter": {"lang": "de"}})] == ["Papierstau"]
    assert sources({"filter": {"lang": "xx"}}) == []
    assert len(sources({"top_k": 2})) == 2 and len(sources({"top_k": 1})) == 1
    scores = _scores(worker, kb_tenant, kb["id"], query)
    assert len(scores) == 5 and scores == sorted(scores, reverse=True) and scores[1] > scores[2]
    cut = sources({"min_score": (scores[1] + scores[2]) / 2})
    assert len(cut) == 2 and all(s["score"] >= (scores[1] + scores[2]) / 2 for s in cut)
    assert [s["score"] for s in cut] == sorted((s["score"] for s in cut), reverse=True)
    assert [s["n"] for s in cut] == [1, 2]


def test_each_reference_keeps_its_own_settings_in_one_call(worker, client, kb_tenant):
    a = make_kb(worker, kb_tenant, name="A")
    b = make_kb(worker, kb_tenant, name="B")
    ingest(worker, kb_tenant, a, "a.md", PRINTER, metadata={"lang": "en"}, source_url="https://a.test")
    ingest(worker, kb_tenant, b, "b.md", [("One", "paper jams tray"), ("Two", "paper jams tray two"),
                                         ("Three", "paper jams")], metadata={"lang": "de"}, source_url="https://b.test")
    refs = [{"id": a["id"], "filter": {"lang": "de"}}, {"id": b["id"], "filter": {"lang": "de"}, "top_k": 2}]
    run_id, row = run(worker, client, kb_tenant, rag_spec(*refs), "paper jams tray two")
    assert row["status"] == "completed"
    srcs = tool_result(worker, run_id)["sources"]
    assert {s["kb_id"] for s in srcs} == {b["id"]} and len(srcs) == 2  # A's filter excludes everything; B's top_k=2
    # The same refs with the filter swapped: A alone matches.
    refs = [{"id": a["id"], "filter": {"lang": "en"}, "top_k": 1}, {"id": b["id"], "filter": {"lang": "en"}}]
    run_id, row = run(worker, client, kb_tenant, rag_spec(*refs), "paper jams tray two")
    assert {s["kb_id"] for s in tool_result(worker, run_id)["sources"]} == {a["id"]}


# --------------------------------------------------------------------------- caps and gone knowledge bases


def test_many_big_hits_are_capped_at_24_kib_with_matching_sources(worker, client, kb_tenant):
    kb = make_kb(worker, kb_tenant)
    sections = [(f"Part {i}", "paper jams tray two " + f"word{i} " * 1000) for i in range(1, 9)]  # ~7 KB each
    ingest(worker, kb_tenant, kb, "big.md", sections)
    run_id, row = run(worker, client, kb_tenant, rag_spec({"id": kb["id"], "top_k": 8}), "paper jams tray two")
    assert row["status"] == "completed" and row["output"].startswith("According to [1] Part ")
    result = tool_result(worker, run_id)
    content = result["content"]
    assert len(content.encode("utf-8")) <= MAX_RETRIEVE_RESULT_BYTES and content.endswith(RETRIEVE_INSTRUCTION)
    srcs = result["sources"]
    assert 1 <= len(srcs) < 8 and [s["n"] for s in srcs] == list(range(1, len(srcs) + 1))
    assert content.count("\n[") + 1 == len(srcs)  # one numbered block per recorded source
    assert all("content" not in s and "text" not in s for s in srcs)  # sources carry no section text
    assert len(json.dumps(result["sources"])) < 4000


def test_one_of_two_knowledge_bases_deleted_before_the_run_continues_with_the_other(worker, client, kb_tenant):
    keep = make_kb(worker, kb_tenant, name="Keep")
    gone = make_kb(worker, kb_tenant, name="Gone")
    ingest(worker, kb_tenant, keep, "k.md", PRINTER)
    ingest(worker, kb_tenant, gone, "g.md", [("Paper jams", "paper jams in the gone manual")], source_url="https://g.test")
    KnowledgeStore(worker.engine).delete_knowledge_base(kb_tenant, gone["id"])
    run_id, row = run(worker, client, kb_tenant, rag_spec({"id": gone["id"]}, {"id": keep["id"]}), "paper jams tray two")
    assert row["status"] == "completed" and row["output"] == f"According to [1] Paper jams: {PRINTER_URL}#paper-jams"
    assert {s["kb_id"] for s in tool_result(worker, run_id)["sources"]} == {keep["id"]}
    # Naming the deleted one: the definition no longer offers it, so the model gets the choices.
    prompt = json.dumps({"search_query": "paper jams", "knowledge_base": "Gone"})
    _, row = run(worker, client, kb_tenant, rag_spec({"id": gone["id"]}, {"id": keep["id"]}), prompt)
    assert row["status"] == "completed" and "'Keep'" in row["output"] and "'Gone'" not in row["output"]


def test_deleted_between_definition_and_call_gives_kb_gone_one_or_all(worker, kb_tenant):
    a = make_kb(worker, kb_tenant, name="Alpha")
    b = make_kb(worker, kb_tenant, name="Beta")
    for kb in (a, b):
        ingest(worker, kb_tenant, kb, "p.md", PRINTER)
    spec = AgentSpec.model_validate(rag_spec({"id": a["id"]}, {"id": b["id"]}))
    run_id = uuid.uuid4()
    worker.run_store.create_run(kb_tenant, uuid.uuid4(), 1, "q", run_id)
    ctx = RunContext(deps=worker.deps_for(str(run_id), kb_tenant, spec), model=None, usage=RunUsage(),
                     tool_call_id="call_g")  # type: ignore[arg-type]
    tool = RetrieveTool(spec.knowledge_bases, lambda: KnowledgeStore(worker.engine))
    definition = asyncio.run(tool.definition(ctx))  # the model saw both names
    KnowledgeStore(worker.engine).delete_knowledge_base(kb_tenant, b["id"])
    with pytest.raises(ToolFailed) as one:
        asyncio.run(tool.call({"search_query": "toner", "knowledge_base": "Beta"}, ctx, definition))
    assert str(one.value) == KB_GONE_ONE.format(name="Beta")
    res = asyncio.run(tool.call({"search_query": "toner"}, ctx, definition))  # Alpha still answers
    assert {s["kb_id"] for s in res.metadata["sources"]} == {a["id"]}
    KnowledgeStore(worker.engine).delete_knowledge_base(kb_tenant, a["id"])
    with pytest.raises(ToolFailed) as all_:
        asyncio.run(tool.call({"search_query": "toner"}, ctx, definition))
    assert str(all_.value) == KB_GONE_ALL
    assert [k for k, _ in usage_rows(worker.engine, run_id)] == ["embed:retrieve:call_g"]  # only the successful call


# --------------------------------------------------------------------------- step layout


def test_toolset_factory_is_unchanged_without_knowledge_bases(worker, kb_tenant):
    from pydantic_ai.toolsets import FunctionToolset

    from eap_worker.mcp import RunToolset
    from eap_worker.workflow import _toolset_for_run

    def build(spec_dict):
        spec = AgentSpec.model_validate(spec_dict)
        ctx = RunContext(deps=worker.deps_for(str(uuid.uuid4()), kb_tenant, spec), model=None, usage=RunUsage())
        return asyncio.run(_toolset_for_run(ctx))  # type: ignore[arg-type]

    plain = build(ticket_spec())
    # Still a FunctionToolset (no wrapper: same steps); a spec with tools resolves secret refs in its call.
    assert isinstance(plain, FunctionToolset) and not isinstance(plain, RunToolset)
    kb = str(uuid.uuid4())
    with_kb = build(rag_spec({"id": kb}))
    assert isinstance(with_kb, RunToolset)
    assert with_kb.platform is not None and with_kb.platform.names == {"retrieve"}
    assert with_kb.mcp is None


def test_step_names_of_runs_and_turns_with_knowledge_bases(worker, client, worker_settings, kb_tenant):
    schema = worker_settings.dbos_system_schema
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", PRINTER)
    # A run whose only tool call is retrieve: exactly the steps of a run with one built-in tool call.
    run_id, row = run(worker, client, kb_tenant, rag_spec({"id": kb["id"]}), "toner light")
    assert row["status"] == "completed"
    names = step_names(schema, run_id)
    assert names == BASELINE["ticket"]
    assert [n for n in names if "call_tool" in n] == ["eap-agent__dynamic_toolset__eap-spec-tools.call_tool"]
    assert not [n for n in names if "validate_args" in n or "retrieve" in n or "embed" in n]
    # A conversation turn adds only the history load, never a retrieval step.
    conv, _ = worker.run_store.create_conversation(kb_tenant, uuid.uuid4(), title="rag")
    turn_id = start_turn(client, worker.run_store, kb_tenant, conv["id"], "toner light", rag_spec({"id": kb["id"]}))
    assert wait_terminal(worker.run_store, kb_tenant, turn_id)["status"] == "completed"
    expected = BASELINE["ticket"][:2] + ["eap.load_history"] + BASELINE["ticket"][2:]
    assert step_names(schema, turn_id) == expected
    # No KB: the baselines are untouched, for a tool run and for a plain echo run.
    run_id, _ = run(worker, client, kb_tenant, ticket_spec(), "Laptop broken")
    assert step_names(schema, run_id) == BASELINE["ticket"]
    run_id, row = run(worker, client, kb_tenant, ticket_spec(tools=(), model="retrieve"), "hi")
    assert row["output"] == "Echo: hi" and step_names(schema, run_id) == BASELINE["echo"]


# --------------------------------------------------------------------------- crash and replay


def test_usage_is_recorded_once_across_a_crash_at_after_tool_call(worker, client, kb_tenant, crash_schema, tmp_path):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", PRINTER)
    spec = rag_spec({"id": kb["id"]})
    executor_id = f"retrieve-crash-{uuid.uuid4().hex[:8]}"
    first = second = dbos_client = None
    try:
        first = WorkerProcess(_env(crash_schema, executor_id, EAP_CRASH_AT="after_tool_call"), tmp_path / "w1.log")
        first.wait_ready()
        dbos_client = DBOSClient(system_database_url=DBOS_DB_URL, dbos_system_schema=crash_schema)
        run_id = enqueue_run(dbos_client, worker.run_store, tenant_id=kb_tenant, spec=spec,
                             input_text="wifi button pairing", app_version=CRASH_APP_VERSION)
        assert first.wait_exit() == 137, (tmp_path / "w1.log").read_text()

        # At the crash: the query was embedded and its usage recorded; the tool result was not.
        events = event_rows(worker.engine, run_id)
        [call] = by_type(events, "tool_call")
        key = f"embed:retrieve:{call['tool_call_id']}"
        assert [k for _, k, _, _ in events][-2:] == [f"tool:{call['tool_call_id']}:call", key]
        assert by_type(events, "tool_result") == []
        assert usage_rows(worker.engine, run_id) == [(key, "embedding"), ("model:0", "chat")]
        assert worker.run_store.get_run(kb_tenant, run_id)["status"] == "running"

        second = WorkerProcess(_env(crash_schema, executor_id), tmp_path / "w2.log")
        row = wait_terminal(worker.run_store, kb_tenant, run_id, timeout=90)
        assert row["status"] == "completed", (row["error"], (tmp_path / "w2.log").read_text())
        assert row["output"] == f"According to [1] Wifi setup: {PRINTER_URL}#wifi-setup"
        assert usage_rows(worker.engine, run_id) == [(key, "embedding"), ("model:0", "chat"), ("model:1", "chat")]
        events = event_rows(worker.engine, run_id)
        assert [e[0] for e in events] == list(range(len(events)))
        keys = [k for _, k, _, _ in events]
        assert len(keys) == len(set(keys)) and keys.count(key) == 1
        tid = call["tool_call_id"]
        assert keys == ["start", "model:0:final", f"tool:{tid}:call", key, f"tool:{tid}:result", "model:1:final", "end"]
        replayed = by_type(events, "tool_result")[0]

        # The recovered result is exactly what an uninterrupted run answers for the same query.
        ref_id, ref_row = run(worker, client, kb_tenant, spec, "wifi button pairing")
        assert ref_row["output"] == row["output"]
        reference = tool_result(worker, ref_id)
        assert replayed["content"] == reference["content"] and replayed["sources"] == reference["sources"]
        assert replayed["status"] == "ok" and replayed["content"].endswith(RETRIEVE_INSTRUCTION)
        with worker.engine.connect() as conn:
            total = conn.execute(text("SELECT count(*) FROM run_model_requests WHERE run_id = :r AND operation = 'embedding'"),
                                 {"r": str(run_id)}).scalar_one()
        assert total == 1
    finally:
        if first is not None:
            first.kill()
        if second is not None:
            second.stop()
        if dbos_client is not None:
            dbos_client.destroy()


# --------------------------------------------------------------------------- no leaks

SECRET = "ZEBRA-SECRET-7731"


def _all_telemetry(finished) -> str:
    spans, logs = finished()
    dump = repr([(s.name, dict(s.attributes or {}), [(e.name, dict(e.attributes or {})) for e in s.events]) for s in spans])
    return dump + repr([(r.body, dict(r.attributes or {})) for r in logs])


def test_retrieved_text_reaches_only_the_tool_result(worker, client, kb_tenant, telemetry):
    _, finished = telemetry
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", [("Paper jams", f"Open tray two. {SECRET} is the admin code.")])
    run_id, row = run(worker, client, kb_tenant, rag_spec({"id": kb["id"]}), "How do I clear paper jams?")
    assert row["status"] == "completed" and row["output"].startswith("According to [1] Paper jams:")
    spans, _ = _wait_for_run_span(finished, run_id)
    assert _run_spans(spans, run_id)
    assert SECRET not in _all_telemetry(finished)  # spans, span events, log records
    assert SECRET not in json.dumps({k: v for k, v in row.items() if k not in ("created_at", "updated_at")}, default=str)
    assert SECRET not in json.dumps(usage_rows(worker.engine, run_id))
    holders = [(k, t) for _, k, t, d in event_rows(worker.engine, run_id) if SECRET in json.dumps(d)]
    assert len(holders) == 1 and holders[0][1] == "tool_result", holders  # the tool result and nothing else
    assert SECRET not in json.dumps(tool_result(worker, run_id)["sources"])


def test_a_failing_embedding_call_leaks_neither_query_nor_chunks(worker, client, kb_tenant, telemetry, monkeypatch):
    from pydantic_ai.exceptions import ModelHTTPError

    import eap_worker.platform_tools as platform_tools

    _, finished = telemetry
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", [("Paper jams", f"Open tray two. {SECRET}")])

    async def boom(settings, **kw):
        raise ModelHTTPError(400, "test:hash", body=f"rejected input {kw['text']} {SECRET}")

    monkeypatch.setattr(platform_tools, "embed_query", boom)
    run_id, row = run(worker, client, kb_tenant, rag_spec({"id": kb["id"]}), "query-with-SECRETQ words")
    assert row["status"] == "completed" and row["output"].startswith("Retrieve error:")
    result = tool_result(worker, run_id)
    assert result["status"] == "error" and result["content"] == platform_tools.RETRIEVAL_FAILED
    _wait_for_run_span(finished, run_id)
    telemetry_dump = _all_telemetry(finished)
    assert SECRET not in telemetry_dump and "rejected input" not in telemetry_dump  # no provider error text
    assert SECRET not in json.dumps(event_rows(worker.engine, run_id), default=str)
    assert by_type(event_rows(worker.engine, run_id), "embedding") == []


# --------------------------------------------------------------------------- workflow child agent runs


@pytest.fixture
def defs(worker, kb_tenant):
    yield
    cleanup_definitions(worker.engine, kb_tenant)


def test_an_agent_step_of_a_workflow_can_retrieve(worker, client, kb_tenant, defs):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", PRINTER)
    agent_id = publish_agent(worker.engine, kb_tenant, {**agent_spec(model="retrieve"),
                                                       "knowledge_bases": [{"id": kb["id"], "top_k": 2}]})
    spec = {
        "name": "Ask the manual",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        "start": "ask",
        "steps": [{"id": "ask", "type": "agent", "agent_id": agent_id, "input": {"prompt": {"$ref": "input.q"}}}],
    }
    workflow_id, version_id = publish_workflow(worker.engine, kb_tenant, spec)
    run_id = enqueue_workflow_run(client, worker.run_store, tenant_id=kb_tenant, workflow_id=workflow_id,
                                  version_id=version_id, input_value={"q": "How do I clear a paper jam in tray two?"},
                                  app_version=APP_VERSION)
    parent = wait_terminal(worker.run_store, kb_tenant, run_id)
    assert parent["status"] == "completed", parent["error"]
    assert parent["output"] == json.dumps(f"According to [1] Paper jams: {PRINTER_URL}#paper-jams")
    child_id = event_rows(worker.engine, run_id)[1][3]["child_run_id"]
    events = event_rows(worker.engine, child_id)
    [call] = by_type(events, "tool_call")
    assert call["tool_name"] == "retrieve"
    result = tool_result(worker, child_id)
    assert result["status"] == "ok" and result["content"].endswith(RETRIEVE_INSTRUCTION)
    assert len(result["sources"]) == 2 and result["sources"][0]["kb_id"] == kb["id"]  # the agent's top_k
    assert (f"embed:retrieve:{call['tool_call_id']}", "embedding") in usage_rows(worker.engine, child_id)
    assert by_type(event_rows(worker.engine, run_id), "embedding") == []  # nothing on the parent


# --------------------------------------------------------------------------- knowledge.search results


def test_knowledge_search_orders_by_score_and_flags_truncated_content(worker, client, kb_tenant):
    kb = make_kb(worker, kb_tenant)
    long_body = "paper jams tray two " + "filler " * 900  # > 4000 characters
    ingest(worker, kb_tenant, kb, "doc.md", [("Exact", "paper jams tray two"), ("Long", long_body),
                                            ("Far", "toner cartridge orange light")])
    run_id = uuid.uuid4()
    input_json = json.dumps({"knowledge_base_id": kb["id"], "query": "paper jams tray two", "top_k": 3},
                            sort_keys=True, separators=(",", ":"))
    worker.run_store.create_tool_run(kb_tenant, "knowledge.search", input_json, run_id, knowledge_base_id=kb["id"])
    client.enqueue({"queue_name": "eap-agent-runs", "workflow_name": "eap.tool_run", "workflow_id": str(run_id),
                    "app_version": APP_VERSION}, str(run_id), kb_tenant, "knowledge.search", input_json)
    row = wait_terminal(worker.run_store, kb_tenant, run_id)
    assert row["status"] == "completed", row
    hits = json.loads(row["output"])["hits"]
    assert [h["title"] for h in hits] == ["Exact", "Long", "Far"]
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True) and [h["n"] for h in hits] == [1, 2, 3]
    assert [h["content_truncated"] for h in hits] == [False, True, False]
    assert len(hits[1]["content"]) == 4000 and hits[0]["content"] == "paper jams tray two"
    result = json.loads(row["output"])
    assert len(result["text"].encode("utf-8")) <= MAX_RETRIEVE_RESULT_BYTES
    assert result["text"].endswith(RETRIEVE_INSTRUCTION)
