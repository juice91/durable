import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import TracesPage from "./page";
import { EMPTY_FILTERS, listTraceRuns as listRuns } from "@/lib/traces";
import { api } from "@/lib/api";
import { routes } from "@/lib/routes";

const nav = vi.hoisted(() => ({ query: "", replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/traces",
  useRouter: () => ({ push: vi.fn(), replace: nav.replace }),
  useSearchParams: () => new URLSearchParams(nav.query),
}));

vi.mock("@/lib/traces", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/traces")>();
  return {
    ...original,
    listTraceRuns: vi.fn(),
  };
});

vi.mock("@/lib/api", () => ({
  api: { agents: { list: vi.fn() } },
}));

vi.mock("@/lib/workflows", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  listWorkflows: vi.fn(async () => [{ id: "wf-abcdef123456", name: "Triage", draft_spec: null, latest_version: 3 }]),
}));

describe("TracesPage", () => {
  const mockRuns = [
    {
      run_id: "run-1",
      agent_id: "agent-1",
      version: 1,
      status: "completed",
      display_status: "completed",
      input: "test input",
      output: "test output",
      error: null,
      heartbeat_at: null,
      created_at: "2026-11-12T10:00:00Z",
      updated_at: "2026-11-12T10:00:05Z",
      trace_id: "trace-abc123",
      conversation_id: "conv-123",
      turn: 1,
    },
  ];

  const mockResponse = {
    items: mockRuns,
    next_before: null,
  };

  beforeEach(() => {
    vi.clearAllMocks();
    nav.query = "";
    vi.mocked(api.agents.list).mockResolvedValue([
      {
        id: "agent-1",
        name: "Support assistant",
        draft_spec: {
          spec_version: 1,
          name: "Support assistant",
          instructions: "Help customers",
          model: { provider: "openai", name: "gpt-4.1-mini" },
          tools: [],
        },
        latest_version: 1,
      },
    ]);
  });

  it("labels a workflow run with its workflow, not 'Tool: unknown', and links to its run view", async () => {
    (listRuns as any).mockResolvedValue({ items: [...mockRuns, {
      run_id: "wfrun-1", kind: "workflow" as const, agent_id: null, version: null, tool_id: null,
      workflow_id: "wf-abcdef123456", workflow_version: 3, status: "running", display_status: "running",
      input: "{}", output: null, error: null, heartbeat_at: null, created_at: "2026-11-12T11:00:00Z",
      updated_at: "2026-11-12T11:00:05Z", trace_id: null, conversation_id: null, turn: null,
    }], next_before: null });
    render(<TracesPage />);
    const link = await screen.findByRole("link", { name: "Workflow: Triage v3" });
    expect(link).toHaveAttribute("href", routes.workflowRun("wf-abcdef123456", "wfrun-1"));
    expect(screen.queryByText(/Tool: unknown/)).toBeNull();
  });

  it("renders the traces page title and header", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);

    render(<TracesPage />);

    await waitFor(() => {
      expect(screen.getByText("Traces")).toBeInTheDocument();
    });
  });

  it("renders status filter dropdown", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);

    render(<TracesPage />);

    await waitFor(() => {
      expect(screen.getByLabelText(/filter by status/i)).toBeInTheDocument();
    });
  });

  it("calls listRuns with correct parameters", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);

    render(<TracesPage />);

    await waitFor(() => {
      expect(listRuns).toHaveBeenCalledWith(EMPTY_FILTERS, { limit: 50, before: undefined });
    });
  });

  it("shows the agent name instead of its UUID", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);

    render(<TracesPage />);

    // The name also appears in the agent filter's options, so look in the table.
    const table = await screen.findByRole("table");
    expect(await within(table).findByText("Support assistant")).toBeInTheDocument();
    expect(screen.queryByText("agent-1")).not.toBeInTheDocument();
  });

  it("labels agent, tool, workflow and child rows and links workflow and parent runs", async () => {
    (listRuns as any).mockResolvedValue({
      items: [
        {
          run_id: "agent-run-1", kind: "agent", agent_id: "agent-1", version: 1, tool_id: null,
          status: "completed", display_status: "completed", input: "hi", output: "ok", error: null,
          heartbeat_at: null, created_at: "2026-11-13T10:00:00Z", updated_at: "2026-11-13T10:00:05Z",
          trace_id: "trace-agent", conversation_id: null, turn: null,
        },
        {
          run_id: "tool-run-1", kind: "tool", agent_id: null, version: null, tool_id: "clock.now",
          status: "completed", display_status: "completed", input: "{}", output: "12:00", error: null,
          heartbeat_at: null, created_at: "2026-11-13T10:01:00Z", updated_at: "2026-11-13T10:01:01Z",
          trace_id: null, conversation_id: null, turn: null,
        },
        {
          run_id: "wfrun-rows", kind: "workflow", agent_id: null, version: null, tool_id: null,
          workflow_id: "wf-abcdef123456", workflow_version: 3, status: "running", display_status: "running",
          input: "{}", output: null, error: null, heartbeat_at: null, created_at: "2026-11-13T10:02:00Z",
          updated_at: "2026-11-13T10:02:05Z", trace_id: null, conversation_id: null, turn: null,
        },
        {
          run_id: "child-run-1", kind: "agent", agent_id: "agent-1", version: 2,
          parent_run_id: "parent-run-0001", parent_step_id: "classify",
          status: "completed", display_status: "completed", input: "step", output: "done", error: null,
          heartbeat_at: null, created_at: "2026-11-13T10:03:00Z", updated_at: "2026-11-13T10:03:04Z",
          trace_id: null, conversation_id: null, turn: null,
        },
      ],
      next_before: null,
    });
    render(<TracesPage />);

    expect(await screen.findByRole("columnheader", { name: "Agent / Tool / Workflow" })).toBeInTheDocument();
    // Both the plain agent and the child row carry this name as a direct text node.
    expect(await within(screen.getByRole("table")).findAllByText("Support assistant")).toHaveLength(2);

    const subjects = screen.getAllByTestId("run-subject");
    const plainAgent = subjects.find((node) => node.getAttribute("data-kind") === "agent" && !node.textContent?.includes("Part of"));
    expect(plainAgent).toHaveTextContent("Support assistant");
    expect(subjects.find((node) => node.getAttribute("data-kind") === "tool")).toHaveTextContent("Tool: clock.now");

    const workflow = await screen.findByRole("link", { name: "Workflow: Triage v3" });
    expect(workflow).toHaveAttribute("href", routes.workflowRun("wf-abcdef123456", "wfrun-rows"));
    expect(workflow.closest("[data-testid='run-subject']")).toHaveAttribute("data-kind", "workflow");

    const child = subjects.find((node) => node.textContent?.includes("Part of workflow run"));
    expect(child).toBeTruthy();
    expect(child).toHaveAttribute("data-kind", "agent");
    expect(child).toHaveTextContent("Support assistant");
    expect(child).toHaveTextContent("Part of workflow run parent-r... (step classify)");
    expect(screen.getByTestId("run-parent-link")).toHaveAttribute("href", routes.run("parent-run-0001"));
    expect(screen.queryByText(/Tool: unknown/)).toBeNull();
  });
  it("shows model, tokens, cost and duration columns, and 'not recorded' for a run without usage", async () => {
    (listRuns as any).mockResolvedValue({
      items: [
        { ...mockRuns[0], run_id: "run-usage", model_id: "openai:gpt-4.1-mini", model_overridden: true, input_tokens: 1200, output_tokens: 34,
          cost_usd: "0.0012000000", started_at: "2026-11-12T10:00:01Z", finished_at: "2026-11-12T10:00:03.500Z" },
        { ...mockRuns[0], run_id: "run-old", created_at: "2026-11-11T10:00:00Z", updated_at: "2026-11-11T10:00:02Z" },
        { ...mockRuns[0], run_id: "run-unpriced", created_at: "2026-11-10T10:00:00Z", input_tokens: 5, output_tokens: 6, cost_usd: null },
      ],
      next_before: null,
    });
    render(<TracesPage />);
    const rows = await screen.findAllByTestId("trace-row");
    expect(rows[0].querySelector("[data-testid='trace-row-model']")).toHaveTextContent("openai:gpt-4.1-mini(override)");
    expect(rows[0].querySelector("[data-testid='trace-row-tokens']")).toHaveTextContent("1,200 / 34");
    expect(rows[0].querySelector("[data-testid='trace-row-cost']")).toHaveTextContent("$0.0012");
    expect(rows[0].querySelector("[data-testid='trace-row-duration']")).toHaveTextContent("2.50 s");
    expect(rows[0].querySelector("[data-testid='trace-row-open']")).toHaveAttribute("href", routes.trace("run-usage"));
    expect(rows[1].querySelector("[data-testid='trace-row-tokens']")).toHaveTextContent("not recorded");
    expect(rows[1].querySelector("[data-testid='trace-row-cost']")).toHaveTextContent("not recorded");
    expect(rows[2].querySelector("[data-testid='trace-row-cost']")).toHaveTextContent("no price");
  });

  it("reads the drill-down filters from the URL and writes filter changes back", async () => {
    nav.query = "status=failed,needs_attention&model=openai%3Agpt-4.1&from=2026-11-01T00%3A00%3A00&to=2026-11-02T00%3A00%3A00&agent_id=agent-1&kind=agent";
    (listRuns as any).mockResolvedValue(mockResponse);
    render(<TracesPage />);
    await waitFor(() => expect(listRuns).toHaveBeenCalled());
    expect((listRuns as any).mock.calls[0][0]).toEqual({
      status: ["failed", "needs_attention"], model: "openai:gpt-4.1", provider: null,
      from: "2026-11-01T00:00:00", to: "2026-11-02T00:00:00", agent_id: "agent-1", kind: "agent",
    });
    expect((screen.getByLabelText(/model/i) as HTMLInputElement).value).toBe("openai:gpt-4.1");

    fireEvent.change(screen.getByLabelText(/filter by status/i), { target: { value: "completed" } });
    const url = nav.replace.mock.calls.at(-1)![0] as string;
    const params = new URLSearchParams(url.split("?")[1]);
    expect(url.startsWith(`${routes.traces()}?`)).toBe(true);
    expect(params.get("status")).toBe("completed");
    expect(params.get("model")).toBe("openai:gpt-4.1");
    expect(params.get("agent_id")).toBe("agent-1");

    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(nav.replace).toHaveBeenLastCalledWith(routes.traces());
  });

  it("sends the Recovering status filter as status=recovering, as-is", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);
    const first = render(<TracesPage />);
    await waitFor(() => expect(listRuns).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText(/filter by status/i), { target: { value: "recovering" } });
    const url = nav.replace.mock.calls.at(-1)![0] as string;
    expect(new URLSearchParams(url.split("?")[1]).get("status")).toBe("recovering");

    // The URL drives the API call: the page asks for exactly status=recovering.
    first.unmount();
    (listRuns as any).mockClear();
    nav.query = "status=recovering";
    render(<TracesPage />);
    await waitFor(() => expect(listRuns).toHaveBeenCalled());
    expect((listRuns as any).mock.calls[0][0]).toMatchObject({ status: ["recovering"] });
  });

  it("commits a text filter on Enter", async () => {
    (listRuns as any).mockResolvedValue(mockResponse);
    render(<TracesPage />);
    const provider = await screen.findByLabelText(/provider/i);
    fireEvent.change(provider, { target: { value: "anthropic" } });
    fireEvent.keyDown(provider, { key: "Enter" });
    expect(nav.replace).toHaveBeenLastCalledWith(routes.traces({ provider: "anthropic" }));
  });

  it("drops a slower response for old filters and a stale Load more page", async () => {
    const deferred = () => {
      let resolve!: (v: unknown) => void;
      const promise = new Promise((r) => { resolve = r; });
      return { promise, resolve };
    };
    const row = (id: string, at: string) => ({ ...mockRuns[0], run_id: id, created_at: at });
    const first = deferred();
    (listRuns as any).mockReturnValueOnce(first.promise);
    const view = render(<TracesPage />);

    // The filter changes before the first (unfiltered) response arrives.
    const second = deferred();
    (listRuns as any).mockReturnValueOnce(second.promise);
    nav.query = "status=failed";
    view.rerender(<TracesPage />);
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(2));

    await act(async () => second.resolve({ items: [row("run-new", "2026-11-12T10:00:00Z")], next_before: "cursor-new" }));
    await act(async () => first.resolve({ items: [row("run-old", "2026-11-12T11:00:00Z")], next_before: "cursor-old" }));
    let ids = screen.getAllByTestId("trace-row").map((r) => r.querySelector("[data-testid='trace-row-open']")!.getAttribute("href"));
    expect(ids).toEqual([routes.trace("run-new")]);

    // Load more for "failed", then the filter changes again before that page arrives.
    const more = deferred();
    (listRuns as any).mockReturnValueOnce(more.promise);
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect((listRuns as any).mock.calls[2]).toEqual([expect.objectContaining({ status: ["failed"] }), { limit: 50, before: "cursor-new" }]);
    const third = deferred();
    (listRuns as any).mockReturnValueOnce(third.promise);
    nav.query = "status=completed";
    view.rerender(<TracesPage />);
    await waitFor(() => expect(listRuns).toHaveBeenCalledTimes(4));
    await act(async () => third.resolve({ items: [row("run-completed", "2026-11-12T09:00:00Z")], next_before: null }));
    await act(async () => more.resolve({ items: [row("run-stale-page", "2026-11-12T08:00:00Z")], next_before: null }));
    ids = screen.getAllByTestId("trace-row").map((r) => r.querySelector("[data-testid='trace-row-open']")!.getAttribute("href"));
    expect(ids).toEqual([routes.trace("run-completed")]);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("forgets the old filters' cursor when the new filters' first page fails", async () => {
    (listRuns as any).mockResolvedValueOnce({ items: mockRuns, next_before: "cursor-a" });
    const view = render(<TracesPage />);
    expect(await screen.findByRole("button", { name: "Load more" })).toBeInTheDocument();

    (listRuns as any).mockRejectedValueOnce(new Error("Service unavailable"));
    nav.query = "status=failed";
    view.rerender(<TracesPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Service unavailable");
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
    expect(screen.queryAllByTestId("trace-row")).toHaveLength(0);
    expect(listRuns).toHaveBeenCalledTimes(2);
    for (const call of (listRuns as any).mock.calls) expect(call[1].before).toBeUndefined();
  });

  it("dims the old rows (aria-busy) while a new first page loads", async () => {
    (listRuns as any).mockResolvedValueOnce({ items: mockRuns, next_before: null });
    const view = render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    expect(screen.getByTestId("traces-table")).toHaveAttribute("aria-busy", "false");

    let resolve!: (v: unknown) => void;
    (listRuns as any).mockReturnValueOnce(new Promise((r) => { resolve = r; }));
    nav.query = "kind=tool";
    view.rerender(<TracesPage />);
    await waitFor(() => expect(screen.getByTestId("traces-table")).toHaveAttribute("aria-busy", "true"));
    expect(screen.getByTestId("traces-table").className).toContain("opacity-50");
    await act(async () => resolve({ items: [{ ...mockRuns[0], run_id: "run-tool" }], next_before: null }));
    expect(screen.getByTestId("traces-table")).toHaveAttribute("aria-busy", "false");
    expect(screen.getByTestId("trace-row-open")).toHaveAttribute("href", routes.trace("run-tool"));
  });

  it("clears the old error as soon as the next first page starts", async () => {
    (listRuns as any).mockRejectedValueOnce(new Error("boom"));
    const view = render(<TracesPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");

    (listRuns as any).mockReturnValueOnce(new Promise(() => undefined));
    nav.query = "kind=agent";
    view.rerender(<TracesPage />);
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(screen.getByText("Loading runs...")).toBeInTheDocument();
  });
});
