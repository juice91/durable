"""RM.6 acceptance: the embedding service end to end, beyond the implementer's tests
(test_embeddings.py): fail-closed policy with no provider call, dimension errors, exactly-once usage
under DBOS step re-execution, cost arithmetic, unmeasured usage, ranking, query instructions, the
request body per provider, batching, no text in spans or events, and run_model_requests."""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from dataclasses import replace
from decimal import Decimal

import httpx2 as httpx
import pytest
from sqlalchemy import text

from eap_schemas.embedding_policy import EmbeddingDimensionMismatch, EmbeddingModelNotAllowed, EmbeddingModelPolicy
from eap_worker.config import WorkerSettings
from eap_worker.embeddings import EmbeddingFailed, embed_documents, embed_query, plan_requests, record_embedding
from eap_worker.models import hash_embedding
from eap_worker.pricing import embedding_usage_record

BASE = WorkerSettings(
    app_database_url="postgresql+psycopg://unused",
    dbos_system_database_url="postgresql://unused",
    dbos_system_schema="dbos",
    app_version="test",
    executor_id="test",
    allowed_tools=None,
    litellm_base_url=None,
    litellm_api_key=None,
)
IDS = dict(tenant_id="t_embed_acc", run_id=str(uuid.uuid4()))
M = "litellm:acc-embed"
TEXTS = ["one two", "three four", "five six", "seven eight", "nine ten"]


def settings(policy: str | None = None, instructions: dict | None = None, **kw) -> WorkerSettings:
    return replace(BASE, allowed_embedding_models=EmbeddingModelPolicy.parse(policy, instructions), **kw)


def gateway_settings(dims: int = 1024, instructions: dict | None = None, **kw) -> WorkerSettings:
    return settings(f"{M}={dims}", instructions, litellm_base_url="http://gw/v1", litellm_api_key="k", **kw)


def docs(s, texts, model_id="test:hash", key="embed:batch:0"):
    return asyncio.run(embed_documents(s, request_key=key, model_id=model_id, texts=texts, **IDS))


def query(s, q, model_id="test:hash", key="embed:recall:1"):
    return asyncio.run(embed_query(s, request_key=key, model_id=model_id, text=q, **IDS))


@pytest.fixture
def gw(monkeypatch):
    """OpenAI-compatible fake gateway behind the worker's AsyncOpenAI; logs every request body."""
    import openai

    st = {"bodies": [], "usage": True, "dims": None, "dims_by_call": {}, "count_off": False}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        n = len(st["bodies"])
        st["bodies"].append(body)
        dims = st["dims_by_call"].get(n) or st["dims"] or body.get("dimensions") or 8
        k = len(body["input"]) - (1 if st["count_off"] else 0)
        # vector i encodes the input's own number so order is checkable: [i-th word count, ...]
        data = [{"object": "embedding", "index": i, "embedding": [float(len(t))] + [0.0] * (dims - 1)}
                for i, t in enumerate(body["input"][:k])]
        out = {"object": "list", "model": "served", "data": data}
        if st["usage"]:
            out["usage"] = {"prompt_tokens": 3 * len(body["input"]), "total_tokens": 3 * len(body["input"])}
        return httpx.Response(200, json=out)

    real = openai.AsyncOpenAI
    monkeypatch.setattr(
        openai, "AsyncOpenAI",
        lambda **kw: real(**kw, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))),
    )
    return st


# ----------------------------------------------------------------------------- policy: no provider call


@pytest.mark.parametrize("policy,model_id", [
    (None, M),                              # default policy does not allow litellm:*
    (f"{M}=1024", "litellm:other"),         # not on the list
    (f"{M}=1024", "openai:text-embedding-3-small"),
])
def test_disallowed_model_makes_no_provider_call(gw, monkeypatch, policy, model_id):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-unused")
    s = replace(settings(policy), litellm_base_url="http://gw/v1", litellm_api_key="k")
    with pytest.raises(EmbeddingModelNotAllowed):
        docs(s, ["x"], model_id=model_id)
    with pytest.raises(EmbeddingModelNotAllowed):
        query(s, "x", model_id=model_id)
    assert gw["bodies"] == []  # nothing was sent anywhere


# ----------------------------------------------------------------------------- dimensions


