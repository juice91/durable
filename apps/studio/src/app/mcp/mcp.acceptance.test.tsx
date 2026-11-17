import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import McpServersPage from "./page";
import McpServerDetailPage from "./[id]/page";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { api, ApiError } from "@/lib/api";
import { mcpServers, type McpServer } from "@/lib/mcpServers";
import { toolRunLedger } from "@/lib/toolRunLedger";

// SL.8: the connection test, the tool list and the header names live on /mcp/[id].
const params = { id: "srv-1" };
vi.mock("next/navigation", () => ({
  useParams: () => params,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
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
    mcpServers: {
      list: vi.fn(),
      get: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      setEnabled: vi.fn(),
      remove: vi.fn(),
      test: vi.fn(),
    },
  };
});

const SECRET = "s3cr3t-header-value";

function server(overrides: Partial<McpServer> = {}): McpServer {
  return {
    id: "srv-1",
    name: "docs",
    url: "https://mcp.example.com/mcp",
    transport: "streamable_http",
    enabled: true,
    header_names: ["authorization"],
    created_at: null,
    updated_at: null,
    last_checked_at: "2026-11-13T10:00:00Z",
    last_status: "ok",
    last_error_code: null,
    ...overrides,
  };
}

// "Add server" is a menu now: open it, then pick an item.
async function pickAddItem(name: string) {
  fireEvent.keyDown(screen.getByTestId("mcp-add-server"), { key: "Enter" });
  fireEvent.click(await screen.findByRole("menuitem", { name }));
}

