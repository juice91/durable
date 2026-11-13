"""Agent memory: conversation resource ids, working memory, observational memory.

Revision ID: 0012_memory
Revises: 0011_knowledge_bases
Create Date: 2026-11-13 15:00:00

- `conversations.resource_id`: the conversation's owner (a user or entity), set once at creation and
  immutable (`^[A-Za-z0-9_.:@-]{1,128}$`). Existing conversations get NULL: resource-scoped memory
  is off for them, conversation scope still works. Partial index `(tenant_id, resource_id)` for
  forget-resource.
- `working_memories`: one note per `(tenant_id, agent_id, scope, scope_key)`; `scope_key` is the
  conversation id (scope `conversation`) or the resource id (scope `resource`). `format` is
  `markdown` (content a JSON string) or `json` (content an object). `version` counts writes.
  A conversation-scoped row also carries `conversation_id` (= `scope_key`), with a composite FK to
  `conversations` ON DELETE CASCADE: deleting a conversation deletes its note, and a write racing
  that delete fails on the FK instead of leaving an orphan. Resource rows have it NULL.
- `memory_observations`: observations and reflections of one conversation, covering turns
  `covers_from_turn..covers_to_turn`. Idempotent by `(tenant_id, conversation_id, kind,
  covers_to_turn)`. A reflection replaces earlier rows by setting their `superseded_by` (composite
  self-FK). Cascades from `conversations`; `run_id` references the run that wrote it.
- `ix_side_effects_tenant_key_pattern` on `side_effects (tenant_id, key text_pattern_ops)`: prefix
  deletes of a run's rows when a conversation is deleted or a resource forgotten.

Every check text is inline (same texts as eap_run_store.store and eap_run_store.memory).

Downgrade drops both tables and the column: all working memory, observations and conversation
resource ids are lost.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0012_memory"
down_revision: Union[str, Sequence[str], None] = "0011_knowledge_bases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store (a migration never imports app code).
_RESOURCE_ID_CHECK = "resource_id IS NULL OR resource_id ~ '^[A-Za-z0-9_.:@-]{1,128}$'"
_WM_SCOPE_CHECK = (
    "(scope = 'conversation' AND conversation_id IS NOT NULL AND conversation_id = scope_key)"
    " OR (scope = 'resource' AND conversation_id IS NULL)"
)
_WM_FORMAT_CHECK = (
    "(format = 'markdown' AND jsonb_typeof(content) = 'string')"
    " OR (format = 'json' AND jsonb_typeof(content) = 'object')"
)
_WM_VERSION_CHECK = "version >= 1"
_OBS_KIND_CHECK = "kind IN ('observation', 'reflection')"
_OBS_TURNS_CHECK = "covers_from_turn >= 1 AND covers_to_turn >= covers_from_turn"
_OBS_TOKENS_CHECK = "approx_tokens >= 0"
_OBS_SUPERSEDED_CHECK = "superseded_by IS NULL OR superseded_by <> id"


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=False)
    now = sa.text("now()")

    op.add_column("conversations", sa.Column("resource_id", sa.String(), nullable=True))
    op.create_check_constraint("ck_conversations_resource_id", "conversations", _RESOURCE_ID_CHECK)
    op.create_index(
        "ix_conversations_tenant_resource",
        "conversations",
        ["tenant_id", "resource_id"],
        unique=False,
        postgresql_where=sa.text("resource_id IS NOT NULL"),
    )

    op.create_table(
        "working_memories",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=True),
        sa.Column("format", sa.String(), nullable=False),
        sa.Column("content", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("updated_by_run_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "agent_id", "scope", "scope_key", name="working_memories_pkey"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_working_memories_tenant_conversation",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_WM_SCOPE_CHECK, name="ck_working_memories_scope"),
        sa.CheckConstraint(_WM_FORMAT_CHECK, name="ck_working_memories_format"),
        sa.CheckConstraint(_WM_VERSION_CHECK, name="ck_working_memories_version"),
    )
    # Tenant-wide forget-resource (every agent).
    op.create_index(
        "ix_working_memories_tenant_scope_key", "working_memories", ["tenant_id", "scope", "scope_key"], unique=False
    )
    # The conversation FK's cascade lookup.
    op.create_index(
        "ix_working_memories_tenant_conversation",
        "working_memories",
        ["tenant_id", "conversation_id"],
        unique=False,
        postgresql_where=sa.text("conversation_id IS NOT NULL"),
    )
    # Retention of conversation-scoped notes (by age).
    op.create_index(
        "ix_working_memories_conversation_updated",
        "working_memories",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("scope = 'conversation'"),
    )

    op.create_table(
        "memory_observations",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("covers_from_turn", sa.Integer(), nullable=False),
        sa.Column("covers_to_turn", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("approx_tokens", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=True),
        sa.Column("superseded_by", uuid, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=now, nullable=False),
        sa.PrimaryKeyConstraint("id", name="memory_observations_pkey"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_memory_observations_tenant_id_id"),
        sa.UniqueConstraint(
            "tenant_id", "conversation_id", "kind", "covers_to_turn", name="uq_memory_observations_slot"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_memory_observations_tenant_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["runs.tenant_id", "runs.id"], name="fk_memory_observations_tenant_run"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "superseded_by"],
            ["memory_observations.tenant_id", "memory_observations.id"],
            name="fk_memory_observations_tenant_superseded_by",
        ),
        sa.CheckConstraint(_OBS_KIND_CHECK, name="ck_memory_observations_kind"),
        sa.CheckConstraint(_OBS_TURNS_CHECK, name="ck_memory_observations_turns"),
        sa.CheckConstraint(_OBS_TOKENS_CHECK, name="ck_memory_observations_tokens"),
        sa.CheckConstraint(_OBS_SUPERSEDED_CHECK, name="ck_memory_observations_superseded"),
    )
    # The self-FK's lookup when a row is deleted.
    op.create_index(
        "ix_memory_observations_tenant_superseded_by",
        "memory_observations",
        ["tenant_id", "superseded_by"],
        unique=False,
        postgresql_where=sa.text("superseded_by IS NOT NULL"),
    )
    # Retention (by age).
    op.create_index("ix_memory_observations_created", "memory_observations", ["created_at"], unique=False)

    # Prefix deletes of one run's side_effects rows (`{run_id}:...`, `wm:{run_id}:...`) with LIKE,
    # when a conversation is deleted or a resource forgotten.
    op.create_index(
        "ix_side_effects_tenant_key_pattern",
        "side_effects",
        ["tenant_id", "key"],
        unique=False,
        postgresql_ops={"key": "text_pattern_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_side_effects_tenant_key_pattern", table_name="side_effects")
    op.drop_index("ix_memory_observations_created", table_name="memory_observations")
    op.drop_index("ix_memory_observations_tenant_superseded_by", table_name="memory_observations")
    op.drop_table("memory_observations")
    op.drop_index("ix_working_memories_conversation_updated", table_name="working_memories")
    op.drop_index("ix_working_memories_tenant_conversation", table_name="working_memories")
    op.drop_index("ix_working_memories_tenant_scope_key", table_name="working_memories")
    op.drop_table("working_memories")
    op.drop_index("ix_conversations_tenant_resource", table_name="conversations")
    op.drop_constraint("ck_conversations_resource_id", "conversations", type_="check")
    op.drop_column("conversations", "resource_id")
