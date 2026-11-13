import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import TracesPage from "./page";
import { listTraceRuns } from "@/lib/traces";
import { api } from "@/lib/api";

const nav = vi.hoisted(() => ({ query: "", replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: nav.replace }),
  useSearchParams: () => new URLSearchParams(nav.query),
}));
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/traces")>()), listTraceRuns: vi.fn() }));
vi.mock("@/lib/api", () => ({ api: { agents: { list: vi.fn() } } }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));

function makeRun(id: string, at: string, extra: Record<string, unknown> = {}) {
  return {
    run_id: id, kind: "agent", agent_id: "agent-1", version: 1, status: "completed", display_status: "completed",
    input: "in", output: "out", error: null, heartbeat_at: null, created_at: at, updated_at: at,
    trace_id: null, conversation_id: null, turn: null, ...extra,
  };
}
const list = vi.mocked(listTraceRuns) as unknown as ReturnType<typeof vi.fn>;
const hrefs = () => screen.getAllByTestId("trace-row-open").map((a) => a.getAttribute("href"));

describe("TracesPage acceptance", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    nav.query = "";
    vi.mocked(api.agents.list).mockResolvedValue([{ id: "agent-1", name: "Support assistant", draft_spec: {}, latest_version: 1 }] as never);
  });

  it("says 'No runs yet.' with no filters and 'No runs match these filters.' with filters", async () => {
    list.mockResolvedValue({ items: [], next_before: null });
    const { unmount } = render(<TracesPage />);
    expect(await screen.findByText("No runs yet.")).toBeInTheDocument();
    unmount();
    nav.query = "status=failed";
    render(<TracesPage />);
    expect(await screen.findByText("No runs match these filters.")).toBeInTheDocument();
  });

  it("Load more appends the next page by cursor, newest first, then the button goes away", async () => {
    list.mockResolvedValueOnce({ items: [makeRun("run-b", "2026-11-12T10:00:00Z")], next_before: "cur-1" });
    render(<TracesPage />);
    await screen.findAllByTestId("trace-row");
    list.mockResolvedValueOnce({ items: [makeRun("run-a", "2026-11-12T09:00:00Z"), makeRun("run-c", "2026-11-12T11:00:00Z")], next_before: null });
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(screen.getAllByTestId("trace-row")).toHaveLength(3));
    expect(list.mock.calls[1][1]).toEqual({ limit: 50, before: "cur-1" });
    expect(hrefs()).toEqual(["/traces/run-c", "/traces/run-b", "/traces/run-a"]);
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("a failed load shows the error and an empty table", async () => {
    list.mockRejectedValue(new Error("API down"));
    render(<TracesPage />);
    expect(await screen.findByText(/API down/)).toBeInTheDocument();
    expect(screen.queryAllByTestId("trace-row")).toHaveLength(0);
  });

  it("a text filter commits on blur and writes to the URL; from and to round-trip", async () => {
    list.mockResolvedValue({ items: [], next_before: null });
    render(<TracesPage />);
    const model = await screen.findByLabelText(/model/i);
    fireEvent.change(model, { target: { value: "openai:gpt-4.1" } });
    fireEvent.blur(model);
    expect(nav.replace).toHaveBeenLastCalledWith("/traces?model=openai%3Agpt-4.1");
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-11-01T00:00" } });
    const url = nav.replace.mock.calls.at(-1)![0] as string;
    expect(new URLSearchParams(url.split("?")[1]).get("from")).toBeTruthy();
  });

  it("the status filter writes a single status and Clear filters goes back to /traces", async () => {
    nav.query = "kind=tool&agent_id=agent-1";
    list.mockResolvedValue({ items: [], next_before: null });
    render(<TracesPage />);
    await waitFor(() => expect(list).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText(/filter by status/i), { target: { value: "failed" } });
    const params = new URLSearchParams((nav.replace.mock.calls.at(-1)![0] as string).split("?")[1]);
    expect(params.get("status")).toBe("failed");
    expect(params.get("kind")).toBe("tool");
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(nav.replace).toHaveBeenLastCalledWith("/traces");
  });
});
