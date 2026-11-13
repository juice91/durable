"""RM.6 embedding service: policy, dimensions, batching, usage records, query instructions, the
test:hash embedders, the OpenAI-compatible client (mocked transport), and idempotent recording."""

from __future__ import annotations

import asyncio
import json
import math
import pickle
import uuid
from dataclasses import replace
from decimal import Decimal

import httpx2 as httpx
import pytest
from sqlalchemy import text

from eap_schemas.embedding_policy import EmbeddingDimensionMismatch, EmbeddingModelNotAllowed, EmbeddingModelPolicy
from eap_worker import embeddings, models
from eap_worker.config import WorkerSettings
from eap_worker.embeddings import EmbeddingFailed, embed_documents, embed_query, plan_requests, record_embedding
from eap_worker.models import hash_embedding

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
IDS = dict(tenant_id="t_embed", run_id=str(uuid.uuid4()))


def settings(policy: str | None = None, instructions: dict | None = None, **kw) -> WorkerSettings:
    return replace(BASE, allowed_embedding_models=EmbeddingModelPolicy.parse(policy, instructions), **kw)


def docs(s: WorkerSettings, texts, model_id="test:hash", key="embed:batch:0"):
    return asyncio.run(embed_documents(s, request_key=key, model_id=model_id, texts=texts, **IDS))


def query(s: WorkerSettings, q, model_id="test:hash", key="embed:recall:1"):
    return asyncio.run(embed_query(s, request_key=key, model_id=model_id, text=q, **IDS))


def cos(a, b) -> float:
    return sum(x * y for x, y in zip(a, b))


# ----------------------------------------------------------------------------- test:hash


def test_hash_embedding_is_deterministic_normalised_and_ranks_shared_words():
    a = hash_embedding("Refunds are issued within 14 days of the return", 1536)
    assert a == hash_embedding("Refunds are issued within 14 days of the return", 1536)
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0)
    assert len(hash_embedding("x", 768)) == 768
    assert hash_embedding("", 1536)[0] == 1.0  # no words: a unit vector, never zero

    call = docs(settings(), [
        "Refunds are issued within 14 days of the return",
        "Our office is closed on public holidays",
        "Shipping takes three to five business days",
    ])
    q = query(settings(), "how many days until refunds are issued").vector
    scores = [cos(q, v) for v in call.vectors]
    assert scores[0] == max(scores) and scores[0] > scores[1]


def test_hash768_dimensions():
    call = docs(settings(), ["a b c"], model_id="test:hash768")
    assert call.dimensions == 768 and len(call.vectors[0]) == 768


# ----------------------------------------------------------------------------- policy and dimensions


def test_disallowed_model_fails_closed_before_any_call(monkeypatch):
    def boom(name):
        raise AssertionError("no embedder may be built for a disallowed model")

    monkeypatch.setattr(embeddings, "scripted_embedding_model", boom)
    with pytest.raises(EmbeddingModelNotAllowed):
        docs(settings("litellm:other=1024"), ["x"])
    with pytest.raises(EmbeddingModelNotAllowed):
        docs(settings(), ["x"], model_id="litellm:Qwen3-VL-Embedding-8B-mlx-8bit")
    # allowed by a wildcard but no known dimensions
    with pytest.raises(EmbeddingModelNotAllowed, match="unknown"):
        docs(settings(), ["x"], model_id="test:unknown")


def test_dimension_mismatch_and_errors_pickle():
    with pytest.raises(EmbeddingDimensionMismatch) as info:
        docs(settings("test:hash=768"), ["x"])
    assert (info.value.expected, info.value.actual) == (768, 1536)
    for err in (info.value, EmbeddingFailed("litellm:m", "bad answer")):
        again = pickle.loads(pickle.dumps(err))
        assert type(again) is type(err) and str(again) == str(err)


def test_unknown_scripted_embedder_fails():
    with pytest.raises(EmbeddingFailed):
        docs(settings("test:nope=1536"), ["x"], model_id="test:nope")


# ----------------------------------------------------------------------------- usage and cost


def test_usage_record_cost_and_event_have_no_text():
    secret = "the launch code is swordfish"
    call = docs(settings(), [secret, "second text here"])
    tokens = math.ceil(len(secret) / 4) + math.ceil(len("second text here") / 4)
    u = call.usage
    assert u["request_key"] == "embed:batch:0" and call.event_key == "embed:batch:0"
    assert u["operation"] == "embedding" and u["model_id"] == "test:hash"
    assert u["input_tokens"] == tokens and u["output_tokens"] == 0 and u["usage_measured"] is True
    # model-prices.test.json: test:hash at USD 0.13 per million input tokens
    assert u["cost_usd"] == (Decimal(tokens) * Decimal("0.13") / Decimal(1_000_000)).quantize(Decimal("1e-10"))
    assert u["price_source"] == "override"
    assert call.event_type == "embedding"
    assert call.event_data["inputs"] == 2 and call.event_data["input_tokens"] == tokens
    assert call.event_data["cost_usd"] == format(u["cost_usd"], "f")
    assert "swordfish" not in json.dumps(call.event_data) + repr(u)
    assert pickle.loads(pickle.dumps(call)) == call  # checkpointable


