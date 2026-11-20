import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import TracesPage from "./page";
import { listTraceRuns } from "@/lib/traces";
import { getExternalTrace, listExternalTraces } from "@/lib/externalTraces";
import { api } from "@/lib/api";
import { optionsOf } from "@/test/select";

const nav = vi.hoisted(() => ({ query: "", replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/traces",
  useRouter: () => ({ push: vi.fn(), replace: nav.replace }),
  useSearchParams: () => new URLSearchParams(nav.query),
}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a>,
}));
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/traces")>()), listTraceRuns: vi.fn() }));
vi.mock("@/lib/externalTraces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/externalTraces")>()),
  listExternalTraces: vi.fn(),
  getExternalTrace: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/api")>()), api: { agents: { list: vi.fn() } } }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));
vi.mock("@/components/traces/RunsHistogram", () => ({ RunsHistogram: () => null }));

const TRACE = "0af7651916cd43dd8448eb211c80319c";

describe("Traces page: external traces", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    nav.query = "";
    vi.mocked(api.agents.list).mockResolvedValue([]);
    vi.mocked(listTraceRuns).mockResolvedValue({
      items: [{
        run_id: "run-1", agent_id: "agent-1", version: 1, status: "completed", display_status: "completed", input: "hi", output: "ok",
        error: null, heartbeat_at: null, created_at: "2026-11-12T10:00:00Z", updated_at: "2026-11-12T10:00:05Z", trace_id: null,
        conversation_id: null, turn: null,
      }] as never,
      next_before: null,
    });
    vi.mocked(listExternalTraces).mockResolvedValue({
      items: [{ trace_id: TRACE, source: "external", name: "agent.run", service_name: "my-agent", start: "2026-11-12T11:00:00Z", end: "2026-11-12T11:00:02Z", span_count: 2, status: "ok" }],
      next_before: null,
    });
    vi.mocked(getExternalTrace).mockResolvedValue({
      trace_id: TRACE, source: "external", service_name: "my-agent", run_id: null, truncated: false,
      resource_attributes: { "service.name": "my-agent", "deployment.environment": "staging" },
      spans: [
        { span_id: "aaaaaaaaaaaaaaaa", parent_id: null, name: "agent.run", start: "2026-11-12T11:00:00Z", end: "2026-11-12T11:00:02Z", status: "ok", attributes: { "llm.model": "gpt" } },
        { span_id: "bbbbbbbbbbbbbbbb", parent_id: "aaaaaaaaaaaaaaaa", name: "llm.call", start: "2026-11-12T11:00:00Z", end: "2026-11-12T11:00:01Z", status: "error", attributes: {} },
      ],
    });
  });

  it("lists platform runs and external traces together by default, the external ones with a service badge", async () => {
    render(<TracesPage />);
    const row = await screen.findByTestId("external-trace-row");
    expect(within(row).getByTestId("external-trace-service-badge")).toHaveTextContent("my-agent");
    expect(row).toHaveTextContent("agent.run");
    expect(await screen.findAllByTestId("trace-row")).toHaveLength(1);
    expect(listTraceRuns).toHaveBeenCalled();
  });

  it("Source=External asks for no runs; Source=Platform asks for no external traces", async () => {
    nav.query = "source=external";
    const { unmount } = render(<TracesPage />);
    await screen.findByTestId("external-trace-row");
    expect(listTraceRuns).not.toHaveBeenCalled();
    expect(screen.queryByTestId("trace-row")).toBeNull();
    unmount();
    vi.clearAllMocks();
    nav.query = "source=platform";
    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    expect(listExternalTraces).not.toHaveBeenCalled();
    expect(screen.queryByTestId("external-traces")).toBeNull();
    // the runs query never carries `source`
    expect(vi.mocked(listTraceRuns).mock.calls[0][0].source).toBe("platform");
  });

  it("a run-only filter (an agent, a model, a status) leaves external traces out", async () => {
    nav.query = "model=gpt-4";
    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    expect(listExternalTraces).not.toHaveBeenCalled();
  });

  it("the Source select writes the filter to the URL", async () => {
    render(<TracesPage />);
    const select = (await screen.findByTestId("source-filter")) as HTMLSelectElement;
    expect(optionsOf(select).map((o) => o.textContent)).toEqual(["All", "Platform", "External"]);
    expect(select.value).toBe("all");
    fireEvent.change(select, { target: { value: "external" } });
    await waitFor(() => expect(nav.replace.mock.calls.at(-1)?.[0]).toContain("source=external"));
  });

  it("opening an external trace shows its resource attributes and span tree", async () => {
    nav.query = "source=external";
    render(<TracesPage />);
    fireEvent.click(await screen.findByTestId("external-trace-row"));
    const sheet = await screen.findByTestId("external-trace-sheet");
    await waitFor(() => expect(getExternalTrace).toHaveBeenCalledWith(TRACE, expect.anything()));
    const resource = await within(sheet).findByTestId("external-trace-resource");
    expect(resource).toHaveTextContent("deployment.environment");
    expect(resource).toHaveTextContent("staging");
    expect(within(sheet).getByTestId("external-trace-service")).toHaveTextContent("my-agent");
    const spans = within(sheet).getAllByTestId("external-span");
    expect(spans.map((s) => s.querySelector("summary")?.textContent)).toEqual([expect.stringContaining("agent.run"), expect.stringContaining("llm.call")]);
    expect(spans[1]).toHaveStyle({ marginLeft: "16px" });
    expect(within(sheet).queryByTestId("external-trace-run-link")).toBeNull();
  });

  it("links the platform run a span names with pydanticos.run_id", async () => {
    nav.query = `source=external&external=${TRACE}`;
    vi.mocked(getExternalTrace).mockResolvedValueOnce({
      trace_id: TRACE, source: "external", service_name: "my-agent", run_id: "11111111-2222-3333-4444-555555555555", truncated: false,
      resource_attributes: {}, spans: [],
    });
    render(<TracesPage />);
    const link = await screen.findByTestId("external-trace-run-link");
    expect(link).toHaveTextContent("11111111");
  });
});
