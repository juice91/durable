// The API with rows, for the page suites (accessibility and roles): one of each resource.
import { AGENT as WORKFLOW_AGENT, demoSpec } from "./workflowFixtures";
import { breakdownFixture, summaryFixture, timeseriesFixture } from "@/components/metrics/metricsFixtures";
import { OPTIONS as ARCHITECT_OPTIONS, SEEDED_STATE as ARCHITECT_STATE } from "@/components/architect/architectFixtures";
import { modelsFixture, processorCatalogFixture } from "@/components/agents/specSections.fixtures";

const T0 = "2026-11-14T10:00:00Z";
const T1 = "2026-11-14T10:00:02Z";

const AGENT = {
  id: "a1",
  name: "Support bot",
  latest_version: 2,
  draft_spec: {
    spec_version: 1,
    name: "Support bot",
    description: "Answers support questions.",
    instructions: "Help the customer.",
    model: { provider: "test", name: "echo" },
    tools: ["http.get"],
    input_processors: [{ type: "pii_redact", config: {} }],
  },
};

/** RM.18: an agent with memory (per-user working memory), for the Memory tab. */
const MEMORY_AGENT = {
  id: "a2",
  name: "Memory bot",
  latest_version: 1,
  draft_spec: { ...AGENT.draft_spec, name: "Memory bot", memory: { working: { scope: "resource" } } },
};
const MEMORY_VERSIONS = [{ agent_id: "a2", version: 1, spec: MEMORY_AGENT.draft_spec, spec_hash: "m1", created_at: T0, created_by: "dev", restored_from_version: null }];
const MEMORY_NOTE = { agent_id: "a2", scope: "resource", scope_key: "alice", format: "markdown", content: "# User\n- Name: Alice", version: 2, updated_by_run_id: null, created_at: T0, updated_at: T1 };

const VERSIONS = [1, 2].map((version) => ({
  agent_id: "a1",
  version,
  spec: AGENT.draft_spec,
  spec_hash: `h${version}`,
  created_at: T0,
  created_by: "dev",
  restored_from_version: null,
}));

function run(id: string, extra: Record<string, unknown> = {}) {
  return {
    run_id: id,
    kind: "agent",
    agent_id: "a1",
    version: 2,
    status: "completed",
    display_status: "completed",
    input: "hello",
    output: "hi there",
    error: null,
    heartbeat_at: null,
    created_at: T0,
    updated_at: T1,
    trace_id: "t1",
    conversation_id: null,
    turn: null,
    model_id: "test:echo",
    input_tokens: 12,
    output_tokens: 7,
    cost_usd: "0.0001",
    started_at: T0,
    finished_at: T1,
    ...extra,
  };
}

const WORKFLOW_RUN = run("wr1", { kind: "workflow", agent_id: null, version: null, workflow_id: "w1", workflow_version: 1 });

/** A step-by-step run paused after `classify` (waiting on the synthetic `classify_pause` step). */
const PAUSED_RUN = run("wr2", { kind: "workflow", agent_id: null, version: null, workflow_id: "w1", workflow_version: 1, status: "waiting_approval", display_status: "waiting_approval", pause_after_each_step: true });
const PAUSED_EVENTS = [
  { seq: 0, type: "step_started", data: { step_id: "classify", step_type: "agent", input: { text: "hi" } }, ts: T0 },
  { seq: 1, type: "step_completed", data: { step_id: "classify", output: "billing" }, ts: T0 },
  { seq: 2, type: "step_waiting", data: { step_id: "classify_pause", prompt: "Paused after classify", pause: true, timeout_hours: 24, workflow_id: "w1", deadline: "2099-01-01T00:00:00Z" }, ts: T0 },
];

const TOOLS = [
  { id: "http.get", title: "HTTP GET", description: "Fetch a URL.", side_effect: false, external_idempotent: true, input_schema: { type: "object", properties: { url: { type: "string" } }, required: ["url"] }, allowed: true },
  { id: "clock.now", title: "Current time", description: "The current time.", side_effect: false, external_idempotent: true, input_schema: { type: "object", properties: {} }, allowed: true },
];

