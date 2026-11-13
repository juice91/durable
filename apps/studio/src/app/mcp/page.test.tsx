import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import McpServersPage from "./page";
import { mcpServers, type McpServer } from "@/lib/mcpServers";
import { ApiError } from "@/lib/api";

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
  });

  it("lists servers with URL, enabled state, set headers and the last test result", async () => {
    render(<McpServersPage />);
    const row = await screen.findByTestId("mcp-server-row");
    expect(within(row).getByText("docs")).toBeInTheDocument();
    expect(within(row).getByText("https://mcp.example.com/mcp")).toBeInTheDocument();
    expect(within(row).getByText("Enabled")).toBeInTheDocument();
    expect(within(row).getByText("Headers: authorization (set)")).toBeInTheDocument();
    expect(within(row).getByTestId("mcp-last-test")).toHaveTextContent(/private, loopback or link-local/);
  });

  it("tests the connection and shows discovered tools with a collapsible schema", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue({
      status: "ok",
      tools: [{ name: "search", description: "Search the docs", input_schema: { type: "object", properties: { q: { type: "string" } } } }],
    });
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));

    const tool = await screen.findByTestId("mcp-tool");
    expect(within(tool).getByText("search")).toBeInTheDocument();
    expect(within(tool).getByText("Search the docs")).toBeInTheDocument();
    expect(within(tool).getByText("Input schema")).toBeInTheDocument();
  });

  it("explains an outbound policy refusal from the test", async () => {
    vi.mocked(mcpServers.test).mockResolvedValue({ status: "error", error_code: "address_blocked", tools: [] });
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));

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
    expect(screen.getByTestId("mcp-server-form")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    const row = screen.getByTestId("mcp-server-row");
    await waitFor(() => expect(within(row).getByText("Disabled")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(screen.queryByTestId("mcp-server-form")).not.toBeInTheDocument());
    expect(mcpServers.update).not.toHaveBeenCalled();
    expect(within(screen.getByTestId("mcp-server-row")).getByText("Disabled")).toBeInTheDocument();
    expect(within(screen.getByTestId("mcp-server-row")).getByTestId("mcp-last-test")).toHaveTextContent("Connected");
  });

  it("deleting a server closes its open edit form, so Save cannot bring it back", async () => {
    vi.mocked(mcpServers.remove).mockResolvedValue(undefined);
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    expect(screen.getByTestId("mcp-server-form")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete server" }));
    await waitFor(() => expect(screen.queryByTestId("mcp-server-row")).not.toBeInTheDocument());
    expect(screen.queryByTestId("mcp-server-form")).not.toBeInTheDocument();
    expect(mcpServers.update).not.toHaveBeenCalled();
  });
});
