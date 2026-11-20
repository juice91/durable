// AC.17 acceptance (tester): the trace Sheet and Metrics page with data shaped like real worker runs
// (fallback chain, approval states, output retry, eval case and judge). Complements
// capabilitySpans.test.tsx and fallbackKpis.test.tsx (kept): each section's rows, statuses in the
// Sheet, the totals split, the Fallback KPI card on the page, the eval filter, 375 px and axe.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { useState } from "react";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import MetricsPage from "@/app/metrics/page";
import { SpanDetail } from "./SpanDetail";
import { TraceSheet } from "./TraceSheet";
import { TraceTotals } from "./TraceTotals";
import { summaryFixture, timeseriesFixture, breakdownFixture } from "@/components/metrics/metricsFixtures";
import { getTrace, type Trace, type TraceSpan } from "@/lib/traces";
import { searchLogs } from "@/components/logs/logsQuery";
import { RUN_KIND_LABELS } from "@/lib/runs";
import type { TraceTab } from "@/lib/routes/observability";
import type { MetricValues, UsageValues } from "@/lib/metrics";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { optionsOf } from "@/test/select";

const replace = vi.fn();
let search = "";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace }),
  useSearchParams: () => new URLSearchParams(search),
  usePathname: () => "/metrics",
  redirect: vi.fn(),
}));
vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()),
  searchLogs: vi.fn(),
}));
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<object>()), getTrace: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  const list = vi.fn().mockResolvedValue([{ id: "a-1", name: "Support bot" }]);
  return { ...original, api: { ...original.api, agents: { ...original.api.agents, list } } };
});
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));

const SRC = join(__dirname, "..", "..");
const T = (s: number) => new Date(Date.UTC(2026, 10, 15, 9, 0, s)).toISOString();
const U = { input_tokens: 40, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, usage_measured: true };

function span(kind: TraceSpan["kind"], attributes: TraceSpan["attributes"], extra: Partial<TraceSpan> = {}): TraceSpan {
  return {
    id: `s-${kind}`, parent_id: null, kind, name: kind, start: T(1), end: T(2), status: "ok",
    attributes, input: null, output: null, truncated: {}, ...extra,
  };
}

// Shapes taken from GET /runs/{id}/trace of real scripted runs (fail-503 then priced, a gated tool).
const REAL_MODEL = span("model_request", {
  request_key: "model:0", model_request: 0, model_id: "test:priced", provider_model_name: "scripted-priced",
  cost_usd: "0.0200000000", price_source: "override", usage: { ...U, input_tokens: 1000, output_tokens: 500 },
  tool_calls: 0, answering_model: "test:priced", requested_model_id: "test:fail-503", fallback_index: 1,
  failed_attempts: [{ model_id: "test:fail-503", outcome: "error", error_type: "ModelHTTPError", http_status: 503 }],
}, { name: "test:priced" });
const REAL_ATTEMPT = span("fallback_attempt", {
  request_key: "model:0:failed:0", model_request: 0, fallback_position: 0, model_id: "test:fail-503",
  outcome: "error", error_type: "ModelHTTPError", http_status: 503, timed: true, usage: null,
}, { status: "error", name: "test:fail-503" });
const approval = (status: TraceSpan["status"], decision: Record<string, unknown> | undefined, extra: Partial<TraceSpan> = {}) =>
  span("tool_approval", { tool_call_id: "c1", tool_name: "tickets__create", deadline: T(3600), waiting_since: T(1), waited: true, decision }, { status, ...extra });