def test_usage_measured_false_when_provider_omits_usage():
    call = docs(settings("test:hash-nousage=1536"), ["some text"], model_id="test:hash-nousage")
    assert call.usage_measured is False and call.input_tokens == 0
    assert call.usage["cost_usd"] is None and call.event_data["cost_usd"] is None


# ----------------------------------------------------------------------------- batching and instructions


def test_plan_requests_caps_inputs_and_tokens():
    assert plan_requests(["a"] * 5, 2, 100) == [(0, 2), (2, 4), (4, 5)]
    # 40 chars = 10 tokens each; cap 25 tokens -> 2 per request
    assert plan_requests(["x" * 40] * 3, 10, 25) == [(0, 2), (2, 3)]
    # one text over the token cap goes alone
    assert plan_requests(["x" * 400, "y"], 10, 25) == [(0, 1), (1, 2)]


def test_batched_call_keeps_order_and_sums_usage():
    texts = [f"text number {i}" for i in range(5)]
    call = docs(settings(embedding_max_batch_inputs=2), texts)
    assert call.requests == 3 and call.event_data["requests"] == 3
    assert list(call.vectors) == [tuple(hash_embedding(t, 1536)) for t in texts]
    assert call.input_tokens == sum(math.ceil(len(t) / 4) for t in texts)


def test_query_instruction_applies_to_queries_only():
    s = settings(None, {"test:hash": "Given a question, retrieve passages"})
    assert query(s, "refund window").vector == tuple(
        hash_embedding("Instruct: Given a question, retrieve passages\nQuery: refund window", 1536)
    )
    assert docs(s, ["refund window"]).vectors[0] == tuple(hash_embedding("refund window", 1536))
    assert query(settings(), "refund window").vector == tuple(hash_embedding("refund window", 1536))


def test_empty_input_is_rejected():
    with pytest.raises(ValueError):
        docs(settings(), [])
    with pytest.raises(ValueError):
        docs(settings(), "a string, not a list")


# ----------------------------------------------------------------------------- OpenAI-compatible client


