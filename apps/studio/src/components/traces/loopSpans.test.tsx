// LP.15: the trace Sheet renders the workflow loop spans (`loop`, `loop_iteration`, LP.9) and the
// `evaluate` span with its judge requests (verdict, results, judge usage and cost; never text).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React, { useState } from "react";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { SpanDetail } from "./SpanDetail";
import { TraceSheet } from "./TraceSheet";
import { KIND_BADGE, KIND_ICON, KIND_TONE } from "./spanStyles";
import { LOOP_SHOWN } from "./LoopSpanSections";
import { KIND_LABELS, SPAN_KINDS, getTrace, spanLabel, type Trace, type TraceSpan } from "@/lib/traces";
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

const SRC = join(__dirname, "..", "..");
const T = (s: number) => new Date(Date.UTC(2026, 10, 15, 9, 0, s)).toISOString();
const U = { input_tokens: 50, output_tokens: 10, cache_read_tokens: 0, cache_write_tokens: 0, usage_measured: true };
const REASON = "SECRET judge reason text";

function span(kind: TraceSpan["kind"], attributes: TraceSpan["attributes"], extra: Partial<TraceSpan> = {}): TraceSpan {
  return {
    id: `s-${kind}`, parent_id: null, kind, name: kind, start: T(1), end: T(3), status: "ok",
    attributes, input: null, output: null, truncated: {}, ...extra,
  };
}

const LOOP = span("loop", {
  step_id: "refine", step_type: "loop", iterations_started: 3, iterations_completed: 3, iterations_failed: 0,
  iterations_omitted: 0, iterations: 3, exited: "condition",
}, { id: "wr1:step:refine", parent_id: "run:wr1", name: "refine", start: T(1), end: T(9) });
const iteration = (i: number) => span("loop_iteration", {
  step_id: "refine", scope: `refine.${i}`, index: i, iteration_status: "completed", until: i === 2 ? "true" : "false",
  last_key: `loop:refine.${i}:step:judge:completed`,
}, { id: `wr1:loop:refine.${i}`, parent_id: LOOP.id, name: `refine #${i}`, start: T(1 + i * 2), end: T(2 + i * 2), input: { feedback: "" } });
const evaluate = (i: number) => span("evaluate", {
  step_id: "judge", step_type: "evaluate", scope: `refine.${i}`, passed: i === 2, score: i === 2 ? 1 : 0.25,
  evaluators: 2, evaluators_passed: i === 2 ? 2 : 1, evaluators_failed: i === 2 ? 0 : 1, evaluators_skipped: 0,
  results: [
    { name: "contains", kind: "contains", passed: true, score: 1 },
    { name: "judge", kind: "llm_judge", passed: i === 2, score: i === 2 ? 1 : 0.25 },
  ],
  judge_requests: 1, judge_input_tokens: 50, judge_output_tokens: 10, judge_cost_usd: "0.0004",
}, {
  id: `wr1:step:refine.${i}~judge`, parent_id: `wr1:loop:refine.${i}`, name: "judge", start: T(1 + i * 2), end: T(2 + i * 2),
  output: { passed: i === 2, score: i === 2 ? 1 : 0.25, reason: REASON, results: [] },
});
const judge = (i: number) => span("judge", {
  request_key: `judge:refine.${i}:judge:1`, step_id: "judge", scope: `refine.${i}`, evaluator_index: 1, evaluator_name: "judge",
  model_id: "test:judge", usage: U, cost_usd: "0.0004",
}, { id: `wr1:judge:refine.${i}:judge:1`, parent_id: `wr1:step:refine.${i}~judge`, name: "judge test:judge", start: T(2 + i * 2), end: T(2 + i * 2) });

function workflowTrace(): Trace {
  return {
    run: {
      run_id: "wr1", kind: "workflow", agent_id: null, version: null, tool_id: null, workflow_id: "wf-1", workflow_version: 1,
      knowledge_base_id: null, status: "completed", display_status: "completed", input: "{}", output: "{}", error: null,
      heartbeat_at: null, created_at: T(0), updated_at: T(9), trace_id: null, conversation_id: null, turn: null,
    },
    spans: [
      span("run", { run_kind: "workflow", run_id: "wr1", judge_requests: 3 }, { id: "run:wr1", name: "workflow wf-1", start: T(0), end: T(9) }),
      LOOP,
      ...[0, 1, 2].flatMap((i) => [iteration(i), evaluate(i), judge(i)]),
    ],
    totals: {
      runs: 1, model_requests: 3, tool_calls: 0, input_tokens: 150, output_tokens: 30, cache_read_tokens: 0, cache_write_tokens: 0,
      cost_usd: "0.0012", unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 9000, status: "completed",
      chat_requests: 0, chat_input_tokens: 0, chat_output_tokens: 0, chat_cost_usd: "0",
      judge_requests: 3, judge_input_tokens: 150, judge_output_tokens: 30, judge_cost_usd: "0.0012",
      embedding_requests: 0, memory_requests: 0, embedding_input_tokens: 0, embedding_cost_usd: "0",
    },
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0, iterations: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152, max_loop_iteration_spans: 100 },
  } as unknown as Trace;
}

