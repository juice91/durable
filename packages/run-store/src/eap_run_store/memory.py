"""Agent memory in Postgres (RAG and memory milestone, RM.5 and RM.8; migrations 0012_memory and
0013_semantic_recall).

Tables (Alembic owns them; the models here are what `alembic check` compares):

- `conversations.resource_id` (on ConversationModel in store.py): the owner a conversation acts
  for, set once by `RunStore.create_conversation` and never changed.
- `working_memories`: one note per `(tenant_id, agent_id, scope, scope_key)`; `scope_key` is the
  conversation id (scope `conversation`) or the resource id (scope `resource`). Per agent: two
  agents of one resource never share a note. `format` `markdown` stores a JSON string, `json` an
  object. A conversation-scoped row repeats its conversation id in `conversation_id`, whose FK
  cascades on conversation delete.
- `memory_observations`: observations and reflections of one conversation over turns
  `covers_from_turn..covers_to_turn`; a row is active while `superseded_by` is NULL. The active rows
  of a conversation cover a gap-free prefix of its turns, `1..covered_to_turn`: `insert_observation`
  accepts only `covers_from_turn = covered_to_turn + 1`, and `reflect` replaces exactly the active
  rows up to its end.
- `message_embeddings` (RM.8): semantic recall. One row per `(tenant_id, run_id, role)` (role
  `user` = the turn's processed input, `assistant` = its final answer), with the agent, the
  conversation's resource_id (NULL when it has none), conversation and turn, the processed text, the
  embedding model and an untyped `public.vector` with `dimensions`; one partial expression HNSW index
  per dimension (768, 1024, 1536). Cascades from the conversation and from the run.
- `memory_forget_watermarks` (RM.8): per conversation whose resource was forgotten,
  `forgotten_through_turn` = its highest turn at the forget (upserted to the max). `index_turn`
  skips a turn at or below it, so a post-turn index step that runs late (or is recovered) after a
  forget cannot bring the forgotten memory back. Turn numbers, not clocks: forget and `create_turn`
  hold the same conversation lock, so the comparison is exact. No agent_id: a conversation belongs
  to one agent, and a per-agent forget only marks that agent's conversations. Cascades from the
  conversation; retention keeps them.

Every method takes the tenant id (and the agent id where memory is per agent) from trusted input
(the principal, the run's deps) and filters by both in SQL. Memory text never goes to logs or event
data: events carry versions, keys and counts. The store does not redact: callers pass text that
`memory.redact` already processed (RM.12, RM.13). It imports no pydantic_ai or dbos.

Concurrency rules. **Lock order: the conversation row, then the working memory row, then
observation rows, then message embedding rows, then the run row (event append), then the
side_effects ledger row.** Every method
that locks more than one of these takes them in that order, and none waits for an earlier one while
holding a later one:

- `update_working_memory` (conversation scope) takes the conversation `FOR KEY SHARE` (ownership,
  and `delete_conversation` waits for it), then the note `FOR UPDATE` (or inserts it; a concurrent
  insert of the same note makes this call wait and re-read it locked), then the run row through
  `append_event_in_session` when an event is given, then inserts the ledger row. Resource scope
  starts at the note. Two merges into one note therefore apply one after the other, each on the
  other's result: no field is lost. Two replaces are last writer wins.
- `insert_observation`, `reflect` and `delete_observation(s)` take the conversation
  `FOR NO KEY UPDATE` (serialising the conversation's observation writers without blocking
  `update_working_memory`'s KEY SHARE), then observation rows.
- `index_turn` takes the conversation `FOR KEY SHARE` (like a conversation-scoped working memory
  write: `delete_conversation` and `forget_resource` wait for it, and it waits for them), then reads
  the conversation's forget watermark (written only by forget while it holds the conversation
  `FOR UPDATE`, so this check cannot race a forget), then inserts its embedding rows (a concurrent insert of the same `(run_id, role)` waits for the first
  and then skips), then the run row through `append_event_in_session` when an event is given.
- `search_recall` and `recall_neighbours` take no lock (one read transaction each).
- `delete_message_embeddings_before` (retention) locks a batch of embedding rows
  `FOR UPDATE SKIP LOCKED` and deletes them: it never waits for, nor deadlocks with, a
  `delete_conversation` or `forget_resource` holding some of those rows (they are left to it).
- `delete_conversation` takes the conversation `FOR UPDATE` (waits for every writer above, and
  `create_turn`'s lock), deletes its working memory, observations and message embeddings, then
  updates its runs, then
  scrubs those runs' events (event rows come under their run row), then deletes their ledger rows.
- `forget_resource` locks the resource's conversations `FOR UPDATE` in id order (no new turn can
  start in them), refuses with ResourceBusy while any of them has a queued or running turn, else
  deletes the resource notes, then the resource's message embeddings, upserts the conversations'
  forget watermarks, then scrubs the `memory_updated` events of those conversations' runs,
  then deletes those runs' ledger rows, in one transaction; then deletes each conversation as
  above when asked.

Scrubbing (deletion keeps runs, events and usage rows as the audit and cost record, without text).
`scrub_event_data(data)` replaces an event's `data` with `{"redacted": true, ...kept}`, where kept
is, at the top level only: numbers, booleans and nulls; objects whose values are all numbers (usage
counts); and strings under SCRUB_KEEP_STRING_KEYS (ids, model and tool names, statuses and codes)
that also match the identifier pattern `^[A-Za-z0-9_.:@/-]{1,128}$`. Every other string, list and
object is dropped (inputs, message parts, tool args and results, error messages, processor details,
working memory key names). Type, seq, key and ts stay, so seq stays gap-free; `run_model_requests`
rows and run totals are untouched.

What each deletion removes:
- `delete_conversation`: the runs' `input`, `output`, `messages`, `request_context` and `error`
  (NULL; error codes survive in the scrubbed `run_failed` data); every event scrubbed; every
  `side_effects` row of those runs (`{run_id}:...` tool results and `wm:{run_id}:...` ledger rows);
  the conversation's working memory, observations and message embeddings.
- `forget_resource` without conversations: the notes, the message embeddings whose `resource_id`
  is the resource (of that agent, or every agent), the `memory_updated` events' key names and
  the ledger rows of the resource's runs. Those runs' own turn text stays (it is the conversation,
  not memory), so the model's `update_working_memory` tool-call events and `runs.messages` still
  hold the patch it sent; `include_conversations=True` removes those too.

Run checks: a write that names a run (`run_id`, or an event's run) must name a run of the same
tenant and agent, and of the same conversation for conversation-scoped notes and observations
(RunNotFound otherwise, nothing written).
- `load_snapshot` takes no lock: one REPEATABLE READ, READ ONLY transaction, so working memory,
  observations and turns come from one snapshot. A concurrent observation is either wholly in the
  view (and its turns are out of the history) or wholly not (and its turns are in).

Idempotency for DBOS steps (a re-executed step writes once):

- working memory: the ledger row `side_effects(tenant_id, key, state = 'completed', result)` is
  inserted in the write's transaction, with `key = "{ledger_key}|{agent_id}"` (`ledger_row_key`):
  one agent's key never answers another agent's write. A call whose row exists returns the stored
  result (`replayed=True`) and writes nothing; two concurrent calls with one key commit one write.
  Build ledger keys with `working_memory_ledger_key(run_id, tool_call_id)`. The ledger result holds
  the agent, scope, scope key, version, format and changed keys, never the content; the same key
  for another scope or scope key (or a stored result naming another agent) is MemoryLedgerConflict.
- observations: unique `(tenant_id, conversation_id, kind, covers_to_turn)`; a repeated insert or
  reflection returns the existing row (`status='existing'`).
- message embeddings: unique `(tenant_id, run_id, role)`; `index_turn` inserts with
  `ON CONFLICT DO NOTHING` and reports which roles already existed. An existing row is never
  replaced (not even under another embedding model: re-indexing after a model change is P1).
- an optional `MemoryEvent` (an event plus an optional usage row) is appended in the same
  transaction as the write, with EventLog's rules (a repeated event key is not written again).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    literal_column,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from eap_schemas.embedding_policy import parse_embedding_model_id
from eap_schemas.knowledge import approx_tokens as _approx_tokens
from eap_schemas.memory import (
    HistoryConfig,
    WorkingMemoryConfig,
    WorkingMemoryInvalid,
    merge_working_memory,
    validate_working_memory_content,
    validate_working_memory_schema,
    validate_working_memory_text,
)

from eap_run_store.knowledge import (
    HNSW_DIMENSIONS,
    HNSW_EF_CONSTRUCTION,
    HNSW_M,
    HNSW_MAX_EF_SEARCH,
    MIN_EF_SEARCH,
    vector_literal,
)
from eap_run_store.store import (
    TERMINAL_RUN_STATUSES,
    Base,
    ConversationNotFound,
    RunNotFound,
    TurnInProgress,
    append_event_in_session,
    validate_resource_id,
)

# Same texts as migration 0012_memory.
WM_SCOPE_CHECK = (
    "(scope = 'conversation' AND conversation_id IS NOT NULL AND conversation_id = scope_key)"
    " OR (scope = 'resource' AND conversation_id IS NULL)"
)
WM_FORMAT_CHECK = (
    "(format = 'markdown' AND jsonb_typeof(content) = 'string')"
    " OR (format = 'json' AND jsonb_typeof(content) = 'object')"
)
WM_VERSION_CHECK = "version >= 1"
OBS_KIND_CHECK = "kind IN ('observation', 'reflection')"
OBS_TURNS_CHECK = "covers_from_turn >= 1 AND covers_to_turn >= covers_from_turn"
OBS_TOKENS_CHECK = "approx_tokens >= 0"
OBS_SUPERSEDED_CHECK = "superseded_by IS NULL OR superseded_by <> id"
# Same texts as migration 0013_semantic_recall.
FW_TURN_CHECK = "forgotten_through_turn >= 0"
ME_ROLE_CHECK = "role IN ('user', 'assistant')"
ME_TURN_CHECK = "turn >= 1"
ME_EMBEDDING_CHECK = (
    "embedding IS NOT NULL AND dimensions IS NOT NULL AND dimensions IN (768, 1024, 1536)"
    " AND public.vector_dims(embedding) = dimensions"
)

WORKING_MEMORY_SCOPES = ("conversation", "resource")
WORKING_MEMORY_FORMATS = ("markdown", "json")
OBSERVATION_KINDS = ("observation", "reflection")
MESSAGE_ROLES = ("user", "assistant")
RECALL_SCOPES = ("resource", "conversation")
MAX_RECALL_TOP_K = 50
MAX_MESSAGE_RANGE = 10
MAX_RETENTION_BATCH = 10_000
LEDGER_PREFIX = "wm:"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:@/-]{1,128}$")
# Top-level string fields kept when an event is scrubbed (if they look like identifiers).
SCRUB_KEEP_STRING_KEYS = frozenset({
    "agent_id", "action", "code", "conversation_id", "error_code", "finish_reason", "format", "kind",
    "model_id", "phase", "price_source", "processor", "provider_model_name", "request_key", "run_id",
    "scope", "status", "step_id", "tool_call_id", "tool_id", "tool_name", "type",
})


# ---------------------------------------------------------------------------------------- models


class WorkingMemoryModel(Base):
    __tablename__ = "working_memories"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, primary_key=True)
    # Like conversations.agent_id: no FK (agents belong to the API's metadata).
    agent_id: Mapped[str] = mapped_column("agent_id", String, primary_key=True)
    scope: Mapped[str] = mapped_column("scope", String, primary_key=True)
    scope_key: Mapped[str] = mapped_column("scope_key", String, primary_key=True)
    # = scope_key for scope 'conversation' (the cascade FK), NULL for scope 'resource'.
    conversation_id: Mapped[str | None] = mapped_column("conversation_id", String, nullable=True)
    format: Mapped[str] = mapped_column("format", String, nullable=False)
    content: Mapped[Any] = mapped_column("content", JSONB, nullable=False)
    version: Mapped[int] = mapped_column("version", Integer, nullable=False)
    updated_by_run_id: Mapped[str | None] = mapped_column("updated_by_run_id", String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_working_memories_tenant_conversation",
            ondelete="CASCADE",
        ),
        CheckConstraint(WM_SCOPE_CHECK, name="ck_working_memories_scope"),
        CheckConstraint(WM_FORMAT_CHECK, name="ck_working_memories_format"),
        CheckConstraint(WM_VERSION_CHECK, name="ck_working_memories_version"),
        Index("ix_working_memories_tenant_scope_key", "tenant_id", "scope", "scope_key"),
        Index(
            "ix_working_memories_tenant_conversation",
            "tenant_id",
            "conversation_id",
            postgresql_where=text("conversation_id IS NOT NULL"),
        ),
        Index(
            "ix_working_memories_conversation_updated",
            "updated_at",
            postgresql_where=text("scope = 'conversation'"),
        ),
    )


class MemoryObservationModel(Base):
    __tablename__ = "memory_observations"

    id: Mapped[str] = mapped_column(
        "id", PG_UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    agent_id: Mapped[str] = mapped_column("agent_id", String, nullable=False)
    conversation_id: Mapped[str] = mapped_column("conversation_id", String, nullable=False)
    kind: Mapped[str] = mapped_column("kind", String, nullable=False)
    covers_from_turn: Mapped[int] = mapped_column("covers_from_turn", Integer, nullable=False)
    covers_to_turn: Mapped[int] = mapped_column("covers_to_turn", Integer, nullable=False)
    content: Mapped[str] = mapped_column("content", Text, nullable=False)
    approx_tokens: Mapped[int] = mapped_column("approx_tokens", Integer, nullable=False)
    run_id: Mapped[str | None] = mapped_column("run_id", String, nullable=True)
    superseded_by: Mapped[str | None] = mapped_column("superseded_by", PG_UUID(as_uuid=False), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_memory_observations_tenant_id_id"),
        UniqueConstraint("tenant_id", "conversation_id", "kind", "covers_to_turn", name="uq_memory_observations_slot"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_memory_observations_tenant_conversation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["runs.tenant_id", "runs.id"], name="fk_memory_observations_tenant_run"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "superseded_by"],
            ["memory_observations.tenant_id", "memory_observations.id"],
            name="fk_memory_observations_tenant_superseded_by",
        ),
        CheckConstraint(OBS_KIND_CHECK, name="ck_memory_observations_kind"),
        CheckConstraint(OBS_TURNS_CHECK, name="ck_memory_observations_turns"),
        CheckConstraint(OBS_TOKENS_CHECK, name="ck_memory_observations_tokens"),
        CheckConstraint(OBS_SUPERSEDED_CHECK, name="ck_memory_observations_superseded"),
        Index(
            "ix_memory_observations_tenant_superseded_by",
            "tenant_id",
            "superseded_by",
            postgresql_where=text("superseded_by IS NOT NULL"),
        ),
        Index("ix_memory_observations_created", "created_at"),
    )


class MessageEmbeddingModel(Base):
    __tablename__ = "message_embeddings"

    id: Mapped[str] = mapped_column(
        "id", PG_UUID(as_uuid=False), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[str] = mapped_column("tenant_id", String, nullable=False)
    agent_id: Mapped[str] = mapped_column("agent_id", String, nullable=False)
    # The conversation's resource_id at index time (immutable), NULL when it has none.
    resource_id: Mapped[str | None] = mapped_column("resource_id", String, nullable=True)
    conversation_id: Mapped[str] = mapped_column("conversation_id", String, nullable=False)
    turn: Mapped[int] = mapped_column("turn", Integer, nullable=False)
    run_id: Mapped[str] = mapped_column("run_id", String, nullable=False)
    role: Mapped[str] = mapped_column("role", String, nullable=False)
    text_: Mapped[str] = mapped_column("text", Text, nullable=False)
    embedding_model: Mapped[str] = mapped_column("embedding_model", String, nullable=False)
    dimensions: Mapped[int] = mapped_column("dimensions", SmallInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        "created_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    # Untyped vector (`public.vector` in the migration), as kb_chunks.embedding.
    embedding: Mapped[Any] = mapped_column("embedding", Vector(), nullable=False, deferred=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "run_id", "role", name="uq_message_embeddings_run_role"),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_message_embeddings_tenant_conversation",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_message_embeddings_tenant_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(ME_ROLE_CHECK, name="ck_message_embeddings_role"),
        CheckConstraint(ME_TURN_CHECK, name="ck_message_embeddings_turn"),
        CheckConstraint(ME_EMBEDDING_CHECK, name="ck_message_embeddings_embedding"),
        Index("ix_message_embeddings_tenant_agent_resource", "tenant_id", "agent_id", "resource_id"),
        Index("ix_message_embeddings_tenant_conversation_turn", "tenant_id", "conversation_id", "turn"),
        Index("ix_message_embeddings_tenant_created", "tenant_id", "created_at"),
        *(
            Index(
                f"ix_message_embeddings_embedding_{dims}",
                literal_column(f"(embedding::public.vector({dims}))").label(f"embedding_{dims}"),
                postgresql_ops={f"embedding_{dims}": "public.vector_cosine_ops"},
                postgresql_using="hnsw",
                postgresql_with={"m": HNSW_M, "ef_construction": HNSW_EF_CONSTRUCTION},
                postgresql_where=text(f"dimensions = {dims}"),
            )
            for dims in HNSW_DIMENSIONS
        ),
    )


class MemoryForgetWatermarkModel(Base):
    """forget_resource's mark on a conversation: index_turn skips turns <= forgotten_through_turn."""

    __tablename__ = "memory_forget_watermarks"

    tenant_id: Mapped[str] = mapped_column("tenant_id", String, primary_key=True)
    conversation_id: Mapped[str] = mapped_column("conversation_id", String, primary_key=True)
    forgotten_through_turn: Mapped[int] = mapped_column("forgotten_through_turn", Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        "updated_at", DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_memory_forget_watermarks_tenant_conversation",
            ondelete="CASCADE",
        ),
        CheckConstraint(FW_TURN_CHECK, name="ck_memory_forget_watermarks_turn"),
    )


