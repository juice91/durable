import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StepDetail } from "@/lib/workflowRunState";

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));

import { StepIoPanel } from "./StepIoPanel";

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
});
