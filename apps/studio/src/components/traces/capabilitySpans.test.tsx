import { describe, expect, it } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen, within } from "@testing-library/react";
import { SpanDetail } from "./SpanDetail";
import { TraceTotals } from "./TraceTotals";
import { attemptFailure } from "./CapabilitySpanSections";
import { KIND_BADGE, KIND_ICON, KIND_TONE } from "./spanStyles";
import {
  CAPABILITY_SPAN_KINDS,
  EMPTY_FILTERS,
  KIND_LABELS,
  SPAN_KINDS,
  parseTraceFilters,
  spanLabel,
  usageSplit,
  type TraceSpan,
  type TraceTotals as Totals,
} from "@/lib/traces";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

// AC.17: fallback attempts and the answering model, tool approval waits, output retries, eval cases
// and judge requests in the trace Sheet's span detail; judge usage in the totals; `eval` in the
// Traces kind filter.

const T = (s: number) => new Date(Date.UTC(2026, 10, 15, 9, 0, s)).toISOString();
const USAGE = { input_tokens: 4, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, usage_measured: true };

function span(kind: TraceSpan["kind"], attributes: TraceSpan["attributes"], extra: Partial<TraceSpan> = {}): TraceSpan {
  return {
    id: `s-${kind}`, parent_id: null, kind, name: kind, start: T(1), end: T(3), status: "ok",
    attributes, input: null, output: null, truncated: {}, ...extra,
  };
}

const MODEL = span("model_request", {
  request_key: "model:0", model_request: 0, model_id: "litellm:b", answering_model: "litellm:b",
  requested_model_id: "litellm:a", fallback_index: 1, output_candidate: true,
  failed_attempts: [{ model_id: "litellm:a", outcome: "error", error_type: "ModelHTTPError", http_status: 503 }],
  usage: USAGE, cost_usd: "0.001",
});
const ATTEMPT = span("fallback_attempt", {
  request_key: "model:0:failed:0", model_request: 0, fallback_position: 0, model_id: "litellm:a",
  outcome: "error", error_type: "ModelHTTPError", http_status: 503, timed: true, usage: USAGE, cost_usd: "0.0004",
}, { status: "error", start: T(1), end: T(2) });
const APPROVAL = span("tool_approval", {
  tool_call_id: "c1", tool_name: "send_email", deadline: T(3600), waiting_since: T(1), waited: true,
  decision: { type: "tool_denied", decided_by: "user-1", comment: "not now", reason: null, at: T(3) },
});
const RETRY = span("output_retry", { event_key: "output:0:retry", model_request: 0, errors: ["$.title", "$.items[0]"], error_count: 2, cause: "schema" }, { end: T(1) });
const CASE = span("eval_case", {
  case_id: "k1", case_status: "failed", position: 0, assertions: { passed: 1, total: 2 }, scores: 1, labels: 0,
  evaluator_failures: 0, duration_ms: 900, cost_usd: "0.003", error_code: null, child_run_id: "child-1",
}, { status: "error" });
const JUDGE = span("judge", {
  request_key: "judge:k1:0", case_id: "k1", evaluator_index: 0, model_id: "litellm:judge", usage: USAGE, cost_usd: "0.002",
}, { end: T(1) });

describe("capability span kinds (lib/traces, spanStyles)", () => {
  it("every new kind is declared with a label, badge, icon and tone", () => {
    for (const kind of ["fallback_attempt", "tool_approval", "output_retry", "eval_case", "judge"] as const) {
      expect(SPAN_KINDS).toContain(kind);
      expect(CAPABILITY_SPAN_KINDS.has(kind)).toBe(true);
      expect(KIND_LABELS[kind]).toBeTruthy();
      expect(KIND_BADGE[kind]).toBeTruthy();
      expect(KIND_ICON[kind]).toBeTruthy();
      expect(KIND_TONE[kind]).toBeTruthy();
    }
  });

  it("labels the spans", () => {
    expect(spanLabel(ATTEMPT)).toBe("Failed attempt 1: litellm:a");
    expect(spanLabel(APPROVAL)).toBe("Approval: send_email");
    expect(spanLabel(RETRY)).toBe("Output retry after #0 (2 errors)");
    expect(spanLabel(CASE)).toBe("Case 1: failed");
    expect(spanLabel(JUDGE)).toBe("Judge (evaluator 1): litellm:judge");
  });

  it("parses eval as a Traces kind filter", () => {
    expect(parseTraceFilters(new URLSearchParams("kind=eval")).kind).toBe("eval");
    expect(parseTraceFilters(new URLSearchParams("kind=nope"))).toEqual(EMPTY_FILTERS);
  });
});

