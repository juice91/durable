# Identity and access (IAM)

The API authenticates through plug-ins. Core ships only the interfaces (`packages/iam`, package `eap_iam`); every
implementation is a separate distribution found through entry points. The design is
`docs/superpowers/specs/2026-11-22-iam-design.md`; this page is what runs today (phases P0a and P0b).

| Entry-point group | Interface | Plug-ins today |
|---|---|---|
| `eap.iam.identity` | `IdentityProvider` | `apikey` (tenant and personal `pos_` keys, and the Architect run token), `cfaccess` (the Cloudflare Access JWT, e-mail as the user), `devloopback` (the dev bearer, loopback only) |
| `eap.iam.policy` | `PolicyEngine` | `openaccess` (transitional: every authenticated user is admin), `openfga` (installed, **not selected**: see "OpenFGA" below) |
| `eap.iam.membership` | `MembershipSource` | none |
| `eap.db.migrations` | Alembic script location | none yet (the runner exists: `eap-db plugins upgrade`) |

## Configuration

- `EAP_AUTH_MODE` is the master switch: `dev`, `production` or the deprecated alias `api_key`. Unset or anything else
  refuses to start.
- `EAP_IAM_IDENTITY`: ordered, comma-separated identity provider names. First match wins; a credential that a provider
  recognises and finds invalid answers 401 and is never retried by a later provider. A route that accepts only API
  keys skips user providers. An unknown or duplicate name refuses to start.
- `EAP_IAM_POLICY`: exactly one policy engine. `EAP_IAM_MEMBERSHIP`: at most one membership source.
- `dev` defaults to `apikey,devloopback` and `openaccess` when those are installed. `production` needs the lists
  (at least one identity provider and exactly one engine) and refuses a `transitional` engine unless
  `EAP_IAM_ALLOW_TRANSITIONAL=1`. `api_key` means `production` with `apikey` and the transitional engine, serving only
  the AG-UI routes to keys.
- `EAP_IAM_IGNORE_PLUGINS` hides installed plug-ins (the API suite runs with every plug-in absent and the fakes in place
  under `EAP_IAM_TEST_FAKES=1`).

## The dev bearer

`Authorization: Bearer dev:<tenant>:<user>` is accepted only when the TCP peer is `127.0.0.1` or `::1` and the request
carries no `Cf-Access-Jwt-Assertion`, `X-Forwarded-For`, `CF-Connecting-IP`, `Forwarded` or `X-Real-IP` header. Run the
API with `--no-proxy-headers` (the image does); the API refuses to start with `FORWARDED_ALLOW_IPS=*`.

## The Cloudflare Access JWT (`cfaccess`)

On the public host Cloudflare Access puts a signed JWT in `Cf-Access-Jwt-Assertion`. `eap-iam-cfaccess` verifies it on
every request (RS256 only, the team JWKS cached with a bounded refresh on an unknown `kid`, `iss`, `aud`, `exp`, `nbf`,
a required `email`) and the principal is `user:<e-mail>` in the one tenant `EAP_CF_ACCESS_TENANT`. A request with no
such header is left to the next provider; one with an invalid header answers 401. A bare e-mail header is never read.
Settings: `EAP_CF_ACCESS_TEAM_DOMAIN`, `EAP_CF_ACCESS_AUD`, `EAP_CF_ACCESS_TENANT` (all required when listed). The
deployed order is `zitadel,apikey,cfaccess,devloopback`. See `plugins/iam-cfaccess/README.md`.

## ZITADEL tokens, members and Studio sign-in (`zitadel`)