const MCP_SERVER = {
  id: "m1",
  name: "Docs",
  url: "https://docs.example.test/mcp",
  transport: "streamable_http",
  enabled: true,
  header_names: ["Authorization"],
  created_at: T0,
  updated_at: T0,
  last_checked_at: T0,
  last_status: "ok",
  last_error_code: null,
};

const LOGS = {
  items: [
    { ts: T0, level: "info", message: "run started", service: "worker", trace_id: "t1", span_id: "s1", run_id: "r1" },
    { ts: T1, level: "error", message: "tool failed", service: "worker", trace_id: "t1", span_id: "s2", run_id: "r1" },
  ],
  from: T0,
  to: T1,
  limit: 100,
};

const PROCESSOR_RESULTS = {
  items: [{ run_id: "r1", seq: 3, ts: T0, agent_id: "a1", agent_version: 2, parent_run_id: null, phase: "input", index: 0, processor_type: "pii_redact", action: "redact", match_counts: { email: 1 } }],
  next_before: null,
};

const KB = {
  id: "kb1",
  tenant_id: "t1",
  name: "Product docs",
  description: "Manuals and FAQs.",
  embedding_model: "test:hash",
  dimensions: 1536,
  chunking: { max_tokens: 512, overlap_tokens: 64, min_tokens: 40, chunker_version: 1 },
  active_generation: 1,
  last_generation: 1,
  pending_generation: null,
  pending_embedding_model: null,
  pending_dimensions: null,
  status: "ready",
  error: null,
  created_at: T0,
  updated_at: T1,
  document_count: 2,
  chunk_count: 9,
};

function kbDocument(key: string, extra: Record<string, unknown> = {}) {
  return {
    id: `doc-${key}`, tenant_id: "t1", kb_id: "kb1", document_key: key, title: key.replace(/\.\w+$/, ""), source_url: null,
    mime_type: "text/markdown", metadata: {}, content_hash: "abc", active_version: 1, latest_version: 1, status: "ready",
    error: null, chunk_count: 9, last_run_id: "r1", created_at: T0, updated_at: T1, ...extra,
  };
}

const WEBHOOK_TRIGGER = {
  id: "wh1", name: "github-push", description: "", workflow_id: "w1", workflow_version: null, version: "latest", preset: "github", preset_config: {},
  secrets: [{ ref: "secret:gh-signing", added_at: T0, last_matched_at: null }], tolerance_seconds: 300,
  actions: { start: { payload_schema: { type: "object" }, input_map: {}, run_name: null } },
  limits: { rate_per_minute: 60, max_concurrent_runs: 2, max_runs_per_day: 1000, max_body_bytes: 262144 },
  status: "disabled", config_hash: "0123456789abcdef0123", approved: false, enable_requested_by: null, enable_requested_at: null,
  enabled_by: null, enabled_at: null, last_delivery_at: null, last_outcome: null, revision: 1, created_by: "dev", created_at: T0, updated_at: T0,
  url: "https://api.example.test/hooks/wh1", warnings: [],
};

const EXTERNAL_KEY = { id: "k1", name: "laptop", prefix: "pos_k1_", status: "active", scopes: ["run"], workflow_ids: ["w1"], expires_at: T1, last_used_at: null, created_at: T0 };
const EXTERNAL_CLIENT = { id: "cl1", name: "claude-code", description: "", enabled: true, max_active_runs: null, max_runs_per_day: null, revision: 1, created_at: T0, keys: [EXTERNAL_KEY] };
const A2A_ENDPOINT = {
  endpoint_id: "0123456789abcdef0123456789abcdef", card_name: "Acme automations", card_description: "Runs the exported workflows.", enabled: true,
  url: "https://durable.example.test/api/v1/external/a2a/0123456789abcdef0123456789abcdef",
  card_url: "https://durable.example.test/api/v1/external/a2a/0123456789abcdef0123456789abcdef/.well-known/agent-card.json",
  created_by: "user:alice", created_at: T0, updated_at: T1,
};
const WORKFLOW_EXPORT = {
  id: "ex1", name: "triage", title: "Triage a ticket", description: "", tags: [], examples: [], workflow_id: "w1", workflow_name: "Triage", version: "latest", resolved_version: 1,
  protocols: ["mcp"], result: "output", client_inputs: false, max_runtime_seconds: 3600, require_idempotency_key: false, enabled: false, disabled_reason: null,
  enabled_by: null, enabled_at: null, approval_hash: "0123456789abcdef0123", input_schema: { type: "object" }, warnings: [], revision: 1,
  keys: [{ id: "k1", prefix: "pos_k1_", name: "laptop", status: "active", client_id: "cl1", client_name: "claude-code" }],
};

