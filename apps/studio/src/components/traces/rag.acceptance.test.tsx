// RM.19 acceptance (tester): RAG and memory in traces and Metrics. Complements ragSpans.test.tsx,
// operations.test.tsx and EventTimeline.rag.test.tsx (kept): every span kind's detail, hit links
// only through isSafeHttpUrl, the Open knowledge base meta-row link, the event rows, the Metrics
// Operation filter in the URL, the operation KPIs and Usage by operation (with its drill-down), the
// kb_ingest kind, the exact request count, phone width by classes, and axe in both themes.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { useState } from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import MetricsPage from "@/app/metrics/page";
import { SpanDetail } from "./SpanDetail";
import { TraceSheet } from "./TraceSheet";
import { EventTimeline } from "@/components/runs/EventTimeline";
import { summaryFixture, timeseriesFixture, breakdownFixture } from "@/components/metrics/metricsFixtures";
import { getTrace, parseTraceFilters, type Trace, type TraceSpan } from "@/lib/traces";
import { RUN_KIND_LABELS, type RunEvent } from "@/lib/runs";
import { listKnowledgeBases } from "@/lib/knowledge";
import { resetKnowledgeBaseNames } from "@/components/knowledge/useKnowledgeBaseNames";
import { searchLogs } from "@/components/logs/logsQuery";
import { isSafeHttpUrl } from "@/lib/safeUrl";
import { routes } from "@/lib/routes";
import type { TraceTab } from "@/lib/routes/observability";
import type { UsageValues } from "@/lib/metrics";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { optionsOf } from "@/test/select";

const replace = vi.fn();
const push = vi.fn();
let search = "";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
  useSearchParams: () => new URLSearchParams(search),
  usePathname: () => "/metrics",
  redirect: vi.fn(),
}));
vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()),
  searchLogs: vi.fn(),
}));
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<object>()), getTrace: vi.fn() }));
vi.mock("@/lib/knowledge", async (importOriginal) => ({ ...(await importOriginal<object>()), listKnowledgeBases: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  const list = vi.fn().mockResolvedValue([{ id: "a-1", name: "Support bot" }]);
  return { ...original, api: { ...original.api, agents: { ...original.api.agents, list } } };
});
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));

const SRC = join(__dirname, "..", "..");
const T = (s: number) => new Date(Date.UTC(2026, 10, 14, 9, 0, s)).toISOString();

function span(kind: TraceSpan["kind"], attributes: TraceSpan["attributes"], extra: Partial<TraceSpan> = {}): TraceSpan {
  return {
    id: `s-${kind}`, parent_id: null, kind, name: kind, start: T(1), end: T(2), status: "ok",
    attributes, input: null, output: null, truncated: {}, ...extra,
  };
}

const usage = (o: Partial<UsageValues> = {}): UsageValues => ({
  requests: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, cost_usd: "0",
  unpriced_requests: 0, unmeasured_requests: 0, ...o,
});

const U = { input_tokens: 12, output_tokens: 3, cache_read_tokens: 0, cache_write_tokens: 0, usage_measured: true };