describe("SpanDetail capability sections", () => {
  it("shows the answering model, the fallback and failed attempts of a model request", () => {
    render(<SpanDetail span={MODEL} />);
    const section = screen.getByTestId("span-fallback");
    expect(within(section).getByTestId("span-answering-model")).toHaveTextContent("litellm:b");
    expect(within(section).getByTestId("span-fallback-index")).toHaveTextContent("fallback 1 answered");
    expect(within(section).getByTestId("span-fallback-attempt")).toHaveTextContent("litellm:a: ModelHTTPError, HTTP 503");
    expect(within(section).getByTestId("span-output-candidate")).toBeInTheDocument();
    // Shown in the section, not repeated in the generic attributes.
    expect(screen.queryByTestId("span-attributes")?.textContent ?? "").not.toContain("answering_model");
  });

  it("leaves a model request without fallback data as before", () => {
    render(<SpanDetail span={span("model_request", { model_id: "m", usage: USAGE })} />);
    expect(screen.queryByTestId("span-fallback")).toBeNull();
  });

  it("shows a failed attempt with its usage and duration", () => {
    render(<SpanDetail span={ATTEMPT} />);
    expect(screen.getByTestId("span-attempt-failure")).toHaveTextContent("ModelHTTPError, HTTP 503");
    expect(screen.getByTestId("span-usage")).toBeInTheDocument();
    expect(screen.getByTestId("span-duration")).toHaveTextContent("1");
    expect(screen.getByTestId("span-status")).toHaveTextContent("Error");
    expect(screen.queryByTestId("span-attempt-untimed")).toBeNull();
  });

  it("says when an attempt's timing is unknown", () => {
    render(<SpanDetail span={{ ...ATTEMPT, attributes: { ...ATTEMPT.attributes, timed: false, usage: null } }} />);
    expect(screen.getByTestId("span-attempt-untimed")).toBeInTheDocument();
  });

  it("shows a tool approval's decision", () => {
    render(<SpanDetail span={APPROVAL} />);
    const section = screen.getByTestId("span-tool-approval");
    expect(within(section).getByTestId("span-approval-decision")).toHaveTextContent("denied");
    expect(section).toHaveTextContent("user-1");
    expect(section).toHaveTextContent("not now");
  });

  it("shows a waiting approval", () => {
    const waiting = { ...APPROVAL, status: "waiting" as const, end: null, attributes: { ...APPROVAL.attributes, decision: undefined } };
    render(<SpanDetail span={waiting} />);
    expect(screen.getByTestId("span-approval-decision")).toHaveTextContent("waiting");
    expect(screen.getByTestId("span-status")).toHaveTextContent("Waiting");
  });

  it("shows an eval denial", () => {
    const evalDenied = { ...APPROVAL, attributes: { ...APPROVAL.attributes, waited: false, decision: { type: "tool_denied", decided_by: "eval" } } };
    render(<SpanDetail span={evalDenied} />);
    expect(screen.getByTestId("span-tool-approval")).toHaveTextContent("no approvals during evaluation");
  });

  it("lists an output retry's JSON paths", () => {
    render(<SpanDetail span={RETRY} />);
    expect(screen.getByTestId("span-output-retry-count")).toHaveTextContent("2");
    const paths = screen.getByTestId("span-output-retry-paths");
    expect(within(paths).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["$.title", "$.items[0]"]);
  });

  it("shows an eval case with a link to its case run", () => {
    render(<SpanDetail span={CASE} />);
    expect(screen.getByTestId("span-eval-case-status")).toHaveTextContent("failed");
    expect(screen.getByTestId("span-eval-case-assertions")).toHaveTextContent("1 of 2 passed");
    expect(screen.getByTestId("span-link-case-run")).toHaveAttribute("href", expect.stringContaining("child-1"));
  });

  it("shows a judge request's usage", () => {
    render(<SpanDetail span={JUDGE} />);
    expect(screen.getByTestId("span-judge")).toBeInTheDocument();
    expect(screen.getByTestId("span-usage")).toHaveTextContent("litellm:judge");
  });

  it("describes attempt failures without text", () => {
    expect(attemptFailure({ outcome: "rejected" })).toBe("rejected");
    expect(attemptFailure({ error_type: "TimeoutError" })).toBe("TimeoutError");
  });

  it.each([MODEL, ATTEMPT, APPROVAL, RETRY, CASE, JUDGE])("has no axe violations for $kind", async (s) => {
    const { container } = render(<SpanDetail span={s} />);
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

describe("judge usage in the totals", () => {
  const totals: Totals = {
    runs: 2, model_requests: 3, tool_calls: 0, input_tokens: 100, output_tokens: 20, cache_read_tokens: 0,
    cache_write_tokens: 0, cost_usd: "0.004", unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 5000,
    status: "completed", chat_requests: 1, chat_input_tokens: 10, chat_output_tokens: 2, chat_cost_usd: "0.001",
    memory_input_tokens: 0, memory_output_tokens: 0, memory_cost_usd: "0", moderation_input_tokens: 0,
    moderation_output_tokens: 0, moderation_cost_usd: "0", embedding_input_tokens: 0,
    judge_requests: 2, judge_input_tokens: 90, judge_output_tokens: 18, judge_cost_usd: "0.003",
  };

  it("splits judge usage from the chat model's", () => {
    const split = usageSplit(totals as unknown as Record<string, unknown>);
    expect(split?.chat.inputTokens).toBe(10);
    expect(split?.others.map((g) => g.id)).toEqual(["judge"]);
    render(<TraceTotals totals={totals} />);
    expect(screen.getByTestId("totals-usage-judge")).toHaveTextContent("90 in, 18 out");
    expect(screen.getByTestId("totals-input-tokens")).toHaveTextContent("10");
  });
});
