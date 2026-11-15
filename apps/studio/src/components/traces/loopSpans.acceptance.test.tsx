// LP.15 acceptance (tester): the trace Sheet with loop, iteration and evaluate spans, an unknown or
// legacy span kind, hostile attributes, keyboard use, phone width and the colour rule.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { useState } from "react";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { SpanDetail } from "./SpanDetail";
import { TraceSheet } from "./TraceSheet";
import { getTrace, type Trace, type TraceSpan } from "@/lib/traces";
import { searchLogs } from "@/components/logs/logsQuery";
import type { TraceTab } from "@/lib/routes/observability";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { setWindowWidth } from "@/components/frame/frameTesting";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(""),
  usePathname: () => "/workflows/wf-1/runs/wr1",
  redirect: vi.fn(),
}));
vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()),
  searchLogs: vi.fn(),
}));
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<object>()), getTrace: vi.fn() }));

const T = (s: number) => new Date(Date.UTC(2026, 10, 15, 9, 0, s)).toISOString();
const LONG = "y".repeat(5000);

function span(kind: string, attributes: Record<string, unknown>, extra: Partial<TraceSpan> = {}): TraceSpan {
  return {
    id: `s-${kind}`, parent_id: null, kind: kind as TraceSpan["kind"], name: kind, start: T(1), end: T(3), status: "ok",
    attributes, input: null, output: null, truncated: {}, ...extra,
  } as TraceSpan;
}

function trace(spans: TraceSpan[]): Trace {
  return {
    run: {
      run_id: "wr1", kind: "workflow", agent_id: null, version: null, tool_id: null, workflow_id: "wf-1", workflow_version: 1,
      knowledge_base_id: null, status: "completed", display_status: "completed", input: "{}", output: "{}", error: null,
      heartbeat_at: null, created_at: T(0), updated_at: T(9), trace_id: null, conversation_id: null, turn: null,
    },
    spans: [span("run", { run_kind: "workflow" }, { id: "run:wr1", name: "workflow", start: T(0), end: T(9) }), ...spans],
    totals: {
      runs: 1, model_requests: 0, tool_calls: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0,
      cost_usd: "0", unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 9000, status: "completed",
      chat_requests: 0, chat_input_tokens: 0, chat_output_tokens: 0, chat_cost_usd: "0",
      judge_requests: 0, judge_input_tokens: 0, judge_output_tokens: 0, judge_cost_usd: "0",
      embedding_requests: 0, memory_requests: 0, embedding_input_tokens: 0, embedding_cost_usd: "0",
    },
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0, iterations: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  } as unknown as Trace;
}

function View() {
  const [tab, setTab] = useState<TraceTab>("spans");
  return <TraceSheet runId="wr1" open tab={tab} onTabChange={setTab} onOpenChange={() => undefined} />;
}

const loopSpan = span("loop", { step_id: "each", step_type: "foreach", iterations_started: 2, iterations_completed: 1, iterations_failed: 1, iterations_omitted: 0, exited: "weird_future_value" },
  { id: "wr1:step:each", parent_id: "run:wr1", name: "each" });
const iterSpan = span("loop_iteration", { step_id: "each", scope: "each.0", index: 0, iteration_status: "completed" },
  { id: "wr1:loop:each.0", parent_id: loopSpan.id, name: "each #0" });
const evalSpan = span("evaluate", { step_id: "ev", scope: "each.0", passed: true, score: 1, evaluators: 1, evaluators_passed: 1, evaluators_failed: 0, evaluators_skipped: 0, results: [{ name: "c", kind: "contains", passed: true, score: 1 }], judge_requests: 0 },
  { id: "wr1:step:each.0~ev", parent_id: iterSpan.id, name: "ev" });

describe("section rendering is defensive", () => {
  afterEach(cleanup);

  it("an unknown span kind renders its generic detail without a loop section", () => {
    render(<SpanDetail span={span("future_kind", { some_attr: "v" })} />);
    expect(screen.queryByTestId("span-loop")).toBeNull();
    expect(screen.queryByTestId("span-evaluate")).toBeNull();
    expect(screen.getByTestId("span-attributes")).toHaveTextContent("some_attr");
  });

  it("a legacy loop, iteration and evaluate span with no attributes renders", () => {
    for (const kind of ["loop", "loop_iteration", "evaluate"]) {
      const { unmount } = render(<SpanDetail span={span(kind, {})} />);
      expect(screen.getByTestId(kind === "loop" ? "span-loop" : kind === "evaluate" ? "span-evaluate" : "span-loop-iteration")).toBeInTheDocument();
      unmount();
    }
  });

  it("an evaluate span still shows as a plain workflow_step-like span when it has no step attributes", () => {
    render(<SpanDetail span={span("workflow_step", { step_id: "x", step_type: "evaluate", passed: true })} />);
    expect(screen.queryByTestId("span-evaluate")).toBeNull();
  });

  it("malformed attribute types do not throw and show no text from wrong-typed values", () => {
    const bad = span("evaluate", {
      passed: "yes", score: "high", evaluators: "3", results: [null, 5, "x", { name: { n: 1 }, kind: 7, passed: "no", score: NaN }, { name: LONG, kind: "k", passed: false, error_code: LONG }],
      judge_requests: "2", judge_input_tokens: null, judge_cost_usd: 5,
    });
    render(<SpanDetail span={bad} />);
    expect(screen.queryByTestId("span-evaluate-verdict")).toBeNull();
    expect(screen.queryByTestId("span-evaluate-score")).toBeNull();
    expect(screen.getAllByTestId("span-evaluate-result").length).toBe(2);
    expect(screen.getAllByTestId("span-evaluate-result")[1].innerHTML).toContain("break-words");
  });

  it("a loop with an unknown exit and failed iterations shows the raw exit and counts", () => {
    render(<SpanDetail span={loopSpan} />);
    expect(screen.getByTestId("span-loop-exited")).toHaveTextContent("weird_future_value");
    expect(screen.getByTestId("span-loop-failed")).toHaveTextContent("1");
    cleanup();
    render(<SpanDetail span={span("loop", { iterations_omitted: 1 })} />);
    expect(screen.getByTestId("span-loop-omitted")).toHaveTextContent("1 iteration is not shown");
  });

  it("a hostile string in an attribute is text only", () => {
    const html = `<img src=x onerror="window.__pwn=1">`;
    render(<SpanDetail span={span("loop_iteration", { step_id: html, scope: html, index: 0, iteration_status: html, until: html, failed_step_id: html })} />);
    expect(screen.getByTestId("span-loop-iteration").querySelector("img")).toBeNull();
    expect(screen.getByTestId("span-loop-iteration")).toHaveTextContent(html);
  });
});