describe("SpanDetail renders each new span kind", () => {
  const CASES: [string, TraceSpan, string, string[]][] = [
    ["retrieve", span("retrieve", { query: "q?", hits: [], hit_count: 0, top_k: 4 }), "span-retrieve", ["Query", "Top k", "Hits"]],
    ["embedding", span("embedding", { dimensions: 768, inputs: 3, usage: U, model_id: "test:hash", cost_usd: "0.0000120000" }), "span-embedding", ["Dimensions", "Inputs"]],
    ["embedding batch", span("embedding", { batch: 1, chunks: 3, embedded: 6, total: 9 }), "span-embedding", ["Batch", "Chunks", "Embedded"]],
    ["memory_recall", span("memory_recall", { hits: 2, turns: 1, groups: 1, messages: 4 }), "span-embedding", ["Hits", "Turns", "Groups", "Messages added"]],
    ["memory_index", span("memory_index", { turn: 5, inserted: 2, existing: 1, redacted: 0 }), "span-embedding", ["Turn", "Inserted", "Already indexed", "Redacted"]],
    ["observer", span("observer", { memory_kind: "observation", covers_from_turn: 1, covers_to_turn: 4, notes: 3, approx_tokens: 90 }), "span-observer", ["Memory kind", "Covers turns", "Notes"]],
    ["reflector", span("reflector", { memory_kind: "reflection", superseded: 2, covers_from_turn: 0, covers_to_turn: 4 }), "span-observer", ["Superseded"]],
    ["working_memory", span("working_memory", { scope: "thread", format: "markdown", version: 2, changed_keys: ["a"] }), "span-working-memory", ["Scope", "Format", "Version", "Changed keys"]],
    ["ingest", span("ingest", { phase: "finalized", generation: 2, document_id: "d1", doc_version: 3 }), "span-ingest", ["Phase", "Generation", "Document"]],
  ];

  it.each(CASES)("%s: its section and rows", (_name, s, testId, labels) => {
    render(<SpanDetail span={s} />);
    const section = screen.getByTestId(testId);
    for (const label of labels) expect(within(section).getByText(label)).toBeInTheDocument();
  });

  it("the observer and reflector sections are titled apart", () => {
    const { unmount } = render(<SpanDetail span={span("observer", {})} />);
    expect(screen.getByRole("region", { name: "Observation" })).toBeInTheDocument();
    unmount();
    render(<SpanDetail span={span("reflector", {})} />);
    expect(screen.getByRole("region", { name: "Reflection" })).toBeInTheDocument();
  });

  it.each(["embedding", "memory_recall", "memory_index", "observer", "reflector"] as const)("%s shows model and usage, working memory and ingest do not", (kind) => {
    render(<SpanDetail span={span(kind, { model_id: "test:hash", usage: U, cost_usd: "0.0000120000" })} />);
    expect(screen.getByTestId("span-usage")).toHaveTextContent("test:hash");
    expect(screen.getByTestId("span-input-tokens")).toHaveTextContent("12");
    expect(screen.getByTestId("span-cost")).toHaveTextContent("$0.000012");
  });

  it.each(["working_memory", "ingest", "retrieve"] as const)("%s has no usage block", (kind) => {
    render(<SpanDetail span={span(kind, {})} />);
    expect(screen.queryByTestId("span-usage")).toBeNull();
  });

  it("a failed recall shows its error code in the error block and no usage numbers", () => {
    render(<SpanDetail span={span("memory_recall", { stage: "recall", error_code: "embedding_timeout", usage: null, cost_usd: null }, { status: "error" })} />);
    expect(screen.getByTestId("span-error")).toHaveTextContent("embedding_timeout");
    expect(screen.queryByTestId("span-input-tokens")).toBeNull();
  });

  it("a retrieve span keeps the query and the model-visible result in Input and Output, as a tool call did", () => {
    render(<SpanDetail span={span("retrieve", { query: "q", hits: [], hit_count: 0 }, { input: { search_query: "q" }, output: "[1] Title\n> body" })} />);
    expect(screen.getByTestId("span-retrieve-query")).toHaveTextContent("q");
    expect(document.body).toHaveTextContent("search_query");
    expect(document.body).toHaveTextContent("[1] Title");
  });
});

