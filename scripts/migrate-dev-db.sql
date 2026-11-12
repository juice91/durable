-- SUPERSEDED (milestone 2): Alembic in packages/db now owns the eap_app schema. This script is folded
-- into the baseline revision (packages/db/src/eap_db/migrations/versions/0001_baseline.py); a database it
-- brought up to date is put under Alembic with `uv run alembic -c packages/db/alembic.ini stamp head`
-- (see packages/db/README.md). Kept as the record of how existing dev databases reached the baseline.
--
-- Dev-DB migration for the milestone-1 tenant-ownership fix (review 2026-11-12).
--
-- The app creates tables with SQLAlchemy create_all, which never alters existing tables. This brings
-- an existing dev eap_app database up to the current models:
--   * runs:                       UNIQUE (tenant_id, id) so children can reference (tenant_id, run_id)
--   * execution_events:           tenant_id NOT NULL (backfilled from runs), composite FK to runs
--   * fake_external_tickets:      tenant_id NOT NULL (backfilled from the run in the key),
--                                 primary key (tenant_id, idempotency_key)
--   * fake_external_ticket_calls: tenant_id NOT NULL (backfilled), index (tenant_id, idempotency_key)
--
-- Idempotent: safe to run repeatedly. Deletes nothing. Does not touch agents/agent_versions.
--
--   docker exec -i eap_postgres psql -v ON_ERROR_STOP=1 -U eap_admin -d eap_app < scripts/migrate-dev-db.sql
--
-- Run it with the worker stopped, or restart the worker (and API) right after: a process still running
-- the old code inserts events/tickets without tenant_id and will fail until restarted.

BEGIN;
SET LOCAL lock_timeout = '15s';

-- runs ------------------------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_runs_tenant_id_id') THEN
        ALTER TABLE runs ADD CONSTRAINT uq_runs_tenant_id_id UNIQUE (tenant_id, id);
    END IF;
END $$;

-- execution_events ------------------------------------------------------------------------------
ALTER TABLE execution_events ADD COLUMN IF NOT EXISTS tenant_id VARCHAR;

UPDATE execution_events e
SET tenant_id = r.tenant_id
FROM runs r
WHERE r.id = e.run_id AND e.tenant_id IS NULL;

ALTER TABLE execution_events ALTER COLUMN tenant_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_execution_events_tenant_run') THEN
        ALTER TABLE execution_events
            ADD CONSTRAINT fk_execution_events_tenant_run
            FOREIGN KEY (tenant_id, run_id) REFERENCES runs (tenant_id, id);
    END IF;
END $$;

-- The composite FK subsumes the old single-column one.
ALTER TABLE execution_events DROP CONSTRAINT IF EXISTS execution_events_run_id_fkey;

CREATE INDEX IF NOT EXISTS ix_execution_events_tenant_run ON execution_events (tenant_id, run_id);

-- fake_external_tickets ---------------------------------------------------------------------------
-- Idempotency keys are "{run_id}:{tool_call_id}"; the owning tenant is the run's.
ALTER TABLE fake_external_tickets ADD COLUMN IF NOT EXISTS tenant_id VARCHAR;

UPDATE fake_external_tickets t
SET tenant_id = r.tenant_id
FROM runs r
WHERE r.id = split_part(t.idempotency_key, ':', 1) AND t.tenant_id IS NULL;

-- Tickets whose run no longer exists (should be none) are kept under an explicit orphan tenant.
UPDATE fake_external_tickets SET tenant_id = '__orphaned__' WHERE tenant_id IS NULL;

ALTER TABLE fake_external_tickets ALTER COLUMN tenant_id SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint c
        WHERE c.conname = 'fake_external_tickets_pkey'
          AND c.conrelid = 'fake_external_tickets'::regclass
          AND array_length(c.conkey, 1) = 2
    ) THEN
        ALTER TABLE fake_external_tickets DROP CONSTRAINT IF EXISTS fake_external_tickets_pkey;
        ALTER TABLE fake_external_tickets
            ADD CONSTRAINT fake_external_tickets_pkey PRIMARY KEY (tenant_id, idempotency_key);
    END IF;
END $$;

-- fake_external_ticket_calls ----------------------------------------------------------------------
ALTER TABLE fake_external_ticket_calls ADD COLUMN IF NOT EXISTS tenant_id VARCHAR;

UPDATE fake_external_ticket_calls c
SET tenant_id = r.tenant_id
FROM runs r
WHERE r.id = split_part(c.idempotency_key, ':', 1) AND c.tenant_id IS NULL;

UPDATE fake_external_ticket_calls SET tenant_id = '__orphaned__' WHERE tenant_id IS NULL;

ALTER TABLE fake_external_ticket_calls ALTER COLUMN tenant_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_fake_external_ticket_calls_tenant_key
    ON fake_external_ticket_calls (tenant_id, idempotency_key);

COMMIT;
