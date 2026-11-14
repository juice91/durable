import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ToolsPage from "./page";
import { api, type Agent, type AgentSpec } from "@/lib/api";
import { listTools, type ToolDefinition } from "@/lib/tools";
import { startToolRun } from "@/lib/toolRuns";
import { getRun, subscribeRun, type SubscribeHandlers } from "@/lib/runs";
import { toolRunLedger } from "@/lib/toolRunLedger";
import { routes } from "@/lib/routes";
import { findToolSheet, toolsNavigation } from "@/components/tools/toolsTesting";
import { AGENT_SPECS_TIMEOUT_MS, useAgentLatestSpecs } from "@/lib/agentUsage";
import { mcpUsageByServer, useMcpUsage } from "@/components/mcp/mcpUsage";
import { loadAgentProcessorSpecs, processorUsage } from "@/lib/processors";
import { expectNoAxeViolations } from "@/test/a11y";

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
  { id: "tickets.create", title: "Create ticket", description: "Creates a support ticket", side_effect: true, external_idempotent: false, allowed: true,
    input_schema: { type: "object", required: ["subject"], properties: { subject: { type: "string", title: "Subject" } } } },
  { id: "clock.now", title: "Current time", description: "Returns the current time", side_effect: false, external_idempotent: true, allowed: true,
    input_schema: { type: "object", properties: {} } },
];

function spec(builtIn: string[], mcp: Array<[string, string]> = [], pii = false): AgentSpec {
  return {
    spec_version: 1, name: "x", instructions: "", model: { provider: "openai", name: "gpt" }, tools: builtIn,
    ...(mcp.length ? { mcp_tools: mcp.map(([server, tool]) => ({ server, tool })) } : {}),
    ...(pii ? { input_processors: [{ type: "pii_redact" }] } : {}),
  } as unknown as AgentSpec;
}

// Alpha: published v3 (draft differs). Beta: draft only. Gamma: published v1, uses nothing.
const alphaDraft = spec(["tickets.create"], [["docs", "search"]], true);
const alphaLatest = spec(["tickets.create", "clock.now"], [["docs", "search"]], true);
const agents: Agent[] = [
  { id: "a1", name: "Alpha", draft_spec: alphaDraft, latest_version: 3 },
  { id: "a2", name: "Beta", draft_spec: spec(["tickets.create"], [["docs", "search"]], true), latest_version: null },
  { id: "a3", name: "Gamma", draft_spec: spec([]), latest_version: 1 },
];
const version = (id: string, n: number, s: AgentSpec) => ({ agent_id: id, version: n, spec: s, spec_hash: "h", created_at: "", created_by: "" });

function mockVersions(fail: string[] = []) {
  vi.mocked(api.versions.get).mockImplementation(async (id: string, n: number) => {
    if (fail.includes(id)) throw new Error("boom");
    return version(id, n, id === "a1" ? alphaLatest : spec([]));
  });
}

function row(name: string): HTMLElement {
  const table = screen.getByRole("table", { name: "Tool catalog", hidden: true });
  return within(table).getAllByTestId("tool-row").find((item) => within(item).queryByText(name)) as HTMLElement;
}
const count = (name: string) => within(row(name)).getByTestId("tool-used-by-count");

beforeEach(() => {
  vi.clearAllMocks();
  toolRunLedger.reset();
  toolsNavigation.reset(routes.tools());
  vi.mocked(listTools).mockResolvedValue(tools);
  vi.mocked(subscribeRun).mockReturnValue(() => undefined);
  vi.mocked(api.agents.list).mockResolvedValue(agents);
  mockVersions();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  document.documentElement.classList.remove("dark");
});