# ---------------------------------------------------------------------------------------- errors


class WorkingMemoryVersionConflict(RuntimeError):
    """The note's version is not the one the caller expected (`If-Match`; API: 409)."""

    code = "working_memory_version_conflict"

    def __init__(self, scope: str, scope_key: str, expected: int, actual: int) -> None:
        self.scope = scope
        self.scope_key = scope_key
        self.expected = expected
        self.actual = actual
        super().__init__(scope, scope_key, expected, actual)  # pickle-safe

    def __str__(self) -> str:
        return f"working memory version is {self.actual}, not {self.expected}"


class MemoryLedgerConflict(RuntimeError):
    """The ledger key exists but is not a completed working memory write (another writer's key)."""

    code = "memory_ledger_conflict"

    def __init__(self, key: str, state: str) -> None:
        self.key = key
        self.state = state
        super().__init__(key, state)  # pickle-safe

    def __str__(self) -> str:
        return f"ledger key {self.key!r} is in state {self.state!r}, not a completed working memory write"


class ObservationNotLatest(RuntimeError):
    """delete_observation(only_latest=True) refused: the row is superseded, or a newer active row
    covers later turns. Coverage is the highest active `covers_to_turn`, so deleting an older row
    would hide its turns from both the history and the observations (API: 409). Nothing deleted."""

    code = "observation_not_latest"

    def __init__(self, conversation_id: str, observation_id: str) -> None:
        self.conversation_id = conversation_id
        self.observation_id = observation_id
        super().__init__(conversation_id, observation_id)  # pickle-safe

    def __str__(self) -> str:
        return f"observation {self.observation_id} is not the conversation's newest active observation"


class ResourceBusy(TurnInProgress):
    """forget_resource refused: a conversation of the resource has a queued or running turn (its
    write could recreate the note after the delete). Nothing was deleted; retry when the turns end
    (API: 409). `conversation_id`, `run_id` and `status` name the first busy turn."""

    code = "resource_busy"

    def __init__(
        self, resource_id: str, conversation_id: str, run_id: str, status: str, busy_conversations: Sequence[str] = ()
    ) -> None:
        super().__init__(conversation_id, run_id, status)
        self.resource_id = resource_id
        self.busy_conversations = tuple(busy_conversations)
        self.args = (resource_id, conversation_id, run_id, status, self.busy_conversations)  # pickle-safe

    def __str__(self) -> str:
        return (
            f"a turn is still in progress in {len(self.busy_conversations)} conversation(s) of this resource "
            f"(first: {self.conversation_id}, run {self.run_id} is {self.status})"
        )


# ---------------------------------------------------------------------------------- value types


@dataclass(frozen=True)
class MemoryEvent:
    """An event (and optionally its usage row) appended in the write's transaction.

    `data` is merged over the default data the store builds (counts, ids, versions; never text).
    `usage` is a run_model_requests row as for EventLog.append (e.g. the observer's
    `memory:observe:{turn}` request)."""

    run_id: str
    event_key: str
    type: str = "memory_updated"
    data: Mapping[str, Any] | None = None
    usage: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class WorkingMemory:
    agent_id: str
    scope: str
    scope_key: str
    format: str  # 'markdown' (content: str) or 'json' (content: dict)
    content: str | dict[str, Any]
    version: int
    updated_by_run_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class WorkingMemoryWrite:
    """The result of update_working_memory, and what the ledger stores (no content)."""

    scope: str
    format: str
    version: int
    changed_keys: tuple[str, ...]  # top-level keys a json write touched; () for markdown
    replayed: bool = False  # True: the ledger key existed, nothing was written
    agent_id: str = ""
    scope_key: str = ""
    # The row as this write stored it (RETURNING, same transaction); None on a replay. Never in the
    # ledger or event data.
    note: "WorkingMemory | None" = field(default=None, compare=False, repr=False)

    def event_data(self) -> dict[str, Any]:
        """The default `memory_updated` data: no content, no resource id."""
        return {"scope": self.scope, "format": self.format, "version": self.version, "changed_keys": list(self.changed_keys)}

    def ledger_result(self) -> dict[str, Any]:
        return {**self.event_data(), "agent_id": self.agent_id, "scope_key": self.scope_key}

    @classmethod
    def from_ledger(cls, result: Mapping[str, Any]) -> "WorkingMemoryWrite":
        return cls(
            scope=result["scope"],
            format=result["format"],
            version=int(result["version"]),
            changed_keys=tuple(result.get("changed_keys") or ()),
            replayed=True,
            agent_id=result.get("agent_id", ""),
            scope_key=result.get("scope_key", ""),
        )


