# Identity and access (IAM)

The API authenticates through plug-ins. Core ships only the interfaces (`packages/iam`, package `eap_iam`); every
implementation is a separate distribution found through entry points. The design is
an internal design note; this page is what runs today (phases P0a and P0b).

| Entry-point group | Interface | Plug-ins today |
|---|---|---|
| `eap.iam.identity` | `IdentityProvider` | `apikey` (tenant and personal `pos_` keys, and the Architect run token), `cfaccess` (the Cloudflare Access JWT, e-mail as the user), `devloopback` (the dev bearer, loopback only), `localadmin` (the one built-in admin account: [admin-account.md](admin-account.md)), `webhook` (an inbound webhook delivery, authenticated by its HMAC signature) |
| `eap.iam.policy` | `PolicyEngine` | `openaccess` (transitional: every authenticated user is admin), `openfga` (per-user authorization: see "OpenFGA" below) |
| `eap.iam.membership` | `MembershipSource` | `zitadel`, `localadmin` |
| `eap.db.migrations` | Alembic script location | `localadmin` (run by `eap-db plugins upgrade`) |

**The default policy is `openaccess`** (`deploy/k8s/configmap.yaml`): every signed-in user is an admin, and neither roles
nor per-resource grants are enforced. That suits a trial only. To enforce roles (admin, editor, viewer) and per-resource
grants, run your own OpenFGA server and follow [Switching to OpenFGA](#switching-to-openfga).

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

## The built-in admin account (`localadmin`) and the Studio sign-in modes

For a deployment without an identity provider, `eap-iam-localadmin` gives exactly one admin account: first-run setup with
a one-time code from the API log, password sign-in, throttling, sessions and a reset command. It is an identity provider
and a membership source, and it implements `eap_iam.PasswordSignInProvider`, which the routes under
`/api/v1/auth/local` (`routes/local_auth.py`, `services/local_auth.py`) call. Studio has three explicit sign-in modes,
`EAP_STUDIO_AUTH=local`, `zitadel` or `access` (Cloudflare Access in front, with `cfaccess`); a mode that cannot work
shows "No sign-in method is configured". All of it, side by side: [admin-account.md](admin-account.md).

## The dev bearer

`Authorization: Bearer dev:<tenant>:<user>` is accepted only when the TCP peer is `127.0.0.1` or `::1` and the request
carries no `Cf-Access-Jwt-Assertion`, `X-Forwarded-For`, `CF-Connecting-IP`, `Forwarded` or `X-Real-IP` header. Run the
API with `--no-proxy-headers` (the image does); the API refuses to start with `FORWARDED_ALLOW_IPS=*`.

In development, `scripts/dev-up.sh` gives Studio `Bearer dev:acme:alice` for its loopback page
(`EAP_STUDIO_LOOPBACK_DEV_BEARER`) when `EAP_AUTH_MODE` is `dev`, `EAP_STUDIO_AUTH` is not set and `EAP_IAM_IDENTITY`
(if set) lists `devloopback`. Set the variable to another bearer, or to nothing to turn this off.

## The Cloudflare Access JWT (`cfaccess`): single-tenant only

A supported option for a single-tenant installation, with Studio in the `access` mode
([the three Studio sign-in modes](admin-account.md#the-three-studio-sign-in-modes)); the default chain does not list it.
Access has no tenant concept, so `eap-iam-cfaccess` makes every person Access lets
through a user of one tenant (`EAP_CF_ACCESS_TENANT`), named by e-mail. With a second ZITADEL organisation that is a way
into the first tenant, so the plug-in **refuses to start** when `EAP_ZITADEL_ORG_TENANT_MAP` names a tenant other than
`EAP_CF_ACCESS_TENANT` (or cannot be read), or when `EAP_TENANT_REGISTRY=enforce`. The deployed order is
`zitadel,apikey,devloopback`.

For a single-tenant installation that still lists it: it verifies the `Cf-Access-Jwt-Assertion` JWT on every request
(RS256 only, the team JWKS cached with a bounded refresh on an unknown `kid`, `iss`, `aud`, `exp`, `nbf`, a required
`email`) and the principal is `user:<e-mail>`. A request with no such header is left to the next provider; one with an
invalid header answers 401. A bare e-mail header is never read. Settings: `EAP_CF_ACCESS_TEAM_DOMAIN`,
`EAP_CF_ACCESS_AUD`, `EAP_CF_ACCESS_TENANT` (all required when listed). See `plugins/iam-cfaccess/README.md`.

**Removing it** leaves the rights it created under e-mail subjects. With OpenFGA, `python -m eap_iam_openfga.email_subjects`
(dry run; `--apply` deletes) removes every tuple whose subject is an e-mail address, and stops if a resource would be left
without an owner. The backfill and reconcile write e-mail subjects only while `cfaccess` is listed.

## ZITADEL tokens, members and Studio sign-in (`zitadel`)

`eap-iam-zitadel` (an enterprise plug-in, `plugins/iam-zitadel/README.md`) verifies ZITADEL access tokens: a bearer
that is shaped like a JWT is verified on every request (RS256 only, the instance JWKS with a bounded refresh on an
unknown `kid`, `iss` exactly, the project id in `aud`, `exp`, `nbf`) and any failure is a 401 that never falls through to
a later provider; an organisation that is not in `EAP_ZITADEL_ORG_TENANT_MAP` is a 403. The principal's subject is the
stable `zitadel:<sub>` (e-mails change; P2 keys OpenFGA tuples by the subject), its e-mail is a separate attribute
(`principal.extra["email"]`, `me.email` in capabilities), the tenant of its organisation, and its project roles; a
machine user is `kind="service"`. **Only roles granted by the user's own organisation count**: the claim is
`{role: {granting organisation id: domain}}`, and ZITADEL lets one organisation grant a role to another's user, so a
role whose map does not name the token's resource-owner organisation is ignored (one WARNING per subject). The same plug-in is the membership source (`EAP_IAM_MEMBERSHIP=zitadel`):
`GET/POST /api/v1/iam/members`, `PATCH/DELETE /api/v1/iam/members/{id}` list, invite, change a role and remove, admin
only, with the change made in ZITADEL; the last admin can never be demoted or removed. Where the platform's machine
user may grant roles but not create users (the operator organisation), the list answers `can_create_users: false`, an
invitation of an address with no account is 409
`no_user_creation`, and Studio says to create the person in the ZITADEL console first. The capabilities answer lists the
membership source only when it is configured (the machine user's key), and Studio shows the Members tab only then.
Studio signs in with OIDC code + PKCE when `EAP_STUDIO_AUTH=zitadel` (the three modes: [admin-account.md](admin-account.md); tokens in memory only, one silent
renew on a 401, then the login). Roles are recorded but not enforced until a policy engine replaces `openaccess`.
Setup is in `plugins/iam-zitadel/README.md`.

### Subjects in recorded data (legacy mapping)

`created_by`, `published_by`, `decided_by` and `audit_log.actor_subject` record the principal's subject. Rows written
through `cfaccess` (and before) hold the **e-mail**; rows written through `zitadel` hold **`zitadel:<sub>`**. They are
not rewritten and the two forms of one person are not merged in the data. Audit rows from a ZITADEL actor also carry
`detail.actor_email` (the e-mail at that time), and Studio shows an e-mail for both forms: the audit view reads
`actor_email`, the Members list introduces user ids, and the caller's own capabilities introduce the caller; an id none of
these knows (an approver who has left, read by a non-admin) shows as `ZITADEL user <first 8 characters>…`. For P2 the
OpenFGA backfill gives the owner's account both subjects (`zitadel:<sub>` and the legacy e-mail) so old rows keep their
owner.

## Per-resource authorisation in the services (P2b wave 1)

A resource service is built per request with an `Access` (`eap_api/services/access.py`: the caller's core principal plus
`AuthzService`). It calls `require(action, kind, id)` before it acts: **404** (the unknown-resource body) when the caller
may not read the resource, **403** `forbidden` when it can read but not act, **503** when the engine cannot answer.
Lists call `filter_ids` once (a restricted list is a SQL `id IN (...)` through the store); creates register the resource
(tenant link + owner) after the commit and before the response. `Access` is `None` for internal callers (a script, the
Architect's drafter runs); the HTTP, MCP and AG-UI entry points always pass one. MCP tools get theirs from `call_service`.

| Endpoint | Action on |
|---|---|
| `POST /agents`, `POST /workflows` | `create` (tenant); registered |
| `GET /agents`, `/workflows` | `read` list filter |
| `GET` an agent / workflow, its versions, compare | `read` |
| `PATCH` an agent / workflow draft | `edit` |
| publish a version, restore a version | `publish` |
| archive, unarchive | `archive` |
| `POST /agents/{id}/runs`, conversations and turns, AG-UI run | `run` on the agent |
| `POST /workflows/{id}/runs` (also rerun), AG-UI run | `run` on the workflow |
| `GET /runs`, a run, its events, stream | `read` on the run's agent or workflow (list: two filters) |
| `POST /runs/{id}/cancel` | `run` on the workflow |
| approve / reject / resume a step, approve / deny a tool call | `approve` on the run's workflow, else its agent |
| `GET /approvals`, one approval | `read` on the approval's workflow, else its agent |
| `POST /workflows/{id}/schedules` | `run` on the workflow, `create` schedule; registered |
| schedule list / get / `PATCH` / `DELETE` | `read` (filter) / `read` / `edit` / `archive` (unregistered) |

A run is checked through its parent (the model's `run` type inherits `can_read`, `can_approve` and `can_cancel` from its
agent or workflow), so no tuple is written per run.

## Per-resource authorisation, wave 2 (P2b)

The remaining services take the same `Access` and follow the same rules (404 unreadable, 403 readable but not allowed, one
engine call for a list, register on create and unregister on delete, 503 when the engine cannot answer). A document, source
or sync is checked through its knowledge base, a suite or eval run through the agent or workflow it targets, a trace, span or
log through the run it belongs to, so no tuple is written for them.

| Domain | Endpoints | Action on |
|---|---|---|
| Knowledge bases | `POST /knowledge-bases`; list; get, documents (list / get) and sources (list / get / syncs) | `create` (tenant, registered); `read` list filter; `read` |
| | `PATCH` a base, put / delete / re-ingest / re-extract a document, reindex, sources (create / patch / sync) | `edit` |
| | the search tester (`POST .../search`, a `knowledge.search` run) | `run` |
| | `DELETE` a base | `archive` (unregistered) |
| Secrets (named, not numbered) | list, put a new one; put an existing one; delete | `read` filter; `create` (admin, registered); `edit`; `archive` (unregistered) |
| MCP servers | create, install from the catalog; list, get, runtime state; patch, runtime start / stop / restart / logs, test; delete; call a tool | `create` (admin, registered); `read`; `edit`; `archive`; `run` (new in the model) |
| Source connections | list, get; create, patch, delete, check | tenant `read`; tenant `manage_connections` (admin, new) |
| MCP catalog, tool registry, processor catalog and tester, embedding models, source adapters | read | tenant `read`; a direct tool run is tenant `run` (editors) |
| Attachments | upload, discard; download | tenant `write` (editors, new); tenant `read` |
| Memory, feedback on a message | read; change notes and observations; resource notes, forget a resource of one agent | `read`, `run`, `edit` on the conversation's agent; forgetting across every agent is tenant `manage_settings` |
| Evals | suites, cases, eval runs, compare | `read` / `edit` / `run` on the target agent or workflow; the tenant overviews are filtered to the readable targets (two engine calls) |
| Scorers, scorer suggestions | library; attach; score a run; suggest rules | tenant `read` / `write`; `edit` on the agent; `run` on the run's agent or workflow; `edit` on the target |
| Traces, spans, runs histogram, processor results | read, feedback | `read` on the run's agent or workflow (a list is restricted in SQL); external (OTLP) traces: tenant `read` |
| Metrics, logs | one agent's or one run's; tenant-wide | `read` on the agent or run; tenant `read_telemetry` (admin, new) |
| Tenant API keys | create; list; revoke | `create` (admin, registered); `read` list filter (owner or admin); `revoke` |
| Personal API keys | create, list, revoke | the caller must be a member (tenant `read`), else 403 `not_a_member`; they stay person-only and own-keys-only |
| OTLP ingest | `POST /otlp/v1/traces` | an API key's `otlp` scope (the engine abstains, as for every key); a person (the dev bearer) needs tenant `otlp` (admin, new) |
| Workflow explanations | read, generate | `read` on the workflow (the matrix has no separate right; generation costs one model call) |
| Architect | state, options; draft; save; seed; set the model | tenant `read`; `create` agent / workflow; `create` (new, registered) or `edit` (existing) and `publish` when publishing; `create` agent; tenant `manage_settings` |
| AG-UI | resume decisions (approve / reject) | `approve` on the agent or workflow, on top of `run`; attachments: tenant `write` / `read` |

**The Architect acts for a person.** The run token carries no claim; the API reads who started the Architect's run from the
run itself (`principal`, `on_behalf_of`, recorded when the turn started), so the token cannot be made to name someone else,
and asks the membership source for that person's current roles (cached 60 seconds; no source, no answer or no recorded
person: no roles, so `openfga` grants nothing by role). Every run a request starts records that actor, conversation turns
included. The
MCP-control principal of an Architect run is `kind="run"` with `on_behalf_of` set to that person, the engine decides for that
person, and a tool that changes things (`manage`) is refused for a turn whose person cannot `write` (a viewer's Architect gets the
read tools only; the next section). `grant_use` on the refs of a published spec is P3.

### Viewers and the Architect

A tenant `viewer` has no `run` on any agent. One narrow exception, in the service layer
(`services/converse.py`; the OpenFGA model is unchanged, so there is nothing to bootstrap or backfill):

| | Viewer of the built-in Architect |
|---|---|
| Start a conversation, send a turn (`POST .../agents/{id}/conversations`, `POST /conversations/{id}/runs`, `POST /ag-ui/agents/{id}`) | allowed, needing `read` on the tenant and on the agent, in conversations whose every turn they started |
| A turn in a conversation someone else wrote in (they still read it) | 403: their text must not enter a thread whose next turn runs with someone else's write tools |
| Any other `run`: a run outside a conversation, rename, clone or delete a conversation, stop a run, notes and memory, eval runs | 403 as before |
| The Architect's helpers (drafters, scorer suggester, workflow explainer) and every other agent | 403 as before |
| Approve or reject a tool call | 403 as before |
| Attach a file | 403 as before (tenant `write`) |

It applies to people only: an API key, a service and a run are decided as before.

**The turn is read-only, decided by the same rights.** A run a person starts records their roles (`runs.actor_roles`;
child runs copy them). At the start of a run of the Architect the worker asks the policy engine whether that person may
`write` the tenant (the question the control endpoint asks of the run token); if not, or if the engine cannot answer:

- the tool list is an allowlist: the `platform.*` reads, `architect.list_*` and `architect.get_*`
  (`ARCHITECT_READ_TOOL_IDS`), the `read` control tools and knowledge retrieval. No draft, publish, restore, archive,
  schedule or eval write tool, no `manage` control tool, MCP server tool, agent tool, client tool, `secrets.request` or
  other tool an editor added to the Architect, and no memory-note tool;
- a write tool called anyway is unknown to the run; the tool bodies and the control bridge refuse (`read_only`), and the
  control endpoint refuses `manage` for the run token;
- the instructions say the person is read-only, so the Architect explains and asks them to turn to an editor.

An editor's or admin's turn is unchanged, in a conversation a viewer started too. The roles are those held when the turn
started; the control endpoint also checks the current ones. Under `openaccess` every person may write, so nothing is
read-only. The worker needs the same `EAP_IAM_POLICY` and OpenFGA settings as the API (it already has them for grants).

**No request path without an Access.** `apps/api/api_tests/test_iam_access_guard.py` walks the dependency tree of every route
and proves each per-resource service is built with an `Access`; every AG-UI handler asks `require_access`; every MCP control tool
builds its services through `call_service`, which binds the caller's access. A service that is not per-resource must be listed
there with its reason (the list is eight lines long).

Left for later: the capability registry's dynamic entries (`EntryPolicy`), a per-run actor check for tool and
knowledge-base runs in lists, and the access check for the personal keys' scopes (still the owner's roles).

## The Access tab and explain

**API** (`routes/resource_access.py`; `kind` is `agent`, `workflow`, `knowledge_base`, `secret` (id: its name) or
`mcp_server`):

| Endpoint | Who | What |
|---|---|---|
| `GET /api/v1/iam/access/{kind}/{id}` | whoever may read the resource (404 otherwise) | `supported`, `can_manage`, the model's `relations` for the kind with the roles that may hold each, the `grants` (`relation`, `subject_type` `user` / `role`, `subject`) and, for a secret or server, `used_by` (the agents and workflows granted it, those the caller may read) |
| `POST .../grants` `{relation, member_id}` or `{relation, role}` | an admin, a person | grant to a member of the tenant (checked against the membership source) or to a role; idempotent; 422 for what the model cannot hold or an unknown member |
| `DELETE .../grants?relation=&subject_type=&subject=` | an admin, a person | remove a grant as the list names it (also one of a person who has left) |
| `GET /api/v1/iam/explain?kind=&id=&action=` (repeat `action`; no `id`: create, or `kind=tenant`) | the caller, about themselves | per action `allowed`, a `reason` (`allowed`, `not_a_member`, `no_permission`, `engine_unavailable`) and `needed` (the roles and grants the model accepts); never who else holds anything, and the same answer for a resource that is not there |

Relations are exactly the model's: `owner` everywhere (people only); `editor`, `runner`, `approver`, `viewer` on agents and
workflows; `editor`, `runner`, `viewer` on knowledge bases; secrets and MCP servers have `owner` only (what agents may use is
the separate grant below). `editor`, `runner`, `approver` and `viewer` also take a **role**: the tuple
`tenant:<T>#editor runner workflow:<T>/<id>` reaches everyone with that role *or a higher one*, on that resource only.
Grants only add rights. The API lets admins change them (the model's `can_manage_access` also names owners; the API is
stricter). Every change writes an audit row (`grant_access`, `revoke_access`; `detail.name` =
`<relation> <user|role>:<subject>`). With `openaccess` the list says `supported: false` and a write answers 503
`grants_unavailable`.

A 403 from the policy engine carries `refused: {action, kind, id}` (the caller's own request) beside its usual body.

**Studio**, only when the capabilities answer has the `grants` / `explain` features (hidden under `openaccess`): an
**Access** tab on agents, workflows, knowledge bases and MCP servers and, for an admin, an Access button on each secret
(admins edit, everyone else reads); the owner and, for actions the caller is refused, "why can't I?" with what to ask an
admin for. Lists show an owner only where the API already returns one (API keys).

**What Studio offers.** A control the caller cannot use is not shown: create buttons (and their `c` shortcut and palette
command), edit, save, publish, restore, archive and delete, secret, MCP server, source connection and API key changes,
approve and deny, schedules, eval and scorer changes, run and Playground controls (a read-only note replaces the chat
composer), and the Audit and Members tabs. Studio asks `GET /api/v1/iam/explain` about the caller (`lib/permissions.tsx`),
so no role name is decided in the browser; nothing is offered while the answer is loading. When it cannot be read, or
the engine cannot explain (`openaccess`), the control is offered and the server refuses. The one exception is the
built-in Architect's chat, which Studio never closes to a signed-in person (the server limits a viewer to read tools).

**Refusals.** A 403 with `refused` reads as a sentence everywhere (`lib/access.ts`), for example "Your role (viewer)
can't run this agent. Ask an admin for the editor role or a runner grant on it."; the dismissible notice says the same,
also while the engine cannot answer.

## OpenFGA (`openfga`)

`eap-iam-openfga` (`plugins/iam-openfga`, an enterprise plug-in) is the target policy engine (design 5.4 to 5.6).
**It is not selected by default:** `EAP_IAM_POLICY` stays `openaccess` (every signed-in user is an admin; a trial only)
until you run your own OpenFGA and follow [Switching to OpenFGA](#switching-to-openfga) below.
P2b wired `require`, `filter_ids` and register / unregister into every resource service (waves 1 and 2, above);
P2c deploys the `EAP_OPENFGA_*` settings to the API and the worker. The plug-in is installed in their image, so
`EAP_IAM_POLICY=openfga` is accepted once the store id is set.

**The model** is `plugins/iam-openfga/model/durable.fga`, tested by `durable.fga.yaml` (the section 2.2 matrix, a runner
grant on one workflow, an approver grant, agent-bound grants (`granted_to` / `can_use_as_agent`, see "Agent and workflow grants"), a run inheriting `can_approve`, tenant
isolation, list-objects). Run it with `fga model test --tests plugins/iam-openfga/model/durable.fga.yaml`; CI's
`deploy-checks` job does, with the CLI pinned in `ci.yml` (`FGA_CLI_VERSION`, installed by `go install`, which the Go checksum
database verifies). Differences from the design's DSL, all additive: the `tenant` type has `can_read`, `can_create_<kind>`
(editors for agents, workflows, knowledge bases, schedules; admins for secrets, MCP servers and API keys),
`can_manage_members`, `can_read_audit` and `can_manage_settings`, because those matrix rows are about a resource that does
not exist yet; wave 2 adds the tenant relations `can_write` and `can_run` (editors: attachments, the scorer library and direct
tool runs), `can_manage_connections`, `can_read_telemetry` and `can_otlp` (admins: source connections, tenant-wide metrics and
logs, OTLP ingest by a person) and `can_run` on an MCP server (owner, or editors: calling a tool); and an `approver` grant also gives `read` (the table in 2.3 says so). Object ids carry the tenant:
`agent:<tenant>/<id>`, so a secret or MCP server named the same in two tenants never shares a tuple, and a list answer is
filtered by the tenant prefix as a second line of defence. A secret is identified by its name, everything else by its id.

**Who is asked.** Tenant roles (`admin`, `editor`, `viewer`) travel with every request as contextual tuples
(`user:<encoded subject> editor tenant:<T>`), so a role change needs no sync and nothing about a role is stored. The OpenFGA user is
the principal's `subject` (`zitadel:<sub>`). An API key abstains (its scopes and allowlists, `eap_iam.permits`, decide, as
with `openaccess`); a `run` principal is checked as the principal it acts `on_behalf_of`. The action is the verb (`publish`
or `agent.publish`) and answers the model relation `can_<verb>`; an action or kind the model does not have is **denied
without a call**, as is a resource of another tenant. A collection (`id` None) answers only `create`, against the tenant.

### Ids in OpenFGA

OpenFGA allows one `:` in `type:id`, and platform subjects have their own (`zitadel:<sub>`, `apikey:<id>`, `run:<id>`).
The plug-in therefore percent-encodes **every subject id and resource id**: `%` becomes `%25`, then `:` becomes `%3A`;
nothing else changes. `zitadel:123` is the user `user:zitadel%3A123`; the secret `vault:crm` of tenant `studio` is
`secret:studio/vault%3Acrm`; an e-mail or a UUID reads as it is. `eap_iam_openfga.names` is the only place that builds
a ref from a platform id or reads one back, so the API, Studio, the Access tab, explain and the audit log only ever see
platform ids. Use the encoded form when you query OpenFGA by hand (the `fga` CLI, the HTTP API).

- A stored id the encoding cannot have produced (a lower-case `%3a`, another `%` sequence) matches nothing the platform
  asks about and is left out of lists.
- Refused before any call (`GrantError` for a write, a deny without a call for a decision, "rows skipped" in a
  reconcile): an empty id; whitespace, a control character, `#` (a userset) or `*` (a wildcard); and a ref over the
  server's limits once encoded: **512 bytes for a user** (`user:` included), **256 characters for an object**
  (`<kind>:<tenant>/` included). An agent or workflow also stands as a user (run links, agent-bound grants) and must
  fit both.
- A tenant id is not encoded: it may hold neither `:` nor `/`.

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
permission set (the engine is not on the tool-call path). A grant the model cannot hold (`granted_to` through the Access tab's grants, a relation the kind
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
  `approver`, `editor`, `granted_to`, `legacy_usable_by`) and never re-adds an owner an admin revoked. `--prune` (with `--apply`) deletes the
  tuples of objects the catalog no longer lists. Runs are not in the catalog; they are registered when they start.
- `python -m eap_iam_openfga.backfill [--tenant T] [--owner SUBJECT]... [--owner-email E]... [--apply]` is the one-off for tenant
  `studio`: the links, plus `owner` on every resource for each owner account, plus the creator recorded on the row when it
  is a real principal (an e-mail or `zitadel:<sub>`). Legacy creators (`operator`, `studio:operator`, `dev:<user>`) never become
  principals; the summary counts them. `--owner-email` resolves an account through `EAP_IAM_MEMBERSHIP` to **both** of its
  subjects (`zitadel:<id>` and the e-mail), as "Subjects in recorded data" asks.

Both read `EAP_DATABASE_URL` (the same read role the API uses) and the `EAP_OPENFGA_*` settings. Tenant roles are not
backfilled: they are contextual (a stored role could outlive its removal in ZITADEL). The plug-in owns no platform table, so it
has no Alembic branch; tuples live in OpenFGA's own database (backed up nightly).

### Tests

`uv run pytest -p no:eap_db -q plugins/iam-openfga` runs the unit tests (a fake server over `httpx2.MockTransport`: check, batch,
list, the over-limit fallback, fail-closed, no secret in an error, the id encoding, the reconcile diff, bootstrap
idempotency, the DSL). The
integration tests (`test_integration.py`: the matrix with contextual roles, grants, ListObjects and BatchCheck, the reconcile,
the bootstrap, all against the pinned server image) are skipped unless `EAP_TEST_OPENFGA_URL` points at a server or
`EAP_TEST_OPENFGA_DOCKER=1` lets the test start a throw-away container (memory datastore, removed afterwards). The Access
tab's API against a real server (`apps/api/api_tests/test_iam_access_openfga_real.py`, which also needs Postgres) is
switched on the same way. CI Heavy sets the second; a skipped run is not a pass.

## Switching to OpenFGA

For the operator. Do the steps in order and stop at the first one that does not give the stated result. Nothing before
step 5 changes who may do what. Commands run in the API pod, which already has `EAP_DATABASE_URL` and every
`EAP_OPENFGA_*` setting; `K` below is `kubectl --context <your cluster context>`. "Deploy" means your normal
`deploy/scripts/up.sh` run (with the secrets input you always use): it restarts the API and the worker, which is when a
changed ConfigMap value takes effect.

### What changes at the switch

- Every request of a person is decided by OpenFGA from their ZITADEL roles (`admin`, `editor`, `viewer`) plus per-resource
  grants. **A caller without a role and without a grant sees nothing.** That includes the dev bearer of the SSH-tunnel
  page and of scripts, which carries no role: sign in through ZITADEL, or use an API key (keys keep their scopes and
  allowlists, unchanged).
- Creating secrets, MCP servers and tenant API keys, approving, and managing members become admin only. Studio shows the
  Access tabs and "why can't I?" ("The Access tab and explain"); the Architect acts with its person's current roles.
- The API fails closed: while OpenFGA cannot answer, requests answer 503.
- From the switch until the grants backfill (step 8), publishing a version that uses an MCP server or a `secret:` reference
  needs an admin, because nothing is granted yet. Runs are not gated until step 9.

### 0. Preconditions

1. OpenFGA runs and both sides hold the same preshared key (field names only, never a value):

   ```sh
   K -n iam get deploy openfga                                   # READY 1/1
   K -n iam get secret openfga-secrets -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}'
   # must list OPENFGA_DB_PASSWORD and OPENFGA_PRESHARED_KEY
   K -n pydanticos exec deploy/api -- sh -c 'test -n "$EAP_OPENFGA_PRESHARED_KEY" && echo "key present"'
   ```

   OpenFGA is deployed with preshared-key authentication (`OPENFGA_AUTHN_METHOD=preshared`,
   `deploy/k8s/iam/openfga.yaml`). If either command shows no key: add the field `OPENFGA_PRESHARED_KEY` to the deploy
   secret item (16 or more of `A-Za-z0-9._~-`, for example `openssl rand -hex 32`, straight into the secret manager),
   deploy, and check again. An installation that runs OpenFGA without authentication (not this manifest) enables it by
   setting that field and `OPENFGA_AUTHN_METHOD=preshared` on the server; the API and the worker need no other change
   (the key is optional for them, and sent only when present).
2. Sign-in through ZITADEL works, your account is a member with the role `admin`, and every person who needs access is a
   member with a role (Studio, Settings, Members).
3. A fresh backup of the databases exists (the nightly `postgres-backup` job, or run it now).

### 1. Bootstrap the store and the model

```sh
K -n pydanticos exec deploy/api -- python -m eap_iam_openfga.bootstrap --dry-run   # reads and plans, writes nothing
K -n pydanticos exec deploy/api -- python -m eap_iam_openfga.bootstrap             # prints the two ids
```

It finds or creates the store `durable` and writes the model only when it changed. It prints `EAP_OPENFGA_STORE_ID` and
`EAP_OPENFGA_MODEL_ID`; neither is a secret. Run it again after any release that changes
`plugins/iam-openfga/model/durable.fga`, and update the model id (a pinned id keeps the old model).

### 2. Put the ids in the ConfigMap and deploy

In `deploy/k8s/configmap.yaml` set `EAP_OPENFGA_STORE_ID` and `EAP_OPENFGA_MODEL_ID` to the printed values (leave
`EAP_IAM_POLICY: openaccess`), merge, deploy, and check:

```sh
K -n pydanticos exec deploy/api -- printenv EAP_OPENFGA_STORE_ID EAP_OPENFGA_MODEL_ID EAP_IAM_POLICY
K -n pydanticos exec statefulset/worker -- printenv EAP_OPENFGA_STORE_ID EAP_IAM_POLICY
```

### 3. Backfill: dry run, review

```sh
K -n pydanticos exec deploy/api -- python -m eap_iam_openfga.backfill --tenant studio --owner-email <your e-mail>
```

Review the counts: `link:<kind>` must equal the number of agents, workflows, knowledge bases, secrets, schedules, MCP
servers and API keys; `owner:<kind>` covers your two subjects (and recorded creators). "rows skipped" must be 0.

### 4. Backfill: apply, and check

```sh
K -n pydanticos exec deploy/api -- python -m eap_iam_openfga.backfill --tenant studio --owner-email <your e-mail> --apply
K -n pydanticos exec deploy/api -- python -m eap_iam_openfga.reconcile --tenant studio     # dry run: 0 to add
```

### 5. Switch the policy

Make sure test users exist: one `editor` and one `viewer` besides your `admin` account. Then set
`EAP_IAM_POLICY: openfga` in `deploy/k8s/configmap.yaml`, merge and deploy.

### 6. Link what was created in between, and smoke test

A resource created between step 4 and the switch has no link yet (nobody, not even an admin, can act on it):

```sh
K -n pydanticos exec deploy/api -- python -m eap_iam_openfga.backfill --tenant studio --owner-email <your e-mail> --apply
```

Sign in to Studio as each test user (a private window each):

| Check | admin | editor | viewer |
|---|---|---|---|
| Agents, workflows, knowledge bases are listed and open | yes | yes | yes |
| Run an agent (Playground) or a harmless workflow (not the Architect) | works | works | refused, with the reason |
| Edit and save a draft | works | works | refused |
| Create a secret or an MCP server | works | refused | refused |
| Access tab of an agent (and a role or member grant on one test workflow) | can add and remove; the grant takes effect | read only | read only |
| Ask the Architect to list agents | works | works | works (read tools only) |
| Ask the Architect to create an agent | drafts it | drafts it | explains it is read-only; nothing is saved, no approval card |
| Settings, Members | shown | hidden | hidden |

Also check: an API key client still works (same scopes), a scheduled workflow runs, and the API log has no
`policy engine unavailable`. If any check fails, roll back.

### 7. Verify

`GET /api/v1/iam/capabilities` answers `"policy": {"name": "openfga", ...}`; Studio shows the Access tab.

### 8. Agent-bound grants: dry run, then apply

```sh
K -n pydanticos exec deploy/api -- python /app/scripts/iam/backfill_agent_grants.py --tenant studio --json /tmp/grants.json
K -n pydanticos exec deploy/api -- cat /tmp/grants.json > grants.json     # review; names only
K -n pydanticos exec deploy/api -- python /app/scripts/iam/backfill_agent_grants.py --tenant studio --apply --operator <you>
```

The news workflows (`http.request` with `secret:` references) and the agents of their agent steps must be in the report.
Exit code 1 after `--apply` means a grant is missing: do not continue. Details: "Agent and workflow grants" below.

### 9. Enforce the grants

Set `EAP_AGENT_GRANTS_ENFORCE: "1"` in `deploy/k8s/configmap.yaml` (the API and the worker both read it), merge, deploy.
Then verify the news workflows still post: look at the next scheduled run of each (start one by hand only if an extra post
is acceptable). It must succeed with no `permission_denied` event; if one appears, grant what it names in the workflow's
or agent's Access section, or set the switch back to `"0"` and deploy.

### Rollback

Set `EAP_IAM_POLICY: openaccess` and `EAP_AGENT_GRANTS_ENFORCE: "0"` in `deploy/k8s/configmap.yaml`, merge, deploy. Every
signed-in person is admin again and nothing is gated, as before the switch. No data is changed back and no migration is
undone. What stays in OpenFGA (the store, the model, links, owners, grants) is harmless: `openaccess` never reads it.

Before switching on again, repeat from step 3, because nothing was registered or unregistered meanwhile: run
`python -m eap_iam_openfga.reconcile --tenant studio --apply --prune` (dry run first without the two flags) to link new
resources and drop the tuples of deleted ones, and check the Access tab of any secret or MCP server that was deleted and
created again under the same name while rolled back: it would still carry the old grants.

## Agent and workflow grants

An admin authorises an **agent or a workflow** to use an **MCP server** or a **named secret**. That is the only grant: runs of
that agent may use what it was granted, whoever started the run, and nobody's personal credential is used implicitly. The
tenant-wide rule "every member may name any `secret:<name>`, every agent may call any registered server" goes away once the
grants are on. The design is an internal design note.

| Grant | Object | Meaning |
|---|---|---|
| use an MCP server | `mcp_server:<t>/<server id>` | runs may call the server's tools, and the server's own bound credentials (sealed headers and env values, `secret:<name>` in a managed server's env) come with it |
| use a secret | `secret:<t>/<name>` | runs may resolve `secret:<name>` in `http.request`, a built-in tool argument or an MCP tool argument |

The grant is an OpenFGA tuple `agent:<t>/<id>` (or `workflow:<t>/<id>`) `granted_to` the resource; the model answers
`can_use_as_agent`. `can_grant_use` on a server or secret is **admin only** (editors and owners lose it; the old `usable_by`
relation is now `legacy_usable_by`, read by nothing). A server grant does not grant secrets a person names in arguments: a
`secret:<name>` reference needs its own grant. Grants follow the agent or workflow, all versions. Deleting a server, a secret, an
agent or a workflow removes its tuples. Needs a policy engine that keeps grants (`openfga`; the transitional `openaccess` has no
grants, so nothing below is written or enforced with it).

### Granting

- **Studio** shows an "Access" section on the agent page and on the workflow page: the granted servers and secrets, who granted
  each and when, the credentials a server carries, and "granted, unused by the latest version" for a grant the newest version no
  longer needs. Admins can add and remove; everyone who may read the agent or workflow sees the list.
- **API**: `GET /api/v1/agents/{id}/access`, `POST /api/v1/agents/{id}/grants` (body `{kind: mcp_server | secret, name}`),
  `DELETE /api/v1/agents/{id}/grants?kind=&name=`, and the same under `/workflows/{id}`. Writes are admin only (403 for an editor,
  404 for a non-member, 503 `grants_unavailable` when the engine keeps no grants). Every grant and revoke writes an audit row
  (`grant_agent_use`, `revoke_agent_use`; resource the agent or workflow, `detail.name` = `<kind>:<name>`, `detail.reason`).
- **Publish is the grant.** When a new version of an agent or workflow (a publish, a restore, the Architect's publish card, the
  control tool `agent.publish`) newly uses an MCP server or a literal `secret:` reference that is not granted yet, the publish
  card says "This version newly uses MCP server `x`, secret `y`. Approving grants them." (`GET .../publish-grants` returns the line
  and whether the caller may approve). **An admin's approval writes the grants** (audit reason `published version N`, or `approved
  publish` for a card); a person who is not an admin cannot approve such a publish (403 `grant_approval_required`). A version that
  uses nothing new publishes as before. A version that drops a server leaves the grant in place; revoking is never automatic.
  Unregistered names (no such server or secret) and references computed at run time are not granted.

### At run time

When a run starts the worker asks the policy engine once what its subject may use (the agent; for a workflow run the workflow),
stores the answer on the run (`runs.grant_set`, migration 0040) and every gate reads that stored set:

| Gate | Refused when |
|---|---|
| an MCP tool call, and listing a server's tools (no connection is made, no credential is sent) | the server is not in the set |
| a `secret:<name>` in a built-in or MCP tool argument | the name is not in the set |
| `http.request` | any `secret:` name is not in the set, checked before any value is read |

The refusal is a tool error to the model, `permission_denied: agent X is not authorized to use MCP server Y` (or `secret Y`,
`workflow X ...`), plus a `permission_denied` run event (`not_granted` or `authz_unavailable`), and never fails the run by itself.
Names are checked before values are read, so a refusal says nothing about whether a secret exists. A call to `http.request` that
names an ungranted secret asks no approval: no grant, no approval card. A workflow's `http` and `tool` steps use the workflow's
grants; its agent steps are agent runs and use the agent's own grants (wrapping an unauthorized agent in an authorized workflow
does not authorize it). A child run (an agent used as a tool) takes the intersection with its parent agent run's set. A revoke
reaches the next run; a run in flight keeps its start-time set. When the engine cannot answer at start, the run gets an empty set
marked `unavailable` and nothing gated is allowed (the run itself still runs); the next run asks again. Limitations of this first
version: an API key's scopes are not intersected into the set; the refusal event names the resource (never a value) whoever reads
the run.

### Turning it on (operator steps)

Nothing is enforced until you turn it on, and the switch is two things that must both be true: the policy engine keeps grants,
**and** `EAP_AGENT_GRANTS_ENFORCE=1`. While `EAP_IAM_POLICY=openaccess` the grant API answers 503, publishes are
unchanged and no run stores or checks a set; `EAP_IAM_POLICY` is not changed by any of this. The steps for a tenant (for example
`studio`), after the switch to `openfga` ("Switching to OpenFGA" above, whose steps 8 and 9 are these in short; the
deployment already puts the `EAP_OPENFGA_*` settings on the API **and the worker** and holds `EAP_AGENT_GRANTS_ENFORCE`
in the ConfigMap):

1. **Migrate**: `uv run alembic -c packages/db/alembic.ini upgrade head` (0040 adds `runs.grant_set`). The worker image needs the
   same IAM plug-in distribution as the API (`eap-iam-openfga`) and the same `EAP_IAM_POLICY` / `EAP_OPENFGA_*`, because the worker
   asks the engine at run start.
2. **Report** (dry run, writes nothing; works with any engine):
   `EAP_DATABASE_URL=... uv run python scripts/iam/backfill_agent_grants.py --tenant studio --json report.json`.
   It lists, per agent and per workflow, the MCP servers and secrets its **published versions** (all of them: a pinned schedule may
   run an old one) would be granted, the latest draft's extra uses marked `draft` (never granted), dangling names (not registered:
   reported, not granted), definitions with run-time (`dynamic`) references (only the static part is granted), agents and workflows
   with nothing to grant, and servers and secrets no spec names. **The news workflows (`http.request` with `secret:` refs) and the
   agents of their agent steps must be in it.** Read it once.
3. **Apply**: `... backfill_agent_grants.py --tenant studio --apply --operator <you>` writes the grants (audit actor
   `backfill:<you>`), re-reads them and diffs (exit 1 when one is missing); running it twice writes nothing the second time. This
   needs `EAP_IAM_POLICY=openfga` for the process. Archived definitions are skipped.
4. **Enable**: set `EAP_AGENT_GRANTS_ENFORCE=1` on the **worker** (the gates) and restart it; set it on the API too so Studio says
   the grants are enforced. Runs that start after the restart use their start-time sets.
5. **Verify**: run one news workflow; its run's `grant_set` (and a `permission_denied` event, if anything is missing) says what it
   may use. To revert: unset `EAP_AGENT_GRANTS_ENFORCE` and restart the worker (nothing is lost; the grants stay).

The old `legacy_usable_by` tuples are inert after the model change; delete them with the OpenFGA CLI when convenient. Grants are
tuples, so they live in OpenFGA's database (backed up with it).

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

## Inbound webhook signatures (`webhook`)

`POST /api/v1/hooks/{trigger_id}` is the only route that needs no sign-in (see `docs/webhooks.md`). It accepts the
principal kind `webhook` and no other, so the chain consults only the `webhook` provider there: a dev bearer, an Access
JWT or an API key is never read. The provider verifies the trigger's HMAC over the raw body (`RequestView.body`, set only
by this route together with `route_params`) and answers a `webhook` principal whose subject is the trigger id and whose
grant is the trigger row (start a run of exactly its workflow). Every other route lists the kinds it accepts and none
lists `webhook`, so a signature authenticates nothing else. The plug-in is added to the chain whenever it is installed,
whatever `EAP_IAM_IDENTITY` lists. A run it starts records `principal = webhook:<trigger id>`,
`started_by_kind = webhook` and acts on behalf of nobody (`on_behalf_of` empty, no roles): a person's approval of a
trigger never makes its runs that person.

The hook route asks no policy engine: the authority is the approved trigger row, which also selects the tenant. Managing
triggers is decided like every resource (`services/webhook_triggers.py` takes an `Access`): `read` on the workflow to see
a trigger and its deliveries, `edit` to change it, and `grant_use` on its secrets (admin) to enable it or to add a secret
to an enabled one; generating a signing secret is `create` secret. Agent-bound grants apply to a webhook run as to any
run (the workflow's grants for its steps, each agent's for agent steps). Details: `docs/webhooks.md`, "Who may do what"
and "The identity of a webhook run".

## External access (`run` keys)

An external agent authenticates with a tenant API key whose only scope is `run`: it belongs to an external client, has a
non-empty workflow allowlist, allows no agent and expires. The action `run.external` needs that scope
(`eap_iam.constraints.ACTION_SCOPES`), so such a key is refused by every other route and every other key is refused by
`run.external`; no role lets a person hold a personal `run` key. The `openfga` engine abstains for keys: the constraints
decide, asked through `services/authz.py`. Managing external clients, keys and exports is
`tenant.manage_external_access` (admin); reading them is `tenant.read_external_access` (editor). See
`docs/external-access.md`. The external routes are the MCP run surface (`/api/v1/external/mcp`) and the A2A server
(`/api/v1/external/a2a/{endpoint_id}`); both take a `run` key only, apart from the A2A public Agent Card, which anyone
may read and which lists no workflow. A key of another tenant than the endpoint's reads it as not found.

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