describe("deep links", () => {
  it("?tool= opens that tool's Sheet on Overview, with the Run tab inactive", async () => {
    toolsNavigation.reset(routes.tool("clock.now"));
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    expect(sheet).toHaveAttribute("data-tool", "clock.now");
    expect(within(sheet).getByRole("heading", { name: "Current time" })).toBeInTheDocument();
    expect(within(sheet).getByTestId("tool-tab-overview")).toHaveAttribute("data-state", "active");
    expect(within(sheet).getByTestId("tool-tab-run")).toHaveAttribute("data-state", "inactive");
    expect(within(sheet).getByTestId("tool-policy")).toHaveTextContent("Reads only");
    expect(within(sheet).getByTestId("tool-overview")).toBeInTheDocument();
  });

  it("&tab=run opens the Run tab with the runner shown, and shows no other tool", async () => {
    toolsNavigation.reset(routes.tool("tickets.create", { tab: "run" }));
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    expect(within(sheet).getByTestId("tool-tab-run")).toHaveAttribute("data-state", "active");
    expect(await within(sheet).findByTestId("tool-runner")).toBeVisible();
    expect(screen.getAllByTestId("tool-sheet")).toHaveLength(1);
  });

  it("an unknown ?tool= shows tool-not-found after loading, without a tab list, and opens nothing else", async () => {
    toolsNavigation.reset(routes.tool("nope.tool"));
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    expect(await within(sheet).findByTestId("tool-not-found")).toHaveTextContent("nope.tool");
    expect(within(sheet).queryByTestId("tool-tab-overview")).toBeNull();
    expect(within(sheet).queryByTestId("tool-runner")).toBeNull();
  });

  it("an unknown MCP tool (?server= with no match) names the server in tool-not-found", async () => {
    toolsNavigation.reset(routes.tool("search", { server: "nowhere" }));
    render(<ToolsPage />);
    const missing = await screen.findByTestId("tool-not-found");
    expect(missing).toHaveTextContent("search");
    expect(missing).toHaveTextContent("nowhere");
  });

  it("an MCP tool name without ?server= is not found, even though a row with that name exists", async () => {
    toolsNavigation.reset(routes.tool("search"));
    render(<ToolsPage />);
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(3));
    expect(await screen.findByTestId("tool-not-found")).toBeInTheDocument();
  });

  it("the unknown-tool Sheet closes with Escape and the list is back", async () => {
    toolsNavigation.reset(routes.tool("nope.tool"));
    render(<ToolsPage />);
    await screen.findByTestId("tool-not-found");
    fireEvent.keyDown(screen.getByTestId("tool-sheet"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("tool-sheet")).toBeNull());
    expect(toolsNavigation.url()).toBe(routes.tools());
  });
});

describe("Back and close", () => {
  it("a row pushes one history entry and Escape closes it by going Back", async () => {
    render(<ToolsPage />);
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(3));
    fireEvent.click(within(row("Current time")).getByRole("button"));
    expect(toolsNavigation.push).toHaveBeenCalledTimes(1);
    const sheet = await findToolSheet();
    fireEvent.keyDown(sheet, { key: "Escape" });
    expect(toolsNavigation.back).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByTestId("tool-sheet")).toBeNull());
    expect(toolsNavigation.url()).toBe(routes.tools());
  });

  it("switching tool while open replaces, so one Back closes it; the Run tab also replaces", async () => {
    render(<ToolsPage />);
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(3));
    fireEvent.click(within(row("Current time")).getByRole("button"));
    await findToolSheet();
    fireEvent.mouseDown(screen.getByTestId("tool-tab-run"));
    expect(toolsNavigation.push).toHaveBeenCalledTimes(1);
    expect(toolsNavigation.url()).toBe(routes.tool("clock.now", { tab: "run" }));
    fireEvent.keyDown(screen.getByTestId("tool-sheet"), { key: "Escape" });
    expect(toolsNavigation.back).toHaveBeenCalledTimes(1);
    expect(toolsNavigation.url()).toBe(routes.tools());
  });

  it("a Sheet opened by a deep link closes by clearing tool, server and tab (no Back out of the app)", async () => {
    toolsNavigation.reset(`${routes.tool("clock.now", { tab: "run" })}`);
    render(<ToolsPage />);
    fireEvent.click(within(await findToolSheet()).getByRole("button", { name: /close/i }));
    expect(toolsNavigation.back).not.toHaveBeenCalled();
    expect(toolsNavigation.url()).toBe(routes.tools());
    await waitFor(() => expect(screen.queryByTestId("tool-sheet")).toBeNull());
  });

  it("browser Back from an open Sheet closes it and shows the list", async () => {
    render(<ToolsPage />);
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(3));
    fireEvent.click(within(row("Create ticket")).getByRole("button"));
    await findToolSheet();
    act(() => toolsNavigation.back());
    await waitFor(() => expect(screen.queryByTestId("tool-sheet")).toBeNull());
    expect(screen.getByRole("table", { name: "Tool catalog" })).toBeInTheDocument();
  });
});

