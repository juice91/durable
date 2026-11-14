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
  `0004_mcp_servers.py` adds `mcp_servers` and `mcp_server_secrets` (encrypted MCP header values,
  composite FK to `mcp_servers(tenant_id, id)`, ON DELETE CASCADE); models in
  `apps/api/src/eap_api/mcp_servers/models.py`.
  `0005_workflows.py` adds `workflows` (tenant-owned drafts, like `agents`) and `workflow_versions`
  (immutable published specs, UNIQUE `(workflow_id, version)`, composite FK to
  `workflows(tenant_id, id)`); models in `apps/api/src/eap_api/workflows/models.py`. Downgrade drops
  both tables, so every workflow definition and published version is deleted.
  `0006_tool_runs.py` adds `runs.kind` (`'agent'` | `'tool'`, default `'agent'`) and `runs.tool_id`,
  makes `runs.agent_id` / `runs.agent_version` nullable, adds the `ck_runs_kind_target` check and
  the indexes `ix_runs_tenant_kind_created` and `ix_runs_tenant_tool_created`. **Downgrade deletes
  data:** every tool run, its `execution_events` and its `side_effects` reservations
  (`{run_id}:tool`, `{run_id}:tool_run`), then restores NOT NULL on the agent columns.
  `0007_workflow_runs.py` adds `runs.workflow_id`, `runs.workflow_version` (kind `'workflow'`),
  `runs.parent_run_id`, `runs.parent_step_id` (a workflow's agent step runs as a child agent run;
  composite FK to `runs(tenant_id, id)`), widens `ck_runs_kind_target`, adds `ck_runs_parent_link`
  and the indexes `ix_runs_tenant_workflow_created` and `ix_runs_parent_run`. Per-step state is not a
  table: it is derived from the run's events. **Downgrade deletes data:** every workflow run and
  every child agent run, their `execution_events` and their `side_effects` reservations (keys
  `{run_id}:...`), then restores the 0006 check and drops the columns.
  In both downgrades the fake external ticket service's rows are left alone (they stand for an
  external system). Deleting the rows is deliberate: a later re-upgrade must not hand a reused
  client `run_id` an old completed result or an old `reserved` reservation.
  `0003_runs_tenant_created_index.py` adds `ix_runs_tenant_created` (a tenant's runs newest first).
  `0008_run_usage.py` adds `run_model_requests` (one row per model request, keyed by
  `(run_id, request_key)`, written in its event's transaction, `ON CONFLICT DO NOTHING`; `cost_usd`
  fixed at record time), the run totals and timestamps on `runs` (`model_id`, `model_overridden`,
  `input_tokens`, `output_tokens`, `cost_usd`, `started_at`, `finished_at`; NULL for older runs) and
  `ix_execution_events_tenant_type_ts`. Downgrade drops them (usage data is lost).
  `0009_version_restored_from.py` adds `agent_versions.restored_from` (a restore publishes a new
  version and names its source; composite FK to the same agent's version). Downgrade drops it.
  `0010_run_options.py` adds `runs.request_context` and `runs.draft_spec` (draft test runs), widens
  `ck_runs_kind_target` and adds `ix_conversations_tenant_agent_updated`. **Downgrade deletes data:**
  every draft run with its events, usage rows and reservations; drain the worker queue first.
  `0011_knowledge_bases.py` (RAG) needs the `vector` extension (see pgvector below) and adds
  `knowledge_bases` (active and pending generations), `kb_documents`, `kb_chunks` (an untyped
  `public.vector` with `dimensions` and one partial HNSW index per dimension, 768, 1024, 1536),
  `runs.knowledge_base_id`, the `kb_ingest` run kind and `run_model_requests.operation`
  (`chat` | `embedding`). **Downgrade deletes data:** every `kb_ingest` run with its events, usage
  rows and reservations, and all knowledge base content; the extension stays.
  `0012_memory.py` adds `conversations.resource_id` (immutable), `working_memories` (one note per
  agent, scope and scope key; conversation notes cascade with their conversation),
  `memory_observations` (observations and reflections, `superseded_by`) and
  `ix_side_effects_tenant_key_pattern` (prefix deletes of a run's ledger rows). Downgrade drops
  them: all working memory, observations and resource ids are lost.
  `0013_semantic_recall.py` adds `message_embeddings` (one row per indexed turn text and role,
  partial HNSW per dimension, cascades from its conversation and its run) and
  `memory_forget_watermarks` (a late index step after a forget skips). Downgrade drops both.
- `src/eap_db/metadata.py` — the target metadata: the union of `eap_api.models.Base.metadata` (which
  also holds `eap_api.mcp_servers.models` and `eap_api.workflows.models`),
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

### Offline SQL (`upgrade --sql`)

`uv run alembic -c packages/db/alembic.ini upgrade base:head --sql > upgrade.sql` writes the whole
chain as SQL without a database connection (`EAP_DATABASE_URL` only selects the dialect). Offline,
0011 cannot check the installed pgvector: it emits `CREATE EXTENSION IF NOT EXISTS vector SCHEMA
public` instead, so run the script as a role that may create the extension (or create it first as a
superuser), and make sure the server has pgvector 0.8.0 or later; an online `upgrade head` checks
both. Verified against a fresh `pgvector/pgvector:0.8.2-pg17` database.

## pgvector (the `vector` extension)

Knowledge bases and semantic recall store embeddings in `eap_app` with pgvector. The dev Postgres
runs `pgvector/pgvector:0.8.2-pg17-trixie` (`infrastructure/docker/compose.yaml`): the official
Postgres 17 image plus the extension, so it opens a volume written by the former `postgres:17` image
unchanged, with no dump and restore. The `-trixie` suffix matters: `postgres:17` is Debian 13 with
glibc 2.41, and the plain `-pg17` tag is Debian 12 with glibc 2.36, whose different collation
version would leave text indexes on an existing volume silently mis-sorted (Postgres warns
`collation version mismatch`). The extension lives in `eap_app`'s `public` schema.

`CREATE EXTENSION` needs a superuser and the `eap_app` role is not one, so the admin role creates it:
`init-scripts/01-setup-databases.sh` for a new volume, and `scripts/dev-up.sh` (through
`ensure_vector_extension` in `scripts/dev-lib.sh`) for an existing one, on every start and as a
no-op once it exists. Migrations only check for it (`IF NOT EXISTS`). A downgrade never drops the
extension: it is shared by every schema in the database, including the test schemas. Production
provisioning needs the same one-time admin step:
`CREATE EXTENSION IF NOT EXISTS vector;` in the app database, as a superuser.

### Writing migrations and models that use pgvector

Migrations run with the search path set to the target schema alone (`env.py` refuses a path of more
than one schema; see Tests), so nothing from the extension resolves unqualified there. Migration 0011
and later ones therefore write:

- `CREATE EXTENSION IF NOT EXISTS vector SCHEMA public` (a no-op where the admin step already ran;
  it fails for the non-superuser app role when the extension is missing, so name that fix in the
  error), and never drop the extension on downgrade;
- the column type as `public.vector`: pgvector's `Vector()` renders an unqualified `VECTOR`, so add
  vector columns with `op.execute("ALTER TABLE ... ADD COLUMN embedding public.vector")` (or a
  column type whose DDL is `public.vector`), not `sa.Column(..., Vector())`;
- casts as `::public.vector(N)`, e.g. the partial index expression
  `((embedding::public.vector(1536)) public.vector_cosine_ops) WHERE dimensions = 1536`;
- operator classes as `public.vector_cosine_ops`;
- operators in migration SQL as `OPERATOR(public.<=>)`.

Models import the type from `pgvector.sqlalchemy` (`Vector`); the app's connections have `public` on
their search path (the default `"$user", public`, and `<schema>,public` in test sessions), so runtime
queries use the plain `vector` type and `<=>`.

### Upgrading an existing dev volume

What must survive is everything in `eap_app` (agents, versions, runs, events, conversations, MCP
servers with their encrypted secrets) and `eap_dbos` (in-flight workflow checkpoints). Both stay on
the Postgres data volume; only the container is replaced.

**Pass the container's own compose project.** The project name differs between machines (by
default it is the compose file's directory, `docker`, but an older checkout or an explicit `-p` may
have made it `eap`), and the data volume is `<project>_postgres_data`. A plain `docker compose -f ...
up -d postgres` uses the default project: it fails on the `eap_postgres` name when the container
belongs to another project, and once that container is removed it starts on
`docker_postgres_data`, a different, possibly empty, volume. Read the project and the volume first:

```
scripts/dev-down.sh                       # the app processes; Postgres keeps running
project=$(docker inspect eap_postgres --format '{{index .Config.Labels "com.docker.compose.project"}}')
volume=$(docker inspect eap_postgres \
  --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}')
echo "project=$project volume=$volume"    # continue only if volume is "${project}_postgres_data"
# optional backup:
mkdir -p ~/.eap-backups
docker exec eap_postgres sh -c 'pg_dumpall -U "$POSTGRES_USER"' > ~/.eap-backups/eap-$(date +%Y%m%d-%H%M%S).sql
docker compose -p "$project" -f infrastructure/docker/compose.yaml pull postgres
docker compose -p "$project" -f infrastructure/docker/compose.yaml up -d postgres   # recreates the container
docker inspect eap_postgres \
  --format '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}'
                                          # must print the same $volume
scripts/dev-up.sh                         # creates the extension, then migrates
```

If the project label is empty (a container not created by compose), take the project from the
volume name (`eap_postgres_data` → `-p eap`) and `docker rm -f eap_postgres` before `up`; a named
volume survives `docker rm`. A volume not named `<project>_postgres_data` cannot be mounted by
compose as it is: take the backup above and restore it into the new container instead. Never add a top-level `name:` to `compose.yaml` to settle this: it
would move the volumes of fresh clones.

`dev-up.sh` alone does not swap the image: it restarts the existing container as it is. When that
container has no pgvector, it stops and prints these steps with the project and volume filled in.
If no `eap_postgres` container exists at all, `dev-up.sh` and `dev-db.sh up` refuse to let compose
create one while a `*_postgres_data` volume other than the default project's exists, and print the
`-p <project>` command for each; `EAP_POSTGRES_ALLOW_DEFAULT_VOLUME=1` accepts the default.
The compose healthcheck escapes its variable (`$${POSTGRES_USER}`) so the container's own
environment supplies the user; compose run without `dev-lib.sh`'s environment still gets a healthy
container.

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
`EAP_DATABASE_URL` to a URL whose `search_path` is `<schema>,public`, before any test module is
imported. Every suite, and every worker or API subprocess a test starts, therefore runs against a
migrated throwaway schema and never against the shared `public` schema's tables. The schema is
dropped when the session ends.

`public` is on that path only so that pgvector's `vector` type and operators resolve; the session
schema is at head, so its tables shadow those in `public`. The migrations themselves run with the
path set to the schema alone (an unqualified `alembic_version` would otherwise resolve to the dev
database's own) and write extension types as `public.vector`. For a schema of your own, use
`schema_url(base, schema)` while it is empty or partly migrated, and
`schema_url(base, schema, public=True)` once it is at head and only for app queries, never to
migrate again. `create_migrated_schema(base, schema)` returns the schema-only URL (safe for an
`upgrade`/`downgrade` round trip); `public=True` opts in for runtime use. The migration environment
enforces this: it refuses to run when `current_schemas(false)` has more than one schema, so a
downgrade's `DROP ... IF EXISTS` or `DELETE` can never fall through to `public`. If Postgres is unreachable the plugin only warns. Disable it with `-p no:eap_db`.
