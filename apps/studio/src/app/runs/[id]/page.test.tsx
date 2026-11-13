import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { act, render, screen, waitFor } from "@testing-library/react";
import RunDetailPage from "./page";
import {
  ApiError,
  getRun,
  listEvents,
  subscribeRun,
  type Run,
  type RunEvent,
  type SubscribeHandlers,
} from "@/lib/runs";
import * as grafanaLib from "@/lib/grafana";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  useParams: () => ({ id: "r1" }),
  usePathname: () => "/runs/r1",
}));

vi.mock("@/lib/runs", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/runs")>();
  return {
    ...actual,
    getRun: vi.fn(),
    listEvents: vi.fn(),
    subscribeRun: vi.fn(),
    deltaReducer: vi.fn((state) => state),
  };
});

vi.mock("@/lib/grafana", () => ({
  buildTraceLink: vi.fn((traceId) => `http://grafana/explore?trace=${traceId}`),
  buildLogsLink: vi.fn((traceId) => `http://grafana/explore?logs=${traceId}`),
}))

vi.mock("@/lib/sse", () => ({
  parseSSE: vi.fn(),
}));

// Copied from GET /api/v1/runs/0593b408-e845-4b31-9874-cae982e81b4e.
const ticketRun = {
  run_id: "0593b408-e845-4b31-9874-cae982e81b4e",
  agent_id: "853b7576-7cca-49af-a396-97089a390396",
  version: 1,
  status: "completed",
  display_status: "completed",
  input: "Customers in Europe see prices in USD instead of EUR on checkout.",
  output:
    "\n\nI've created a ticket for this issue: **TCK-6E5549094063**. Our team will look into the currency display problem for European customers.",
  error: null,
  heartbeat_at: "2026-11-11T17:38:15.926448+00:00",
  created_at: "2026-11-11T17:38:05.795192+00:00",
  updated_at: "2026-11-11T17:38:18.962035+00:00",
  trace_id: null,
  conversation_id: null,
  turn: null,
} satisfies Run;

// Copied from GET /api/v1/runs/0593b408-e845-4b31-9874-cae982e81b4e/events.
const ticketEvents = [
  {
    seq: 2,
    type: "tool_call",
    data: {
      args: {
        body: "Customers in Europe are shown prices in USD instead of EUR at checkout.",
        title: "European customers see USD instead of EUR on checkout",
      },
      tool_name: "tickets__create",
      tool_call_id: "chatcmpl-tool-91c11516ce5ca50c",
    },
    ts: "2026-11-11T17:38:14.289548+00:00",
  },
  {
    seq: 3,
    type: "tool_result",
    data: {
      status: "ok",
      content: { created: true, ticket_id: "TCK-6E5549094063" },
      tool_name: "tickets__create",
      tool_call_id: "chatcmpl-tool-91c11516ce5ca50c",
    },
    ts: "2026-11-11T17:38:14.319800+00:00",
  },
] satisfies RunEvent[];