def test_dimension_mismatch_reports_expected_and_actual_and_returns_no_call(gw):
    gw["dims_by_call"] = {1: 2048}  # the second provider request answers with the wrong width
    with pytest.raises(EmbeddingDimensionMismatch) as info:
        docs(gateway_settings(1024, embedding_max_batch_inputs=2), TEXTS, model_id=M)
    assert (info.value.expected, info.value.actual) == (1024, 2048)
    assert "1024" in str(info.value) and "2048" in str(info.value)
    assert len(gw["bodies"]) == 2  # stopped at the bad batch; no partial result escapes


def test_dimension_mismatch_on_a_scripted_embedder():
    with pytest.raises(EmbeddingDimensionMismatch):
        docs(settings("test:hash768=1536"), ["x"], model_id="test:hash768")


def test_wrong_vector_count_is_embedding_failed(gw):
    gw["count_off"] = True
    with pytest.raises(EmbeddingFailed, match="vectors for"):
        docs(gateway_settings(), ["a", "b"], model_id=M)


# ----------------------------------------------------------------------------- cost arithmetic


def test_cost_is_tokens_times_price_in_decimal():
    # test:hash is priced at USD 0.13 per million input tokens (config/model-prices.test.json)
    for tokens in (1, 7, 999_999, 123_456_789):
        row, data = embedding_usage_record(
            "k", model_id="test:hash", provider_model_name=None, input_tokens=tokens, usage_measured=True,
            inputs=1, dimensions=1536, input_type="document",
        )
        expected = (Decimal(tokens) * Decimal("0.13") / Decimal(1_000_000)).quantize(Decimal("1e-10"))
        assert isinstance(row["cost_usd"], Decimal) and row["cost_usd"] == expected
        assert data["cost_usd"] == format(expected, "f")
        assert row["output_tokens"] == 0
    # one token is exactly 0.00000013: no float drift
    assert embedding_usage_record(
        "k", model_id="test:hash", provider_model_name=None, input_tokens=1, usage_measured=True,
        inputs=1, dimensions=1536, input_type="document",
    )[0]["cost_usd"] == Decimal("0.0000001300")


def test_zero_priced_litellm_model_costs_zero_not_none(gw):
    call = docs(gateway_settings(), ["a b c", "d e"], model_id=M)
    assert call.usage["usage_measured"] is True and call.input_tokens == 6
    assert call.usage["cost_usd"] == Decimal(0) and call.usage["cost_usd"] is not None
    assert Decimal(call.event_data["cost_usd"]) == 0
    assert call.usage["operation"] == "embedding" and call.usage["model_id"] == M


def test_unmeasured_usage_has_no_cost_and_zero_tokens():
    call = docs(settings("test:hash-nousage=1536"), ["a longer text to embed"], model_id="test:hash-nousage")
    assert call.usage["usage_measured"] is False and call.event_data["usage_measured"] is False
    assert call.usage["input_tokens"] == 0 and call.usage["cost_usd"] is None
    assert call.event_data["cost_usd"] is None and call.usage["operation"] == "embedding"


def test_one_unmeasured_request_makes_the_whole_call_unmeasured(gw):
    gw["usage"] = False
    call = docs(gateway_settings(embedding_max_batch_inputs=2), TEXTS, model_id=M)
    assert call.requests == 3 and call.usage["usage_measured"] is False and call.usage["cost_usd"] is None


# ----------------------------------------------------------------------------- ranking


def test_hash_ranking_puts_passages_sharing_words_first():
    passages = [
        "the cat sat on the warm windowsill all afternoon",
        "quarterly revenue grew because of subscription renewals",
        "postgres vacuum reclaims dead tuples from heavily updated tables",
        "refund requests are processed within fourteen business days",
    ]
    vecs = docs(settings(), passages).vectors
    for q, want in [("when are subscription renewals revenue", 1), ("vacuum dead tuples postgres", 2),
                    ("refund requests processed days", 3), ("cat on the windowsill", 0)]:
        qv = query(settings(), q).vector
        scores = [sum(a * b for a, b in zip(qv, v)) for v in vecs]
        assert scores.index(max(scores)) == want, (q, scores)
        assert sorted(scores, reverse=True)[0] > sorted(scores, reverse=True)[1]


# ----------------------------------------------------------------------------- query instruction


