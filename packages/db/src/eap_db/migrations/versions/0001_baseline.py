"""Baseline: the eap_app schema as milestone 1 left it.

Revision ID: 0001
Revises:
Create Date: 2026-11-12 11:00:42.912019

Every table the platform had created at runtime with SQLAlchemy create_all (eap_api.models,
eap_run_store, eap_worker.tickets), including the tenant-ownership changes that
scripts/migrate-dev-db.sql applied by hand: execution_events.tenant_id with the composite foreign key
to runs(tenant_id, id), fake_external_tickets keyed by (tenant_id, idempotency_key), and
fake_external_ticket_calls.tenant_id with its (tenant_id, idempotency_key) index.

Constraint names are spelled out and equal the names Postgres gave the create_all tables (e.g.
agents_pkey, fake_external_tickets_ticket_id_key), so an existing dev database matches this revision exactly and
is brought under Alembic with `alembic stamp 0001` instead of being recreated.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('agents',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('draft_spec', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('archived', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name='agents_pkey')
    )
    op.create_table('agent_versions',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('agent_id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=False),
    sa.Column('spec', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('spec_hash', sa.String(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(), nullable=False),
    sa.ForeignKeyConstraint(['agent_id'], ['agents.id'], name='agent_versions_agent_id_fkey'),
    sa.PrimaryKeyConstraint('id', name='agent_versions_pkey'),
    sa.UniqueConstraint('agent_id', 'version', name='uq_agent_version')
    )
    op.create_table('runs',
    sa.Column('id', sa.String(), nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('agent_id', sa.String(), nullable=False),
    sa.Column('agent_version', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('input', sa.Text(), nullable=True),
    sa.Column('output', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name='runs_pkey'),
    sa.UniqueConstraint('tenant_id', 'id', name='uq_runs_tenant_id_id')
    )
    op.create_table('side_effects',
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('state', sa.String(), nullable=False),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('tenant_id', 'key', name='side_effects_pkey')
    )
    op.create_table('execution_events',
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('event_key', sa.String(), nullable=False),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('ts', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id', 'run_id'], ['runs.tenant_id', 'runs.id'], name='fk_execution_events_tenant_run'),
    sa.PrimaryKeyConstraint('run_id', 'seq', name='execution_events_pkey'),
    sa.UniqueConstraint('run_id', 'event_key', name='uq_run_event_key')
    )
    op.create_index('ix_execution_events_tenant_run', 'execution_events', ['tenant_id', 'run_id'], unique=False)
    op.create_table('fake_external_ticket_calls',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('idempotency_key', sa.String(), nullable=False),
    sa.Column('called_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name='fake_external_ticket_calls_pkey')
    )
    op.create_index(op.f('ix_fake_external_ticket_calls_idempotency_key'), 'fake_external_ticket_calls', ['idempotency_key'], unique=False)
    op.create_index('ix_fake_external_ticket_calls_tenant_key', 'fake_external_ticket_calls', ['tenant_id', 'idempotency_key'], unique=False)
    op.create_table('fake_external_tickets',
    sa.Column('tenant_id', sa.String(), nullable=False),
    sa.Column('idempotency_key', sa.String(), nullable=False),
    sa.Column('ticket_id', sa.String(), nullable=False),
    sa.Column('title', sa.Text(), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('tenant_id', 'idempotency_key', name='fake_external_tickets_pkey'),
    sa.UniqueConstraint('ticket_id', name='fake_external_tickets_ticket_id_key')
    )


def downgrade() -> None:
    op.drop_table('fake_external_tickets')
    op.drop_index('ix_fake_external_ticket_calls_tenant_key', table_name='fake_external_ticket_calls')
    op.drop_index(op.f('ix_fake_external_ticket_calls_idempotency_key'), table_name='fake_external_ticket_calls')
    op.drop_table('fake_external_ticket_calls')
    op.drop_index('ix_execution_events_tenant_run', table_name='execution_events')
    op.drop_table('execution_events')
    op.drop_table('side_effects')
    op.drop_table('runs')
    op.drop_table('agent_versions')
    op.drop_table('agents')