describe("RunDetailPage", () => {
  let handlers: SubscribeHandlers;

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(subscribeRun).mockImplementation((_runId, nextHandlers) => {
      handlers = nextHandlers;
      return vi.fn();
    });
  });

  it("shows loading state while fetching run", async () => {
    (getRun as any).mockResolvedValueOnce(null);

    render(<RunDetailPage />);

    expect(screen.getByText(/loading run/i)).toBeInTheDocument();
  });

  it("shows error state when run not found", async () => {
    (getRun as any).mockRejectedValueOnce(new ApiError(404, "Run not found"));

    render(<RunDetailPage />);

    expect(await screen.findByText("Run not found")).toBeInTheDocument();
  });

  it("labels a workflow run with its workflow, not 'Agent: null | Version: latest'", async () => {
    (getRun as any).mockResolvedValueOnce({
      ...ticketRun, kind: "workflow", agent_id: null, version: null, workflow_id: "wf-abcdef123456", workflow_version: 3,
    });
    (listEvents as any).mockResolvedValueOnce([]);
    render(<RunDetailPage />);
    const link = await screen.findByRole("link", { name: "Workflow: wf-abcde... v3" });
    expect(link).toHaveAttribute("href", `/workflows/wf-abcdef123456/runs/${ticketRun.run_id}`);
    expect(screen.queryByText(/Agent: null|Version: latest/)).toBeNull();
  });

  it("shows a workflow's child agent run as part of its workflow run", async () => {
    (getRun as any).mockResolvedValueOnce({ ...ticketRun, kind: "agent", parent_run_id: "parent-run-0001", parent_step_id: "classify" });
    (listEvents as any).mockResolvedValueOnce([]);
    render(<RunDetailPage />);
    expect(await screen.findByTestId("run-parent-link")).toHaveAttribute("href", "/runs/parent-run-0001");
    expect(screen.getByText(/version: 1/i)).toBeInTheDocument();
  });

  it("renders run header with agent info", async () => {
    (getRun as any).mockResolvedValueOnce(ticketRun);
    (listEvents as any).mockResolvedValueOnce([]);

    render(<RunDetailPage />);

    expect(await screen.findByText("Run 0593b408-e845-4b31-9874-cae982e81b4e")).toBeInTheDocument();
    expect(screen.getByText(/853b7576-7cca-49af-a396-97089a390396/i)).toBeInTheDocument();
    expect(screen.getByText(/version: 1/i)).toBeInTheDocument();
  });

  it("shows status pill with running style", async () => {
    const mockRun: Run = {
      ...ticketRun,
      status: "running",
      display_status: "running",
      heartbeat_at: new Date().toISOString(),
    };

    (getRun as any).mockResolvedValueOnce(mockRun);
    (listEvents as any).mockResolvedValueOnce([]);

    render(<RunDetailPage />);

    expect(await screen.findByText("Running")).toBeInTheDocument();
  });

  it.each([
    ["completed", "run_completed", { output: "Done" }, "Completed", null],
    ["failed", "run_failed", { status: "failed", error: "Provider unavailable" }, "Failed", "Provider unavailable"],
    [
      "needs attention",
      "run_failed",
      { status: "needs_attention", error: "Side effect outcome is unknown" },
      "Needs Attention",
      "Side effect outcome is unknown",
    ],
  ] as const)(
    "shows %s from a live terminal event without a status frame",
    async (_caseName, type, data, expectedStatus, expectedError) => {
      const mockRun: Run = {
        ...ticketRun,
        status: "running",
        display_status: "running",
        error: null,
        heartbeat_at: new Date().toISOString(),
      };
      vi.mocked(getRun).mockResolvedValueOnce(mockRun);
      vi.mocked(listEvents).mockResolvedValueOnce([]);

      render(<RunDetailPage />);
      await waitFor(() => expect(subscribeRun).toHaveBeenCalled());

      act(() => {
        handlers.onEvent?.({
          seq: 1,
          type,
          data,
          ts: "2026-11-13T00:00:00Z",
        });
      });

      expect(await screen.findByText(expectedStatus)).toBeInTheDocument();
      if (expectedError) {
        expect(screen.getByRole("alert")).toHaveTextContent(expectedError);
      }
    },
  );

  it("falls back to failed when a live run_failed event has an invalid status", async () => {
    vi.mocked(getRun).mockResolvedValueOnce({
      ...ticketRun,
      status: "running",
      display_status: "running",
      error: null,
      heartbeat_at: new Date().toISOString(),
    });
    vi.mocked(listEvents).mockResolvedValueOnce([]);

    render(<RunDetailPage />);
    await waitFor(() => expect(subscribeRun).toHaveBeenCalled());
    act(() => {
      handlers.onEvent?.({
        seq: 1,
        type: "run_failed",
        data: { status: "completed", error: "Invalid status" },
        ts: "2026-11-13T00:00:00Z",
      });
    });

    expect(await screen.findByText("Failed")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Invalid status");
  });

  it("shows recovering notice when heartbeat is old", async () => {
    const oldDate = new Date(Date.now() - 25000).toISOString(); // 25 seconds ago
    const mockRun: Run = {
      ...ticketRun,
      status: "running",
      display_status: "running",
      heartbeat_at: oldDate,
    };

    (getRun as any).mockResolvedValueOnce(mockRun);
    (listEvents as any).mockResolvedValueOnce([]);

    render(<RunDetailPage />);

    // Wait for the run to load
    await screen.findByText(/853b7576-7cca-49af-a396-97089a390396/i);
    
    // Check for the specific recovery message in the alert box
    const alertBox = screen.getByText(/events may be delayed as the system recovers/i);
    expect(alertBox).toBeInTheDocument();
  });

  it("renders event timeline", async () => {
    (getRun as any).mockResolvedValueOnce(ticketRun);
    (listEvents as any).mockResolvedValueOnce(ticketEvents);

    render(<RunDetailPage />);

    expect(await screen.findByText("Event Timeline")).toBeInTheDocument();
    expect(screen.getByText("seq 2")).toBeInTheDocument();
    expect(screen.getByText("seq 3")).toBeInTheDocument();
    expect(screen.getByText(/European customers see USD instead of EUR on checkout/)).toBeInTheDocument();
    expect(screen.getByText(/TCK-6E5549094063/)).toBeInTheDocument();
  });

  it("renders markdown in model messages", async () => {
    const mockEvents: RunEvent[] = [
      {
        seq: 1,
        type: "model_message",
        data: {
          text: "Created **TCK-12345** for the issue.",
        },
        ts: "2026-11-12T00:00:00Z",
      },
    ];

    const mockRun: Run = {
      ...ticketRun,
      output: "Created **TCK-12345** for the issue.",
    };

    (getRun as any).mockResolvedValueOnce(mockRun);
    (listEvents as any).mockResolvedValueOnce(mockEvents);

    render(<RunDetailPage />);

    await screen.findByText("Event Timeline");
    // Markdown should be rendered as <strong> tag, not raw **text**
    const strongText = await screen.findByText("TCK-12345");
    expect(strongText.tagName).toBe("STRONG");
    expect(screen.queryByText(/\*\*TCK-12345\*\*/)).not.toBeInTheDocument();
  });

  it("renders Trace and Logs links when trace_id is present", async () => {
    const runWithTrace: Run = {
      ...ticketRun,
      trace_id: "trace-test-123",
      conversation_id: "conv-test",
      turn: 1,
    };

    (getRun as any).mockResolvedValueOnce(runWithTrace);
    (listEvents as any).mockResolvedValueOnce([]);

    render(<RunDetailPage />);

    await screen.findByText("Run 0593b408-e845-4b31-9874-cae982e81b4e");

    expect(screen.getByRole("link", { name: /trace/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /logs/i })).toBeInTheDocument();
    expect(grafanaLib.buildTraceLink).toHaveBeenCalledWith("trace-test-123");
    expect(grafanaLib.buildLogsLink).toHaveBeenCalledWith("trace-test-123");
  });

  it("does not render Trace and Logs links when trace_id is null", async () => {
    const runWithoutTrace: Run = {
      ...ticketRun,
      trace_id: null,
      conversation_id: "conv-test",
      turn: 1,
    };

    (getRun as any).mockResolvedValueOnce(runWithoutTrace);
    (listEvents as any).mockResolvedValueOnce([]);

    render(<RunDetailPage />);

    await screen.findByText("Run 0593b408-e845-4b31-9874-cae982e81b4e");

    expect(screen.queryByRole("link", { name: /trace/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /logs/i })).not.toBeInTheDocument();
    expect(grafanaLib.buildTraceLink).not.toHaveBeenCalled();
    expect(grafanaLib.buildLogsLink).not.toHaveBeenCalled();
  });
});
