// Follow-up 2 / 1c acceptance (Studio): the Logs page's agent filter end to end (URL state, the real
// request the page sends, paging, errors) and the Draft label in the run picker.
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { ApiError } from "@/lib/api";
import { listTraceRuns, type TraceRun } from "@/lib/traces";
import { searchLogs, type LogItem } from "@/components/logs/logsQuery";
import LogsPage from "@/app/logs/page";

const AGENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const OTHER = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee";
const RUN = "11111111-2222-3333-4444-555555555555";

const nav = vi.hoisted(() => ({ search: "", listeners: new Set<() => void>(), urls: [] as string[] }));
vi.mock("next/navigation", async () => {
  const React = await import("react");
  return {
    usePathname: () => "/logs",
    useRouter: () => ({
      push: vi.fn(),
      replace: (url: string) => {
        nav.urls.push(url);
        nav.search = url.includes("?") ? url.slice(url.indexOf("?") + 1) : "";
        nav.listeners.forEach((l) => l());
      },
    }),
    useSearchParams: () => {
      const s = React.useSyncExternalStore((cb) => (nav.listeners.add(cb), () => nav.listeners.delete(cb)), () => nav.search, () => nav.search);
      return React.useMemo(() => new URLSearchParams(s), [s]);
    },
  };
});
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { list: vi.fn(async () => [{ id: AGENT, name: "Support bot" }, { id: OTHER, name: "Billing bot" }]) } } };
});
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/traces")>()), listTraceRuns: vi.fn(), getTrace: vi.fn() }));
vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()), searchLogs: vi.fn() }));

const item = (message: string, ts = "2026-11-15T10:00:00.000000+00:00"): LogItem =>
  ({ ts, level: "info", message, service: "eap-worker", trace_id: null, span_id: null, run_id: RUN }) as LogItem;
const reply = (items: LogItem[], extra: object = {}, limit = 100) => ({ items, from: "2026-11-15T09:00:00+00:00", to: "2026-11-15T10:00:00+00:00", limit, ...extra });
const calls = () => vi.mocked(searchLogs).mock.calls.map((c) => c[0]);
const agentSelect = () => screen.findByLabelText("Agent") as Promise<HTMLSelectElement>;

beforeEach(() => {
  nav.search = "";
  nav.urls = [];
  vi.mocked(searchLogs).mockReset();
  vi.mocked(searchLogs).mockResolvedValue(reply([]));
  vi.mocked(listTraceRuns).mockReset();
  vi.mocked(listTraceRuns).mockResolvedValue({ items: [], next_before: null });
});
afterEach(cleanup);

describe("agent filter and the URL", () => {
  it("choosing an agent writes agent_id to the URL and searches with it; clearing removes both", async () => {
    render(<LogsPage />);
    const select = await agentSelect();
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(1));
    expect(calls()[0]).not.toHaveProperty("agent_id");
    fireEvent.change(select, { target: { value: AGENT } });
    await waitFor(() => expect(calls().at(-1)).toMatchObject({ agent_id: AGENT }));
    expect(new URLSearchParams(nav.urls.at(-1)!.split("?")[1]).get("agent_id")).toBe(AGENT);
    expect(select.value).toBe(AGENT);
    fireEvent.change(select, { target: { value: "" } });
    await waitFor(() => expect(calls().at(-1)).not.toHaveProperty("agent_id"));
    expect(nav.urls.at(-1)).not.toContain("agent_id");
    expect(select.value).toBe("");
  });

  it("restores the agent from the URL along with the other filters, and sends them all", async () => {
    nav.search = `agent_id=${OTHER}&level=error&q=timeout&range=6h`;
    render(<LogsPage />);
    expect(((await agentSelect()).value)).toBe(OTHER);
    await waitFor(() => expect(searchLogs).toHaveBeenCalled());
    expect(calls()[0]).toMatchObject({ agent_id: OTHER, level: "error", q: "timeout", limit: 100 });
    expect(calls()[0].from).toBeTruthy();
  });

  it("Load older keeps the agent filter and moves only `to`", async () => {
    nav.search = `agent_id=${AGENT}`;
    const full = Array.from({ length: 100 }, (_, i) => item(`m${i}`, `2026-11-15T10:00:${String(59 - (i % 60)).padStart(2, "0")}.${String(i).padStart(6, "0")}+00:00`));
    vi.mocked(searchLogs)
      .mockResolvedValueOnce(reply(full, { agent_runs: 4, agent_runs_limited: false }))
      .mockResolvedValueOnce(reply([item("older", "2026-11-15T08:00:00.000000+00:00")], { agent_runs: 4, agent_runs_limited: false }));
    render(<LogsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Load older" }));
    expect(await screen.findByText("older")).toBeInTheDocument();
    expect(calls()[1]).toMatchObject({ agent_id: AGENT });
    expect(calls()[1].to).toBeTruthy();
    expect(calls()[1].from).toBe("2026-11-15T09:00:00+00:00"); // the first response's window
  });

  it("the agent note is gone with a run picked and for the unfiltered page", async () => {
    nav.search = `agent_id=${AGENT}`;
    vi.mocked(searchLogs).mockResolvedValue(reply([item("x")], { agent_runs: 1, agent_runs_limited: false }));
    render(<LogsPage />);
    expect(await screen.findByTestId("logs-agent-note")).toHaveTextContent("Searched this agent's 1 run in this time range.");
    fireEvent.change(await agentSelect(), { target: { value: "" } });
    await waitFor(() => expect(screen.queryByTestId("logs-agent-note")).toBeNull());
  });
});

describe("agent filter errors", () => {
  it("a 404 for the agent says the agent was not found (not the run)", async () => {
    nav.search = `agent_id=${OTHER}`;
    vi.mocked(searchLogs).mockRejectedValue(new ApiError(404, "Agent not found"));
    render(<LogsPage />);
    expect(await screen.findByTestId("logs-error")).toHaveTextContent("Agent not found");
    expect(screen.getByTestId("logs-error")).not.toHaveTextContent("Run not found");
  });

  it("a 422 shows the API's message", async () => {
    nav.search = `agent_id=${AGENT}`;
    vi.mocked(searchLogs).mockRejectedValue(new ApiError(422, "bad agent"));
    render(<LogsPage />);
    expect(await screen.findByTestId("logs-error")).toHaveTextContent("bad agent");
  });
});

describe("run picker", () => {
  it("lists a draft run as Draft and a published one with its version", async () => {
    const base = { kind: "agent", agent_id: AGENT, status: "completed", display_status: "completed", created_at: "2026-11-15T10:00:00Z" };
    vi.mocked(listTraceRuns).mockResolvedValue({
      items: [
        { ...base, run_id: RUN, version: null, draft: true },
        { ...base, run_id: "22222222-2222-3333-4444-555555555555", version: 4, draft: false },
      ] as unknown as TraceRun[],
      next_before: null,
    });
    const { container } = render(<LogsPage />);
    await waitFor(() => expect(container.querySelectorAll("#logs-run-options option")).toHaveLength(2));
    const labels = [...container.querySelectorAll("#logs-run-options option")].map((o) => o.getAttribute("label"));
    expect(labels[0]).toMatch(/ · Draft · completed$/);
    expect(labels[1]).toMatch(/ · v4 · completed$/);
  });
});
