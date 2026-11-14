import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { getTrace, listTraceRuns, type Trace } from "@/lib/traces";
import { routes } from "@/lib/routes";
import { openSheetIds } from "@/components/ui/Sheet";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { searchLogs, type LogItem } from "./logsQuery";
import LogsPage from "@/app/logs/page";
import TracesPage from "@/app/traces/page";

const RUN = "11111111-2222-3333-4444-555555555555";

const nav = vi.hoisted(() => ({ path: "/logs", search: "", entries: [] as string[], listeners: new Set<() => void>(), replace: vi.fn(), push: vi.fn(), back: vi.fn() }));
function setUrl(url: string) {
  const q = url.indexOf("?");
  nav.search = q === -1 ? "" : url.slice(q + 1);
  nav.listeners.forEach((l) => l());
}
nav.replace.mockImplementation((url: string) => {
  nav.entries[Math.max(0, nav.entries.length - 1)] = url;
  setUrl(url);
});
nav.push.mockImplementation((url: string) => {
  nav.entries.push(url);
  setUrl(url);
});
nav.back.mockImplementation(() => {
  if (nav.entries.length > 1) nav.entries.pop();
  setUrl(nav.entries[nav.entries.length - 1] ?? nav.path);
});

vi.mock("next/navigation", async () => {
  const React = await import("react");
  return {
    usePathname: () => nav.path,
    useRouter: () => ({ replace: nav.replace, push: nav.push, back: nav.back, prefetch: vi.fn() }),
    useSearchParams: () => {
      const s = React.useSyncExternalStore(
        (cb) => {
          nav.listeners.add(cb);
          return () => nav.listeners.delete(cb);
        },
        () => nav.search,
        () => nav.search,
      );
      return React.useMemo(() => new URLSearchParams(s), [s]);
    },
  };
});
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/api")>()), api: { agents: { list: vi.fn(async () => []) } } }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/traces")>()), getTrace: vi.fn(), listTraceRuns: vi.fn() }));
vi.mock("@/lib/runs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/runs")>()),
  getRun: vi.fn(async () => {
    throw new Error("not needed");
  }),
}));
vi.mock("./logsQuery", async (importOriginal) => ({ ...(await importOriginal<typeof import("./logsQuery")>()), searchLogs: vi.fn() }));

const record = (message: string, run_id: string | null = RUN): LogItem => ({
  ts: "2026-11-13T10:00:03.123456+00:00", level: "error", message, service: "eap-worker", trace_id: "t1", span_id: "s1", run_id,
});

function trace(): Trace {
  const at = "2026-11-13T10:00:00Z";
  return {
    run: {
      run_id: RUN, kind: "agent", agent_id: "agent-1", version: 1, status: "completed", display_status: "completed", input: "hi", output: "ok",
      error: null, heartbeat_at: null, created_at: at, updated_at: "2026-11-13T10:00:10Z", trace_id: null, conversation_id: null, turn: null,
    } as Trace["run"],
    spans: [{ id: "root", parent_id: null, kind: "run", name: "agent run", start: at, end: "2026-11-13T10:00:10Z", status: "ok", attributes: { run_kind: "agent", run_id: RUN }, input: null, output: null, truncated: {} }],
    totals: { runs: 1, model_requests: 0, tool_calls: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null, cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 10_000, status: "completed" },
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  };
}

const escape = () => fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

beforeEach(() => {
  nav.path = routes.logs();
  nav.search = "";
  nav.entries = [nav.path];
  for (const fn of [nav.replace, nav.push, nav.back]) fn.mockClear();
  vi.mocked(searchLogs).mockResolvedValue({ items: [record("model call failed"), record("outside a run", null)], from: "2026-11-13T09:00:00Z", to: "2026-11-13T10:00:00Z", limit: 100 });
  vi.mocked(getTrace).mockResolvedValue(trace());
  vi.mocked(listTraceRuns).mockResolvedValue({ items: [], next_before: null });
});
afterEach(() => cleanup());

