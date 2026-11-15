// AC.14: the agent capabilities rows outside the chat thread: the trace Sheet's Events tab, the
// workflow step detail's unresolved refs (AC.9 hand-off), the Config panel's waiting pill, and the
// shared structured-output view.
import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(async () => []), listEmbeddingModels: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));

import type { Agent, AgentSpec } from "@/lib/api";
import type { RunEvent, RunEventData } from "@/lib/runs";
import type { StepDetail } from "@/lib/workflowRunState";
import { deriveRunView } from "@/lib/workflowRunState";
import { copyText } from "@/lib/clipboard";
import { EventTimeline } from "@/components/runs/EventTimeline";
import { StatusPill } from "@/components/runs/StatusPill";
import { StepIoPanel } from "@/components/workflows/StepIoPanel";
import { ConfigPanel } from "@/components/agentPage/ConfigPanel";
import { ApprovalCard, deadlineCountdown } from "@/components/approvals/ApprovalCard";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { StructuredOutputView } from "./StructuredOutput";

const ev = (seq: number, type: RunEvent["type"], data: unknown): RunEvent => ({ seq, type, data: data as RunEventData, ts: "2026-11-15T00:00:00Z" });

describe("Events tab rows", () => {
  it("shows the output candidate, a retry, the approval events, a denied result, output_json and the limit", () => {
    render(
      <EventTimeline
        currentPartialText={{}}
        events={[
          ev(1, "model_message", { model_request: 0, text: '{"priority":1}', output_candidate: true, requested_model_id: "test:fail-503", fallback_index: 1, failed_attempts: [{ model_id: "test:fail-503", error_type: "ModelHTTPError", http_status: 503 }] }),
          ev(2, "output_retry", { model_request: 0, errors: ["priority"] }),
          ev(3, "tool_approval_waiting", { tool_call_id: "c1", tool_name: "tickets.create", args: { title: "x" }, deadline: "2099-01-01T00:00:00Z" }),
          ev(4, "tool_denied", { tool_call_id: "c1", decided_by: "dev", comment: "not now" }),
          ev(5, "tool_result", { tool_call_id: "c1", status: "denied", content: "Tool call was denied: not now" }),
          ev(6, "run_completed", { output: '{"priority":"high"}', output_json: { priority: "high" } }),
          ev(7, "run_failed", { status: "failed", error: "the run exceeded a usage limit", error_code: "usage_limit_exceeded", limit: "max_tool_calls", value: 3 }),
        ]}
      />,
    );
    expect(screen.getByTestId("event-output-candidate")).toBeInTheDocument();
    expect(screen.getByTestId("event-fallback")).toHaveTextContent("answered by fallback #1");
    expect(screen.getByTestId("event-fallback")).toHaveTextContent("test:fail-503 (ModelHTTPError, HTTP 503)");
    expect(screen.getByTestId("event-output-retry")).toHaveTextContent("(at priority)");
    expect(screen.getByTestId("event-tool-approval-waiting")).toHaveTextContent("tickets.create");
    expect(screen.getByTestId("event-tool-decision")).toHaveTextContent("Tool call denied");
    expect(screen.getByTestId("event-tool-decision")).toHaveTextContent("not now");
    expect(screen.getByText("Tool denied:")).toBeInTheDocument();
    expect(within(screen.getByTestId("event-output-json")).getByText("high")).toBeInTheDocument();
    expect(screen.getByText("Usage limit exceeded: max tool calls (3).")).toBeInTheDocument();
    expect(screen.getByTestId("event-error-code")).toHaveTextContent("usage_limit_exceeded");
  });
});

