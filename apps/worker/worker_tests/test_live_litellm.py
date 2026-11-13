"""Optional: one real agent run against the LiteLLM gateway (qwen3.8-thinking), and one real
embedding call (RM.6).

The chat run is skipped unless EAP_LITELLM_API_KEY or LITELLM_API_KEY is set; its base URL comes
from EAP_LITELLM_BASE_URL / OPENAI_BASE_URL (default: the local SSH tunnel).

The embedding call is skipped unless EAP_LITELLM_EMBEDDING_API_KEY, EAP_LITELLM_API_KEY or
LITELLM_API_KEY is set. It uses the worker's embedding settings (EAP_LITELLM_EMBEDDING_BASE_URL,
falling back to EAP_LITELLM_BASE_URL / OPENAI_BASE_URL, default the local SSH tunnel) and
EAP_LIVE_EMBEDDING_MODEL, an allowlist entry (default
`litellm:Qwen3-VL-Embedding-8B-mlx-8bit=1024`). It needs no database.
"""

from __future__ import annotations

import os

import pytest

from worker_tests.conftest import APP_VERSION, enqueue_run, event_rows, ticket_spec, wait_terminal

HAS_KEY = bool(os.environ.get("EAP_LITELLM_API_KEY") or os.environ.get("LITELLM_API_KEY"))
HAS_EMBEDDING_KEY = bool(os.environ.get("EAP_LITELLM_EMBEDDING_API_KEY") or HAS_KEY)
DEFAULT_LIVE_EMBEDDING_MODEL = "litellm:Qwen3-VL-Embedding-8B-mlx-8bit=1024"
DEFAULT_BASE_URL = "http://127.0.0.1:14001/v1"


@pytest.mark.skipif(not HAS_KEY, reason="no EAP_LITELLM_API_KEY / LITELLM_API_KEY set")
def test_live_qwen_one_turn(worker, client, tenant, monkeypatch):
    from dataclasses import replace

    from eap_schemas.models_policy import ModelPolicy
    from eap_worker.config import WorkerSettings

    env = WorkerSettings.from_env()
    base_url = env.litellm_base_url or "http://127.0.0.1:14001/v1"
    monkeypatch.setattr(worker, "settings", replace(worker.settings, litellm_base_url=base_url,
                                                     litellm_api_key=env.litellm_api_key,
                                                     allowed_models=ModelPolicy.parse("litellm:qwen3.8-thinking")))
    spec = ticket_spec(tools=())
    spec["model"] = {"provider": "litellm", "name": "qwen3.8-thinking"}
    spec["instructions"] = "Answer in one short sentence."
    run_id = enqueue_run(client, worker.run_store, tenant_id=tenant, spec=spec,
                         input_text="What is the capital of France?", app_version=APP_VERSION)
    run = wait_terminal(worker.run_store, tenant, run_id, timeout=280)
    assert run["status"] == "completed", run["error"]
    events = event_rows(worker.engine, run_id)
    assert [e[0] for e in events] == list(range(len(events)))
    assert [e[1] for e in events] == ["start", "model:0:final", "end"]
    assert events[1][2] == "model_message"
    assert run["output"]
    # P0.1: the proxy reports streamed usage (Pydantic AI sends stream_options.include_usage), and
    # litellm:* is priced only from the override file (0 in the repo and test price files).
    usage = events[1][3]["usage"]
    assert usage["measured"] is True and usage["input_tokens"] > 0 and usage["output_tokens"] > 0, usage
    assert events[1][3]["model_id"] == "litellm:qwen3.8-thinking"
    assert run["input_tokens"] == usage["input_tokens"] and run["cost_usd"] == 0


@pytest.mark.skipif(
    not HAS_EMBEDDING_KEY, reason="no EAP_LITELLM_EMBEDDING_API_KEY / EAP_LITELLM_API_KEY / LITELLM_API_KEY set"
)
def test_live_embedding_call():
    """RM.6: documents and a query through the gateway at the declared dimensions, with usage."""
    import asyncio
    import math
    import uuid
    from dataclasses import replace

    from eap_schemas.embedding_policy import EmbeddingModelPolicy
    from eap_worker.config import WorkerSettings
    from eap_worker.embeddings import embed_documents, embed_query

    entry = os.environ.get("EAP_LIVE_EMBEDDING_MODEL") or DEFAULT_LIVE_EMBEDDING_MODEL
    model_id = entry.partition("=")[0]
    env = WorkerSettings.from_env()
    settings = replace(
        env,
        litellm_base_url=env.litellm_base_url or DEFAULT_BASE_URL,
        allowed_embedding_models=EmbeddingModelPolicy.parse(
            entry, {model_id: "Given a question, retrieve passages that answer it"}
        ),
    )
    dims = settings.allowed_embedding_models.resolve(model_id).dimensions
    ids = dict(tenant_id="t_live", run_id=str(uuid.uuid4()), model_id=model_id)

    async def run():
        docs = await embed_documents(settings, request_key="embed:live:docs", texts=[
            "Refunds are issued within 14 days of receiving the returned item.",
            "The office is closed on public holidays.",
        ], **ids)
        q = await embed_query(settings, request_key="embed:live:query", text="How long do refunds take?", **ids)
        return docs, q

    docs, q = asyncio.run(run())
    assert len(docs.vectors) == 2 and all(len(v) == dims for v in docs.vectors) and len(q.vector) == dims
    for v in (*docs.vectors, q.vector):
        assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-2)
    scores = [sum(a * b for a, b in zip(q.vector, v)) for v in docs.vectors]
    assert scores[0] > scores[1], scores  # the refund passage ranks first
    assert docs.usage["operation"] == "embedding" and docs.usage["model_id"] == model_id
    assert docs.usage_measured is True and docs.input_tokens > 0, docs.usage
    assert docs.usage["cost_usd"] == 0  # litellm:* is 0 in the repo and test price files