describe("MCP tool rows", () => {
  it("lists the MCP tool with its server as source, no schema, and opens an Overview-only Sheet from the row", async () => {
    render(<ToolsPage />);
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(3));
    expect(within(row("search")).getByTestId("tool-source")).toHaveTextContent("docs");
    fireEvent.click(within(row("search")).getByRole("button"));
    // Parameter order differs from routes.tool(); compare the parameters, not the string.
    expect(toolsNavigation.url().split("?")[0]).toBe("/tools");
    expect(Object.fromEntries(toolsNavigation.params())).toEqual({ tool: "search", server: "docs" });
    const sheet = await findToolSheet();
    expect(within(sheet).getByTestId("tool-tab-overview")).toHaveAttribute("data-state", "active");
    expect(within(sheet).queryByTestId("tool-tab-run")).toBeNull();
    expect(within(sheet).queryByTestId("tool-runner")).toBeNull();
    expect(within(sheet).queryByTestId("tool-policy")).toBeNull();
    expect(within(sheet).queryByTestId("tool-input-schema")).toBeNull();
    expect(within(sheet).getByRole("link", { name: "MCP Servers" })).toHaveAttribute("href", routes.mcp());
  });

  it("?tab=run for an MCP tool stays on Overview and does not rewrite the URL", async () => {
    toolsNavigation.reset(`${routes.tool("search", { server: "docs" })}&tab=run`);
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    await within(sheet).findByTestId("tool-overview");
    expect(within(sheet).queryByTestId("tool-runner")).toBeNull();
    expect(toolsNavigation.replace).not.toHaveBeenCalled();
  });
});

describe("used by", () => {
  it("counts the agents using each tool by draft or latest version, and the Sheet lists them with where", async () => {
    render(<ToolsPage />);
    await waitFor(() => expect(count("Create ticket")).toHaveAttribute("data-count", "2"));
    expect(count("Current time")).toHaveAttribute("data-count", "1");
    expect(count("search")).toHaveAttribute("data-count", "2");
    expect(count("Create ticket")).not.toHaveAttribute("data-partial");
    expect(count("Create ticket")).toHaveTextContent("2 agents");
    expect(count("Current time")).toHaveTextContent("1 agent");
    fireEvent.click(within(row("Create ticket")).getByRole("button"));
    const sheet = await findToolSheet();
    const items = within(sheet).getAllByTestId("tool-used-by-agent");
    expect(items.map((item) => item.textContent)).toEqual(["Alphadraft, v3", "Betadraft"]);
  });

  it("an agent that only the latest version uses counts too (Current time: Alpha v3, not its draft)", async () => {
    toolsNavigation.reset(routes.tool("clock.now"));
    render(<ToolsPage />);
    const item = await screen.findByTestId("tool-used-by-agent");
    expect(item).toHaveTextContent("Alpha");
    expect(item).toHaveTextContent("v3");
    expect(item).not.toHaveTextContent("draft");
  });

  it("a failed version read gives data-partial and 'n+' counts (drafts still count), with the reason in the Sheet", async () => {
    mockVersions(["a1"]);
    toolsNavigation.reset(routes.tool("clock.now"));
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    await waitFor(() => expect(count("Create ticket")).toHaveAttribute("data-partial", "true"));
    expect(count("Create ticket")).toHaveAttribute("data-count", "2");
    expect(count("Create ticket")).toHaveTextContent("2+ agents");
    expect(count("Current time")).toHaveAttribute("data-count", "0");
    expect(count("Current time")).toHaveTextContent("0+ agents");
    expect(await within(sheet).findByText(/could not be read; only their drafts were checked/)).toBeInTheDocument();
  });

  it("a rejected agents list shows dashes instead of counts and a Retry that loads again", async () => {
    vi.mocked(api.agents.list).mockRejectedValueOnce(new Error("down"));
    toolsNavigation.reset(routes.tool("clock.now"));
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(2));
    expect(screen.queryAllByTestId("tool-used-by-count")).toHaveLength(0);
    fireEvent.click(await within(sheet).findByRole("button", { name: "Retry" }));
    expect(await within(sheet).findByTestId("tool-used-by-agent")).toHaveTextContent("Alpha");
    expect(api.agents.list).toHaveBeenCalledTimes(2);
  });

  it("a version read that never answers times out after 15 s and counts as partial", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let signal: AbortSignal | undefined;
    vi.mocked(api.versions.get).mockImplementation((id: string, n: number, init?: RequestInit) => {
      if (id !== "a1") return Promise.resolve(version(id, n, spec([])));
      signal = init?.signal ?? undefined;
      return new Promise(() => undefined);
    });
    render(<ToolsPage />);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.queryAllByTestId("tool-used-by-count")).toHaveLength(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(AGENT_SPECS_TIMEOUT_MS + 1); });
    expect(signal?.aborted).toBe(true);
    expect(count("Create ticket")).toHaveAttribute("data-partial", "true");
    expect(count("Create ticket")).toHaveAttribute("data-count", "2");
  });
});