describe("workflow step detail: unresolved refs (AC.9 hand-off)", () => {
  it("reads unresolved_refs from step_completed and warns with the paths", () => {
    const view = deriveRunView(null, [ev(1, "step_started", { step_id: "route", step_type: "branch" }), ev(2, "step_completed", { step_id: "route", output: { next: "default" }, unresolved_refs: ["steps.triage.output.priority"] })], "running");
    const detail: StepDetail = view.steps.route;
    expect(detail.unresolvedRefs).toEqual(["steps.triage.output.priority"]);
    render(<StepIoPanel step={null} detail={detail} onClose={() => undefined} />);
    const note = screen.getByTestId("step-io-unresolved-refs");
    expect(note).toHaveTextContent("A reference resolved to nothing; the branch took its default.");
    expect(note).toHaveTextContent("steps.triage.output.priority");
  });
});

describe("waiting status", () => {
  it("labels waiting_approval in the status pill and in the Config panel header", () => {
    render(<StatusPill status="waiting_approval" />);
    expect(screen.getByTestId("status-pill")).toHaveTextContent("Waiting for approval");
    const spec = { spec_version: 1, name: "Bot", instructions: "Help", model: { provider: "test", name: "echo" }, tools: [] } as unknown as AgentSpec;
    const agent: Agent = { id: "a1", name: "Bot", draft_spec: spec, latest_version: null };
    const { rerender } = render(<ConfigPanel agent={agent} versions={[]} turnStatus="waiting_approval" />);
    expect(screen.getByTestId("config-waiting-approval")).toHaveTextContent("Waiting for approval");
    rerender(<ConfigPanel agent={agent} versions={[]} turnStatus="running" />);
    expect(screen.queryByTestId("config-waiting-approval")).toBeNull();
  });

  it("counts the deadline down", () => {
    const now = Date.parse("2026-11-15T00:00:00Z");
    expect(deadlineCountdown("2026-11-15T00:59:00Z", now)).toBe("in 59 min");
    expect(deadlineCountdown("2026-11-15T02:05:00Z", now)).toBe("in 2 h 5 min");
    expect(deadlineCountdown("2026-11-14T00:00:00Z", now)).toBe("passed");
    expect(deadlineCountdown(null, now)).toBeNull();
  });
});

describe("StructuredOutputView", () => {
  it("renders nested fields, keeps [n] as text without sources, drops raw HTML, and copies the JSON", async () => {
    const value = { a: { b: ["x [1]", 2] }, html: "<img src=x onerror=alert(1)>", empty: "" };
    const { container } = render(<StructuredOutputView value={value} />);
    expect(screen.getAllByTestId("structured-output-field").map((f) => f.getAttribute("data-path"))).toEqual(["a", "a.b", "html", "empty"]);
    expect(screen.queryByTestId("citation-link")).toBeNull();
    expect(screen.getByText("x [1]")).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    fireEvent.click(screen.getByTestId("structured-output-copy"));
    await waitFor(() => expect(copyText).toHaveBeenCalledWith(JSON.stringify(value, null, 2)));
    expect(await screen.findByTestId("structured-output-copy-status")).toHaveTextContent("Copied");
  });
});

