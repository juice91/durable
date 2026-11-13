"""Embedding service (RAG and memory milestone, task RM.6).

`embed_documents` and `embed_query` turn texts into vectors with an allowlisted embedding model and
return the vectors together with the priced usage record of the call. They do no database I/O:

    call = await embed_documents(settings, tenant_id=..., run_id=..., request_key="embed:batch:3",
                                 model_id="litellm:Qwen3-VL-Embedding-8B-mlx-8bit", texts=[...])
    # then, in ONE transaction with the caller's own writes:
    event_log.append(tenant_id, run_id, call.event_key, call.event_type, call.event_data, usage=call.usage)
    # or KnowledgeStore.write_embeddings(..., event_key=..., usage=call.usage)

Re-execution contract (DBOS steps). These functions are safe to call again when a step re-executes:
they record nothing themselves. The caller writes `call.usage` (a run_model_requests row keyed by
`request_key`) in the same transaction as its event, keyed by the same request key, so the row is
written exactly once: EventLog.append writes nothing for an event key that already exists, and the
usage insert is ON CONFLICT DO NOTHING on (run_id, request_key). A step that crashes after the
provider call but before its commit calls the provider once more on re-execution (one bounded
duplicate provider charge, never a duplicate usage row). To avoid even that, embed in one step
(its return value is checkpointed: `EmbeddingCall` is plain data and pickles) and append in the
next. Use a request key that is stable across re-executions (`embed:batch:{i}`,
`embed:retrieve:{tool_call_id}`, `embed:index:{turn}`, `embed:recall:{turn}`), never a random one.

Rules:
- The model must pass `settings.allowed_embedding_models` (EAP_ALLOWED_EMBEDDING_MODELS) before any
  provider call: EmbeddingModelNotAllowed otherwise.
- Queries get the model's query instruction (`EmbeddingModel.query_text`); documents are embedded
  as-is.
- `dimensions` is always sent to `litellm:*` and `openai:*` models (Qwen3 needs 1024 out of its
  native 4096), except OpenAI models that reject it. Every returned vector is checked against the
  declared dimensions: EmbeddingDimensionMismatch otherwise.
- A call is split into provider requests of at most `embedding_max_batch_inputs` texts and
  `embedding_max_batch_tokens` approximate tokens (`ceil(len / 4)`); one text larger than the token
  cap goes alone. The requests of one call share its single usage record (tokens summed).
- `usage_measured` is false when the provider omitted usage on any request of the call (0 tokens,
  no cost, as for chat).
- Transport retries are off inside the client: the DBOS step owns retrying; `is_retryable(exc)`
  tells a step which errors are worth another attempt.
- Vectors are matched to inputs by the provider's `index` (a gateway may return them in any order);
  missing or duplicate indices, and non-finite or zero vectors, are EmbeddingFailed.
- No text in events, spans or logs: the event data holds counts only, and spans come from
  Pydantic AI's instrumentation configured by eap_observability with content excluded.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic_ai.embeddings import Embedder, EmbeddingSettings

from eap_schemas.embedding_policy import (
    EmbeddingDimensionMismatch,
    EmbeddingModel,
    EmbeddingModelNotAllowed,
)
from eap_schemas.knowledge import approx_tokens
from eap_worker.config import WorkerSettings
from eap_worker.models import scripted_embedding_model
from eap_worker.pricing import embedding_usage_record

log = logging.getLogger("eap_worker")

EMBEDDING_EVENT_TYPE = "embedding"
OPENAI_EMBEDDING_API_KEY_ENV = "EAP_OPENAI_EMBEDDING_API_KEY"
OPENAI_BASE_URL = "https://api.openai.com/v1"
# OpenAI models that reject the `dimensions` parameter.
_NO_DIMENSIONS_PARAM = frozenset({"openai:text-embedding-ada-002"})

InputType = Literal["document", "query"]

__all__ = [
    "EMBEDDING_EVENT_TYPE",
    "EmbeddingCall",
    "EmbeddingDimensionMismatch",
    "EmbeddingFailed",
    "EmbeddingModelNotAllowed",
    "embed_documents",
    "embed_query",
    "is_retryable",
    "plan_requests",
    "record_embedding",
]


class EmbeddingFailed(RuntimeError):
    """The embedding call could not be made or gave an unusable answer (not a transport error,
    which propagates as Pydantic AI's ModelHTTPError / ModelAPIError for the step to retry)."""

    code = "embedding_failed"

    def __init__(self, model_id: str, reason: str) -> None:
        self.model_id = model_id
        self.reason = reason
        super().__init__(model_id, reason)  # pickle-safe (DBOS step errors)

    def __str__(self) -> str:
        return f"embedding call to '{self.model_id}' failed: {self.reason}"


@dataclass(frozen=True)
class EmbeddingCall:
    """One embedding call's result: plain, picklable data.

    `vectors[i]` embeds `texts[i]`. `usage` is the run_model_requests row (with
    `operation='embedding'`) for `EventLog.append(..., usage=...)` or
    `KnowledgeStore.write_embeddings(..., usage=...)`; `event_data` is the `embedding` event's data
    (counts only, never text). Write both under `event_key` (= the request key)."""

    request_key: str
    model_id: str
    dimensions: int
    input_type: InputType
    vectors: tuple[tuple[float, ...], ...]
    usage: dict[str, Any]
    event_data: dict[str, Any]
    requests: int  # provider requests made for this call

    @property
    def event_key(self) -> str:
        return self.request_key

    @property
    def event_type(self) -> str:
        return EMBEDDING_EVENT_TYPE

    @property
    def vector(self) -> tuple[float, ...]:
        """The only vector of a single-text call (embed_query)."""
        if len(self.vectors) != 1:
            raise ValueError(f"call has {len(self.vectors)} vectors, not 1")
        return self.vectors[0]

    @property
    def input_tokens(self) -> int:
        return int(self.usage["input_tokens"])

    @property
    def usage_measured(self) -> bool:
        return bool(self.usage["usage_measured"])


# ----------------------------------------------------------------------------- model building


def _embedding_model(model: EmbeddingModel, settings: WorkerSettings) -> tuple[Any, Any]:
    """(Pydantic AI embedding model, client to close or None) for an allowed model."""
    if model.provider == "test":
        scripted = scripted_embedding_model(model.name)
        if scripted is None:
            raise EmbeddingFailed(model.id, "no scripted embedder of that name (test:hash, test:hash768, test:hash-nousage)")
        return scripted, None
    if model.provider not in ("litellm", "openai"):
        raise EmbeddingFailed(model.id, f"provider '{model.provider}' has no embedding client (use litellm:* or openai:*)")

    from openai import AsyncOpenAI
    from pydantic_ai.embeddings.openai import OpenAIEmbeddingModel
    from pydantic_ai.providers.openai import OpenAIProvider

    if model.provider == "litellm":
        base_url, api_key = _litellm_endpoint(model, settings)
        if not base_url:
            raise EmbeddingFailed(model.id, "EAP_LITELLM_EMBEDDING_BASE_URL (or EAP_LITELLM_BASE_URL) is not set")
        if not api_key:
            raise EmbeddingFailed(model.id, "EAP_LITELLM_EMBEDDING_API_KEY (or EAP_LITELLM_API_KEY) is not set")
    else:
        # Direct OpenAI: pinned to api.openai.com, never OPENAI_BASE_URL (the worker's LiteLLM
        # fallback), so an openai:* model is never served, and priced, from the proxy.
        base_url = OPENAI_BASE_URL
        # A dedicated key, never OPENAI_API_KEY: in this repo that may be a LiteLLM virtual key.
        api_key = os.environ.get(OPENAI_EMBEDDING_API_KEY_ENV)
        if not api_key:
            raise EmbeddingFailed(model.id, f"{OPENAI_EMBEDDING_API_KEY_ENV} is not set")
    client = AsyncOpenAI(
        base_url=base_url,
        api_key=api_key,
        max_retries=0,  # transport retries off: the DBOS step owns retrying
        timeout=settings.embedding_timeout_s,
    )
    # Order vectors by the provider's `index` (Pydantic AI keeps the response order).
    client.embeddings = _OrderedEmbeddings(client.embeddings, model.id)
    return OpenAIEmbeddingModel(model.name, provider=OpenAIProvider(openai_client=client)), client


def _litellm_endpoint(model: EmbeddingModel, settings: WorkerSettings) -> tuple[str | None, str | None]:
    """(base URL, key) for a litellm:* embedding model. URL and key fall back to the chat gateway as
    a PAIR: the chat key is used only when no separate embedding URL is set (or it equals the chat
    URL), so the chat key is never sent to another host."""
    embed_url = settings.litellm_embedding_base_url
    chat_url = settings.litellm_base_url
    if embed_url and embed_url.rstrip("/") != (chat_url or "").rstrip("/"):
        if not settings.litellm_embedding_api_key:
            raise EmbeddingFailed(
                model.id, "EAP_LITELLM_EMBEDDING_API_KEY is required when EAP_LITELLM_EMBEDDING_BASE_URL is set"
            )
        return embed_url, settings.litellm_embedding_api_key
    return chat_url, settings.litellm_embedding_api_key or settings.litellm_api_key


class _OrderedEmbeddings:
    """A thin wrapper over the OpenAI client's `embeddings` resource: `create` checks that the
    response indices are exactly 0..n-1 and returns the data sorted by index."""

    def __init__(self, inner: Any, model_id: str) -> None:
        self._inner = inner
        self._model_id = model_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        response = await self._inner.create(*args, **kwargs)
        inputs = kwargs.get("input")
        n = 1 if isinstance(inputs, str) else len(inputs or ())
        data = list(response.data or ())
        if len(data) != n:
            raise EmbeddingFailed(self._model_id, f"provider returned {len(data)} vectors for {n} inputs")
        indices = [getattr(d, "index", None) for d in data]
        if sorted(i for i in indices if isinstance(i, int)) != list(range(n)):
            raise EmbeddingFailed(
                self._model_id, f"provider returned vector indices that are not exactly 0..{n - 1}"
            )
        response.data = sorted(data, key=lambda d: d.index)
        return response


def _embedding_settings(model: EmbeddingModel) -> EmbeddingSettings | None:
    if model.provider == "test" or model.id in _NO_DIMENSIONS_PARAM:
        return None
    return EmbeddingSettings(dimensions=model.dimensions)


# ----------------------------------------------------------------------------- batching


def plan_requests(texts: Sequence[str], max_inputs: int, max_tokens: int) -> list[tuple[int, int]]:
    """Split `texts` into consecutive provider requests: [(start, end), ...], each with at most
    `max_inputs` texts and `max_tokens` approximate tokens; a single text over the token cap is a
    request of its own."""
    spans: list[tuple[int, int]] = []
    start, tokens = 0, 0
    for i, text in enumerate(texts):
        t = approx_tokens(text)
        if i > start and (i - start >= max_inputs or tokens + t > max_tokens):
            spans.append((start, i))
            start, tokens = i, 0
        tokens += t
    if start < len(texts):
        spans.append((start, len(texts)))
    return spans


# ----------------------------------------------------------------------------- calls


async def _embed(
    settings: WorkerSettings,
    *,
    tenant_id: str,
    run_id: str,
    request_key: str,
    model_id: str,
    texts: Sequence[str],
    input_type: InputType,
) -> EmbeddingCall:
    if not request_key:
        raise ValueError("request_key is required")
    if isinstance(texts, str) or not texts:
        raise ValueError("texts must be a non-empty sequence of strings")
    model = settings.allowed_embedding_models.resolve(model_id)  # raises EmbeddingModelNotAllowed
    sent = [model.query_text(t) for t in texts] if input_type == "query" else list(texts)
    pai_model, client = _embedding_model(model, settings)
    # instrument=None: the default set by eap_observability (instrument_pydantic_ai), content off.
    embedder = Embedder(pai_model, settings=_embedding_settings(model))
    vectors: list[tuple[float, ...]] = []
    input_tokens = 0
    measured = True
    provider_model_name: str | None = None
    spans = plan_requests(sent, settings.embedding_max_batch_inputs, settings.embedding_max_batch_tokens)
    try:
        for start, end in spans:
            batch = sent[start:end]
            result = await embedder.embed(batch, input_type=input_type)
            if len(result.embeddings) != len(batch):
                raise EmbeddingFailed(
                    model.id, f"provider returned {len(result.embeddings)} vectors for {len(batch)} inputs"
                )
            for embedding in result.embeddings:
                model.check_dimensions(len(embedding))  # raises EmbeddingDimensionMismatch
                vector = tuple(float(x) for x in embedding)
                if not all(math.isfinite(x) for x in vector):
                    raise EmbeddingFailed(model.id, "provider returned a vector with non-finite values")
                if math.sqrt(sum(x * x for x in vector)) <= 0.0:
                    raise EmbeddingFailed(model.id, "provider returned a zero vector (no cosine similarity)")
                vectors.append(vector)
            usage = result.usage
            tokens = int(getattr(usage, "input_tokens", 0) or 0)
            # Pydantic AI maps an omitted usage block to an empty RequestUsage (0 tokens); a non-empty
            # input always costs tokens, so 0 means "not reported".
            if getattr(usage, "unmeasured_requests", 0) or (tokens == 0 and any(batch)):
                measured = False
            input_tokens += tokens
            provider_model_name = provider_model_name or result.model_name
    finally:
        if client is not None:
            await client.close()
    row, event_data = embedding_usage_record(
        request_key,
        model_id=model.id,
        provider_model_name=provider_model_name,
        input_tokens=input_tokens,
        usage_measured=measured,
        inputs=len(sent),
        dimensions=model.dimensions,
        input_type=input_type,
        at=datetime.now(timezone.utc),
    )
    event_data["requests"] = len(spans)
    log.info(
        "embedding call tenant=%s run=%s key=%s model=%s inputs=%d requests=%d tokens=%d measured=%s",
        tenant_id, run_id, request_key, model.id, len(sent), len(spans), row["input_tokens"], measured,
    )
    return EmbeddingCall(
        request_key=request_key,
        model_id=model.id,
        dimensions=model.dimensions,
        input_type=input_type,
        vectors=tuple(vectors),
        usage=row,
        event_data=event_data,
        requests=len(spans),
    )


async def embed_documents(
    settings: WorkerSettings,
    *,
    tenant_id: str,
    run_id: str,
    request_key: str,
    model_id: str,
    texts: Sequence[str],
) -> EmbeddingCall:
    """Embed documents (chunks, stored messages) as-is. Records nothing: see the module docstring.

    Raises EmbeddingModelNotAllowed (before any provider call), EmbeddingDimensionMismatch,
    EmbeddingFailed, ValueError for an empty `texts`, and provider errors (ModelHTTPError, ...)."""
    return await _embed(
        settings, tenant_id=tenant_id, run_id=run_id, request_key=request_key, model_id=model_id,
        texts=texts, input_type="document",
    )


async def embed_query(
    settings: WorkerSettings,
    *,
    tenant_id: str,
    run_id: str,
    request_key: str,
    model_id: str,
    text: str,
) -> EmbeddingCall:
    """Embed one search query, with the model's query instruction applied. `call.vector` is the
    vector. Same errors and contract as embed_documents."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    return await _embed(
        settings, tenant_id=tenant_id, run_id=run_id, request_key=request_key, model_id=model_id,
        texts=[text], input_type="query",
    )


_RETRYABLE_STATUS = frozenset({408, 409, 429})


def is_retryable(exc: BaseException) -> bool:
    """Whether a DBOS step should try an embedding call again after `exc`.

    True for HTTP 408, 409, 429 and 5xx, connection errors and timeouts. False for policy and
    answer errors (EmbeddingModelNotAllowed, EmbeddingDimensionMismatch, EmbeddingFailed), other
    4xx (authentication, not found, rejected input) and anything else."""
    from pydantic_ai.exceptions import ModelAPIError, ModelConnectionError, ModelHTTPError

    if isinstance(exc, (EmbeddingModelNotAllowed, EmbeddingDimensionMismatch, EmbeddingFailed)):
        return False
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in _RETRYABLE_STATUS or 500 <= exc.status_code <= 599
    if isinstance(exc, ModelConnectionError):  # includes ModelTimeoutError
        return True
    if isinstance(exc, ModelAPIError):
        from pydantic_ai.exceptions import (
            ModelRateLimitError,
            ModelRequestTimeout,
            ModelServerError,
            ModelUnavailableError,
        )

        return isinstance(exc, (ModelRateLimitError, ModelRequestTimeout, ModelServerError, ModelUnavailableError))
    return isinstance(exc, (TimeoutError, ConnectionError))


def record_embedding(event_log: Any, tenant_id: str, run_id: str, call: EmbeddingCall) -> tuple[Any, bool]:
    """Write the call's `embedding` event and its usage row in one transaction
    (EventLog.append; synchronous, run it with asyncio.to_thread from async code). Idempotent:
    a second write under the same request key writes nothing and returns created=False."""
    from uuid import UUID

    return event_log.append(
        tenant_id, UUID(str(run_id)), call.event_key, EMBEDDING_EVENT_TYPE, call.event_data, usage=call.usage
    )