describe("shared usage module", () => {
  it("Tools, MCP Servers and Processors count the same agents for one fixture", async () => {
    render(<ToolsPage />);
    await waitFor(() => expect(count("search")).toHaveAttribute("data-count", "2"));
    const toolsCount = Number(count("search").getAttribute("data-count"));
    const mcp = renderHook(() => useMcpUsage());
    await waitFor(() => expect(mcp.result.current.status).toBe("ready"));
    expect(mcp.result.current.usageFor("docs").map((u) => u.agentName)).toEqual(["Alpha", "Beta"]);
    expect(mcp.result.current.usageForTool("docs", "search")).toHaveLength(toolsCount);
    const processors = processorUsage(await loadAgentProcessorSpecs(agents));
    expect(processors.pii_redact.map((u) => u.agent_name)).toEqual(["Alpha", "Beta"]);
    expect(processors.pii_redact).toHaveLength(toolsCount);
    expect(processors.pii_redact.map((u) => u.version)).toEqual([3, null]);
  });

  it("the hook and the pure MCP grouping agree, and the scan reads each published agent's latest version once", async () => {
    const scan = renderHook(() => useAgentLatestSpecs());
    await waitFor(() => expect(scan.result.current.status).toBe("ready"));
    expect(scan.result.current.partial).toBe(false);
    expect(api.versions.get).toHaveBeenCalledTimes(2);
    expect(api.versions.get).toHaveBeenCalledWith("a1", 3, expect.anything());
    const byServer = mcpUsageByServer(scan.result.current.agents, scan.result.current.latestSpecs);
    expect(byServer.get("docs")).toEqual([
      { agentId: "a1", agentName: "Alpha", draft: true, published: 3 },
      { agentId: "a2", agentName: "Beta", draft: true, published: null },
    ]);
  });

  it("a failed version is partial for all three: Tools '+', MCP partial, Processors 'unknown'", async () => {
    mockVersions(["a1"]);
    render(<ToolsPage />);
    await waitFor(() => expect(count("search")).toHaveAttribute("data-partial", "true"));
    const mcp = renderHook(() => useMcpUsage());
    await waitFor(() => expect(mcp.result.current.status).toBe("ready"));
    expect(mcp.result.current.partial).toBe(true);
    const processors = processorUsage(await loadAgentProcessorSpecs(agents));
    expect(processors.pii_redact.find((u) => u.agent_name === "Alpha")).toMatchObject({ unknown: true, version: 3 });
  });

  it("Processors give up on a version read after 15 s and mark that agent unknown, as before the refactor but now bounded", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    vi.mocked(api.versions.get).mockImplementation(() => new Promise(() => undefined));
    const pending = loadAgentProcessorSpecs(agents);
    await vi.advanceTimersByTimeAsync(AGENT_SPECS_TIMEOUT_MS + 1);
    const loaded = await pending;
    expect(loaded.map((item) => item.failed ?? false)).toEqual([true, false, true]);
    expect(loaded[1].latest).toBeNull();
  });
});

