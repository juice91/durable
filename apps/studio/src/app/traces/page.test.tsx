import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen, waitFor } from "@testing-library/react";
import TracesPage from "./page";
import { listRuns } from "@/lib/runs";
import { api } from "@/lib/api";

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

vi.mock("@/lib/api", () => ({
  api: { agents: { list: vi.fn() } },
}));

describe("TracesPage", () => {
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
    vi.mocked(api.agents.list).mockResolvedValue([
      {
        id: "agent-1",
        name: "Support assistant",
        draft_spec: {
          spec_version: 1,
          name: "Support assistant",
          instructions: "Help customers",
          model: { provider: "openai", name: "gpt-4.1-mini" },
          tools: [],
        },
        latest_version: 1,
      },
    ]);
  });

  it("renders the traces page title and header", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);

    render(<TracesPage />);

    await waitFor(() => {
      expect(screen.getByText("Traces")).toBeInTheDocument();
    });
  });

  it("renders status filter dropdown", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);

    render(<TracesPage />);

    await waitFor(() => {
      expect(screen.getByLabelText(/filter by status/i)).toBeInTheDocument();
    });
  });

  it("calls listRuns with correct parameters", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);

    render(<TracesPage />);

    await waitFor(() => {
      expect(listRuns).toHaveBeenCalledWith({
        limit: 50,
        before: undefined,
        status: undefined,
      });
    });
  });

  it("shows the agent name instead of its UUID", async () => {
    vi.mocked(listRuns).mockResolvedValue(mockResponse as any);

    render(<TracesPage />);

    expect(await screen.findByText("Support assistant")).toBeInTheDocument();
    expect(screen.queryByText("agent-1")).not.toBeInTheDocument();
  });
});
