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
`api.vendor.example`, `www.ti.com`) is enforced on every request and every redirect hop; documents must be PDFs; a replayed step
changes nothing; every table and route is tenant-scoped, and another tenant gets `404`.

## 1. Bootstrap the tenant secrets

The values go from 1Password into `PUT /api/v1/secrets/{name}` without touching argv, a log or a temp file. Open the
tunnel first (`deploy/scripts/tunnel.sh`, API on `127.0.0.1:18000`), then:

```sh
API=http://127.0.0.1:18000/api/v1
AUTH="Authorization: Bearer dev:studio:operator"   # the dev principal of the studio tenant

put_secret() {  # put_secret <secret name> <op:// reference>: the value is piped, never printed
  op read "$2" | python3 -c 'import json, sys; print(json.dumps({"value": sys.stdin.read().rstrip("\n")}))' \
    | curl -sf -X PUT "$API/secrets/$1" -H "$AUTH" -H 'Content-Type: application/json' --data-binary @-
}
put_secret ti-client-id     op://<vault>/<item>/TI_KEY
put_secret ti-client-secret op://<vault>/<item>/TI_SECRET
curl -sf "$API/secrets" -H "$AUTH" | jq '.[].name'   # names only
```

The secret names are free (a name matches `^[a-z][a-z0-9_.-]{0,63}$`); the connection refers to them. In Studio the same is
Settings → Secrets, then pick them in Settings → Connections.

## 2. Create the connection and check it

```sh
CONN=$(curl -sf -X POST "$API/settings/connections" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "name": "ti", "kind": "oauth2_client_credentials", "base_url": "https://api.vendor.example",
  "config": {"provider": "acme_catalog", "allowed_hosts": ["api.vendor.example", "www.ti.com"]},
  "credential_refs": {"client_id": "secret:ti-client-id", "client_secret": "secret:ti-client-secret"}}' | jq -r .id)
curl -sf -X POST "$API/settings/connections/$CONN/check" -H "$AUTH"
# {"ok": true, "checked_at": "...", "error_code": null}
```

The check fetches a token and one product and persists `last_checked_at` / `last_status`. A failure answers a stable
`error_code`: `secret_missing`, `connection_auth_failed`, `connection_unreachable`, `connection_response_invalid`,
`connection_disabled`, `connection_not_found`. A connection that a source uses cannot be deleted (`409 connection_in_use`).

## 3. Create the knowledge base and the source

```sh
KB=$(curl -sf -X POST "$API/knowledge-bases" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"name": "TI catalogue", "embedding_model": "litellm:Qwen3-VL-Embedding-8B-mlx-8bit"}' | jq -r .id)
SRC=$(curl -sf -X POST "$API/knowledge-bases/$KB/sources" -H "$AUTH" -H 'Content-Type: application/json' -d "{
  \"connection_id\": \"$CONN\", \"name\": \"ti-catalogue\", \"kind\": \"product_catalog\", \"provider\": \"acme_catalog\",
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

Run it: `POST /workflows/$WF/runs` with `{"input": {"source_id": "...", "mode": "targeted", "part_numbers": ["..."]}}`; poll
`GET /runs/{run_id}`; the run's `output` is the bounded report (`discovered`, `inserted`, `updated`, `unchanged`,
`documents_queued`, `failed`, `errors` up to 50, `checkpoint`, `complete`, `stop_reason`, `budget`, `source_revision`).
Cancel with `POST /runs/{run_id}/cancel`: the child sync is cancelled too and keeps its checkpoint. Schedule an incremental
sync with a workflow schedule of this workflow and the input `{"source_id": "...", "mode": "incremental"}`.

`GET /knowledge-bases/{kb}/sources/{src}/syncs` lists the sync runs and `.../syncs/{run_id}` returns the report.

## 5. Staged live tests (against the real TI API, after approval of the account)

Run them in this order; stop at the first stage that does not match, and read `errors` of the report.

1. **Check** the connection (stage 2). `ok: true` proves the token endpoint, the credentials and `allowed_hosts`.
2. **One part:** targeted sync of the hero part `EXAMPLE-PART-001`. Expect `discovered 1`, `failed 0`, one datasheet queued, and
   the document `ready` in `GET /knowledge-bases/$KB/documents` after the ingest. Run it again: `unchanged`, no new version.
3. **Ten to a hundred parts:** a targeted sync of a hand-picked list. Check `API calls per product` stays at about 3
   (product, parametrics, quality) and that 404 parts are counted `not_found`, not failed runs.
4. **One page:** a `full` sync with `product_daily_limit` set so the run stops after the first listing page (about `3 x
   page_size + 1` calls). Expect `complete: false`, `stop_reason: "budget"` and a `checkpoint.cursor.page`.
5. **Resume:** raise the limit and run `full` again; the walk continues at the stored page (watch the listing calls in
   Grafana) and the final report has `complete: true` and a `source_revision`.
6. **Incremental:** run `incremental` twice; the second run costs one call per listing page and reports every item `unchanged`.
7. **Full catalogue last.** About 1,486 pages plus about three detail calls per product against 3,000 calls a day is a
   multi-day walk: let the daily budget stop it and schedule the next run. Ask TI for a non-standard limit first if the
   timetable matters.

Open items to confirm on the authenticated API (the adapter keeps them configurable): the listing endpoint
`/v1/products?Page=N&Size=100`, the `/parametrics` and `/quality` paths, and whether TI offers a changed-since filter.

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
