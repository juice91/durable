"""Chat attachments and message feedback: `chat_attachments`, `message_feedback`.

Revision ID: 0021_chat_attachments
Revises: 0020_scorers
Create Date: 2026-11-16 14:00:00

- `chat_attachments`: a file a user attached in the chat composer (image, PDF, text), stored as
  bytes (`data`) in the tenant's own rows. Uploaded first (`run_id` and `message_id` NULL), tied to
  a user message (`message_id`, the AG-UI message id) when the message that references it is sent.
  The worker finds a turn's attachments through the turn's `ag_ui_runs.input_message_ids`, so a
  regenerate (same message id, new run) sees them too. `(tenant_id, message_id)` is indexed.
- `message_feedback`: one thumbs rating (+ optional comment) per (tenant, message, user) on an
  assistant message, tied to the run (composite tenant FK, ON DELETE CASCADE).

Lock footprint: two new tables and their indexes; `runs` is only referenced by its existing
`(tenant_id, id)` unique key (SHARE ROW EXCLUSIVE on `runs` while the empty-table FK is created).

Downgrade: every attachment and feedback row is lost; runs keep their text.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0021_chat_attachments"
down_revision: Union[str, Sequence[str], None] = "0020_scorers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Same texts as eap_run_store.chat_attachments.
_SIZE_CHECK = "size >= 0"
_RATING_CHECK = "rating IN ('up', 'down')"
_COMMENT_CHECK = "comment IS NULL OR char_length(comment) <= 2000"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "chat_attachments",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("media_type", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="chat_attachments_pkey"),
        sa.CheckConstraint(_SIZE_CHECK, name="ck_chat_attachments_size"),
    )
    op.create_index("ix_chat_attachments_tenant_message", "chat_attachments", ["tenant_id", "message_id"], unique=False)
    op.create_table(
        "message_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("rating", sa.String(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="message_feedback_pkey"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["runs.tenant_id", "runs.id"],
            name="fk_message_feedback_tenant_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "message_id", "created_by", name="uq_message_feedback_message_user"),
        sa.CheckConstraint(_RATING_CHECK, name="ck_message_feedback_rating"),
        sa.CheckConstraint(_COMMENT_CHECK, name="ck_message_feedback_comment"),
    )
    op.create_index("ix_message_feedback_tenant_run", "message_feedback", ["tenant_id", "run_id"], unique=False)


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_message_feedback_tenant_run", table_name="message_feedback")
    op.drop_table("message_feedback")
    op.drop_index("ix_chat_attachments_tenant_message", table_name="chat_attachments")
    op.drop_table("chat_attachments")