describe("SpanDetail with real-run shapes", () => {
  afterEach(cleanup);

  it("a fallen-back request: answering model, requested model, the attempt line and no raw keys repeated", () => {
    render(<SpanDetail span={REAL_MODEL} />);
    const s = screen.getByTestId("span-fallback");
    expect(within(s).getByTestId("span-answering-model")).toHaveTextContent("test:priced");
    expect(s).toHaveTextContent("test:fail-503");
    expect(within(s).getByTestId("span-fallback-attempt")).toHaveTextContent("HTTP 503");
    expect(screen.queryByTestId("span-output-candidate")).toBeNull();
    expect(screen.getByTestId("span-input-tokens")).toHaveTextContent("1,000");
  });

  it("a failed attempt with no usage (a 503) shows the failure and says not recorded", () => {
    render(<SpanDetail span={REAL_ATTEMPT} />);
    expect(screen.getByTestId("span-attempt-failure")).toHaveTextContent("ModelHTTPError, HTTP 503");
    expect(screen.getByTestId("span-input-tokens")).toHaveTextContent("not recorded");
    expect(screen.getByTestId("span-status")).toHaveTextContent("Error");
  });

  it.each([
    ["waiting", { status: "waiting" as const, end: null }, undefined, "Waiting"],
    ["incomplete", { status: "incomplete" as const, end: null }, undefined, "Incomplete"],
    ["timed out", { status: "error" as const }, { type: "tool_approval_timed_out", decided_by: null, at: T(3) }, "Error"],
    ["approved", {}, { type: "tool_approved", decided_by: "alice", comment: null, at: T(3) }, "OK"],
  ] as [string, Partial<TraceSpan>, Record<string, unknown> | undefined, string][])("approval %s: status and decision", (_n, extra, decision, status) => {
    render(<SpanDetail span={approval(extra.status ?? "ok", decision, extra)} />);
    expect(screen.getByTestId("span-status")).toHaveTextContent(new RegExp(status, "i"));
    const row = screen.getByTestId("span-approval-decision");
    if (decision) expect(row.textContent).toMatch(/approved|timed out/i);
    else expect(row.textContent).toMatch(/waiting|none/i);
    expect(screen.getByTestId("span-tool-approval")).toHaveTextContent("tickets__create");
  });

  it("an approval never shows tool arguments or a comment that is absent", () => {
    render(<SpanDetail span={approval("ok", { type: "tool_approved", decided_by: "alice", comment: null })} />);
    expect(screen.getByTestId("span-tool-approval").textContent).not.toMatch(/Laptop|body|args/i);
    expect(screen.queryByText("Comment")).toBeNull();
  });
});

// ------------------------------------------------------------------ the trace Sheet

const EVAL_TOTALS = {
  runs: 3, model_requests: 4, tool_calls: 0, input_tokens: 2400, output_tokens: 1100, cache_read_tokens: 0,
  cache_write_tokens: 0, cost_usd: "0.0408000000", unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 3000,
  status: "completed", chat_requests: 2, chat_input_tokens: 2000, chat_output_tokens: 1000, chat_cost_usd: "0.0400000000",
  judge_requests: 2, judge_input_tokens: 400, judge_output_tokens: 100, judge_cost_usd: "0.0008000000",
  embedding_requests: 0, memory_requests: 0, embedding_input_tokens: 0, embedding_cost_usd: "0",
};
function evalTrace(): Trace {
  return {
    run: {
      run_id: "ev1", kind: "eval", agent_id: "a-1", version: 1, tool_id: null, workflow_id: null, workflow_version: null,
      knowledge_base_id: null, status: "completed", display_status: "completed", input: "{}", output: "{}", error: null,
      heartbeat_at: null, created_at: T(0), updated_at: T(9), trace_id: null, conversation_id: null, turn: null,
    },
    spans: [
      span("run", { run_kind: "eval", run_id: "ev1", judge_requests: 2 }, { id: "run:ev1", name: "eval ev1", start: T(0), end: T(9) }),
      span("eval_case", { case_id: "k1", case_status: "passed", position: 0, assertions: { passed: 2, total: 2 }, child_run_id: "ch1" },
        { id: "ev1:case:k1", parent_id: "run:ev1", name: "case k1", start: T(1), end: T(4) }),
      span("child_run", { run_id: "ch1" }, { id: "ch1", parent_id: "ev1:case:k1", name: "agent run", start: T(1), end: T(3) }),
      span("judge", { request_key: "judge:k1:1", case_id: "k1", evaluator_index: 1, model_id: "test:judge", usage: { ...U, input_tokens: 200, output_tokens: 50 }, cost_usd: "0.0004" },
        { id: "ev1:judge:k1:1", parent_id: "ev1:case:k1", name: "test:judge", start: T(4), end: T(4) }),
    ],
    totals: EVAL_TOTALS,
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  } as unknown as Trace;
}
function View() {
  const [tab, setTab] = useState<TraceTab>("spans");
  return <TraceSheet runId="ev1" open tab={tab} onTabChange={setTab} onOpenChange={() => undefined} />;
}

