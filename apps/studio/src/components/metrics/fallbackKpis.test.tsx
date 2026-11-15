import { describe, expect, it } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { FallbackKpis, fallbackCards } from "./FallbackKpis";
import { BreakdownTable } from "./BreakdownTable";
import { summaryFixture, RANGE } from "./metricsFixtures";
import { BREAKDOWN_OPERATIONS, OPERATION_LABELS, parsePageState, type FallbackValues } from "@/lib/metrics";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

// AC.17: the Overview's model fallback row, `judge` among the operations, `eval` as a kind, and
// the model breakdown as usage by answering model.

const fb = (v: Partial<FallbackValues> = {}): FallbackValues => ({
  chat_requests: 0, fell_back: 0, billed_failed_attempts: 0, exhausted_runs: 0, fallback_rate: null, ...v,
});

describe("FallbackKpis", () => {
  it("renders the rate, failed attempts and exhausted runs with deltas", () => {
    const summary = summaryFixture(
      { fallback: fb({ chat_requests: 11, fell_back: 2, billed_failed_attempts: 3, exhausted_runs: 1, fallback_rate: 0.25 }) },
      { fallback: fb({ chat_requests: 10, fell_back: 1, billed_failed_attempts: 1, fallback_rate: 0.1 }) },
    );
    render(<FallbackKpis summary={summary} />);
    expect(screen.getByTestId("metrics-fallback-kpis")).toBeInTheDocument();
    expect(screen.getByTestId("kpi-fallback_rate-value")).toHaveTextContent("25%");
    expect(screen.getByTestId("kpi-fallback_failed_attempts-value")).toHaveTextContent("3");
    expect(screen.getByTestId("kpi-fallback_exhausted-value")).toHaveTextContent("1");
    const [rate] = fallbackCards(summary);
    expect(rate.delta?.tone).toBe("worse");
    expect(rate.hint).toContain("3 of 12");
  });

  it("is hidden without fallback activity or from an older API", () => {
    expect(fallbackCards(summaryFixture({ fallback: fb() }))).toEqual([]);
    // Agent chat requests all answered by the primary: no 0% row.
    expect(fallbackCards(summaryFixture({ fallback: fb({ chat_requests: 50, fallback_rate: 0 }) }, { fallback: fb() }))).toEqual([]);
    expect(fallbackCards(summaryFixture({}))).toEqual([]);
    const { container } = render(<FallbackKpis summary={summaryFixture({})} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("counts exhausted runs in the rate", () => {
    expect(fallbackCards(summaryFixture({ fallback: fb({ exhausted_runs: 2, fallback_rate: 1 }) }))[0].value).toBe("100%");
  });

  it("has no axe violations", async () => {
    const { container } = render(
      <FallbackKpis summary={summaryFixture({ fallback: fb({ chat_requests: 4, fell_back: 1, fallback_rate: 0.25 }) }, null)} />,
    );
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

describe("judge operation, eval kind and answering-model breakdown", () => {
  it("lists judge among the operations and parses eval as a kind", () => {
    expect(BREAKDOWN_OPERATIONS).toContain("judge");
    expect(OPERATION_LABELS.judge).toBe("Judge");
    const state = parsePageState(new URLSearchParams("operation=judge&kind=eval"));
    expect(state.operation).toBe("judge");
    expect(state.kind).toBe("eval");
  });

  it("notes that the model breakdown is by answering model", () => {
    render(
      <BreakdownTable title="By model" by="model" items={[]} filters={{ ...RANGE }} sort="runs" />,
    );
    expect(screen.getByTestId("breakdown-model-note")).toHaveTextContent("model that answered");
  });
});
