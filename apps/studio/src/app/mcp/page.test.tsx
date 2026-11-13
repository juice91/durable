import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import McpServersPage from "./page";
import McpServerDetailPage from "./[id]/page";
import { mcpServers, type McpServer } from "@/lib/mcpServers";
import { api, ApiError } from "@/lib/api";
import { routes } from "@/lib/routes";

// SL.8 moved the connection test, the tool list and the header names to /mcp/[id].
const push = vi.fn();
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "srv-1" }),
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

const server: McpServer = {
  id: "srv-1",
  name: "docs",
  url: "https://mcp.example.com/mcp",
  transport: "streamable_http",
  enabled: true,
  header_names: ["authorization"],
  created_at: null,
  updated_at: null,
  last_checked_at: "2026-11-13T10:00:00Z",
  last_status: "error",
  last_error_code: "address_blocked",
};

describe("McpServersPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(mcpServers.list).mockResolvedValue([server]);
    vi.mocked(mcpServers.get).mockResolvedValue(server);
    vi.mocked(api.agents.list).mockResolvedValue([]);
    vi.mocked(api.versions.get).mockRejectedValue(new Error("no version"));
  });

  it("lists servers with URL, enabled state, set headers and the last test result", async () => {
    render(<McpServersPage />);
    const row = await screen.findByTestId("mcp-server-row");
    expect(row).toHaveAttribute("data-row-id", "srv-1");
    expect(within(row).getByRole("link", { name: "docs, enabled" })).toHaveAttribute("href", routes.mcpServer("srv-1"));
    expect(within(row).getByText("https://mcp.example.com/mcp")).toBeInTheDocument();
    expect(within(row).getByText("Enabled")).toBeInTheDocument();
    expect(within(row).getByTestId("mcp-last-test")).toHaveTextContent(/private, loopback or link-local/);
    // Header names left the list (decision "MCP Servers"): the detail's Connection card shows them.
    expect(row).not.toHaveTextContent("authorization");
    cleanup();
    render(<McpServerDetailPage />);
    expect(await screen.findByTestId("mcp-header-names")).toHaveTextContent("authorization (set)");
  });

  it("tests the connection and shows discovered tools with a collapsible schema", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue({
      status: "ok",
      tools: [{ name: "search", description: "Search the docs", input_schema: { type: "object", properties: { q: { type: "string" } } } }],
    });
    render(<McpServerDetailPage />);
    fireEvent.click(within(await screen.findByTestId("mcp-connection")).getByRole("button", { name: "Test connection" }));

    const tool = await screen.findByTestId("mcp-tool");
    expect(within(tool).getByText("search")).toBeInTheDocument();
    expect(within(tool).getByText("Search the docs")).toBeInTheDocument();
    // The schema is no longer a <details>: it opens in the tool Sheet.
    expect(screen.queryByTestId("mcp-tool-sheet")).toBeNull();
    fireEvent.click(within(tool).getByRole("button", { name: "search" }));
    const sheet = await screen.findByTestId("mcp-tool-sheet");
    expect(within(sheet).getByText("Input schema")).toBeInTheDocument();
    expect(within(sheet).getByTestId("mcp-tool-schema")).toHaveTextContent('"q"');
  });

  it("explains an outbound policy refusal from the test", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue({ status: "error", error_code: "address_blocked", tools: [] });
    render(<McpServerDetailPage />);
    fireEvent.click(within(await screen.findByTestId("mcp-connection")).getByRole("button", { name: "Test connection" }));

    const error = await screen.findByTestId("mcp-test-error");
    expect(error).toHaveTextContent("Blocked by the outbound policy");
    expect(error).toHaveTextContent("address_blocked");
  });

  it("disables a server", async () => {
    vi.mocked(mcpServers.setEnabled).mockResolvedValue({ ...server, enabled: false });
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Disable" }));
    await waitFor(() => expect(mcpServers.setEnabled).toHaveBeenCalledWith("srv-1", false));
    expect(await screen.findByText("Disabled")).toBeInTheDocument();
  });

  it("deletes only after confirmation", async () => {
    vi.mocked(mcpServers.remove).mockResolvedValue(undefined);
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = screen.getByRole("dialog");
    expect(mcpServers.remove).not.toHaveBeenCalled();
    // Delete waits for the fresh references check that opening the confirm starts.
    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Delete server" })).toBeEnabled());
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete server" }));

    await waitFor(() => expect(mcpServers.remove).toHaveBeenCalledWith("srv-1"));
    await waitFor(() => expect(screen.queryByTestId("mcp-server-row")).not.toBeInTheDocument());
  });

  it("delete dialog: focus starts on Cancel, Tab is trapped, background is inert, Escape cancels and restores focus", async () => {
    render(<McpServersPage />);
    const deleteButton = await screen.findByRole("button", { name: "Delete" });
    deleteButton.focus();
    fireEvent.click(deleteButton);

    const dialog = await screen.findByRole("dialog", { name: "Delete docs?" });
    const cancel = within(dialog).getByRole("button", { name: "Cancel" });
    const confirm = within(dialog).getByRole("button", { name: "Delete server" });
    await waitFor(() => expect(cancel).toHaveFocus());
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(deleteButton.closest("[inert]")).not.toBeNull();
    expect(dialog.closest("[inert]")).toBeNull();

    fireEvent.keyDown(cancel, { key: "Tab", shiftKey: true });
    expect(confirm).toHaveFocus();
    fireEvent.keyDown(confirm, { key: "Tab" });
    expect(cancel).toHaveFocus();

    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(mcpServers.remove).not.toHaveBeenCalled();
    expect(deleteButton.closest("[inert]")).toBeNull();
    expect(deleteButton).toHaveFocus();
  });

  it("moves focus to the page heading after a confirmed delete", async () => {
    vi.mocked(mcpServers.remove).mockResolvedValue(undefined);
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete server" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(screen.getByRole("heading", { name: "MCP Servers", level: 1 })).toHaveFocus();
  });

  it("shows a failed delete inside the dialog as an alert and keeps it open", async () => {
    vi.mocked(mcpServers.remove).mockRejectedValue(new ApiError(500, "database unavailable"));
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete server" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("database unavailable");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.queryByTestId("mcp-action-error")).not.toBeInTheDocument();
  });

  it("on a 404 closes the dialog, reloads the list and says the server was already deleted", async () => {
    vi.mocked(mcpServers.remove).mockRejectedValue(new ApiError(404, "MCP server not found"));
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    vi.mocked(mcpServers.list).mockResolvedValue([]);
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete server" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByTestId("mcp-notice")).toHaveTextContent("docs was already deleted.");
    await waitFor(() => expect(mcpServers.list).toHaveBeenCalledTimes(2));
    expect(screen.queryByTestId("mcp-server-row")).not.toBeInTheDocument();
  });

  it("while a delete runs, Cancel is disabled and Escape is ignored", async () => {
    let finish: () => void = () => undefined;
    vi.mocked(mcpServers.remove).mockImplementation(() => new Promise<void>((res) => { finish = res; }));
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete server" }));

    await waitFor(() => expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeDisabled());
    expect(dialog).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    await act(async () => finish());
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
    expect(screen.getByRole("heading", { name: "MCP Servers", level: 1 })).toHaveFocus();
  });

  it("an unchanged Save after Disable keeps the row as it is now (no stale write-back, no request)", async () => {
    vi.mocked(mcpServers.setEnabled).mockResolvedValue({ ...server, enabled: false, last_status: "ok", last_error_code: null });
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const editDialog = await screen.findByRole("dialog", { name: "Edit docs" });
    expect(within(editDialog).getByTestId("mcp-server-form")).toBeInTheDocument();
    // The edit form is a modal Dialog now: a user can no longer reach Disable behind it.
    expect(screen.queryByRole("button", { name: "Disable" })).toBeNull();
    // Guard test (onSaved): the row changes behind the open form (dispatched directly), then the
    // unchanged Save (onSaved(null)) must keep the row as it is now.
    fireEvent.click(screen.getByRole("button", { name: "Disable", hidden: true }));
    const row = screen.getByTestId("mcp-server-row");
    await waitFor(() => expect(within(row).getByText("Disabled")).toBeInTheDocument());

    fireEvent.click(within(editDialog).getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByTestId("mcp-server-form")).not.toBeInTheDocument());
    expect(mcpServers.update).not.toHaveBeenCalled();
    expect(within(screen.getByTestId("mcp-server-row")).getByText("Disabled")).toBeInTheDocument();
    expect(within(screen.getByTestId("mcp-server-row")).getByTestId("mcp-last-test")).toHaveTextContent("Connected");
  });

  it("deleting a server closes its open edit form, so Save cannot bring it back", async () => {
    vi.mocked(mcpServers.remove).mockResolvedValue(undefined);
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    await screen.findByRole("dialog", { name: "Edit docs" });
    expect(screen.getByTestId("mcp-server-form")).toBeInTheDocument();
    // Unreachable for a user behind the modal; dispatched directly to cover forgetServer's guard.
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Delete", hidden: true }));
    const confirm = await screen.findByRole("dialog", { name: "Delete docs?", hidden: true });
    fireEvent.click(within(confirm).getByRole("button", { name: "Delete server", hidden: true }));
    await waitFor(() => expect(screen.queryByTestId("mcp-server-row")).not.toBeInTheDocument());
    expect(screen.queryByTestId("mcp-server-form")).not.toBeInTheDocument();
    expect(mcpServers.update).not.toHaveBeenCalled();
  });
});