describe("trace Sheet of an eval run", () => {
  beforeEach(() => {
    vi.mocked(getTrace).mockReset().mockResolvedValue(evalTrace());
    vi.mocked(searchLogs).mockReset().mockReturnValue(new Promise(() => undefined));
  });
  afterEach(cleanup);

  it("labels the run as an eval and lists the case, its run and the judge request nested in the tree", async () => {
    render(<View />);
    await screen.findByText(/Case 1: passed/);
    expect(RUN_KIND_LABELS.eval).toBe("Eval");
    expect(screen.getByText(/Judge \(evaluator 2\): test:judge/)).toBeInTheDocument();
  });

  it("selecting the judge span shows its section and usage; selecting the case links its run", async () => {
    render(<View />);
    fireEvent.click(await screen.findByText(/Judge \(evaluator 2\)/));
    expect(await screen.findByTestId("span-judge")).toHaveTextContent("judge:k1:1");
    expect(screen.getByTestId("span-usage")).toHaveTextContent("test:judge");
    fireEvent.click(screen.getByText(/Case 1: passed/));
    expect(await screen.findByTestId("span-eval-case-assertions")).toHaveTextContent("2 of 2 passed");
    expect(screen.getByTestId("span-link-case-run")).toBeInTheDocument();
  });

  it("the run span's usage splits out the judge requests", () => {
    render(<SpanDetail span={span("run", { run_kind: "eval", requests: 4, input_tokens: 2400, output_tokens: 1100, chat_requests: 2, chat_input_tokens: 2000, chat_output_tokens: 1000, judge_requests: 2, judge_input_tokens: 400, judge_output_tokens: 100 })} />);
    expect(screen.getByTestId("span-usage-judge")).toHaveTextContent("400");
  });

  it("totals split the judge's usage from the chat model's and the parts add up", () => {
    render(<TraceTotals totals={EVAL_TOTALS as unknown as Trace["totals"]} />);
    expect(screen.getByTestId("totals-usage-judge")).toHaveTextContent("400");
    const t = EVAL_TOTALS;
    expect(t.chat_input_tokens + t.judge_input_tokens + t.embedding_input_tokens).toBe(t.input_tokens);
  });
});

// ------------------------------------------------------------------ the Metrics page

const usage = (o: Partial<UsageValues> = {}): UsageValues => ({
  requests: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, cost_usd: "0",
  unpriced_requests: 0, unmeasured_requests: 0, ...o,
});
const OPS = {
  chat: usage({ requests: 3, input_tokens: 2000 }), moderation: usage(), observer: usage(), reflector: usage(),
  embedding: usage(), memory: usage(), judge: usage({ requests: 2, input_tokens: 400, output_tokens: 100, cost_usd: "0.0008000000" }),
};
// Review 2026-11-15 shape: (fell_back + exhausted) / (chat_requests + exhausted) = 2 / 8.
const FB = { chat_requests: 7, fell_back: 1, billed_failed_attempts: 2, exhausted_runs: 1, fallback_rate: 0.25 };
const SUMMARY = (() => {
  const s = summaryFixture({ runs: 6, completed: 5, failed: 1, requests: 5, input_tokens: 2400 }, { runs: 4, requests: 3 });
  return {
    ...s,
    current: { ...s.current, operations: OPS, fallback: FB } as MetricValues,
    previous: { ...s.previous!, fallback: { ...FB, fell_back: 0, fallback_rate: 0, billed_failed_attempts: 0, exhausted_runs: 0 } } as MetricValues,
  };
})();

