# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html); see `docs/releasing.md`.

## [Unreleased]

## [0.3.0] - 2026-11-24

### Features

- Resolve nested $ref/$template markers; check tool inputs at publish
- **architect:** SystemContextProvider and provenance (phase C)
- IAM P1: eap-iam-zitadel plug-in, Studio OIDC login, Members
- **studio:** Account menu and profile page
- **iam:** Personal API keys (backend)
- **iam:** eap-iam-openfga policy plug-in, model, reconcile and backfill (P2a)

### Fixes

- **iam-zitadel:** Bootstrap idempotent against omitted default enums
- **iam:** ZITADEL uses the v1 login UI on a fresh instance
- **worker:** Undefined list_runs_with_previews breaks the Architect; pyflakes CI gate

### Documentation

- AGENTS.md for coding agents
- **agents:** long-running commands and agent-neutral trailers

### Infrastructure

- Studio signs in through ZITADEL

## [0.2.0] - 2026-11-23

### Features

- Studio parity round 2: schedules, step-by-step, processor phases, workflow experiments, spans
- Attachments round 2: object storage (R2), API-key uploads, unsent expiry, capabilities from discovery
- MCP local runtime: catalog mirror, ToolHive operator runtime, managed servers, Studio browse and run
- Architect: built-in agent that drafts and publishes agents and workflows with approval
- **deploy:** Direct Anthropic provider key for the API and worker
- Secret input: agents request credentials through a masked approval card; refs resolve at use, ToolHive-integrated
- **studio:** The Architect page's New button offers an agent or a workflow
- **deploy:** Allow Anthropic models on deploy-host alongside the gateway
- Architect: ranked default model, PUT /architect/model, page selector and readable drafter failures
- MCP catalog: ToolHive registry as a second source, with network permissions honoured
- Knowledge-base source sync: generic connections, sources, structured store, resumable DBOS sync; Acme Semiconductor adapter
- **api,studio:** The Agents list shows one Architect; the drafters are system helpers
- **studio:** MCP page — catalog first, remote servers second
- **studio:** Settings as tabs
- **workflow:** A tool step's error result fails the run with its stable code
- Knowledge-base sources: GitHub repository documents adapter (second provider)
- **deploy:** Allow the general tools clock.now and secrets.request for tenant agents
- PydanticOS controlled through MCP: /api/v1/mcp with 36 functional tools, resources, scopes and a connect snippet
- **tools:** http.request general HTTPS tool, $template step inputs, Bluesky post example workflow
- **tools:** feed.parse, json.query, text.check; the Architect can compose collect-to-publish workflows
- **models:** Anthropic model discovery via /v1/models; the Architect's default prefers claude-sonnet-5-5
- Archive and unarchive any workflow from the API, Studio and MCP
- **evals:** Regex evaluator and reusable regex scorer take negate and field
- **architect:** read-only platform.* tools so the Architect can answer questions about the tenant
- **evals:** AI-suggested rules for agents and workflows, one click to add as scorer or suite evaluator; scorer and eval tools over MCP
- **studio:** The evaluation pages call them test suites, not datasets
- **observability:** External OpenTelemetry traces over OTLP/HTTP with tenant API keys
- **kb-sources:** Adapters are entry-point plug-ins; the Acme adapter is its own workspace member; Studio lists providers from the API
- Optimistic concurrency — revision, ETag and If-Match on definition-class resources
- **tools:** html.extract (pure HTML article and link extraction) and http.request body_from_url
- **tools:** http.request reducers — response_query, extract_html, parse_feed
- **architect:** Startup reconcile republishes the seeded agents when the seed text changes
- **workflows:** Tool steps may keep an error result and continue (on_error: continue)
- **studio:** The Approvals inbox updates live — new cards appear in place with a toast
- **tools:** html.extract links mode takes exclude_urls and exclude_terms
- **workflows:** A run can end as skipped (workflow.return outcome: skipped)
- **providers:** Cloudflare Workers AI through AI Gateway (provider id cloudflare)
- **studio:** Readable run view for branch-heavy workflows — case lists, Not reached, folded unreached steps
- **studio:** Every text content is selectable and copyable
- **html.extract:** Headline fallback for image-only card links
- **workflows:** AI explanation of a published workflow (AI 說明)
- **architect:** test-suite tools (create suite, add cases, attach scorer, start and read runs)
- **architect:** Discover and call the platform's control-plane tools (platform__list_capabilities, control__*)
- **http.request:** max_redirects follows redirects hop by hop under the same guards
- **studio:** The workflow Graph tab says what each step does and how
- **architect:** platform.get_workflow says what each step does; platform.get_run shows a run's input and output
- **clock.now:** Optional parts and offset_minutes
- **workflows:** Schedule input validation error code, and a publish warning for unguarded optional inputs
- Runs show when DBOS recovered them (Recovered badge, recovery_attempts, Metrics tile)
- KB sync crash-recovery demo script and docs; clock.sleep tool
- **studio:** Themed Select replaces native selects
- Agents as tools: a supervisor agent calls published agents as durable child runs
- text.check: opt-in verbatim_exempt rule
- Studio: pin the built-in Architect as the first Agents row
- IAM P0a: core interfaces and the apikey, devloopback, openaccess plug-ins (behaviour unchanged)
- KB: ingest source code (code-aware extraction); design KB includes durable's code
- **architect:** Capability registry, known gaps, read API and Architect tools (phase A)
- **architect:** Versioned system manifest and CI drift check (phase B)
- IAM P0b: eap-iam-cfaccess, the real actor everywhere, audit_log, Studio without the baked bearer
- atproto.richtext: Bluesky facets with correct UTF-8 byte offsets
- Studio: current user in the sidebar footer (badge, tooltip, rail avatar)

