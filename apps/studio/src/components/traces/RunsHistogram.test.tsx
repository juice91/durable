import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RunsHistogram } from "./RunsHistogram";
import { EMPTY_FILTERS, fetchRunsHistogram } from "@/lib/traces";

vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/traces")>()), fetchRunsHistogram: vi.fn() }));
const fetchHistogram = vi.mocked(fetchRunsHistogram);

const HOURLY = {
  interval: "hour" as const,
  buckets: [
    { start: "2026-11-16T10:00:00Z", runs: 4, failed: 1 },
    { start: "2026-11-16T11:00:00Z", runs: 0, failed: 0 },
    { start: "2026-11-16T12:00:00Z", runs: 10, failed: 0 },
  ],
};

describe("RunsHistogram", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchHistogram.mockResolvedValue(HOURLY);
  });

  it("requests the histogram for the filters and draws one bar per bucket with a failed segment", async () => {
    render(<RunsHistogram filters={{ ...EMPTY_FILTERS, agent_id: "a1" }} onPickBucket={vi.fn()} />);
    const bars = await screen.findAllByRole("button", { name: /runs/ });
    expect(bars).toHaveLength(3);
    expect(fetchHistogram.mock.calls[0][0]).toMatchObject({ agent_id: "a1" });
    expect(bars[0]).toHaveAccessibleName(/4 runs, 1 failed/);
    expect(screen.getAllByTestId("histogram-failed")).toHaveLength(1);
  });

  it("clicking a bucket reports its from and to (an hour wide for hourly buckets)", async () => {
    const onPick = vi.fn();
    render(<RunsHistogram filters={EMPTY_FILTERS} onPickBucket={onPick} />);
    const bars = await screen.findAllByRole("button", { name: /runs/ });
    fireEvent.click(bars[2]);
    expect(onPick).toHaveBeenCalledWith({ from: "2026-11-16T12:00:00.000Z", to: "2026-11-16T13:00:00.000Z" });
  });

  it("a day bucket spans a day, and Enter picks it", async () => {
    fetchHistogram.mockResolvedValue({ interval: "day", buckets: [{ start: "2026-11-15T00:00:00Z", runs: 2, failed: 0 }] });
    const onPick = vi.fn();
    render(<RunsHistogram filters={EMPTY_FILTERS} onPickBucket={onPick} />);
    const bar = await screen.findByRole("button", { name: /2 runs/ });
    fireEvent.keyDown(bar, { key: "Enter" });
    expect(onPick).toHaveBeenCalledWith({ from: "2026-11-15T00:00:00.000Z", to: "2026-11-16T00:00:00.000Z" });
  });

  it("says so when the histogram cannot load", async () => {
    fetchHistogram.mockRejectedValue(new Error("boom"));
    render(<RunsHistogram filters={EMPTY_FILTERS} onPickBucket={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Could not load the runs histogram/)).toBeInTheDocument());
  });
});