let calls: URL[] = [];
function mockFetch(summary: unknown = SUMMARY) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const url = new URL(input);
    calls.push(url);
    const p = url.pathname;
    let body: unknown = { items: [], next_before: null };
    if (p === "/api/v1/models") body = { items: [{ id: "gpt-4o", provider: "openai" }] };
    else if (p === "/api/v1/workflows") body = [];
    else if (p.endsWith("/summary")) body = summary;
    else if (p.endsWith("/timeseries")) body = timeseriesFixture([{ runs: 2 }, { runs: 3 }, { runs: 1 }]);
    else if (p.endsWith("/breakdown")) body = breakdownFixture(url.searchParams.get("by") === "agent" ? "agent" : "model", [{ key: url.searchParams.get("by") === "agent" ? "a-1" : "gpt-4o", runs: 3 }]);
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
}

describe("Metrics page with the AC.17 data", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    replace.mockReset();
    search = "";
    mockFetch();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    document.documentElement.classList.remove("dark");
  });

  it("shows the Fallback KPI card with its numbers and a worse delta, after the operation KPIs", async () => {
    render(<MetricsPage />);
    const block = await screen.findByTestId("metrics-fallback-kpis");
    expect(within(block).getByTestId("kpi-fallback_rate-value")).toHaveTextContent("25%");
    expect(within(block).getByTestId("kpi-fallback_failed_attempts-value")).toHaveTextContent("2");
    expect(within(block).getByTestId("kpi-fallback_exhausted-value")).toHaveTextContent("1");
    const ops = screen.getByTestId("metrics-operation-kpis");
    expect(ops.compareDocumentPosition(block) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps the block for operation=chat and hides it for another operation", async () => {
    search = "operation=chat";
    const first = render(<MetricsPage />);
    await screen.findByTestId("metrics-fallback-kpis");
    first.unmount();
    search = "operation=judge";
    mockFetch();
    render(<MetricsPage />);
    await screen.findByTestId("kpi-runs-value");
    expect(screen.queryByTestId("metrics-fallback-kpis")).toBeNull();
  });

  it("an older API without the fallback block shows none", async () => {
    const old = { ...SUMMARY, current: { ...SUMMARY.current, fallback: undefined }, previous: { ...SUMMARY.previous, fallback: undefined } };
    mockFetch(old);
    render(<MetricsPage />);
    await screen.findByTestId("kpi-runs-value");
    expect(screen.queryByTestId("metrics-fallback-kpis")).toBeNull();
  });

  it("the Kind filter offers Eval, writes kind=eval to the URL and sends it to every call", async () => {
    render(<MetricsPage />);
    await screen.findByTestId("kpi-runs-value");
    const kind = screen.getByLabelText("Kind") as HTMLSelectElement;
    expect(optionsOf(kind).map((o) => o.textContent)).toContain("Eval");
    fireEvent.change(kind, { target: { value: "eval" } });
    expect(new URL(replace.mock.calls[0][0] as string, "http://x").searchParams.get("kind")).toBe("eval");
  });

  it("kind=eval in the URL reaches the summary, timeseries and breakdown requests and the Kind select", async () => {
    search = "kind=eval";
    mockFetch();
    render(<MetricsPage />);
    await waitFor(() => expect(calls.filter((u) => u.pathname.startsWith("/api/v1/metrics/")).length).toBeGreaterThanOrEqual(4));
    for (const u of calls.filter((c) => c.pathname.startsWith("/api/v1/metrics/"))) expect(u.searchParams.get("kind")).toBe("eval");
    expect((screen.getByLabelText("Kind") as HTMLSelectElement).value).toBe("eval");
  });

  it("Usage by operation lists the Judge row with its requests", async () => {
    render(<MetricsPage />);
    const table = await screen.findByTestId("usage-by-operation");
    expect(within(table).getByText("Judge")).toBeInTheDocument();
    expect(within(table).getByText("Judge").closest("tr")).toHaveTextContent("2");
  });
});

// ------------------------------------------------------------------ 375 px, guards, axe

