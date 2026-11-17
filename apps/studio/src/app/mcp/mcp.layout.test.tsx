import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import McpServersPage from "./page";
import { api, type Agent } from "@/lib/api";
import { mcpServers, type McpServer } from "@/lib/mcpServers";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { ShortcutsProvider } from "@/components/shortcuts/ShortcutsProvider";

// `c` is bound by the global provider, which reads the router.
vi.mock("next/navigation", () => ({
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/mcp",
  useSearchParams: () => new URLSearchParams(),
}));

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
  header_names: [],
  created_at: null,
  updated_at: null,
  last_checked_at: "2026-11-13T10:00:00Z",
  last_status: "ok",
  last_error_code: null,
};

const agents: Agent[] = [
  { id: "a1", name: "Writer", latest_version: null, draft_spec: { spec_version: 1, name: "w", instructions: "", model: { provider: "p", name: "m" }, tools: [], mcp_tools: [{ server: "docs", tool: "search" }] } },
];

// "Add server" is a menu now: open it, then pick an item.
async function pickAddItem(name: string) {
  fireEvent.keyDown(screen.getByTestId("mcp-add-server"), { key: "Enter" });
  fireEvent.click(await screen.findByRole("menuitem", { name }));
}

describe("MCP servers list (layout)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(mcpServers.list).mockResolvedValue([server]);
    vi.mocked(mcpServers.get).mockResolvedValue(server);
    vi.mocked(api.agents.list).mockResolvedValue(agents);
    vi.mocked(api.versions.get).mockRejectedValue(new Error("no version"));
  });

  it("renders a DataList whose row links to the detail page, with Used by computed from agents", async () => {
    render(<McpServersPage />);
    const row = await screen.findByTestId("mcp-server-row");
    expect(within(row).getByRole("link", { name: "docs, enabled" })).toHaveAttribute("href", routes.mcpServer("srv-1"));
    expect(await within(row).findByTestId("mcp-used-by-count")).toHaveTextContent("1 agent");
    expect(within(row).getByTestId("mcp-last-test")).toHaveTextContent("Connected");
  });

  it("opens Add server with the button and with `c`, but not while typing", async () => {
    render(<ShortcutsProvider><McpServersPage /></ShortcutsProvider>);
    await screen.findByTestId("mcp-server-row");
    fireEvent.keyDown(screen.getByRole("searchbox"), { key: "c" });
    expect(screen.queryByRole("dialog")).toBeNull();
    fireEvent.keyDown(document.body, { key: "c" });
    expect(await screen.findByRole("menuitem", { name: "From the catalog" })).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("menuitem", { name: "Remote server by URL" }));
    expect(await screen.findByRole("dialog", { name: "Add MCP server" })).toBeInTheDocument();
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await pickAddItem("Remote server by URL");
    expect(await screen.findByRole("form", { name: "Register MCP server" })).toBeInTheDocument();
  });

  it("a row action does not follow the row link; the delete confirm lists references", async () => {
    render(<McpServersPage />);
    const row = await screen.findByTestId("mcp-server-row");
    await within(row).findByTestId("mcp-used-by-count");
    fireEvent.click(within(row).getByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete docs?" });
    await waitFor(() => expect(within(dialog).getAllByTestId("mcp-delete-reference")).toHaveLength(1));
    expect(within(dialog).getByTestId("mcp-delete-reference")).toHaveTextContent("Writerdraft");
  });

  it("shows the tool count after a test from the row", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue({ status: "ok", tools: [{ name: "a", description: null, input_schema: {} }, { name: "b", description: null, input_schema: {} }] });
    render(<McpServersPage />);
    const row = await screen.findByTestId("mcp-server-row");
    fireEvent.click(within(row).getByRole("button", { name: "Test connection" }));
    expect(await within(row).findByTestId("mcp-tool-count")).toHaveTextContent("2");
  });

  it("names the row link with its enabled state", async () => {
    render(<McpServersPage />);
    const row = await screen.findByTestId("mcp-server-row");
    expect(within(row).getByRole("link", { name: "docs, enabled" })).toBeInTheDocument();
  });

  it("marks Used by as a lower bound (N+) when some versions could not be read", async () => {
    vi.mocked(api.agents.list).mockResolvedValue([...agents, { ...agents[0], id: "a2", name: "Pub", latest_version: 1, draft_spec: { ...agents[0].draft_spec, mcp_tools: undefined } }]);
    vi.mocked(api.versions.get).mockRejectedValue(new Error("boom"));
    render(<McpServersPage />);
    const count = await screen.findByTestId("mcp-used-by-count");
    expect(count).toHaveTextContent("1+ agents");
    expect(count.getAttribute("title")).toContain("other agents may also use it");
  });

  it("has no axe violations", async () => {
    const { container } = render(<McpServersPage />);
    await screen.findByTestId("mcp-used-by-count");
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