@pytest.fixture
def fake_gateway(monkeypatch):
    """Route the worker's AsyncOpenAI through an httpx MockTransport; returns the request log and a
    setter for the response."""
    import openai

    state = {"requests": [], "status": 200, "usage": True, "dims": None, "indices": None, "vector": None}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        state["requests"].append({"body": body, "auth": request.headers.get("authorization"), "url": str(request.url)})
        if state["status"] != 200:
            return httpx.Response(state["status"], json={"error": {"message": "boom"}})
        dims = state["dims"] or body.get("dimensions") or 8
        # vector i is e_i (distinct per input) unless overridden; `indices` relabels/reorders the data
        data = [{"object": "embedding", "index": i,
                 "embedding": state["vector"] or [1.0 if j == i % dims else 0.0 for j in range(dims)]}
                for i in range(len(body["input"]))]
        if state["indices"] is not None:
            data = [{**data[i], "index": idx} for i, idx in state["indices"]]
        payload = {"object": "list", "model": "served-name", "data": data}
        if state["usage"]:
            payload["usage"] = {"prompt_tokens": 7 * len(body["input"]), "total_tokens": 7 * len(body["input"])}
        # json.dumps (allow_nan) so a test can send NaN / Infinity like a broken gateway would
        return httpx.Response(200, content=json.dumps(payload).encode(), headers={"content-type": "application/json"})

    real = openai.AsyncOpenAI

    def patched(**kw):
        state["client_kwargs"] = {k: v for k, v in kw.items() if k != "api_key"}
        return real(**kw, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(openai, "AsyncOpenAI", patched)
    return state


QWEN = "litellm:Qwen3-VL-Embedding-8B-mlx-8bit"


def qwen_settings(**kw) -> WorkerSettings:
    return settings(
        f"{QWEN}=1024", {QWEN: "Given a question, retrieve passages"},
        litellm_base_url="http://chat-gateway/v1", litellm_api_key="chat-key",
        litellm_embedding_base_url="http://embed-gateway/v1", litellm_embedding_api_key="embed-key", **kw,
    )


def test_litellm_sends_dimensions_uses_embedding_key_and_no_retries(fake_gateway):
    call = docs(qwen_settings(), ["doc one", "doc two"], model_id=QWEN)
    req = fake_gateway["requests"][0]
    assert req["body"]["dimensions"] == 1024 and req["body"]["model"] == "Qwen3-VL-Embedding-8B-mlx-8bit"
    assert req["body"]["input"] == ["doc one", "doc two"]
    assert req["url"].startswith("http://embed-gateway/v1") and req["auth"] == "Bearer embed-key"
    assert fake_gateway["client_kwargs"]["max_retries"] == 0
    assert call.usage["input_tokens"] == 14 and call.usage["usage_measured"] is True
    assert call.usage["provider_model_name"] == "served-name"
    assert call.usage["cost_usd"] == 0  # litellm:* at 0

    query(qwen_settings(), "what is the refund window", model_id=QWEN)
    assert fake_gateway["requests"][1]["body"]["input"] == [
        "Instruct: Given a question, retrieve passages\nQuery: what is the refund window"
    ]

    fake_gateway["status"] = 503
    before = len(fake_gateway["requests"])
    with pytest.raises(Exception):
        docs(qwen_settings(), ["x"], model_id=QWEN)
    assert len(fake_gateway["requests"]) == before + 1  # transport retries are off


def test_litellm_falls_back_to_chat_gateway_settings(fake_gateway):
    s = replace(qwen_settings(), litellm_embedding_base_url=None, litellm_embedding_api_key=None)
    docs(s, ["x"], model_id=QWEN)
    req = fake_gateway["requests"][0]
    assert req["url"].startswith("http://chat-gateway/v1") and req["auth"] == "Bearer chat-key"


def test_litellm_missing_usage_and_wrong_dimensions(fake_gateway):
    fake_gateway["usage"] = False
    call = docs(qwen_settings(), ["x"], model_id=QWEN)
    assert call.usage_measured is False and call.usage["cost_usd"] is None
    fake_gateway["dims"] = 4096  # a gateway that ignores `dimensions`
    with pytest.raises(EmbeddingDimensionMismatch):
        docs(qwen_settings(), ["x"], model_id=QWEN)


def test_litellm_without_credentials_fails():
    with pytest.raises(EmbeddingFailed, match="BASE_URL"):
        docs(settings(f"{QWEN}=1024"), ["x"], model_id=QWEN)


# ----------------------------------------------------------------------------- spans


def test_embedding_spans_carry_no_text():
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from eap_observability.agents import instrument_pydantic_ai, uninstrument_pydantic_ai

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    assert instrument_pydantic_ai(tracer_provider=provider) is True
    try:
        docs(settings(), ["the launch code is swordfish"])
    finally:
        uninstrument_pydantic_ai()
    spans = exporter.get_finished_spans()
    assert spans, "an embeddings span is expected"
    dump = json.dumps([{**dict(s.attributes or {}), "name": s.name} for s in spans], default=str)
    assert "embeddings" in dump and "swordfish" not in dump


# ----------------------------------------------------------------------------- recording (database)


def test_record_embedding_once_per_request_key(worker, tenant):
    run_id = uuid.uuid4()
    worker.run_store.create_run(tenant, uuid.uuid4(), 1, "input", run_id)
    s = settings()
    first = asyncio.run(embed_documents(s, tenant_id=tenant, run_id=str(run_id), request_key="embed:batch:0",
                                        model_id="test:hash", texts=["alpha beta", "gamma"]))
    _, created = record_embedding(worker.event_log, tenant, str(run_id), first)
    assert created
    # step re-execution: a new provider call, the same request key -> nothing written twice
    again = asyncio.run(embed_documents(s, tenant_id=tenant, run_id=str(run_id), request_key="embed:batch:0",
                                        model_id="test:hash", texts=["alpha beta", "gamma"]))
    _, created = record_embedding(worker.event_log, tenant, str(run_id), again)
    assert not created
    with worker.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT request_key, operation, input_tokens, output_tokens, cost_usd, usage_measured "
            "FROM run_model_requests WHERE run_id = :r"), {"r": str(run_id)}).fetchall()
        events = conn.execute(text(
            "SELECT event_key, type, data FROM execution_events WHERE run_id = :r"), {"r": str(run_id)}).fetchall()
    assert len(rows) == 1 and rows[0].operation == "embedding" and rows[0].request_key == "embed:batch:0"
    assert rows[0].input_tokens == first.input_tokens and rows[0].output_tokens == 0 and rows[0].cost_usd > 0
    assert [(e.event_key, e.type) for e in events] == [("embed:batch:0", "embedding")]
    assert events[0].data["inputs"] == 2 and "alpha" not in json.dumps(events[0].data)


# ----------------------------------------------------------------------------- review fixes


def test_vectors_follow_provider_index_not_response_order(fake_gateway):
    # the gateway answers data in reverse order, each labelled with its true index
    fake_gateway["indices"] = [(2, 2), (1, 1), (0, 0)]
    call = docs(qwen_settings(), ["a", "b", "c"], model_id=QWEN)
    for i, v in enumerate(call.vectors):
        assert v[i] == 1.0 and sum(v) == 1.0  # input i got e_i


