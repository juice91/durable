import { describe, expect, it, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MetricsFilterBar } from "./MetricsFilterBar";
import { OperationKpis, operationCards } from "./OperationKpis";
import { UsageByOperation } from "./UsageByOperation";
import { breakdownHref, breakdownLabel } from "./BreakdownTable";
import { RANGE, metricValues, summaryFixture } from "./metricsFixtures";
import {
  DEFAULT_PAGE_STATE,
  metricsOperationHref,
  metricsQueryParams,
  pageStateToFilters,
  pageStateToParams,
  parsePageState,
  tracesHref,
  type BreakdownItem,
  type MetricsSummary,
  type UsageValues,
} from "@/lib/metrics";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { optionsOf } from "@/test/select";

// RM.19: the Metrics `operation` filter, the embedding and memory KPI row and Usage by operation.

const usage = (overrides: Partial<UsageValues> = {}): UsageValues => ({
  requests: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, cost_usd: "0",
  unpriced_requests: 0, unmeasured_requests: 0, ...overrides,
});

function withOperations(summary: MetricsSummary, current: Record<string, UsageValues>, previous?: Record<string, UsageValues>): MetricsSummary {
  return {
    ...summary,
    current: { ...summary.current, operations: current },
    previous: summary.previous ? { ...summary.previous, operations: previous ?? {} } : null,
  };
}

const OPS = {
  chat: usage({ requests: 10, input_tokens: 1000, output_tokens: 200, cost_usd: "0.0100000000" }),
  embedding: usage({ requests: 6, input_tokens: 600, cost_usd: "0.0006000000" }),
  observer: usage({ requests: 2, input_tokens: 300, output_tokens: 40, cost_usd: "0.0030000000" }),
  reflector: usage({ requests: 1, input_tokens: 100, output_tokens: 10, unpriced_requests: 1, cost_usd: "0" }),
  moderation: usage({ requests: 1, input_tokens: 5 }),
  memory: usage({ requests: 5, input_tokens: 450, output_tokens: 50, cost_usd: "0.0030500000", unpriced_requests: 1 }),
};
const PREV = { embedding: usage({ requests: 3, input_tokens: 300, cost_usd: "0.0003000000" }), memory: usage({ requests: 1, input_tokens: 100, cost_usd: "0.0010000000" }) };

describe("operation in the page state and queries", () => {
  it("round-trips through the URL and reaches the API query", () => {
    const state = parsePageState(new URLSearchParams("operation=embedding&kind=kb_ingest"));
    expect(state.operation).toBe("embedding");
    expect(state.kind).toBe("kb_ingest");
    expect(pageStateToParams(state).toString()).toBe("kind=kb_ingest&operation=embedding");
    const filters = pageStateToFilters(state, new Date("2026-11-14T00:00:00Z"));
    expect(metricsQueryParams(filters).get("operation")).toBe("embedding");
  });

  it("ignores an unknown operation and leaves the key out when unset", () => {
    expect(parsePageState(new URLSearchParams("operation=bogus"))).toEqual(DEFAULT_PAGE_STATE);
    expect("operation" in parsePageState(new URLSearchParams(""))).toBe(false);
  });

  it("never carries the operation to the Traces list (it has no such filter)", () => {
    const href = tracesHref({ ...RANGE, operation: "memory", kind: "kb_ingest" });
    expect(href).not.toContain("operation");
    expect(href).toContain("kind=kb_ingest");
  });

  it("narrows the Metrics page to an operation", () => {
    expect(metricsOperationHref({ ...DEFAULT_PAGE_STATE, agent_id: "a1" }, "memory")).toBe("/metrics?agent_id=a1&operation=memory");
  });

  it("labels operation breakdown rows and drills down without narrowing the Traces list", () => {
    const item = { key: "observer", label: "observer", ...metricValues() } as BreakdownItem;
    expect(breakdownLabel(item, "operation")).toBe("Observer");
    expect(breakdownHref(item, "operation", RANGE)).toBe(tracesHref(RANGE));
  });
});