### Fixes

- **api:** Eval run double submit is idempotent under concurrency
- **deploy:** Install helm from the release tarball when Homebrew is not writable
- **deploy:** Give the API the LiteLLM key and egress to the gateway
- **worker:** built-in agents keep the Architect's tools under a deployment tool allowlist
- **mcp:** Install OCI catalog packages whose identifier carries the tag or digest
- **deploy:** kube-API port substitution matches kustomize output; API egress to managed MCP servers
- **architect, mcp:** Discover managed MCP tools; secret refs in MCP tool arguments resolve at the call
- **architect:** JSON-string specs, ranked default model and derived ids, repeated failing tool calls end readably
- **api:** fastmcp-slim[server] is a runtime dependency of the API
- **architect:** Gaps found while the Architect designed a real collect-to-publish workflow
- **deploy:** Wait for the ToolHive operator rollout, not for every labelled pod
- **deploy:** The drain ignores runs parked on a pending approval
- **kb-sources:** Acme listing uses the documented Page<Product> envelope; listing diagnostics when a page yields no items
- **deploy:** The drain ignores recoverable run kinds, can be skipped, and restores the API on failure
- **architect:** The seed reconciles built-in tool ids, on every call and at API startup
- **deploy:** The gate matches CI runs by workflow file path, not by name
- **studio,api:** The agents list shows the published model and flags unpublished draft changes
- **studio:** The secrets form keeps password managers out and leaves nothing typed after a save
- **studio,architect:** The Architect's created list shows names, hides archived items; kind-aware ids for CJK names
- **studio:** Register the Architect list's show-archived key under Data in this browser
- **studio:** Eval runs read "Test run: <target>", never "Agent: unknown"
- **studio:** knowledge-base source sync runs are labelled, never "Agent: unknown"
- **mcp:** Test starts an idle-stopped managed server; keep_warm; Studio says what happened
- **studio:** Label KB source sync runs in the trace view
- **studio:** Dropdowns accept mouse selection
- workflow.history: skip items a workflow's own recent runs rejected
- Architect step cap equals the platform's workflow cap (50)

### Security

- Fix vulnerable JS dependencies; let the workflow policy test accept the CLA workflow
- Fix fixable CRITICAL CVEs in the MCP runner images
- Secrets via External Secrets Operator (1Password) and k3s secrets encryption at rest

### Documentation

- **spike:** Architect spike result
- **kb-sources:** The framework names no provider secret; TI specifics move to a provider page
- **licence:** Apache-2.0 LICENSE and NOTICE, dependency licence tables, licence fields
- Deploy docs: generic public hostname
- Pluggable IAM design (interfaces in core, ZITADEL + OpenFGA plug-ins)
- **spec:** system-aware Architect (SystemContextProvider) design

### Infrastructure

- Skip docs-only heavy runs, cancel superseded runs, xdist, vitest shards, caches
- CI/CD speed round 2: heavy without the hosted wait, tree-hash deploy gate, 5 Studio shards, heavy on pull_request
- **heavy:** Remove the per-run Postgres container with its anonymous volume
- pull-request runs execute only the affected tests; main stays full; the deploy gate accepts only full runs
- Run the hosted CI on the Mac Studio runners when CI_RUNNER is set
- setup-python only on hosted runners; uv provisions Python on the Mac Studio
- pnpm/action-setup installs into the job's temp dir
- 25-minute timeout for python and deploy-checks on the shared Mac runners
- **k8s:** Project CLOUDFLARE_API_TOKEN into the api and worker pods
- Document the four self-hosted runners; 15 s vitest timeout under CI
- Cloudflare Tunnel connector for durable.example-org.ai, same-origin Studio API
- Deploy ZITADEL and OpenFGA in namespace iam (infrastructure only)
- Make optional 1Password fields truly optional (unblock deploys)
- Disable service links on ZITADEL and OpenFGA pods
- Fix k3s-secrets-encryption.sh: server port and status wait
- Stop Studio vitest shard timeouts: cap workers per CI shard

[Unreleased]: https://github.com/juice91/durable/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/juice91/durable/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/juice91/durable/releases/tag/v0.2.0