`eap-iam-zitadel` (an enterprise plug-in, `plugins/iam-zitadel/README.md`) verifies ZITADEL access tokens: a bearer
that is shaped like a JWT is verified on every request (RS256 only, the instance JWKS with a bounded refresh on an
unknown `kid`, `iss` exactly, the project id in `aud`, `exp`, `nbf`) and any failure is a 401 that never falls through to
a later provider; an organisation that is not in `EAP_ZITADEL_ORG_TENANT_MAP` is a 403. The principal's subject is the
stable `zitadel:<sub>` (e-mails change; P2 keys OpenFGA tuples by the subject), its e-mail is a separate attribute
(`principal.extra["email"]`, `me.email` in capabilities), the tenant of its organisation, and its project roles; a
machine user is `kind="service"`. The same plug-in is the membership source (`EAP_IAM_MEMBERSHIP=zitadel`):
`GET/POST /api/v1/iam/members`, `PATCH/DELETE /api/v1/iam/members/{id}` list, invite, change a role and remove, admin
only, with the change made in ZITADEL; the last admin can never be demoted or removed. The capabilities answer lists the
membership source only when it is configured (the machine user's key), and Studio shows the Members tab only then.
Studio signs in with OIDC code + PKCE when `EAP_STUDIO_AUTH=zitadel` (code default `access`; this deployment's configmap sets `zitadel`; tokens in memory only, one silent
renew on a 401, then the login). Roles are recorded but not enforced until a policy engine replaces `openaccess`.
Setup is in `deploy/README.md`, "ZITADEL bootstrap and sign-in".

### Subjects in recorded data (legacy mapping)

`created_by`, `published_by`, `decided_by` and `audit_log.actor_subject` record the principal's subject. Rows written
through `cfaccess` (and before) hold the **e-mail**; rows written through `zitadel` hold **`zitadel:<sub>`**. They are
not rewritten and the two forms of one person are not merged in the data. Audit rows from a ZITADEL actor also carry
`detail.actor_email` (the e-mail at that time), and Studio shows an e-mail for both forms: the audit view reads
`actor_email`, the Members list introduces user ids, and the caller's own capabilities introduce the caller; an id none of
these knows (an approver who has left, read by a non-admin) shows as `ZITADEL user <first 8 characters>…`. For P2 the
OpenFGA backfill gives the owner's account both subjects (`zitadel:<sub>` and the legacy e-mail) so old rows keep their
owner.

## OpenFGA (`openfga`)

`eap-iam-openfga` (`plugins/iam-openfga`, an enterprise plug-in) is the target policy engine (design 5.4 to 5.6). **It is
built but not switched on:** `EAP_IAM_POLICY` stays `openaccess`, no service calls its methods yet (P2b wires `require` and
`filter_ids` service by service, P2c adds Studio's Access tab and flips the setting). It is installed and discoverable, so
`EAP_IAM_POLICY=openfga` is accepted once it is configured.

**The model** is `plugins/iam-openfga/model/durable.fga`, tested by `durable.fga.yaml` (the section 2.2 matrix, a runner
grant on one workflow, an approver grant, `usable_by` for editors on one secret, a run inheriting `can_approve`, tenant
isolation, list-objects). Run it with `fga model test --tests plugins/iam-openfga/model/durable.fga.yaml`; CI's
`deploy-checks` job does, with the CLI pinned in `ci.yml` (`FGA_CLI_VERSION`, installed by `go install`, which the Go checksum
database verifies). Differences from the design's DSL, all additive: the `tenant` type has `can_read`, `can_create_<kind>`
(editors for agents, workflows, knowledge bases, schedules; admins for secrets, MCP servers and API keys),
`can_manage_members`, `can_read_audit` and `can_manage_settings`, because those matrix rows are about a resource that does
not exist yet; and an `approver` grant also gives `read` (the table in 2.3 says so). Object ids carry the tenant:
`agent:<tenant>/<id>`, so a secret or MCP server named the same in two tenants never shares a tuple, and a list answer is
filtered by the tenant prefix as a second line of defence. A secret is identified by its name, everything else by its id.

**Who is asked.** Tenant roles (`admin`, `editor`, `viewer`) travel with every request as contextual tuples
(`user:<subject> editor tenant:<T>`), so a role change needs no sync and nothing about a role is stored. The OpenFGA user is
the principal's `subject` (`zitadel:<sub>`). An API key abstains (its scopes and allowlists, `eap_iam.permits`, decide, as
with `openaccess`); a `run` principal is checked as the principal it acts `on_behalf_of`. The action is the verb (`publish`
or `agent.publish`) and answers the model relation `can_<verb>`; an action or kind the model does not have is **denied
without a call**, as is a resource of another tenant. A collection (`id` None) answers only `create`, against the tenant.

**Cost.** `check`: one call. `check_many`: one BatchCheck (OpenFGA's limit is 50 checks per request); above that, one
ListObjects. `filter_ids`: one Check (is the caller a tenant admin: then `All`) plus one ListObjects. Nothing is cached
across requests, so a revoked right stops at once. When ListObjects returns as many objects as the configured limit it may
be truncated: the plug-in logs a warning and, if the caller passed `candidate_ids` (the page), answers exactly with
BatchChecks of the page. Without candidates it returns the part it got, which can hide rows but never shows one (the design's
"All plus a per-page check" needs a third filter result in core; that is P2b's to add if a list really exceeds the limit).

**Failure behaviour.** Fail closed. Any engine failure (unreachable, timeout, 4xx or 5xx, an answer that is not JSON, a
batch item with no decision) raises `eap_iam.PolicyUnavailable`, which the entry point answers as **503**; nothing is
allowed and nothing is cached. The message names the call and the HTTP status (and OpenFGA's error code when it is a plain
identifier), never a response body, a tuple or the preshared key. A run that already started continues on its stored
permission set (the engine is not on the tool-call path). A grant the model cannot hold (`usable_by`, a relation the kind
lacks, a wildcard subject) is a `GrantError` and writes nothing.

### Settings

| Variable | Meaning |
|---|---|
| `EAP_OPENFGA_URL` | the server, e.g. `http://openfga.iam.svc.cluster.local:8080` (required) |
| `EAP_OPENFGA_STORE_ID` | the store (required; printed by the bootstrap) |
| `EAP_OPENFGA_MODEL_ID` | pin the model (optional; without it the store's latest is used; pin it in production) |
| `EAP_OPENFGA_PRESHARED_KEY` | the preshared key (falls back to `OPENFGA_PRESHARED_KEY`, the deploy Secret's field); a header, never logged |
| `EAP_OPENFGA_TIMEOUT_SECONDS` | per request, default 3 |
| `EAP_OPENFGA_LIST_LIMIT` | what ListObjects may return, default 1000 (the server's `OPENFGA_LIST_OBJECTS_MAX_RESULTS`); keep them equal |
| `EAP_OPENFGA_BATCH_SIZE` | checks per BatchCheck, default 50, at most 100 (the server's limit) |

Selecting `openfga` without the URL and the store id refuses to start.

### Bootstrap

`python -m eap_iam_openfga.bootstrap [--url URL] [--store-name durable | --store-id ID] [--model-file F] [--key-file F] [--dry-run]`
finds the store by name (or creates it), writes the model **only when it changed** (compared on meaning, not bytes), and
prints the store id and model id, which are not secrets, as the two settings to deploy. A second run changes nothing; two
stores with one name stop the run (pass `--store-id`). `--dry-run` reads and plans and writes nothing, not even the store.
The model is transformed by the plug-in's own DSL reader (`eap_iam_openfga.dsl`, the subset the model uses; CI checks it
against `fga model transform`), so the API image needs no CLI.

### Reconcile and backfill

Both compute what the platform's data implies, read through `eap_run_store.iam_catalog.RunStoreCatalog` (read only, no SQL in
the plug-in), diff it against OpenFGA's tuples and, **only with `--apply`**, write the difference. They are idempotent and
print a summary of counts by tuple type (`link:agent`, `owner:secret`), never an id or a subject.

- `python -m eap_iam_openfga.reconcile [--tenant T] [--apply] [--prune]` repairs the **links** `tenant:<T> tenant <kind>:<T>/<id>`
  (a resource without its link is invisible: fail closed). It never adds owners, never touches a grant (`runner`,
  `approver`, `editor`, `usable_by`) and never re-adds an owner an admin revoked. `--prune` (with `--apply`) deletes the
  tuples of objects the catalog no longer lists. Runs are not in the catalog; they are registered when they start.
- `python -m eap_iam_openfga.backfill [--tenant T] [--owner SUBJECT]... [--owner-email E]... [--apply]` is the one-off for tenant
  `studio`: the links, plus `owner` on every resource for each owner account, plus the creator recorded on the row when it
  is a real principal (an e-mail or `zitadel:<sub>`). Legacy creators (`operator`, `studio:operator`, `dev:<user>`) never become
  principals; the summary counts them. `--owner-email` resolves an account through `EAP_IAM_MEMBERSHIP` to **both** of its
  subjects (`zitadel:<id>` and the e-mail), as "Subjects in recorded data" asks.

Both read `EAP_DATABASE_URL` (the same read role the API uses) and the `EAP_OPENFGA_*` settings. Tenant roles are not
backfilled: they are contextual (a stored role could outlive its removal in ZITADEL). The plug-in owns no platform table, so it
has no Alembic branch; tuples live in OpenFGA's own database (backed up nightly, `deploy/README.md`).

### Tests

`uv run pytest -p no:eap_db -q plugins/iam-openfga` runs the unit tests (a fake server over `httpx2.MockTransport`: check, batch,
list, the over-limit fallback, fail-closed, no secret in an error, the reconcile diff, bootstrap idempotency, the DSL). The
integration tests (`test_integration.py`: the matrix with contextual roles, grants, ListObjects and BatchCheck, the reconcile,
the bootstrap, all against the pinned server image) are skipped unless `EAP_TEST_OPENFGA_URL` points at a server or
`EAP_TEST_OPENFGA_DOCKER=1` lets the test start a throw-away container (memory datastore, removed afterwards). CI Heavy sets
the second; a skipped run is not a pass.

## Personal API keys

A signed-in person can make API keys of their own (Studio's Profile page, `POST /api/v1/iam/me/api-keys`); tenant keys
(created by admins, owner none) are unchanged.

- **Ownership.** `api_keys.owner_subject` (for example `zitadel:<sub>`) and `owner_email` (the e-mail at creation, for
  display) name the person; both are NULL on a tenant key and on every key that existed before migration 0039.
- **Routes** (people only: `zitadel` or `cfaccess`; a call made with an API key or the loopback dev bearer is 403
  `not_a_person` with a message saying so): `POST /api/v1/iam/me/api-keys` (name, scopes, optional `expires_at`; 201 with
  the `key` once, `Cache-Control: no-store`), `GET` (own keys: prefix, scopes, status, created, last used, expiry; never
  the secret), `DELETE /{id}` (revoke, idempotent; another person's key, a tenant key and an unknown id are one 404).
  At most 20 active personal keys per person, still inside the tenant's 100.
- **Scope cap.** A key may carry only scopes the owner's roles allow (`eap_iam.ROLE_SCOPES`): viewer `mcp` (read);
  editor `agui:run`, `mcp`, `manage`; admin also `otlp`. A person who is not a member of the tenant gets 403. With no
  membership source configured nothing records a person's roles; the owner counts as admin, as every user does under the
  transitional `openaccess` engine, and the stored scopes stand at use time.
- **Expiry.** Always set: default 90 days, at most 365 days from now.
- **Using a key.** The `apikey` plug-in resolves it as a key principal (`kind="api_key"`, `subject` the key id, so
  conversations and the key-active check behave as before) with `on_behalf_of` the owner and `extra["owner"]`. Its scopes
  are the key's scopes intersected with `scopes_for_roles(owner's CURRENT roles)`, and its roles are those roles. The roles
  come from the membership source (the factory context carries it, so the plug-in imports no other plug-in) and are cached
  for 60 seconds per owner: **a removed member's keys stop working, and a demoted member's scopes shrink, within 60
  seconds**; a membership source that fails answers 503 (fail closed). Revoking a key is immediate.
- **Attribution.** Audit rows and runs started with a personal key name the owner as the actor (`actor_kind` `user`,
  `actor_subject` the owner); audit `detail.via_api_key` carries the key id, the non-secret part of the prefix.
- **Admins** still list and revoke every key of the tenant in the API keys section; the listing now has `owner_subject`
  and `owner_email`.
- **The secret** is shown once, stored only as a salted hash, never logged, and absent from every error text.

## The actor and the audit log

`created_by`, `published_by` and `decided_by` carry the principal's subject (the e-mail of an Access user); a run records
`principal`, `on_behalf_of` and `started_by_kind` (NULL on runs from before: legacy). Every mutating API call and every
mutating MCP control tool call writes one `audit_log` row (actor, action, resource, outcome, request id; never a body or
a credential). `GET /api/v1/audit` lists them (filters `actor`, `action`, `resource_kind`, `resource_id`, `outcome`,
`since`, `until`; cursor). `EAP_AUDIT_RETENTION_DAYS` (default 400, `off` keeps all) bounds the table; the worker prunes
daily. The implied-permissions report is `GET /api/v1/iam/implied-permissions`: read only, nothing is enforced.

## Roles and your own profile

ZITADEL projects carry three roles: `admin` (manages the tenant: members, secrets, API keys, the audit log, and
everything an editor does), `editor` (builds and runs agents, workflows and knowledge bases) and `viewer` (read-only).
They are recorded but not enforced until a policy engine replaces `openaccess`. Studio's account menu (sidebar footer)
and Settings, Profile show the caller's identity, roles, sign-in method and, for ZITADEL, the session expiry and links to
the issuer's self-service console (`<issuer>/ui/console/users/me`: password, second factor, passkeys). Profile's recent
activity reads `GET /api/v1/audit?actor=me`: the caller's own rows, open to every signed-in person (not only an admin),
still tenant-scoped, and not itself recorded as `audit.read`.

## `GET /api/v1/iam/capabilities`

The active plug-ins, the policy engine's features, the caller (`me`) and the `kind.action` strings the caller may do.
Names and flags only; people routes only (a key gets 401).