describe("accessibility", () => {
  it("has no axe violations in the tool approval card and the structured view", async () => {
    const { container } = render(
      <div>
        <ApprovalCard approval={{ kind: "tool", runId: "r", toolCallId: "c1", toolName: "tickets.create", args: { title: "x" }, deadline: "2099-01-01T00:00:00Z" }} onRefresh={() => undefined} />
        <StructuredOutputView value={{ priority: "high", tags: ["a"] }} />
      </div>,
    );
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

describe("Events tab: eval runs and the answering model", () => {
  it("renders the eval start, case results and aggregates as rows (no JSON fallback)", () => {
    render(
      <EventTimeline
        currentPartialText={{}}
        events={[
          ev(0, "run_started", { kind: "eval", suite_id: "s1", agent_version: 3, suite_hash: "abcdef0123456789", cases: 2, max_concurrency: 4 }),
          ev(1, "eval_case_result", { case_id: "c1", position: 0, status: "passed", assertions: { passed: 2, total: 2 }, scores: 1, labels: 0, evaluator_failures: 0, duration_ms: 120, cost_usd: "0.0012", error_code: null, child_run_id: "child-1" }),
          ev(2, "eval_case_result", { case_id: "c2", position: 1, status: "error", assertions: { passed: 0, total: 0 }, scores: 0, labels: 0, evaluator_failures: 0, duration_ms: 50, cost_usd: null, error_code: "output_invalid", child_run_id: "child-2" }),
          ev(3, "run_completed", { output: "{}", aggregates: { cases: 2, passed: 1, failed: 0, errors: 1, pass_rate: 0.5, scores_avg: { judge: 0.75 }, duration_ms_p50: 85, cost_usd: "0.0012" } }),
          ev(4, "model_message", { model_request: 0, text: "x", requested_model_id: "test:fail-503", fallback_index: 1, answering_model: "test:backup", failed_attempts: [] }),
        ]}
      />,
    );
    expect(screen.getByTestId("event-eval-start")).toHaveTextContent("cases: 2");
    const cases = screen.getAllByTestId("event-eval-case");
    expect(cases[0]).toHaveTextContent("Case passed");
    expect(cases[0]).toHaveTextContent("assertions: 2 of 2 passed");
    expect(cases[1]).toHaveTextContent("Case error");
    expect(cases[1]).toHaveTextContent("error: output_invalid");
    expect(screen.getAllByTestId("event-child-run-link")).toHaveLength(2);
    expect(screen.getByTestId("event-eval-aggregates")).toHaveTextContent("pass rate: 50%");
    expect(screen.getByTestId("event-eval-aggregates")).toHaveTextContent("avg scores: judge 0.75");
    expect(screen.getByTestId("event-fallback")).toHaveTextContent("answered by fallback #1 (test:backup)");
  });

  it("collapses a long string leaf and expands it from the keyboard-reachable button", () => {
    const long = "word ".repeat(200).trim();
    render(<StructuredOutputView value={{ summary: long, short: "ok" }} />);
    const button = screen.getByRole("button", { name: /Show all \(999 characters\)/ });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("structured-output-long-string")).not.toHaveTextContent(long);
    fireEvent.click(button);
    expect(screen.getByRole("button", { name: "Show less" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("structured-output-long-string")).toHaveTextContent(long);
  });
});

describe("AC.14 review fixes", () => {
  it("derives the shown status from the approval events, ahead of the status frame", async () => {
    const { effectiveTurnStatus } = await import("./turnCapabilities");
    const waiting = [ev(1, "tool_approval_waiting", { tool_call_id: "c1", tool_name: "t", args: {}, deadline: null })];
    expect(effectiveTurnStatus("running", waiting)).toBe("waiting_approval");
    const decided = [...waiting, ev(2, "tool_approved", { tool_call_id: "c1" })];
    expect(effectiveTurnStatus("waiting_approval", decided)).toBe("running");
    expect(effectiveTurnStatus("waiting_approval", [])).toBe("waiting_approval");
    expect(effectiveTurnStatus("recovering", waiting)).toBe("recovering");
    expect(effectiveTurnStatus("running", [...waiting, ev(3, "run_failed", {})])).toBe("running");
  });

  it("never cuts the collapsed preview inside a Markdown link", async () => {
    const { stringPreview } = await import("./StructuredOutput");
    const link = `[the full guide](https://docs.example.com/${"a".repeat(80)})`;
    const text = `${"word ".repeat(40)}${link} ${"tail ".repeat(100)}`;
    const preview = stringPreview(text);
    expect(preview.plain).toBe(false);
    expect(preview.text).not.toContain("[the full guide](");
    expect(preview.text.endsWith("…")).toBe(true);
    const oneLink = `[x](https://e.example/${"b".repeat(600)})`;
    const plain = stringPreview(oneLink);
    expect(plain.plain).toBe(true);
    render(<StructuredOutputView value={{ s: oneLink }} />);
    expect(screen.queryByRole("link")).toBeNull();
  });
});