function View() {
  const [tab, setTab] = useState<TraceTab>("spans");
  return <TraceSheet runId="wr1" open tab={tab} onTabChange={setTab} onOpenChange={() => undefined} />;
}

describe("loop and evaluate span kinds (lib/traces, spanStyles)", () => {
  it("every LP.9 and LP.15 kind is declared with a label, badge, icon and tone", () => {
    for (const kind of ["loop", "loop_iteration", "evaluate"] as const) {
      expect(SPAN_KINDS).toContain(kind);
      expect(KIND_LABELS[kind]).toBeTruthy();
      expect(KIND_BADGE[kind]).toBeTruthy();
      expect(KIND_ICON[kind]).toBeTruthy();
      expect(KIND_TONE[kind]).toBeTruthy();
      expect(LOOP_SHOWN[kind]?.length).toBeGreaterThan(0);
    }
    expect(KIND_BADGE.evaluate).toBe("EVAL");
  });

  it("labels the evaluate span with its verdict", () => {
    expect(spanLabel(evaluate(0))).toBe("Evaluate: judge (failed)");
    expect(spanLabel(evaluate(2))).toBe("Evaluate: judge (passed)");
    expect(spanLabel(span("evaluate", {}, { name: "ev" }))).toBe("Evaluate: ev");
  });
});

describe("span detail sections", () => {
  afterEach(cleanup);

  it("a loop span shows its iteration counts and how it exited", () => {
    render(<SpanDetail span={LOOP} />);
    const section = screen.getByTestId("span-loop");
    expect(within(section).getByTestId("span-loop-started")).toHaveTextContent("3");
    expect(within(section).getByTestId("span-loop-completed")).toHaveTextContent("3");
    expect(within(section).getByTestId("span-loop-failed")).toHaveTextContent("0");
    expect(within(section).getByTestId("span-loop-exited")).toHaveTextContent("condition met");
    expect(screen.queryByTestId("span-loop-omitted")).toBeNull();
    // Shown in the section, not again in the generic attribute list.
    expect(screen.getByTestId("span-attributes")).not.toHaveTextContent("iterations_started");
  });

  it("a loop with iterations left out says so", () => {
    render(<SpanDetail span={{ ...LOOP, attributes: { ...LOOP.attributes, iterations_started: 120, iterations_omitted: 20 } }} />);
    expect(screen.getByTestId("span-loop-omitted")).toHaveTextContent("20 iterations are not shown");
  });

  it("an iteration span shows its index, result and until verdict; a failed one its error", () => {
    render(<SpanDetail span={iteration(2)} />);
    expect(screen.getByTestId("span-iteration-index")).toHaveTextContent("3");
    expect(screen.getByTestId("span-iteration-status")).toHaveTextContent("completed");
    expect(screen.getByTestId("span-iteration-until")).toHaveTextContent("met");
    cleanup();
    render(<SpanDetail span={span("loop_iteration", { step_id: "each", scope: "each.1", index: 1, iteration_status: "failed", error_code: "model_error", error: "the model failed", failed_step_id: "triage" }, { status: "error" })} />);
    expect(screen.getByTestId("span-error")).toHaveTextContent("model_error");
    expect(screen.getByTestId("span-loop-iteration")).toHaveTextContent("triage");
  });

  it("an evaluate span shows the verdict, score, each evaluator and the judge usage, without reason text in the section", () => {
    render(<SpanDetail span={evaluate(0)} />);
    const section = screen.getByTestId("span-evaluate");
    expect(within(section).getByTestId("span-evaluate-verdict")).toHaveTextContent("failed");
    expect(within(section).getByTestId("span-evaluate-score")).toHaveTextContent("0.25");
    const results = within(section).getAllByTestId("span-evaluate-result");
    expect(results.map((r) => r.getAttribute("data-passed"))).toEqual(["true", "false"]);
    expect(results[1]).toHaveTextContent("judge (llm_judge)");
    expect(results[1]).toHaveTextContent("failed, 0.25");
    expect(within(section).getByTestId("span-evaluate-judges")).toHaveTextContent("1");
    expect(within(section).getByTestId("span-evaluate-judge-tokens")).toHaveTextContent("50 in, 10 out");
    expect(within(section).getByTestId("span-evaluate-judge-cost")).toHaveTextContent("$0.0004");
    expect(section).not.toHaveTextContent(REASON);
    expect(screen.queryByTestId("span-attributes")?.textContent ?? "").not.toMatch(/judge_cost_usd|results/);
  });

  it("an evaluate span without judges or with an unpriced judge", () => {
    render(<SpanDetail span={span("evaluate", { passed: true, score: 1, judge_requests: 0, judge_cost_usd: null })} />);
    expect(screen.getByTestId("span-evaluate-judges")).toHaveTextContent("0");
    expect(screen.queryByTestId("span-evaluate-judge-cost")).toBeNull();
    cleanup();
    render(<SpanDetail span={span("evaluate", { judge_requests: 2, judge_input_tokens: 9, judge_output_tokens: 1, judge_cost_usd: null })} />);
    expect(screen.getByTestId("span-evaluate-judge-cost")).toHaveTextContent("no price");
    expect(screen.queryByTestId("span-evaluate-verdict")).toBeNull();
  });

  it("a workflow judge span names its step, scope and evaluator, with usage and cost", () => {
    render(<SpanDetail span={judge(1)} />);
    expect(screen.getByTestId("span-judge-step")).toHaveTextContent("judge");
    expect(screen.getByTestId("span-judge-evaluator")).toHaveTextContent("judge");
    expect(screen.getByTestId("span-judge")).toHaveTextContent("refine.1");
    expect(screen.getByTestId("span-cost")).toHaveTextContent("$0.0004");
  });
});