describe("MetricsFilterBar operation and kind", () => {
  it("offers every operation and the Knowledge ingest kind", () => {
    const onChange = vi.fn();
    render(<MetricsFilterBar state={DEFAULT_PAGE_STATE} onChange={onChange} range={RANGE} />);
    const op = screen.getByLabelText("Operation") as HTMLSelectElement;
    expect(optionsOf(op).map((o) => o.value)).toEqual(["", "chat", "embedding", "memory", "observer", "reflector", "moderation", "judge"]); // AC.17: + judge
    fireEvent.change(op, { target: { value: "observer" } });
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ operation: "observer" }));
    const kind = screen.getByLabelText("Kind") as HTMLSelectElement;
    expect(optionsOf(kind).map((o) => o.value)).toContain("kb_ingest");
  });

  it("Clear filters drops the operation", () => {
    const onChange = vi.fn();
    render(<MetricsFilterBar state={{ ...DEFAULT_PAGE_STATE, operation: "embedding" }} onChange={onChange} range={RANGE} />);
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(onChange.mock.calls[0][0].operation).toBeUndefined();
  });
});

describe("OperationKpis (Embedding and memory row)", () => {
  const summary = withOperations(summaryFixture({ requests: 20 }, { requests: 4 }), OPS, PREV);

  it("builds embedding and memory tokens and cost cards with deltas and drill-downs", () => {
    const cards = operationCards(summary, DEFAULT_PAGE_STATE);
    expect(cards.map((c) => c.id)).toEqual(["embedding_tokens", "embedding_cost", "memory_tokens", "memory_cost"]);
    expect(cards[0].value).toBe("600");
    expect(cards[0].delta?.direction).toBe("up");
    expect(cards[1].value).toBe("$0.00060");
    expect(cards[2].value).toBe("500");
    expect(cards[3].hint).toContain("1 of 5 memory requests have no price");
    expect(cards[0].href).toBe("/metrics?operation=embedding");
    expect(cards[3].href).toBe("/metrics?operation=memory");
  });

  it("renders the row, and nothing without an operations split", () => {
    const { unmount } = render(<OperationKpis summary={summary} state={DEFAULT_PAGE_STATE} />);
    const row = screen.getByTestId("metrics-operation-kpis");
    expect(within(row).getByRole("heading", { name: "Embedding and memory" })).toBeInTheDocument();
    expect(within(row).getByTestId("kpi-embedding_tokens-value")).toHaveTextContent("600");
    unmount();
    render(<OperationKpis summary={summaryFixture()} state={DEFAULT_PAGE_STATE} />);
    expect(screen.queryByTestId("metrics-operation-kpis")).toBeNull();
  });

  it("has no axe violations", async () => {
    const { container } = render(<OperationKpis summary={summary} state={DEFAULT_PAGE_STATE} />);
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

describe("UsageByOperation", () => {
  const summary = withOperations(summaryFixture(), OPS);

  it("lists the five operations with tokens and cost, linking to the narrowed page", () => {
    render(<UsageByOperation summary={summary} state={DEFAULT_PAGE_STATE} />);
    const row = (op: string) => screen.getAllByTestId("usage-operation-row").find((r) => r.getAttribute("data-operation") === op);
    expect(screen.getAllByTestId("usage-operation-row").map((r) => r.getAttribute("data-operation"))).toEqual([
      "chat", "embedding", "observer", "reflector", "moderation",
    ]);
    const rows = ["chat", "embedding", "observer", "reflector", "moderation"].map((op) => row(op)!);
    expect(rows[1]).toHaveTextContent("Embedding");
    expect(rows[1]).toHaveTextContent("600");
    expect(rows[3]).toHaveTextContent("no price");
    expect(within(rows[2]).getByRole("link", { name: "Observer: show this operation's metrics" })).toHaveAttribute("href", "/metrics?operation=observer");
    expect(row("memory")).toBeUndefined();
  });

  it("does not link the operation the page is narrowed to, and renders nothing without a split", () => {
    const { unmount } = render(<UsageByOperation summary={summary} state={{ ...DEFAULT_PAGE_STATE, operation: "chat" }} />);
    const chat = screen.getAllByTestId("usage-operation-row").find((r) => r.getAttribute("data-operation") === "chat")!;
    expect(within(chat).queryByRole("link")).toBeNull();
    unmount();
    const { container } = render(<UsageByOperation summary={summaryFixture()} state={DEFAULT_PAGE_STATE} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("has no axe violations", async () => {
    const { container } = render(<UsageByOperation summary={summary} state={DEFAULT_PAGE_STATE} />);
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
