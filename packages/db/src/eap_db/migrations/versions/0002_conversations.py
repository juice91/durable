"""Conversations: a tenant-owned thread whose turns are runs.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-12 15:00:00

- `conversations` (id, tenant_id, agent_id, title, created_at, updated_at), with UNIQUE (tenant_id, id)
  so runs can reference it per tenant, and an index for "the tenant's conversations, newest first".
- `runs.conversation_id` + `runs.turn`: both NULL for a standalone run (every existing row), both set
  for a turn. The composite foreign key (tenant_id, conversation_id) -> conversations(tenant_id, id)
  means a run can only be a turn of its own tenant's conversation; UNIQUE (conversation_id, turn)
  numbers turns once.
- `runs.messages`: the run's own new Pydantic AI messages, written once when it completes; the next
  turns' history is built from these.

Only additions with nullable columns: no existing row is rewritten or removed.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('conversations',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('agent_id', sa.String(), nullable=False),
    sa.Column('title', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name='conversations_pkey'),
    sa.UniqueConstraint('tenant_id', 'id', name='uq_conversations_tenant_id_id')
    )
    op.create_index('ix_conversations_tenant_updated', 'conversations', ['tenant_id', 'updated_at'], unique=False)
    op.add_column('runs', sa.Column('conversation_id', sa.String(), nullable=True))
    op.add_column('runs', sa.Column('turn', sa.Integer(), nullable=True))
    op.add_column('runs', sa.Column('messages', postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.create_foreign_key(
        'fk_runs_tenant_conversation', 'runs', 'conversations',
        ['tenant_id', 'conversation_id'], ['tenant_id', 'id'],
    )
    op.create_unique_constraint('uq_runs_conversation_turn', 'runs', ['conversation_id', 'turn'])
    op.create_check_constraint('ck_runs_conversation_turn', 'runs', '(conversation_id IS NULL) = (turn IS NULL)')


def downgrade() -> None:
    op.drop_constraint('ck_runs_conversation_turn', 'runs', type_='check')
    op.drop_constraint('uq_runs_conversation_turn', 'runs', type_='unique')
    op.drop_constraint('fk_runs_tenant_conversation', 'runs', type_='foreignkey')
    op.drop_column('runs', 'messages')
    op.drop_column('runs', 'turn')
    op.drop_column('runs', 'conversation_id')
    op.drop_index('ix_conversations_tenant_updated', table_name='conversations')
    op.drop_table('conversations')