/** The API with rows: one agent, two versions, runs, tools, an MCP server, a workflow, logs, metrics. */
export function respond(url: URL): unknown {
  const p = url.pathname.replace(/^\/api\/v1/, "");
  if (p === "/architect") return ARCHITECT_STATE;
  if (p === "/architect/options") return ARCHITECT_OPTIONS;
  if (p === "/agents") return [AGENT, MEMORY_AGENT];
  if (p === "/agents/a2") return MEMORY_AGENT;
  if (p === "/agents/a2/versions") return MEMORY_VERSIONS;
  if (p === "/agents/a2/memory/working") return { items: [MEMORY_NOTE] };
  if (p === "/agents/a2/memory/resources/alice/working") return MEMORY_NOTE;
  if (p === "/agents/a1") return AGENT;
  if (p === "/agents/a1/versions") return VERSIONS;
  if (/^\/agents\/a1\/versions\/\d+$/.test(p)) return VERSIONS[Number(p.split("/").pop()) - 1] ?? VERSIONS[1];
  // AC.16: the Tests tab's suite list and eval runs.
  if (p === "/agents/a1/eval-suites") return { items: [{ id: "s1", agent_id: "a1", name: "Refund suite", description: null, evaluators: [{ kind: "equals_expected" }], case_count: 2, archived: false, archived_at: null, created_at: T0, updated_at: T1 }] };
  if (p === "/agents/a1/evals/runs") return { items: [], next_cursor: null };
  // W1.5: the tenant-wide Datasets, Scorers and Experiments lists.
  if (p === "/eval-suites") return { items: [{ id: "s1", agent_id: "a1", name: "Refund suite", description: null, evaluators: [{ kind: "equals_expected" }], case_count: 2, archived: false, archived_at: null, created_at: T0, updated_at: T1, agent_name: "Support bot", agent_archived: false, last_run: { run_id: "er1", version: 2, status: "completed", display_status: "completed", created_at: T1, updated_at: T1, aggregates: { cases: 2, passed: 1, failed: 1, errors: 0, pass_rate: 0.5 } } }], next_cursor: null };
  // Parity 2: one dataset (suite) of an agent, and a workflow's suites on its Tests tab.
  if (p === "/eval-suites/s1") return { id: "s1", agent_id: "a1", workflow_id: null, target_kind: "agent", target_id: "a1", target_name: "Support bot", name: "Refund suite", description: null, evaluators: [{ kind: "equals_expected" }], case_count: 1, archived: false, archived_at: null, created_at: T0, updated_at: T1, cases: [{ id: "c1", suite_id: "s1", name: "Refund case", input: "I want a refund", expected_output: null, metadata: {}, evaluators: [], position: 0, created_at: T0, updated_at: T1 }] };
  if (p === "/workflows/w1/evals/suites") return { items: [{ id: "ws1", agent_id: null, workflow_id: "w1", target_kind: "workflow", target_id: "w1", target_name: "Triage", name: "Triage cases", description: null, evaluators: [{ kind: "equals_expected" }], case_count: 1, archived: false, archived_at: null, created_at: T0, updated_at: T1 }] };
  if (p === "/evals/runs") return { items: [{ ...run("er1", { kind: "eval" }), suite_id: "s1", suite_hash: "h", suite_name: "Refund suite", agent_name: "Support bot", aggregates: { cases: 2, passed: 1, failed: 1, errors: 0, pass_rate: 0.5, scores_avg: { quality: 0.75 }, duration_ms_p50: 120, cost_usd: "0.0002" } }], next_cursor: null };
  if (p === "/evals/scorers") return { items: [{ kind: "equals_expected", name: "equals_expected", suite_count: 1, suites: [{ id: "s1", name: "Refund suite", agent_id: "a1", agent_name: "Support bot", case_level_uses: 0 }], history: [{ run_id: "er1", suite_id: "s1", agent_id: "a1", created_at: T1, metric: "pass_rate", value: 0.5 }] }], next_cursor: null };
  if (p === "/agents/a1/request-context") return { agent_id: "a1", version: 2, draft: false, fields: [], schema: { type: "object", properties: {} } };
  if (p === "/agents/a1/conversations" || p === "/conversations") return { items: [{ id: "c1", agent_id: "a1", title: "Refund question", turn_count: 2, created_at: T0, updated_at: T1 }], next_before: null };
  if (p === "/conversations/c1") return { id: "c1", agent_id: "a1", title: "Refund question", turn_count: 1, created_at: T0, updated_at: T1, runs: [run("r1", { conversation_id: "c1", turn: 1 })] };
  if (p === "/models") return { items: modelsFixture };
  if (p === "/providers") return { items: [{ id: "litellm", label: "LiteLLM gateway", configured: true, env_var: "EAP_LITELLM_API_KEY", base_url_host: "gateway.example.test", status: "ok", model_count: 3, checked_at: T0, error: null }] };
  if (p === "/iam/capabilities") return { mode: "dev", identity: [], policy: { name: "p", transitional: true, features: [] }, membership: null, actions: [], me: { subject: "zitadel:u1", email: "zed@example.test", tenant_id: "acme", kind: "user", roles: ["editor"], provider: "cf-access" } };
  if (p === "/audit") return { items: [{ id: "au1", at: T1, actor_kind: "user", actor_subject: "zitadel:u1", on_behalf_of: null, action: "create_agent", resource_kind: "agents", resource_id: "a1", outcome: "success", request_id: null, run_id: null, detail: null }], next_cursor: null, retention_days: 400 };
  if (p === "/processors") return processorCatalogFixture;
  if (p === "/processor-results") return PROCESSOR_RESULTS;
  if (p === "/tools") return TOOLS;
  if (p.startsWith("/tools/")) return TOOLS.find((tool) => `/tools/${tool.id}` === p) ?? TOOLS[0];
  if (p === "/mcp-catalog") return { items: [{ name: "io.example/files", title: "Files", description: "Read and write files.", version: "1.0.0", status: "active", licence: "MIT", packages: [], remotes: [{ type: "streamable-http", url: "https://files.example.test/mcp" }], local: false, remote: true, fetched_at: T0 }], next_after: null };
  if (p === "/mcp-servers") return [MCP_SERVER];
  if (p === "/secrets") return [{ name: "slack-token", created_at: T0, updated_at: T0, used_by: [{ server_id: "m1", name: MCP_SERVER.name }] }];
  if (p === "/mcp-servers/m1") return MCP_SERVER;
  if (p === "/knowledge-bases") return { items: [KB] };
  if (p === "/knowledge-bases/kb1") return { ...KB, ingest_tokens: 4200, ingest_cost_usd: "0.0004", ingest_requests: 3 };
  if (p === "/knowledge-bases/kb1/documents") return { items: [kbDocument("install.md"), kbDocument("billing.md", { status: "failed", error: "embedding_failed: provider down", chunk_count: 0, active_version: null })], next_after: null };
  if (p === "/kb-sources/adapters") return { items: [{ kind: "product_catalog", provider: "acme", distribution: "eap-kb-sources-acme", display_name: "Acme Parts", connection_kind: "oauth2_client_credentials", base_url: "https://api.acme.test", token_url: "https://auth.acme.test/token", config_schema: null }] };
  if (p === "/settings/connections") return { items: [{ id: "cn1", name: "catalogue-prod", kind: "oauth2_client_credentials", base_url: "https://api.acme.test", config: { provider: "acme", token_url: "https://auth.acme.test/token" }, credential_refs: { client_id: "secret:conn-id", client_secret: "secret:conn-secret" }, enabled: true, last_checked_at: T1, last_status: "ok", last_error_code: null, created_at: T0, updated_at: T1 }] };
  if (p === "/knowledge-bases/kb1/sources") return { items: [{ id: "src1", knowledge_base_id: "kb1", connection_id: "cn1", kind: "product_catalog", provider: "acme", name: "Acme parts", enabled: true, config: { page_size: 100 }, last_successful_sync_at: T1, last_checkpoint_summary: { segments: 3, has_cursor: true }, last_source_revision: null, created_at: T0, updated_at: T1 }] };
  if (p === "/webhook-triggers") return { items: [WEBHOOK_TRIGGER] };
  if (p === "/agent-access") return { enabled: true, base_url: "https://api.example.test", grants_enforced: true, can_manage: true, max_runtime_seconds: 1209600 };
  if (p === "/agent-access/clients") return { items: [EXTERNAL_CLIENT] };
  if (p === "/agent-access/exports") return { items: [WORKFLOW_EXPORT] };
  if (p === "/agent-access/a2a-endpoint") return { endpoint: A2A_ENDPOINT };
  if (p === "/webhook-triggers/wh1") return WEBHOOK_TRIGGER;
  if (/^\/webhook-triggers\/wh1\/(events|deliveries)$/.test(p)) return { items: [] };
  if (p === "/workflows") return [{ id: "w1", name: demoSpec.name, draft_spec: demoSpec, latest_version: 1 }];
  if (p === "/workflows/w1") return { id: "w1", name: demoSpec.name, draft_spec: demoSpec, latest_version: 1 };
  if (p === "/workflows/w1/versions") return [{ workflow_id: "w1", version: 1, spec: demoSpec, spec_hash: "wh1", created_at: T0, created_by: "dev" }];
  if (/^\/workflows\/w1\/versions\/\d+$/.test(p)) return { workflow_id: "w1", version: 1, spec: demoSpec, spec_hash: "wh1", created_at: T0, created_by: "dev" };
  if (p === "/runs") {
    if (url.searchParams.get("workflow_id") || url.searchParams.get("kind") === "workflow") return { items: [WORKFLOW_RUN], next_before: null };
    return { items: [run("r1"), run("r2", { status: "failed", display_status: "failed", error: "Tool failed" })], next_before: null };
  }
  if (p === "/runs/histogram") return { interval: "day", buckets: [{ start: T0, runs: 3, failed: 1 }, { start: T1, runs: 5, failed: 0 }] };
  if (p === "/spans") return { items: [{ run_id: "r1", span_id: "s1", parent_id: null, kind: "tool_call", name: "lookup", start: T0, end: T1, status: "ok", agent_id: "a1", workflow_id: null, model: null }], next_before: null };
  if (p === "/runs/wr1") return WORKFLOW_RUN;
  if (p === "/runs/wr2") return PAUSED_RUN;
  if (p === "/runs/wr2/events") return PAUSED_EVENTS;
  if (/^\/runs\/[^/]+\/events$/.test(p)) return [];
  if (/^\/runs\/[^/]+$/.test(p)) return run(p.split("/").pop()!);
  if (p === "/logs") return LOGS;
  if (p === "/approvals") {
    return {
      items: [{
        run_id: "r1", kind: "tool", approval_key: "call_1", tool_call_id: "call_1", step_id: null, root_run_id: "r1", agent_id: "a1",
        workflow_id: null, tool_name: "send_email", args: { to: "a@example.test" }, prompt: null, status: "pending", requested_at: T0,
        deadline: T1, decided_at: null, decision: null, decided_by: null, run_status: "running",
      }],
      next_cursor: null,
    };
  }
  if (p === "/metrics/summary") return summaryFixture({ runs: 12, failed: 1 });
  if (p === "/metrics/timeseries") return timeseriesFixture([{ runs: 4 }, { runs: 8, failed: 1 }]);
  if (p === "/metrics/breakdown") {
    const by = url.searchParams.get("by") === "agent" ? "agent" : "model";
    return breakdownFixture(by, [{ key: by === "agent" ? "a1" : "test:echo", runs: 12 }]);
  }
  return { items: [], next_before: null, total: 0 };
}

