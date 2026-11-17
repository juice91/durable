/**
 * SL.15 accessibility smoke: every main page rendered inside the real frame WITH data (populated
 * lists, a loaded entity), in the light and the dark theme. Each must show exactly one level-one
 * heading and pass axe with the page rules on (one <main>, one h1). The empty-API version of this
 * walk is SL.4's `components/frame/frame.acceptance.test.tsx`; overlays (Sheets, popovers, the
 * palette) have their own axe tests next to their components.
 *
 * Fixtures reuse the shared fixture modules (metrics, workflow specs, processor catalog, models);
 * the API is the same `fetch` stub the frame acceptance test uses, answering with rows.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import fs from "node:fs";
import path from "node:path";

const nav = vi.hoisted(() => ({ path: "/agents", params: {} as Record<string, string> }));

vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => nav.params,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

import { AXE_TIMEOUT_MS } from "./a11y";
import { expectAccessiblePage } from "./a11yPages";
import { FrameHarness, mockMatchMedia } from "@/components/frame/frameTesting";
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

/** The API with rows: one agent, two versions, runs, tools, an MCP server, a workflow, logs, metrics. */
function respond(url: URL): unknown {
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

beforeEach(() => {
  window.localStorage.clear();
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1440 });
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown) => {
      const url = new URL(String(typeof input === "string" ? input : (input as Request).url), "http://localhost");
      return new Response(JSON.stringify(respond(url)), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
  // EventSource-backed run streams are not opened by any page at rest; a stub keeps one from throwing.
  vi.stubGlobal("EventSource", class { close() {} addEventListener() {} removeEventListener() {} });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

interface PageCase {
  name: string;
  path: string;
  params?: Record<string, string>;
  load: () => Promise<{ default: React.ComponentType }>;
  /** The visible h1. */
  h1: string;
  /** Text (or a test id with `#`) that shows the fixtures rendered, not an empty or error state. */
  shows: string;
}

/** An agent tab page inside `app/agents/[id]/layout.tsx`, as Next renders it. */
async function inAgentLayout(tab: Promise<{ default: React.ComponentType }>): Promise<{ default: React.ComponentType }> {
  const [{ default: Layout }, { default: Tab }] = await Promise.all([import("@/app/agents/[id]/layout"), tab]);
  return { default: () => <Layout><Tab /></Layout> };
}

const PAGES: PageCase[] = [
  { name: "agents", path: "/agents", load: () => import("@/app/agents/page"), h1: "Agents", shows: "Support bot" },
  { name: "agents/new", path: "/agents/new", load: () => import("@/app/agents/new/page"), h1: "New Agent", shows: "Identity" },
  { name: "agent chat", path: "/agents/a1/chat", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/chat/page")), h1: "Support bot", shows: "Refund question" },
  { name: "agent conversation", path: "/agents/a1/chat/c1", params: { id: "a1", conversationId: "c1" }, load: () => inAgentLayout(import("@/app/agents/[id]/chat/[conversationId]/page")), h1: "Support bot", shows: "hello" },
  { name: "agent traces", path: "/agents/a1/traces", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/traces/page")), h1: "Support bot", shows: "#agent-trace-row" },
  { name: "agent editor", path: "/agents/a1/editor", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/editor/page")), h1: "Support bot", shows: "Identity" },
  { name: "agent tests", path: "/agents/a1/tests", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/tests/page")), h1: "Support bot", shows: "Refund suite" },
  { name: "agent memory", path: "/agents/a2/memory", params: { id: "a2" }, load: () => inAgentLayout(import("@/app/agents/[id]/memory/page")), h1: "Memory bot", shows: "#memory-resource-option" },
  { name: "workflows", path: "/workflows", load: () => import("@/app/workflows/page"), h1: "Workflows", shows: "Triage" },
  { name: "workflow schedules", path: "/workflows/schedules", load: () => import("@/app/workflows/schedules/page"), h1: "Schedules", shows: "No schedules" },
  { name: "workflow schedules tab", path: "/workflows/w1/schedules", params: { id: "w1" }, load: () => import("@/app/workflows/[id]/schedules/page"), h1: "Triage", shows: "No schedules" },
  { name: "workflow graph", path: "/workflows/w1", params: { id: "w1" }, load: () => import("@/app/workflows/[id]/page"), h1: "Triage", shows: "classify" },
  { name: "workflow tests tab", path: "/workflows/w1/tests", params: { id: "w1" }, load: () => import("@/app/workflows/[id]/tests/page"), h1: "Triage", shows: "#eval-suite-row" },
  { name: "workflow traces", path: "/workflows/w1/traces", params: { id: "w1" }, load: () => import("@/app/workflows/[id]/traces/page"), h1: "Triage", shows: "#workflow-trace-row" },
  { name: "workflow run", path: "/workflows/w1/runs/wr1", params: { id: "w1", runId: "wr1" }, load: () => import("@/app/workflows/[id]/runs/[runId]/page"), h1: "wr1", shows: "classify" },
  { name: "workflow run paused", path: "/workflows/w1/runs/wr2", params: { id: "w1", runId: "wr2" }, load: () => import("@/app/workflows/[id]/runs/[runId]/page"), h1: "wr2", shows: "#workflow-paused-banner" },
  { name: "architect", path: "/architect", load: () => import("@/app/architect/page"), h1: "Architect", shows: "faq-bot · v2" },
  { name: "tools", path: "/tools", load: () => import("@/app/tools/page"), h1: "Tools", shows: "Current time" },
  { name: "mcp", path: "/mcp", load: () => import("@/app/mcp/page"), h1: "MCP Servers", shows: "Docs" },
  { name: "mcp catalog", path: "/mcp/browse", load: () => import("@/app/mcp/browse/page"), h1: "Catalog", shows: "Files" },
  { name: "mcp detail", path: "/mcp/m1", params: { id: "m1" }, load: () => import("@/app/mcp/[id]/page"), h1: "Docs", shows: "https://docs.example.test/mcp" },
  { name: "knowledge", path: "/knowledge", load: () => import("@/app/knowledge/page"), h1: "Knowledge", shows: "Product docs" },
  { name: "knowledge base", path: "/knowledge/kb1", params: { id: "kb1" }, load: () => import("@/app/knowledge/[id]/page"), h1: "Product docs", shows: "#knowledge-document-row" },
  { name: "processors", path: "/processors", load: () => import("@/app/processors/page"), h1: "Processors", shows: "#processor-row" },
  { name: "processor detail", path: "/processors/pii_redact", params: { type: "pii_redact" }, load: () => import("@/app/processors/[type]/page"), h1: "PII redaction", shows: "#result-row" },
  { name: "metrics", path: "/metrics", load: () => import("@/app/metrics/page"), h1: "Metrics", shows: "#recent-failure" },
  { name: "threads", path: "/threads", load: () => import("@/app/threads/page"), h1: "Threads", shows: "Refund question" },
  { name: "traces", path: "/traces", load: () => import("@/app/traces/page"), h1: "Traces", shows: "#trace-row" },
  { name: "spans", path: "/traces/spans", load: () => import("@/app/traces/spans/page"), h1: "Traces", shows: "#span-row" },
  { name: "logs", path: "/logs", load: () => import("@/app/logs/page"), h1: "Logs", shows: "tool failed" },
  { name: "approvals", path: "/approvals", load: () => import("@/app/approvals/page"), h1: "Approvals", shows: "send_email" },
  { name: "datasets", path: "/datasets", load: () => import("@/app/datasets/page"), h1: "Datasets", shows: "Refund suite" },
  { name: "dataset detail", path: "/datasets/s1", params: { id: "s1" }, load: () => import("@/app/datasets/[id]/page"), h1: "Refund suite", shows: "#eval-case-row" },
  { name: "scorers", path: "/scorers", load: () => import("@/app/scorers/page"), h1: "Scorers", shows: "#scorer-row" },
  { name: "experiments", path: "/experiments", load: () => import("@/app/experiments/page"), h1: "Experiments", shows: "#experiment-row" },
  { name: "providers", path: "/settings/providers", load: () => import("@/app/settings/providers/page"), h1: "Providers", shows: "LiteLLM gateway" },
  { name: "settings", path: "/settings", load: () => import("@/app/settings/page"), h1: "Settings", shows: "Data in this browser" },
];

describe("PAGES covers every page route in app/", () => {
  const APP = path.resolve(__dirname, "..", "app");
  /** `app/agents/[id]/page.tsx` → `/agents/:id`. */
  const patternOf = (file: string) => {
    const dir = path.dirname(path.relative(APP, file)).split(path.sep).join("/");
    return dir === "." ? "/" : `/${dir.replace(/\[(\w+)\]/g, ":$1")}`;
  };
  const live: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name === "page.tsx" && !/\bredirect\(/.test(fs.readFileSync(full, "utf8"))) live.push(patternOf(full));
    }
  };
  walk(APP);

  /** A PAGES path with each fixture param value turned back into `:name` (`/mcp/m1` → `/mcp/:id`). */
  const patternOfCase = (page: PageCase) =>
    Object.entries(page.params ?? {}).reduce((acc, [name, value]) => acc.replace(`/${value}`, `/:${name}`), page.path);

  test("every route that renders (not a redirect page) has a PAGES case, and every case is a route", () => {
    expect([...new Set(PAGES.map(patternOfCase))].sort()).toEqual([...live].sort());
  });
});

async function shown(marker: string): Promise<void> {
  if (marker.startsWith("#")) await screen.findAllByTestId(marker.slice(1), {}, { timeout: 4000 });
  else await waitFor(() => expect(within(screen.getByRole("main")).getAllByText(marker, { exact: false }).length).toBeGreaterThan(0), { timeout: 4000 });
}

describe("every main page with fixtures, inside the frame, light and dark", () => {
  test.each(PAGES)("$name: one h1, no axe violations", async (page) => {
    nav.path = page.path;
    nav.params = page.params ?? {};
    const Page = (await page.load()).default;
    await expectAccessiblePage(() => <Page />, {
      ready: async () => {
        await shown(page.shows);
        const main = screen.getByRole("main");
        await waitFor(() => expect(within(main).getByRole("heading", { level: 1 })).toHaveTextContent(page.h1));
        expect(within(main).queryByTestId("datalist-error")).toBeNull();
      },
    });
  }, AXE_TIMEOUT_MS);
});

describe("expectAccessiblePage catches a broken page", () => {
  const ready = () => screen.findAllByRole("heading", { level: 1 });

  test("two h1s fail", async () => {
    const Two = () => <><h1>One</h1><h1>Two</h1></>;
    await expect(expectAccessiblePage(() => <Two />, { ready, themes: ["light"] })).rejects.toThrow(/one h1 in light/);
  }, AXE_TIMEOUT_MS);

  test("an axe violation fails (an unlabelled button)", async () => {
    const Bad = () => <><h1>Bad</h1><button type="button"><svg aria-hidden="true" /></button></>;
    await expect(expectAccessiblePage(() => <Bad />, { ready, themes: ["dark"] })).rejects.toThrow(/button-name/);
  }, AXE_TIMEOUT_MS);
});

/** Tab stops in DOM order (an approximation of the browser's sequential focus order without positive tabindex). */
function tabStops(): HTMLElement[] {
  const selector = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]';
  return Array.from(document.querySelectorAll<HTMLElement>(selector)).filter((el) => el.tabIndex >= 0 && !el.closest("[hidden], [inert], [aria-hidden='true']"));
}

/** SL.15 focus-order fixes, pinned. */
describe("focus order", () => {
  afterEach(() => cleanup());

  test("no positive tabindex anywhere on a page; the skip link is the first tab stop, then the sidebar, then main", async () => {
    const media = mockMatchMedia(true);
    try {
      nav.path = "/agents";
      const Page = (await import("@/app/agents/page")).default;
      render(<FrameHarness><Page /></FrameHarness>);
      await screen.findAllByText("Support bot");
      const stops = tabStops();
      expect(stops.filter((el) => el.tabIndex > 0)).toEqual([]);
      expect(stops[0]).toHaveTextContent("Skip to content");
      const sidebar = screen.getByTestId("sidebar");
      const main = screen.getByRole("main");
      const firstInSidebar = stops.findIndex((el) => sidebar.contains(el));
      const firstInMain = stops.findIndex((el) => main.contains(el));
      expect(firstInSidebar).toBeGreaterThan(0);
      expect(firstInMain).toBeGreaterThan(firstInSidebar);
    } finally {
      media.restore();
    }
  });

  test.each([
    ["workflow graph", "/workflows/w1", { id: "w1" }, () => import("@/app/workflows/[id]/page")],
    ["workflow run", "/workflows/w1/runs/wr1", { id: "w1", runId: "wr1" }, () => import("@/app/workflows/[id]/runs/[runId]/page")],
  ] as const)("%s: the floating panel (visually left) comes before the canvas's zoom controls (visually bottom right)", async (_name, pathname, params, load) => {
    const media = mockMatchMedia(true);
    try {
      nav.path = pathname;
      nav.params = { ...params };
      const Page = (await load()).default;
      render(<FrameHarness><Page /></FrameHarness>);
      const panel = await screen.findByTestId("workflow-panel");
      const zoomIn = await screen.findByTestId("workflow-zoom-in");
      const stops = tabStops();
      const firstInPanel = stops.findIndex((el) => panel.contains(el));
      expect(firstInPanel).toBeGreaterThanOrEqual(0);
      expect(firstInPanel).toBeLessThan(stops.indexOf(zoomIn));
    } finally {
      media.restore();
    }
  });
});
