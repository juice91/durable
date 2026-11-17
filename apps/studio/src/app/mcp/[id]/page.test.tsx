import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import McpServerDetailPage from "./page";
import { api, ApiError, type Agent, type AgentSpec, type AgentVersion } from "@/lib/api";
import { mcpServers, type McpServer } from "@/lib/mcpServers";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { MCP_USAGE_TIMEOUT_MS } from "@/components/mcp/mcpUsage";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "srv-1" }), useRouter: () => ({ push }) }));

vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return {
    ...actual,
    mcpServers: { list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), setEnabled: vi.fn(), remove: vi.fn(), test: vi.fn() },
  };
});

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, agents: { ...actual.api.agents, list: vi.fn() }, versions: { ...actual.api.versions, get: vi.fn() } } };
});

const server: McpServer = {
  id: "srv-1",
  name: "docs",
  url: "https://mcp.example.com/mcp",
  transport: "streamable_http",
  enabled: true,
  header_names: ["authorization"],
  created_at: null,
  updated_at: null,
  last_checked_at: null,
  last_status: null,
  last_error_code: null,
};

function spec(servers: string[]): AgentSpec {
  return { spec_version: 1, name: "x", instructions: "", model: { provider: "p", name: "m" }, tools: [], ...(servers.length ? { mcp_tools: servers.map((s) => ({ server: s, tool: "search" })) } : {}) };
}

const agents: Agent[] = [
  { id: "a1", name: "Writer", draft_spec: spec(["docs"]), latest_version: null },
  { id: "a2", name: "Helper", draft_spec: spec([]), latest_version: 2 },
  { id: "a3", name: "Unrelated", draft_spec: spec(["other"]), latest_version: null },
];
const helperV2: AgentVersion = { agent_id: "a2", version: 2, spec: spec(["docs"]), spec_hash: "h", created_at: "", created_by: "" };

