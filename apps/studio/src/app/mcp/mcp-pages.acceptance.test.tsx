import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import McpServersPage from "./page";
import McpServerDetailPage from "./[id]/page";
import { api, ApiError, type Agent, type AgentSpec, type AgentVersion } from "@/lib/api";
import { mcpServers, type McpServer, type McpTestResult } from "@/lib/mcpServers";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { ShortcutsProvider } from "@/components/shortcuts/ShortcutsProvider";

// SL.8 acceptance: the /mcp DataList and the /mcp/[id] detail page, beyond the re-pointed P0 tests.

// Links record where they would go instead of navigating (jsdom cannot).
const navigations: string[] = [];
vi.mock("next/link", () => ({
  default: ({ href, children, onClick, ...rest }: { href: string; children: React.ReactNode; onClick?: (e: React.MouseEvent) => void }) => (
    <a
      href={href}
      {...rest}
      onClick={(event) => {
        onClick?.(event);
        event.preventDefault();
        navigations.push(href);
      }}
    >
      {children}
    </a>
  ),
}));

const params = { id: "srv-1" };
const push = vi.fn();
vi.mock("next/navigation", () => ({
  useParams: () => params,
  useRouter: () => ({ push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/mcp",
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, agents: { ...actual.api.agents, list: vi.fn() }, versions: { ...actual.api.versions, get: vi.fn() } } };
});

vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return {
    ...actual,
    mcpServers: { list: vi.fn(), get: vi.fn(), create: vi.fn(), update: vi.fn(), setEnabled: vi.fn(), remove: vi.fn(), test: vi.fn() },
  };
});

const SECRET = "Bearer sk-live-9f8e7d6c5b4a";

function server(overrides: Partial<McpServer> = {}): McpServer {
  return {
    id: "srv-1",
    name: "docs",
    url: "https://mcp.example.com/mcp",
    transport: "streamable_http",
    enabled: true,
    header_names: ["authorization", "x-api-key"],
    created_at: null,
    updated_at: null,
    last_checked_at: null,
    last_status: null,
    last_error_code: null,
    ...overrides,
  };
}

function spec(servers: string[]): AgentSpec {
  return {
    spec_version: 1,
    name: "x",
    instructions: "",
    model: { provider: "p", name: "m" },
    tools: [],
    ...(servers.length ? { mcp_tools: servers.map((name) => ({ server: name, tool: "search" })) } : {}),
  };
}
const agent = (id: string, name: string, draft: string[], latest: number | null): Agent => ({ id, name, draft_spec: spec(draft), latest_version: latest });
const version = (agentId: string, n: number, servers: string[]): AgentVersion => ({ agent_id: agentId, version: n, spec: spec(servers), spec_hash: "h", created_at: "", created_by: "" });

// Writer: draft only. Helper: latest v2 only. Both: draft and v3. Old: only an OLDER version (v1)
// uses it, so it is not a reference. Unrelated: another server.
const AGENTS: Agent[] = [
  agent("a1", "Writer", ["docs"], null),
  agent("a2", "Helper", [], 2),
  agent("a3", "Both", ["docs"], 3),
  agent("a4", "Old", [], 4),
  agent("a5", "Unrelated", ["other"], null),
];
const VERSIONS: Record<string, AgentVersion[]> = {
  a2: [version("a2", 1, []), version("a2", 2, ["docs"])],
  a3: [version("a3", 3, ["docs"])],
  a4: [version("a4", 1, ["docs"]), version("a4", 4, [])],
};

function findVersion(id: string, n: number): AgentVersion {
  const version = VERSIONS[id]?.find((item) => item.version === n);
  if (!version) throw new ApiError(404, "version not found");
  return version;
}

const TOOLS: McpTestResult = {
  status: "ok",
  tools: [
    { name: "search", description: "Search the docs", input_schema: { type: "object", properties: { q: { type: "string" } }, required: ["q"] } },
    { name: "fetch_page", description: null, input_schema: { type: "object", properties: { url: { type: "string" } } } },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  navigations.length = 0;
  params.id = "srv-1";
  vi.mocked(mcpServers.list).mockResolvedValue([server()]);
  vi.mocked(mcpServers.get).mockResolvedValue(server());
  vi.mocked(api.agents.list).mockResolvedValue(AGENTS);
  vi.mocked(api.versions.get).mockImplementation(async (id: string, n: number) => findVersion(id, n));
});