@dataclass(frozen=True)
class Observation:
    id: str
    agent_id: str
    conversation_id: str
    kind: str  # 'observation' | 'reflection'
    covers_from_turn: int
    covers_to_turn: int
    content: str
    approx_tokens: int
    run_id: str | None
    superseded_by: str | None
    created_at: datetime


@dataclass(frozen=True)
class ObservationWrite:
    """status: 'created'; 'existing' (the slot was written before: a re-executed step); 'overlap'
    (covers_from_turn <= covered_to_turn: another writer got there first; nothing written); 'gap'
    (covers_from_turn > covered_to_turn + 1: nothing written); 'ahead' (covers_to_turn is after the
    conversation's last completed turn: nothing written); 'stale' (reflect only: the rows to
    supersede are no longer exactly the active rows up to their end; nothing written)."""

    status: Literal["created", "existing", "overlap", "gap", "stale", "ahead"]
    observation: Observation | None
    covered_to_turn: int  # the active rows' end after this call
    superseded: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemorySnapshot:
    """What `eap.load_memory` assembles from, read in one REPEATABLE READ transaction.

    `observations`: the active rows (at most one reflection, then observations), oldest first.
    `turns`: the COMPLETED turns after `covered_to_turn` (and before `before_turn`), oldest first,
    after the history limits; each is completed_turns_before's dict plus `approx_tokens`.
    `working_memory_key` is None when working memory is off, or resource-scoped and the
    conversation has no resource_id (then `working_memory` is None too)."""

    conversation_id: str
    agent_id: str
    resource_id: str | None
    working_memory_scope: str | None
    working_memory_key: str | None
    working_memory: WorkingMemory | None
    observations: tuple[Observation, ...]
    covered_to_turn: int
    turns: tuple[dict[str, Any], ...]
    trimmed_turns: int  # completed uncovered turns dropped by the limits

    @property
    def turn_numbers(self) -> tuple[int, ...]:
        return tuple(t["turn"] for t in self.turns)


@dataclass(frozen=True)
class ForgetResult:
    working_memories: int  # resource notes deleted
    conversations: tuple[str, ...]  # conversations deleted (include_conversations)
    busy_conversations: tuple[str, ...]  # skipped: a turn was still queued or running
    message_embeddings: int = 0  # recall rows of the resource deleted (RM.8)


@dataclass(frozen=True)
class TurnText:
    """One text of a turn to index: role `user` (the processed input) or `assistant` (the final
    answer), already redacted, and its embedding."""

    role: Literal["user", "assistant"]
    text: str
    embedding: Sequence[float]


@dataclass(frozen=True)
class IndexTurnResult:
    """`inserted`: roles written by this call; `existing`: roles already indexed (a re-executed
    step), left as they were. `skipped`: the turn is at or below the conversation's forget
    watermark (its resource was forgotten after it); nothing was written (no rows, no event)."""

    conversation_id: str
    turn: int
    run_id: str
    resource_id: str | None
    inserted: tuple[str, ...]
    existing: tuple[str, ...]
    skipped: bool = False


@dataclass(frozen=True)
class RecallHit:
    """One message_embeddings row found by search_recall."""

    id: str
    conversation_id: str
    turn: int
    run_id: str
    role: str
    text: str
    created_at: datetime
    distance: float
    score: float  # 1 - cosine distance


@dataclass(frozen=True)
class RecallMessage:
    """One message_embeddings row returned by recall_neighbours (no distance)."""

    id: str
    conversation_id: str
    turn: int
    run_id: str
    role: str
    text: str
    created_at: datetime


# --------------------------------------------------------------------------------------- helpers


def working_memory_ledger_key(run_id: str, tool_call_id: str) -> str:
    """The ledger key of one `update_working_memory` tool call: `wm:{run_id}:{tool_call_id}`."""
    if not run_id or not tool_call_id:
        raise ValueError("run_id and tool_call_id are required")
    return f"{LEDGER_PREFIX}{run_id}:{tool_call_id}"


def ledger_row_key(ledger_key: str, agent_id: str) -> str:
    """The side_effects key of a working memory write: `{ledger_key}|{agent_id}`."""
    return f"{ledger_key}|{agent_id}"


def turn_approx_tokens(turn: Mapping[str, Any]) -> int:
    """A turn's size for `history.max_tokens`: ceil(chars / 4) of its stored messages JSON
    (compact), or of input + output for a turn completed without messages."""
    messages = turn.get("messages")
    if messages:
        return _approx_tokens(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))
    return _approx_tokens((turn.get("input") or "") + (turn.get("output") or ""))


def apply_history_limits(
    turns: Sequence[dict[str, Any]], history: HistoryConfig | None
) -> tuple[list[dict[str, Any]], int]:
    """Keep the newest `last_turns` whole turns, then drop the oldest until the total
    `approx_tokens` fits `max_tokens`. Returns (kept turns oldest first, dropped count)."""
    kept = list(turns)
    if history is None:
        return kept, 0
    kept = kept[-history.last_turns :] if history.last_turns else kept
    if history.max_tokens is not None:
        total = 0
        start = len(kept)
        for i in range(len(kept) - 1, -1, -1):
            if total + kept[i]["approx_tokens"] > history.max_tokens:
                break
            total += kept[i]["approx_tokens"]
            start = i
        kept = kept[start:]
    return kept, len(turns) - len(kept)


def scrub_event_data(data: Any) -> dict[str, Any]:
    """An event's data without any text (see the module docstring): `{"redacted": true, ...kept}`."""
    kept: dict[str, Any] = {}
    if isinstance(data, dict):
        for key, value in data.items():
            if value is None or isinstance(value, (bool, int, float)):
                kept[key] = value
            elif isinstance(value, str):
                if key in SCRUB_KEEP_STRING_KEYS and _IDENTIFIER.fullmatch(value):
                    kept[key] = value
            elif isinstance(value, dict) and all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in value.values()
            ):
                kept[key] = dict(value)
    kept["redacted"] = True
    return kept


def _like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _scrub_runs(
    session, tenant_id: str, run_ids: Sequence[str], *, event_type: str | None = None, all_side_effects: bool = False
) -> int:
    """Scrub the events of `run_ids` (only `event_type` when given) and delete their side_effects
    rows: the working memory ledger rows (`wm:{run_id}:...`), and with `all_side_effects` every row
    (`{run_id}:...` too: tool results). Returns the number of events rewritten. Prefix deletes use
    `LIKE 'prefix%'` on ix_side_effects_tenant_key_pattern (text_pattern_ops)."""
    if not run_ids:
        return 0
    sql = "SELECT run_id, seq, data FROM execution_events WHERE tenant_id = :t AND run_id = ANY(:ids)"
    params: dict[str, Any] = {"t": tenant_id, "ids": list(run_ids)}
    if event_type is not None:
        sql += " AND type = :type"
        params["type"] = event_type
    updates = []
    for r in session.execute(text(sql + " ORDER BY run_id, seq FOR UPDATE"), params).all():
        scrubbed = scrub_event_data(r.data)
        if scrubbed != r.data:
            updates.append({"t": tenant_id, "r": r.run_id, "s": r.seq, "d": json.dumps(scrubbed)})
    if updates:
        session.execute(
            text(
                "UPDATE execution_events SET data = CAST(:d AS jsonb)"
                " WHERE tenant_id = :t AND run_id = :r AND seq = :s"
            ),
            updates,
        )
    patterns = [{"t": tenant_id, "p": _like_prefix(f"{LEDGER_PREFIX}{rid}:")} for rid in run_ids]
    if all_side_effects:
        patterns += [{"t": tenant_id, "p": _like_prefix(f"{rid}:")} for rid in run_ids]
    session.execute(
        text("DELETE FROM side_effects WHERE tenant_id = :t AND key LIKE :p ESCAPE '\\'"), patterns
    )
    return len(updates)


def _check_run(session, tenant_id: str, run_ids: Iterable[str | None], agent_id: str, conversation_id: str | None) -> None:
    """Each named run must be the tenant's, of `agent_id`, and (when given) of `conversation_id`;
    RunNotFound otherwise (the session is rolled back)."""
    for run_id in {str(r) for r in run_ids if r is not None}:
        row = session.execute(
            text("SELECT agent_id, conversation_id FROM runs WHERE tenant_id = :t AND id = :r"),
            {"t": tenant_id, "r": run_id},
        ).first()
        if (
            row is None
            or row.agent_id != agent_id
            or (conversation_id is not None and row.conversation_id != conversation_id)
        ):
            session.rollback()
            raise RunNotFound(tenant_id, run_id)


def _require_tenant(tenant_id: str) -> None:
    if not tenant_id:
        raise ValueError("tenant_id is required")


def _require_agent(agent_id: Any) -> str:
    if not agent_id:
        raise ValueError("agent_id is required")
    return str(agent_id)


def _check_scope(scope: str, scope_key: str) -> None:
    if scope not in WORKING_MEMORY_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(WORKING_MEMORY_SCOPES)}")
    if not scope_key:
        raise ValueError("scope_key is required")
    if scope == "resource":
        validate_resource_id(scope_key)