describe("MCP server detail page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(mcpServers.get).mockResolvedValue(server);
    vi.mocked(mcpServers.list).mockResolvedValue([server, { ...server, id: "srv-2", name: "search" }]);
    vi.mocked(api.agents.list).mockResolvedValue(agents);
    vi.mocked(api.versions.get).mockResolvedValue(helperV2);
  });

  it("shows the connection card with header names only, and the server name as the h1", async () => {
    render(<McpServerDetailPage />);
    expect(await screen.findByRole("heading", { level: 1, name: "docs" })).toBeInTheDocument();
    const connection = screen.getByTestId("mcp-connection");
    expect(within(connection).getByText("https://mcp.example.com/mcp")).toBeInTheDocument();
    expect(within(connection).getByTestId("mcp-header-names")).toHaveTextContent("authorization (set)");
    expect(within(connection).getByTestId("mcp-last-test")).toHaveTextContent("Not tested");
    expect(screen.getByRole("link", { name: "MCP Servers" })).toHaveAttribute("href", routes.mcp());
  });

  it("shows a Runtime tab only for managed servers", async () => {
    const { unmount } = render(<McpServerDetailPage />);
    await screen.findByTestId("mcp-tab-run");
    expect(screen.queryByTestId("mcp-tab-runtime")).toBeNull();
    unmount();
    vi.mocked(mcpServers.get).mockResolvedValue({ ...server, runtime: "managed", runtime_state: "running" });
    render(<McpServerDetailPage />);
    expect(await screen.findByTestId("mcp-tab-runtime")).toBeInTheDocument();
  });

  it("has a Run tab that lists the tested server's tools", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue({
      status: "ok",
      tools: [{ name: "search", description: "Search docs", input_schema: { type: "object", properties: { q: { type: "string" } } } }],
    });
    render(<McpServerDetailPage />);
    await screen.findByRole("heading", { level: 1, name: "docs" });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Run" }), { button: 0, ctrlKey: false });
    const empty = await screen.findByTestId("mcp-run-empty");
    fireEvent.click(within(empty).getByRole("button", { name: "Test connection" }));
    const select = await screen.findByLabelText("Tool");
    expect(within(select).getByRole("option", { name: "search" })).toBeInTheDocument();
  });

  it("gives a not-found state for an unknown id", async () => {
    vi.mocked(mcpServers.get).mockRejectedValue(new ApiError(404, "not found"));
    render(<McpServerDetailPage />);
    const notFound = await screen.findByTestId("mcp-server-not-found");
    expect(within(notFound).getByRole("link", { name: "Back to MCP servers" })).toHaveAttribute("href", routes.mcp());
  });

  it("tests the connection, lists the tools and opens a tool's read-only Sheet", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue({
      status: "ok",
      tools: [{ name: "search", description: "Search the docs", input_schema: { type: "object", properties: { q: { type: "string" } } } }],
    });
    vi.mocked(mcpServers.get).mockResolvedValueOnce(server).mockResolvedValue({ ...server, last_status: "ok", last_checked_at: "2026-11-14T10:00:00Z" });
    render(<McpServerDetailPage />);
    expect(await screen.findByText("Test the connection to list its tools")).toBeInTheDocument();
    fireEvent.click(within(screen.getByTestId("mcp-connection")).getByRole("button", { name: "Test connection" }));

    const tool = await screen.findByTestId("mcp-tool");
    expect(within(tool).getByText("Search the docs")).toBeInTheDocument();
    expect(screen.getByTestId("mcp-test-tools")).toHaveTextContent("Connected. 1 tool discovered.");
    await waitFor(() => expect(screen.getByTestId("mcp-connection").querySelector("[data-testid='mcp-last-test']")).toHaveTextContent("Connected"));

    fireEvent.click(within(tool).getByRole("button", { name: "search" }));
    const sheet = await screen.findByTestId("mcp-tool-sheet");
    expect(within(sheet).getByTestId("mcp-tool-schema")).toHaveTextContent('"q"');
  });

  it("shows an outbound-policy test failure", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue({ status: "error", error_code: "address_blocked", tools: [] });
    render(<McpServerDetailPage />);
    fireEvent.click(await within(await screen.findByTestId("mcp-connection")).findByRole("button", { name: "Test connection" }));
    expect(await screen.findByTestId("mcp-test-error")).toHaveTextContent("Blocked by the outbound policy");
  });

  it("lists the agents whose draft or latest version uses it", async () => {
    render(<McpServerDetailPage />);
    const rows = await screen.findAllByTestId("mcp-used-by-agent");
    expect(rows.map((row) => row.textContent)).toEqual(["Helperv2", "Writerdraft"]);
    expect(within(rows[0]).getByRole("link", { name: "Helper" })).toHaveAttribute("href", routes.agentEditor("a2"));
  });

  it("deletes after a confirm that lists the referencing agents, then returns to the list", async () => {
    vi.mocked(mcpServers.remove).mockResolvedValue(undefined);
    render(<McpServerDetailPage />);
    fireEvent.click(await screen.findByTestId("mcp-delete"));
    const dialog = await screen.findByRole("dialog", { name: "Delete docs?" });
    await waitFor(() => expect(within(dialog).getAllByTestId("mcp-delete-reference")).toHaveLength(2));
    expect(within(dialog).getByTestId("mcp-delete-references")).toHaveTextContent("These 2 agents use its tools");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete server" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(routes.mcp()));
    expect(mcpServers.remove).toHaveBeenCalledWith("srv-1");
  });

  it("after a confirmed delete focus lands on the page's main, not <body> (browser check)", async () => {
    vi.mocked(mcpServers.remove).mockResolvedValue(undefined);
    const { rerender } = render(<main tabIndex={-1}><McpServerDetailPage /></main>);
    fireEvent.click(await screen.findByTestId("mcp-delete"));
    const dialog = await screen.findByRole("dialog", { name: "Delete docs?" });
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Delete server" })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete server" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(routes.mcp()));
    // Until the navigation lands, the confirm stays open and busy (no flash of the old page's focus).
    expect(screen.getByRole("dialog", { name: "Delete docs?" })).toHaveAttribute("aria-busy", "true");
    // The navigation unmounts the detail page.
    rerender(<main tabIndex={-1} />);
    expect(document.querySelector("main")).toHaveFocus();
  });

  it("opening the confirm while the result is fresh still re-checks: cached list, Re-checking…, Delete disabled until the answer", async () => {
    render(<McpServerDetailPage />);
    await screen.findAllByTestId("mcp-used-by-agent");
    expect(api.versions.get).toHaveBeenCalledWith("a2", 2, expect.objectContaining({ signal: expect.any(AbortSignal) }));
    // Within the minute: a new agent now uses the server, and the re-check is slow.
    let release: (value: Agent[]) => void = () => undefined;
    const newcomer: Agent = { id: "a9", name: "Newcomer", draft_spec: spec(["docs"]), latest_version: null };
    vi.mocked(api.agents.list).mockImplementation(() => new Promise<Agent[]>((resolve) => { release = resolve; }));

    fireEvent.click(screen.getByTestId("mcp-delete"));
    const dialog = await screen.findByRole("dialog", { name: "Delete docs?" });
    const refs = within(dialog).getByTestId("mcp-delete-references");
    expect(api.agents.list).toHaveBeenCalledTimes(2);
    expect(within(refs).getAllByTestId("mcp-delete-reference")).toHaveLength(2); // the cached list stays visible
    expect(within(refs).getByTestId("mcp-delete-references-rechecking")).toHaveTextContent("Re-checking…");
    expect(within(dialog).getByRole("button", { name: "Delete server" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeEnabled();

    await act(async () => release([...agents, newcomer]));
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Delete server" })).toBeEnabled());
    expect(within(refs).queryByTestId("mcp-delete-references-rechecking")).toBeNull();
    expect(within(refs).getAllByTestId("mcp-delete-reference").map((item) => item.textContent)).toEqual(["Helperv2", "Newcomerdraft", "Writerdraft"]);
  });

  it("keeps Delete server disabled while the first load runs", async () => {
    vi.mocked(api.agents.list).mockImplementation(() => new Promise<Agent[]>(() => undefined));
    render(<McpServerDetailPage />);
    fireEvent.click(await screen.findByTestId("mcp-delete"));
    const dialog = await screen.findByRole("dialog", { name: "Delete docs?" });
    expect(within(dialog).getByTestId("mcp-delete-references")).toHaveAttribute("data-status", "loading");
    expect(within(dialog).getByRole("button", { name: "Delete server" })).toBeDisabled();
  });

  it("a hung agents list times out into the error state, so Delete is enabled again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(api.agents.list).mockImplementation(() => new Promise<Agent[]>(() => undefined));
      render(<McpServerDetailPage />);
      fireEvent.click(await screen.findByTestId("mcp-delete"));
      const dialog = await screen.findByRole("dialog", { name: "Delete docs?" });
      expect(within(dialog).getByRole("button", { name: "Delete server" })).toBeDisabled();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(MCP_USAGE_TIMEOUT_MS + 10);
      });
      await waitFor(() => expect(within(dialog).getByTestId("mcp-delete-references")).toHaveAttribute("data-status", "error"));
      expect(within(dialog).getByRole("button", { name: "Delete server" })).toBeEnabled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("a hung version read times out and counts as partial", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      vi.mocked(api.versions.get).mockImplementation(() => new Promise<AgentVersion>(() => undefined));
      render(<McpServerDetailPage />);
      await screen.findByTestId("mcp-used-by");
      await act(async () => {
        await vi.advanceTimersByTimeAsync(MCP_USAGE_TIMEOUT_MS + 10);
      });
      await waitFor(() => expect(screen.getByTestId("mcp-used-by")).toHaveTextContent("Some published versions could not be read"));
      expect(screen.getAllByTestId("mcp-used-by-agent").map((item) => within(item).getByRole("link").textContent)).toEqual(["Writer"]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps Delete server enabled when references could not be checked", async () => {
    vi.mocked(api.agents.list).mockRejectedValue(new ApiError(500, "boom"));
    render(<McpServerDetailPage />);
    fireEvent.click(await screen.findByTestId("mcp-delete"));
    const dialog = await screen.findByRole("dialog", { name: "Delete docs?" });
    await waitFor(() => expect(within(dialog).getByTestId("mcp-delete-references")).toHaveAttribute("data-status", "error"));
    expect(within(dialog).getByRole("button", { name: "Delete server" })).toBeEnabled();
  });

  it("says it could not check references when agents fail to load", async () => {
    vi.mocked(api.agents.list).mockRejectedValue(new ApiError(500, "boom"));
    render(<McpServerDetailPage />);
    fireEvent.click(await screen.findByTestId("mcp-delete"));
    const dialog = await screen.findByRole("dialog", { name: "Delete docs?" });
    await waitFor(() => expect(within(dialog).getByTestId("mcp-delete-references")).toHaveTextContent("Could not check which agents use it"));
  });

  it("edits in a Dialog and drops the stale tool list when the URL changes", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue({ status: "ok", tools: [{ name: "search", description: null, input_schema: {} }] });
    vi.mocked(mcpServers.update).mockResolvedValue({ ...server, url: "https://new.example.com/mcp" });
    render(<McpServerDetailPage />);
    const connection = await screen.findByTestId("mcp-connection");
    fireEvent.click(within(connection).getByRole("button", { name: "Test connection" }));
    await screen.findByTestId("mcp-tool");
    fireEvent.click(within(connection).getByRole("button", { name: "Edit" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit docs" });
    fireEvent.change(within(dialog).getByLabelText("URL"), { target: { value: "https://new.example.com/mcp" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(within(connection).getByText("https://new.example.com/mcp")).toBeInTheDocument();
    expect(screen.queryByTestId("mcp-tool")).toBeNull();
  });

  it("has no axe violations", async () => {
    const { container } = render(<McpServerDetailPage />);
    await screen.findAllByTestId("mcp-used-by-agent");
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