describe("MCP servers acceptance", () => {
  beforeEach(() => {
    toolRunLedger.reset();
    vi.mocked(mcpServers.list).mockReset();
    vi.mocked(mcpServers.get).mockReset();
    vi.mocked(mcpServers.create).mockReset();
    vi.mocked(mcpServers.update).mockReset();
    vi.mocked(mcpServers.setEnabled).mockReset();
    vi.mocked(mcpServers.remove).mockReset();
    vi.mocked(mcpServers.test).mockReset();
    vi.mocked(mcpServers.list).mockResolvedValue([server()]);
    vi.mocked(mcpServers.get).mockResolvedValue(server());
    vi.mocked(api.agents.list).mockResolvedValue([]);
    vi.mocked(api.versions.get).mockRejectedValue(new Error("no version"));
    params.id = "srv-1";
  });

  afterEach(() => {
    document.querySelectorAll("[data-preinert], [data-late]").forEach((node) => node.remove());
  });

  it("lists rows with header names only, never secret values", async () => {
    render(<McpServersPage />);
    const row = await screen.findByTestId("mcp-server-row");
    expect(row).toHaveAttribute("data-row-id", "srv-1");
    expect(within(row).getByText("docs")).toBeInTheDocument();
    expect(within(row).getByText("https://mcp.example.com/mcp")).toBeInTheDocument();
    expect(within(row).getByText("Enabled")).toBeInTheDocument();
    expect(within(row).getByTestId("mcp-last-test")).toHaveTextContent("Connected");
    expect(row.querySelector("input")).toBeNull();
    expect(document.body.textContent).not.toContain(SECRET);
    // Header names moved to the detail's Connection card: names only, never values.
    cleanup();
    render(<McpServerDetailPage />);
    const names = await screen.findByTestId("mcp-header-names");
    expect(names).toHaveTextContent("authorization (set)");
    expect(screen.getByTestId("mcp-connection").querySelector("input")).toBeNull();
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("registers a server with header values, then resets the form so the secret is gone", async () => {
    vi.mocked(mcpServers.list).mockResolvedValue([]);
    vi.mocked(mcpServers.create).mockImplementation(async (body) => server({
      id: "srv-new",
      name: body.name,
      url: body.url,
      enabled: body.enabled ?? true,
      header_names: Object.keys(body.headers ?? {}),
      last_status: null,
      last_checked_at: null,
    }));
    render(<McpServersPage />);
    await screen.findByTestId("mcp-add-server");
    await pickAddItem("Remote server by URL");
    expect(await screen.findByRole("dialog", { name: "Add MCP server" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Register MCP server" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "west" } });
    fireEvent.change(screen.getByLabelText("URL"), { target: { value: "https://mcp.example.com/mcp" } });
    fireEvent.click(screen.getByRole("button", { name: "Add header" }));
    fireEvent.change(screen.getByLabelText("Header 1 name"), { target: { value: "Authorization" } });
    const value = screen.getByLabelText("Header 1 value");
    expect(value).toHaveAttribute("type", "password");
    expect(value).toHaveAttribute("autocomplete", "off");
    fireEvent.change(value, { target: { value: SECRET } });
    fireEvent.click(screen.getByRole("button", { name: "Register" }));

    await waitFor(() => expect(mcpServers.create).toHaveBeenCalledWith({
      name: "west",
      url: "https://mcp.example.com/mcp",
      enabled: true,
      headers: { Authorization: SECRET },
    }));
    await waitFor(() => expect(screen.queryByTestId("mcp-server-form")).not.toBeInTheDocument());
    expect(screen.queryByRole("dialog")).toBeNull();
    const row = await screen.findByTestId("mcp-server-row");
    expect(row).toHaveAttribute("data-row-id", "srv-new");
    expect(within(row).getByText("west")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(SECRET);
    expect(screen.queryByDisplayValue(SECRET)).not.toBeInTheDocument();
    // The stored header shows by name on the new server's detail page.
    const created = vi.mocked(mcpServers.create).mock.results[0].value as Promise<McpServer>;
    vi.mocked(mcpServers.get).mockResolvedValue(await created);
    cleanup();
    params.id = "srv-new";
    render(<McpServerDetailPage />);
    expect(await screen.findByTestId("mcp-header-names")).toHaveTextContent("Authorization (set)");
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("edits without sending a header value unless Replace was used, and refuses a partial change", async () => {
    vi.mocked(mcpServers.update).mockImplementation(async (_id, body) => server({ ...body, header_names: body.headers ? Object.keys(body.headers) : ["authorization"] }));
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    expect(screen.queryByLabelText("New value for authorization")).not.toBeInTheDocument();
    expect(screen.getByTestId("mcp-header-set")).toHaveTextContent("•••• set");
    expect(document.body.textContent).not.toContain(SECRET);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "docs2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mcpServers.update).toHaveBeenCalledWith("srv-1", { name: "docs2" }));
    expect(vi.mocked(mcpServers.update).mock.calls[0][1]).not.toHaveProperty("headers");

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    expect(screen.getByRole("button", { name: "Replace authorization" })).toHaveTextContent("Replace");
    expect(screen.getByRole("button", { name: "Remove authorization" })).toHaveTextContent("Remove");
    fireEvent.click(screen.getByRole("button", { name: "Replace authorization" }));
    const replacement = screen.getByLabelText("New value for authorization");
    expect(replacement).toHaveValue("");
    expect(replacement).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "Undo changes to authorization" })).toBeInTheDocument();
    fireEvent.change(replacement, { target: { value: "Bearer new" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mcpServers.update).toHaveBeenCalledWith("srv-1", { headers: { authorization: "Bearer new" } }));
    expect(mcpServers.update).toHaveBeenCalledTimes(2);

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Add header" }));
    expect(screen.getByRole("button", { name: "Remove header 2" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Header 2 name"), { target: { value: "X-Team" } });
    fireEvent.change(screen.getByLabelText("Header 2 value"), { target: { value: "blue" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(await screen.findByTestId("mcp-form-error")).toHaveTextContent("Replace authorization");
    expect(mcpServers.update).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove authorization" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mcpServers.update).toHaveBeenCalledWith("srv-1", { headers: {} }));
  });

  it("enables and disables through setEnabled", async () => {
    vi.mocked(mcpServers.setEnabled)
      .mockResolvedValueOnce(server({ enabled: false }))
      .mockResolvedValueOnce(server({ enabled: true }));
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Disable" }));
    await waitFor(() => expect(mcpServers.setEnabled).toHaveBeenCalledWith("srv-1", false));
    expect(await screen.findByText("Disabled")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Enable" }));
    await waitFor(() => expect(mcpServers.setEnabled).toHaveBeenCalledWith("srv-1", true));
    expect(await screen.findByText("Enabled")).toBeInTheDocument();
  });

  it("shows discovered tools or a clear outbound-policy error", async () => {
    vi.mocked(mcpServers.test).mockResolvedValueOnce({
      status: "ok",
      tools: [{ name: "search", description: "Search the docs", input_schema: { type: "object", properties: { q: { type: "string" } } } }],
    });
    render(<McpServerDetailPage />);
    const connection = await screen.findByTestId("mcp-connection");
    const testButton = () => within(connection).getByRole("button", { name: "Test connection" });
    fireEvent.click(testButton());
    // The result summary stays in the Connection card; the tools are the Available tools list.
    expect(await within(connection).findByTestId("mcp-test-tools")).toHaveTextContent("1 tool discovered");
    const tool = within(screen.getByTestId("mcp-tools-list")).getByTestId("mcp-tool");
    expect(within(tool).getByText("search")).toBeInTheDocument();
    expect(within(tool).getByText("Search the docs")).toBeInTheDocument();
    fireEvent.click(within(tool).getByRole("button", { name: "search" }));
    expect(within(await screen.findByTestId("mcp-tool-sheet")).getByText("Input schema")).toBeInTheDocument();
    fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("mcp-tool-sheet")).toBeNull());

    vi.mocked(mcpServers.test).mockResolvedValueOnce({ status: "error", error_code: "address_blocked", tools: [] });
    await waitFor(() => expect(testButton()).toBeEnabled());
    fireEvent.click(testButton());
    const blocked = await screen.findByTestId("mcp-test-error");
    expect(blocked).toHaveTextContent("Blocked by the outbound policy");
    expect(blocked).toHaveTextContent("address_blocked");
    expect(screen.queryByTestId("mcp-tool")).toBeNull();

    vi.mocked(mcpServers.test).mockResolvedValueOnce({ status: "error", error_code: "connect_failed", tools: [] });
    await waitFor(() => expect(testButton()).toBeEnabled());
    fireEvent.click(testButton());
    await waitFor(() => expect(screen.getByTestId("mcp-test-error")).toHaveTextContent("connect_failed"));
    const failed = screen.getByTestId("mcp-test-error");
    expect(failed).toHaveTextContent("Connection failed");
    expect(failed).not.toHaveTextContent("Blocked by the outbound policy");
  });

  it("confirms delete in a portaled dialog that inerts the page, including children added later", async () => {
    const preInert = document.createElement("div");
    preInert.dataset.preinert = "1";
    preInert.setAttribute("inert", "");
    document.body.appendChild(preInert);
    const view = render(<McpServersPage />);
    const deleteButton = await screen.findByRole("button", { name: "Delete" });
    deleteButton.focus();
    fireEvent.click(deleteButton);

    const dialog = await screen.findByRole("dialog", { name: "Delete docs?" });
    expect(dialog.closest("[data-confirm-dialog-root]")?.parentElement).toBe(document.body);
    expect(within(dialog).getByRole("button", { name: "Delete server" })).toBeInTheDocument();
    expect(mcpServers.remove).not.toHaveBeenCalled();
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus());
    expect(view.container).toHaveAttribute("inert");
    expect(dialog.closest("[inert]")).toBeNull();

    const late = document.createElement("button");
    late.dataset.late = "1";
    late.textContent = "Toast";
    await act(async () => {
      document.body.appendChild(late);
      await Promise.resolve();
    });
    expect(late).toHaveAttribute("inert");

    const stacked = render(<ConfirmDialog title="Second check" confirmLabel="OK" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(dialog.closest("[data-confirm-dialog-root]")).toHaveAttribute("inert");
    expect(screen.getByRole("dialog", { name: "Second check" }).closest("[data-confirm-dialog-root]")).not.toHaveAttribute("inert");
    stacked.unmount();
    expect(view.container).toHaveAttribute("inert");
    expect(dialog.closest("[inert]")).toBeNull();

    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(view.container).not.toHaveAttribute("inert");
    expect(late).not.toHaveAttribute("inert");
    expect(preInert).toHaveAttribute("inert");
    expect(deleteButton).toHaveFocus();
    expect(mcpServers.remove).not.toHaveBeenCalled();
  });

  it("keeps a 500 or network error inside the open dialog", async () => {
    vi.mocked(mcpServers.remove).mockRejectedValueOnce(new ApiError(500, "database unavailable"));
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete server" }));
    const alert = await screen.findByTestId("confirm-dialog-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("database unavailable");
    expect(dialog).toContainElement(alert);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByTestId("mcp-action-error")).not.toBeInTheDocument();

    vi.mocked(mcpServers.remove).mockRejectedValueOnce(new TypeError("Failed to fetch"));
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete server" }));
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent("Failed to fetch");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByTestId("mcp-server-row")).toBeInTheDocument();
  });

  it("on a 404 closes the dialog, announces the deletion and reloads the list", async () => {
    vi.mocked(mcpServers.remove).mockRejectedValue(new ApiError(404, "MCP server not found"));
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    vi.mocked(mcpServers.list).mockResolvedValue([]);
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete server" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const notice = screen.getByTestId("mcp-notice");
    expect(notice).toHaveAttribute("role", "status");
    expect(notice).toHaveTextContent("docs was already deleted.");
    await waitFor(() => expect(mcpServers.list).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("mcp-server-row")).not.toBeInTheDocument();
  });

  it("while delete is in flight, Cancel is disabled, Escape is ignored, and focus stays on the dialog", async () => {
    let finish: () => void = () => undefined;
    vi.mocked(mcpServers.remove).mockImplementation(() => new Promise<void>((resolve) => { finish = resolve; }));
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete server" }));

    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled());
    expect(dialog).toHaveAttribute("aria-busy", "true");
    expect(dialog).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(mcpServers.remove).toHaveBeenCalledTimes(1);

    await act(async () => finish());
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("an unchanged Save after Disable leaves the row Disabled and sends no update", async () => {
    vi.mocked(mcpServers.setEnabled).mockResolvedValue(server({ enabled: false }));
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const editDialog = await screen.findByRole("dialog", { name: "Edit docs" });
    expect(within(editDialog).getByTestId("mcp-server-form")).toBeInTheDocument();
    // The modal hides the row actions from a user; the Disable is dispatched directly so the
    // onSaved guard (an unchanged save keeps the current row) stays covered.
    expect(screen.queryByRole("button", { name: "Disable" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Disable", hidden: true }));
    await waitFor(() => expect(within(screen.getByTestId("mcp-server-row")).getByText("Disabled")).toBeInTheDocument());

    fireEvent.click(within(editDialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByTestId("mcp-server-form")).not.toBeInTheDocument());
    expect(mcpServers.update).not.toHaveBeenCalled();
    expect(within(screen.getByTestId("mcp-server-row")).getByText("Disabled")).toBeInTheDocument();
    expect(mcpServers.setEnabled).toHaveBeenCalledWith("srv-1", false);
  });

  it("closes the edit form on delete and does not re-add the server when the save returns", async () => {
    let resolveUpdate: (value: McpServer) => void = () => undefined;
    vi.mocked(mcpServers.update).mockImplementation(() => new Promise((resolve) => { resolveUpdate = resolve; }));
    vi.mocked(mcpServers.remove).mockResolvedValue(undefined);
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "docs2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mcpServers.update).toHaveBeenCalledWith("srv-1", { name: "docs2" }));

    // Unreachable behind the modal for a user; dispatched directly to cover the forgetServer and
    // onSaved guards (a late save never re-adds a deleted row).
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Delete", hidden: true }));
    const confirm = await screen.findByRole("dialog", { name: "Delete docs?", hidden: true });
    fireEvent.click(within(confirm).getByRole("button", { name: "Delete server", hidden: true }));
    await waitFor(() => expect(screen.queryByTestId("mcp-server-form")).not.toBeInTheDocument());
    expect(screen.queryByTestId("mcp-server-row")).not.toBeInTheDocument();

    await act(async () => {
      resolveUpdate(server({ name: "docs2" }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.queryByTestId("mcp-server-row")).not.toBeInTheDocument();
    expect(screen.queryByText("docs2")).not.toBeInTheDocument();
    expect(screen.queryByTestId("mcp-server-form")).not.toBeInTheDocument();
  });
});
