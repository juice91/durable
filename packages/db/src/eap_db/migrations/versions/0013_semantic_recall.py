"""Semantic recall: `message_embeddings` with partial HNSW indexes per dimension; forget watermarks.

Revision ID: 0013_semantic_recall
Revises: 0012_memory
Create Date: 2026-11-13 18:00:00

- `message_embeddings`: one row per indexed turn text, `(tenant_id, run_id, role)` unique (the
  idempotency key of `eap.memory_index`; role `user` = the turn's processed input, `assistant` =
  its final answer). `agent_id`, `resource_id` (the conversation's, NULL when it has none),
  `conversation_id` and `turn` are copied from the conversation and the run. `text` is the
  processed and redacted text, never raw input.
- `embedding` is an untyped `public.vector` with `dimensions` (both NOT NULL);
  `ck_message_embeddings_embedding` requires `dimensions IN (768, 1024, 1536)` and
  `vector_dims(embedding) = dimensions`, with explicit IS NOT NULL terms so a NULL can never make
  the check pass. One partial expression HNSW index per dimension:
  `((embedding::public.vector(N)) public.vector_cosine_ops) WITH (m = 16, ef_construction = 64)
  WHERE dimensions = N`; recall uses the same cast and predicate.
- Composite tenant FKs: to `conversations` ON DELETE CASCADE (deleting a conversation deletes its
  embeddings, and an insert racing that delete fails on the FK), and to `runs` ON DELETE CASCADE
  (derived data never outlives its run).
- B-tree indexes `(tenant_id, agent_id, resource_id)` (resource-scoped recall and forget),
  `(tenant_id, conversation_id, turn)` (conversation scope, neighbours, the cascade) and
  `(tenant_id, created_at)` (retention by age, in tenant batches).
- `memory_forget_watermarks`: one row per conversation whose resource was forgotten,
  `forgotten_through_turn` = the conversation's highest turn at the forget (upserted to the max).
  A post-turn index step of a turn at or below it skips, so a late or recovered step cannot undo a
  forget. Turn numbers, not clocks: forget and `create_turn` take the same conversation lock, so
  the comparison is exact. No agent_id: a conversation belongs to one agent, and a per-agent forget
  only marks that agent's conversations. Composite FK to `conversations` ON DELETE CASCADE (nothing
  of the resource lingers after the conversation is deleted). Retention keeps them.

The vector extension was created by 0011 (`CREATE EXTENSION IF NOT EXISTS vector SCHEMA public` is
repeated here as a no-op guard). Every check text is inline (same texts as eap_run_store.memory).

Downgrade drops both tables (all recall embeddings and forget watermarks are lost) and keeps the
extension.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0013_semantic_recall"
down_revision: Union[str, Sequence[str], None] = "0012_memory"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store.memory (a migration never imports app code).
_ME_ROLE_CHECK = "role IN ('user', 'assistant')"
_ME_TURN_CHECK = "turn >= 1"
_ME_EMBEDDING_CHECK = (
    "embedding IS NOT NULL AND dimensions IS NOT NULL AND dimensions IN (768, 1024, 1536)"
    " AND public.vector_dims(embedding) = dimensions"
)
_FW_TURN_CHECK = "forgotten_through_turn >= 0"
HNSW_DIMENSIONS = (768, 1024, 1536)


def upgrade() -> None:
    # Present since 0011 (created by the admin role); a no-op that keeps this revision self-describing.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")
    uuid = postgresql.UUID(as_uuid=False)

    op.create_table(
        "message_embeddings",
        sa.Column("id", uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("agent_id", sa.String(), nullable=False),
        sa.Column("resource_id", sa.String(), nullable=True),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding_model", sa.String(), nullable=False),
        sa.Column("dimensions", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="message_embeddings_pkey"),
        sa.UniqueConstraint("tenant_id", "run_id", "role", name="uq_message_embeddings_run_role"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_message_embeddings_tenant_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_message_embeddings_tenant_run",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_ME_ROLE_CHECK, name="ck_message_embeddings_role"),
        sa.CheckConstraint(_ME_TURN_CHECK, name="ck_message_embeddings_turn"),
    )
    # pgvector's SQLAlchemy type renders an unqualified VECTOR, which does not resolve with the
    # migration's single-schema search path: the column and its check are added in SQL. The table
    # is new and empty, so NOT NULL needs no default.
    op.execute("ALTER TABLE message_embeddings ADD COLUMN embedding public.vector NOT NULL")
    op.create_check_constraint("ck_message_embeddings_embedding", "message_embeddings", _ME_EMBEDDING_CHECK)
    op.create_index(
        "ix_message_embeddings_tenant_agent_resource",
        "message_embeddings",
        ["tenant_id", "agent_id", "resource_id"],
        unique=False,
    )
    op.create_index(
        "ix_message_embeddings_tenant_conversation_turn",
        "message_embeddings",
        ["tenant_id", "conversation_id", "turn"],
        unique=False,
    )
    op.create_index(
        "ix_message_embeddings_tenant_created", "message_embeddings", ["tenant_id", "created_at"], unique=False
    )
    for dims in HNSW_DIMENSIONS:
        op.execute(
            f"CREATE INDEX ix_message_embeddings_embedding_{dims} ON message_embeddings USING hnsw "
            f"((embedding::public.vector({dims})) public.vector_cosine_ops) "
            f"WITH (m = 16, ef_construction = 64) WHERE dimensions = {dims}"
        )

    op.create_table(
        "memory_forget_watermarks",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.String(), nullable=False),
        sa.Column("forgotten_through_turn", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "conversation_id", name="memory_forget_watermarks_pkey"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_memory_forget_watermarks_tenant_conversation",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(_FW_TURN_CHECK, name="ck_memory_forget_watermarks_turn"),
    )


def downgrade() -> None:
    op.drop_table("memory_forget_watermarks")  # forget watermarks are lost
    op.drop_table("message_embeddings")  # its indexes, HNSW included, go with it
    # The vector extension stays: it is per database and shared with every other schema.