def _canonical_uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _wm(row: Mapping[str, Any]) -> WorkingMemory:
    return WorkingMemory(
        agent_id=row["agent_id"],
        scope=row["scope"],
        scope_key=row["scope_key"],
        format=row["format"],
        content=row["content"],
        version=row["version"],
        updated_by_run_id=row["updated_by_run_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _obs(row: Mapping[str, Any]) -> Observation:
    return Observation(
        id=str(row["id"]),
        agent_id=row["agent_id"],
        conversation_id=row["conversation_id"],
        kind=row["kind"],
        covers_from_turn=row["covers_from_turn"],
        covers_to_turn=row["covers_to_turn"],
        content=row["content"],
        approx_tokens=row["approx_tokens"],
        run_id=row["run_id"],
        superseded_by=None if row["superseded_by"] is None else str(row["superseded_by"]),
        created_at=row["created_at"],
    )


def _changed_keys(old: Any, new: Any, patch: Mapping[str, Any] | None) -> tuple[str, ...]:
    if patch is not None:
        return tuple(sorted(patch))
    if not isinstance(new, dict):
        return ()
    base = old if isinstance(old, dict) else {}
    return tuple(sorted(k for k in set(base) | set(new) if base.get(k, _MISSING) != new.get(k, _MISSING)))


_MISSING: Any = object()

_WM_COLUMNS = "agent_id, scope, scope_key, format, content, version, updated_by_run_id, created_at, updated_at"
_OBS_COLUMNS = (
    "id, agent_id, conversation_id, kind, covers_from_turn, covers_to_turn, content, approx_tokens, run_id,"
    " superseded_by, created_at"
)


def _append_event(session, tenant_id: str, event: MemoryEvent | None, default: dict[str, Any]) -> None:
    if event is None:
        return
    append_event_in_session(
        session,
        tenant_id,
        event.run_id,
        event.event_key,
        event.type,
        {**default, **dict(event.data or {})},
        dict(event.usage) if event.usage is not None else None,
    )


# ------------------------------------------------------------------------------------------ store


class MemoryStore:
    """Working memory, observations, the memory snapshot and memory deletions. Tenant- and
    agent-scoped in SQL; see the module docstring for the lock order and idempotency."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.Session = sessionmaker(bind=engine)

    # ------------------------------------------------------------------ conversation lock

    @staticmethod
    def _lock_conversation(session, tenant_id: str, agent_id: str, conversation_id: str, mode: str) -> Mapping[str, Any]:
        """Lock the tenant's conversation (`mode`: KEY SHARE, NO KEY UPDATE, UPDATE) and check that it
        belongs to `agent_id`. Raises ConversationNotFound otherwise (the caller's session is rolled back)."""
        row = session.execute(
            text(
                "SELECT id, agent_id, resource_id FROM conversations WHERE tenant_id = :t AND id = :c"
                f" FOR {mode}"
            ),
            {"t": tenant_id, "c": conversation_id},
        ).mappings().first()
        if row is None or row["agent_id"] != agent_id:
            session.rollback()
            raise ConversationNotFound(tenant_id, conversation_id)
        return row

    # ------------------------------------------------------------------ working memory

    def get_working_memory(self, tenant_id: str, agent_id: str, scope: str, scope_key: str) -> WorkingMemory | None:
        """The agent's note for this scope and key, or None."""
        _require_tenant(tenant_id)
        agent = _require_agent(agent_id)
        _check_scope(scope, scope_key)
        with self.engine.connect() as conn:
            row = conn.execute(
                text(
                    f"SELECT {_WM_COLUMNS} FROM working_memories"
                    " WHERE tenant_id = :t AND agent_id = :a AND scope = :s AND scope_key = :k"
                ),
                {"t": tenant_id, "a": agent, "s": scope, "k": scope_key},
            ).mappings().first()
        return None if row is None else _wm(row)

    def list_working_memories(
        self, tenant_id: str, agent_id: str, *, scope: str | None = None, limit: int = 100
    ) -> list[WorkingMemory]:
        """The agent's notes (optionally one scope), most recently updated first, at most `limit`."""
        _require_tenant(tenant_id)
        agent = _require_agent(agent_id)
        if scope is not None and scope not in WORKING_MEMORY_SCOPES:
            raise ValueError(f"scope must be one of {', '.join(WORKING_MEMORY_SCOPES)}")
        sql = f"SELECT {_WM_COLUMNS} FROM working_memories WHERE tenant_id = :t AND agent_id = :a"
        params: dict[str, Any] = {"t": tenant_id, "a": agent, "n": max(1, min(int(limit), 1000))}
        if scope is not None:
            sql += " AND scope = :s"
            params["s"] = scope
        sql += " ORDER BY updated_at DESC, scope, scope_key LIMIT :n"
        with self.engine.connect() as conn:
            return [_wm(r) for r in conn.execute(text(sql), params).mappings().all()]

    def update_working_memory(
        self,
        tenant_id: str,
        agent_id: str,
        *,
        scope: str,
        scope_key: str,
        content: str | dict[str, Any] | None = None,
        patch: dict[str, Any] | None = None,
        json_schema: dict[str, Any] | None = None,
        ledger_key: str | None = None,
        expected_version: int | None = None,
        run_id: str | None = None,
        event: MemoryEvent | None = None,
    ) -> WorkingMemoryWrite:
        """Write the agent's note for (scope, scope_key) in one transaction: lock (or insert) the
        row, apply the change, increment `version`, append `event`, insert the ledger row.

        Exactly one of:
        - `content: str` — template mode, **replace** (format `markdown`, at most 16 KiB);
        - `content: dict` — schema mode, **replace** (format `json`; Studio PUT), validated against
          `json_schema`;
        - `patch: dict` — schema mode, **deep-merge** (`merge_working_memory`: null deletes, arrays
          replace), the result validated against `json_schema`.

        A json write onto a note stored as markdown (the agent changed mode) starts from `{}`.
        `ledger_key` (a tool call: `working_memory_ledger_key(run_id, tool_call_id)`): if it exists,
        the first write's result is returned with `replayed=True` and nothing is written.
        `expected_version` (`If-Match`; 0 = the note must not exist): WorkingMemoryVersionConflict
        when it differs. `run_id` is recorded as `updated_by_run_id`. `event` defaults to data
        `{scope, format, version, changed_keys}`.

        Raises WorkingMemoryInvalid (nothing written; model-visible), WorkingMemoryVersionConflict,
        ConversationNotFound (conversation scope: unknown, another tenant's or another agent's
        conversation, or deleted meanwhile), MemoryLedgerConflict, RunNotFound (`run_id` or the
        event's run is not this tenant's and agent's, or for conversation scope not of that
        conversation).
        """
        _require_tenant(tenant_id)
        agent = _require_agent(agent_id)
        _check_scope(scope, scope_key)
        if (content is None) == (patch is None):
            raise ValueError("pass exactly one of content and patch")
        if patch is not None or isinstance(content, dict):
            fmt = "json"
            if json_schema is None:
                raise ValueError("json_schema is required for a json write")
            validate_working_memory_schema(json_schema)
            if patch is not None and not isinstance(patch, dict):
                raise WorkingMemoryInvalid("the patch must be a JSON object")
            if isinstance(content, dict):
                validate_working_memory_content(json_schema, content)
        else:
            fmt = "markdown"
            validate_working_memory_text(content)

        params = {"t": tenant_id, "a": agent, "s": scope, "k": scope_key}
        row_key = None if ledger_key is None else ledger_row_key(ledger_key, agent)
        with self.Session() as session:
            if row_key is not None:
                replay = self._ledger_result(session, tenant_id, row_key, agent, scope, scope_key)
                if replay is not None:
                    session.rollback()
                    return replay
            _check_run(
                session,
                tenant_id,
                (run_id, None if event is None else event.run_id),
                agent,
                scope_key if scope == "conversation" else None,
            )
            if scope == "conversation":
                self._lock_conversation(session, tenant_id, agent, scope_key, "KEY SHARE")
            select_locked = text(
                "SELECT format, content, version FROM working_memories"
                " WHERE tenant_id = :t AND agent_id = :a AND scope = :s AND scope_key = :k FOR UPDATE"
            )
            row = session.execute(select_locked, params).mappings().first()
            if row is None:
                if expected_version not in (None, 0):
                    session.rollback()
                    raise WorkingMemoryVersionConflict(scope, scope_key, int(expected_version), 0)
                new = self._new_content(fmt, None, content, patch, json_schema)
                try:
                    inserted = session.execute(
                        text(
                            "INSERT INTO working_memories (tenant_id, agent_id, scope, scope_key, conversation_id,"
                            " format, content, version, updated_by_run_id)"
                            " VALUES (:t, :a, :s, :k, :conv, :f, CAST(:content AS jsonb), 1, :run)"
                            f" ON CONFLICT ON CONSTRAINT working_memories_pkey DO NOTHING RETURNING {_WM_COLUMNS}"
                        ),
                        {
                            **params,
                            "conv": scope_key if scope == "conversation" else None,
                            "f": fmt,
                            "content": json.dumps(new, ensure_ascii=False),
                            "run": run_id,
                        },
                    ).mappings().first()
                except IntegrityError as exc:
                    session.rollback()
                    if "fk_working_memories_tenant_conversation" in str(exc.orig):
                        raise ConversationNotFound(tenant_id, scope_key) from None
                    raise
                if inserted is not None:
                    result = WorkingMemoryWrite(
                        scope, fmt, 1, _changed_keys(None, new, patch), False, agent, scope_key, _wm(inserted)
                    )
                    return self._finish_write(session, tenant_id, row_key, event, result)
                # A concurrent first write won the insert: wait for it, then apply on top of it.
                row = session.execute(select_locked, params).mappings().one()
            if expected_version is not None and row["version"] != expected_version:
                session.rollback()
                raise WorkingMemoryVersionConflict(scope, scope_key, int(expected_version), row["version"])
            old = row["content"] if row["format"] == fmt else None
            new = self._new_content(fmt, old, content, patch, json_schema)
            stored = session.execute(
                text(
                    "UPDATE working_memories SET format = :f, content = CAST(:content AS jsonb),"
                    " version = version + 1, updated_by_run_id = :run, updated_at = now()"
                    " WHERE tenant_id = :t AND agent_id = :a AND scope = :s AND scope_key = :k"
                    f" RETURNING {_WM_COLUMNS}"
                ),
                {**params, "f": fmt, "content": json.dumps(new, ensure_ascii=False), "run": run_id},
            ).mappings().one()
            result = WorkingMemoryWrite(
                scope, fmt, stored["version"], _changed_keys(old, new, patch), False, agent, scope_key, _wm(stored)
            )
            return self._finish_write(session, tenant_id, row_key, event, result)

    @staticmethod
    def _new_content(
        fmt: str,
        old: Any,
        content: str | dict[str, Any] | None,
        patch: dict[str, Any] | None,
        json_schema: dict[str, Any] | None,
    ) -> str | dict[str, Any]:
        if fmt == "markdown":
            return content  # validated already
        if patch is None:
            return content  # a validated replace
        merged = merge_working_memory(old if isinstance(old, dict) else None, patch)
        return validate_working_memory_content(json_schema, merged)

    @staticmethod
    def _ledger_result(
        session, tenant_id: str, key: str, agent_id: str, scope: str, scope_key: str
    ) -> WorkingMemoryWrite | None:
        row = session.execute(
            text("SELECT state, result FROM side_effects WHERE tenant_id = :t AND key = :k"),
            {"t": tenant_id, "k": key},
        ).first()
        if row is None:
            return None
        state, result = row
        if state != "completed" or not isinstance(result, dict) or "version" not in result:
            raise MemoryLedgerConflict(key, state)
        if (result.get("agent_id"), result.get("scope"), result.get("scope_key")) != (agent_id, scope, scope_key):
            raise MemoryLedgerConflict(key, "mismatch")
        return WorkingMemoryWrite.from_ledger(result)

    def _finish_write(
        self, session, tenant_id: str, ledger_key: str | None, event: MemoryEvent | None, result: WorkingMemoryWrite
    ) -> WorkingMemoryWrite:
        try:
            _append_event(session, tenant_id, event, result.event_data())
        except Exception:
            session.rollback()
            raise
        if ledger_key is not None:
            now = datetime.now(timezone.utc)
            stored = session.execute(
                text(
                    "INSERT INTO side_effects (tenant_id, key, state, result, created_at, completed_at)"
                    " VALUES (:t, :k, 'completed', CAST(:r AS jsonb), :now, :now)"
                    " ON CONFLICT ON CONSTRAINT side_effects_pkey DO NOTHING RETURNING key"
                ),
                {"t": tenant_id, "k": ledger_key, "r": json.dumps(result.ledger_result()), "now": now},
            ).first()
            if stored is None:
                # The same key committed concurrently: that write stands, this one is undone.
                session.rollback()
                with self.Session() as again:
                    replay = self._ledger_result(
                        again, tenant_id, ledger_key, result.agent_id, result.scope, result.scope_key
                    )
                if replay is None:  # pragma: no cover - the conflicting row cannot vanish
                    raise MemoryLedgerConflict(ledger_key, "missing")
                return replay
        session.commit()
        return result

    def delete_working_memory(
        self, tenant_id: str, agent_id: str, scope: str, scope_key: str, *, expected_version: int | None = None
    ) -> bool:
        """Delete the agent's note. False when there was none (idempotent). With `expected_version`,
        WorkingMemoryVersionConflict when the note's version differs (0 for a missing note)."""
        _require_tenant(tenant_id)
        agent = _require_agent(agent_id)
        _check_scope(scope, scope_key)
        params = {"t": tenant_id, "a": agent, "s": scope, "k": scope_key}
        with self.Session() as session:
            if expected_version is not None:
                current = session.execute(
                    text(
                        "SELECT version FROM working_memories"
                        " WHERE tenant_id = :t AND agent_id = :a AND scope = :s AND scope_key = :k FOR UPDATE"
                    ),
                    params,
                ).scalar_one_or_none()
                if (current or 0) != expected_version:
                    session.rollback()
                    raise WorkingMemoryVersionConflict(scope, scope_key, int(expected_version), current or 0)
            deleted = session.execute(
                text(
                    "DELETE FROM working_memories"
                    " WHERE tenant_id = :t AND agent_id = :a AND scope = :s AND scope_key = :k"
                ),
                params,
            ).rowcount
            session.commit()
        return deleted > 0

    # ------------------------------------------------------------------ observations

    @staticmethod
    def _covered_to(session, tenant_id: str, conversation_id: str) -> int:
        return session.execute(
            text(
                "SELECT coalesce(max(covers_to_turn), 0) FROM memory_observations"
                " WHERE tenant_id = :t AND conversation_id = :c AND superseded_by IS NULL"
            ),
            {"t": tenant_id, "c": conversation_id},
        ).scalar_one()

    @staticmethod
    def _slot(session, tenant_id: str, conversation_id: str, kind: str, covers_to_turn: int) -> Observation | None:
        row = session.execute(
            text(
                f"SELECT {_OBS_COLUMNS} FROM memory_observations"
                " WHERE tenant_id = :t AND conversation_id = :c AND kind = :kind AND covers_to_turn = :to"
            ),
            {"t": tenant_id, "c": conversation_id, "kind": kind, "to": covers_to_turn},
        ).mappings().first()
        return None if row is None else _obs(row)

    def insert_observation(
        self,
        tenant_id: str,
        agent_id: str,
        conversation_id: str,
        *,
        covers_from_turn: int,
        covers_to_turn: int,
        content: str,
        approx_tokens: int | None = None,
        run_id: str | None = None,
        event: MemoryEvent | None = None,
    ) -> ObservationWrite:
        """Record an observation over turns covers_from_turn..covers_to_turn, once.

        Idempotent by (conversation, 'observation', covers_to_turn): a repeat returns
        status 'existing' with the stored row (and appends `event` if it is missing). Otherwise
        `covers_from_turn` must equal the active rows' end + 1 ('overlap' / 'gap' otherwise, nothing
        written), and `covers_to_turn` must not exceed the last completed turn ('ahead' otherwise).
        `approx_tokens` defaults to ceil(len(content) / 4). `run_id` and the event's run must be
        turns of this conversation (RunNotFound). `event` (e.g. the observer's
        usage row) defaults to data `{kind, covers_from_turn, covers_to_turn, approx_tokens}`.
        Raises ConversationNotFound (unknown, another tenant's or agent's, or deleted)."""
        _require_tenant(tenant_id)
        agent = _require_agent(agent_id)
        conv = str(conversation_id)
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content is required")
        if not 1 <= int(covers_from_turn) <= int(covers_to_turn):
            raise ValueError("turns must satisfy 1 <= covers_from_turn <= covers_to_turn")
        tokens = _approx_tokens(content) if approx_tokens is None else int(approx_tokens)
        with self.Session() as session:
            self._lock_conversation(session, tenant_id, agent, conv, "NO KEY UPDATE")
            _check_run(session, tenant_id, (run_id, None if event is None else event.run_id), agent, conv)
            existing = self._slot(session, tenant_id, conv, "observation", int(covers_to_turn))
            covered = self._covered_to(session, tenant_id, conv)
            if existing is not None:
                self._event_or_rollback(session, tenant_id, event, existing)
                session.commit()
                return ObservationWrite("existing", existing, covered)
            if covers_from_turn <= covered:
                session.rollback()
                return ObservationWrite("overlap", None, covered)
            if covers_from_turn > covered + 1:
                session.rollback()
                return ObservationWrite("gap", None, covered)
            last_completed = session.execute(
                text(
                    "SELECT coalesce(max(turn), 0) FROM runs"
                    " WHERE tenant_id = :t AND conversation_id = :c AND status = 'completed'"
                ),
                {"t": tenant_id, "c": conv},
            ).scalar_one()
            if covers_to_turn > last_completed:
                session.rollback()
                return ObservationWrite("ahead", None, covered)
            row = session.execute(
                text(
                    "INSERT INTO memory_observations (tenant_id, agent_id, conversation_id, kind, covers_from_turn,"
                    " covers_to_turn, content, approx_tokens, run_id)"
                    " VALUES (:t, :a, :c, 'observation', :from, :to, :content, :tokens, :run)"
                    f" RETURNING {_OBS_COLUMNS}"
                ),
                {
                    "t": tenant_id,
                    "a": agent,
                    "c": conv,
                    "from": int(covers_from_turn),
                    "to": int(covers_to_turn),
                    "content": content,
                    "tokens": tokens,
                    "run": run_id,
                },
            ).mappings().one()
            created = _obs(row)
            self._event_or_rollback(session, tenant_id, event, created)
            session.commit()
            return ObservationWrite("created", created, created.covers_to_turn)

    def reflect(
        self,
        tenant_id: str,
        agent_id: str,
        conversation_id: str,
        *,
        supersede_ids: Iterable[str],
        content: str,
        approx_tokens: int | None = None,
        run_id: str | None = None,
        event: MemoryEvent | None = None,
    ) -> ObservationWrite:
        """Replace the active rows `supersede_ids` with one reflection, in one transaction.

        The reflection covers min(covers_from_turn)..max(covers_to_turn) of those rows, which must
        be exactly the active rows up to that end ('stale' otherwise: a concurrent reflection or
        deletion; nothing written). Idempotent by (conversation, 'reflection', covers_to_turn): a
        re-executed step gets status 'existing'. `event` defaults to data
        `{kind, covers_from_turn, covers_to_turn, approx_tokens, superseded}` (a count).
        Raises ConversationNotFound, ValueError for an empty or non-UUID id list."""
        _require_tenant(tenant_id)
        agent = _require_agent(agent_id)
        conv = str(conversation_id)
        ids = sorted({_canonical_uuid(i) or "" for i in supersede_ids})
        if not ids or "" in ids:
            raise ValueError("supersede_ids must be a non-empty list of observation ids")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content is required")
        tokens = _approx_tokens(content) if approx_tokens is None else int(approx_tokens)
        with self.Session() as session:
            self._lock_conversation(session, tenant_id, agent, conv, "NO KEY UPDATE")
            _check_run(session, tenant_id, (run_id, None if event is None else event.run_id), agent, conv)
            rows = session.execute(
                text(
                    f"SELECT {_OBS_COLUMNS} FROM memory_observations"
                    " WHERE tenant_id = :t AND conversation_id = :c AND id = ANY(CAST(:ids AS uuid[]))"
                    " ORDER BY covers_from_turn FOR UPDATE"
                ),
                {"t": tenant_id, "c": conv, "ids": ids},
            ).mappings().all()
            covered = self._covered_to(session, tenant_id, conv)
            if len(rows) != len(ids):
                session.rollback()
                return ObservationWrite("stale", None, covered)
            covers_from = min(r["covers_from_turn"] for r in rows)
            covers_to = max(r["covers_to_turn"] for r in rows)
            existing = self._slot(session, tenant_id, conv, "reflection", covers_to)
            if existing is not None:
                self._event_or_rollback(session, tenant_id, event, existing, superseded=len(ids))
                session.commit()
                return ObservationWrite("existing", existing, covered)
            active_up_to = set(
                str(i)
                for i in session.execute(
                    text(
                        "SELECT id FROM memory_observations WHERE tenant_id = :t AND conversation_id = :c"
                        " AND superseded_by IS NULL AND covers_to_turn <= :to"
                    ),
                    {"t": tenant_id, "c": conv, "to": covers_to},
                ).scalars()
            )
            if active_up_to != set(ids):
                session.rollback()
                return ObservationWrite("stale", None, covered)
            row = session.execute(
                text(
                    "INSERT INTO memory_observations (tenant_id, agent_id, conversation_id, kind, covers_from_turn,"
                    " covers_to_turn, content, approx_tokens, run_id)"
                    " VALUES (:t, :a, :c, 'reflection', :from, :to, :content, :tokens, :run)"
                    f" RETURNING {_OBS_COLUMNS}"
                ),
                {
                    "t": tenant_id,
                    "a": agent,
                    "c": conv,
                    "from": covers_from,
                    "to": covers_to,
                    "content": content,
                    "tokens": tokens,
                    "run": run_id,
                },
            ).mappings().one()
            created = _obs(row)
            session.execute(
                text(
                    "UPDATE memory_observations SET superseded_by = :r"
                    " WHERE tenant_id = :t AND conversation_id = :c AND id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"r": created.id, "t": tenant_id, "c": conv, "ids": ids},
            )
            self._event_or_rollback(session, tenant_id, event, created, superseded=len(ids))
            session.commit()
            return ObservationWrite("created", created, max(covered, covers_to), tuple(ids))

    @staticmethod
    def _event_or_rollback(session, tenant_id: str, event: MemoryEvent | None, obs: Observation, **extra: Any) -> None:
        try:
            _append_event(
                session,
                tenant_id,
                event,
                {
                    "kind": obs.kind,
                    "covers_from_turn": obs.covers_from_turn,
                    "covers_to_turn": obs.covers_to_turn,
                    "approx_tokens": obs.approx_tokens,
                    **extra,
                },
            )
        except Exception:
            session.rollback()
            raise

    def list_observations(
        self,
        tenant_id: str,
        conversation_id: str,
        *,
        agent_id: str | None = None,
        include_superseded: bool = False,
    ) -> list[Observation]:
        """The conversation's observations and reflections (active only by default), ordered by
        covered turns. `agent_id` restricts to that agent's rows ([] for another agent)."""
        _require_tenant(tenant_id)
        sql = f"SELECT {_OBS_COLUMNS} FROM memory_observations WHERE tenant_id = :t AND conversation_id = :c"
        params: dict[str, Any] = {"t": tenant_id, "c": str(conversation_id)}
        if agent_id is not None:
            sql += " AND agent_id = :a"
            params["a"] = str(agent_id)
        if not include_superseded:
            sql += " AND superseded_by IS NULL"
        sql += " ORDER BY covers_from_turn, covers_to_turn, kind DESC, created_at"
        with self.engine.connect() as conn:
            return [_obs(r) for r in conn.execute(text(sql), params).mappings().all()]

    def delete_observation(
        self, tenant_id: str, conversation_id: str, observation_id: str, *, only_latest: bool = False
    ) -> int:
        """Delete one row and every row it superseded (recursively). Returns the number deleted
        (0: unknown id, another tenant's or conversation's; idempotent).

        `only_latest` (the API, RM.9): raise ObservationNotLatest, deleting nothing, unless the row
        is active and has the conversation's highest `covers_to_turn`; checked under the
        conversation lock that insert_observation and reflect take, so it cannot race them."""
        _require_tenant(tenant_id)
        oid = _canonical_uuid(observation_id)
        if oid is None:
            return 0
        conv = str(conversation_id)
        with self.Session() as session:
            locked = session.execute(
                text("SELECT id FROM conversations WHERE tenant_id = :t AND id = :c FOR NO KEY UPDATE"),
                {"t": tenant_id, "c": conv},
            ).first()
            if locked is None:
                session.rollback()
                return 0
            if only_latest:
                target = session.execute(
                    text(
                        "SELECT covers_to_turn, superseded_by FROM memory_observations"
                        " WHERE tenant_id = :t AND conversation_id = :c AND id = :id"
                    ),
                    {"t": tenant_id, "c": conv, "id": oid},
                ).first()
                if target is None:
                    session.rollback()
                    return 0
                if target.superseded_by is not None or target.covers_to_turn < self._covered_to(session, tenant_id, conv):
                    session.rollback()
                    raise ObservationNotLatest(conv, oid)
            deleted = session.execute(
                text(
                    "WITH RECURSIVE doomed AS ("
                    " SELECT id FROM memory_observations WHERE tenant_id = :t AND conversation_id = :c AND id = :id"
                    " UNION SELECT o.id FROM memory_observations o JOIN doomed d ON o.superseded_by = d.id"
                    " WHERE o.tenant_id = :t AND o.conversation_id = :c)"
                    " DELETE FROM memory_observations WHERE tenant_id = :t AND id IN (SELECT id FROM doomed)"
                ),
                {"t": tenant_id, "c": conv, "id": oid},
            ).rowcount
            session.commit()
        return deleted

    def delete_observations(self, tenant_id: str, conversation_id: str, *, agent_id: str | None = None) -> int:
        """Delete every observation and reflection of the conversation (optionally only `agent_id`'s).
        Returns the number deleted (idempotent)."""
        _require_tenant(tenant_id)
        conv = str(conversation_id)
        sql = "DELETE FROM memory_observations WHERE tenant_id = :t AND conversation_id = :c"
        params: dict[str, Any] = {"t": tenant_id, "c": conv}
        if agent_id is not None:
            sql += " AND agent_id = :a"
            params["a"] = str(agent_id)
        with self.Session() as session:
            locked = session.execute(
                text("SELECT id FROM conversations WHERE tenant_id = :t AND id = :c FOR NO KEY UPDATE"),
                {"t": tenant_id, "c": conv},
            ).first()
            if locked is None:
                session.rollback()
                return 0
            deleted = session.execute(text(sql), params).rowcount
            session.commit()
        return deleted

    # ------------------------------------------------------------------ snapshot

    def load_snapshot(
        self,
        tenant_id: str,
        agent_id: str,
        conversation_id: str,
        *,
        before_turn: int | None = None,
        working: WorkingMemoryConfig | None = None,
        observations: bool = False,
        history: HistoryConfig | None = None,
    ) -> MemorySnapshot:
        """Everything a turn's memory context is assembled from, in one REPEATABLE READ, READ ONLY
        transaction.

        - the conversation's resource_id;
        - `working` set: the note of its scope (keyed by the conversation id, or by the resource
          id; none when the conversation has no resource_id);
        - `observations` True: the active observations and reflection; the turns they cover
          (1..covered_to_turn) are left out of `turns`;
        - the COMPLETED turns after that and before `before_turn` (all when None), then
          `history` limits (`MemoryConfig.effective_history()`; None = no limit).

        Raises ConversationNotFound (unknown, another tenant's or another agent's conversation)."""
        _require_tenant(tenant_id)
        agent = _require_agent(agent_id)
        conv = str(conversation_id)
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
            with conn.begin():
                conn.execute(text("SET TRANSACTION READ ONLY"))
                row = conn.execute(
                    text("SELECT agent_id, resource_id FROM conversations WHERE tenant_id = :t AND id = :c"),
                    {"t": tenant_id, "c": conv},
                ).mappings().first()
                if row is None or row["agent_id"] != agent:
                    raise ConversationNotFound(tenant_id, conv)
                resource_id = row["resource_id"]

                wm_scope = wm_key = None
                note = None
                if working is not None:
                    wm_scope = working.scope
                    wm_key = conv if working.scope == "conversation" else resource_id
                    if wm_key is not None:
                        wm_row = conn.execute(
                            text(
                                f"SELECT {_WM_COLUMNS} FROM working_memories"
                                " WHERE tenant_id = :t AND agent_id = :a AND scope = :s AND scope_key = :k"
                            ),
                            {"t": tenant_id, "a": agent, "s": wm_scope, "k": wm_key},
                        ).mappings().first()
                        note = None if wm_row is None else _wm(wm_row)

                active: tuple[Observation, ...] = ()
                covered = 0
                if observations:
                    active = tuple(
                        _obs(r)
                        for r in conn.execute(
                            text(
                                f"SELECT {_OBS_COLUMNS} FROM memory_observations"
                                " WHERE tenant_id = :t AND conversation_id = :c AND agent_id = :a"
                                " AND superseded_by IS NULL ORDER BY covers_from_turn, covers_to_turn"
                            ),
                            {"t": tenant_id, "c": conv, "a": agent},
                        ).mappings().all()
                    )
                    covered = max((o.covers_to_turn for o in active), default=0)

                sql = (
                    "SELECT turn, id, input, output, messages, created_at FROM runs"
                    " WHERE tenant_id = :t AND conversation_id = :c AND status = 'completed' AND turn > :covered"
                )
                params: dict[str, Any] = {"t": tenant_id, "c": conv, "covered": covered}
                if before_turn is not None:
                    sql += " AND turn < :before"
                    params["before"] = int(before_turn)
                turn_rows = conn.execute(text(sql + " ORDER BY turn"), params).mappings().all()

        turns = []
        for r in turn_rows:
            item = {
                "turn": r["turn"],
                "run_id": r["id"],
                "input": r["input"],
                "output": r["output"],
                "messages": r["messages"],
                "created_at": r["created_at"],
            }
            item["approx_tokens"] = turn_approx_tokens(item)
            turns.append(item)
        kept, trimmed = apply_history_limits(turns, history)
        return MemorySnapshot(
            conversation_id=conv,
            agent_id=agent,
            resource_id=resource_id,
            working_memory_scope=wm_scope,
            working_memory_key=wm_key,
            working_memory=note,
            observations=active,
            covered_to_turn=covered,
            turns=tuple(kept),
            trimmed_turns=trimmed,
        )

    # ------------------------------------------------------------------ semantic recall (RM.8)

    def index_turn(
        self,
        tenant_id: str,
        agent_id: str,
        conversation_id: str,
        run_id: str,
        items: Sequence[TurnText],
        *,
        embedding_model: str,
        event: MemoryEvent | None = None,
    ) -> IndexTurnResult:
        """Store the embeddings of one completed turn's texts, once per `(run_id, role)`, in one
        transaction.

        `items`: one or two TurnText with distinct roles, processed and redacted text only (the
        store does not redact), embeddings all of one dimension in (768, 1024, 1536). The turn
        number is the run's, and the resource id the conversation's (never caller input).
        A role already indexed for this run is left as it was and reported in `existing` (a
        re-executed step writes nothing new); `event` (e.g. the `embed:index:{turn}` usage) is
        appended in the same transaction, with default data
        `{model_id, dimensions, turn, inserted, existing}` (counts) unless already appended.

        Forget watermark: when the run's turn is <= the conversation's `forgotten_through_turn`
        (its resource was forgotten after that turn existed), nothing is written, not even `event`,
        and the result has `skipped=True` (a late or recovered step after forget_resource). A turn
        created after the forget has a higher number and indexes normally. Turn numbers, no clocks:
        forget and `create_turn` take the same conversation lock.

        Raises ConversationNotFound (unknown, another tenant's or another agent's conversation, or
        deleted), RunNotFound (the run, or the event's run, is not a turn of this tenant, agent and
        conversation), ValueError for bad items."""
        _require_tenant(tenant_id)
        agent = _require_agent(agent_id)
        conv = str(conversation_id)
        run = str(run_id)
        parse_embedding_model_id(embedding_model)
        items = list(items)
        if not 1 <= len(items) <= len(MESSAGE_ROLES):
            raise ValueError("pass one or two items (roles user and assistant)")
        roles = [i.role for i in items]
        if len(set(roles)) != len(roles) or any(r not in MESSAGE_ROLES for r in roles):
            raise ValueError(f"item roles must be distinct, each one of {', '.join(MESSAGE_ROLES)}")
        if any(not isinstance(i.text, str) or not i.text.strip() for i in items):
            raise ValueError("item text is required")
        dims = {len(i.embedding) for i in items}
        if len(dims) != 1 or not dims <= set(HNSW_DIMENSIONS):
            raise ValueError(f"embeddings must all have one of {HNSW_DIMENSIONS} dimensions")
        (dim,) = dims
        literals = [vector_literal(i.embedding) for i in items]

        with self.Session() as session:
            conv_row = self._lock_conversation(session, tenant_id, agent, conv, "KEY SHARE")
            run_row = session.execute(
                text("SELECT agent_id, conversation_id, turn FROM runs WHERE tenant_id = :t AND id = :r"),
                {"t": tenant_id, "r": run},
            ).first()
            if run_row is None or run_row.agent_id != agent or run_row.conversation_id != conv or run_row.turn is None:
                session.rollback()
                raise RunNotFound(tenant_id, run)
            _check_run(session, tenant_id, (None if event is None else event.run_id,), agent, conv)
            turn = int(run_row.turn)
            resource_id = conv_row["resource_id"]
            forgotten_through = session.execute(
                text(
                    "SELECT forgotten_through_turn FROM memory_forget_watermarks"
                    " WHERE tenant_id = :t AND conversation_id = :c"
                ),
                {"t": tenant_id, "c": conv},
            ).scalar_one_or_none()
            if forgotten_through is not None and turn <= forgotten_through:
                session.rollback()
                return IndexTurnResult(conv, turn, run, resource_id, (), (), skipped=True)
            inserted: list[str] = []
            existing: list[str] = []
            for item, literal in zip(items, literals):
                row = session.execute(
                    text(
                        "INSERT INTO message_embeddings (tenant_id, agent_id, resource_id, conversation_id, turn,"
                        " run_id, role, text, embedding_model, dimensions, embedding)"
                        " VALUES (:t, :a, :res, :c, :turn, :r, :role, :text, :model, :dims, CAST(:e AS public.vector))"
                        " ON CONFLICT ON CONSTRAINT uq_message_embeddings_run_role DO NOTHING RETURNING role"
                    ),
                    {
                        "t": tenant_id,
                        "a": agent,
                        "res": conv_row["resource_id"],
                        "c": conv,
                        "turn": turn,
                        "r": run,
                        "role": item.role,
                        "text": item.text,
                        "model": embedding_model,
                        "dims": dim,
                        "e": literal,
                    },
                ).first()
                (inserted if row is not None else existing).append(item.role)
            try:
                _append_event(
                    session,
                    tenant_id,
                    event,
                    {
                        "model_id": embedding_model,
                        "dimensions": dim,
                        "turn": turn,
                        "inserted": len(inserted),
                        "existing": len(existing),
                    },
                )
            except Exception:
                session.rollback()
                raise
            session.commit()
        return IndexTurnResult(conv, turn, run, conv_row["resource_id"], tuple(inserted), tuple(existing))

    @staticmethod
    def _recall_sql(dims: int, scope_clause: str, exclude: bool, with_min_score: bool) -> str:
        # `dims` is one of HNSW_DIMENSIONS (checked), inlined so that the partial index predicate
        # and the cast match the index definition exactly.
        distance = f"(m.embedding::public.vector({dims})) OPERATOR(public.<=>) CAST(:q AS public.vector({dims}))"
        excl = " AND NOT (m.conversation_id = :c AND m.turn = ANY(CAST(:ex AS integer[])))" if exclude else ""
        cut = " WHERE hits.distance <= :max_distance" if with_min_score else ""
        return (
            "SELECT * FROM ("
            f" SELECT m.id, m.conversation_id, m.turn, m.run_id, m.role, m.text, m.created_at, {distance} AS distance"
            " FROM message_embeddings m"
            f" WHERE m.tenant_id = :t AND m.agent_id = :a AND {scope_clause}"
            f" AND m.embedding_model = :model AND m.dimensions = {dims}{excl}"
            f" ORDER BY {distance} LIMIT :k"
            f") hits{cut} ORDER BY hits.distance, hits.id"
        )

    def _prepare_recall(
        self,
        session,
        tenant_id: str,
        agent_id: str,
        conversation_id: str,
        query_embedding: Sequence[float],
        embedding_model: str,
        scope: str,
        top_k: int,
        min_score: float | None,
        exclude_turns: Iterable[int],
    ) -> tuple[str, dict[str, Any]]:
        _require_tenant(tenant_id)
        agent = _require_agent(agent_id)
        conv = str(conversation_id)
        parse_embedding_model_id(embedding_model)
        if scope not in RECALL_SCOPES:
            raise ValueError(f"scope must be one of {', '.join(RECALL_SCOPES)}")
        dims = len(query_embedding)
        if dims not in HNSW_DIMENSIONS:
            raise ValueError(f"query embedding has {dims} dimensions; expected one of {HNSW_DIMENSIONS}")
        if not 1 <= int(top_k) <= MAX_RECALL_TOP_K:
            raise ValueError(f"top_k must be between 1 and {MAX_RECALL_TOP_K}")
        if min_score is not None and not 0.0 <= float(min_score) <= 1.0:
            raise ValueError("min_score must be between 0 and 1")
        excluded = sorted({int(t) for t in exclude_turns})
        row = session.execute(
            text("SELECT agent_id, resource_id FROM conversations WHERE tenant_id = :t AND id = :c"),
            {"t": tenant_id, "c": conv},
        ).mappings().first()
        if row is None or row["agent_id"] != agent:
            session.rollback()
            raise ConversationNotFound(tenant_id, conv)
        params: dict[str, Any] = {
            "t": tenant_id,
            "a": agent,
            "c": conv,
            "model": embedding_model,
            "q": vector_literal(query_embedding),
            "k": int(top_k),
        }
        if scope == "resource" and row["resource_id"] is not None:
            scope_clause = "m.resource_id = :res"
            params["res"] = row["resource_id"]
        else:  # conversation scope, or a conversation without a resource id
            scope_clause = "m.conversation_id = :c"
        if excluded:
            params["ex"] = excluded
        if min_score is not None:
            params["max_distance"] = 1.0 - float(min_score)
        ef_search = min(HNSW_MAX_EF_SEARCH, max(MIN_EF_SEARCH, 4 * int(top_k)))
        # SET LOCAL equivalents (bindable): reset when this transaction ends.
        session.execute(text("SELECT set_config('hnsw.ef_search', :v, true)"), {"v": str(ef_search)})
        session.execute(text("SELECT set_config('hnsw.iterative_scan', 'relaxed_order', true)"))
        return self._recall_sql(dims, scope_clause, bool(excluded), min_score is not None), params

    def search_recall(
        self,
        tenant_id: str,
        agent_id: str,
        conversation_id: str,
        query_embedding: Sequence[float],
        *,
        embedding_model: str,
        scope: str = "resource",
        top_k: int = 4,
        min_score: float | None = None,
        exclude_turns: Iterable[int] = (),
    ) -> list[RecallHit]:
        """The `top_k` indexed messages nearest to `query_embedding` by cosine distance, for the turn
        running in `conversation_id`, in one SQL query.

        Rows must have this tenant and agent, `embedding_model` (rows under another model are
        ignored) and the query's dimensions, and lie in the scope: `resource` = the
        conversation's resource_id (read from the conversation row, never caller input; a
        conversation without one falls back to its own rows), `conversation` = this conversation.
        `exclude_turns` are turns of THIS conversation already in the assembled context: they never
        return. Ranking: the partial HNSW index of the query's dimension, `ORDER BY distance LIMIT
        top_k`, then the `min_score` cut (score = 1 - distance), re-sorted by exact distance (ties
        by id). Hits are messages (a turn's user and assistant rows count separately).
        `hnsw.ef_search = max(40, 4 × top_k)` and `hnsw.iterative_scan = relaxed_order` hold for
        this transaction only. The iterative scan stops after `hnsw.max_scan_tuples` index tuples
        (pgvector default 20,000): when the filters are very selective (a small resource in a large
        index of the same dimension, or most turns excluded), recall can come back with fewer than
        `top_k` hits even though more matching rows exist.

        Raises ConversationNotFound (unknown, another tenant's or another agent's conversation),
        ValueError for unsupported dimensions, scope, top_k (1..50) or min_score."""
        with self.Session() as session:
            sql, params = self._prepare_recall(
                session, tenant_id, agent_id, conversation_id, query_embedding, embedding_model, scope, top_k,
                min_score, exclude_turns,
            )
            rows = session.execute(text(sql), params).mappings().all()
            session.commit()
        return [
            RecallHit(
                id=str(r["id"]),
                conversation_id=r["conversation_id"],
                turn=r["turn"],
                run_id=r["run_id"],
                role=r["role"],
                text=r["text"],
                created_at=r["created_at"],
                distance=float(r["distance"]),
                score=1.0 - float(r["distance"]),
            )
            for r in rows
        ]

    def explain_search_recall(
        self,
        tenant_id: str,
        agent_id: str,
        conversation_id: str,
        query_embedding: Sequence[float],
        *,
        embedding_model: str,
        scope: str = "resource",
        top_k: int = 4,
        min_score: float | None = None,
        exclude_turns: Iterable[int] = (),
        disable_seqscan: bool = True,
    ) -> str:
        """EXPLAIN of exactly the SQL `search_recall` runs, with the same settings (diagnostics and
        tests: a small table makes the planner prefer a sequential scan, hence `disable_seqscan`)."""
        with self.Session() as session:
            sql, params = self._prepare_recall(
                session, tenant_id, agent_id, conversation_id, query_embedding, embedding_model, scope, top_k,
                min_score, exclude_turns,
            )
            if disable_seqscan:
                session.execute(text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(r[0] for r in session.execute(text("EXPLAIN " + sql), params))
            session.rollback()
        return plan

    def recall_neighbours(
        self,
        tenant_id: str,
        agent_id: str,
        conversation_id: str,
        turn: int,
        message_range: int,
        *,
        current_conversation_id: str,
        exclude_turns: Iterable[int] = (),
    ) -> list[RecallMessage]:
        """The indexed messages of turns `turn - message_range .. turn + message_range` of the hit's
        `conversation_id` (the hit's turn included), for the turn running in
        `current_conversation_id`. Oldest turn first, user before assistant, under any embedding
        model (the text is the same). Only stored (processed, redacted) text, never raw input.

        Scoping, all in SQL: this tenant's and agent's rows, and the current conversation's resource
        (`resource_id` read from its row; a current conversation without one only sees its own
        rows). `exclude_turns` are turns of the CURRENT conversation already in context: they are
        dropped only from rows of that conversation. [] when the current conversation is not this
        tenant's and agent's (unknown or deleted)."""
        _require_tenant(tenant_id)
        agent = _require_agent(agent_id)
        if not 0 <= int(message_range) <= MAX_MESSAGE_RANGE:
            raise ValueError(f"message_range must be between 0 and {MAX_MESSAGE_RANGE}")
        current = str(current_conversation_id)
        params: dict[str, Any] = {
            "t": tenant_id,
            "a": agent,
            "c": str(conversation_id),
            "cur": current,
            "lo": int(turn) - int(message_range),
            "hi": int(turn) + int(message_range),
        }
        excluded = sorted({int(t) for t in exclude_turns})
        with self.engine.connect() as conn:
            with conn.begin():
                row = conn.execute(
                    text("SELECT agent_id, resource_id FROM conversations WHERE tenant_id = :t AND id = :cur"),
                    params,
                ).mappings().first()
                if row is None or row["agent_id"] != agent:
                    return []
                sql = (
                    "SELECT id, conversation_id, turn, run_id, role, text, created_at FROM message_embeddings"
                    " WHERE tenant_id = :t AND conversation_id = :c AND agent_id = :a AND turn BETWEEN :lo AND :hi"
                )
                if row["resource_id"] is not None:
                    sql += " AND resource_id = :res"
                    params["res"] = row["resource_id"]
                else:
                    sql += " AND conversation_id = :cur"
                if excluded:
                    sql += " AND NOT (conversation_id = :cur AND turn = ANY(CAST(:ex AS integer[])))"
                    params["ex"] = excluded
                sql += " ORDER BY turn, CASE role WHEN 'user' THEN 0 ELSE 1 END, id"
                rows = conn.execute(text(sql), params).mappings().all()
        return [
            RecallMessage(
                id=str(r["id"]),
                conversation_id=r["conversation_id"],
                turn=r["turn"],
                run_id=r["run_id"],
                role=r["role"],
                text=r["text"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def message_embedding_tenants_before(self, cutoff: datetime, *, limit: int = 1000) -> list[str]:
        """Tenants that have message embeddings created before `cutoff` (timezone-aware), in id
        order, at most `limit` (the retention sweep's tenant batches; an operator-only call)."""
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        with self.engine.connect() as conn:
            return list(
                conn.execute(
                    text(
                        "SELECT DISTINCT tenant_id FROM message_embeddings WHERE created_at < :cut"
                        " ORDER BY tenant_id LIMIT :n"
                    ),
                    {"cut": cutoff, "n": max(1, min(int(limit), MAX_RETENTION_BATCH))},
                ).scalars()
            )

    def delete_message_embeddings_before(self, tenant_id: str, cutoff: datetime, *, limit: int = 1000) -> int:
        """Retention: delete up to `limit` of the tenant's message embeddings created before
        `cutoff` (timezone-aware), oldest first, in one transaction. Returns the number deleted;
        call again until it returns less than `limit`. Idempotent (a re-run deletes what is left).
        Embeddings are immutable, so their age is `created_at`. Every agent of the tenant.
        Rows locked by a concurrent delete_conversation or forget_resource are skipped
        (`FOR UPDATE SKIP LOCKED`): that delete removes them. Forget watermarks are kept."""
        _require_tenant(tenant_id)
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        with self.Session() as session:
            ids = session.execute(
                text(
                    "SELECT id FROM message_embeddings WHERE tenant_id = :t AND created_at < :cut"
                    " ORDER BY created_at, id LIMIT :n FOR UPDATE SKIP LOCKED"
                ),
                {"t": tenant_id, "cut": cutoff, "n": max(1, min(int(limit), MAX_RETENTION_BATCH))},
            ).scalars().all()
            deleted = 0
            if ids:
                deleted = session.execute(
                    text("DELETE FROM message_embeddings WHERE tenant_id = :t AND id = ANY(CAST(:ids AS uuid[]))"),
                    {"t": tenant_id, "ids": [str(i) for i in ids]},
                ).rowcount
            session.commit()
        return deleted

    # ------------------------------------------------------------------ deletions

    def delete_conversation(self, tenant_id: str, conversation_id: str) -> bool:
        """Delete a conversation and its derived memory in one transaction.

        Deletes its conversation-scoped working memory, observations and message embeddings (and,
        by cascade, any
        other row that references the conversation), keeps its runs as the audit and cost record
        with `input`, `output`, `messages`, `request_context` and `error` set to NULL and detached
        from the conversation (`conversation_id` and `turn` NULL), scrubs every event of those runs
        (`scrub_event_data`) and deletes all their side_effects rows (tool results and ledger), then
        deletes the conversation row. Usage rows and run totals are untouched. Resource-scoped
        working memory is untouched. Returns False when the tenant has no such conversation
        (idempotent). Raises TurnInProgress when its latest turn is still queued or running."""
        _require_tenant(tenant_id)
        conv = str(conversation_id)
        params = {"t": tenant_id, "c": conv}
        with self.Session() as session:
            locked = session.execute(
                text("SELECT id FROM conversations WHERE tenant_id = :t AND id = :c FOR UPDATE"), params
            ).first()
            if locked is None:
                session.rollback()
                return False
            latest = session.execute(
                text(
                    "SELECT id, status FROM runs WHERE tenant_id = :t AND conversation_id = :c"
                    " ORDER BY turn DESC LIMIT 1"
                ),
                params,
            ).first()
            if latest is not None and latest.status not in TERMINAL_RUN_STATUSES:
                session.rollback()
                raise TurnInProgress(conv, latest.id, latest.status)
            session.execute(
                text("DELETE FROM working_memories WHERE tenant_id = :t AND conversation_id = :c"), params
            )
            session.execute(
                text("DELETE FROM memory_observations WHERE tenant_id = :t AND conversation_id = :c"), params
            )
            # The FK cascade would remove these with the conversation row; deleting them here keeps
            # the lock order (embedding rows before the run rows) explicit.
            session.execute(
                text("DELETE FROM message_embeddings WHERE tenant_id = :t AND conversation_id = :c"), params
            )
            run_ids = session.execute(
                text(
                    "UPDATE runs SET input = NULL, output = NULL, messages = NULL, request_context = NULL, error = NULL,"
                    " conversation_id = NULL, turn = NULL WHERE tenant_id = :t AND conversation_id = :c RETURNING id"
                ),
                params,
            ).scalars().all()
            _scrub_runs(session, tenant_id, run_ids, all_side_effects=True)
            session.execute(text("DELETE FROM conversations WHERE tenant_id = :t AND id = :c"), params)
            session.commit()
        return True

    def forget_resource(
        self,
        tenant_id: str,
        resource_id: str,
        *,
        agent_id: str | None = None,
        include_conversations: bool = False,
    ) -> ForgetResult:
        """Delete a resource's memory (one agent's, or every agent's when `agent_id` is None).

        First, in one transaction: lock the resource's conversations (of that agent, or all)
        `FOR UPDATE` in id order, so no turn can start in them; if any has a queued or running
        latest turn, raise ResourceBusy and change nothing (its write could recreate the note, and
        its ledger would be gone). Otherwise delete the resource-scoped notes and the message
        embeddings indexed under the resource (that agent's, or every agent's), scrub the
        `memory_updated` events of those conversations' runs (they carry key names) and delete
        their working memory ledger rows. The turns' own text stays (see the module docstring).
        Then, with `include_conversations`, delete each of those conversations through
        delete_conversation, one transaction each; one whose turn started in between is skipped and
        reported in `busy_conversations`. Idempotent.

        In that transaction it also sets each locked conversation's forget watermark to its highest
        existing turn (upsert to the max), so a post-turn index step of a turn that existed before
        the forget, running late or recovered, skips (see index_turn)."""
        _require_tenant(tenant_id)
        validate_resource_id(resource_id)
        if not resource_id:
            raise ValueError("resource_id is required")
        params: dict[str, Any] = {"t": tenant_id, "k": resource_id}
        agent_clause = ""
        if agent_id is not None:
            params["a"] = _require_agent(agent_id)
            agent_clause = " AND agent_id = :a"
        with self.Session() as session:
            ids = session.execute(
                text(
                    "SELECT id FROM conversations WHERE tenant_id = :t AND resource_id = :k"
                    + agent_clause
                    + " ORDER BY id FOR UPDATE"
                ),
                params,
            ).scalars().all()
            latest = session.execute(
                text(
                    "SELECT DISTINCT ON (conversation_id) conversation_id, id, status FROM runs"
                    " WHERE tenant_id = :t AND conversation_id = ANY(:ids) ORDER BY conversation_id, turn DESC"
                ),
                {"t": tenant_id, "ids": list(ids)},
            ).all()
            live = [r for r in latest if r.status not in TERMINAL_RUN_STATUSES]
            if live:
                session.rollback()
                first = live[0]
                raise ResourceBusy(
                    resource_id, first.conversation_id, first.id, first.status, [r.conversation_id for r in live]
                )
            notes = session.execute(
                text(
                    "DELETE FROM working_memories WHERE tenant_id = :t AND scope = 'resource' AND scope_key = :k"
                    + agent_clause
                ),
                params,
            ).rowcount
            embeddings = session.execute(
                text("DELETE FROM message_embeddings WHERE tenant_id = :t AND resource_id = :k" + agent_clause),
                params,
            ).rowcount
            # The watermarks: every turn that exists now is forgotten; later turns (higher numbers,
            # created under the same conversation lock after this commits) index normally.
            if ids:
                session.execute(
                    text(
                        "INSERT INTO memory_forget_watermarks (tenant_id, conversation_id, forgotten_through_turn)"
                        " SELECT CAST(:t AS varchar), c.id, coalesce((SELECT max(r.turn) FROM runs r"
                        " WHERE r.tenant_id = CAST(:t AS varchar) AND r.conversation_id = c.id), 0)"
                        " FROM unnest(CAST(:ids AS text[])) AS c(id)"
                        " ON CONFLICT ON CONSTRAINT memory_forget_watermarks_pkey DO UPDATE SET forgotten_through_turn"
                        " = greatest(memory_forget_watermarks.forgotten_through_turn,"
                        " excluded.forgotten_through_turn), updated_at = now()"
                    ),
                    {"t": tenant_id, "ids": list(ids)},
                )
            writer_runs = session.execute(
                text("SELECT id FROM runs WHERE tenant_id = :t AND conversation_id = ANY(:ids) ORDER BY id"),
                {"t": tenant_id, "ids": list(ids)},
            ).scalars().all()
            _scrub_runs(session, tenant_id, writer_runs, event_type="memory_updated")
            session.commit()
        deleted: list[str] = []
        busy: list[str] = []
        if include_conversations:
            for conv in ids:
                try:
                    if self.delete_conversation(tenant_id, conv):
                        deleted.append(conv)
                except TurnInProgress:
                    busy.append(conv)
        return ForgetResult(notes, tuple(deleted), tuple(busy), embeddings)


__all__ = [
    "ForgetResult",
    "IndexTurnResult",
    "MemoryEvent",
    "MemoryForgetWatermarkModel",
    "MemoryLedgerConflict",
    "MessageEmbeddingModel",
    "MemoryObservationModel",
    "MemorySnapshot",
    "MemoryStore",
    "Observation",
    "ObservationNotLatest",
    "ResourceBusy",
    "ObservationWrite",
    "RecallHit",
    "RecallMessage",
    "TurnText",
    "WorkingMemory",
    "WorkingMemoryModel",
    "WorkingMemoryVersionConflict",
    "WorkingMemoryWrite",
    "SCRUB_KEEP_STRING_KEYS",
    "apply_history_limits",
    "ledger_row_key",
    "scrub_event_data",
    "turn_approx_tokens",
    "working_memory_ledger_key",
]
