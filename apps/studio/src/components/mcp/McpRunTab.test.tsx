import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { McpRunTab } from "./McpRunTab";
import { listMcpToolRuns, startMcpToolRun } from "@/lib/mcpToolRuns";
import { mapToolRunError } from "@/lib/toolRuns";
import { getRun, subscribeRun, type Run } from "@/lib/runs";
import { toolRunLedger } from "@/lib/toolRunLedger";
import type { McpDiscoveredTool, McpServer } from "@/lib/mcpServers";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

vi.mock("@/lib/mcpToolRuns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpToolRuns")>();
  return { ...actual, startMcpToolRun: vi.fn(), listMcpToolRuns: vi.fn() };
});
vi.mock("@/lib/runs", () => ({ getRun: vi.fn(), subscribeRun: vi.fn() }));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a>,
}));

const server: McpServer = {
  id: "srv-1", name: "docs", url: "https://mcp.example.com/mcp", transport: "streamable_http", enabled: true,
  header_names: [], created_at: null, updated_at: null, last_checked_at: null, last_status: "ok", last_error_code: null,
};
const tools: McpDiscoveredTool[] = [
  {
    name: "add",
    description: "Add two integers.",
    input_schema: {
      type: "object",
      properties: { a: { type: "integer", title: "A" }, b: { type: "integer", title: "B" } },
      required: ["a"],
    },
  },
  { name: "ping", description: null, input_schema: { type: "object", properties: {} } },
];

function run(id: string, extra: Partial<Run> = {}): Run {
  return {
    run_id: id, kind: "tool", tool_id: "mcp:srv-1/add", agent_id: null, version: null, status: "completed",
    display_status: "completed", input: '{"a":1}', output: '{"result":3}', error: null, heartbeat_at: null,
    created_at: "2026-11-16T10:00:00Z", updated_at: "2026-11-16T10:00:01Z", trace_id: null, conversation_id: null, turn: null,
    ...extra,
  } as Run;
}

const accepted = (id: string) => ({ run_id: id, kind: "tool" as const, tool_id: "mcp:srv-1/add", status: "completed", display_status: "completed" as const, trace_id: null });

function renderTab(props: Partial<React.ComponentProps<typeof McpRunTab>> = {}) {
  return render(<McpRunTab server={server} tools={tools} onLoadTools={vi.fn()} loadingTools={false} {...props} />);
}

describe("MCP run tab", () => {
  beforeEach(() => {
    toolRunLedger.reset();
    vi.mocked(startMcpToolRun).mockReset();
    vi.mocked(listMcpToolRuns).mockReset().mockResolvedValue([]);
    vi.mocked(subscribeRun).mockReset().mockReturnValue(() => undefined);
    vi.mocked(getRun).mockReset().mockResolvedValue(run("run-1"));
  });

  it("asks for a connection test when no tools are known", () => {
    const onLoadTools = vi.fn();
    renderTab({ tools: [], onLoadTools });
    fireEvent.click(within(screen.getByTestId("mcp-run-empty")).getByRole("button", { name: "Test connection" }));
    expect(onLoadTools).toHaveBeenCalledTimes(1);
  });

  it("does not offer a test for a disabled server", () => {
    renderTab({ tools: [], server: { ...server, enabled: false } });
    expect(screen.getByRole("button", { name: "Test connection" })).toBeDisabled();
  });

  it("lists the server's tools, runs the selected one on the server's route and shows its history", async () => {
    vi.mocked(startMcpToolRun).mockResolvedValue(accepted("run-1"));
    vi.mocked(subscribeRun).mockImplementation((_id, handlers) => { queueMicrotask(() => handlers.onEnd?.("run_completed")); return () => undefined; });
    vi.mocked(listMcpToolRuns).mockResolvedValueOnce([run("run-0", { display_status: "failed", status: "failed" })]);
    renderTab();
    const select = screen.getByLabelText("Tool");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["Select a tool", "add", "ping"]);

    fireEvent.change(select, { target: { value: "add" } });
    expect(await screen.findByTestId("tool-runner")).toBeInTheDocument();
    await waitFor(() => expect(listMcpToolRuns).toHaveBeenCalledWith("srv-1", "add"));
    expect(await screen.findAllByTestId("mcp-run-history-row")).toHaveLength(1);

    fireEvent.change(screen.getByLabelText(/^A/), { target: { value: "1" } });
    vi.mocked(listMcpToolRuns).mockResolvedValue([run("run-1"), run("run-0", { display_status: "failed", status: "failed" })]);
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(startMcpToolRun).toHaveBeenCalledWith("srv-1", "add", { a: 1 }, expect.any(String)));
    await waitFor(() => expect(screen.getAllByTestId("mcp-run-history-row")).toHaveLength(2));
    await waitFor(() => expect(getRun).toHaveBeenCalledWith("run-1"));
    expect(await screen.findByText(/"result": 3/)).toBeInTheDocument();
  });

  it("shows the API's refusal on the runner's field and banner", async () => {
    vi.mocked(startMcpToolRun).mockRejectedValue(mapToolRunError(422, {
      detail: [{ loc: ["body", "input", "a"], msg: "Input should be of type integer", type: "type_error" }],
    }));
    renderTab();
    fireEvent.change(screen.getByLabelText("Tool"), { target: { value: "add" } });
    await screen.findByTestId("tool-runner");
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByTestId("tool-run-banner")).toHaveTextContent("Fix the highlighted input fields");
    expect(screen.getByText("Input should be of type integer")).toBeInTheDocument();
  });

  it("reports a history that cannot load, with Retry", async () => {
    vi.mocked(listMcpToolRuns).mockRejectedValueOnce(new Error("boom")).mockResolvedValue([]);
    renderTab();
    fireEvent.change(screen.getByLabelText("Tool"), { target: { value: "ping" } });
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Retry" })));
    expect(await screen.findByText("No runs of this tool yet.")).toBeInTheDocument();
  });

  it("has no accessibility violations", async () => {
    const { container } = renderTab();
    fireEvent.change(screen.getByLabelText("Tool"), { target: { value: "add" } });
    await screen.findByTestId("tool-runner");
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