describe("run result duration and copy", () => {
  function finishedRun(over: Record<string, unknown> = {}) {
    return {
      run_id: "run-1", kind: "tool", agent_id: null, version: null, tool_id: "clock.now", status: "completed", display_status: "completed",
      input: "{}", output: '{"now":"12:00"}', error: null, heartbeat_at: null, created_at: "2026-11-14T10:00:00Z",
      updated_at: "2026-11-14T10:00:02Z", trace_id: null, conversation_id: null, turn: null, ...over,
    } as Awaited<ReturnType<typeof getRun>>;
  }
  async function runClock(run: Awaited<ReturnType<typeof getRun>>, end = true) {
    toolsNavigation.reset(routes.tool("clock.now", { tab: "run" }));
    vi.mocked(startToolRun).mockResolvedValue({ run_id: "run-1", kind: "tool", tool_id: "clock.now", status: "queued", display_status: "queued", trace_id: null });
    let handlers: SubscribeHandlers = {};
    vi.mocked(subscribeRun).mockImplementation((_id, next) => { handlers = next; return () => undefined; });
    vi.mocked(getRun).mockResolvedValue(run);
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    fireEvent.click(within(sheet).getByRole("button", { name: "Run" }));
    const result = await within(sheet).findByTestId("tool-run-result");
    // The result subscribes in an effect, which can run after the element is in the DOM: wait for it,
    // or `handlers` is still empty and ending the stream does nothing (a race under load).
    await waitFor(() => expect(subscribeRun).toHaveBeenCalledWith("run-1", expect.any(Object)));
    if (end) await act(async () => { handlers.onEnd?.("run_completed"); });
    return { sheet, result };
  }
  const clipboard = (writeText: (text: string) => Promise<void>) =>
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });

  it("shows no duration while the run is still going, then the duration once it ends", async () => {
    const { sheet, result } = await runClock(finishedRun(), false);
    expect(within(result).queryByTestId("tool-run-duration")).toBeNull();
    expect(within(sheet).queryByTestId("tool-run-copy")).toBeNull();
    cleanup();
    const ended = await runClock(finishedRun());
    expect(await within(ended.sheet).findByTestId("tool-run-duration")).toHaveTextContent("2.00 s");
  });

  it("Copy writes the formatted output to the clipboard and says Copied", async () => {
    const writeText = vi.fn(async () => undefined);
    clipboard(writeText);
    const { sheet } = await runClock(finishedRun());
    fireEvent.click(await within(sheet).findByTestId("tool-run-copy"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('{\n  "now": "12:00"\n}'));
    expect(await within(sheet).findByText("Copied")).toBeInTheDocument();
  });

  it("a clipboard that refuses leaves the button on Copy without an error page or alert", async () => {
    clipboard(vi.fn(async () => { throw new Error("denied"); }));
    const { sheet } = await runClock(finishedRun());
    fireEvent.click(await within(sheet).findByTestId("tool-run-copy"));
    await act(async () => { await Promise.resolve(); });
    expect(within(sheet).getByTestId("tool-run-copy")).toHaveTextContent("Copy");
    expect(within(sheet).queryByText("Copied")).toBeNull();
  });

  it("a failed run shows its duration and error but no Copy button", async () => {
    const { sheet } = await runClock(finishedRun({ status: "failed", display_status: "failed", output: null, error: "boom", updated_at: "2026-11-14T10:00:00.400Z" }));
    expect(await within(sheet).findByTestId("tool-run-duration")).toHaveTextContent("400 ms");
    expect(within(sheet).getByTestId("tool-run-error")).toHaveTextContent("boom");
    expect(within(sheet).queryByTestId("tool-run-copy")).toBeNull();
  });

  it("a run whose timestamps are unusable shows no duration rather than a wrong one", async () => {
    const { sheet } = await runClock(finishedRun({ updated_at: "2026-11-14T09:00:00Z" }));
    await within(sheet).findByTestId("tool-run-output");
    expect(within(sheet).queryByTestId("tool-run-duration")).toBeNull();
  });
});