describe("phone width (by classes) and guards for the AC.17 files", () => {
  const FILES = [
    "components/traces/CapabilitySpanSections.tsx", "components/traces/SpanDetail.tsx", "components/traces/TraceTotals.tsx",
    "components/traces/spanStyles.ts", "components/metrics/FallbackKpis.tsx", "components/metrics/UsageByOperation.tsx",
    "components/metrics/BreakdownTable.tsx", "components/metrics/MetricsFilterBar.tsx", "components/metrics/overview/MetricsOverview.tsx",
  ];
  const read = (f: string) => readFileSync(join(SRC, f), "utf8");

  it("no fixed or minimum width over 400 px", () => {
    const offenders: string[] = [];
    const re = /(?<![\w-])(?:min-)?w-\[(\d+(?:\.\d+)?)(px|rem)\]|(?<![\w-])(?:min-)?w-(\d+)(?![\w./\[])|width:\s*(\d+(?:\.\d+)?)(px|rem)?|minWidth:\s*(\d+)/g;
    for (const f of FILES) {
      for (const m of read(f).matchAll(re)) {
        const v = m[1] ? Number(m[1]) * (m[2] === "rem" ? 16 : 1) : m[3] ? Number(m[3]) * 4 : m[4] ? Number(m[4]) * (m[5] === "rem" ? 16 : 1) : Number(m[6]);
        if (v > 400) offenders.push(`${f}: ${m[0]} = ${v}px`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it("the Fallback KPI row auto-fits and the new section text wraps instead of widening the Sheet", () => {
    expect(read("components/metrics/FallbackKpis.tsx")).toMatch(/KPI_GRID|auto-fit/);
    expect(read("components/metrics/FallbackKpis.tsx")).not.toMatch(/(?<![\w-])(?:sm:|md:|lg:)?grid-cols-[1-9]\b/);
    const text = read("components/traces/CapabilitySpanSections.tsx");
    expect(text).toContain("min-w-0");
    expect(text).toMatch(/break-(words|all)/);
    expect(text).not.toMatch(/whitespace-nowrap/);
  });

  it("long model ids, tool names and error paths render in wrapping elements", () => {
    const long = "x".repeat(300);
    render(<SpanDetail span={span("output_retry", { errors: [`$.${long}`], error_count: 1, model_request: 0, cause: long })} />);
    const paths = screen.getByTestId("span-output-retry-paths");
    expect(paths.className + paths.innerHTML).toMatch(/break-/);
    cleanup();
  });

  it("the new files hold no raw route literals, hex colours, innerHTML or console calls", () => {
    for (const f of FILES) {
      const text = read(f);
      expect(text, f).not.toMatch(/dangerouslySetInnerHTML|console\.(log|debug)/);
      expect(text, f).not.toMatch(/#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b(?![\w-])/);
      expect(text, f).not.toMatch(/["'`]\/(agents|runs|traces|metrics|evals)\//);
    }
  });

  it("eval is a run kind label (the Traces kind options are built from it)", () => {
    expect(Object.keys(RUN_KIND_LABELS)).toContain("eval");
  });
});

describe("accessibility of the AC.17 surfaces", () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    document.documentElement.classList.remove("dark");
  });

  it.each(["light", "dark"])("every capability section in the %s theme", async (theme) => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    const spans: TraceSpan[] = [
      REAL_MODEL, REAL_ATTEMPT, approval("waiting", undefined, { end: null }),
      approval("error", { type: "tool_approval_timed_out", decided_by: null, at: T(3) }),
      span("output_retry", { errors: ["$.a"], error_count: 1, model_request: 0, cause: "schema" }),
      span("eval_case", { case_id: "k1", case_status: "failed", position: 0, assertions: { passed: 1, total: 2 }, child_run_id: "ch1" }, { status: "error" }),
      span("judge", { request_key: "judge:k1:0", case_id: "k1", evaluator_index: 0, model_id: "test:judge", usage: U, cost_usd: "0.0004" }),
    ];
    for (const s of spans) {
      const { container, unmount } = render(<SpanDetail span={s} />);
      await expectNoAxeViolations(container);
      unmount();
    }
  }, AXE_TIMEOUT_MS);

  it.each(["light", "dark"])("the Metrics page with the Fallback block and kind=eval in the %s theme", async (theme) => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    document.documentElement.classList.toggle("dark", theme === "dark");
    search = "kind=eval";
    mockFetch();
    render(<MetricsPage />);
    await screen.findByTestId("metrics-fallback-kpis");
    await screen.findByTestId("usage-by-operation");
    await waitFor(() => expect(screen.getByTestId("breakdown-row-model-gpt-4o")).toBeInTheDocument());
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
