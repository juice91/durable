// The RAG and memory milestone's browser check (Studio items): the toast export, the stepper's
// sr-only labels, span names at 390 px, and the Ingest runs tab's sections and cost columns.
import React from "react";
import "@testing-library/jest-dom";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { toast } from "@/components/ui/Toaster";
import { AgentStepper } from "@/components/agents/stepper/AgentStepper";
import type { EditorStepDefinition } from "@/components/agents/stepper/types";
import { TraceWaterfall } from "@/components/traces/TraceWaterfall";
import { IngestRunsTab, ingestRunTotals, type IngestRun } from "@/components/knowledge/IngestRunsTab";
import { flattenSpans, traceWindow, type TraceSpan } from "@/lib/traces";
import { listKnowledgeBaseRuns } from "@/lib/knowledge";

vi.mock("next/navigation", () => ({
  usePathname: () => "/knowledge/kb1",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/knowledge", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/knowledge")>()),
  listKnowledgeBaseRuns: vi.fn(),
}));

describe("toast (item 2)", () => {
  it("is a declared export of the Toaster module (a re-exported import warned on every agent route)", () => {
    expect(typeof toast).toBe("function");
    expect(typeof toast.success).toBe("function");
  });
});

describe("AgentStepper footer (item 4)", () => {
  const step = (id: string, label: string): EditorStepDefinition =>
    ({ id, label, Component: () => null, status: () => ({ text: "", valid: true }) }) as unknown as EditorStepDefinition;

  it("Previous and Next contain their sr-only labels (positioned buttons), so the labels cannot add a page scroll", () => {
    render(
      <AgentStepper
        steps={[step("a", "Identity"), step("b", "Tools"), step("c", "Knowledge")]}
        statuses={{}}
        errorSteps={[]}
        activeStep="b"
        onStepChange={() => undefined}
        layout="compact"
        actions={null}
      >
        <div />
      </AgentStepper>,
    );
    for (const [id, label] of [["stepper-previous", "Previous step: "], ["stepper-next", "Next step: "]] as const) {
      const button = screen.getByTestId(id);
      expect(button).toHaveClass("relative");
      const hidden = within(button).getByText(label.trim());
      expect(hidden).toHaveClass("sr-only");
      expect(hidden.closest(".relative")).toBe(button);
    }
  });
});

describe("TraceWaterfall at 390 px (item 5)", () => {
  const T = (s: number) => new Date(Date.UTC(2026, 10, 14, 9, 0, s)).toISOString();
  const spans: TraceSpan[] = [
    { id: "root", parent_id: null, kind: "run", name: "run", start: T(0), end: T(4), status: "ok", attributes: { run_kind: "agent" }, input: null, output: null, truncated: {} },
    { id: "m", parent_id: "root", kind: "model_request", name: "Model request 1", start: T(1), end: T(3), status: "ok", attributes: {}, input: null, output: null, truncated: {} },
  ] as TraceSpan[];

  it("hides the timeline bars below sm and gives the name the row (a status dot stands in for the bar)", () => {
    render(
      <TraceWaterfall rows={flattenSpans(spans)} window={traceWindow(spans, Date.parse(T(5)))} selectedId={null} onSelect={() => undefined} collapsed={new Set()} onToggle={() => undefined} now={Date.parse(T(5))} />,
    );
    const rows = screen.getAllByTestId("trace-span-row");
    for (const row of rows) {
      const timeline = within(row).getByTestId("trace-span-timeline");
      expect(timeline).toHaveClass("hidden", "sm:block");
      // The name cell grows below sm; its fixed 2/5 width applies from sm.
      const name = row.firstElementChild as HTMLElement;
      expect(name).toHaveClass("flex-1", "sm:w-2/5", "sm:flex-none");
      expect(within(row).getByTestId("trace-span-dot-narrow")).toHaveClass("sm:hidden");
    }
  });
});

describe("Ingest runs: sections, tokens and cost (item 8)", () => {
  const base = {
    kind: "kb_ingest", agent_id: null, version: null, knowledge_base_id: "kb1", status: "completed", display_status: "completed",
    error: null, heartbeat_at: null, created_at: "2026-11-14T10:00:00Z", updated_at: "2026-11-14T10:00:05Z", trace_id: null,
    conversation_id: null, turn: null,
  } as const;
  const done: IngestRun = {
    ...base, run_id: "11111111-aaaa", input: JSON.stringify({ document_id: "d1", doc_version: 2 }),
    output: JSON.stringify({ approx_tokens: 1200, chunk_count: 7, mode: "ingest", status: "switched" }), input_tokens: 1234, cost_usd: "0.0000250000",
  } as IngestRun;
  const running: IngestRun = {
    ...base, run_id: "22222222-bbbb", status: "running", display_status: "running", input: JSON.stringify({ reindex: 3 }), output: null, input_tokens: null, cost_usd: null,
  } as IngestRun;

  it("ingestRunTotals: sections from the output, tokens from the run's usage (else the output's estimate), its cost", () => {
    expect(ingestRunTotals(done)).toEqual({ sections: 7, tokens: 1234, costUsd: "0.0000250000" });
    expect(ingestRunTotals({ ...done, input_tokens: null })).toEqual({ sections: 7, tokens: 1200, costUsd: "0.0000250000" });
    expect(ingestRunTotals(running)).toEqual({ sections: null, tokens: null, costUsd: null });
    expect(ingestRunTotals({ output: "not json", input_tokens: undefined, cost_usd: undefined })).toEqual({ sections: null, tokens: null, costUsd: null });
  });

  it("shows the columns per run, a dash while a run has none", async () => {
    vi.mocked(listKnowledgeBaseRuns).mockResolvedValue({ items: [done, running], next_before: null });
    render(<IngestRunsTab kbId="kb1" documents={[]} />);
    const rows = await screen.findAllByTestId("knowledge-run-row");
    expect(within(rows[0]).getByTestId("knowledge-run-sections")).toHaveTextContent("7");
    expect(within(rows[0]).getByTestId("knowledge-run-tokens")).toHaveTextContent("1,234");
    expect(within(rows[0]).getByTestId("knowledge-run-cost")).toHaveTextContent("$0.000025");
    for (const id of ["knowledge-run-sections", "knowledge-run-tokens", "knowledge-run-cost"]) {
      expect(within(rows[1]).getByTestId(id)).toHaveTextContent("—");
    }
    for (const header of ["Sections", "Tokens", "Cost"]) expect(screen.getByRole("columnheader", { name: new RegExp(header) })).toBeInTheDocument();
  });
});
