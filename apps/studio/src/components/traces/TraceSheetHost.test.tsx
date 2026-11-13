import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { getTrace, listTraceRuns, type Trace, type TraceSpan } from "@/lib/traces";
import { getRun, listEvents, subscribeRun } from "@/lib/runs";
import { routes } from "@/lib/routes";
import { openSheetIds } from "@/components/ui/Sheet";
import { expectNoAxeViolations } from "@/test/a11y";
import { TraceLink } from "./TraceLink";
import { TraceSheetHost } from "./TraceSheetHost";
import { TracesTab } from "@/components/agentPage/TracesTab";

// A small stateful router with a history stack: push adds an entry, replace rewrites it, back pops.
const nav = vi.hoisted(() => ({
  path: "/traces", search: "", entries: [] as string[], listeners: new Set<() => void>(), replace: vi.fn(), push: vi.fn(), back: vi.fn(),
}));
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
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/traces")>()), getTrace: vi.fn(), listTraceRuns: vi.fn() }));
vi.mock("@/lib/runs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/runs")>()),
  getRun: vi.fn(),
  listEvents: vi.fn(async () => []),
  subscribeRun: vi.fn(() => () => undefined),
}));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/api")>()), api: { agents: { list: vi.fn(async () => []) } } }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));
vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()),
  searchLogs: vi.fn(async () => ({ items: [], from: "2026-11-13T09:00:00Z", to: "2026-11-13T10:00:00Z", limit: 500 })),
}));

const T = (s: number) => new Date(Date.UTC(2026, 10, 13, 10, 0, s)).toISOString();
function span(id: string, parent: string | null, kind: TraceSpan["kind"], name: string, attributes: TraceSpan["attributes"] = {}): TraceSpan {
  return { id, parent_id: parent, kind, name, start: T(0), end: T(5), status: "ok", attributes, input: null, output: null, truncated: {} };
}
function traceOf(runId: string): Trace {
  return {
    run: {
      run_id: runId, kind: "agent", agent_id: "agent-1", version: 1, tool_id: null, workflow_id: null, workflow_version: null,
      status: "completed", display_status: "completed", input: "hi", output: "ok", error: null, heartbeat_at: null,
      created_at: T(0), updated_at: T(5), trace_id: null, conversation_id: null, turn: null,
    } as Trace["run"],
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

const sheets = () => screen.queryAllByTestId("trace-sheet");
const topSheet = () => {
  const all = sheets();
  return all[all.length - 1];
};
const childRow = async (sheet: HTMLElement) =>
  (await within(sheet).findAllByTestId("trace-span-row")).find((row) => row.getAttribute("data-kind") === "child_run")!;
const escape = () => fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });

beforeEach(() => {
  nav.path = "/traces";
  nav.search = "";
  nav.entries = [nav.path];
  for (const fn of [nav.replace, nav.push, nav.back]) fn.mockClear();
  vi.mocked(getTrace).mockImplementation(async (runId: string) => traceOf(runId));
  vi.mocked(getRun).mockImplementation(async (runId: string) => traceOf(runId).run);
  vi.mocked(listEvents).mockResolvedValue([]);
  vi.mocked(subscribeRun).mockReturnValue(() => undefined);
});
afterEach(() => cleanup());

function Host({ runIds = ["r1", "r2", "r3"] }: { runIds?: string[] }) {
  return (
    <TraceSheetHost runIds={runIds}>
      {(sheet) => (
        <div>
          <span data-testid="open-run">{sheet.runId ?? "none"}</span>
          {runIds.map((id) => (
            <TraceLink key={id} runId={id} data-testid={`link-${id}`}>
              {id}
            </TraceLink>
          ))}
        </div>
      )}
    </TraceSheetHost>
  );
}

