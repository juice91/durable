# eap-db

Alembic migrations for the `eap_app` database. This package is the only thing that creates or
alters its tables: the API and the worker never call `create_all`, and at startup both refuse to run
unless the database is at the latest migration:

```
database schema is not at the latest migration; run: uv run alembic -c packages/db/alembic.ini upgrade head
```

DBOS's own system database (`eap_dbos`) is managed by DBOS and is not covered here.

## Where things are

- `alembic.ini` — CLI config. The database URL is **not** stored here; `env.py` reads
  `EAP_DATABASE_URL`.
- `src/eap_db/migrations/` — `env.py` and `versions/`. `0001_baseline.py` is the schema as milestone 1
  left it, including the tenant columns, composite keys and indexes from `scripts/migrate-dev-db.sql`.
  `0002_conversations.py` adds `conversations` and the nullable `runs.conversation_id`, `runs.turn`
  (composite FK to `conversations(tenant_id, id)`, UNIQUE `(conversation_id, turn)`) and
  `runs.messages`.
- `src/eap_db/metadata.py` — the target metadata: the union of `eap_api.models.Base.metadata`,
  `eap_run_store.Base.metadata` and `eap_worker.tickets.metadata`. Tables are defined only in those
  modules; this package never duplicates them.
- `src/eap_db/__init__.py` — `require_schema_at_head(engine)`, the startup check the API and worker
  call (imports only Alembic and SQLAlchemy, never another app's code).
- `src/eap_db/testing.py` — pytest plugin, see Tests.

All commands below run from the repo root.

```
export EAP_DATABASE_URL=postgresql+psycopg://eap_app:eap_app_password@localhost:54329/eap_app
```

## Upgrade a database

```
uv run alembic -c packages/db/alembic.ini upgrade head   # apply every pending migration
uv run alembic -c packages/db/alembic.ini current        # shows "<rev> (head)" when up to date
uv run alembic -c packages/db/alembic.ini history        # the revision chain
```

An empty database is built entirely by `upgrade head`. To migrate a single schema instead of
`public`, set its search path in the URL:
`...?options=-csearch_path%3Dmy_schema`.

### A database that existed before Alembic

A dev database created by the old runtime `create_all` (and brought up to date with
`scripts/migrate-dev-db.sql`) already has the baseline schema. Do not upgrade it (the baseline would
try to create existing tables); confirm it matches and record it as migrated:

```
uv run python -c "from sqlalchemy import create_engine; import os; from eap_db.metadata import schema_diff; \
  e = create_engine(os.environ['EAP_DATABASE_URL']); c = e.connect(); print(schema_diff(c) or 'no differences')"
uv run alembic -c packages/db/alembic.ini stamp head     # only if it printed "no differences"
uv run alembic -c packages/db/alembic.ini check          # "No new upgrade operations detected."
```

`stamp` only writes the `alembic_version` table; no table or row is touched.

## Create a new migration

1. Change the SQLAlchemy model where the table lives (`apps/api/src/eap_api/models.py`,
   `packages/run-store/src/eap_run_store/store.py` or `apps/worker/src/eap_worker/tickets.py`). A
   table in a new module also needs that module's metadata added to `target_metadata()` in
   `src/eap_db/metadata.py`.
2. Bring your database to head (`upgrade head`), then generate the revision from the difference:

   ```
   uv run alembic -c packages/db/alembic.ini revision --autogenerate -m "add conversations"
   ```

3. Read and edit the generated file in `src/eap_db/migrations/versions/`. Autogenerate does not
   write data migrations (backfills), and cannot tell a rename from a drop plus an add. Give
   constraints and indexes explicit names. Adding a `NOT NULL` column to a table with rows needs a
   server default or a backfill before the constraint.
4. Apply it and confirm the models and the database agree:

   ```
   uv run alembic -c packages/db/alembic.ini upgrade head
   uv run alembic -c packages/db/alembic.ini check          # "No new upgrade operations detected."
   ```

5. Run `uv run pytest -q`: every session migrates a fresh schema from scratch, so a broken chain or a
   model/migration mismatch fails `packages/db/db_tests`.

Hand-written migrations (`revision -m ...` without `--autogenerate`) follow the same steps 3–5.

## Tests

The `eap_db` pytest plugin (entry point `pytest11`, loaded automatically in the workspace venv)
creates a uniquely named schema `eap_test_<hex>` in the database of `EAP_TEST_DATABASE_URL` (default:
the dev database above), runs `alembic upgrade head` in it, and sets `EAP_TEST_DATABASE_URL` and
`EAP_DATABASE_URL` to a URL whose `search_path` is that schema, before any test module is imported.
Every suite, and every worker or API subprocess a test starts, therefore runs against a migrated
throwaway schema and never against the shared `public` schema. The schema is dropped when the
session ends. If Postgres is unreachable the plugin only warns. Disable it with `-p no:eap_db`.