describe("retrieve hit links go through isSafeHttpUrl only", () => {
  const hit = (n: number, url: string | null, ref = "kb:doc") => ({ n, title: `Hit ${n}`, url, ref, kb_id: "kb1", chunk_id: `c${n}`, score: 0.5 });
  const BAD = [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,<script>alert(1)</script>", "//evil.example/x",
    "vbscript:x", "file:///etc/passwd", "ftp://host/x", "/relative/path", "mailto:a@b.c", "not a url", "",
  ];

  it("agrees with the predicate on every URL", () => {
    for (const url of BAD) expect(isSafeHttpUrl(url), url).toBe(false);
    for (const url of ["http://a.example/x", "https://a.example/x#y"]) expect(isSafeHttpUrl(url), url).toBe(true);
  });

  it("only http(s) hits are links; the others show their title and ref as text", () => {
    const hits = [...BAD.map((u, i) => hit(i + 1, u, `ref-${i + 1}`)), hit(20, "https://a.example/ok"), hit(21, "http://b.example/ok"), hit(22, null, "kb:faq")];
    render(<SpanDetail span={span("retrieve", { query: "q", hits, hit_count: hits.length })} />);
    expect(screen.getAllByTestId("span-retrieve-hit")).toHaveLength(hits.length);
    const links = screen.getAllByTestId("span-retrieve-hit-link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["https://a.example/ok", "http://b.example/ok"]);
    links.forEach((a) => expect(a).toHaveAttribute("rel", "noopener noreferrer"));
    for (const a of document.querySelectorAll("a[href]")) expect(a.getAttribute("href")).toMatch(/^https?:\/\//);
    expect(screen.getByText("Hit 1")).toBeInTheDocument();
    expect(screen.getByText("Hit 1").closest("a")).toBeNull();
    expect(screen.getAllByTestId("span-retrieve-hit")[0]).toHaveTextContent("ref-1");
    expect(document.body.innerHTML).not.toMatch(/href="(javascript|data|vbscript|file|\/\/)/i);
  });

  it("a hit with a malformed shape neither throws nor links", () => {
    const hits = [{ title: 5, url: { x: 1 }, score: "high" }, null, "str", { url: "https://ok.example/" }];
    render(<SpanDetail span={span("retrieve", { query: 7, hits, top_k: "x" })} />);
    expect(screen.getAllByTestId("span-retrieve-hit").length).toBeGreaterThan(0);
    expect(screen.getAllByTestId("span-retrieve-hit-link")).toHaveLength(1);
  });
});

// ------------------------------------------------------------------ the trace Sheet meta row

const RUN = {
  run_id: "r1", kind: "kb_ingest" as const, agent_id: null, version: null, tool_id: null, workflow_id: null, workflow_version: null,
  knowledge_base_id: "kb1", status: "completed", display_status: "completed" as const, input: "{}", output: "{}", error: null,
  heartbeat_at: null, created_at: T(0), updated_at: T(9), trace_id: null, conversation_id: null, turn: null,
};
const TOTALS = {
  runs: 1, model_requests: 1, tool_calls: 0, input_tokens: 900, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0,
  cost_usd: "0.0009000000", unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 9000, status: "completed",
  embedding_requests: 1, memory_requests: 0, embedding_input_tokens: 900, embedding_cost_usd: "0.0009000000",
};
function ingestTrace(runExtra: Record<string, unknown> = {}): Trace {
  return {
    run: { ...RUN, ...runExtra } as Trace["run"],
    spans: [
      span("run", { run_kind: "kb_ingest", run_id: "r1", knowledge_base_id: "kb1", ingest_mode: "ingest" }, { id: "run:r1", name: "knowledge base kb1 ingest", start: T(0), end: T(9) }),
      span("ingest", { phase: "chunked", embedded: 0, total: 3 }, { id: "i1", parent_id: "run:r1", name: "chunked" }),
      span("embedding", { batch: 0, chunks: 3, model_id: "test:hash", usage: U }, { id: "e1", parent_id: "run:r1", name: "embedding batch 0", start: T(2), end: T(5) }),
    ],
    totals: TOTALS,
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  } as Trace;
}

function View({ runId = "r1" }: { runId?: string }) {
  const [tab, setTab] = useState<TraceTab>("spans");
  return <TraceSheet runId={runId} open tab={tab} onTabChange={setTab} onOpenChange={() => undefined} />;
}

describe("trace Sheet: Open knowledge base meta-row link", () => {
  beforeEach(() => {
    vi.mocked(getTrace).mockReset();
    vi.mocked(searchLogs).mockReset();
    vi.mocked(searchLogs).mockReturnValue(new Promise(() => undefined));
    vi.mocked(listKnowledgeBases).mockReset();
    resetKnowledgeBaseNames();
  });
  afterEach(cleanup);

  it("links a kb_ingest run to its knowledge base's Ingest runs tab, named once the names load", async () => {
    vi.mocked(getTrace).mockResolvedValue(ingestTrace());
    vi.mocked(listKnowledgeBases).mockResolvedValue([{ id: "kb1", name: "Product docs" }] as Awaited<ReturnType<typeof listKnowledgeBases>>);
    render(<View />);
    const link = await screen.findByTestId("trace-sheet-subject-link");
    expect(link).toHaveAttribute("href", routes.knowledgeBase("kb1", { tab: "runs" }));
    await waitFor(() => expect(link).toHaveTextContent("Open knowledge base: Product docs"));
    expect(screen.getByTestId("trace-sheet-meta")).toHaveTextContent("Knowledge ingest run");
  });

  it("still links without the name when the names fail to load", async () => {
    vi.mocked(getTrace).mockResolvedValue(ingestTrace());
    vi.mocked(listKnowledgeBases).mockRejectedValue(new Error("down"));
    render(<View />);
    const link = await screen.findByTestId("trace-sheet-subject-link");
    await waitFor(() => expect(listKnowledgeBases).toHaveBeenCalled());
    expect(link).toHaveAttribute("href", routes.knowledgeBase("kb1", { tab: "runs" }));
    expect(link).toHaveTextContent(/^Open knowledge base$/);
  });

  it("has no link when the run names no knowledge base, and does not fetch the names for an agent run without retrieve", async () => {
    vi.mocked(getTrace).mockResolvedValue(ingestTrace({ knowledge_base_id: null }));
    vi.mocked(listKnowledgeBases).mockResolvedValue([]);
    const first = render(<View />);
    await screen.findAllByTestId("trace-span-row");
    expect(screen.queryByTestId("trace-sheet-subject-link")).toBeNull();
    first.unmount();
    vi.mocked(listKnowledgeBases).mockClear();
    resetKnowledgeBaseNames();
    const agent = ingestTrace({ kind: "agent", agent_id: "a-1", knowledge_base_id: null });
    agent.spans = [span("run", { run_kind: "agent" }, { id: "run:r1" }), span("model_request", {}, { id: "m1", parent_id: "run:r1" })];
    vi.mocked(getTrace).mockResolvedValue(agent);
    render(<View />);
    await screen.findAllByTestId("trace-span-row");
    expect(listKnowledgeBases).not.toHaveBeenCalled();
  });

  it("the selected run span carries the same link and the ingest and embedding rows list in order", async () => {
    vi.mocked(getTrace).mockResolvedValue(ingestTrace());
    vi.mocked(listKnowledgeBases).mockResolvedValue([]);
    render(<View />);
    const rows = await screen.findAllByTestId("trace-span-row");
    expect(rows.map((r) => r.getAttribute("data-kind"))).toEqual(["run", "ingest", "embedding"]);
    fireEvent.click(rows[0]);
    expect(screen.getByTestId("span-link-knowledge-base")).toHaveAttribute("href", routes.knowledgeBase("kb1", { tab: "runs" }));
    fireEvent.click(rows[2]);
    expect(screen.getByTestId("span-embedding")).toHaveTextContent("Chunks");
    expect(screen.getByTestId("totals-cost")).toHaveTextContent("embeddings");
  });

  it("has no axe violations in either theme", async () => {
    vi.mocked(getTrace).mockResolvedValue(ingestTrace());
    vi.mocked(listKnowledgeBases).mockResolvedValue([{ id: "kb1", name: "Docs" }] as Awaited<ReturnType<typeof listKnowledgeBases>>);
    for (const theme of ["light", "dark"]) {
      document.documentElement.classList.toggle("dark", theme === "dark");
      const { unmount } = render(<View />);
      await screen.findAllByTestId("trace-span-row");
      await expectNoAxeViolations(document.body);
      unmount();
    }
    document.documentElement.classList.remove("dark");
  }, AXE_TIMEOUT_MS);
});

// ------------------------------------------------------------------ the Events tab rows

describe("EventTimeline rows for the RAG and memory events", () => {
  const MARK = "zq-leak-marker";
  let seq = 0;
  const ev = (type: RunEvent["type"], data: Record<string, unknown>): RunEvent => ({ seq: ++seq, type, data: data as RunEvent["data"], ts: "2026-11-14T09:00:00Z" });
  const leak = { content: MARK, text: MARK, query: MARK, messages: [{ text: MARK }], notes_text: MARK, value: { deep: MARK } };

  it("each row names its event type and keeps the type label in the header", () => {
    render(<EventTimeline events={[
      ev("embedding", { model_id: "m", inputs: 1, input_tokens: 1, stage: "index", turn: 2, inserted: 1, existing: 0 }),
      ev("memory_updated", { stage: "reflect", kind: "reflection", covers_from_turn: 0, covers_to_turn: 3, superseded: 2, model_id: "m" }),
      ev("ingest_progress", { phase: "finalized", generation: 2, document_id: "d1", doc_version: 4 }),
    ]} currentPartialText={{}} />);
    expect(screen.getByTestId("event-embedding")).toHaveTextContent("Memory index");
    expect(screen.getByTestId("event-embedding")).toHaveTextContent("inserted: 1");
    expect(screen.getByTestId("event-memory-observer")).toHaveTextContent("Observations reflected");
    expect(screen.getByTestId("event-ingest-progress")).toHaveTextContent(/finalized|ready|indexed/i);
    const headers = Array.from(document.querySelectorAll(".uppercase")).map((n) => n.textContent);
    expect(headers).toEqual(["embedding", "memory_updated", "ingest_progress"]);
  });

  it("tolerates events with no data at all and shows no raw JSON dump", () => {
    const { container } = render(<EventTimeline events={[ev("embedding", {}), ev("memory_updated", {}), ev("memory_observe_failed", {}), ev("ingest_progress", {})]} currentPartialText={{}} />);
    expect(container.querySelectorAll('[data-testid^="event-"]').length).toBeGreaterThanOrEqual(4);
    expect(container.querySelector("pre")).toBeNull();
  });

  it("never renders text, queries, messages or nested values these events should not carry", () => {
    const events = [
      ev("embedding", { model_id: "m", inputs: 1, input_tokens: 2, stage: "recall", hits: 1, ...leak }),
      ev("embedding", { stage: "index", status: "failed", error_code: "x", ...leak }),
      ev("memory_updated", { scope: "resource", version: 2, changed_keys: ["a"], ...leak }),
      ev("memory_updated", { stage: "observe", kind: "observation", covers_from_turn: 0, covers_to_turn: 1, ...leak }),
      ev("memory_observe_failed", { stage: "observe", error_code: "overlap", ...leak }),
      ev("ingest_progress", { phase: "chunked", total: 2, ...leak }),
      ev("ingest_progress", { phase: "embedding", batch: 0, embedded: 1, total: 2, ...leak }),
    ];
    const { container } = render(<EventTimeline events={events} currentPartialText={{}} />);
    expect(container).not.toHaveTextContent(MARK);
  });

  it.each(["light", "dark"])("has no axe violations in the %s theme", async (theme) => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    const { container } = render(<EventTimeline events={[
      ev("embedding", { model_id: "m", inputs: 1, input_tokens: 2, stage: "recall", hits: 1 }),
      ev("memory_updated", { stage: "observe", kind: "observation", covers_from_turn: 0, covers_to_turn: 1 }),
      ev("memory_observe_failed", { stage: "reflect", error_code: "overlap" }),
      ev("ingest_progress", { phase: "embedding", batch: 0, embedded: 1, total: 2 }),
    ]} currentPartialText={{}} />);
    await expectNoAxeViolations(container);
    document.documentElement.classList.remove("dark");
  }, AXE_TIMEOUT_MS);
});

// ------------------------------------------------------------------ the Metrics page

const OPS = {
  chat: usage({ requests: 10, input_tokens: 1000, output_tokens: 200, cost_usd: "0.0100000000" }),
  moderation: usage({ requests: 1, input_tokens: 5 }),
  observer: usage({ requests: 2, input_tokens: 300, output_tokens: 40, cost_usd: "0.0030000000" }),
  reflector: usage({ requests: 1, input_tokens: 100, output_tokens: 10, cost_usd: "0.0010000000" }),
  embedding: usage({ requests: 6, input_tokens: 600, cost_usd: "0.0006000000" }),
  memory: usage({ requests: 5, input_tokens: 450, output_tokens: 50, cost_usd: "0.0041000000" }),
};
const SUMMARY = (() => {
  const s = summaryFixture({ runs: 12, completed: 12, requests: 20, input_tokens: 2005, output_tokens: 250, cost_usd: "0.0146000000" }, { runs: 6, requests: 5 });
  return { ...s, current: { ...s.current, operations: OPS } };
})();

let calls: URL[] = [];
function mockMetricsFetch(summary: unknown = SUMMARY) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const url = new URL(input);
    calls.push(url);
    const p = url.pathname;
    let body: unknown = { items: [], next_before: null };
    if (p === "/api/v1/models") body = { items: [{ id: "gpt-4o", provider: "openai" }] };
    else if (p === "/api/v1/workflows") body = [];
    else if (p.endsWith("/summary")) body = summary;
    else if (p.endsWith("/timeseries")) body = timeseriesFixture([{ runs: 5 }, { runs: 3 }, { runs: 4 }]);
    else if (p.endsWith("/breakdown")) body = breakdownFixture(url.searchParams.get("by") === "agent" ? "agent" : "model", [{ key: url.searchParams.get("by") === "agent" ? "a-1" : "gpt-4o", runs: 3 }]);
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}
const metricCalls = () => calls.filter((u) => u.pathname.startsWith("/api/v1/metrics/"));
const hrefOf = (mock: typeof replace, n = 0) => new URL(mock.mock.calls[n][0] as string, "http://x");

describe("Metrics page: the Operation filter lives in the URL", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    replace.mockReset();
    push.mockReset();
    search = "";
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    document.documentElement.classList.remove("dark");
  });

  it("an old URL without operation sends no operation and writes none when another filter changes", async () => {
    search = "range=30d&agent_id=a-1&kind=agent";
    mockMetricsFetch();
    render(<MetricsPage />);
    await waitFor(() => expect(metricCalls()).toHaveLength(4));
    for (const u of calls) expect(u.searchParams.has("operation")).toBe(false);
    await screen.findByTestId("metrics-operation-kpis");
    await waitFor(() => expect(optionsOf(screen.getByLabelText("Model")).some((o) => o.value === "gpt-4o")).toBe(true));
    fireEvent.change(screen.getByLabelText("Model"), { target: { value: "gpt-4o" } });
    const href = hrefOf(replace);
    expect(Object.fromEntries(href.searchParams)).toEqual({ range: "30d", agent_id: "a-1", kind: "agent", model: "gpt-4o" });
  });

  it("choosing an Operation writes operation= to the URL and keeps the other filters", async () => {
    search = "range=30d&agent_id=a-1";
    mockMetricsFetch();
    render(<MetricsPage />);
    await screen.findByTestId("metrics-operation-kpis");
    fireEvent.change(screen.getByLabelText("Operation"), { target: { value: "reflector" } });
    expect(Object.fromEntries(hrefOf(replace).searchParams)).toEqual({ range: "30d", agent_id: "a-1", operation: "reflector" });
  });

  it("with operation=memory in the URL every metrics request carries it, the select shows it, and the KPI row is gone", async () => {
    search = "operation=memory";
    mockMetricsFetch();
    render(<MetricsPage />);
    await waitFor(() => expect(metricCalls().length).toBeGreaterThanOrEqual(4));
    await screen.findByTestId("kpi-runs-value");
    for (const u of metricCalls()) expect(u.searchParams.get("operation"), u.pathname).toBe("memory");
    expect(calls.filter((u) => u.pathname === "/api/v1/runs").every((u) => !u.searchParams.has("operation"))).toBe(true);
    expect((screen.getByLabelText("Operation") as HTMLSelectElement).value).toBe("memory");
    expect(screen.queryByTestId("metrics-operation-kpis")).toBeNull();
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("an unknown operation in the URL is ignored like no filter", async () => {
    search = "operation=bogus";
    mockMetricsFetch();
    render(<MetricsPage />);
    await screen.findByTestId("metrics-operation-kpis");
    for (const u of calls) expect(u.searchParams.has("operation")).toBe(false);
    expect((screen.getByLabelText("Operation") as HTMLSelectElement).value).toBe("");
  });

  it("Clear filters removes the operation from the URL", async () => {
    search = "operation=embedding&agent_id=a-1";
    mockMetricsFetch();
    render(<MetricsPage />);
    await screen.findByTestId("kpi-runs-value");
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(hrefOf(replace).searchParams.has("operation")).toBe(false);
  });
});

