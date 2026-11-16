import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TraceScores } from "./TraceScores";
import { listRunScores } from "@/lib/scorers";

vi.mock("@/lib/scorers", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/scorers")>()), listRunScores: vi.fn() }));
const list = vi.mocked(listRunScores);
const one = { seq: 1, ts: "", scorer: "Valid JSON", scorer_id: "s1", score: 1, passed: true, reason: null };

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

describe("TraceScores polling after a request", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    list.mockReset();
  });
  afterEach(() => vi.useRealTimers());

  it("does not poll without a request", async () => {
    list.mockResolvedValue([]);
    render(<TraceScores runId="r1" />);
    await tick(10_000);
    expect(list).toHaveBeenCalledTimes(1);
  });

  it("polls every 3 s after a request and stops when the new scores arrive", async () => {
    list.mockResolvedValue([]);
    render(<TraceScores runId="r1" requested={{ key: 1, count: 1, baseline: 0 }} />);
    await tick(0);
    expect(list).toHaveBeenCalledTimes(1);
    await tick(3000);
    expect(list).toHaveBeenCalledTimes(2);
    list.mockResolvedValue([one]);
    await tick(3000);
    expect(list).toHaveBeenCalledTimes(3);
    expect(screen.getByTestId("trace-score")).toBeInTheDocument();
    await tick(30_000);
    expect(list).toHaveBeenCalledTimes(3);
    expect(screen.queryByText("Scoring is taking longer than expected")).not.toBeInTheDocument();
  });

  it("gives up after 60 s with a notice and a Refresh link that loads once more", async () => {
    list.mockResolvedValue([]);
    render(<TraceScores runId="r1" requested={{ key: 1, count: 1, baseline: 0 }} />);
    await tick(61_000);
    const calls = list.mock.calls.length;
    expect(calls).toBeLessThanOrEqual(22);
    expect(screen.getByText("Scoring is taking longer than expected")).toBeInTheDocument();
    await tick(30_000);
    expect(list).toHaveBeenCalledTimes(calls);
    list.mockResolvedValue([one]);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await tick(0);
    expect(screen.getByTestId("trace-score")).toBeInTheDocument();
    expect(screen.queryByText("Scoring is taking longer than expected")).not.toBeInTheDocument();
  });
});
