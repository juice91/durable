// Task 19: the Traces page header is a tab strip (Traces, Spans, Threads) over a runs histogram, and
// /traces/spans is the flat spans list that opens the trace sheet at a span.
import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TracesPage from "./page";
import SpansPage from "./spans/page";
import { fetchRunsHistogram, listTraceRuns } from "@/lib/traces";
import { listSpans } from "@/lib/spans";
import { api } from "@/lib/api";
import { observabilityRoutes } from "@/lib/routes/observability";
import { navItems } from "@/lib/nav";

const nav = vi.hoisted(() => ({ path: "/traces", query: "", replace: vi.fn(), push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useRouter: () => ({ push: nav.push, replace: nav.replace }),
  useSearchParams: () => new URLSearchParams(nav.query),
}));
vi.mock("@/lib/traces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/traces")>()),
  listTraceRuns: vi.fn(),
  fetchRunsHistogram: vi.fn(),
}));
vi.mock("@/lib/spans", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/spans")>()), listSpans: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()),
  api: { agents: { list: vi.fn() } },
}));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));

const HIST = { interval: "day" as const, buckets: [{ start: "2026-11-15T00:00:00Z", runs: 3, failed: 1 }] };
const run = {
  run_id: "run-1", kind: "agent", agent_id: "agent-1", version: 1, status: "completed", display_status: "completed",
  input: "in", output: "out", error: null, heartbeat_at: null, created_at: "2026-11-12T10:00:00Z", updated_at: "2026-11-12T10:00:00Z",
  trace_id: null, conversation_id: null, turn: null, tags: [],
};

describe("Traces tabs and histogram", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    nav.path = "/traces";
    nav.query = "";
    vi.mocked(api.agents.list).mockResolvedValue([] as never);
    vi.mocked(fetchRunsHistogram).mockResolvedValue(HIST);
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [run], next_before: null } as never);
    vi.mocked(listSpans).mockResolvedValue({
      items: [{ run_id: "run-9", span_id: "s-9", parent_id: null, kind: "tool_call", name: "lookup", start: "2026-11-16T10:00:00Z", end: null, status: "ok", agent_id: null, workflow_id: null, model: null }],
      next_before: null,
    });
  });

  it("the tab strip links Traces, Spans and Threads, with Traces current on /traces", async () => {
    render(<TracesPage />);
    const nav_ = await screen.findByRole("navigation", { name: "Traces sections" });
    const links = Array.from(nav_.querySelectorAll("a")).map((a) => [a.textContent, a.getAttribute("href")]);
    expect(links).toEqual([["Traces", "/traces"], ["Spans", "/traces/spans"], ["Threads", "/threads"]]);
    expect(screen.getByRole("link", { name: "Traces" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Spans" })).not.toHaveAttribute("aria-current");
  });

  it("Spans is the current tab on /traces/spans", async () => {
    nav.path = "/traces/spans";
    render(<SpansPage />);
    await screen.findByText("lookup");
    expect(screen.getByRole("link", { name: "Spans" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("link", { name: "Traces" })).not.toHaveAttribute("aria-current");
  });

  it("the nav item Traces is active on /traces/spans", () => {
    const item = navItems.find((i) => i.id === "traces")!;
    expect(item.match).toContain("/traces/spans");
  });

  it("clicking a histogram bucket narrows the date filters", async () => {
    render(<TracesPage />);
    fireEvent.click(await screen.findByRole("button", { name: /3 runs, 1 failed/ }));
    const url = nav.replace.mock.calls.at(-1)![0] as string;
    const params = new URLSearchParams(url.split("?")[1]);
    expect(params.get("from")).toBe("2026-11-15T00:00:00.000Z");
    expect(params.get("to")).toBe("2026-11-16T00:00:00.000Z");
  });

  it("the spans page has the span kind filter, reads it from the URL and opens the sheet at the span", async () => {
    nav.path = "/traces/spans";
    nav.query = "span_kind=tool_call";
    render(<SpansPage />);
    await screen.findByText("lookup");
    expect(screen.getByLabelText("Span kind")).toHaveValue("tool_call");
    expect(vi.mocked(listSpans).mock.calls[0][0]).toMatchObject({ span_kind: "tool_call" });
    fireEvent.click(screen.getByRole("button", { name: /lookup/ }));
    await waitFor(() => expect(nav.push).toHaveBeenCalled());
    const url = nav.push.mock.calls.at(-1)![0] as string;
    expect(url.startsWith("/traces/spans?")).toBe(true);
    const params = new URLSearchParams(url.split("?")[1]);
    expect(params.get("trace")).toBe("run-9");
    expect(params.get("span")).toBe("s-9");
    expect(params.get("span_kind")).toBe("tool_call");
  });

  it("the spans page searches by span name and shows Clear filters for the span kind alone", async () => {
    nav.path = "/traces/spans";
    nav.query = "span_kind=tool_call";
    render(<SpansPage />);
    await screen.findByText("lookup");
    expect(screen.getByLabelText("Search")).toHaveAttribute("placeholder", "span name");
    expect(screen.getByRole("button", { name: "Clear filters" })).toBeInTheDocument();
  });

  it("the spans page highlights the row whose span the open sheet shows", async () => {
    nav.path = "/traces/spans";
    nav.query = "trace=run-9&span=s-9";
    render(<SpansPage />);
    const row = (await screen.findByText("lookup")).closest("[data-row-id]") as HTMLElement;
    expect(row).toHaveAttribute("aria-current", "true");
  });

  it("the route builders carry the span", () => {
    expect(observabilityRoutes.spans()).toBe("/traces/spans");
    expect(observabilityRoutes.trace("r1", { span: "s1" })).toBe("/traces?trace=r1&span=s1");
  });
});
