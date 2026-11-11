import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import RunDetailPage from "./page";
import { getRun, listEvents } from "@/lib/runs";
import { useRouter, useParams } from "next/navigation";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  useParams: () => ({ id: "r1" }),
  usePathname: () => "/runs/r1",
}));

vi.mock("@/lib/runs", () => ({
  getRun: vi.fn(),
  listEvents: vi.fn(),
  subscribeRun: vi.fn(),
  BASE_URL: "http://localhost:8000",
  TOKEN: "Bearer dev:acme:alice",
}));

vi.mock("@/lib/sse", () => ({
  parseSSE: vi.fn(),
}));

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
    (getRun as any).mockRejectedValueOnce(new Error("Not found"));

    render(<RunDetailPage />);

    expect(await screen.findByText(/failed to load run/i)).toBeInTheDocument();
  });

  it("renders run header with agent info", async () => {
    const mockRun = {
      id: "r1",
      status: "running",
      display_status: "running",
      agent_id: "agent-123",
      version: 1,
      created_at: "2026-11-11T00:00:00Z",
    };

    (getRun as any).mockResolvedValueOnce(mockRun);
    (listEvents as any).mockResolvedValueOnce([]);

    render(<RunDetailPage />);

    expect(await screen.findByText("Run r1")).toBeInTheDocument();
    expect(screen.getByText(/agent-123/i)).toBeInTheDocument();
    expect(screen.getByText(/version: 1/i)).toBeInTheDocument();
  });

  it("shows status pill with running style", async () => {
    const mockRun = {
      id: "r1",
      status: "running",
      display_status: "running",
      agent_id: "agent-123",
      version: 1,
      created_at: "2026-11-11T00:00:00Z",
    };

    (getRun as any).mockResolvedValueOnce(mockRun);
    (listEvents as any).mockResolvedValueOnce([]);

    render(<RunDetailPage />);

    expect(await screen.findByText("Running")).toBeInTheDocument();
  });

  it("shows recovering notice when heartbeat is old", async () => {
    const oldDate = new Date(Date.now() - 25000).toISOString(); // 25 seconds ago
    const mockRun = {
      id: "r1",
      status: "running",
      display_status: "running",
      agent_id: "agent-123",
      version: 1,
      created_at: "2026-11-11T00:00:00Z",
      heartbeat_at: oldDate,
    };

    (getRun as any).mockResolvedValueOnce(mockRun);
    (listEvents as any).mockResolvedValueOnce([]);

    render(<RunDetailPage />);

    // Wait for the run to load
    await screen.findByText(/agent-123/i);
    
    // Check for the specific recovery message in the alert box
    const alertBox = screen.getByText(/events may be delayed as the system recovers/i);
    expect(alertBox).toBeInTheDocument();
  });

  it("renders event timeline", async () => {
    const mockRun = {
      id: "r1",
      status: "completed",
      display_status: "completed",
      agent_id: "agent-123",
      version: 1,
      created_at: "2026-11-11T00:00:00Z",
    };

    const mockEvents = [
      {
        seq: 1,
        type: "run_started",
        data: { input: "Hello" },
        ts: "2026-11-11T00:00:00Z",
      },
      {
        seq: 2,
        type: "model_message",
        data: { text: "Hello!" },
        ts: "2026-11-11T00:00:01Z",
      },
      {
        seq: 3,
        type: "run_completed",
        data: {},
        ts: "2026-11-11T00:00:02Z",
      },
    ];

    (getRun as any).mockResolvedValueOnce(mockRun);
    (listEvents as any).mockResolvedValueOnce(mockEvents);

    render(<RunDetailPage />);

    expect(await screen.findByText("Event Timeline")).toBeInTheDocument();
    expect(screen.getByText("seq 1")).toBeInTheDocument();
    expect(screen.getByText("seq 2")).toBeInTheDocument();
    expect(screen.getByText("seq 3")).toBeInTheDocument();
  });
});