def test_query_instruction_prefix_on_the_wire_for_queries_only(gw):
    s = gateway_settings(instructions={M: "Retrieve passages that answer the question"})
    query(s, "what is the refund window", model_id=M)
    docs(s, ["refunds take 14 days"], model_id=M)
    q_body, d_body = gw["bodies"]
    assert q_body["input"] == ["Instruct: Retrieve passages that answer the question\nQuery: what is the refund window"]
    assert d_body["input"] == ["refunds take 14 days"]  # documents never get the prefix


def test_no_instruction_configured_sends_the_query_unchanged(gw):
    query(gateway_settings(), "plain query", model_id=M)
    assert gw["bodies"][0]["input"] == ["plain query"]


# ----------------------------------------------------------------------------- dimensions parameter


def test_dimensions_is_sent_to_litellm_models(gw):
    docs(gateway_settings(1024), ["x"], model_id=M)
    query(gateway_settings(1024), "x", model_id=M)
    assert [b["dimensions"] for b in gw["bodies"]] == [1024, 1024]
    assert all(b["model"] == "acc-embed" for b in gw["bodies"])


def test_dimensions_is_not_part_of_test_embedders(monkeypatch):
    seen = []
    from eap_worker import embeddings

    real = embeddings._embedding_settings
    monkeypatch.setattr(embeddings, "_embedding_settings", lambda m: seen.append(real(m)) or seen[-1])
    docs(settings(), ["x"])
    assert seen == [None]  # no EmbeddingSettings(dimensions=...) for test:*


# ----------------------------------------------------------------------------- batching


def test_batches_respect_max_inputs_and_keep_vector_order(gw):
    texts = ["x" * n for n in range(1, 8)]  # distinct lengths: the fake gateway echoes len(text)
    call = docs(gateway_settings(embedding_max_batch_inputs=3, embedding_max_batch_tokens=10**6), texts, model_id=M)
    assert [len(b["input"]) for b in gw["bodies"]] == [3, 3, 1] and call.requests == 3
    assert [v[0] for v in call.vectors] == [float(n) for n in range(1, 8)]
    assert call.usage["input_tokens"] == 3 * 7  # usage of the three requests is summed into one record


def test_batches_respect_max_tokens_and_oversized_text_goes_alone(gw):
    texts = ["a" * 40, "b" * 40, "c" * 40, "d" * 400, "e" * 4]  # 10, 10, 10, 100, 1 tokens
    call = docs(gateway_settings(embedding_max_batch_inputs=100, embedding_max_batch_tokens=25), texts, model_id=M)
    assert [b["input"] for b in gw["bodies"]] == [texts[:2], texts[2:3], texts[3:4], texts[4:]]
    for b in gw["bodies"]:
        assert len(b["input"]) == 1 or sum(math.ceil(len(t) / 4) for t in b["input"]) <= 25
    assert [v[0] for v in call.vectors] == [40.0, 40.0, 40.0, 400.0, 4.0]


def test_plan_requests_covers_every_text_once_in_order():
    texts = ["x" * (i * 7 % 50 + 1) for i in range(60)]
    spans = plan_requests(texts, 8, 40)
    assert spans[0][0] == 0 and spans[-1][1] == len(texts)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))
    assert all(e - s <= 8 for s, e in spans) and plan_requests([], 8, 40) == []


# ----------------------------------------------------------------------------- no text in spans


def test_planted_secret_is_in_no_span_attribute_or_event(gw):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from eap_observability.agents import instrument_pydantic_ai, uninstrument_pydantic_ai

    secret = "PLANTED-SECRET-hunter2-xyzzy"
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    assert instrument_pydantic_ai(tracer_provider=provider) is True
    try:
        docs(settings(), [f"doc with {secret}"])
        query(settings(), f"query with {secret}")
        docs(gateway_settings(), [f"gateway doc {secret}"], model_id=M)
        query(gateway_settings(instructions={M: f"instr {secret}"}), f"gateway query {secret}", model_id=M)
    finally:
        uninstrument_pydantic_ai()
    spans = exporter.get_finished_spans()
    assert len(spans) >= 4
    dump = json.dumps(
        [{"name": s.name, "attrs": dict(s.attributes or {}),
          "events": [{"n": e.name, "a": dict(e.attributes or {})} for e in s.events]} for s in spans],
        default=str,
    )
    assert "PLANTED" not in dump and "hunter2" not in dump
    call = docs(settings(), [f"doc with {secret}"])
    assert "PLANTED" not in json.dumps(call.event_data, default=str) + repr(call.usage)


# ----------------------------------------------------------------------------- database (real Postgres)


