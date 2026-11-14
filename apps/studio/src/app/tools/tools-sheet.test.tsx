import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import ToolsPage from "./page";
import { api, type Agent, type AgentSpec } from "@/lib/api";
import { listTools, type ToolDefinition } from "@/lib/tools";
import { startToolRun } from "@/lib/toolRuns";
import { getRun, subscribeRun, type SubscribeHandlers } from "@/lib/runs";
import { toolRunLedger } from "@/lib/toolRunLedger";
import { routes } from "@/lib/routes";
import { findToolSheet, toolsNavigation } from "@/components/tools/toolsTesting";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

vi.mock("@/lib/tools", () => ({ listTools: vi.fn() }));
vi.mock("@/lib/toolRuns", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/toolRuns")>();
  return { ...actual, startToolRun: vi.fn() };
});
vi.mock("@/lib/runs", () => ({ getRun: vi.fn(), subscribeRun: vi.fn() }));
vi.mock("next/navigation", async () => (await import("@/components/tools/toolsTesting")).toolsNavigationModule);
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, agents: { ...actual.api.agents, list: vi.fn() }, versions: { ...actual.api.versions, get: vi.fn() } } };
});
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a>,
}));

const tools: ToolDefinition[] = [
  {
    id: "tickets.create",
    title: "Create ticket",
    description: "Creates a support ticket",
    side_effect: true,
    external_idempotent: false,
    input_schema: { type: "object", required: ["subject"], properties: { subject: { type: "string", title: "Subject" } } },
    allowed: true,
  },
  {
    id: "clock.now",
    title: "Current time",
    description: "Returns the current time",
    side_effect: false,
    external_idempotent: true,
    input_schema: { type: "object", properties: {} },
    allowed: true,
  },
];

function spec(builtIn: string[], mcp: Array<[string, string]> = []): AgentSpec {
  return {
    spec_version: 1,
    name: "x",
    instructions: "",
    model: { provider: "openai", name: "gpt" },
    tools: builtIn,
    ...(mcp.length ? { mcp_tools: mcp.map(([server, tool]) => ({ server, tool })) } : {}),
  };
}

const agents: Agent[] = [
  { id: "a1", name: "Support bot", draft_spec: spec(["tickets.create"]), latest_version: 2 },
  { id: "a2", name: "Docs bot", draft_spec: spec([], [["docs", "search"]]), latest_version: null },
];

function row(name: string): HTMLElement {
  // `hidden`: behind an open Sheet the list is aria-hidden (Radix modal).
  const table = screen.getByRole("table", { name: "Tool catalog", hidden: true });
  return within(table).getAllByTestId("tool-row").find((item) => within(item).queryByText(name)) as HTMLElement;
}

