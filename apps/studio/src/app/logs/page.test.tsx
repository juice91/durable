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
});
