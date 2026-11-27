# Inbound webhooks

An outside system (a source host, a payment provider, a chat app, an in-house service) starts a workflow run with a
signed HTTP POST. Design: an internal design note. This page is what shipped:
**starting a run**. Waking a workflow that waits for an event (the `wait_event` step) is a later release.

## How it works

1. A person (or the Architect, or a script) creates a **trigger**: a published workflow, a signature preset, a secret
   reference, a payload schema and a mapping from the payload to the workflow input. It is created `disabled`.
2. A person with the right to approve its secrets (an admin) enables it in **Studio > Webhooks** (the card shows exactly
   what the trigger can do and its `config_hash`). Nothing else can enable a trigger: not an API key, not an MCP call.
3. The sender posts to `POST <EAP_WEBHOOK_BASE_URL>/api/v1/hooks/<trigger id>` with the signature headers of the preset.
4. The platform checks the signature, deduplicates the delivery, validates the payload, maps it to the workflow input and
   starts the run **as the trigger** (`principal = webhook:<trigger id>`, `started_by_kind = webhook`, acting on behalf
   of nobody).

The trigger's whole grant is "start a run of this workflow (this version, or the latest) with this mapped input". The
payload can never choose a workflow, version, tenant, agent, tool or secret; it can only become `input` through the
declared mapping.

## Who may do what

With the `openfga` policy engine (docs/iam.md, "Inbound webhooks"); `openaccess` allows every signed-in person, as it
does everywhere.

| Action | Needs |
| --- | --- |
| see a trigger, its events and deliveries | `read` on its workflow (else it answers 404, like one that does not exist; lists leave it out) |
| create, change, ask to enable, rotate, retire, disable, archive | `edit` on its workflow (editor or above) |
| enable or approve a request | a person with `edit` on the workflow **and** `grant_use` on every referenced secret (admin): 403 `grant_approval_required` otherwise |
| add a secret to an already approved (enabled) trigger | the same as enabling, because it takes effect at once; never an API key or MCP (`human_required`) |
| generate a signing secret | creating a secret (admin); the creator is registered as its owner |

This mirrors publishing a definition that newly uses a `secret:` reference: an editor prepares it, an admin approves it.
A trigger stays enabled when the person who enabled it loses their role; disable or archive it.

## The identity of a webhook run