describe("Metrics page: operation KPIs, Usage by operation and its drill-down", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    replace.mockReset();
    search = "";
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    document.documentElement.classList.remove("dark");
  });

  it("shows the Embedding and memory KPI row beside the main KPIs only without an operation filter", async () => {
    mockMetricsFetch();
    const first = render(<MetricsPage />);
    const row = await screen.findByTestId("metrics-operation-kpis");
    expect(within(row).getByTestId("kpi-embedding_tokens-value")).toHaveTextContent("600");
    expect(within(row).getByTestId("kpi-embedding_cost-value")).toHaveTextContent("$0.00060");
    expect(within(row).getByTestId("kpi-memory_tokens-value")).toHaveTextContent("500");
    first.unmount();
    for (const op of ["chat", "embedding", "memory"]) {
      search = `operation=${op}`;
      mockMetricsFetch();
      const r = render(<MetricsPage />);
      await screen.findByTestId("kpi-runs-value");
      expect(screen.queryByTestId("metrics-operation-kpis"), op).toBeNull();
      r.unmount();
    }
  });

  it("lists the five operations in order, summing to the KPI totals, and not the memory union", async () => {
    mockMetricsFetch();
    render(<MetricsPage />);
    const section = await screen.findByTestId("usage-by-operation");
    const rows = within(section).getAllByTestId("usage-operation-row");
    expect(rows.map((r) => r.getAttribute("data-operation"))).toEqual(["chat", "embedding", "observer", "reflector", "moderation"]);
    const cells = (r: HTMLElement, col: number) => Number(within(r).getAllByRole("cell")[col].textContent!.replace(/[^0-9]/g, ""));
    expect(rows.reduce((n, r) => n + cells(r, 0), 0)).toBe(SUMMARY.current.requests);
    expect(rows.reduce((n, r) => n + cells(r, 1), 0)).toBe(SUMMARY.current.input_tokens);
  });

  it("drill-down: following an operation's link narrows every request and swaps the KPI row out", async () => {
    search = "range=30d&agent_id=a-1";
    mockMetricsFetch();
    const { rerender } = render(<MetricsPage />);
    const link = await screen.findByRole("link", { name: "Observer: show this operation's metrics" });
    const target = new URL(link.getAttribute("href")!, "http://x");
    expect(target.pathname).toBe(routes.metrics());
    expect(Object.fromEntries(target.searchParams)).toEqual({ range: "30d", agent_id: "a-1", operation: "observer" });
    search = target.search.slice(1);
    const before = metricCalls().length;
    rerender(<MetricsPage />);
    await waitFor(() => expect(metricCalls().length).toBeGreaterThan(before));
    for (const u of metricCalls().slice(before)) expect(u.searchParams.get("operation")).toBe("observer");
    await waitFor(() => expect(screen.queryByTestId("metrics-operation-kpis")).toBeNull());
    expect((screen.getByLabelText("Operation") as HTMLSelectElement).value).toBe("observer");
    const narrowed = screen.getAllByTestId("usage-operation-row").find((r) => r.getAttribute("data-operation") === "observer")!;
    expect(within(narrowed).queryByRole("link")).toBeNull();
  });

  it("the memory narrowing is reachable from the KPI cards, not from the table", async () => {
    mockMetricsFetch();
    render(<MetricsPage />);
    const row = await screen.findByTestId("metrics-operation-kpis");
    const hrefs = within(row).getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(hrefs).toContain("/metrics?operation=memory");
    expect(hrefs).toContain("/metrics?operation=embedding");
    expect(screen.queryByRole("link", { name: /Memory: show/ })).toBeNull();
  });

  it("without an operations split (an older API) nothing extra is drawn", async () => {
    mockMetricsFetch(summaryFixture({ runs: 12, requests: 3 }, {}));
    render(<MetricsPage />);
    await screen.findByTestId("kpi-runs-value");
    expect(screen.queryByTestId("metrics-operation-kpis")).toBeNull();
    expect(screen.queryByTestId("usage-by-operation")).toBeNull();
  });
});

