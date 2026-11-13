import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen, waitFor } from "@testing-library/react";
import LogsPage from "./page";
import { listRuns } from "@/lib/runs";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

vi.mock("@/lib/runs", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/runs")>();
  return {
    ...original,
    listRuns: vi.fn(),
  };
});

describe("LogsPage", () => {
  const mockRuns = [
    {
      run_id: "run-1",
      agent_id: "agent-1",
      version: 1,
      status: "completed",
      display_status: "completed",
      input: "test input",
      output: "test output",
      error: null,
      heartbeat_at: null,
      created_at: "2026-11-12T10:00:00Z",
      updated_at: "2026-11-12T10:00:05Z",
      trace_id: "trace-abc123",
      conversation_id: "conv-123",
      turn: 1,
    },
  ];

  const mockResponse = {
    items: mockRuns,
    next_before: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("labels a workflow run with its workflow, not 'Tool: unknown'", async () => {
    (listRuns as any).mockResolvedValue({ items: [...mockRuns, {
      run_id: "wfrun-1", kind: "workflow" as const, agent_id: null, version: null, tool_id: null,
      workflow_id: "wf-abcdef123456", workflow_version: 3, status: "running", display_status: "running",
      input: "{}", output: null, error: null, heartbeat_at: null, created_at: "2026-11-12T11:00:00Z",
      updated_at: "2026-11-12T11:00:05Z", trace_id: null, conversation_id: null, turn: null,
    }], next_before: null });
    render(<LogsPage />);
    const link = await screen.findByRole("link", { name: "Workflow: wf-abcde... v3" });
    expect(link).toHaveAttribute("href", "/workflows/wf-abcdef123456/runs/wfrun-1");
    expect(screen.queryByText(/Tool: unknown/)).toBeNull();
  });

  it("renders the logs page title and header", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);

    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByText("Logs")).toBeInTheDocument();
    });
  });

  it("renders status filter dropdown", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);

    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByLabelText(/filter by status/i)).toBeInTheDocument();
    });
  });

  it("renders View all platform logs link", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);

    render(<LogsPage />);

    await waitFor(() => {
      expect(screen.getByRole("link", { name: /view all platform logs/i })).toBeInTheDocument();
    });
  });

  it("calls listRuns with correct parameters", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);

    render(<LogsPage />);

    await waitFor(() => {
      expect(listRuns).toHaveBeenCalledWith({
        limit: 50,
        before: undefined,
        status: undefined,
      });
    });
  });

  it("labels agent, tool, workflow and child rows and links workflow and parent runs", async () => {
    (listRuns as any).mockResolvedValue({
      items: [
        {
          run_id: "agent-run-1", kind: "agent", agent_id: "agent-abcdef12", version: 1, tool_id: null,
          status: "completed", display_status: "completed", input: "hi", output: "ok", error: null,
          heartbeat_at: null, created_at: "2026-11-13T10:00:00Z", updated_at: "2026-11-13T10:00:05Z",
          trace_id: "trace-agent", conversation_id: null, turn: null,
        },
        {
          run_id: "tool-run-1", kind: "tool", agent_id: null, version: null, tool_id: "clock.now",
          status: "completed", display_status: "completed", input: "{}", output: "12:00", error: null,
          heartbeat_at: null, created_at: "2026-11-13T10:01:00Z", updated_at: "2026-11-13T10:01:01Z",
          trace_id: null, conversation_id: null, turn: null,
        },
        {
          run_id: "wfrun-rows", kind: "workflow", agent_id: null, version: null, tool_id: null,
          workflow_id: "wf-abcdef123456", workflow_version: 3, status: "running", display_status: "running",
          input: "{}", output: null, error: null, heartbeat_at: null, created_at: "2026-11-13T10:02:00Z",
          updated_at: "2026-11-13T10:02:05Z", trace_id: null, conversation_id: null, turn: null,
        },
        {
          run_id: "child-run-1", kind: "agent", agent_id: "agent-abcdef12", version: 2,
          parent_run_id: "parent-run-0001", parent_step_id: "classify",
          status: "completed", display_status: "completed", input: "step", output: "done", error: null,
          heartbeat_at: null, created_at: "2026-11-13T10:03:00Z", updated_at: "2026-11-13T10:03:04Z",
          trace_id: null, conversation_id: null, turn: null,
        },
      ],
      next_before: null,
    });
    render(<LogsPage />);

    expect(await screen.findByRole("columnheader", { name: "Agent / Tool / Workflow" })).toBeInTheDocument();

    const subjects = await screen.findAllByTestId("run-subject");
    const plainAgent = subjects.find((node) => node.getAttribute("data-kind") === "agent" && !node.textContent?.includes("Part of"));
    expect(plainAgent).toHaveTextContent("agent-ab...");
    expect(subjects.find((node) => node.getAttribute("data-kind") === "tool")).toHaveTextContent("Tool: clock.now");

    const workflow = await screen.findByRole("link", { name: "Workflow: wf-abcde... v3" });
    expect(workflow).toHaveAttribute("href", "/workflows/wf-abcdef123456/runs/wfrun-rows");
    expect(workflow.closest("[data-testid='run-subject']")).toHaveAttribute("data-kind", "workflow");

    const child = subjects.find((node) => node.textContent?.includes("Part of workflow run"));
    expect(child).toHaveAttribute("data-kind", "agent");
    expect(child).toHaveTextContent("agent-ab...");
    expect(child).toHaveTextContent("Part of workflow run parent-r... (step classify)");
    expect(screen.getByTestId("run-parent-link")).toHaveAttribute("href", "/runs/parent-run-0001");
    expect(screen.queryByText(/Tool: unknown/)).toBeNull();
  });
});
