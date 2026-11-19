import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StepDetail } from "@/lib/workflowRunState";

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));

import { StepIoPanel, isStructuredAgentOutput } from "./StepIoPanel";

const detail = (overrides: Partial<StepDetail>): StepDetail => ({
  stepId: "s",
  state: "pending",
  stepType: null,
  started: false,
  hasOutput: false,
  error: null,
  errorCode: null,
  childRunId: null,
  agentId: null,
  toolId: null,
  prompt: null,
  deadline: null,
  timeoutHours: null,
  decision: null,
  skipReason: null,
  startedAt: null,
  endedAt: null,
  ...overrides,
});

afterEach(() => cleanup());

describe("StepIoPanel", () => {
  it("labels an untaken branch arm", () => {
    render(<StepIoPanel step={null} detail={detail({ state: "skipped", skipReason: "branch_not_taken" })} onClose={() => undefined} />);
    expect(screen.getByTestId("step-io-skip-reason")).toHaveTextContent("Skipped: this branch arm was not taken.");
  });

  it("labels a step the run ended before reaching, distinctly from an untaken arm", () => {
    render(<StepIoPanel step={null} detail={detail({ state: "skipped", skipReason: "run_ended" })} onClose={() => undefined} />);
    const label = screen.getByTestId("step-io-skip-reason");
    expect(label).toHaveTextContent("Not reached: the run ended before this step.");
    expect(label).not.toHaveTextContent(/branch arm/);
  });

  it("shows 'no output (parallel step)' for a parallel step's null output", () => {
    render(<StepIoPanel step={null} detail={detail({ state: "completed", stepType: "parallel", started: true, hasOutput: true, output: null })} onClose={() => undefined} />);
    expect(screen.getByTestId("step-io-output")).toHaveTextContent("No output (parallel step).");
    expect(screen.queryByText("null")).toBeNull();
  });

  it("still prints a non-parallel step's null output as JSON", () => {
    render(<StepIoPanel step={null} detail={detail({ state: "completed", stepType: "tool", started: true, hasOutput: true, output: null })} onClose={() => undefined} />);
    expect(screen.getByTestId("step-io-output")).toHaveTextContent("null");
  });

  it("renders a structured agent step's object output as JSON (AC.9)", () => {
    const output = { priority: "high", tags: ["disk"] };
    render(<StepIoPanel step={null} detail={detail({ state: "completed", stepType: "agent", started: true, hasOutput: true, output })} onClose={() => undefined} />);
    const block = screen.getByTestId("step-io-output");
    expect(block).toHaveAttribute("data-output-format", "json");
    expect(block.textContent).toBe(JSON.stringify(output, null, 2));
    expect(screen.getByText("Structured output")).toBeInTheDocument();
  });

  it("keeps a text agent step's output as plain text", () => {
    render(<StepIoPanel step={null} detail={detail({ state: "completed", stepType: "agent", started: true, hasOutput: true, output: "Echo: hi" })} onClose={() => undefined} />);
    const block = screen.getByTestId("step-io-output");
    expect(block).toHaveAttribute("data-output-format", "text");
    expect(block.textContent).toBe("Echo: hi");
    expect(screen.queryByText("Structured output")).toBeNull();
  });

  it("shows markup inside a structured output as escaped text, under the JSON label", () => {
    const output = { summary: "<img src=x onerror=alert(1)>", note: "<b>bold</b>" };
    const { container } = render(<StepIoPanel step={null} detail={detail({ state: "completed", stepType: "agent", started: true, hasOutput: true, output })} onClose={() => undefined} />);
    const block = screen.getByTestId("step-io-output");
    expect(block.previousElementSibling).toHaveTextContent("Structured output");
    expect(block).toHaveAttribute("data-output-format", "json");
    expect(block.textContent).toContain("<img src=x onerror=alert(1)>");
    expect(block.innerHTML).toContain("&lt;img src=x onerror=alert(1)&gt;");
    expect(block.querySelector("img, b")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
  });

  it("only treats an agent step's object as structured", () => {
    expect(isStructuredAgentOutput("agent", { a: 1 })).toBe(true);
    expect(isStructuredAgentOutput("agent", "text")).toBe(false);
    expect(isStructuredAgentOutput("agent", null)).toBe(false);
    expect(isStructuredAgentOutput("tool", { a: 1 })).toBe(false);
  });
});

describe("StepIoPanel: error kept (on_error: continue)", () => {
  it("shows an amber badge with the code for a step that kept its error, derived from the run's events", async () => {
    const { deriveRunView } = await import("@/lib/workflowRunState");
    const { demoSpec } = await import("@/test/workflowFixtures");
    const events = [
      { seq: 0, type: "run_started", data: {}, ts: "2026-11-20T10:00:00Z" },
      { seq: 1, type: "step_started", data: { step_id: "lookup", step_type: "tool", tool_id: "http.request", input: {} }, ts: "2026-11-20T10:00:01Z" },
      { seq: 2, type: "step_completed", data: { step_id: "lookup", output: { error: { code: "body_source_rejected" } }, outcome: "error_kept", error_code: "body_source_rejected" }, ts: "2026-11-20T10:00:02Z" },
    ] as Parameters<typeof deriveRunView>[1];
    const view = deriveRunView(demoSpec, events, "running");
    render(<StepIoPanel step={null} detail={view.steps.lookup} onClose={() => undefined} />);
    const badge = screen.getByTestId("step-io-error-kept");
    expect(badge).toHaveTextContent("error kept: body_source_rejected");
    expect(badge.className).toContain("text-warning-text");
    expect(screen.getByTestId("step-io-state")).toHaveTextContent("Completed");
    expect(screen.queryByTestId("step-io-error")).toBeNull();
  });

  it("shows no badge for an ordinary completed step", () => {
    render(<StepIoPanel step={null} detail={detail({ state: "completed", stepType: "tool", started: true, hasOutput: true, output: { ok: true } })} onClose={() => undefined} />);
    expect(screen.queryByTestId("step-io-error-kept")).toBeNull();
  });
});
