# Knowledge-base sources: the operator guide

A tenant connects an external product catalogue, binds it to a knowledge base and keeps both in step. The
first provider is the Acme Semiconductor Product Information API. What the sync leaves behind is an ordinary
knowledge base (datasheets) plus a structured product store; agents use it through `retrieve` and never see a
provider. Design: `docs/superpowers/specs/2026-11-18-kb-source-sync-design.md`. Acceptance:
`docs/superpowers/plans/2026-11-18-kb-source-sync-acceptance.md`. The workflow itself:
`docs/workflows/kb-source-sync.md`.

## Concepts

| Thing | What it is |
|---|---|
| **Secret** | A tenant secret (`PUT /api/v1/secrets/{name}`), stored encrypted, never returned. |
| **Connection** | `settings_connections`: kind `oauth2_client_credentials`, `base_url`, non-secret `config` (`provider`, `allowed_hosts`, `token_url`) and `credential_refs` (`{"client_id": "secret:<name>", "client_secret": "secret:<name>"}`). Values are never stored here. |
| **Source** | `kb_sources`: one connection bound to one knowledge base, kind `product_catalog`, with its `config` (`document_policy`, `page_size`, daily limits) and a stored checkpoint. |
| **Sync run** | One execution of the native workflow `eap.kb_source_sync` (kind `kb_source_sync`): segments of at most 25 pages or 20 parts, each a DBOS child that writes keyed, hash-guarded rows. Nobody starts it by hand. |
| **Workflow** | "KB Source Synchronization" (`examples/workflows/kb-source-sync.json`): the one way an operator, a schedule or Studio runs a sync. It composes `kb.source.get`, `settings.connection.check`, `kb.source.sync`, `workflow.run.wait` and `workflow.run.result`. |
| **Structured store** | `kb_source_records` (raw payloads by hash), `kb_entities`, `kb_entity_identifiers` (generic, orderable and manufacturer part numbers), `kb_entity_parameters` (value, unit, normalised value), `kb_entity_documents` (the ledger of datasheets: hash, ETag, Last-Modified, the knowledge-base document). |
| **Modes** | `targeted` (up to 100 part numbers), `incremental` (walks the catalogue listing, fetches only items whose listing entry changed), `full` (refreshes everything; a complete walk marks vanished products `removed`). |
| **Checkpoint** | The cursor of an unfinished walk plus the daily call budget, at most 8 KiB, stored on the source. The next run of the same mode resumes it; `restart: true` on the raw sync route starts over. |

Safety properties to know: the secret values are resolved only inside the step that calls the provider and never appear
in events, run rows, DBOS outputs, API responses, spans or logs; the connection's `allowed_hosts` (default
the connection's `allowed_hosts`) is enforced on every request and every redirect hop; documents must be PDFs; a replayed step
changes nothing; every table and route is tenant-scoped, and another tenant gets `404`.

## 1. Credentials enter through the platform, never through an API call

A data source is not a feature: its credentials are entered where every other tenant secret is entered, and the
framework names no provider secret. Three surfaces, all backed by the same encrypted tenant secret store:

- **Studio → Settings → Connections**: the connection sheet's Client ID / Client secret pickers choose a stored secret by
  name or create one inline through a masked input (the value is sent once, stored encrypted, never shown again).
- **Studio → Settings → Secrets**: the same masked input, for secrets created ahead of time.
- **An agent with `secrets.request`**: the tool pauses on an approval card with a masked input; the person types the value,
  the agent only ever sees the reference `secret:<name>` (this is how an onboarding agent or the Architect connects a source).

`PUT /api/v1/secrets/{name}` is the storage behind these surfaces, not an operator entry point: do not script provider
credentials into it from a password manager or a shell.

## 2. Create the connection and check it

```sh
CONN=$(curl -sf -X POST "$API/settings/connections" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "name": "<connection name>", "kind": "oauth2_client_credentials", "base_url": "https://<provider api host>",
  "config": {"provider": "<provider id>", "allowed_hosts": ["<provider api host>", "<document host>"]},
  "credential_refs": {"client_id": "secret:<client-id-secret-name>", "client_secret": "secret:<client-secret-secret-name>"}}' | jq -r .id)
curl -sf -X POST "$API/settings/connections/$CONN/check" -H "$AUTH"
# {"ok": true, "checked_at": "...", "error_code": null}
```

The check fetches a token and one product and persists `last_checked_at` / `last_status`. A failure answers a stable
`error_code`: `secret_missing`, `connection_auth_failed`, `connection_unreachable`, `connection_response_invalid`,
`connection_disabled`, `connection_not_found`. A connection that a source uses cannot be deleted (`409 connection_in_use`).