describe("Logs: a line opens its record, and Open trace stacks the trace", () => {
  it("line → log Sheet → Open trace stacks the trace Sheet → Escape twice returns to the list", async () => {
    render(<LogsPage />);
    const rows = await screen.findAllByTestId("log-row");
    fireEvent.click(within(rows[0]).getByRole("button", { name: /^Open the log record/ }));

    const logSheet = await screen.findByTestId("log-sheet");
    expect(within(logSheet).getByTestId("log-sheet-message")).toHaveTextContent("model call failed");
    expect(within(logSheet).getByTestId("log-sheet-run")).toHaveTextContent(RUN);
    expect(rows[0]).toHaveAttribute("aria-current", "true");

    const open = within(logSheet).getByTestId("log-sheet-open-trace");
    expect(open).toHaveAttribute("href", routes.trace(RUN));
    fireEvent.click(open);
    expect(nav.search).toBe(`trace=${RUN}`);
    const traceSheet = await screen.findByTestId("trace-sheet");
    expect(openSheetIds()).toHaveLength(2);
    expect(await within(traceSheet).findAllByTestId("trace-span-row")).toHaveLength(1);

    escape();
    await waitFor(() => expect(screen.queryByTestId("trace-sheet")).toBeNull());
    expect(screen.getByTestId("log-sheet")).toBeInTheDocument();
    expect(nav.search).toBe("");
    escape();
    await waitFor(() => expect(screen.queryByTestId("log-sheet")).toBeNull());
    expect(openSheetIds()).toHaveLength(0);
    expect(screen.getAllByTestId("log-row")).toHaveLength(2);
  });

  it("a record outside a run offers no trace, and the log Sheet has no axe violations", async () => {
    render(<LogsPage />);
    const rows = await screen.findAllByTestId("log-row");
    fireEvent.click(rows[1].querySelector("pre")!);
    const logSheet = await screen.findByTestId("log-sheet");
    expect(within(logSheet).queryByTestId("log-sheet-open-trace")).toBeNull();
    expect(logSheet).toHaveTextContent("Not logged inside a run");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("the Trace column opens the trace Sheet in place, and the logs search is not re-sent", async () => {
    render(<LogsPage />);
    await screen.findAllByTestId("log-row");
    const calls = vi.mocked(searchLogs).mock.calls.length;
    fireEvent.click(screen.getByRole("link", { name: `Open trace of run ${RUN}` }));
    expect(await screen.findByTestId("trace-sheet")).toBeInTheDocument();
    expect(screen.queryByTestId("log-sheet")).toBeNull();
    expect(vi.mocked(searchLogs).mock.calls.length).toBe(calls);
  });
});

describe("Traces list: a row opens the Sheet without reloading the list", () => {
  it("row click sets ?trace=, keeps the filters, and does not re-query the runs", async () => {
    nav.path = routes.traces();
    nav.search = "status=failed";
    vi.mocked(listTraceRuns).mockClear();
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [trace().run], next_before: null });
    render(<TracesPage />);
    const row = (await screen.findAllByTestId("trace-row"))[0];
    expect(listTraceRuns).toHaveBeenCalledTimes(1);
    fireEvent.click(row.querySelector("td:nth-child(3)")!);
    expect(nav.search).toBe(`status=failed&trace=${RUN}`);
    expect(await screen.findByTestId("trace-sheet")).toBeInTheDocument();
    expect(row).toHaveAttribute("aria-current", "true");
    expect(listTraceRuns).toHaveBeenCalledTimes(1);
  });
});

describe("filter writes keep an open trace Sheet", () => {
  it("Traces: changing a filter while the Sheet is open keeps trace and tab", async () => {
    nav.path = routes.traces();
    nav.search = `trace=${RUN}&tab=logs`;
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [trace().run], next_before: null });
    render(<TracesPage />);
    await screen.findByTestId("trace-sheet");
    // The filter bar sits under the modal Sheet; a write from it (a blur commit) must not close it.
    fireEvent.change(screen.getByLabelText(/filter by status/i), { target: { value: "failed" } });
    const params = new URLSearchParams(nav.search);
    expect(params.get("status")).toBe("failed");
    expect(params.get("trace")).toBe(RUN);
    expect(params.get("tab")).toBe("logs");
    expect(screen.getByTestId("trace-sheet")).toBeInTheDocument();
  });

  it("Logs: opening a trace cancels the pending search debounce, and a filter write keeps the Sheet", async () => {
    render(<LogsPage />);
    await screen.findAllByTestId("log-row");
    fireEvent.change(screen.getByLabelText("Search messages"), { target: { value: "late text" } });
    fireEvent.click(screen.getByRole("link", { name: `Open trace of run ${RUN}` }));
    await screen.findByTestId("trace-sheet");
    await new Promise((resolve) => setTimeout(resolve, 400)); // past the 300 ms debounce
    expect(new URLSearchParams(nav.search).get("q")).toBeNull();
    expect(new URLSearchParams(nav.search).get("trace")).toBe(RUN);
    fireEvent.change(screen.getByLabelText("Level"), { target: { value: "error" } });
    const params = new URLSearchParams(nav.search);
    expect(params.get("level")).toBe("error");
    expect(params.get("trace")).toBe(RUN);
    expect(screen.getByTestId("trace-sheet")).toBeInTheDocument();
  });

  it("Logs: opening a record cancels the pending search debounce", async () => {
    render(<LogsPage />);
    const rows = await screen.findAllByTestId("log-row");
    fireEvent.change(screen.getByLabelText("Search messages"), { target: { value: "late text" } });
    fireEvent.click(within(rows[0]).getByRole("button", { name: /^Open the log record/ }));
    await screen.findByTestId("log-sheet");
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(new URLSearchParams(nav.search).get("q")).toBeNull();
    expect(screen.getByTestId("log-sheet")).toBeInTheDocument();
  });
});
