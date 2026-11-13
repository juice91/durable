"""Semantic recall at run time (RAG and memory milestone, RM.14).

Only for a spec with `memory.semantic_recall` AND a conversation turn. Every other run (no semantic
recall, a standalone run, a workflow child run) keeps its exact step layout.

Steps (eap_worker.workflow):

    eap.load_memory                    (RM.12) the snapshot; its "turns" are the turns in context
    eap.memory_recall                  PRE-TURN, right after eap.load_memory, before the first model
                                       request (never for a blocked input):
                                       0. skip (no embedding, no cost, no event) when the scope has
                                          no indexed row under the model (MemoryStore
                                          .recall_rows_exist): a brand-new user;
                                       1. embed the turn's PROCESSED input (embed_query, request key
                                          `embed:recall:{turn}`), each attempt cut off after
                                          EAP_RECALL_TIMEOUT_S (default 8 s), at most one retry, so
                                          a hanging provider delays the answer by about 17 s at most;
                                       2. MemoryStore.search_recall (same tenant, agent, resource or
                                          conversation scope, embedding model and dimensions;
                                          excluding the turns already in context and this turn);
                                          hits are messages, grouped by (conversation, turn), best
                                          score first, at most `top_k` turns;
                                       3. MemoryStore.recall_neighbours_many (one query): each hit
                                          turn widened by `message_range` turns, overlapping ranges
                                          merged;
                                       4. fit to the 8 KiB block (eap_worker.memory.fit_recall);
                                       5. ONE transaction: the `embedding` event (key
                                          `embed:recall:{turn}`; counts, model, cost, never text)
                                          and its usage row.
                                       The groups are the step's checkpointed output, so a replay
                                       renders the same "Related earlier conversations" block, and
                                       nothing re-reads the store.
    ... the turn (model requests, tools) ...
    eap.finish_run
    eap.memory_index                   POST-TURN, after eap.finish_run (the answer is never delayed),
                                       only for a COMPLETED turn: the processed input and the final
                                       answer, each passed through `memory.redact` and clipped, are
                                       embedded (embed_documents, request key `embed:index:{turn}`)
                                       and stored with MemoryStore.index_turn (idempotent by
                                       (run_id, role); skips a turn at or below the conversation's
                                       forget watermark), with the `embedding` event and usage row
                                       in the same transaction.
    eap.memory_observe ...             (RM.13, when configured) after eap.memory_index

Failures never fail the turn. An embedding model removed from the allowlist since publish
(`embedding_model_not_allowed`), a provider error after the step's own retries, a dimension
mismatch, a recall query past its deadline (`embedding_timeout`), a store error after a charged
embedding (its usage row is still recorded) or a redaction that cannot finish: the step records ONE
`embedding` event under
`embed:recall:{turn}:failed` / `embed:index:{turn}:failed` with {"stage", "status": "failed",
"error_code", "model_id"} (no text) and returns; recall then proceeds without the block. Anything
unexpected that escapes a step is caught in the workflow wrapper (logged by code), except DBOS's own
control-flow signals, which are re-raised.

Deleted conversations: the recall `embedding` event is appended with
MemoryStore.append_conversation_event (holding the conversation `FOR KEY SHARE`), index_turn takes
the same lock; when the conversation is gone, only the request's usage row is recorded on the run
(the provider charged), and nothing else. Its message_embeddings rows cascade with it, so a
deleted conversation is never recalled.

Crash safety. `recall_embedding`: inside eap.memory_recall, right after the query embedding
returned and before anything is written; recovery re-runs the step (one bounded duplicate provider
call) and writes one event and one usage row. `after_memory_index`: inside eap.memory_index, right
after the index_turn transaction committed and before the checkpoint; recovery re-runs the step,
index_turn answers `existing`, and the event key exists, so one row per role, one event, one usage
row (again one bounded duplicate provider call).

Privacy. Only processed turn text is indexed, after `memory.redact` (the recall query is embedded,
never stored). Recalled text goes only into the instructions block, which is stripped from every
stored request (eap_worker.memory.scrub_memory_messages); events carry counts, turn numbers, model
ids and usage; logs carry ids and counts. The DBOS checkpoint of eap.memory_recall holds the
recalled (stored, redacted) text: the accepted P0 gap (operator-only data).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, TypeVar

from dbos import DBOS

from eap_observability import log_failure
from eap_run_store import ConversationNotFound, MemoryEvent, MemoryStore, RunNotFound, TurnText
from eap_schemas.memory import MemoryConfig, SemanticRecallConfig
from eap_schemas.processors import PiiRedactConfig
from eap_worker.crash import crash_point
from eap_worker.embeddings import EMBEDDING_EVENT_TYPE, embed_documents, embed_query, is_retryable
from eap_worker.errors import classify
from eap_worker.memory import MemoryRedactionFailed, fit_recall, redact_memory_write
from eap_worker.observational import is_dbos_control_flow
from eap_worker.runtime import get_runtime

log = logging.getLogger("eap_worker.recall")

RECALL_STEP = "eap.memory_recall"
INDEX_STEP = "eap.memory_index"

# Bounds: the text embedded per message (index and query), and the search fan-out. Hits are
# messages (a turn's user and assistant rows each count), so the store is asked for twice `top_k`
# messages and they are grouped into at most `top_k` turns.
MAX_INDEX_CHARS = 8_000
MAX_QUERY_CHARS = 8_000
MAX_SEARCH_MESSAGES = 50  # MemoryStore's top_k cap

# Embedding retries inside one step (the step's checkpoint then holds the outcome). The post-turn
# index keeps the general embedding budget (EAP_EMBEDDING_TIMEOUT_S per request, 3 attempts). The
# pre-turn recall query delays the answer, so it gets its own deadline per attempt
# (EAP_RECALL_TIMEOUT_S, default 8 s) and at most one retry: at most about 2 x 8 s + 1 s.
EMBED_ATTEMPTS = 3
RECALL_ATTEMPTS = 2
EMBED_BACKOFF_SECONDS = 1.0

REDACTION_FAILED = "memory_redaction_failed"
EMBEDDING_TIMEOUT = "embedding_timeout"

T = TypeVar("T")


def recall_request_key(turn: int) -> str:
    return f"embed:recall:{int(turn)}"


def index_request_key(turn: int) -> str:
    return f"embed:index:{int(turn)}"


def failed_key(request_key: str) -> str:
    return f"{request_key}:failed"


def _store() -> MemoryStore:
    return MemoryStore(get_runtime().engine)


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return EMBEDDING_TIMEOUT
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and code else classify(exc).code  # type: ignore[arg-type]


async def _with_retries(
    call: Callable[[], Awaitable[T]], *, attempts: int = EMBED_ATTEMPTS, timeout: float | None = None
) -> tuple[T | None, BaseException | None]:
    """(result, None) or (None, the last error): retryable provider errors (is_retryable, which
    includes TimeoutError) are tried `attempts` times with backoff; each attempt is cancelled after
    `timeout` seconds (TimeoutError) when given. DBOS control-flow signals are re-raised."""
    for attempt in range(attempts):
        try:
            if timeout is None:
                return await call(), None
            return await asyncio.wait_for(call(), timeout), None
        except Exception as exc:  # noqa: BLE001 - recorded as a failure event, never fails the turn
            if is_dbos_control_flow(exc):
                raise
            if attempt + 1 < attempts and is_retryable(exc):
                await asyncio.sleep(EMBED_BACKOFF_SECONDS * (2**attempt))
                continue
            return None, exc
    return None, None  # pragma: no cover - the loop always returns


def _clip(value: str, limit: int) -> str:
    """At most `limit` characters, cut at the last whitespace, marked ` [...]`."""
    if len(value) <= limit:
        return value
    cut = value[:limit]
    space = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    return (cut[:space] if space > 0 else cut) + " [...]"


def _date(value: Any) -> str:
    try:
        return value.date().isoformat()
    except (AttributeError, TypeError, ValueError):
        return str(value or "")[:10]


def _record_failure(
    store: MemoryStore, *, tenant_id: str, agent_id: str, conversation_id: str, run_id: str, request_key: str,
    stage: str, model_id: str, code: str, inputs: int,
) -> bool:
    """One text-free `embedding` event (`{request_key}:failed`) for a failed recall or index.
    Returns False when the conversation is gone (nothing written)."""
    event = MemoryEvent(
        run_id=run_id,
        event_key=failed_key(request_key),
        type=EMBEDDING_EVENT_TYPE,
        data={"stage": stage, "status": "failed", "error_code": code, "model_id": model_id, "inputs": inputs,
              "input_tokens": 0},
    )
    try:
        store.append_conversation_event(tenant_id, agent_id, conversation_id, event)
    except ConversationNotFound:
        return False
    log.warning("run %s: semantic recall %s failed code=%s model=%s", run_id, stage, code, model_id)
    return True


def _record_usage_only(store: MemoryStore, tenant_id: str, run_id: str, usage: Mapping[str, Any]) -> None:
    try:
        store.record_usage(tenant_id, run_id, usage)
    except RunNotFound:  # pragma: no cover - runs outlive their conversation
        log.info("run %s: recall embedding usage not recorded, the run is gone", run_id)


# --------------------------------------------------------------------------- recall (pre-turn)


def group_hits(hits: Sequence[Any], top_k: int) -> list[dict[str, Any]]:
    """Message hits (best first) grouped by (conversation, turn): [{"conversation_id", "turn",
    "score"}], best score first (ties: earlier position in `hits`), at most `top_k` turns."""
    best: dict[tuple[str, int], dict[str, Any]] = {}
    for hit in hits:
        key = (str(hit.conversation_id), int(hit.turn))
        if key not in best:
            best[key] = {"conversation_id": key[0], "turn": key[1], "score": float(hit.score)}
        elif float(hit.score) > best[key]["score"]:
            best[key]["score"] = float(hit.score)
    ordered = sorted(best.values(), key=lambda h: -h["score"])  # stable: first-seen order on ties
    return ordered[: int(top_k)]


def build_groups(
    hit_turns: Sequence[Mapping[str, Any]],
    neighbours: Mapping[tuple[str, int], Sequence[Any]],
    current_conversation_id: str,
) -> list[dict[str, Any]]:
    """The recall groups, most relevant first: each hit turn's neighbour messages, with ranges of
    one conversation that overlap or touch an earlier (better) group merged into it, and messages
    already shown left out. A group: {"same_conversation", "from_turn", "to_turn", "date", "score",
    "messages": [{"turn", "role", "text"}]} (no ids, no datetimes)."""
    groups: list[dict[str, Any]] = []
    seen: set[str] = set()
    for hit in hit_turns:
        conv, turn = str(hit["conversation_id"]), int(hit["turn"])
        rows = [m for m in neighbours.get((conv, turn), ()) if str(m.id) not in seen]
        if not rows:
            continue
        lo, hi = min(int(m.turn) for m in rows), max(int(m.turn) for m in rows)
        target = None
        for g in groups:
            if g["_conversation_id"] == conv and lo <= g["to_turn"] + 1 and hi >= g["from_turn"] - 1:
                target = g
                break
        if target is None:
            target = {
                "_conversation_id": conv,
                "same_conversation": conv == str(current_conversation_id),
                "from_turn": lo,
                "to_turn": hi,
                "date": "",
                "score": round(float(hit["score"]), 6),
                "_messages": [],
            }
            groups.append(target)
        for m in rows:
            seen.add(str(m.id))
            target["_messages"].append(m)
        target["_messages"].sort(key=lambda m: (int(m.turn), 0 if m.role == "user" else 1, str(m.id)))
        target["from_turn"] = min(target["from_turn"], lo)
        target["to_turn"] = max(target["to_turn"], hi)
    out = []
    for g in groups:
        messages = g.pop("_messages")
        g.pop("_conversation_id")
        g["date"] = _date(messages[0].created_at) if messages else ""
        g["messages"] = [{"turn": int(m.turn), "role": m.role, "text": m.text} for m in messages]
        out.append(g)
    return out


@DBOS.step(name=RECALL_STEP)
async def memory_recall_step(
    run_id: str,
    tenant_id: str,
    agent_id: str,
    conversation_id: str,
    turn: int,
    query_text: str,
    recall_json: dict[str, Any],
    exclude_turns: list[int],
) -> dict[str, Any]:
    """Recall for turn `turn`. Returns {"status": "ok" | "skipped" | "failed" |
    "conversation_deleted", "error_code", "hits" (messages found), "turns" (hit turns), "groups"
    (fit_recall output, most relevant first), "model_id"}. Never raises for an embedding problem."""
    config = SemanticRecallConfig.model_validate(recall_json)
    model_id = config.embedding_model
    request_key = recall_request_key(turn)
    base = {"status": "ok", "error_code": None, "hits": 0, "turns": 0, "groups": [], "model_id": model_id}
    query = _clip(query_text or "", MAX_QUERY_CHARS)
    if not query.strip():
        return {**base, "status": "skipped", "reason": "empty_input"}
    store = _store()
    # Nothing indexed in the scope yet (a brand-new user): no query embedding, no cost, no event.
    try:
        found = await asyncio.to_thread(
            store.recall_rows_exist, tenant_id, agent_id, conversation_id, embedding_model=model_id, scope=config.scope
        )
    except ConversationNotFound:
        return {**base, "status": "conversation_deleted"}
    if not found:
        return {**base, "status": "skipped", "reason": "nothing_indexed"}
    settings = get_runtime().settings
    call, error = await _with_retries(
        lambda: embed_query(settings, tenant_id=tenant_id, run_id=run_id, request_key=request_key,
                            model_id=model_id, text=query),
        attempts=RECALL_ATTEMPTS,
        timeout=settings.recall_timeout_s,
    )

    def fail(code: str) -> dict[str, Any]:
        written = _record_failure(store, tenant_id=tenant_id, agent_id=agent_id, conversation_id=conversation_id,
                                  run_id=run_id, request_key=request_key, stage="recall", model_id=model_id,
                                  code=code, inputs=1)
        return {**base, "status": "failed" if written else "conversation_deleted", "error_code": code}

    if error is not None or call is None:
        code = _error_code(error) if error is not None else "embedding_failed"
        return await asyncio.to_thread(fail, code)
    # Crash-injection tests: the query was embedded; nothing is written or checkpointed yet.
    crash_point("recall_embedding")
    excluded = sorted({int(t) for t in exclude_turns} | {int(turn)})

    def work() -> dict[str, Any]:
        try:
            hits = store.search_recall(
                tenant_id, agent_id, conversation_id, call.vector, embedding_model=call.model_id,
                scope=config.scope, top_k=min(MAX_SEARCH_MESSAGES, 2 * config.top_k), min_score=config.min_score,
                exclude_turns=excluded,
            )
            hit_turns = group_hits(hits, config.top_k)
            neighbours = store.recall_neighbours_many(
                tenant_id, agent_id, [(h["conversation_id"], h["turn"]) for h in hit_turns], config.message_range,
                current_conversation_id=conversation_id, exclude_turns=excluded,
            )
        except ConversationNotFound:
            _record_usage_only(store, tenant_id, run_id, call.usage)
            return {**base, "status": "conversation_deleted"}
        except Exception as exc:  # noqa: BLE001 - the embedding was charged: record it, never fail the turn
            log_failure(log, "semantic recall search failed", exc, code=classify(exc).code, run_id=run_id,
                        tenant_id=tenant_id)
            _record_usage_only(store, tenant_id, run_id, call.usage)
            return fail(classify(exc).code)
        groups = fit_recall(build_groups(hit_turns, neighbours, conversation_id))
        shown = sum(len(g["messages"]) for g in groups)
        event = MemoryEvent(
            run_id=run_id,
            event_key=request_key,
            type=EMBEDDING_EVENT_TYPE,
            data={**call.event_data, "stage": "recall", "hits": len(hits), "turns": len(hit_turns),
                  "groups": len(groups), "messages": shown},
            usage=call.usage,
        )
        try:
            store.append_conversation_event(tenant_id, agent_id, conversation_id, event)
        except ConversationNotFound:
            _record_usage_only(store, tenant_id, run_id, call.usage)
            return {**base, "status": "conversation_deleted"}
        log.info("run %s: semantic recall hits=%d turns=%d groups=%d messages=%d", run_id, len(hits),
                 len(hit_turns), len(groups), shown)
        return {**base, "hits": len(hits), "turns": len(hit_turns), "groups": groups}

    return await asyncio.to_thread(work)


async def recall_before_turn(
    run_id: str, tenant_id: str, agent_id: str, conversation_id: str, turn: int, spec: Any, query_text: str,
    loaded: Mapping[str, Any],
) -> dict[str, Any] | None:
    """The pre-turn recall (workflow code, right after eap.load_memory). Returns the step's recorded
    output, or None when something unexpected escaped the step (logged by code; the turn proceeds
    without the block). Never raises an `Exception` other than DBOS control flow."""
    config: SemanticRecallConfig = spec.memory.semantic_recall
    exclude = [int(t) for t in loaded.get("turns") or ()]
    try:
        return await memory_recall_step(
            run_id, tenant_id, agent_id, str(conversation_id), int(turn), query_text,
            config.model_dump(mode="json"), exclude,
        )
    except Exception as exc:  # noqa: BLE001 - recall never fails the turn
        if is_dbos_control_flow(exc):
            raise
        log_failure(log, "semantic recall failed", exc, code=classify(exc).code, run_id=run_id, tenant_id=tenant_id)
        return None


# --------------------------------------------------------------------------- index (post-turn)


def index_texts(
    redact: PiiRedactConfig | None, user_text: str | None, answer_text: str | None
) -> tuple[list[tuple[str, str]], int]:
    """([(role, text)] to index, redaction count): each text passes `memory.redact` and is then
    clipped to MAX_INDEX_CHARS (at whitespace); empty texts are left out. Raises
    MemoryRedactionFailed."""
    out: list[tuple[str, str]] = []
    count = 0
    for role, value in (("user", user_text), ("assistant", answer_text)):
        if not isinstance(value, str) or not value.strip():
            continue
        redacted, n = redact_memory_write(redact, value)
        count += n
        clipped = _clip(redacted, MAX_INDEX_CHARS)
        if clipped.strip():
            out.append((role, clipped))
    return out, count


@DBOS.step(name=INDEX_STEP)
async def memory_index_step(
    run_id: str,
    tenant_id: str,
    agent_id: str,
    conversation_id: str,
    turn: int,
    user_text: str | None,
    answer_text: str | None,
    recall_json: dict[str, Any],
    redact_json: dict[str, Any] | None,
) -> dict[str, Any]:
    """Index completed turn `turn`. Returns {"status": "indexed" | "existing" | "skipped" (forget
    watermark) | "empty" | "failed" | "conversation_deleted", "error_code", "inserted", "existing",
    "redacted"}."""
    config = SemanticRecallConfig.model_validate(recall_json)
    model_id = config.embedding_model
    redact = None if redact_json is None else PiiRedactConfig.model_validate(redact_json)
    request_key = index_request_key(turn)
    base: dict[str, Any] = {"status": "indexed", "error_code": None, "inserted": [], "existing": [], "redacted": 0}
    store = _store()

    def fail(code: str, inputs: int) -> dict[str, Any]:
        written = _record_failure(store, tenant_id=tenant_id, agent_id=agent_id, conversation_id=conversation_id,
                                  run_id=run_id, request_key=request_key, stage="index", model_id=model_id,
                                  code=code, inputs=inputs)
        return {**base, "status": "failed" if written else "conversation_deleted", "error_code": code}

    try:
        texts, redacted = index_texts(redact, user_text, answer_text)
    except MemoryRedactionFailed:
        return await asyncio.to_thread(fail, REDACTION_FAILED, 0)
    if not texts:
        return {**base, "status": "empty"}
    settings = get_runtime().settings
    call, error = await _with_retries(
        lambda: embed_documents(settings, tenant_id=tenant_id, run_id=run_id, request_key=request_key,
                                model_id=model_id, texts=[t for _, t in texts])
    )
    if error is not None or call is None:
        code = _error_code(error) if error is not None else "embedding_failed"
        return await asyncio.to_thread(fail, code, len(texts))

    def work() -> dict[str, Any]:
        items = [TurnText(role=role, text=text, embedding=vector)  # type: ignore[arg-type]
                 for (role, text), vector in zip(texts, call.vectors)]
        event = MemoryEvent(
            run_id=run_id,
            event_key=request_key,
            type=EMBEDDING_EVENT_TYPE,
            data={**call.event_data, "stage": "index", "redacted": redacted},
            usage=call.usage,
        )
        try:
            result = store.index_turn(tenant_id, agent_id, conversation_id, run_id, items,
                                      embedding_model=call.model_id, event=event)
        except ConversationNotFound:
            _record_usage_only(store, tenant_id, run_id, call.usage)
            return {**base, "status": "conversation_deleted", "redacted": redacted}
        except Exception as exc:  # noqa: BLE001 - the embedding was charged: record it, never fail the turn
            log_failure(log, "semantic recall index write failed", exc, code=classify(exc).code, run_id=run_id,
                        tenant_id=tenant_id)
            _record_usage_only(store, tenant_id, run_id, call.usage)
            return fail(classify(exc).code, len(texts))
        if result.skipped:
            # Forgotten after this turn existed: no rows, no event; the provider still charged.
            _record_usage_only(store, tenant_id, run_id, call.usage)
            log.info("run %s: semantic recall index skipped (forget watermark) turn=%d", run_id, result.turn)
            return {**base, "status": "skipped", "redacted": redacted}
        status = "indexed" if result.inserted else "existing"
        log.info("run %s: semantic recall index %s turn=%d inserted=%d existing=%d redacted=%d", run_id, status,
                 result.turn, len(result.inserted), len(result.existing), redacted)
        return {**base, "status": status, "inserted": list(result.inserted), "existing": list(result.existing),
                "redacted": redacted}

    out = await asyncio.to_thread(work)
    # Crash-injection tests: the rows, event and usage committed; the step is not checkpointed yet.
    crash_point("after_memory_index")
    return out


async def index_after_turn(
    run_id: str, tenant_id: str, agent_id: str, conversation_id: str, turn: int, spec: Any,
    user_text: str | None, answer_text: str | None,
) -> dict[str, Any] | None:
    """The post-turn index of a COMPLETED turn (workflow code, after eap.finish_run). Never raises
    an `Exception` other than DBOS control flow; the turn stays completed."""
    memory: MemoryConfig = spec.memory
    config = memory.semantic_recall
    assert config is not None
    redact = memory.effective_redact(tuple(spec.input_processors))
    try:
        return await memory_index_step(
            run_id, tenant_id, agent_id, str(conversation_id), int(turn), user_text, answer_text,
            config.model_dump(mode="json"), None if redact is None else redact.model_dump(mode="json"),
        )
    except Exception as exc:  # noqa: BLE001 - the user's turn is completed; never fail it from here
        if is_dbos_control_flow(exc):
            raise
        log_failure(log, "semantic recall index failed", exc, code=classify(exc).code, run_id=run_id,
                    tenant_id=tenant_id)
        return None


__all__ = [
    "INDEX_STEP",
    "MAX_INDEX_CHARS",
    "RECALL_STEP",
    "build_groups",
    "failed_key",
    "group_hits",
    "index_after_turn",
    "index_request_key",
    "index_texts",
    "memory_index_step",
    "memory_recall_step",
    "recall_before_turn",
    "recall_request_key",
]