describe("TraceSheetHost: the Sheet lives in the URL", () => {
  it("opens from ?trace= on load (a reload keeps it) on the tab from &tab=", async () => {
    nav.search = "trace=r2&tab=events";
    render(<Host />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(within(sheet).getByTestId("trace-sheet-run-id")).toHaveTextContent("Run r2");
    expect(within(sheet).getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "true");
    expect(await within(sheet).findByText("Event Timeline")).toBeInTheDocument();
    expect(screen.getByTestId("open-run")).toHaveTextContent("r2");
  });

  it("a plain click on a TraceLink opens the Sheet in place; its href stays the shareable URL", async () => {
    render(<Host />);
    const link = screen.getByTestId("link-r1");
    expect(link).toHaveAttribute("href", routes.trace("r1"));
    fireEvent.click(link);
    // Opening from a list pushes one entry (Back closes the Sheet).
    expect(nav.push).toHaveBeenLastCalledWith("/traces?trace=r1", { scroll: false });
    expect(nav.replace).not.toHaveBeenCalled();
    expect(await screen.findByTestId("trace-sheet")).toBeInTheDocument();
  });

  it("a modified click (new tab) is left to the browser", () => {
    render(<Host />);
    const event = new MouseEvent("click", { bubbles: true, cancelable: true, metaKey: true });
    screen.getByTestId("link-r1").dispatchEvent(event);
    expect(nav.replace).not.toHaveBeenCalled();
    expect(nav.push).not.toHaveBeenCalled();
  });

  it("outside a host a TraceLink is a plain link to /traces?trace=", () => {
    render(<TraceLink runId="r9" tab="logs">r9</TraceLink>);
    expect(screen.getByRole("link", { name: "r9" })).toHaveAttribute("href", routes.trace("r9", { tab: "logs" }));
  });

  it("switching tabs writes &tab= (Spans removes it), and closing removes both", async () => {
    nav.search = "trace=r1";
    render(<Host />);
    const sheet = await screen.findByTestId("trace-sheet");
    const logs = within(sheet).getByRole("tab", { name: "Logs" });
    fireEvent.mouseDown(logs);
    expect(nav.search).toBe("trace=r1&tab=logs");
    fireEvent.mouseDown(within(screen.getByTestId("trace-sheet")).getByRole("tab", { name: "Spans" }));
    expect(nav.search).toBe("trace=r1");
    fireEvent.click(within(screen.getByTestId("trace-sheet")).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(sheets()).toHaveLength(0));
    expect(nav.search).toBe("");
  });
});

describe("TraceSheetHost: prev and next within the list", () => {
  it("steps through the hosting list's runs and stops at the ends", async () => {
    nav.search = "trace=r1";
    render(<Host />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(within(sheet).getByTestId("trace-sheet-position")).toHaveTextContent("1 / 3");
    expect(within(sheet).getByRole("button", { name: "Previous trace" })).toBeDisabled();
    fireEvent.click(within(sheet).getByRole("button", { name: "Next trace" }));
    expect(nav.search).toBe("trace=r2");
    await waitFor(() => expect(within(screen.getByTestId("trace-sheet")).getByTestId("trace-sheet-run-id")).toHaveTextContent("Run r2"));
    fireEvent.click(within(screen.getByTestId("trace-sheet")).getByRole("button", { name: "Next trace" }));
    await waitFor(() => expect(within(screen.getByTestId("trace-sheet")).getByTestId("trace-sheet-position")).toHaveTextContent("3 / 3"));
    expect(within(screen.getByTestId("trace-sheet")).getByRole("button", { name: "Next trace" })).toBeDisabled();
    fireEvent.click(within(screen.getByTestId("trace-sheet")).getByRole("button", { name: "Previous trace" }));
    expect(nav.search).toBe("trace=r2");
  });

  it("keeps the tab while stepping, and shows no prev / next for a run outside the list", async () => {
    nav.search = "trace=r2&tab=logs";
    const view = render(<Host />);
    fireEvent.click(within(await screen.findByTestId("trace-sheet")).getByRole("button", { name: "Next trace" }));
    expect(nav.search).toBe("trace=r3&tab=logs");
    view.unmount();
    nav.search = "trace=elsewhere";
    render(<Host />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(within(sheet).queryByRole("button", { name: "Next trace" })).toBeNull();
  });

  it("a host inside another one hands its run ids to the outer host (one Sheet only)", async () => {
    nav.search = "trace=b";
    render(
      <TraceSheetHost runIds={["x"]}>
        <TraceSheetHost runIds={["a", "b", "c"]}>
          <p>inner</p>
        </TraceSheetHost>
      </TraceSheetHost>,
    );
    await waitFor(() => expect(sheets()).toHaveLength(1));
    await waitFor(() => expect(within(sheets()[0]).getByTestId("trace-sheet-position")).toHaveTextContent("2 / 3"));
  });
});

describe("TraceSheetHost: stacking", () => {
  it("a child run's link stacks a second Sheet; a third replaces the second; Escape closes the top only", async () => {
    nav.search = "trace=r1";
    render(<Host />);
    const first = await screen.findByTestId("trace-sheet");
    fireEvent.click(await childRow(first));
    fireEvent.click(await within(first).findByTestId("span-link-child-trace"));
    await waitFor(() => expect(sheets()).toHaveLength(2));
    expect(topSheet()).toHaveAttribute("data-trace-depth", "1");
    expect(within(topSheet()).getByTestId("trace-sheet-run-id")).toHaveTextContent("Run r1-child");
    expect(openSheetIds()).toHaveLength(2);
    // The URL still names the first trace: the child is not a URL of its own.
    expect(nav.search).toBe("trace=r1");

    // From the child, its own child replaces it: never more than two.
    const child = topSheet();
    fireEvent.click(await childRow(child));
    fireEvent.click(await within(child).findByTestId("span-link-child-trace"));
    await waitFor(() => expect(within(topSheet()).getByTestId("trace-sheet-run-id")).toHaveTextContent("Run r1-child-child"));
    expect(sheets()).toHaveLength(2);

    escape();
    await waitFor(() => expect(sheets()).toHaveLength(1));
    expect(nav.search).toBe("trace=r1");
    escape();
    await waitFor(() => expect(sheets()).toHaveLength(0));
    expect(nav.search).toBe("");
  });

  it("Events: the parent run link stacks the parent's Events tab", async () => {
    vi.mocked(getRun).mockImplementation(async (runId: string) => ({ ...traceOf(runId).run, parent_run_id: "parent-1", parent_step_id: "s1" }));
    nav.search = "trace=r1&tab=events";
    render(<Host />);
    const parent = await within(await screen.findByTestId("trace-sheet")).findByTestId("run-parent-link");
    expect(parent).toHaveAttribute("href", routes.run("parent-1"));
    fireEvent.click(parent);
    await waitFor(() => expect(sheets()).toHaveLength(2));
    expect(within(topSheet()).getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("TraceSheet: chrome", () => {
  it("Expand toggles full width, and the Sheet has a name and no axe violations", async () => {
    nav.search = "trace=r1";
    render(<Host />);
    const sheet = await screen.findByTestId("trace-sheet");
    await within(sheet).findAllByTestId("trace-span-row");
    expect(sheet).toHaveAttribute("data-sheet-size", "wide");
    const expand = within(sheet).getByRole("button", { name: "Expand to full width" });
    expect(expand).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(expand);
    expect(sheet).toHaveAttribute("data-sheet-size", "full");
    expect(within(sheet).getByRole("button", { name: "Collapse to the side" })).toHaveAttribute("aria-pressed", "true");
    expect(sheet).toHaveAttribute("role", "dialog");
    expect(sheet).toHaveAccessibleName(within(sheet).getByRole("heading", { level: 2 }).textContent ?? "");
    await act(async () => {
      await expectNoAxeViolations(document.body);
    });
  });

  it("Tree / Timeline: the tree drops the time axis bars and keeps the rows", async () => {
    nav.search = "trace=r1";
    render(<Host />);
    const sheet = await screen.findByTestId("trace-sheet");
    await within(sheet).findAllByTestId("trace-span-row");
    expect(within(sheet).getAllByTestId("trace-span-bar").length).toBeGreaterThan(0);
    fireEvent.click(within(sheet).getByRole("radio", { name: "Tree" }));
    expect(within(sheet).queryAllByTestId("trace-span-bar")).toHaveLength(0);
    expect(within(sheet).getAllByTestId("trace-span-row")).toHaveLength(2);
    expect(within(sheet).getAllByTestId("trace-span-dot")).toHaveLength(2);
  });

  it("shows the run's status and kind, and links its agent", async () => {
    nav.search = "trace=r1";
    render(<Host />);
    const meta = await within(await screen.findByTestId("trace-sheet")).findByTestId("trace-sheet-meta");
    expect(meta).toHaveTextContent("Completed");
    expect(meta).toHaveTextContent("Agent run");
    expect(within(meta).getByTestId("trace-sheet-subject-link")).toHaveAttribute("href", routes.agentChat("agent-1"));
  });
});

describe("agent Traces tab", () => {
  it("a row opens the trace Sheet in place on the agent page, with prev / next through its rows", async () => {
    nav.path = routes.agentTraces("a1");
    const run = (id: string, at: number) => ({ ...traceOf(id).run, created_at: T(at) });
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [run("r1", 2), run("r2", 1)], next_before: null } as never);
    render(<TracesTab agentId="a1" />);
    const rows = await screen.findAllByRole("button", { name: /^Run started/ });
    fireEvent.click(rows[0]);
    expect(nav.push).toHaveBeenLastCalledWith(`${routes.agentTraces("a1")}?trace=r1`, { scroll: false });
    const sheet = await screen.findByTestId("trace-sheet");
    expect(within(sheet).getByTestId("trace-sheet-position")).toHaveTextContent("1 / 2");
    expect(screen.getAllByTestId("agent-trace-row")[0]).toHaveAttribute("aria-current", "true");
  });
});

describe("review fixes: Retry, same-run links, sort order, focus after stepping", () => {
  it("a failed trace load shows the error with Retry, which loads it again", async () => {
    vi.mocked(getTrace).mockClear();
    vi.mocked(getTrace).mockRejectedValueOnce(new Error("down"));
    nav.search = "trace=r1";
    render(<Host />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(await within(sheet).findByTestId("trace-load-error")).toHaveTextContent("Failed to load the trace");
    fireEvent.click(within(sheet).getByRole("button", { name: "Retry" }));
    expect(await within(sheet).findAllByTestId("trace-span-row")).toHaveLength(2);
    expect(within(sheet).queryByTestId("trace-load-error")).toBeNull();
    expect(getTrace).toHaveBeenCalledTimes(2);
  });

  it("events that cannot be read show an alert with Retry, not 'No events were recorded'", async () => {
    vi.mocked(listEvents).mockRejectedValueOnce(new Error("down"));
    nav.search = "trace=r1&tab=events";
    render(<Host />);
    const sheet = await screen.findByTestId("trace-sheet");
    const alert = await within(sheet).findByTestId("run-events-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("Failed to load the run's events");
    expect(within(sheet).queryByText(/No events were recorded/)).toBeNull();
    vi.mocked(listEvents).mockResolvedValueOnce([{ seq: 7, type: "run_completed", data: {}, ts: T(5) }] as never);
    fireEvent.click(within(alert).getByRole("button", { name: "Retry" }));
    expect(await within(sheet).findByText("seq 7")).toBeInTheDocument();
    expect(within(sheet).queryByTestId("run-events-error")).toBeNull();
  });

  it("a run that cannot be loaded offers Retry in the Events tab", async () => {
    vi.mocked(getRun).mockRejectedValueOnce(new Error("down"));
    nav.search = "trace=r1&tab=events";
    render(<Host />);
    const sheet = await screen.findByTestId("trace-sheet");
    expect(await within(sheet).findByTestId("run-events-error")).toHaveTextContent("Failed to load run");
    fireEvent.click(within(sheet).getByRole("button", { name: "Retry" }));
    expect(await within(sheet).findByText("Event Timeline")).toBeInTheDocument();
  });

  it("'Run details' on the Sheet's own run switches its tab instead of stacking a copy", async () => {
    nav.search = "trace=r1";
    render(<Host />);
    const sheet = await screen.findByTestId("trace-sheet");
    await within(sheet).findAllByTestId("trace-span-row"); // the root span (run r1) is selected
    const details = within(sheet).getByTestId("span-link-run");
    expect(details).toHaveAttribute("href", routes.run("r1"));
    fireEvent.click(details);
    expect(nav.search).toBe("trace=r1&tab=events");
    await waitFor(() => expect(within(sheet).getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "true"));
    expect(sheets()).toHaveLength(1);
    expect(openSheetIds()).toHaveLength(1);
  });

  it("prev / next follow the agent Traces tab's sort, not the API order", async () => {
    nav.path = routes.agentTraces("a1");
    nav.entries = [nav.path];
    const run = (id: string, at: number) => ({ ...traceOf(id).run, created_at: T(at) });
    // API order r1, r2, r3; the tab sorts by Started, newest first: r3, r1, r2.
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [run("r1", 2), run("r2", 1), run("r3", 3)], next_before: null } as never);
    render(<TracesTab agentId="a1" />);
    const rows = await screen.findAllByRole("button", { name: /^Run started/ });
    fireEvent.click(rows[1]); // r1
    const sheet = await screen.findByTestId("trace-sheet");
    expect(within(sheet).getByTestId("trace-sheet-run-id")).toHaveTextContent("Run r1");
    expect(within(sheet).getByTestId("trace-sheet-position")).toHaveTextContent("2 / 3");
    fireEvent.click(within(sheet).getByRole("button", { name: "Previous trace" }));
    expect(nav.search).toBe("trace=r3");
  });

  it("after prev / next, closing focuses the row of the run then shown", async () => {
    nav.path = routes.agentTraces("a1");
    nav.entries = [nav.path];
    const run = (id: string, at: number) => ({ ...traceOf(id).run, created_at: T(at) });
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [run("r1", 2), run("r2", 1)], next_before: null } as never);
    render(<TracesTab agentId="a1" />);
    const opener = (await screen.findAllByRole("button", { name: /^Run started/ }))[0];
    opener.focus();
    fireEvent.click(opener);
    const sheet = await screen.findByTestId("trace-sheet");
    fireEvent.click(within(sheet).getByRole("button", { name: "Next trace" }));
    await waitFor(() => expect(within(screen.getByTestId("trace-sheet")).getByTestId("trace-sheet-run-id")).toHaveTextContent("Run r2"));
    escape();
    await waitFor(() => expect(sheets()).toHaveLength(0));
    const r2Row = screen.getAllByTestId("agent-trace-row").find((row) => row.getAttribute("data-row-id") === "r2")!;
    await waitFor(() => expect(within(r2Row).getByRole("button", { name: /^Run started/ })).toHaveFocus());
  });
});
