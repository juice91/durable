/**
 * SL.12 acceptance (tester): deep links and the legacy redirects end to end, prev / next against the
 * list's filters, stacking and focus, history, modified clicks, Expand and the width fit, copy run
 * id, the Logs page and its Sheet, loading / error / retry states, axe in both themes, phone width.
 * The implementer's tests (TraceSheetHost, LogSheet, TraceSheet.frame, traceSheetModel) are not repeated.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ApiError } from "@/lib/api";
import { getTrace, listTraceRuns, type Trace, type TraceSpan } from "@/lib/traces";
import { ApiError as RunsApiError, getRun, listEvents, subscribeRun } from "@/lib/runs";
import { routes } from "@/lib/routes";
import { TRACE_LEGACY_RULES } from "@/lib/routes/observability";
import { openSheetIds } from "@/components/ui/Sheet";
import { expectNoAxeViolations } from "@/test/a11y";
import { searchLogs, type LogItem } from "@/components/logs/logsQuery";
import { Toaster } from "@/components/ui/Toaster";
import { traceSheetWidth, TRACE_SHEET_MIN_PX } from "./traceSheetModel";
import TracesPage from "@/app/traces/page";
import LogsPage from "@/app/logs/page";
import TraceRedirect from "@/app/traces/[runId]/page";
import RunRedirect from "@/app/runs/[id]/page";

// A stateful router with a real history stack: push, replace and back, as the browser does.
const nav = vi.hoisted(() => ({
  path: "/traces", search: "", entries: [] as string[], index: 0, listeners: new Set<() => void>(),
  push: vi.fn(), replace: vi.fn(), back: vi.fn(),
}));
function go(url: string, mode: "push" | "replace" | "pop") {
  const q = url.indexOf("?");
  nav.path = q === -1 ? url : url.slice(0, q);
  nav.search = q === -1 ? "" : url.slice(q + 1);
  if (mode === "push") nav.entries.splice(nav.index + 1, Infinity, url), (nav.index += 1);
  else if (mode === "replace") nav.entries[nav.index] = url;
  nav.listeners.forEach((l) => l());
}
nav.push.mockImplementation((url: string) => go(url, "push"));
nav.replace.mockImplementation((url: string) => go(url, "replace"));
nav.back.mockImplementation(() => {
  nav.index = Math.max(0, nav.index - 1);
  go(nav.entries[nav.index], "pop");
});

vi.mock("next/navigation", async () => {
  const React = await import("react");
  return {
    usePathname: () => nav.path,
    useRouter: () => ({ replace: nav.replace, push: nav.push, back: nav.back, prefetch: vi.fn() }),
    useSearchParams: () => {
      const s = React.useSyncExternalStore(
        (cb) => (nav.listeners.add(cb), () => nav.listeners.delete(cb)),
        () => nav.search,
        () => nav.search,
      );
      return React.useMemo(() => new URLSearchParams(s), [s]);
    },
    redirect: (url: string) => {
      throw Object.assign(new Error("NEXT_REDIRECT"), { redirectTo: url });
    },
  };
});
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/traces")>()), getTrace: vi.fn(), listTraceRuns: vi.fn() }));
vi.mock("@/lib/runs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/runs")>()),
  getRun: vi.fn(),
  listEvents: vi.fn(async () => []),
  subscribeRun: vi.fn(() => () => undefined),
}));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/api")>()), api: { agents: { list: vi.fn(async () => []) } } }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));
vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()), searchLogs: vi.fn() }));

const T = (s: number) => new Date(Date.UTC(2026, 10, 13, 10, 0, s)).toISOString();
type Run = Trace["run"];
function runOf(id: string, status = "completed", at = 0): Run {
  return {
    run_id: id, kind: "agent", agent_id: "agent-1", version: 1, tool_id: null, workflow_id: null, workflow_version: null,
    status, display_status: status, input: "hi", output: "ok", error: null, heartbeat_at: null,
    created_at: T(at), updated_at: T(at + 5), trace_id: null, conversation_id: null, turn: null,
  } as Run;
}
function span(id: string, parent: string | null, kind: TraceSpan["kind"], name: string, attributes: TraceSpan["attributes"] = {}): TraceSpan {
  return { id, parent_id: parent, kind, name, start: T(0), end: T(5), status: "ok", attributes, input: null, output: null, truncated: {} };
}
function traceOf(runId: string): Trace {
  return {
    run: runOf(runId),
    spans: [
      span("root", null, "run", `run ${runId}`, { run_kind: "agent", run_id: runId, agent_id: "agent-1" }),
      span(`child-of-${runId}`, "root", "child_run", "child", { run_id: `${runId}-child` }),
    ],
    totals: {
      runs: 2, model_requests: 0, tool_calls: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null,
      cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 5000, status: "completed",
    },
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  };
}
const record = (message: string, run_id: string | null = "r1"): LogItem => ({
  ts: "2026-11-13T10:00:03.123456+00:00", level: "error", message, service: "eap-worker", trace_id: "t1", span_id: "s1", run_id,
});
const logsReply = (items: LogItem[]) => ({ items, from: "2026-11-13T09:00:00Z", to: "2026-11-13T10:00:00Z", limit: 100 });

const sheets = () => screen.queryAllByTestId("trace-sheet");
const top = () => sheets().at(-1)!;
const escape = () => fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
const runIdOf = (sheet: HTMLElement) => within(sheet).getByTestId("trace-sheet-run-id").textContent;
const childLink = async (sheet: HTMLElement) => {
  const row = (await within(sheet).findAllByTestId("trace-span-row")).find((r) => r.getAttribute("data-kind") === "child_run")!;
  fireEvent.click(row);
  return within(sheet).findByTestId("span-link-child-trace");
};
function startAt(url: string) {
  nav.entries = [url];
  nav.index = 0;
  go(url, "replace");
}
function setWidth(px: number) {
  Object.defineProperty(window, "innerWidth", { value: px, configurable: true, writable: true });
}
const writeText = vi.fn(async (_text: string) => undefined);

beforeEach(() => {
  setWidth(1440);
  startAt("/traces");
  for (const fn of [nav.push, nav.replace, nav.back, writeText]) fn.mockClear();
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
  vi.mocked(getTrace).mockImplementation(async (id: string) => traceOf(id));
  vi.mocked(getRun).mockImplementation(async (id: string) => runOf(id));
  vi.mocked(listEvents).mockResolvedValue([]);
  vi.mocked(subscribeRun).mockReturnValue(() => undefined);
  vi.mocked(listTraceRuns).mockResolvedValue({ items: [], next_before: null });
  vi.mocked(searchLogs).mockResolvedValue(logsReply([record("model call failed"), record("outside", null)]));
});
afterEach(() => {
  cleanup();
  document.documentElement.classList.remove("dark");
});

async function redirectOf(page: () => Promise<unknown>): Promise<string> {
  try {
    await page();
  } catch (error) {
    const target = (error as { redirectTo?: string }).redirectTo;
    if (target) return target;
    throw error;
  }
  throw new Error("the page did not redirect");
}
const oldTrace = (runId: string, search: Record<string, string> = {}) =>
  redirectOf(() => TraceRedirect({ params: Promise.resolve({ runId }), searchParams: Promise.resolve(search) }));
const oldRun = (id: string, search: Record<string, string> = {}) =>
  redirectOf(() => RunRedirect({ params: Promise.resolve({ id }), searchParams: Promise.resolve(search) }));

describe("deep links, reloads and the legacy redirects, end to end", () => {
  it("/traces/:runId redirects to the Sheet's URL, and following it opens that run on Spans", async () => {
    const target = await oldTrace("r7");
    expect(target).toBe(routes.trace("r7"));
    startAt(target);
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(runIdOf(sheet)).toBe("Run r7");
    expect(within(sheet).getByRole("tab", { name: "Spans" })).toHaveAttribute("aria-selected", "true");
    expect((await within(sheet).findAllByTestId("trace-span-row")).length).toBe(2);
  });

  it("/runs/:id redirects to the Events tab, and following it shows the run's events panel", async () => {
    const target = await oldRun("r8");
    expect(target).toBe(routes.run("r8"));
    expect(target).toBe("/traces?trace=r8&tab=events");
    startAt(target);
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(within(sheet).getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "true");
    expect(await within(sheet).findByText("Event Timeline")).toBeInTheDocument();
    expect(within(sheet).getByTestId("run-events-panel")).toBeInTheDocument();
  });

  it("the old pages carry no other query, and an id that cannot be a path segment goes to the list", async () => {
    expect(await oldTrace("r7", { tab: "logs", x: "1" })).toBe("/traces?trace=r7");
    expect(await oldRun("r7", { tab: "logs" })).toBe("/traces?trace=r7&tab=events");
    expect(await oldTrace("..")).toBe(routes.traces());
    expect(await oldRun(".")).toBe(routes.traces());
    expect(routes.legacyTracePath("r7")).toBe("/traces/r7");
    expect(routes.legacyRunPath("r7")).toBe("/runs/r7");
    expect(TRACE_LEGACY_RULES.map((rule) => rule.pattern)).toEqual(["/traces/:runId", "/runs/:id"]);
  });

  it("a reload keeps the Sheet and its tab: unmount and render again from the same URL", async () => {
    startAt(routes.trace("r1", { tab: "logs" }));
    const first = render(<TracesPage />);
    expect(within(await screen.findByTestId("trace-sheet")).getByRole("tab", { name: "Logs" })).toHaveAttribute("aria-selected", "true");
    first.unmount();
    expect(sheets()).toHaveLength(0);
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(runIdOf(sheet)).toBe("Run r1");
    expect(within(sheet).getByRole("tab", { name: "Logs" })).toHaveAttribute("aria-selected", "true");
    expect(nav.replace).not.toHaveBeenCalled();
  });

  it("an unknown &tab= opens Spans, and an id with reserved characters survives the round trip", async () => {
    startAt("/traces?trace=r1&tab=bogus");
    const first = render(<TracesPage />);
    expect(within(await screen.findByTestId("trace-sheet")).getByRole("tab", { name: "Spans" })).toHaveAttribute("aria-selected", "true");
    first.unmount();
    const odd = "run 1&x=2";
    expect(routes.trace(odd)).toBe("/traces?trace=run+1%26x%3D2");
    startAt(routes.trace(odd));
    render(<TracesPage />);
    expect(runIdOf(await screen.findByTestId("trace-sheet"))).toBe(`Run ${odd}`);
    expect(getTrace).toHaveBeenLastCalledWith(odd);
  });
});

const listed = vi.mocked(listTraceRuns) as unknown as ReturnType<typeof vi.fn>;
const rowIds = () => screen.getAllByTestId("trace-row-open").map((a) => new URL(a.getAttribute("href")!, "http://x").searchParams.get("trace"));
const step = (name: "Previous trace" | "Next trace") => fireEvent.click(within(top()).getByRole("button", { name }));
const position = () => within(top()).getByTestId("trace-sheet-position").textContent;

describe("prev / next follow the list the Sheet was opened from", () => {
  it("steps through the filtered list in its displayed order, with the position, and the list stays", async () => {
    startAt("/traces?status=failed");
    listed.mockImplementation(async (filters: { status: string[] }) => ({
      items: filters.status.includes("failed")
        ? [runOf("f3", "failed", 30), runOf("f1", "failed", 10), runOf("f2", "failed", 20)]
        : [runOf("a1", "completed", 40), runOf("f1", "failed", 10)],
      next_before: null,
    }));
    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    expect(rowIds()).toEqual(["f3", "f2", "f1"]); // newest first, whatever the API order
    fireEvent.click(screen.getAllByTestId("trace-row-open")[1]);
    expect(nav.search).toBe("status=failed&trace=f2");
    await screen.findByTestId("trace-sheet");
    expect(position()).toBe("2 / 3");
    step("Next trace");
    await waitFor(() => expect(runIdOf(top())).toBe("Run f1"));
    expect(position()).toBe("3 / 3");
    expect(within(top()).getByRole("button", { name: "Next trace" })).toBeDisabled();
    step("Previous trace");
    step("Previous trace");
    await waitFor(() => expect(runIdOf(top())).toBe("Run f3"));
    expect(position()).toBe("1 / 3");
    expect(nav.search).toBe("status=failed&trace=f3");
    expect(rowIds()).toEqual(["f3", "f2", "f1"]);
    // Opening pushed one entry (Back closes the Sheet); stepping replaced it.
    expect(nav.push).toHaveBeenCalledTimes(1);
    expect(nav.entries).toEqual(["/traces?status=failed", "/traces?status=failed&trace=f3"]);
  });

  it("Load more extends the sequence; a run outside the list has no prev / next", async () => {
    listed.mockResolvedValueOnce({ items: [runOf("b", "completed", 20)], next_before: "cur" });
    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    fireEvent.click(screen.getAllByTestId("trace-row-open")[0]);
    await screen.findByTestId("trace-sheet");
    expect(position()).toBe("1 / 1");
    escape();
    await waitFor(() => expect(sheets()).toHaveLength(0));
    listed.mockResolvedValueOnce({ items: [runOf("c", "completed", 30), runOf("a", "completed", 10)], next_before: null });
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(screen.getAllByTestId("trace-row")).toHaveLength(3));
    fireEvent.click(screen.getAllByTestId("trace-row-open")[1]);
    await waitFor(() => expect(position()).toBe("2 / 3"));
    step("Previous trace");
    await waitFor(() => expect(runIdOf(top())).toBe("Run c"));
    cleanup();
    startAt(routes.trace("not-in-list"));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(within(sheet).queryByTestId("trace-sheet-position")).toBeNull();
    expect(within(sheet).queryByRole("button", { name: "Next trace" })).toBeNull();
  });

  it("stepping starts the new run from its own state: the selected span is not carried over", async () => {
    listed.mockResolvedValue({ items: [runOf("n2", "completed", 20), runOf("n1", "completed", 10)], next_before: null });
    startAt(routes.trace("n2"));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    const rows = await within(sheet).findAllByTestId("trace-span-row");
    await within(sheet).findByRole("button", { name: "Next trace" });
    fireEvent.click(rows[1]);
    expect(rows[1]).toHaveAttribute("aria-selected", "true");
    step("Next trace");
    await waitFor(() => expect(runIdOf(top())).toBe("Run n1"));
    const next = await within(top()).findAllByTestId("trace-span-row");
    expect(next[0]).toHaveAttribute("aria-selected", "true");
    expect(getTrace).toHaveBeenLastCalledWith("n1");
  });
});

const childEvent = { seq: 1, type: "step_completed", ts: T(1), data: { child_run_id: "r1-child", step_id: "s1" } };
const click = (el: HTMLElement) => {
  el.focus();
  fireEvent.click(el);
};

describe("stacking: depth limit, Escape, focus return and history", () => {
  it("Events: a child run link stacks over the Events Sheet; Escape closes it and focus returns to the link", async () => {
    vi.mocked(listEvents).mockResolvedValue([childEvent] as never);
    startAt(routes.run("r1"));
    render(<TracesPage />);
    const first = await screen.findByTestId("trace-sheet");
    const link = await within(first).findByTestId("event-child-run-link");
    expect(link).toHaveAttribute("href", routes.trace("r1-child"));
    click(link);
    await waitFor(() => expect(sheets()).toHaveLength(2));
    expect(top()).toHaveAttribute("data-trace-depth", "1");
    expect(sheets()[0]).toHaveAttribute("data-trace-depth", "0");
    expect(runIdOf(top())).toBe("Run r1-child");
    expect(nav.search).toBe("trace=r1&tab=events"); // the child is not a URL of its own
    escape();
    await waitFor(() => expect(sheets()).toHaveLength(1));
    expect(runIdOf(top())).toBe("Run r1");
    await waitFor(() => expect(link).toHaveFocus());
    expect(nav.search).toBe("trace=r1&tab=events");
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("Traces list: Escape twice returns focus level by level, to the row link that opened the first Sheet", async () => {
    listed.mockResolvedValue({ items: [runOf("r1")], next_before: null });
    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    const rowLink = screen.getByTestId("trace-row-open");
    click(rowLink);
    const first = await screen.findByTestId("trace-sheet");
    const spanLink = await childLink(first);
    click(spanLink);
    await waitFor(() => expect(sheets()).toHaveLength(2));
    expect(openSheetIds()).toHaveLength(2);
    escape();
    await waitFor(() => expect(sheets()).toHaveLength(1));
    expect(nav.search).toBe("trace=r1");
    await waitFor(() => expect(spanLink).toHaveFocus());
    escape();
    await waitFor(() => expect(sheets()).toHaveLength(0));
    expect(nav.search).toBe("");
    await waitFor(() => expect(rowLink).toHaveFocus());
    expect(openSheetIds()).toHaveLength(0);
  });

  it("a stacked child opened from the child replaces it: never more than two trace Sheets, URL unchanged", async () => {
    startAt(routes.trace("r1"));
    render(<TracesPage />);
    click(await childLink(await screen.findByTestId("trace-sheet")));
    await waitFor(() => expect(sheets()).toHaveLength(2));
    click(await childLink(top()));
    await waitFor(() => expect(runIdOf(top())).toBe("Run r1-child-child"));
    expect(sheets()).toHaveLength(2);
    expect(openSheetIds()).toHaveLength(2);
    expect(nav.search).toBe("trace=r1");
  });

  it("history: opening pushes one entry; stepping and tabs replace it; closing goes Back, so Back never reopens it", async () => {
    listed.mockResolvedValue({ items: [runOf("r2", "completed", 20), runOf("r1", "completed", 10)], next_before: null });
    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    click(screen.getAllByTestId("trace-row-open")[0]);
    await screen.findByTestId("trace-sheet");
    expect(nav.push).toHaveBeenCalledTimes(1);
    expect(nav.entries).toEqual(["/traces", "/traces?trace=r2"]);
    await within(top()).findByRole("button", { name: "Next trace" });
    step("Next trace");
    await waitFor(() => expect(runIdOf(top())).toBe("Run r1"));
    fireEvent.mouseDown(within(top()).getByRole("tab", { name: "Events" }));
    expect(nav.search).toBe("trace=r1&tab=events");
    // Stepping and the tab replaced the Sheet's entry: still one entry above the list.
    expect(nav.push).toHaveBeenCalledTimes(1);
    expect(nav.entries).toEqual(["/traces", "/traces?trace=r1&tab=events"]);
    expect(nav.index).toBe(1);
    fireEvent.click(within(top()).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(sheets()).toHaveLength(0));
    // Close went Back to the list's entry.
    expect(nav.back).toHaveBeenCalledTimes(1);
    expect(nav.index).toBe(0);
    expect(nav.search).toBe("");
    act(() => nav.back());
    expect(nav.path).toBe("/traces");
    expect(sheets()).toHaveLength(0);
  });

  it("Back closes a Sheet opened from the list", async () => {
    listed.mockResolvedValue({ items: [runOf("r1", "completed", 10)], next_before: null });
    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    click(screen.getByTestId("trace-row-open"));
    await screen.findByTestId("trace-sheet");
    act(() => nav.back());
    await waitFor(() => expect(sheets()).toHaveLength(0));
    expect(nav.search).toBe("");
  });

  it("Back to the entry before the Sheet's URL closes the Sheet with it", async () => {
    startAt("/traces");
    nav.push("/logs");
    nav.push(routes.trace("r1"));
    render(<TracesPage />);
    expect(runIdOf(await screen.findByTestId("trace-sheet"))).toBe("Run r1");
    act(() => nav.back());
    await waitFor(() => expect(sheets()).toHaveLength(0));
    expect(nav.path).toBe("/logs");
  });
});

const openLogRow = async (index = 0) => {
  const rows = await screen.findAllByTestId("log-row");
  const button = within(rows[index]).getByRole("button", { name: /^Open the log record/ });
  click(button);
  return { button, sheet: await screen.findByTestId("log-sheet") };
};

describe("Logs: record Sheet, Open trace, focus and the depth limit", () => {
  it("Open trace stacks the trace over the log Sheet; each Escape returns focus one level down", async () => {
    startAt("/logs");
    render(<LogsPage />);
    const { button, sheet } = await openLogRow();
    const open = within(sheet).getByTestId("log-sheet-open-trace");
    click(open);
    const trace = await screen.findByTestId("trace-sheet");
    expect(trace).toHaveAttribute("data-trace-depth", "0");
    expect(openSheetIds()).toHaveLength(2);
    expect(await within(trace).findAllByTestId("trace-span-row")).toHaveLength(2);
    escape();
    await waitFor(() => expect(sheets()).toHaveLength(0));
    expect(screen.getByTestId("log-sheet")).toBeInTheDocument();
    await waitFor(() => expect(open).toHaveFocus());
    escape();
    await waitFor(() => expect(screen.queryByTestId("log-sheet")).toBeNull());
    await waitFor(() => expect(button).toHaveFocus());
    expect(nav.search).toBe("");
  });

  it("a child run opened from that trace makes a third Sheet: the log Sheet below gives way, two stay", async () => {
    startAt("/logs");
    render(<LogsPage />);
    const { sheet } = await openLogRow();
    click(within(sheet).getByTestId("log-sheet-open-trace"));
    click(await childLink(await screen.findByTestId("trace-sheet")));
    await waitFor(() => expect(sheets()).toHaveLength(2));
    await waitFor(() => expect(screen.queryByTestId("log-sheet")).toBeNull());
    expect(openSheetIds()).toHaveLength(2);
    expect(runIdOf(top())).toBe("Run r1-child");
    escape();
    await waitFor(() => expect(sheets()).toHaveLength(1));
    escape();
    await waitFor(() => expect(sheets()).toHaveLength(0));
    expect(openSheetIds()).toHaveLength(0);
    expect(screen.getAllByTestId("log-row")).toHaveLength(2);
  });

  it("the log Sheet shows the record's fields; a record outside a run says so", async () => {
    startAt("/logs");
    render(<LogsPage />);
    const { sheet } = await openLogRow();
    expect(sheet).toHaveAccessibleName("Log record");
    expect(within(sheet).getByTestId("log-sheet-message")).toHaveTextContent("model call failed");
    expect(within(sheet).getByTestId("log-sheet-run")).toHaveTextContent("r1");
    for (const label of ["Time", "Level", "Service", "Run", "Trace id", "Span id"]) expect(within(sheet).getByText(label)).toBeInTheDocument();
    expect(within(sheet).getByText("eap-worker", { selector: "dd" })).toBeInTheDocument();
    expect(within(sheet).getByText("t1")).toBeInTheDocument();
    expect(within(sheet).getByTestId("log-sheet-open-trace")).toHaveAttribute("href", routes.trace("r1"));
    fireEvent.keyDown(sheet, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("log-sheet")).toBeNull());
    const other = await openLogRow(1);
    expect(within(other.sheet).getByTestId("log-sheet-run")).toHaveTextContent("-");
    expect(within(other.sheet).queryByTestId("log-sheet-open-trace")).toBeNull();
  });

  it("the filter row is unchanged: the same named controls and actions as before the Sheets", async () => {
    startAt("/logs?range=1h");
    render(<LogsPage />);
    await screen.findAllByTestId("log-row");
    const form = screen.getByRole("search", { name: "Log filters" });
    for (const name of ["Search messages", "Level", "Agent", "Run", "Time range"]) expect(within(form).getByLabelText(name)).toBeInTheDocument();
    for (const name of ["Refresh", "Clear filters"]) expect(screen.getByRole("button", { name })).toBeInTheDocument();
    expect(screen.getByTestId("logs-table")).toBeInTheDocument();
    expect(document.querySelectorAll("h1")).toHaveLength(1);
  });
});

describe("modified clicks follow the href", () => {
  const modified: Array<[string, MouseEventInit]> = [
    ["Cmd", { metaKey: true }],
    ["Ctrl", { ctrlKey: true }],
    ["Shift", { shiftKey: true }],
    ["middle button", { button: 1 }],
  ];
  it.each(modified)("%s-click on a row's trace link leaves the Sheet closed and the URL alone", async (_label, init) => {
    listed.mockResolvedValue({ items: [runOf("r1")], next_before: null });
    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    const link = screen.getByTestId("trace-row-open");
    expect(link).toHaveAttribute("href", routes.trace("r1"));
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, ...init });
    link.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false); // the browser follows the href (new tab or window)
    expect(sheets()).toHaveLength(0);
    expect(nav.replace).not.toHaveBeenCalled();
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("inside a Sheet, a modified click on a child link does not stack; a plain click does", async () => {
    vi.mocked(listEvents).mockResolvedValue([childEvent] as never);
    startAt(routes.run("r1"));
    render(<TracesPage />);
    const link = await within(await screen.findByTestId("trace-sheet")).findByTestId("event-child-run-link");
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, ctrlKey: true });
    link.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(sheets()).toHaveLength(1);
    fireEvent.click(link);
    await waitFor(() => expect(sheets()).toHaveLength(2));
  });

  it("a Logs page link opens in place on a plain click, and follows the href when modified", async () => {
    startAt("/logs");
    render(<LogsPage />);
    const link = await screen.findByRole("link", { name: "Open trace of run r1" });
    expect(link).toHaveAttribute("href", routes.trace("r1"));
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, metaKey: true });
    link.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(sheets()).toHaveLength(0);
    fireEvent.click(link);
    expect(await screen.findByTestId("trace-sheet")).toBeInTheDocument();
  });
});

const layout = () => screen.getByTestId("trace-spans-layout");
const expandButton = (sheet: HTMLElement) => within(sheet).getByRole("button", { name: /Expand to full width|Collapse to the side/ });

describe("Expand and the width fit, at injected widths", () => {
  it("1440 px: the Sheet takes the fit width with the detail beside the tree; Expand goes full and back", async () => {
    startAt(routes.trace("r1"));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    await within(sheet).findAllByTestId("trace-span-row");
    const fit = traceSheetWidth({ viewport: 1440, sidebarCollapsed: false })!;
    expect(fit).toBeGreaterThan(TRACE_SHEET_MIN_PX);
    expect(sheet.style.width).toBe(`${fit}px`);
    expect(layout()).toHaveAttribute("data-detail-beside", "true");
    click(expandButton(sheet));
    expect(sheet.style.width).toBe("");
    expect(sheet).toHaveAttribute("data-sheet-size", "full");
    expect(expandButton(sheet)).toHaveAttribute("aria-pressed", "true");
    expect(layout()).toHaveAttribute("data-detail-beside", "true");
    click(expandButton(sheet));
    expect(sheet.style.width).toBe(`${fit}px`);
    expect(sheet).toHaveAttribute("data-sheet-size", "wide");
    expect(expandButton(sheet)).toHaveAttribute("aria-pressed", "false");
  });

  it("800 px: the Sheet is never narrower than tree plus detail, so the detail still sits beside", async () => {
    setWidth(800);
    startAt(routes.trace("r1"));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    await within(sheet).findAllByTestId("trace-span-row");
    expect(sheet.style.width).toBe(`${TRACE_SHEET_MIN_PX}px`);
    expect(layout()).toHaveAttribute("data-detail-beside", "true");
  });

  it("below 768 px the Sheet is full width and the detail goes below; a live resize re-fits", async () => {
    startAt(routes.trace("r1"));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    await within(sheet).findAllByTestId("trace-span-row");
    expect(layout()).toHaveAttribute("data-detail-beside", "true");
    act(() => {
      setWidth(700);
      window.dispatchEvent(new Event("resize"));
    });
    await waitFor(() => expect(layout()).toHaveAttribute("data-detail-beside", "false"));
    expect(sheet.style.width).toBe("");
    act(() => {
      setWidth(1440);
      window.dispatchEvent(new Event("resize"));
    });
    await waitFor(() => expect(layout()).toHaveAttribute("data-detail-beside", "true"));
    expect(sheet.style.width).toBe(`${traceSheetWidth({ viewport: 1440, sidebarCollapsed: false })}px`);
  });

  it("the log Sheet fits too: 480 px at desktop widths, full width on a phone", async () => {
    startAt("/logs");
    const view = render(<LogsPage />);
    const wide = (await openLogRow()).sheet;
    expect(wide.style.width).toBe("480px");
    view.unmount();
    setWidth(390);
    render(<LogsPage />);
    const narrow = (await openLogRow()).sheet;
    expect(narrow.style.width).toBe("");
  });
});

describe("copy run id", () => {
  it("writes the run id to the clipboard and confirms; a refused write is reported, not thrown", async () => {
    startAt(routes.trace("r-copy"));
    render(<><TracesPage /><Toaster /></>);
    const sheet = await screen.findByTestId("trace-sheet");
    click(within(sheet).getByRole("button", { name: "Copy run id" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("r-copy"));
    expect(await screen.findByText("Run id copied")).toBeInTheDocument();
    writeText.mockRejectedValueOnce(new Error("denied"));
    click(within(sheet).getByRole("button", { name: "Copy run id" }));
    expect(await screen.findByText("Could not copy the run id")).toBeInTheDocument();
    expect(sheets()).toHaveLength(1);
  });

  it("with no clipboard at all the click still does not throw", async () => {
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
    startAt(routes.trace("r1"));
    render(<><TracesPage /><Toaster /></>);
    click(within(await screen.findByTestId("trace-sheet")).getByRole("button", { name: "Copy run id" }));
    expect(await screen.findByText("Could not copy the run id")).toBeInTheDocument();
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => ((resolve = res), (reject = rej)));
  return { promise, resolve, reject };
}
const closeTop = async () => {
  const before = sheets().length;
  click(within(top()).getByRole("button", { name: "Close" }));
  await waitFor(() => expect(sheets()).toHaveLength(before - 1));
};

describe("loading, error and retry states", () => {
  it("trace: 'Loading trace...' on Spans and Logs, then the spans", async () => {
    const slow = deferred<Trace>();
    vi.mocked(getTrace).mockReturnValueOnce(slow.promise);
    startAt(routes.trace("r1"));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(within(sheet).getByText("Loading trace...")).toBeInTheDocument();
    expect(within(sheet).queryByTestId("trace-sheet-meta")).toBeNull();
    fireEvent.mouseDown(within(sheet).getByRole("tab", { name: "Logs" }));
    expect(within(sheet).getByText("Loading trace...")).toBeInTheDocument();
    await act(async () => slow.resolve(traceOf("r1")));
    expect(await within(sheet).findByTestId("trace-logs-panel")).toBeInTheDocument();
    expect(await within(sheet).findByTestId("trace-sheet-meta")).toHaveTextContent("Agent run");
  });

  it.each([
    [new ApiError(404, "gone"), "Run not found"],
    [new ApiError(500, "boom"), "Failed to load the trace"],
    [new TypeError("fetch failed"), "Failed to load the trace"],
  ])("trace: a failed load (%s) is an alert in the Sheet, with the header and tabs still usable; reopening retries", async (failure, text) => {
    vi.mocked(getTrace).mockRejectedValueOnce(failure);
    startAt(routes.trace("r1"));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(await within(sheet).findByRole("alert")).toHaveTextContent(text);
    expect(within(sheet).queryAllByTestId("trace-span-row")).toHaveLength(0);
    expect(within(sheet).getByTestId("trace-sheet-run-id")).toHaveTextContent("Run r1");
    fireEvent.mouseDown(within(sheet).getByRole("tab", { name: "Logs" }));
    expect(await within(sheet).findByRole("alert")).toHaveTextContent(text);
    await closeTop();
    startAt(routes.trace("r1"));
    expect((await within(await screen.findByTestId("trace-sheet")).findAllByTestId("trace-span-row")).length).toBe(2);
  });

  it("events: 'Loading run...', then the panel; a 404 and another failure each say so", async () => {
    const slow = deferred<Trace["run"]>();
    vi.mocked(getRun).mockReturnValueOnce(slow.promise);
    startAt(routes.run("r1"));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(within(sheet).getByText("Loading run...")).toBeInTheDocument();
    await act(async () => slow.resolve(runOf("r1")));
    expect(await within(sheet).findByText("Event Timeline")).toBeInTheDocument();
    await closeTop();
    vi.mocked(getRun).mockRejectedValueOnce(new RunsApiError(404, "gone"));
    startAt(routes.run("r2"));
    expect(await within(await screen.findByTestId("trace-sheet")).findByTestId("run-events-panel")).toHaveTextContent("Run not found");
    await closeTop();
    vi.mocked(getRun).mockRejectedValueOnce(new RunsApiError(500, "boom"));
    startAt(routes.run("r3"));
    expect(await within(await screen.findByTestId("trace-sheet")).findByTestId("run-events-panel")).toHaveTextContent("Failed to load run");
  });

  it("events: a failed run shows its error as an alert; a recovering run says the worker is restarting", async () => {
    vi.mocked(getRun).mockResolvedValueOnce({ ...runOf("r1", "failed"), error: "model timed out" } as never);
    startAt(routes.run("r1"));
    render(<TracesPage />);
    expect(await within(await screen.findByTestId("trace-sheet")).findByRole("alert")).toHaveTextContent("model timed out");
    await closeTop();
    vi.mocked(getRun).mockResolvedValueOnce({ ...runOf("r2", "running"), heartbeat_at: new Date(Date.now() - 60_000).toISOString() } as never);
    startAt(routes.run("r2"));
    expect(await within(await screen.findByTestId("trace-sheet")).findByText(/The worker is restarting/)).toBeInTheDocument();
  });

  it("logs tab: loading, an error with Retry that re-sends the same query, then the records", async () => {
    const slow = deferred<ReturnType<typeof logsReply>>();
    vi.mocked(searchLogs).mockReset();
    vi.mocked(searchLogs).mockReturnValueOnce(slow.promise).mockResolvedValue(logsReply([record("recovered")]));
    startAt(routes.trace("r1", { tab: "logs" }));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(await within(sheet).findByText("Loading logs...")).toBeInTheDocument();
    await act(async () => slow.reject(new ApiError(502, "x")));
    const box = await within(sheet).findByTestId("logs-error");
    expect(box).toHaveTextContent("Could not load logs");
    click(within(box).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(within(sheet).getByTestId("trace-logs-panel")).toHaveTextContent("recovered"));
    const calls = vi.mocked(searchLogs).mock.calls;
    expect(calls.length).toBe(2);
    expect(calls[1][0]).toEqual(calls[0][0]);
  });

  it("logs tab: 503 is the Loki-unavailable state, and the trace beside it stays usable", async () => {
    vi.mocked(searchLogs).mockRejectedValue(new ApiError(503, "Log search is not configured"));
    startAt(routes.trace("r1", { tab: "logs" }));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(await within(sheet).findByTestId("logs-unavailable")).toHaveTextContent("Loki unavailable");
    fireEvent.mouseDown(within(sheet).getByRole("tab", { name: "Spans" }));
    expect((await within(sheet).findAllByTestId("trace-span-row")).length).toBe(2);
  });

  it("Logs page: Retry after a failure shows the records", async () => {
    vi.mocked(searchLogs).mockRejectedValueOnce(new TypeError("fetch failed"));
    startAt("/logs");
    render(<LogsPage />);
    const box = await screen.findByTestId("logs-error");
    click(within(box).getByRole("button", { name: "Retry" }));
    expect((await screen.findAllByTestId("log-row")).length).toBe(2);
    expect(screen.queryByTestId("logs-error")).toBeNull();
  });
});

describe.each(["light", "dark"] as const)("axe with Sheets open (%s theme)", (theme) => {
  beforeEach(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  });
  const settle = () => act(async () => void (await new Promise((resolve) => setTimeout(resolve, 20))));
  const oneH1 = () => {
    expect(document.documentElement.classList.contains("dark")).toBe(theme === "dark");
    expect(document.querySelectorAll("h1")).toHaveLength(1);
    expect(document.querySelectorAll("[data-sheet] h1")).toHaveLength(0);
  };

  it("Traces page: the trace Sheet on each tab, then two stacked trace Sheets", async () => {
    listed.mockResolvedValue({ items: [runOf("r1"), runOf("r0", "failed", 0)], next_before: null });
    vi.mocked(listEvents).mockResolvedValue([childEvent] as never);
    startAt(routes.trace("r1"));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    await within(sheet).findAllByTestId("trace-span-row");
    await settle();
    oneH1();
    await expectNoAxeViolations(document.body);
    for (const name of ["Events", "Logs"]) {
      fireEvent.mouseDown(within(sheet).getByRole("tab", { name }));
      await within(sheet).findByRole("tabpanel");
      await settle();
      await expectNoAxeViolations(document.body);
    }
    fireEvent.mouseDown(within(sheet).getByRole("tab", { name: "Events" }));
    click(await within(sheet).findByTestId("event-child-run-link"));
    await waitFor(() => expect(sheets()).toHaveLength(2));
    await within(top()).findByRole("tabpanel");
    await settle();
    oneH1();
    await expectNoAxeViolations(document.body);
  });

  it("Logs page: the log Sheet alone, and the trace Sheet over it", async () => {
    startAt("/logs");
    render(<LogsPage />);
    const { sheet } = await openLogRow();
    await settle();
    oneH1();
    await expectNoAxeViolations(document.body);
    click(within(sheet).getByTestId("log-sheet-open-trace"));
    await within(await screen.findByTestId("trace-sheet")).findAllByTestId("trace-span-row");
    await settle();
    oneH1();
    expect(openSheetIds()).toHaveLength(2);
    await expectNoAxeViolations(document.body);
  });

  it("the Traces list itself with its filter bar", async () => {
    listed.mockResolvedValue({ items: [runOf("r1")], next_before: null });
    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    expect(screen.getByTestId("traces-filter-bar")).toBeInTheDocument();
    expect(screen.getByTestId("traces-list")).toBeInTheDocument();
    oneH1();
    await expectNoAxeViolations(document.body);
  });
});

describe("phone width (390 px)", () => {
  beforeEach(() => setWidth(390));

  it("the trace Sheet is full width by class and by the absent inline width, with no Expand button", async () => {
    startAt(routes.trace("r1"));
    render(<TracesPage />);
    const sheet = await screen.findByTestId("trace-sheet");
    await within(sheet).findAllByTestId("trace-span-row");
    expect(sheet.className).toMatch(/(^|\s)w-full(\s|$)/);
    expect(sheet.style.width).toBe("");
    expect(layout()).toHaveAttribute("data-detail-beside", "false");
    expect(expandButton(sheet).className).toMatch(/(^|\s)hidden(\s|$)/);
    expect(expandButton(sheet).className).toContain("md:inline-flex");
    expect(within(sheet).getByTestId("trace-sheet-meta").className).toContain("flex-wrap");
  });

  it("a stacked child stays inside the viewport: offset, and capped to the window less the offset", async () => {
    startAt(routes.trace("r1"));
    render(<TracesPage />);
    click(await childLink(await screen.findByTestId("trace-sheet")));
    await waitFor(() => expect(sheets()).toHaveLength(2));
    expect(top().style.right).toBe("24px");
    expect(top().style.maxWidth).toBe("calc(100% - 24px)");
    expect(top().className).toMatch(/(^|\s)w-full(\s|$)/);
  });

  it("the log Sheet and the filter bar adapt: full width, wrapping controls", async () => {
    startAt("/logs");
    const logs = render(<LogsPage />);
    const { sheet } = await openLogRow();
    expect(sheet.className).toMatch(/(^|\s)w-full(\s|$)/);
    expect(sheet.style.width).toBe("");
    logs.unmount();
    startAt("/traces");
    listed.mockResolvedValue({ items: [], next_before: null });
    render(<TracesPage />);
    expect(await screen.findByTestId("traces-filter-bar")).toHaveClass("flex-wrap");
  });
});