describe("trace Sheet with the three kinds and a future kind", () => {
  let restoreWidth: () => void = () => undefined;
  beforeEach(() => {
    vi.mocked(getTrace).mockReset().mockResolvedValue(trace([loopSpan, iterSpan, evalSpan, span("future_kind", { some_attr: "v" }, { id: "wr1:future", parent_id: "run:wr1", name: "future thing" })]));
    vi.mocked(searchLogs).mockReset().mockReturnValue(new Promise(() => undefined));
  });
  afterEach(() => {
    cleanup();
    restoreWidth();
    restoreWidth = () => undefined;
  });

  it("each row selects and shows its own section; the unknown kind row selects without error", async () => {
    render(<View />);
    const rows = await screen.findAllByTestId("trace-span-row");
    expect(rows).toHaveLength(5);
    fireEvent.click(rows[1]);
    expect(await screen.findByTestId("span-loop")).toBeInTheDocument();
    fireEvent.click(rows[2]);
    expect(await screen.findByTestId("span-loop-iteration")).toBeInTheDocument();
    expect(screen.queryByTestId("span-loop")).toBeNull();
    fireEvent.click(rows[3]);
    expect(await screen.findByTestId("span-evaluate")).toBeInTheDocument();
    fireEvent.click(rows[4]);
    expect(screen.queryByTestId("span-evaluate")).toBeNull();
    expect(screen.getByTestId("span-attributes")).toBeInTheDocument();
  });

  it("the keyboard reaches and selects the evaluate span (End, ArrowUp, Enter)", async () => {
    render(<View />);
    const rows = () => screen.getAllByTestId("trace-span-row");
    await screen.findAllByTestId("trace-span-row");
    fireEvent.keyDown(rows()[0], { key: "End" });
    fireEvent.keyDown(rows()[4], { key: "ArrowUp" });
    expect(rows()[3]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(rows()[3], { key: "Enter" });
    expect(within(await screen.findByTestId("span-evaluate")).getByTestId("span-evaluate-verdict")).toHaveTextContent("passed");
  });

  it("at 375 px the sections render in the Sheet and the labels wrap", async () => {
    restoreWidth = setWindowWidth(375);
    render(<View />);
    fireEvent.click((await screen.findAllByTestId("trace-span-row"))[3]);
    const section = await screen.findByTestId("span-evaluate");
    expect(section.closest("[role=dialog]")).not.toBeNull();
    expect(section.querySelector("dd")?.className).toContain("break-words");
  });

  it("has no axe violations with the iteration selected", async () => {
    render(<View />);
    fireEvent.click((await screen.findAllByTestId("trace-span-row"))[2]);
    await screen.findByTestId("span-loop-iteration");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("has no axe violations with an unknown kind selected", async () => {
    render(<View />);
    fireEvent.click((await screen.findAllByTestId("trace-span-row"))[4]);
    await screen.findByTestId("span-attributes");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});

describe("tokens only in the trace files", () => {
  it("spanStyles and LoopSpanSections use no raw colours or palette classes for the evaluate kind", () => {
    for (const f of ["spanStyles.ts", "LoopSpanSections.tsx", "SpanDetail.tsx", "CapabilitySpanSections.tsx"]) {
      const text = readFileSync(join(__dirname, f), "utf8");
      expect(text, f).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(text, f).not.toMatch(/\b(?:rgb|rgba|hsl|hsla|oklch)\(/);
      expect(text, f).not.toMatch(/\b(?:bg|text|border|ring|fill|stroke)-(?:red|green|blue|yellow|amber|orange|emerald|rose|sky|slate|gray|zinc|neutral|stone|lime|teal|cyan|indigo|violet|purple|pink|fuchsia|white|black)(?:-\d{2,3})?\b/);
    }
  });
});