describe("the kb_ingest kind", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    replace.mockReset();
    search = "";
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("is a Metrics Kind option, written to the URL and sent with every metrics request", async () => {
    mockMetricsFetch();
    const { rerender } = render(<MetricsPage />);
    await screen.findByTestId("metrics-operation-kpis");
    const kind = screen.getByLabelText("Kind") as HTMLSelectElement;
    expect(optionsOf(kind).find((o) => o.value === "kb_ingest")?.textContent).toBe("Knowledge ingest");
    fireEvent.change(kind, { target: { value: "kb_ingest" } });
    expect(hrefOf(replace).searchParams.get("kind")).toBe("kb_ingest");
    search = hrefOf(replace).search.slice(1);
    const before = metricCalls().length;
    rerender(<MetricsPage />);
    await waitFor(() => expect(metricCalls().length).toBeGreaterThan(before));
    for (const u of metricCalls().slice(before)) expect(u.searchParams.get("kind")).toBe("kb_ingest");
  });

  it("is a Traces kind: the label exists, the URL filter parses and reaches the runs query", () => {
    expect(RUN_KIND_LABELS.kb_ingest).toBe("Knowledge ingest");
    const filters = parseTraceFilters(new URLSearchParams("kind=kb_ingest"));
    expect(filters.kind).toBe("kb_ingest");
    expect(parseTraceFilters(new URLSearchParams("kind=nope")).kind).toBeFalsy();
  });
});

