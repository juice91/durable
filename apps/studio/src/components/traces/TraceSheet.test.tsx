// Task 20: the trace Sheet's header actions: Score trace (completed runs only) and Download.
import React, { useState } from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TraceSheet } from "./TraceSheet";
import type { TraceTab } from "@/lib/routes/observability";
import { getTrace, type Trace } from "@/lib/traces";
import { listAgentScorers, listReusableScorers, listRunScores } from "@/lib/scorers";

vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()),
  searchLogs: vi.fn(() => new Promise(() => undefined)),
}));
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<object>()), getTrace: vi.fn() }));
vi.mock("@/lib/scorers", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/scorers")>()),
  listReusableScorers: vi.fn(),
  listAgentScorers: vi.fn(),
  listRunScores: vi.fn(),
  scoreRun: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { agents: { list: vi.fn(async () => []) } },
}));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));

function trace(status: string): Trace {
  return {
    run: {
      run_id: "run-77", kind: "agent", agent_id: "agent-1", version: 1, status, display_status: status, input: "in", output: "out", error: null,
      heartbeat_at: null, created_at: "2026-11-16T10:00:00Z", updated_at: "2026-11-16T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
    },
    spans: [{
      id: "run:run-77", parent_id: null, kind: "run", name: "agent", start: "2026-11-16T10:00:00Z", end: "2026-11-16T10:00:05Z", status: "ok",
      attributes: { run_id: "run-77", run_kind: "agent", agent_id: "agent-1" }, input: null, output: null, truncated: {},
    }],
    totals: { runs: 1, model_requests: 0, tool_calls: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null, cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 5000, status },
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  } as unknown as Trace;
}

function View() {
  const [tab, setTab] = useState<TraceTab>("spans");
  return <TraceSheet runId="run-77" open tab={tab} onTabChange={setTab} onOpenChange={() => undefined} />;
}

describe("trace Sheet header actions", () => {
  beforeEach(() => {
    vi.mocked(listReusableScorers).mockResolvedValue([]);
    vi.mocked(listAgentScorers).mockResolvedValue([]);
    vi.mocked(listRunScores).mockResolvedValue([]);
  });
  afterEach(() => vi.restoreAllMocks());

  it("Score trace opens the picker for a completed run", async () => {
    vi.mocked(getTrace).mockResolvedValue(trace("completed"));
    render(<View />);
    const button = await screen.findByRole("button", { name: "Score trace" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(await screen.findByRole("dialog", { name: /Score trace/ })).toBeInTheDocument();
  });

  it.each(["running", "failed", "needs_attention"])("Score trace is disabled for a %s run, with the reason", async (status) => {
    vi.mocked(getTrace).mockResolvedValue(trace(status));
    render(<View />);
    const button = await screen.findByRole("button", { name: "Score trace" });
    await waitFor(() => expect(screen.getByTestId("trace-sheet-meta")).toBeInTheDocument());
    expect(button).toBeDisabled();
    expect(button.parentElement).toHaveAttribute("title", "Only completed runs can be scored");
  });

  it("Download saves the loaded trace as trace-<run id>.json", async () => {
    const loaded = trace("completed");
    vi.mocked(getTrace).mockResolvedValue(loaded);
    const create = vi.fn(() => "blob:trace");
    const revoke = vi.fn();
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke });
    const names: string[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      names.push(this.download);
    });
    render(<View />);
    const button = await screen.findByRole("button", { name: "Download trace" });
    await waitFor(() => expect(button).toBeEnabled());
    fireEvent.click(button);
    expect(names).toEqual(["trace-run-77.json"]);
    // Revoked after the click has been handled, not within it (that can cancel the download).
    expect(revoke).not.toHaveBeenCalled();
    const blob = (create.mock.calls[0] as unknown as [Blob])[0];
    expect(JSON.parse(await new Promise<string>((resolve) => { const r = new FileReader(); r.onload = () => resolve(String(r.result)); r.readAsText(blob); })).run.run_id).toBe("run-77");
    await waitFor(() => expect(revoke).toHaveBeenCalledWith("blob:trace"));
  });
});
