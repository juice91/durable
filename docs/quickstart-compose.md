# Quick start with Docker Compose

Try the whole platform on one machine: Postgres with pgvector, the API, the worker and Studio, no Kubernetes. You need
Docker with Compose 2.24 or later, about 8 GB of memory for the first build and 5 GB of disk.

## Three steps

1. Get the code.

   ```sh
   git clone https://github.com/juice91/durable.git
   cd durable
   ```

2. Start it. Optionally add a model provider key first; the platform also starts without one.

   ```sh
   cp .env.example .env      # optional: uncomment a provider key in .env
   docker compose up -d --wait
   ```

   The first run builds the two images, which takes several minutes. Later starts take under a minute.

3. Create your admin account. This prints a one-time link:

   ```sh
   docker compose exec api setup-link
   ```

   ```text
   Open http://localhost:3000/setup#code=XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX
   ```

   Open it, choose a username and a password (12 characters or more), and sign in.

The API also writes a setup code to its log at every start while there is no admin (`docker compose logs api`). Only the
newest code is valid: `setup-link` and a restart replace it. Once the account exists, the code is gone and the setup
page answers "not found". Anyone who can run Docker commands on this machine can create the account before you do, and
already controls the installation.

## Model providers

Put any of these in `.env`, then run `docker compose up -d` again:

| Setting | Provider |
|---|---|
| `OPENAI_API_KEY` (and optionally `OPENAI_BASE_URL`) | OpenAI; models are `openai:<model>` |
| `ANTHROPIC_API_KEY` | Anthropic; models are `anthropic:<model>` |
| `EAP_LITELLM_BASE_URL` and `EAP_LITELLM_API_KEY` | a LiteLLM or other OpenAI-compatible gateway; models are `litellm:<model>` |

Studio shows each provider's state under **Settings → Providers**. A gateway on this machine is reached at
`host.docker.internal` (Docker Desktop; on Linux add `extra_hosts: ["host.docker.internal:host-gateway"]` to the `api`
and `worker` services in a `compose.override.yaml`). Embeddings (knowledge bases, semantic recall) have their own
settings, listed in `.env.example`.

Keys in `.env` stay on this machine; `.env` is ignored by git.

## What runs

| Service | What it is | Reachable |
|---|---|---|
| `studio` | the web UI; forwards `/api/` to the API | `http://localhost:3000` (this machine only) |
| `api`, `worker` | the control-plane API and the worker that runs agents and workflows | inside Compose only |
| `postgres` | Postgres 17 with pgvector | inside Compose only |
| `init`, `migrate` | run once per start: generate missing secrets, upgrade the database | |

The API is called through Studio's address too (`http://localhost:3000/api/v1/...`), for example with a tenant API key.
Port 3000 taken? Set `EAP_STUDIO_PORT` in `.env`.

Defaults: one built-in admin account ([IAM](iam.md)) plus tenant API keys; every signed-in user is admin (the
`openaccess` policy, which is what a single account is); inbound webhooks and external agents are off. Managed MCP
servers need Kubernetes and are not available; remote MCP servers work.

The API runs in production mode, which refuses a policy that makes every signed-in user admin unless
`EAP_IAM_ALLOW_TRANSITIONAL=1` says that is intended: `compose.yaml` sets it because there is one admin account under
`openaccess`, and after a switch to ZITADEL for several users with OpenFGA as the policy ([IAM](iam.md)) you remove it.

## Secrets

The `init` service generates the sign-in signing key, the key that encrypts stored secrets, the audit-log salt and the
database passwords into the volume `secrets`, once. A later `up` keeps them; nothing prints them and none is in the
repository. The services read them from files (`EAP_DATABASE_URL_FILE` and the like). Each file is readable only by the
one user that needs it, and every `up` applies that again. Postgres reads from a volume of its own, `postgres_secrets`,
which `init` fills with a copy of the database passwords; there is nothing to back up in it.

## Upgrade

```sh
git pull
docker compose up -d --build --wait
```

The database is upgraded before the API starts. Back up first.

## Back up and restore

Your data is in three volumes: `postgres_data` (the database), `secrets` (without `secrets_key` the stored secrets
cannot be read) and `blobs` (chat attachments). Compose prefixes their names with the project name, `eap-quickstart`.
Stop the stack, then archive them:

```sh
docker compose down
mkdir -p backup
for volume in postgres_data secrets blobs; do
  docker run --rm -v "eap-quickstart_${volume}:/data:ro" -v "$PWD/backup:/backup" \
    pgvector/pgvector:0.8.2-pg17-trixie tar -czf "/backup/${volume}.tar.gz" -C /data .
done
docker compose up -d --wait
```

The archive of `secrets` opens everything in the other two: keep it as carefully as a password. To restore, on a stopped
and reset installation (below):

```sh
for volume in postgres_data secrets blobs; do
  docker volume create "eap-quickstart_${volume}"
  docker run --rm -v "eap-quickstart_${volume}:/data" -v "$PWD/backup:/backup:ro" \
    pgvector/pgvector:0.8.2-pg17-trixie tar -xzf "/backup/${volume}.tar.gz" -C /data
done
docker compose up -d --wait
```

Compose warns that it did not create these volumes; that is expected after a restore.

## Forgotten password

```sh
docker compose exec api eap-entrypoint run python -m eap_iam_localadmin reset-password
```

It asks for the new password twice, ends every session, and the next sign-in asks you to change it.

## Logs and traces (optional)

Add to `.env`, then `docker compose up -d`:

```sh
COMPOSE_PROFILES=observability
OTEL_EXPORTER_OTLP_ENDPOINT=http://lgtm:4318
EAP_LOKI_URL=http://lgtm:3100
```

Grafana is on `http://localhost:3001`, and Studio's log search works. This stack has no authentication on its ingest and
query APIs: use it for trying out only.

## Stop and reset

```sh
docker compose down        # stop; data and secrets are kept
docker compose down -v     # delete everything, the database and the secrets included
```

After `down -v` the next `up` is a new installation: new secrets, no admin account. Delete the volumes together: a
database without its `secrets` volume cannot be opened.

## Putting it on a server

This setup listens on this machine only, over plain HTTP. Before other people reach it, read
[Expose Studio and the API](expose.md): TLS, sign-in, and the paths outside systems call.