describe("exact request counts", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    search = "";
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("the operation row and Usage by operation add no request: five data requests, one per section", async () => {
    mockMetricsFetch();
    render(<MetricsPage />);
    await screen.findByTestId("usage-by-operation");
    await screen.findByTestId("metrics-operation-kpis");
    await waitFor(() => expect(screen.getByTestId("breakdown-row-model-gpt-4o")).toBeInTheDocument());
    const data = calls.filter((u) => u.pathname.startsWith("/api/v1/metrics/") || u.pathname === "/api/v1/runs");
    expect(data.map((u) => `${u.pathname.split("/").pop()}${u.searchParams.get("by") ? `:${u.searchParams.get("by")}` : ""}`).sort()).toEqual(
      ["breakdown:agent", "breakdown:model", "runs", "summary", "timeseries"],
    );
    expect(calls.some((u) => u.searchParams.get("by") === "operation")).toBe(false);
    expect(calls.some((u) => u.searchParams.get("compare") === null && u.pathname.endsWith("/summary"))).toBe(false);
  });
});

describe("phone width (by classes)", () => {
  const FILES = [
    "components/traces/RagSpanSections.tsx", "components/traces/SpanDetail.tsx", "components/metrics/OperationKpis.tsx",
    "components/metrics/UsageByOperation.tsx", "components/metrics/MetricsFilterBar.tsx", "components/runs/EventTimeline.tsx",
  ];
  const read = (f: string) => readFileSync(join(SRC, f), "utf8");
  const px = (n: number, unit: string) => (unit === "rem" ? n * 16 : n);

  it("no fixed or minimum width over 400 px in the files RM.19 added or changed", () => {
    const offenders: string[] = [];
    for (const f of FILES) {
      const re = /(?<![\w-])(?:min-)?w-\[(\d+(?:\.\d+)?)(px|rem)\]|(?<![\w-])(?:min-)?w-(\d+)(?![\w./\[])|width:\s*(\d+(?:\.\d+)?)(px|rem)?|minWidth:\s*(\d+)/g;
      for (const m of read(f).matchAll(re)) {
        const v = m[1] ? px(Number(m[1]), m[2]) : m[3] ? Number(m[3]) * 4 : m[4] ? px(Number(m[4]), m[5] ?? "px") : Number(m[6]);
        if (v > 400) offenders.push(`${f}: ${m[0]} = ${v}px`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("the Usage by operation table scrolls inside itself and the KPI row auto-fits like the main one", () => {
    expect(read("components/metrics/UsageByOperation.tsx")).toContain("overflow-x-auto");
    expect(read("components/metrics/UsageByOperation.tsx")).toContain("min-w-0");
    expect(read("components/metrics/OperationKpis.tsx")).toContain("className={KPI_GRID}"); // the main row's auto-fit grid
    expect(read("components/metrics/MetricsKpis.tsx")).toContain("grid-cols-[repeat(auto-fit,minmax(min(100%,11rem),1fr))]");
    expect(read("components/metrics/OperationKpis.tsx")).not.toMatch(/(?<![\w-])(?:sm:|md:|lg:)?grid-cols-[1-9]\b/);
  });

  it("long hit titles, refs, queries and key lists wrap instead of widening the Sheet", () => {
    const text = read("components/traces/RagSpanSections.tsx");
    expect(text).toContain("break-words");
    expect(text).toContain("break-all");
    expect(text).toContain("min-w-0");
    expect(text).not.toMatch(/whitespace-nowrap/);
  });

  it("a long unbroken query and title render inside min-w-0 wrappers", () => {
    const long = "x".repeat(400);
    render(<SpanDetail span={span("retrieve", { query: long, hits: [{ n: 1, title: long, url: null, ref: long, kb_id: "k", chunk_id: "c" }], hit_count: 1 })} />);
    for (const id of ["span-retrieve-query", "span-retrieve-hit"]) {
      const el = screen.getByTestId(id);
      expect((el.querySelector("[class*='break-']") ?? el.closest("[class*='break-']") ?? el.querySelector("dd"))?.className ?? "").toMatch(/break-/);
    }
    cleanup();
  });
});

describe("Metrics page accessibility with the RM.19 sections", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    search = "";
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    document.documentElement.classList.remove("dark");
  });

  it.each(["light", "dark"])("has no axe violations in the %s theme, unfiltered and narrowed", async (theme) => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    for (const qs of ["", "operation=embedding&kind=kb_ingest"]) {
      search = qs;
      mockMetricsFetch();
      const { unmount } = render(<MetricsPage />);
      await screen.findByTestId("usage-by-operation");
      await waitFor(() => expect(screen.getByTestId("breakdown-row-model-gpt-4o")).toBeInTheDocument());
      if (!qs) await screen.findByTestId("metrics-operation-kpis");
      await expectNoAxeViolations(document.body);
      unmount();
    }
  }, AXE_TIMEOUT_MS);
});