describe("trace Sheet of a refine run", () => {
  let restoreWidth: () => void = () => undefined;
  beforeEach(() => {
    vi.mocked(getTrace).mockReset().mockResolvedValue(workflowTrace());
    vi.mocked(searchLogs).mockReset().mockReturnValue(new Promise(() => undefined));
  });
  afterEach(() => {
    cleanup();
    restoreWidth();
    restoreWidth = () => undefined;
  });

  it("lists the loop, three iterations, their evaluate steps and judge requests", async () => {
    render(<View />);
    await screen.findByText(/Loop: refine \(loop\), 3 done/);
    expect(screen.getAllByText(/Iteration \d: refine/)).toHaveLength(3);
    expect(screen.getAllByText(/Evaluate: judge \(failed\)/)).toHaveLength(2);
    expect(screen.getByText(/Evaluate: judge \(passed\)/)).toBeInTheDocument();
    expect(screen.getAllByText(/Judge \(evaluator 2\): test:judge/)).toHaveLength(3);
    fireEvent.click(screen.getByText(/Evaluate: judge \(passed\)/));
    expect(await screen.findByTestId("span-evaluate")).toHaveTextContent("passed");
  });

  it("at 375 px the evaluate section renders inside the Sheet", async () => {
    restoreWidth = setWindowWidth(375);
    render(<View />);
    fireEvent.click(await screen.findByText(/Evaluate: judge \(passed\)/));
    const section = await screen.findByTestId("span-evaluate");
    expect(section).toBeInTheDocument();
  });

  it("has no axe violations with an evaluate span selected", async () => {
    render(<View />);
    fireEvent.click(await screen.findByText(/Evaluate: judge \(passed\)/));
    await screen.findByTestId("span-evaluate");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});

describe("phone width (by classes) and guards for the LP.15 files", () => {
  const FILES = ["components/traces/LoopSpanSections.tsx", "components/workflows/StepIoPanel.tsx", "components/workflows/refineAttempts.ts"];
  const read = (f: string) => readFileSync(join(SRC, f), "utf8");

  it("no fixed or minimum width over 400 px; text wraps", () => {
    const re = /(?<![\w-])(?:min-)?w-\[(\d+)px\]|(?<![\w-])(?:min-)?w-(\d+)(?![\w./[])/g;
    for (const f of FILES) {
      for (const m of read(f).matchAll(re)) expect(m[1] ? Number(m[1]) : Number(m[2]) * 4, `${f}: ${m[0]}`).toBeLessThanOrEqual(400);
    }
    for (const f of FILES.filter((f) => f.endsWith(".tsx"))) {
      expect(read(f)).toContain("min-w-0");
      expect(read(f)).toMatch(/break-words/);
    }
  });

  it("no innerHTML, hex colours, console calls or Markdown rendering of judge text", () => {
    for (const f of FILES) {
      const text = read(f);
      expect(text, f).not.toMatch(/dangerouslySetInnerHTML|console\.(log|debug)/);
      expect(text, f).not.toMatch(/#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b(?![\w-])/);
      expect(text, f).not.toMatch(/SafeMarkdown|ReactMarkdown|<Markdown/);
    }
  });

  it.each(["light", "dark"])("every loop and evaluate section is accessible in the %s theme", async (theme) => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    try {
      for (const s of [LOOP, iteration(0), evaluate(0), evaluate(2), judge(0)]) {
        const { container, unmount } = render(<SpanDetail span={s} />);
        await expectNoAxeViolations(container);
        unmount();
      }
    } finally {
      document.documentElement.classList.remove("dark");
    }
  }, AXE_TIMEOUT_MS);
});
