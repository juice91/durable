import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { Run } from "@/lib/runs";
import { RunSubject, runKind } from "./RunSubject";
import { routes } from "@/lib/routes";

const base: Run = {
  run_id: "run-12345678-aaaa", agent_id: "agent-1", version: 2, status: "completed", display_status: "completed",
  input: "", output: null, error: null, heartbeat_at: null, created_at: "2026-11-13T10:00:00Z",
  updated_at: "2026-11-13T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
};
const workflowRun: Run = {
  ...base, run_id: "wfrun-1", kind: "workflow", agent_id: null, version: null, tool_id: null,
  workflow_id: "wf-abcdef123456", workflow_version: 3,
};

afterEach(() => cleanup());

describe("RunSubject", () => {
  it("labels a workflow run with its workflow and version and links to its execution view", () => {
    render(<RunSubject run={workflowRun} workflowNames={{ "wf-abcdef123456": "Triage" }} />);
    const link = screen.getByRole("link", { name: "Workflow: Triage v3" });
    expect(link).toHaveAttribute("href", routes.workflowRun("wf-abcdef123456", "wfrun-1"));
    expect(screen.queryByText(/Tool: unknown|Agent: null/)).toBeNull();
  });

  it("falls back to the short workflow id without a name", () => {
    render(<RunSubject run={workflowRun} showVersion />);
    expect(screen.getByRole("link", { name: "Workflow: wf-abcde... v3" })).toBeInTheDocument();
    expect(screen.queryByText(/Agent:|Version: latest/)).toBeNull();
  });

  it("keeps tool and agent labels", () => {
    const { rerender } = render(<RunSubject run={{ ...base, kind: "tool", agent_id: null, version: null, tool_id: "clock.now" }} />);
    expect(screen.getByTestId("run-subject")).toHaveTextContent("Tool: clock.now");
    rerender(<RunSubject run={base} agentNames={{ "agent-1": "Support" }} />);
    expect(screen.getByTestId("run-subject")).toHaveTextContent(/^Support$/);
    rerender(<RunSubject run={base} showVersion />);
    expect(screen.getByTestId("run-subject")).toHaveTextContent("Agent: agent-1 | Version: 2");
  });

  it("links a workflow's child agent run to its parent run", () => {
    render(<RunSubject run={{ ...base, kind: "agent", parent_run_id: "parent-run-0001", parent_step_id: "classify" }} />);
    expect(screen.getByTestId("run-subject")).toHaveTextContent("Part of workflow run parent-r... (step classify)");
    expect(screen.getByTestId("run-parent-link")).toHaveAttribute("href", routes.run("parent-run-0001"));
  });

  it("classifies rows without kind", () => {
    expect(runKind({ agent_id: "a", tool_id: null, workflow_id: null })).toBe("agent");
    expect(runKind({ agent_id: null, tool_id: "t", workflow_id: null })).toBe("tool");
    expect(runKind({ agent_id: null, tool_id: null, workflow_id: "w" })).toBe("workflow");
  });
});