## 3. Create the knowledge base and the source

```sh
KB=$(curl -sf -X POST "$API/knowledge-bases" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "<knowledge base name>", "embedding_model": "<allowed embedding model>"}' | jq -r .id)
SRC=$(curl -sf -X POST "$API/knowledge-bases/$KB/sources" -H "$AUTH" -H 'Content-Type: application/json' -d "{
  \"connection_id\": \"$CONN\", \"name\": \"<source name>\", \"kind\": \"product_catalog\", \"provider\": \"<provider id>\",
  \"config\": {\"document_policy\": {\"types\": [\"datasheet\"]}, \"page_size\": 100}}" | jq -r .id)
```

Source `config` (all optional): `document_policy.types` and `document_policy.max_bytes`, `page_size` (1..1000),
`include_extended` (the slower orchestrated endpoint), `product_daily_limit` (default 3,000) and `orchestrated_daily_limit`
(default 1,500), `probe_part`, and the endpoint paths (`products_path`, `product_path`, `parametrics_path`, `quality_path`,
`extended_path`) in case the authenticated API reference differs from the public pages. A knowledge base takes at most
2,000 documents; a sync that reaches the cap counts `kb_capacity` and goes on with the structured store.

## 4. Publish the workflow

```sh
WF=$(jq '{spec: .spec}' examples/workflows/kb-source-sync.json \
  | curl -sf -X POST "$API/workflows" -H "$AUTH" -H 'Content-Type: application/json' --data-binary @- | jq -r .id)
curl -sf -X POST "$API/workflows/$WF/versions" -H "$AUTH" | jq .warnings   # []
```

Run it: `POST /workflows/$WF/runs` with `{"input": {"source_id": "...", "mode": "targeted", "part_numbers": ["<orderable part>"]}}`; poll
`GET /runs/{run_id}`; the run's `output` is the bounded report (`discovered`, `inserted`, `updated`, `unchanged`,
`documents_queued`, `failed`, `errors` up to 50, `checkpoint`, `complete`, `stop_reason`, `budget`, `source_revision`, and `diagnostics` when an adapter reported something that looked wrong, e.g. a catalogue page that parsed to no products).
Cancel with `POST /runs/{run_id}/cancel`: the child sync is cancelled too and keeps its checkpoint. Schedule an incremental
sync with a workflow schedule of this workflow and the input `{"source_id": "...", "mode": "incremental"}`.

`GET /knowledge-bases/{kb}/sources/{src}/syncs` lists the sync runs and `.../syncs/{run_id}` returns the report.

Provider-specific values (hosts, secret field names, probe parts, quotas) are not part of this guide: see the provider page. Providers: GitHub repository documents is built in (`docs/providers/github.md`, source kind `repository_docs`, connection kind `api_token`); other providers are separate packages (`docs/providers/index.md`), for example the Acme Semiconductor product catalogue (`packages/kb-sources-acme/README.md`, source kind `product_catalog`).

## 5. Staged live tests

Never start with a full catalogue. Stage it: (1) check the connection; (2) a targeted sync of one known part and read the
report (`discovered 1`, `failed 0`, one datasheet queued, `kb_ingest` completes); (3) a targeted sync of 10-100 parts;
(4) one listing page in `full` mode and confirm the checkpoint and the resume on the next run; (5) only then a full walk,
which spans days under the provider's daily quota. Provider-specific parts, hosts, quotas and secret field names live on the
provider page (for Acme Semiconductor: `packages/kb-sources-acme/README.md`).

## Failure codes you will see

| Code | Meaning | What to do |
|---|---|---|
| `secret_missing` | a referenced secret is not set | `PUT /secrets/{name}`; the run is `needs_attention` |
| `connection_auth_failed` | TI refused the client credentials | rotate the secrets; the run is `needs_attention` |
| `rate_limited` | 429 after the bounded backoff | the cursor is kept; run again later |
| `not_found` | the part does not exist | counted, the batch goes on |
| `connection_response_invalid` | malformed JSON or a non-PDF document | counted, the item is skipped |
| `url_not_allowed` | a redirect left `allowed_hosts` | widen the connection's `allowed_hosts` only if you trust the host |
| `response_too_large` | a document above `max_bytes` (25 MiB) | counted, skipped |
| `kb_capacity` | the knowledge base has 2,000 documents | use another knowledge base |
| `run_cancelled` | the parent run was cancelled | the checkpoint is kept; run again to resume |

## Rollback

Disable a source (`PATCH .../sources/{id}` with `{"enabled": false}`) to stop future syncs; the migration `0029_kb_sources` has a
downgrade; deleting a connection is refused while a source refers to it.