@pytest.mark.parametrize("indices", [
    [(0, 0), (1, 0), (2, 2)],   # duplicate 0, missing 1
    [(0, 0), (1, 1), (2, 3)],   # out of range
    [(0, 0), (1, 1)],           # one missing (count)
])
def test_missing_or_duplicate_indices_fail(fake_gateway, indices):
    fake_gateway["indices"] = indices
    with pytest.raises(EmbeddingFailed):
        docs(qwen_settings(), ["a", "b", "c"], model_id=QWEN)


@pytest.mark.parametrize("vector", [[float("nan")] + [0.0] * 1023, [float("inf")] + [0.0] * 1023, [0.0] * 1024])
def test_non_finite_or_zero_vectors_fail(fake_gateway, vector):
    fake_gateway["vector"] = vector
    with pytest.raises(EmbeddingFailed):
        docs(qwen_settings(), ["a"], model_id=QWEN)


def test_chat_key_never_goes_to_a_separate_embedding_url(fake_gateway):
    s = replace(qwen_settings(), litellm_embedding_api_key=None)  # separate URL, no embedding key
    with pytest.raises(EmbeddingFailed) as info:
        docs(s, ["x"], model_id=QWEN)
    assert "EAP_LITELLM_EMBEDDING_API_KEY is required" in str(info.value) and "chat-key" not in str(info.value)
    assert fake_gateway["requests"] == []
    # the embedding URL equal to the chat URL: the chat key may be used
    same = replace(s, litellm_embedding_base_url="http://chat-gateway/v1/")
    docs(same, ["x"], model_id=QWEN)
    assert fake_gateway["requests"][0]["auth"] == "Bearer chat-key"


def test_openai_uses_dedicated_key_and_stays_pinned(fake_gateway, monkeypatch):
    oa = "openai:text-embedding-3-small"
    s = settings(f"{oa}=1536", litellm_base_url="http://chat-gateway/v1", litellm_api_key="chat-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://chat-gateway/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "litellm-virtual-key")
    monkeypatch.delenv("EAP_OPENAI_EMBEDDING_API_KEY", raising=False)
    with pytest.raises(EmbeddingFailed, match="EAP_OPENAI_EMBEDDING_API_KEY"):
        docs(s, ["x"], model_id=oa)
    monkeypatch.setenv("EAP_OPENAI_EMBEDDING_API_KEY", "openai-key")
    docs(s, ["x"], model_id=oa)
    req = fake_gateway["requests"][0]
    assert req["url"].startswith("https://api.openai.com/v1") and req["auth"] == "Bearer openai-key"
    assert req["body"]["dimensions"] == 1536


def test_is_retryable():
    from pydantic_ai.exceptions import ModelAPIError, ModelConnectionError, ModelHTTPError, ModelTimeoutError

    from eap_worker.embeddings import is_retryable

    for status in (408, 409, 429, 500, 502, 503, 504):
        assert is_retryable(ModelHTTPError(status, "m")), status
    for status in (400, 401, 403, 404, 413, 422):
        assert not is_retryable(ModelHTTPError(status, "m")), status
    assert is_retryable(ModelConnectionError(model_name="m", message="refused"))
    assert is_retryable(ModelTimeoutError(model_name="m", message="slow"))
    assert is_retryable(TimeoutError()) and is_retryable(ConnectionResetError())
    assert not is_retryable(EmbeddingModelNotAllowed("x:y"))
    assert not is_retryable(EmbeddingDimensionMismatch("x:y", 1024, 4096))
    assert not is_retryable(EmbeddingFailed("x:y", "bad"))
    assert not is_retryable(ModelAPIError(model_name="m", message="undecodable"))
    assert not is_retryable(ValueError("x"))


def test_retryable_on_a_real_gateway_error(fake_gateway):
    from eap_worker.embeddings import is_retryable

    for status, retry in ((503, True), (429, True), (401, False), (400, False)):
        fake_gateway["status"] = status
        with pytest.raises(Exception) as info:
            docs(qwen_settings(), ["x"], model_id=QWEN)
        assert is_retryable(info.value) is retry, (status, type(info.value))


def test_uninstrument_resets_embedder_default():
    from opentelemetry.sdk.trace import TracerProvider
    from pydantic_ai.embeddings import Embedder

    from eap_observability.agents import instrument_pydantic_ai, uninstrument_pydantic_ai

    assert instrument_pydantic_ai(tracer_provider=TracerProvider()) is True
    try:
        assert Embedder._instrument_default not in (False, None)
    finally:
        uninstrument_pydantic_ai()
    assert Embedder._instrument_default is False
