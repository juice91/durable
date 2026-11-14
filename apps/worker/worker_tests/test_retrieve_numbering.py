"""Run-wide source numbers for `retrieve`, and the reindex race of its search (milestone browser
check fixes).

1. Several retrieve calls in one turn (a model's parallel calls, or calls in later responses) used to
   number their sources from [1] each, so `[1]` named several sources and Studio linked the wrong
   one. Each call now continues from the sources the run's earlier retrieve calls returned
   (eap_worker.platform_tools.retrieve_offset): deterministic, from the run's own messages and, for
   earlier calls of the same response, the per-execution RetrieveTally the workflow-side
   RetrieveNumbering capability fills from each call's (checkpointed) result.
2. A knowledge base whose embedding model switches between the query embedding and the search was
   silently skipped (an empty result). The search now reads the models in the same snapshot as the
   chunks (KnowledgeStore.search_snapshot) and searches a moved knowledge base again under its new
   model.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

import pytest
from pydantic_ai import RunContext, ToolFailed
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturn,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.usage import RunUsage

import eap_worker.platform_tools as platform_tools
from eap_run_store import KnowledgeStore
from eap_schemas import AgentSpec
from eap_worker.models import hash_embedding
from eap_worker.platform_tools import (
    SOURCE_NUMBERING,
    RetrieveNumbering,
    RetrieveTool,
    SearchTarget,
    format_results,
    retrieve_offset,
    search_knowledge,
)
from eap_worker.runtime import RetrieveTally
from worker_tests.conftest import event_rows
from worker_tests.test_conversation import step_names
from worker_tests.test_processors_runtime import BASELINE
from worker_tests.test_retrieve import PRINTER, by_type, hit, ingest, kb_tenant, make_kb, rag_spec, run, usage_rows  # noqa: F401

BILLING_URL = "https://docs.example.test/billing"
BILLING = [
    ("Refunds", "Refunds take five business days to reach your card."),
    ("API limits", "The pro plan allows ten thousand API calls per day."),
    ("Invoices", "Invoices are emailed monthly to the account owner."),
]
SOURCE_LINE = re.compile(r"^\[(\d+)\] (.*) — (\S+)$", re.MULTILINE)
RUN = "run-1"


# --------------------------------------------------------------------------- unit


def test_format_results_numbers_from_the_offset():
    text, sources = format_results([hit(1), hit(2)], offset=3)
    assert [s["n"] for s in sources] == [4, 5]
    assert all(s["numbering"] == SOURCE_NUMBERING == "run" for s in sources)
    assert [int(n) for n, _, _ in SOURCE_LINE.findall(text)] == [4, 5]
    # Default: from 1, as before.
    assert [s["n"] for s in format_results([hit(1)])[1]] == [1]


def _call(call_id: str) -> ToolCallPart:
    return ToolCallPart("retrieve", {"search_query": call_id}, tool_call_id=call_id)


def _return(call_id: str, n: int, outcome: str = "success") -> ToolReturnPart:
    return ToolReturnPart(
        "retrieve", f"{n} sources", tool_call_id=call_id, outcome=outcome,  # type: ignore[arg-type]
        metadata={"sources": [{"n": i + 1} for i in range(n)]},
    )


def _messages() -> list[Any]:
    """An earlier turn's history (another run id) with 5 sources, then this run: response 1 with two
    retrieve calls (a: 2 sources, b: failed) and another tool, response 2 with calls c and d."""
    history = [
        ModelRequest(parts=[UserPromptPart("earlier")], run_id="old"),
        ModelResponse(parts=[_call("h")], run_id="old"),
        ModelRequest(parts=[_return("h", 5)], run_id="old"),
        ModelResponse(parts=[TextPart("earlier answer [1]")], run_id="old"),
    ]
    return history + [
        ModelRequest(parts=[UserPromptPart("now")], run_id=RUN),
        ModelResponse(parts=[_call("a"), ToolCallPart("tickets__create", {}, tool_call_id="t"), _call("b")], run_id=RUN),
        ModelRequest(parts=[_return("a", 2), _return("b", 4, outcome="failed")], run_id=RUN),
        ModelResponse(parts=[_call("c"), _call("d"), _call("e")], run_id=RUN),
    ]


def test_retrieve_offset_counts_the_runs_earlier_calls_in_call_order():
    messages = _messages()
    tally = RetrieveTally({"c": 3, "d": 1})
    assert retrieve_offset(messages, RUN, "a", tally) == 0  # the earlier turn's 5 never count
    assert retrieve_offset(messages, RUN, "b", tally) == 2
    assert retrieve_offset(messages, RUN, "c", tally) == 2  # b failed: 0 sources
    assert retrieve_offset(messages, RUN, "d", tally) == 5  # c from the tally (same response)
    assert retrieve_offset(messages, RUN, "e", tally) == 6
    # Pure: the same inputs, the same answer (a re-executed step recomputes it identically).
    assert retrieve_offset(messages, RUN, "d", RetrieveTally({"c": 3})) == 5
    # Not found: every call of the run counts.
    assert retrieve_offset(messages, RUN, "zzz", tally) == 6
    # No run id (outside an agent run): every message counts.
    assert retrieve_offset(messages, None, "a", tally) == 5


def test_retrieve_offset_logs_a_missing_earlier_result(caplog):
    messages = _messages()
    with caplog.at_level("WARNING", logger="eap_worker.platform_tools"):
        assert retrieve_offset(messages, RUN, "d", RetrieveTally()) == 2
    assert "no result recorded for an earlier call c" in caplog.text


def test_numbering_capability_records_counts_and_errors():
    cap = RetrieveNumbering()

    class Deps:
        retrieve_tally = RetrieveTally()

    ctx = RunContext(deps=Deps(), model=None, usage=RunUsage())  # type: ignore[arg-type]

    async def ok(_args):
        return ToolReturn(return_value="x", metadata={"sources": [{"n": 1}, {"n": 2}]})

    async def fails(_args):
        raise ToolFailed("nope")

    async def other(_args):
        return "ticket"

    async def go() -> None:
        assert (await cap.wrap_tool_execute(ctx, call=_call("a"), tool_def=None, args={}, handler=ok)).return_value == "x"  # type: ignore[arg-type]
        with pytest.raises(ToolFailed):
            await cap.wrap_tool_execute(ctx, call=_call("b"), tool_def=None, args={}, handler=fails)  # type: ignore[arg-type]
        await cap.wrap_tool_execute(
            ctx, call=ToolCallPart("tickets__create", {}, tool_call_id="t"), tool_def=None, args={}, handler=other  # type: ignore[arg-type]
        )

    asyncio.run(go())
    assert Deps.retrieve_tally.counts == {"a": 2, "b": 0}


# --------------------------------------------------------------------------- end to end


def _two_docs(worker, tenant) -> dict:
    kb = make_kb(worker, tenant, name="Help center")
    ingest(worker, tenant, kb, "billing.md", BILLING, source_url=BILLING_URL)
    ingest(worker, tenant, kb, "printer.md", PRINTER)
    return kb


def test_retrieve_calls_of_one_turn_number_their_sources_across_the_turn(worker, client, worker_settings, kb_tenant):
    """The browser-check repro: two parallel retrieve calls (refunds, API limits), then a third in a
    later response. Every number names one source, the model text and the structured sources agree,
    and retrieval still adds no DBOS step."""
    kb = _two_docs(worker, kb_tenant)
    spec = rag_spec({"id": kb["id"], "top_k": 2}, model="retrieve-multi")
    rounds = [["how many days do refunds take", "pro plan API calls per day"], ["toner light blinks"]]
    run_id, row = run(worker, client, kb_tenant, spec, json.dumps({"rounds": rounds}))
    assert row["status"] == "completed", row

    events = event_rows(worker.engine, run_id)
    results = by_type(events, "tool_result")
    assert [[s["n"] for s in r["sources"]] for r in results] == [[1, 2], [3, 4], [5, 6]]
    assert all(s["numbering"] == "run" for r in results for s in r["sources"])
    # The model saw exactly those numbers, in its own text.
    for r in results:
        assert [int(n) for n, _, _ in SOURCE_LINE.findall(r["content"])] == [s["n"] for s in r["sources"]]
    # Ranking-independent (HNSW over the shared index is approximate): only the numbers and the
    # number -> source mapping are asserted, never which section ranked first.
    by_n = {s["n"]: s for r in results for s in r["sources"]}
    assert sorted(by_n) == [1, 2, 3, 4, 5, 6]
    for r in results:  # each header in the text names the source with that number
        for n, title, ref in SOURCE_LINE.findall(r["content"]):
            assert (by_n[int(n)]["title"], by_n[int(n)]["ref"]) == (title, ref)
    # Each [n] the model wrote resolves to the source it read under that number.
    cited = re.findall(r"\[(\d+)\] ([^:;]+): (\S+?)(?:;|$)", row["output"])
    assert [int(n) for n, _, _ in cited] == [1, 2, 3, 4, 5, 6]
    for n, title, ref in cited:
        assert (by_n[int(n)]["title"], by_n[int(n)]["ref"]) == (title, ref)

    # No new step: the numbering hook runs in the workflow, around the call_tool steps.
    names = step_names(worker_settings.dbos_system_schema, run_id)
    assert set(names) <= set(BASELINE["ticket"]), names
    assert names.count("eap-agent__dynamic_toolset__eap-spec-tools.call_tool") == 3



def test_a_reexecuted_second_call_of_a_batch_gets_the_same_numbers(worker, kb_tenant):
    """The call step of the second call re-executed (a crash before its checkpoint): its offset comes
    from the checkpointed first result (the tally a replay rebuilds), so the numbers are the same."""
    kb = _two_docs(worker, kb_tenant)
    spec = AgentSpec.model_validate(rag_spec({"id": kb["id"], "top_k": 2}))
    run_id = uuid.uuid4()
    worker.run_store.create_run(kb_tenant, uuid.uuid4(), 1, "q", run_id)
    tool = RetrieveTool(spec.knowledge_bases, lambda: KnowledgeStore(worker.engine))
    messages = [
        ModelRequest(parts=[UserPromptPart("q")], run_id=RUN),
        ModelResponse(parts=[_call("c1"), _call("c2")], run_id=RUN),
    ]

    def ctx_for(call_id: str, deps) -> RunContext:
        return RunContext(deps=deps, model=None, usage=RunUsage(), tool_call_id=call_id, messages=messages, run_id=RUN)  # type: ignore[arg-type]

    async def execution() -> tuple[Any, Any]:
        """One workflow execution: fresh deps (a fresh tally), c1 then c2 through the hook."""
        deps = worker.deps_for(str(run_id), kb_tenant, spec)
        cap = RetrieveNumbering()
        tool_def = await tool.definition(ctx_for("c1", deps))
        results = []
        for call_id, query in (("c1", "refunds take days"), ("c2", "pro plan API calls")):
            ctx = ctx_for(call_id, deps)

            async def handler(args, ctx=ctx):
                return await tool.call(args, ctx, tool_def)

            results.append(await cap.wrap_tool_execute(
                ctx, call=_call(call_id), tool_def=tool_def, args={"search_query": query}, handler=handler,
            ))
        return results[0], results[1]

    first_c1, first_c2 = asyncio.run(execution())
    again_c1, again_c2 = asyncio.run(execution())
    assert [s["n"] for s in first_c1.metadata["sources"]] == [1, 2]
    assert [s["n"] for s in first_c2.metadata["sources"]] == [3, 4]
    assert (again_c1.return_value, again_c1.metadata) == (first_c1.return_value, first_c1.metadata)
    assert (again_c2.return_value, again_c2.metadata) == (first_c2.return_value, first_c2.metadata)
    assert first_c2.return_value.startswith("[3] ")  # ranking-independent: the number, not the section
    # Usage once per call (request keys are per tool call id).
    assert usage_rows(worker.engine, run_id) == [("embed:retrieve:c1", "embedding"), ("embed:retrieve:c2", "embedding")]


# --------------------------------------------------------------------------- reindex race


def _switch_to_768(worker, tenant: str, kb_id: str) -> None:
    """A complete reindex of `kb_id` to test:hash768: new generation, embedded, switched."""
    store = KnowledgeStore(worker.engine)
    pending = store.start_reindex(tenant, kb_id, embedding_model="test:hash768", dimensions=768)
    g = pending["pending_generation"]
    store.copy_generation_chunks(tenant, kb_id, g)
    batch = store.next_unembedded_batch(tenant, kb_id, g)
    store.write_embeddings(tenant, kb_id, g, {c.id: hash_embedding(c.embed_text, 768) for c in batch},
                           embedding_model="test:hash768")
    assert store.finalize_reindex(tenant, kb_id, g, embedding_model="test:hash768").status == "switched"


def test_a_model_switch_between_embedding_and_search_is_searched_again(worker, kb_tenant, monkeypatch):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", PRINTER)
    store = KnowledgeStore(worker.engine)
    run_id = uuid.uuid4()
    worker.run_store.create_run(kb_tenant, uuid.uuid4(), 1, "q", run_id)
    real = platform_tools._embed_query_retrying
    embedded: list[tuple[str, str]] = []

    async def racing(settings, **kw):
        call = await real(settings, **kw)
        embedded.append((kw["model_id"], kw["request_key"]))
        if len(embedded) == 1:  # the switch lands right after the query was embedded
            await asyncio.to_thread(_switch_to_768, worker, kb_tenant, kb["id"])
        return call

    monkeypatch.setattr(platform_tools, "_embed_query_retrying", racing)
    from eap_worker.runtime import get_runtime

    rt = get_runtime()
    hits = asyncio.run(search_knowledge(
        settings=rt.settings, store=store, event_log=rt.event_log, tenant_id=kb_tenant, run_id=str(run_id),
        tool_call_id="call_race", query="toner light", targets=[SearchTarget(kb["id"], 3)],
        models={kb["id"]: "test:hash"},
    ))
    assert hits, "the moved knowledge base must be searched again, not answered empty"
    assert {h.generation for h in hits} == {2} and {h.kb_id for h in hits} == {kb["id"]}
    assert embedded == [("test:hash", "embed:retrieve:call_race"), ("test:hash768", "embed:retrieve:call_race:test:hash768")]
    assert usage_rows(worker.engine, run_id) == [
        ("embed:retrieve:call_race", "embedding"), ("embed:retrieve:call_race:test:hash768", "embedding"),
    ]


def test_search_snapshot_reports_a_moved_knowledge_base(worker, kb_tenant):
    kb = make_kb(worker, kb_tenant)
    ingest(worker, kb_tenant, kb, "printer.md", PRINTER)
    store = KnowledgeStore(worker.engine)
    query = hash_embedding("toner light", 1536)
    before = store.search_snapshot(kb_tenant, [kb["id"]], query, embedding_model="test:hash", top_k=3)
    assert before.hits and before.moved == {}
    _switch_to_768(worker, kb_tenant, kb["id"])
    after = store.search_snapshot(kb_tenant, [kb["id"]], query, embedding_model="test:hash", top_k=3)
    assert after.hits == [] and after.moved == {kb["id"]: "test:hash768"}
    # search() keeps its contract: no rows for a knowledge base under another model.
    assert store.search(kb_tenant, [kb["id"]], query, embedding_model="test:hash", top_k=3) == []
    # Another tenant's knowledge base is never reported.
    assert store.search_snapshot(str(uuid.uuid4()), [kb["id"]], query, embedding_model="test:hash", top_k=3).moved == {}


def test_a_failed_retry_keeps_the_other_knowledge_bases_hits(worker, kb_tenant, monkeypatch):
    """The moved knowledge base's new model cannot embed (e.g. no longer allowed): the retry is best
    effort, so the call still answers with the knowledge base that did not move."""
    from eap_schemas import EmbeddingModelNotAllowed

    stays = make_kb(worker, kb_tenant, name="Billing")
    ingest(worker, kb_tenant, stays, "billing.md", BILLING, source_url=BILLING_URL)
    moves = make_kb(worker, kb_tenant, name="Printer")
    ingest(worker, kb_tenant, moves, "printer.md", PRINTER)
    run_id = uuid.uuid4()
    worker.run_store.create_run(kb_tenant, uuid.uuid4(), 1, "q", run_id)
    real = platform_tools._embed_query_retrying
    seen: list[str] = []

    async def racing(settings, **kw):
        seen.append(kw["model_id"])
        if kw["model_id"] == "test:hash768":
            raise EmbeddingModelNotAllowed("test:hash768")
        call = await real(settings, **kw)
        await asyncio.to_thread(_switch_to_768, worker, kb_tenant, moves["id"])
        return call

    monkeypatch.setattr(platform_tools, "_embed_query_retrying", racing)
    from eap_worker.runtime import get_runtime

    rt = get_runtime()
    hits = asyncio.run(search_knowledge(
        settings=rt.settings, store=KnowledgeStore(worker.engine), event_log=rt.event_log, tenant_id=kb_tenant,
        run_id=str(run_id), tool_call_id="call_fail", query="refunds take days",
        targets=[SearchTarget(stays["id"], 3), SearchTarget(moves["id"], 3)],
        models={stays["id"]: "test:hash", moves["id"]: "test:hash"},
    ))
    assert seen == ["test:hash", "test:hash768"]
    assert hits and {h.kb_id for h in hits} == {stays["id"]}