The run acts as the trigger and nobody else: `principal = webhook:<trigger id>`, `started_by_kind = webhook`,
`on_behalf_of` empty, no recorded roles. Child runs inherit that. With agent-bound grants enforced
(`EAP_AGENT_GRANTS_ENFORCE=1`) it may use exactly what the **workflow** was granted (its `http` and `tool` steps) and what
each agent of an agent step was granted; anything else is refused with `permission_denied`. The trigger's HMAC secret is
read by the API to verify the signature and is never given to the run, so it needs no grant. Each started run writes one
audit row (`webhook_run_started`, actor `webhook` / the trigger id, the trigger's name, the run id).

## Tenants

A trigger id selects exactly one row, and that row names the tenant: the secrets, the workflow, the delivery log and the
run are that tenant's. The request cannot name a tenant, and nothing defaults to one.

## Presets

| Preset | Headers | Signed string | Notes |
| --- | --- | --- | --- |
| `github` | `X-Hub-Signature-256: sha256=<hex>` | the raw body | No timestamp. The legacy SHA-1 `X-Hub-Signature` is ignored. `X-GitHub-Delivery` is **not signed**, so the dedupe key is the body hash. |
| `standard_webhooks` | `webhook-id`, `webhook-timestamp`, `webhook-signature: v1,<base64>[ v1,<base64>...]` | `<id>.<timestamp>.<body>` | Secret `whsec_<base64>`. `v1a` (Ed25519) entries are ignored. Several signatures are accepted during a sender's own rotation. |
| `custom` | your header, e.g. `X-Signature-256: sha256=<hex>` | one of `{body}`, `{timestamp}.{body}`, `{timestamp}:{body}`, `{id}.{timestamp}.{body}` | `preset_config`: `header`, `prefix`, `algorithm` (`sha256`/`sha512`), `encoding` (`hex`/`base64`), `key_encoding` (`utf8`/`base64`/`hex`), optional `timestamp {header, unit}` and `id {header}`. A format with neither a timestamp nor a signed id needs `allow_body_hash_dedupe_only: true`. |

Stripe and Slack presets are a later release. SHA-1 and MD5 do not exist here.

Rules for every preset: the HMAC is computed over the **raw bytes**; every secret and every header candidate is compared
(`hmac.compare_digest`, no early exit); the timestamp window (`tolerance_seconds`, 60 to 900, default 300) is checked
only after a signature matched, in either direction.

## Duplicates and retries

The dedupe key is built only from bytes the signature covers: the body hash for `github` and for a `custom` format whose
id header is not signed, the signed `webhook-id` for `standard_webhooks`. A timestamp is never part of it, so a provider's
retry (new timestamp and signature, same id) is a duplicate. The run id is derived from the trigger and the key
(`uuid5`), so a redelivery can never create a second run, even after a crash at any point: the next delivery finds the
claim (answered `200 duplicate`), takes over an abandoned claim after 60 seconds, or re-hands-off a run that was recorded
but never enqueued. Two genuinely different events with a byte-identical signed body collapse into one: senders must
include a unique field (every provider does). Delivery rows are kept 30 days (the replay bound for formats without a
timestamp).

## What the sender sees

| Status | Body | When |
| --- | --- | --- |
| 202 | `{"status": "accepted", "delivery": "<id>"}` | a run was started |
| 200 | `{"status": "duplicate", "delivery": "<id>"}` | the same delivery was accepted before |
| 401 | `{"detail": "invalid signature"}` | anything signature related: missing, malformed, wrong, stale |
| 404 | `{"detail": "not found"}` | unknown, disabled, pending, archived, or edited-after-approval trigger |
| 413 | `{"detail": "request body too large"}` | over the trigger's `max_body_bytes` (checked before any HMAC) |
| 415 / 422 | fixed message and `code` | the signature was valid but the body is not UTF-8 JSON, is not an object, fails `payload_schema`, a mapping pointer does not resolve, or the mapped input does not fit the workflow's `input_schema`. Only locations and keywords are listed, never values |
| 429 | `Retry-After` | per-IP or per-trigger rate, `max_concurrent_runs`, or `max_runs_per_day` |
| 503 | `Retry-After` | the run could not be handed off (retry: it is the same run), or a live attempt holds the delivery |

The answer never contains a run id, a tenant id or a reason that helps a prober, and unknown and disabled triggers cost
the same as a bad signature.

## Trigger configuration

| Field | Meaning |
| --- | --- |
| `name` | Unique per tenant, `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`. |
| `workflow_id`, `version` | The workflow, and a version number or `"latest"` (required; `latest` shows a warning on the card). |
| `preset`, `preset_config` | See above. |
| `secrets` | One or two references `secret:<name>` to tenant secrets (never a value), or `generate: true` in Studio. |
| `tolerance_seconds` | 60 to 900, default 300; ignored by formats without a timestamp. |
| `actions.start.payload_schema` | Required JSON Schema, `type: object`, at most 16 KiB, no `$ref`, `pattern` or `patternProperties`. |
| `actions.start.input_map` | A JSON object whose leaves are a literal, `{"$pointer": "/a/b"}` (RFC 6901 into the verified payload) or `{"$payload": true}`. No expressions, no headers, no templates: a mapping is data. |
| `limits` | `rate_per_minute` (60, max 600), `max_concurrent_runs` (5, max 50), `max_runs_per_day` (2000), `max_body_bytes` (262144, max 1048576). |

Changing the workflow version, preset config, actions, limits or tolerance returns an enabled trigger to `disabled`: the
approval binds to the `config_hash` the person saw, so a change between the card and the click is refused (409). Renaming
or re-describing does not.

Publish-style warnings on the card (they never block): `webhook_version_latest`, `webhook_side_effects_without_approval`
(a tool or agent step with side effects and no approval or input step: text from the outside can steer an agent step) and
`webhook_payload_into_agent_prompt`.

## Secrets and rotation

A trigger holds references to tenant secrets (encrypted with the platform key). No route, MCP tool, event, log or
delivery row ever returns a value. The one exception is **Generate** in Studio: the create (or rotate) response shows the
new 256-bit value once (`Cache-Control: no-store`) and nothing can show it again. A secret that a live trigger uses cannot
be deleted (`secret_in_use`, with `triggers` listed).

Rotation without downtime: `rotate {add: "secret:new"}` (a second active reference; the grant is unchanged, so no new
approval), update the provider, watch `last_matched_at` per reference, then `rotate {retire: "secret:old"}`. The last
reference cannot be retired.

## Audit and logs

Every lifecycle step is a `webhook_trigger_events` row (`created`, `updated`, `enable_requested`, `enabled`,
`enable_rejected`, `disabled`, `secret_added`, `secret_retired`, `archived`) and the platform's `audit_log` records the
mutating call. Every authenticated delivery is a `webhook_deliveries` row (outcome, reason, size, timestamp skew, secret
slot, the run it started); a stale delivery with a valid signature is a row too (it proves the sender). Failures without
a valid signature are **counters in memory, not rows** (an attacker must not write to the database at the rate of their
bandwidth); Studio shows them since the last API restart. No body, signature, secret or header value is stored or logged.
The run's `run_started` event carries `source: {kind: "webhook", trigger_id, trigger_name, delivery_id}`.

## API

`/api/v1/webhook-triggers` (create, list, get, `PATCH` with `If-Match`, `enable-request`, `enable`, `decision`, `disable`,
`archive`, `rotate`, `events`) and `/api/v1/webhook-deliveries` (list, get) take a signed-in person only; MCP control has
the `webhook.*` tools (see `docs/mcp-control.md`) and can create, request enabling, rotate (not add a secret to an enabled
trigger) and disable, never enable. `GET /api/v1/webhook-triggers/{id}` adds `refusals_since_start` (counters by reason, per API process).

## Public ingress

The hook path is the only route that needs no sign-in; the HMAC signature is its only gate. Where a sign-in gate stands in
front of the host, let exactly `/api/v1/hooks/` through it. See [Expose Studio and the API](expose.md) for the ingress and
the environment (`EAP_WEBHOOK_BASE_URL`, `EAP_WEBHOOK_PUBLIC`, `EAP_WEBHOOKS_ENABLED` (off by default),
`EAP_TRUSTED_PROXY_CIDRS`, `EAP_WEBHOOK_IP_RATE_PER_MINUTE`). A test lists the route table and fails if anything but the
hook route exists under `/api/v1/hooks/`, and sends path tricks (`/api/v1/hooks/../agents`, encoded slashes, double
slashes, case variants) at it.

## Not in this release

`wait_event` (wake a waiting step), Stripe and Slack presets, the unauthenticated-noise table (`webhook_trigger_stats`),
the scheduled delivery retention sweep and the orphan sweep of `queued` webhook runs (a sender that retries re-drives the
run; one that never retries leaves it `queued`), publish-time (as opposed to card-time) warnings, and the run list's "Webhook: <trigger name>" badge and `trigger_id` filter (a run's trigger is on the run row and in its `run_started` event, and the trigger's delivery log links the run).