describe("keyboard", () => {
  it("/ focuses the list search, and typing filters rows (and not while typing in it)", async () => {
    render(<ToolsPage />);
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(3));
    const search = screen.getByRole("searchbox", { name: "Search tools" });
    fireEvent.keyDown(document.body, { key: "/" });
    expect(search).toHaveFocus();
    fireEvent.change(search, { target: { value: "clock" } });
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(1));
    fireEvent.keyDown(search, { key: "/" });
    expect(search).toHaveFocus();
    expect(search).toHaveValue("clock");
  });

  it("/ does nothing while the Sheet is open", async () => {
    toolsNavigation.reset(routes.tool("clock.now"));
    render(<ToolsPage />);
    await findToolSheet();
    const search = screen.getByRole("searchbox", { name: "Search tools", hidden: true });
    // The guard itself, not the focus trap: the shortcut neither runs nor swallows the key.
    const focus = vi.spyOn(search, "focus");
    expect(fireEvent.keyDown(document.body, { key: "/" })).toBe(true);
    expect(focus).not.toHaveBeenCalled();
    expect(search).not.toHaveFocus();
    focus.mockRestore();
  });

  it("Tab order in the open Sheet is the tab list, then the Run form; Escape closes and the row regains focus", async () => {
    render(<ToolsPage />);
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(3));
    const opener = within(row("Create ticket")).getByRole("button");
    opener.focus();
    fireEvent.click(opener);
    const sheet = await findToolSheet();
    const stops = Array.from(sheet.querySelectorAll<HTMLElement>("button, a[href], input, textarea, select, [tabindex]:not([tabindex='-1'])"))
      .filter((el) => !el.closest("[hidden]") && !el.hasAttribute("disabled"));
    const labels = stops.map((el) => el.textContent?.trim() || el.getAttribute("aria-label") || "");
    expect(labels.findIndex((l) => l === "Overview")).toBeLessThan(labels.findIndex((l) => l === "Run"));
    // While Overview shows, the Run tab's form (kept mounted, hidden) has no tab stops.
    const runner = within(sheet).getByTestId("tool-runner");
    expect(runner.closest("[hidden]")).not.toBeNull();
    expect(stops.filter((el) => runner.contains(el))).toEqual([]);
    expect(within(sheet).queryByRole("textbox", { name: /Subject/ })).toBeNull();
    const closeIndex = labels.findIndex((l) => /close/i.test(l));
    expect(closeIndex).toBeGreaterThanOrEqual(0);
    expect(sheet.contains(document.activeElement)).toBe(true);
    fireEvent.keyDown(sheet, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("tool-sheet")).toBeNull());
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("the Overview/Run tabs move with the arrow keys", async () => {
    toolsNavigation.reset(routes.tool("clock.now"));
    render(<ToolsPage />);
    const overview = await screen.findByTestId("tool-tab-overview");
    overview.focus();
    await act(async () => { fireEvent.keyDown(overview, { key: "ArrowRight" }); });
    await waitFor(() => expect(screen.getByTestId("tool-tab-run")).toHaveFocus());
  });
});

describe("phone width and headings", () => {
  it("the Sheet is w-full below md and 480 px from md; the list never forces horizontal scroll classes", async () => {
    toolsNavigation.reset(routes.tool("tickets.create", { tab: "run" }));
    render(<ToolsPage />);
    const sheet = await findToolSheet();
    const classes = sheet.className.split(/\s+/);
    expect(classes).toContain("w-full");
    expect(classes).toContain("md:w-[480px]");
    expect(classes.some((c) => /^w-\[\d+px\]$/.test(c))).toBe(false);
    expect(classes.some((c) => /^(min-w|w)-\[(4|5|6)\d\dpx\]$/.test(c))).toBe(false);
  });

  it("the page has exactly one h1, with the Sheet closed and open", async () => {
    const { container } = render(<ToolsPage />);
    await waitFor(() => expect(screen.getAllByTestId("tool-row")).toHaveLength(3));
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Tools");
    fireEvent.click(within(row("Current time")).getByRole("button"));
    await findToolSheet();
    expect(container.ownerDocument.querySelectorAll("h1")).toHaveLength(1);
  });
});

describe.each([["light", false], ["dark", true]])("axe in the %s theme", (_name, dark) => {
  it("has no violations for the list, the Overview, the Run tab, an MCP Sheet and not-found", async () => {
    document.documentElement.classList.toggle("dark", dark);
    const { container } = render(<ToolsPage />);
    await waitFor(() => expect(count("Create ticket")).toHaveAttribute("data-count", "2"));
    await expectNoAxeViolations(container);
    fireEvent.click(within(row("Create ticket")).getByRole("button"));
    await within(await findToolSheet()).findAllByTestId("tool-used-by-agent");
    await expectNoAxeViolations(document.body);
    fireEvent.mouseDown(screen.getByTestId("tool-tab-run"));
    await screen.findByTestId("tool-runner");
    await expectNoAxeViolations(document.body);
    act(() => { toolsNavigation.replace(routes.tool("search", { server: "docs" })); });
    await waitFor(() => expect(screen.queryByTestId("tool-tab-run")).toBeNull());
    await expectNoAxeViolations(document.body);
    act(() => { toolsNavigation.replace(routes.tool("gone")); });
    await screen.findByTestId("tool-not-found");
    await expectNoAxeViolations(document.body);
  });
});