describe("Tools page Sheet", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    toolRunLedger.reset();
    toolsNavigation.reset(routes.tools());
    vi.mocked(listTools).mockResolvedValue(tools);
    vi.mocked(subscribeRun).mockReturnValue(() => undefined);
    vi.mocked(api.agents.list).mockResolvedValue(agents);
    vi.mocked(api.versions.get).mockImplementation(async (id: string, n: number) => ({
      agent_id: id, version: n, spec: spec(["tickets.create", "clock.now"]), spec_hash: "h", created_at: "", created_by: "",
    }));
  });

  it("lists built-in and MCP tools with their source and how many agents use them", async () => {
    render(<ToolsPage />);
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(3));
    expect(within(row("Create ticket")).getByTestId("tool-source")).toHaveTextContent("Built-in");
    expect(within(row("search")).getByTestId("tool-source")).toHaveTextContent("docs");
    await waitFor(() => expect(within(row("Create ticket")).getByTestId("tool-used-by-count")).toHaveTextContent("1 agent"));
    expect(within(row("Current time")).getByTestId("tool-used-by-count")).toHaveAttribute("data-count", "1");
    expect(within(row("search")).getByTestId("tool-used-by-count")).toHaveTextContent("1 agent");
    expect(screen.queryByTestId("tool-sheet")).toBeNull();
  });

  it("opens a row in the Sheet on Overview with ?tool=, lists the agents using it, and closing goes Back", async () => {
    render(<ToolsPage />);
    await screen.findByText("Create ticket");
    fireEvent.click(within(row("Create ticket")).getByRole("button"));
    expect(toolsNavigation.url()).toBe(routes.tool("tickets.create"));
    const sheet = await findToolSheet();
    expect(within(sheet).getByRole("heading", { name: "Create ticket" })).toBeInTheDocument();
    expect(within(sheet).getByTestId("tool-tab-overview")).toHaveAttribute("data-state", "active");
    expect(within(sheet).getByTestId("tool-input-schema")).toHaveTextContent('"subject"');
    expect(await within(sheet).findByTestId("tool-used-by-agent")).toHaveTextContent("Support bot");
    expect(within(sheet).getByTestId("tool-used-by-agent")).toHaveTextContent("draft, v2");
    expect(within(sheet).getByRole("link", { name: "Support bot" })).toHaveAttribute("href", routes.agentEditor("a1"));

    fireEvent.click(within(sheet).getByRole("button", { name: /close/i }));
    expect(toolsNavigation.back).toHaveBeenCalledTimes(1);
    expect(toolsNavigation.url()).toBe(routes.tools());
    await waitFor(() => expect(screen.queryByTestId("tool-sheet")).toBeNull());
  });

  it("deep links to the Run tab, keeps the input across tab switches, and reopens after a reload", async () => {
    toolsNavigation.reset(routes.tool("tickets.create", { tab: "run" }));
    const first = render(<ToolsPage />);
    const sheet = await findToolSheet();
    expect(within(sheet).getByTestId("tool-tab-run")).toHaveAttribute("data-state", "active");
    fireEvent.change(within(sheet).getByLabelText(/Subject \*/), { target: { value: "Printer down" } });

    fireEvent.mouseDown(within(sheet).getByTestId("tool-tab-overview"));
    expect(toolsNavigation.url()).toBe(routes.tool("tickets.create"));
    await waitFor(() => expect(within(sheet).getByTestId("tool-tab-overview")).toHaveAttribute("data-state", "active"));
    fireEvent.mouseDown(within(sheet).getByTestId("tool-tab-run"));
    expect(toolsNavigation.url()).toBe(routes.tool("tickets.create", { tab: "run" }));
    expect(await within(sheet).findByLabelText(/Subject \*/)).toHaveValue("Printer down");

    first.unmount();
    render(<ToolsPage />);
    const reopened = await findToolSheet();
    expect(within(reopened).getByRole("heading", { name: "Create ticket" })).toBeInTheDocument();
    expect(within(reopened).getByTestId("tool-tab-run")).toHaveAttribute("data-state", "active");
  });

  it("runs a tool from the drawer and shows the status, duration and a Copy button", async () => {
    toolsNavigation.reset(routes.tool("clock.now", { tab: "run" }));
    vi.mocked(startToolRun).mockResolvedValue({ run_id: "run-9", kind: "tool", tool_id: "clock.now", status: "queued", display_status: "queued", trace_id: null });
    let handlers: SubscribeHandlers = {};
    vi.mocked(subscribeRun).mockImplementation((_id, next) => { handlers = next; return () => undefined; });
    vi.mocked(getRun).mockResolvedValue({
      run_id: "run-9", kind: "tool", agent_id: null, version: null, tool_id: "clock.now", status: "completed", display_status: "completed",
      input: "{}", output: '{"now":"12:00"}', error: null, heartbeat_at: null, created_at: "2026-11-14T10:00:00Z", updated_at: "2026-11-14T10:00:01.250Z",
      trace_id: null, conversation_id: null, turn: null,
    });
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    fireEvent.click(within(sheet).getByRole("button", { name: "Run" }));
    await within(sheet).findByTestId("tool-run-result");
    // The result subscribes in an effect, which can run after the element is in the DOM: wait for it,
    // or `handlers` is still empty and ending the stream does nothing (a race under load).
    await waitFor(() => expect(subscribeRun).toHaveBeenCalledWith("run-9", expect.any(Object)));
    await act(async () => { handlers.onEnd?.("run_completed"); });
    expect(await within(sheet).findByTestId("tool-run-output")).toHaveTextContent('"now": "12:00"');
    expect(within(sheet).getByTestId("tool-run-result")).toHaveAttribute("data-status", "completed");
    expect(within(sheet).getByTestId("tool-run-duration")).toHaveTextContent("1.25 s");
    fireEvent.click(within(sheet).getByTestId("tool-run-copy"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('{\n  "now": "12:00"\n}'));
    expect(await within(sheet).findByText("Copied")).toBeInTheDocument();
  });

  it("an MCP tool opens on Overview only, with &server= and no Run tab, even when the link asks for it", async () => {
    toolsNavigation.reset(`${routes.tool("search", { server: "docs" })}&tab=run`);
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    expect(await within(sheet).findByRole("heading", { name: "search" })).toBeInTheDocument();
    expect(within(sheet).queryByTestId("tool-tab-run")).toBeNull();
    expect(within(sheet).getByTestId("tool-tab-overview")).toHaveAttribute("data-state", "active");
    expect(within(sheet).getByText(/connection test runs/)).toBeInTheDocument();
    expect(await within(sheet).findByTestId("tool-used-by-agent")).toHaveTextContent("Docs bot");
  });

  it("switching from a built-in tool's Run tab to an MCP tool drops the tab", async () => {
    toolsNavigation.reset(routes.tool("clock.now", { tab: "run" }));
    render(<ToolsPage />);
    await findToolSheet();
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(3));
    fireEvent.click(within(row("search")).getByRole("button", { hidden: true }));
    expect(toolsNavigation.url()).toBe(routes.tool("search", { server: "docs" }));
  });

  it("a deep link says the catalog could not load (not 'not found') and Retry loads it", async () => {
    vi.mocked(listTools).mockRejectedValueOnce(new Error("Tools API is down"));
    toolsNavigation.reset(routes.tool("clock.now"));
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    const failure = await within(sheet).findByTestId("tool-load-error");
    expect(failure).toHaveTextContent("Couldn't load tools");
    expect(failure).toHaveTextContent("Tools API is down");
    expect(within(sheet).queryByTestId("tool-not-found")).toBeNull();
    fireEvent.click(within(failure).getByRole("button", { name: "Retry" }));
    expect(await within(sheet).findByRole("heading", { name: "Current time" })).toBeInTheDocument();
    expect(listTools).toHaveBeenCalledTimes(2);
  });

  it("an MCP deep link says the agents could not load (not 'no agent uses') and Retry rescans", async () => {
    vi.mocked(api.agents.list).mockRejectedValueOnce(new Error("down"));
    toolsNavigation.reset(routes.tool("search", { server: "docs" }));
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    const failure = await within(sheet).findByTestId("tool-load-error");
    expect(failure).toHaveTextContent("Couldn't load agents");
    expect(within(sheet).queryByTestId("tool-not-found")).toBeNull();
    fireEvent.click(within(failure).getByRole("button", { name: "Retry" }));
    expect(await within(sheet).findByRole("heading", { name: "search" })).toBeInTheDocument();
  });

  it("says when ?tool= names no tool", async () => {
    toolsNavigation.reset(routes.tool("gone.tool"));
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    expect(await within(sheet).findByTestId("tool-not-found")).toHaveTextContent("gone.tool");
  });

  it("the Sheet is full width below 768 px (so below 640 px) and 480 px from md", async () => {
    toolsNavigation.reset(routes.tool("clock.now"));
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    expect(sheet.className).toMatch(/(^|\s)w-full(\s|$)/);
    expect(sheet.className).toContain("md:w-[480px]");
  });

  it("the list and the open Sheet have no axe violations", async () => {
    const { container } = render(<ToolsPage />);
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(3));
    await expectNoAxeViolations(container);
    fireEvent.click(within(row("Create ticket")).getByRole("button"));
    await expectNoAxeViolations(await findToolSheet());
    fireEvent.mouseDown(within(screen.getByTestId("tool-sheet")).getByTestId("tool-tab-run"));
    await screen.findByLabelText(/Subject \*/);
    await expectNoAxeViolations(screen.getByTestId("tool-sheet"));
  }, AXE_TIMEOUT_MS);
});
