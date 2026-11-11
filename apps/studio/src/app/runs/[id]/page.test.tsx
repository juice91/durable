import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import RunDetailPage from "./page";
import { ApiError, getRun, listEvents, type Run, type RunEvent } from "@/lib/runs";

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
  beforeEach(() => {
    vi.clearAllMocks();
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
});