afterEach(() => {
  document.documentElement.classList.remove("dark");
});

const row = () => screen.getByTestId("mcp-server-row");
const connection = () => screen.getByTestId("mcp-connection");

// "Add server" is a menu now: open it, then pick an item.
async function pickAddItem(name: string) {
  fireEvent.keyDown(screen.getByTestId("mcp-add-server"), { key: "Enter" });
  fireEvent.click(await screen.findByRole("menuitem", { name }));
}

describe("MCP list: rows and row actions", () => {
  it("a plain row click opens the detail; row actions and clicks inside their dialogs never do", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue(TOOLS);
    vi.mocked(mcpServers.setEnabled).mockImplementation(async (_id, enabled) => server({ enabled }));
    render(<McpServersPage />);
    await within(await screen.findByTestId("mcp-server-row")).findByTestId("mcp-used-by-count");

    // Positive control: a click on a plain cell follows the row's link.
    fireEvent.click(within(row()).getByText("https://mcp.example.com/mcp"));
    expect(navigations).toEqual([routes.mcpServer("srv-1")]);
    navigations.length = 0;

    fireEvent.click(within(row()).getByRole("button", { name: "Test connection" }));
    await within(row()).findByTestId("mcp-tool-count");
    fireEvent.click(within(row()).getByRole("button", { name: "Disable" }));
    await within(row()).findByRole("button", { name: "Enable" });

    // Edit: the Dialog is portaled but React bubbles its clicks through the row.
    fireEvent.click(within(row()).getByRole("button", { name: "Edit" }));
    const edit = await screen.findByRole("dialog", { name: "Edit docs" });
    fireEvent.click(within(edit).getByText(/Header values are never shown again/));
    fireEvent.click(within(edit).getByLabelText("Name"));
    fireEvent.click(within(edit).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    // Delete: the confirm text, its reference links' surroundings and Cancel.
    fireEvent.click(within(row()).getByRole("button", { name: "Delete" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete docs?" });
    await waitFor(() => expect(within(confirm).getAllByTestId("mcp-delete-reference")).toHaveLength(3));
    fireEvent.click(within(confirm).getByText("Its stored header secrets are deleted too."));
    fireEvent.click(within(confirm).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    expect(navigations).toEqual([]);
    expect(push).not.toHaveBeenCalled();
  });

  it("Test connection updates the status dot and shows the tool count only after a test", async () => {
    vi.mocked(mcpServers.test).mockResolvedValueOnce(TOOLS).mockResolvedValueOnce({ status: "error", error_code: "timeout", tools: [] });
    vi.mocked(mcpServers.get)
      .mockResolvedValueOnce(server({ last_status: "ok", last_checked_at: "2026-11-14T10:00:00Z" }))
      .mockResolvedValueOnce(server({ last_status: "error", last_error_code: "timeout", last_checked_at: "2026-11-14T10:05:00Z" }));
    render(<McpServersPage />);
    await screen.findByTestId("mcp-server-row");
    const dot = () => within(row()).getByTestId("mcp-status-dot");
    expect(dot()).toHaveAttribute("data-status", "untested");
    expect(within(row()).getByTestId("mcp-last-test")).toHaveTextContent("Not tested");
    expect(within(row()).queryByTestId("mcp-tool-count")).toBeNull();

    fireEvent.click(within(row()).getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(dot()).toHaveAttribute("data-status", "ok"));
    expect(within(row()).getByTestId("mcp-last-test")).toHaveTextContent("Connected");
    expect(within(row()).getByTestId("mcp-tool-count")).toHaveTextContent("2");
    expect(mcpServers.test).toHaveBeenCalledWith("srv-1");

    // A failed test turns the dot red and drops the count (no tools are known).
    await waitFor(() => expect(within(row()).getByRole("button", { name: "Test connection" })).toBeEnabled());
    fireEvent.click(within(row()).getByRole("button", { name: "Test connection" }));
    await waitFor(() => expect(dot()).toHaveAttribute("data-status", "error"));
    expect(within(row()).getByTestId("mcp-last-test")).toHaveTextContent("The connection timed out.");
    expect(within(row()).queryByTestId("mcp-tool-count")).toBeNull();
  });

  it("disables then enables from the row, updating the badge and the action label", async () => {
    vi.mocked(mcpServers.setEnabled).mockImplementation(async (_id, enabled) => server({ enabled }));
    render(<McpServersPage />);
    await screen.findByTestId("mcp-server-row");
    expect(within(row()).getByText("Enabled")).toBeInTheDocument();

    fireEvent.click(within(row()).getByRole("button", { name: "Disable" }));
    expect(await within(row()).findByText("Disabled")).toBeInTheDocument();
    expect(mcpServers.setEnabled).toHaveBeenLastCalledWith("srv-1", false);
    expect(within(row()).queryByRole("button", { name: "Disable" })).toBeNull();

    fireEvent.click(within(row()).getByRole("button", { name: "Enable" }));
    expect(await within(row()).findByText("Enabled")).toBeInTheDocument();
    expect(mcpServers.setEnabled).toHaveBeenLastCalledWith("srv-1", true);
    expect(mcpServers.setEnabled).toHaveBeenCalledTimes(2);
  });

  it("`c` opens Add server, but not while typing, with a modifier, or over an open dialog", async () => {
    // `c` is bound by the global shortcut provider (in the root layout).
    render(<ShortcutsProvider><McpServersPage /></ShortcutsProvider>);
    await screen.findByTestId("mcp-server-row");
    expect(screen.getByTestId("mcp-add-server")).toHaveAttribute("aria-keyshortcuts", "c");

    const search = screen.getByRole("searchbox");
    search.focus();
    fireEvent.keyDown(search, { key: "c" });
    fireEvent.keyDown(document.body, { key: "c", metaKey: true });
    fireEvent.keyDown(document.body, { key: "c", ctrlKey: true });
    fireEvent.keyDown(document.body, { key: "C", shiftKey: true });
    expect(screen.queryByRole("dialog")).toBeNull();

    fireEvent.keyDown(document.body, { key: "c" });
    fireEvent.click(await screen.findByRole("menuitem", { name: "Remote server by URL" }));
    const dialog = await screen.findByRole("dialog", { name: "Add MCP server" });
    // Typing "c" in the dialog's fields types; it never reopens or stacks a dialog.
    const name = within(dialog).getByLabelText("Name");
    fireEvent.keyDown(name, { key: "c" });
    fireEvent.keyDown(document.body, { key: "c" });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mcpServers.create).not.toHaveBeenCalled();
  });

  it("shows an empty state whose action opens Add server", async () => {
    vi.mocked(mcpServers.list).mockResolvedValue([]);
    render(<McpServersPage />);
    const list = screen.getByTestId("mcp-server-list");
    await waitFor(() => expect(list).toHaveAttribute("data-state", "empty"));
    expect(within(list).getByText("No servers yet. Install one from the Catalog or register a remote server.")).toBeInTheDocument();
    expect(screen.queryByTestId("mcp-server-row")).toBeNull();
    fireEvent.click(within(list).getByRole("link", { name: "Install from the Catalog" }));
    expect(navigations).toContain(routes.mcpBrowse());
    fireEvent.click(within(list).getByRole("button", { name: "Register a remote server" }));
    expect(await screen.findByRole("dialog", { name: "Add MCP server" })).toBeInTheDocument();
  });

  it("Add server is a menu: From the catalog goes to the Catalog tab, Remote server by URL opens the dialog", async () => {
    render(<McpServersPage />);
    await screen.findByTestId("mcp-server-row");
    await pickAddItem("From the catalog");
    expect(push).toHaveBeenCalledWith(routes.mcpBrowse());
    expect(screen.queryByRole("dialog")).toBeNull();
    await pickAddItem("Remote server by URL");
    expect(await screen.findByRole("dialog", { name: "Add MCP server" })).toBeInTheDocument();
  });

  it("shows a load error with Retry, and the rows once Retry succeeds", async () => {
    vi.mocked(mcpServers.list).mockRejectedValueOnce(new ApiError(503, "database unavailable"));
    render(<McpServersPage />);
    const error = await screen.findByTestId("datalist-error");
    expect(error).toHaveAttribute("role", "alert");
    expect(error).toHaveTextContent("database unavailable");
    expect(screen.getByTestId("mcp-server-list")).toHaveAttribute("data-state", "error");

    fireEvent.click(within(error).getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("mcp-server-row")).toBeInTheDocument();
    expect(screen.queryByTestId("datalist-error")).toBeNull();
    expect(mcpServers.list).toHaveBeenCalledTimes(2);
  });

  it.each(["light", "dark"])("has no axe violations in the %s theme (list, Add dialog, detail, tool sheet, not found)", async (theme) => {
    document.documentElement.classList.toggle("dark", theme === "dark");
    vi.mocked(mcpServers.test).mockResolvedValue(TOOLS);
    const list = render(<McpServersPage />);
    await screen.findByTestId("mcp-used-by-count");
    await expectNoAxeViolations(list.container);
    await pickAddItem("Remote server by URL");
    await expectNoAxeViolations(await screen.findByRole("dialog", { name: "Add MCP server" }));
    list.unmount();
    cleanup();

    const detail = render(<McpServerDetailPage />);
    await screen.findAllByTestId("mcp-used-by-agent");
    fireEvent.click(within(connection()).getByRole("button", { name: "Test connection" }));
    await screen.findAllByTestId("mcp-tool");
    await expectNoAxeViolations(detail.container);
    fireEvent.click(screen.getByRole("button", { name: "search" }));
    await expectNoAxeViolations(await screen.findByTestId("mcp-tool-sheet"));
    detail.unmount();
    cleanup();

    vi.mocked(mcpServers.get).mockRejectedValue(new ApiError(404, "MCP server not found"));
    const missing = render(<McpServerDetailPage />);
    await screen.findByTestId("mcp-server-not-found");
    await expectNoAxeViolations(missing.container);
  }, AXE_TIMEOUT_MS);

  it("fits a phone: no fixed widths, secondary columns hidden below md, long URLs truncate or wrap", async () => {
    const width = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { value: 375, configurable: true, writable: true });
    try {
      const list = render(<McpServersPage />);
      await screen.findByTestId("mcp-server-row");
      // Transport is the column that hides; its header and cell carry the same breakpoint.
      const transportCell = within(row()).getByText("Streamable HTTP").closest("td")!;
      expect(transportCell.className.split(/\s+/)).toEqual(expect.arrayContaining(["hidden", "md:table-cell"]));
      expect(screen.getByRole("columnheader", { name: "Transport" }).className).toContain("hidden");
      // The URL truncates inside its cell instead of widening the table.
      const url = within(row()).getByText("https://mcp.example.com/mcp");
      expect(url.className.split(/\s+/)).toContain("truncate");
      expect(url).toHaveAttribute("title", "https://mcp.example.com/mcp");
      // The shortcut hint is hidden on small screens; the button label stays.
      expect(within(screen.getByTestId("mcp-add-server")).getByText("C").className).toContain("hidden");
      expect(list.container.innerHTML).not.toMatch(/\b(min-)?w-\[\d{3,}px\]/);
      list.unmount();

      const detail = render(<McpServerDetailPage />);
      await screen.findByTestId("mcp-connection");
      const dl = connection().querySelector("dl")!;
      expect(dl.className.split(/\s+/)).toEqual(expect.arrayContaining(["grid-cols-1", "sm:grid-cols-[8rem_1fr]"]));
      expect(within(connection()).getByText("https://mcp.example.com/mcp").className).toContain("break-all");
      expect(detail.container.innerHTML).not.toMatch(/\b(min-)?w-\[\d{3,}px\]/);
    } finally {
      Object.defineProperty(window, "innerWidth", { value: width, configurable: true, writable: true });
    }
  });
});

describe("MCP detail page", () => {
  it("journey: test the connection, list the tools, open one in the Sheet, Escape closes it and focus returns", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue(TOOLS);
    vi.mocked(mcpServers.get).mockResolvedValueOnce(server()).mockResolvedValue(server({ last_status: "ok", last_checked_at: "2026-11-14T10:00:00Z" }));
    render(<McpServerDetailPage />);
    expect(await screen.findByRole("heading", { level: 1, name: "docs" })).toBeInTheDocument();
    expect(screen.getByTestId("mcp-server-switcher")).toBeInTheDocument();
    expect(within(connection()).getByTestId("mcp-status-dot")).toHaveAttribute("data-status", "untested");
    expect(screen.queryByTestId("mcp-tool")).toBeNull();

    fireEvent.click(within(connection()).getByRole("button", { name: "Test connection" }));
    const rows = await screen.findAllByTestId("mcp-tool");
    expect(rows.map((r) => r.getAttribute("data-row-id")).sort()).toEqual(["fetch_page", "search"]);
    expect(within(connection()).getByTestId("mcp-test-tools")).toHaveTextContent("Connected. 2 tools discovered.");
    // The card shows the summary only; the tools are listed once, in Available tools.
    expect(within(connection()).queryByTestId("mcp-tool")).toBeNull();
    await waitFor(() => expect(within(connection()).getByTestId("mcp-status-dot")).toHaveAttribute("data-status", "ok"));

    const opener = within(screen.getByTestId("mcp-tools-list")).getByRole("button", { name: "search" });
    opener.focus();
    fireEvent.click(opener);
    const sheet = await screen.findByTestId("mcp-tool-sheet");
    expect(sheet).toHaveTextContent("Tool on docs");
    expect(within(sheet).getByText("Search the docs")).toBeInTheDocument();
    const schema = JSON.parse(within(sheet).getByTestId("mcp-tool-schema").textContent ?? "{}");
    expect(schema).toEqual(TOOLS.tools[0].input_schema);

    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("mcp-tool-sheet")).toBeNull());
    await waitFor(() => expect(within(screen.getByTestId("mcp-tools-list")).getByRole("button", { name: "search" })).toHaveFocus());

    // A tool without a description says so in the Sheet.
    fireEvent.click(within(screen.getByTestId("mcp-tools-list")).getByRole("button", { name: "fetch_page" }));
    expect(within(await screen.findByTestId("mcp-tool-sheet")).getByText("No description.")).toBeInTheDocument();
  });

  it("lists the agents using it with draft / vN labels and editor links; older versions do not count", async () => {
    render(<McpServerDetailPage />);
    const items = await screen.findAllByTestId("mcp-used-by-agent");
    const summary = items.map((item) => [within(item).getByRole("link").textContent, item.textContent?.replace(within(item).getByRole("link").textContent ?? "", "")]);
    expect(summary).toEqual([["Both", "draft, v3"], ["Helper", "v2"], ["Writer", "draft"]]);
    expect(within(items[0]).getByRole("link", { name: "Both" })).toHaveAttribute("href", routes.agentEditor("a3"));
    expect(within(items[1]).getByRole("link", { name: "Helper" })).toHaveAttribute("href", routes.agentEditor("a2"));
    expect(within(items[2]).getByRole("link", { name: "Writer" })).toHaveAttribute("href", routes.agentEditor("a1"));
    expect(screen.getByTestId("mcp-used-by")).not.toHaveTextContent("Old");
    expect(screen.getByTestId("mcp-used-by")).not.toHaveTextContent("Some published versions could not be read");
  });

  async function openDelete() {
    fireEvent.click(await screen.findByTestId("mcp-delete"));
    return screen.findByRole("dialog", { name: "Delete docs?" });
  }

  it("delete with complete references: lists every referencing agent, then deletes and returns to the list", async () => {
    vi.mocked(mcpServers.remove).mockResolvedValue(undefined);
    render(<McpServerDetailPage />);
    await screen.findAllByTestId("mcp-used-by-agent");
    const dialog = await openDelete();
    const refs = within(dialog).getByTestId("mcp-delete-references");
    await waitFor(() => expect(refs).toHaveAttribute("data-status", "ready"));
    expect(refs).toHaveTextContent("These 3 agents use its tools and will fail to publish or run until they are changed");
    const items = within(refs).getAllByTestId("mcp-delete-reference");
    expect(items.map((item) => item.textContent)).toEqual(["Bothdraft, v3", "Helperv2", "Writerdraft"]);
    expect(within(items[1]).getByRole("link", { name: "Helper" })).toHaveAttribute("href", routes.agentEditor("a2"));
    // Opening the confirm always re-checks the references, even when the page's result is fresh.
    expect(api.agents.list).toHaveBeenCalledTimes(2);
    expect(mcpServers.remove).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Delete server" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(routes.mcp()));
    expect(mcpServers.remove).toHaveBeenCalledWith("srv-1");
  });

  it("delete with a partial check: unreadable versions are flagged, drafts still listed", async () => {
    vi.mocked(api.versions.get).mockImplementation(async (id: string, n: number) => {
      if (id === "a2") throw new ApiError(500, "boom");
      return findVersion(id, n);
    });
    render(<McpServerDetailPage />);
    await screen.findAllByTestId("mcp-used-by-agent");
    expect(screen.getByTestId("mcp-used-by")).toHaveTextContent("Some published versions could not be read; only their drafts were checked.");
    expect(screen.getAllByTestId("mcp-used-by-agent").map((item) => within(item).getByRole("link").textContent)).toEqual(["Both", "Writer"]);

    const dialog = await openDelete();
    const refs = within(dialog).getByTestId("mcp-delete-references");
    await waitFor(() => expect(refs).toHaveAttribute("data-status", "ready"));
    expect(within(refs).getAllByTestId("mcp-delete-reference").map((item) => item.textContent)).toEqual(["Bothdraft, v3", "Writerdraft"]);
    // Found references still carry the caveat that the check was partial.
    expect(within(refs).getByTestId("mcp-delete-references-partial")).toHaveTextContent("Some published versions could not be read; other agents may also use it.");
  });

  it("delete with a partial check and no readable reference says only drafts and readable versions were checked", async () => {
    vi.mocked(api.agents.list).mockResolvedValue([agent("a2", "Helper", [], 2)]);
    vi.mocked(api.versions.get).mockRejectedValue(new TypeError("Failed to fetch"));
    render(<McpServerDetailPage />);
    const dialog = await openDelete();
    const refs = within(dialog).getByTestId("mcp-delete-references");
    await waitFor(() => expect(refs).toHaveTextContent("No agent draft or readable published version uses its tools."));
    expect(within(refs).queryByTestId("mcp-delete-reference")).toBeNull();
  });

  it("delete when the reference check fails: warns, and Delete still works", async () => {
    vi.mocked(api.agents.list).mockRejectedValue(new ApiError(500, "boom"));
    vi.mocked(mcpServers.remove).mockResolvedValue(undefined);
    render(<McpServerDetailPage />);
    await within(await screen.findByTestId("mcp-used-by")).findByText("Could not load agents.");
    const dialog = await openDelete();
    const refs = within(dialog).getByTestId("mcp-delete-references");
    await waitFor(() => expect(refs).toHaveAttribute("data-status", "error"));
    expect(refs).toHaveTextContent("Could not check which agents use it. Agents that use its tools will fail to publish or run until they are changed.");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete server" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(routes.mcp()));
  });

  it("an unknown id is not found, with a link back and no server UI", async () => {
    params.id = "nope";
    vi.mocked(mcpServers.get).mockRejectedValue(new ApiError(404, "MCP server not found"));
    render(<McpServerDetailPage />);
    const missing = await screen.findByTestId("mcp-server-not-found");
    expect(missing).toHaveTextContent("MCP server not found");
    expect(mcpServers.get).toHaveBeenCalledWith("nope");
    expect(within(missing).getByRole("link", { name: "Back to MCP servers" })).toHaveAttribute("href", routes.mcp());
    expect(screen.queryByTestId("mcp-connection")).toBeNull();
    expect(screen.queryByTestId("mcp-delete")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a server deleted elsewhere turns a later test or delete into the not-found state", async () => {
    vi.mocked(mcpServers.test).mockRejectedValue(new ApiError(404, "MCP server not found"));
    const view = render(<McpServerDetailPage />);
    fireEvent.click(within(await screen.findByTestId("mcp-connection")).getByRole("button", { name: "Test connection" }));
    expect(await screen.findByTestId("mcp-server-not-found")).toBeInTheDocument();
    view.unmount();

    vi.mocked(mcpServers.remove).mockRejectedValue(new ApiError(404, "MCP server not found"));
    render(<McpServerDetailPage />);
    const dialog = await openDelete();
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete server" }));
    expect(await screen.findByTestId("mcp-server-not-found")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(push).not.toHaveBeenCalled();
  });

  it.each([
    ["unauthorized", "The server refused the credentials (401/403). Check the header secrets."],
    ["redirect_refused", "The server answered with a redirect, which is not followed."],
    ["protocol_error", "The server did not speak the MCP protocol as expected."],
  ])("explains a %s test failure in words, with its code, in the card and the last test", async (code, words) => {
    vi.mocked(mcpServers.test).mockResolvedValue({ status: "error", error_code: code, tools: [] });
    vi.mocked(mcpServers.get).mockResolvedValueOnce(server()).mockResolvedValue(server({ last_status: "error", last_error_code: code, last_checked_at: "2026-11-14T10:00:00Z" }));
    render(<McpServerDetailPage />);
    fireEvent.click(within(await screen.findByTestId("mcp-connection")).getByRole("button", { name: "Test connection" }));
    const error = await within(connection()).findByTestId("mcp-test-error");
    expect(error).toHaveAttribute("role", "alert");
    expect(error).toHaveTextContent("Connection failed");
    expect(error).not.toHaveTextContent("outbound policy");
    expect(error).toHaveTextContent(words);
    expect(within(error).getByText(code).tagName).toBe("CODE");
    await waitFor(() => expect(within(connection()).getByTestId("mcp-status-dot")).toHaveAttribute("data-status", "error"));
    expect(within(connection()).getByTestId("mcp-last-test")).toHaveTextContent(words);
    expect(within(screen.getByTestId("mcp-available-tools")).getByText("No tools listed")).toBeInTheDocument();
    expect(screen.queryByTestId("mcp-tool")).toBeNull();
  });

  it("never puts a header VALUE in the DOM: names with (set) only, on every surface", async () => {
    // Even if a response carried values, the UI renders `header_names` only.
    const leaky = { ...server({ last_status: "ok" }), headers: { authorization: SECRET, "x-api-key": SECRET } } as McpServer;
    vi.mocked(mcpServers.list).mockResolvedValue([leaky]);
    vi.mocked(mcpServers.get).mockResolvedValue(leaky);
    vi.mocked(mcpServers.test).mockResolvedValue(TOOLS);
    const noSecret = () => {
      expect(document.body.innerHTML).not.toContain(SECRET);
      expect(document.body.innerHTML).not.toContain("sk-live");
      document.querySelectorAll("input, textarea").forEach((field) => expect((field as HTMLInputElement).value).not.toContain("sk-live"));
    };

    const list = render(<McpServersPage />);
    await screen.findByTestId("mcp-server-row");
    noSecret();
    fireEvent.click(within(row()).getByRole("button", { name: "Edit" }));
    const edit = await screen.findByRole("dialog", { name: "Edit docs" });
    expect(within(edit).getAllByTestId("mcp-header-set")).toHaveLength(2);
    noSecret();
    list.unmount();
    cleanup();

    render(<McpServerDetailPage />);
    expect(await screen.findByTestId("mcp-header-names")).toHaveTextContent("authorization (set), x-api-key (set)");
    noSecret();
    fireEvent.click(within(connection()).getByRole("button", { name: "Test connection" }));
    fireEvent.click(within(await screen.findByTestId("mcp-tools-list")).getByRole("button", { name: "search" }));
    await screen.findByTestId("mcp-tool-sheet");
    noSecret();
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("mcp-tool-sheet")).toBeNull());
    fireEvent.click(within(connection()).getByRole("button", { name: "Edit" }));
    await screen.findByRole("dialog", { name: "Edit docs" });
    noSecret();
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await openDelete();
    noSecret();
  });
});
