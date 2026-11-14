import { describe, it, expect, vi, beforeEach } from "vitest";
import React, { useState } from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import RunDetailRedirect from "./page";
import TraceDetailRedirect from "@/app/traces/[runId]/page";
import { TraceSheet } from "@/components/traces/TraceSheet";
import { getTrace, type Trace } from "@/lib/traces";
import type { TraceTab } from "@/lib/routes/observability";
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
import { routes } from "@/lib/routes";

const redirect = vi.hoisted(() => vi.fn((url: string) => {
  throw Object.assign(new Error("NEXT_REDIRECT"), { redirectTo: url });
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  useParams: () => ({ id: "r1" }),
  usePathname: () => "/traces",
  redirect,
}));

// SL.12: the run page is the trace Sheet's Events tab. The Sheet's own trace load stays pending
// unless a test resolves it (the old page did not load the trace until its Trace tab was opened).
vi.mock("@/lib/traces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/traces")>()),
  getTrace: vi.fn(() => new Promise(() => undefined)),
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { agents: { list: vi.fn(async () => []) } },
}));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));
vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()),
  searchLogs: vi.fn(async () => ({ items: [], from: "2026-11-11T17:00:00Z", to: "2026-11-11T18:00:00Z", limit: 500 })),
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

/** The former run page: the trace Sheet of run "r1" on its Events tab, tabs switchable. */
function RunDetailPage({ initialTab = "events" as TraceTab }: { initialTab?: TraceTab }) {
  const [tab, setTab] = useState<TraceTab>(initialTab);
  return <TraceSheet runId={ticketRun.run_id} open onOpenChange={() => undefined} tab={tab} onTabChange={setTab} />;
}

function traceFixture(): Trace {
  return {
    run: { ...ticketRun, kind: "agent", tool_id: null, workflow_id: null, workflow_version: null } as Trace["run"],
    spans: [
      { id: "run", parent_id: null, kind: "run", name: "agent run", start: ticketRun.created_at, end: ticketRun.updated_at, status: "ok", attributes: { run_kind: "agent", run_id: ticketRun.run_id, agent_id: ticketRun.agent_id, agent_version: 1 }, input: null, output: null, truncated: {} },
    ],
    totals: {
      runs: 1, model_requests: 0, tool_calls: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null,
      cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 13_000, status: "completed",
    },
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  };
}

describe("the retired run and trace pages redirect to the trace Sheet (SL.12)", () => {
  beforeEach(() => {
    redirect.mockClear();
  });

  async function target(run: () => Promise<unknown>): Promise<string> {
    await expect(run()).rejects.toThrow("NEXT_REDIRECT");
    return redirect.mock.calls.at(-1)![0];
  }

  it("/runs/:id goes to the Sheet's Events tab, /traces/:runId to its Spans tab", async () => {
    expect(await target(() => RunDetailRedirect({ params: Promise.resolve({ id: "r1" }), searchParams: Promise.resolve({}) }))).toBe(routes.run("r1"));
    expect(routes.run("r1")).toBe(routes.trace("r1", { tab: "events" }));
    expect(await target(() => TraceDetailRedirect({ params: Promise.resolve({ runId: "r1" }), searchParams: Promise.resolve({ x: "1" }) }))).toBe(routes.trace("r1"));
  });

  it("keeps an id with reserved characters in one parameter, and sends a dot-only id to the list", async () => {
    const id = "run:1/x?y#z";
    const url = new URL(await target(() => RunDetailRedirect({ params: Promise.resolve({ id }), searchParams: Promise.resolve({}) })), "http://studio.test");
    expect(url.pathname).toBe(routes.traces());
    expect(url.searchParams.get("trace")).toBe(id);
    expect(url.searchParams.get("tab")).toBe("events");
    expect(await target(() => TraceDetailRedirect({ params: Promise.resolve({ runId: ".." }), searchParams: Promise.resolve({}) }))).toBe(routes.traces());
  });
});

describe("RunDetailPage (the trace Sheet's Events tab)", () => {
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
    expect(link).toHaveAttribute("href", routes.workflowRun("wf-abcdef123456", ticketRun.run_id));
    expect(screen.queryByText(/Agent: null|Version: latest/)).toBeNull();
  });

  it("shows a workflow's child agent run as part of its workflow run", async () => {
    (getRun as any).mockResolvedValueOnce({ ...ticketRun, kind: "agent", parent_run_id: "parent-run-0001", parent_step_id: "classify" });
    (listEvents as any).mockResolvedValueOnce([]);
    render(<RunDetailPage />);
    expect(await screen.findByTestId("run-parent-link")).toHaveAttribute("href", routes.run("parent-run-0001"));
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

  it("switches to the Spans tab, which shows the run's span tree", async () => {
    vi.mocked(getRun).mockResolvedValue(ticketRun);
    vi.mocked(listEvents).mockResolvedValue([]);
    vi.mocked(subscribeRun).mockReturnValue(() => undefined);
    vi.mocked(getTrace).mockResolvedValue(traceFixture());
    render(<RunDetailPage />);
    const tab = await screen.findByRole("tab", { name: "Spans" });
    expect(screen.getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByTestId("trace-span-row")).toBeNull();
    fireEvent.mouseDown(tab);
    fireEvent.click(tab);
    expect(tab).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByTestId("trace-span-row")).toBeInTheDocument();
    // The full view is the Sheet itself: its run id and "Run details" (the Events tab) link.
    expect(screen.getByTestId("trace-sheet-run-id")).toHaveTextContent(`Run ${ticketRun.run_id}`);
    expect(screen.getByTestId("span-link-run")).toHaveAttribute("href", routes.run(ticketRun.run_id));
  });

  it("follows the ARIA tabs pattern: roving tabIndex and arrow, Home and End keys", async () => {
    vi.mocked(getRun).mockResolvedValue(ticketRun);
    vi.mocked(listEvents).mockResolvedValue([]);
    vi.mocked(subscribeRun).mockReturnValue(() => undefined);
    render(<RunDetailPage />);
    const events = await screen.findByRole("tab", { name: "Events" });
    const spans = screen.getByRole("tab", { name: "Spans" });
    const logs = screen.getByRole("tab", { name: "Logs" });
    // Radix tabs: the tab list is the one tab stop and hands focus to the selected tab, which then
    // holds tabIndex 0 while the others hold -1.
    expect(screen.getByRole("tablist", { name: "Trace views" })).toHaveAttribute("tabindex", "0");
    act(() => events.focus());
    expect(events).toHaveAttribute("tabindex", "0");
    expect(spans).toHaveAttribute("tabindex", "-1");
    // Radix moves focus on the next tick; focusing a tab selects it (automatic activation).
    fireEvent.keyDown(events, { key: "ArrowRight" });
    await waitFor(() => expect(logs).toHaveAttribute("aria-selected", "true"));
    expect(logs).toHaveAttribute("tabindex", "0");
    expect(document.activeElement).toBe(logs);
    fireEvent.keyDown(logs, { key: "ArrowRight" }); // wraps
    await waitFor(() => expect(spans).toHaveAttribute("aria-selected", "true"));
    expect(document.activeElement).toBe(spans);
    fireEvent.keyDown(spans, { key: "End" });
    await waitFor(() => expect(logs).toHaveAttribute("aria-selected", "true"));
    fireEvent.keyDown(logs, { key: "Home" });
    await waitFor(() => expect(spans).toHaveAttribute("aria-selected", "true"));
    fireEvent.keyDown(spans, { key: "ArrowLeft" }); // wraps backwards
    await waitFor(() => expect(logs).toHaveAttribute("aria-selected", "true"));
  });
});