def _make_run(worker, tenant) -> uuid.UUID:
    run_id = uuid.uuid4()
    worker.run_store.create_run(tenant, uuid.uuid4(), 1, "input", run_id)
    return run_id


def _rows(worker, run_id):
    with worker.engine.connect() as conn:
        usage = conn.execute(text(
            "SELECT request_key, operation, model_id, input_tokens, output_tokens, cost_usd, usage_measured "
            "FROM run_model_requests WHERE run_id = :r ORDER BY request_key"), {"r": str(run_id)}).fetchall()
        events = conn.execute(text(
            "SELECT event_key, type, data FROM execution_events WHERE run_id = :r ORDER BY seq"),
            {"r": str(run_id)}).fetchall()
    return usage, events


def test_embedding_request_appears_in_run_model_requests_with_a_cost(worker, tenant):
    run_id = _make_run(worker, tenant)
    call = asyncio.run(embed_documents(settings(), tenant_id=tenant, run_id=str(run_id), request_key="embed:batch:0",
                                       model_id="test:hash", texts=["alpha beta gamma", "delta"]))
    _, created = record_embedding(worker.event_log, tenant, str(run_id), call)
    assert created
    usage, events = _rows(worker, run_id)
    assert len(usage) == 1
    u = usage[0]
    assert (u.operation, u.model_id, u.request_key) == ("embedding", "test:hash", "embed:batch:0")
    assert u.input_tokens == call.input_tokens > 0 and u.output_tokens == 0 and u.usage_measured is True
    assert u.cost_usd == (Decimal(u.input_tokens) * Decimal("0.13") / Decimal(1_000_000)).quantize(Decimal("1e-10"))
    assert u.cost_usd > 0
    assert [(e.event_key, e.type) for e in events] == [("embed:batch:0", "embedding")]
    assert "alpha" not in json.dumps(events[0].data)


def test_embed_then_record_twice_with_one_request_key_writes_one_usage_row_and_one_event(worker, tenant):
    run_id = _make_run(worker, tenant)
    kw = dict(tenant_id=tenant, run_id=str(run_id), request_key="embed:retrieve:call_1", model_id="test:hash")
    for expected_created in (True, False, False):
        call = asyncio.run(embed_query(settings(), text="same query", **kw))
        _, created = record_embedding(worker.event_log, tenant, str(run_id), call)
        assert created is expected_created
    usage, events = _rows(worker, run_id)
    assert len(usage) == 1 and len(events) == 1
    # a different request key is a different call and is recorded on its own
    call = asyncio.run(embed_query(settings(), text="same query", **{**kw, "request_key": "embed:retrieve:call_2"}))
    assert record_embedding(worker.event_log, tenant, str(run_id), call)[1] is True
    usage, events = _rows(worker, run_id)
    assert [u.request_key for u in usage] == ["embed:retrieve:call_1", "embed:retrieve:call_2"] and len(events) == 2


def test_dbos_step_reexecution_records_usage_exactly_once(worker, tenant):
    """A real DBOS step that embeds and records, then fails once before its checkpoint: DBOS runs it
    again (a second provider call, the same request key) and the usage row and event stay single."""
    from dbos import DBOS

    run_id = _make_run(worker, tenant)
    attempts: list[int] = []

    @DBOS.step(name=f"acc.embed_step.{uuid.uuid4().hex}", retries_allowed=True, max_attempts=3,
               interval_seconds=0.01, backoff_rate=1.0)
    def embed_and_record() -> int:
        call = asyncio.run(embed_documents(settings(), tenant_id=tenant, run_id=str(run_id),
                                           request_key="embed:batch:0", model_id="test:hash",
                                           texts=["alpha beta", "gamma"]))
        record_embedding(worker.event_log, tenant, str(run_id), call)
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("crash after the commit, before the DBOS checkpoint")
        return call.input_tokens

    @DBOS.workflow(name=f"acc.embed_wf.{uuid.uuid4().hex}")
    def wf() -> int:
        return embed_and_record()

    assert wf() == math.ceil(len("alpha beta") / 4) + math.ceil(len("gamma") / 4)
    assert len(attempts) == 3  # the step really did run three times
    usage, events = _rows(worker, run_id)
    assert len(usage) == 1 and usage[0].operation == "embedding" and usage[0].request_key == "embed:batch:0"
    assert [(e.event_key, e.type) for e in events] == [("embed:batch:0", "embedding")]
