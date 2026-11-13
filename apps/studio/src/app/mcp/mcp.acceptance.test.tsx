import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import McpServersPage from "./page";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { ApiError } from "@/lib/api";
import { mcpServers, type McpServer } from "@/lib/mcpServers";
import { toolRunLedger } from "@/lib/toolRunLedger";

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
  });

  afterEach(() => {
    document.querySelectorAll("[data-preinert], [data-late]").forEach((node) => node.remove());
  });

  it("lists rows with header names only, never secret values", async () => {
    render(<McpServersPage />);
    const row = await screen.findByTestId("mcp-server-row");
    expect(row).toHaveAttribute("data-server-name", "docs");
    expect(within(row).getByText("docs")).toBeInTheDocument();
    expect(within(row).getByText("https://mcp.example.com/mcp")).toBeInTheDocument();
    expect(within(row).getByText("Enabled")).toBeInTheDocument();
    expect(within(row).getByText("Headers: authorization (set)")).toBeInTheDocument();
    expect(within(row).getByTestId("mcp-last-test")).toHaveTextContent("Connected");
    expect(row.querySelector("input")).toBeNull();
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
    fireEvent.click(await screen.findByRole("button", { name: "Register server" }));
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
    expect(screen.queryByTestId("mcp-server-form")).not.toBeInTheDocument();
    const row = await screen.findByTestId("mcp-server-row");
    expect(within(row).getByText("Headers: Authorization (set)")).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(SECRET);
    expect(screen.queryByDisplayValue(SECRET)).not.toBeInTheDocument();
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
    render(<McpServersPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Test connection" }));
    const tools = await screen.findByTestId("mcp-test-tools");
    const tool = within(tools).getByTestId("mcp-tool");
    expect(within(tool).getByText("search")).toBeInTheDocument();
    expect(within(tool).getByText("Search the docs")).toBeInTheDocument();
    expect(within(tool).getByText("Input schema")).toBeInTheDocument();

    vi.mocked(mcpServers.test).mockResolvedValueOnce({ status: "error", error_code: "address_blocked", tools: [] });
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    const blocked = await screen.findByTestId("mcp-test-error");
    expect(blocked).toHaveTextContent("Blocked by the outbound policy");
    expect(blocked).toHaveTextContent("address_blocked");

    vi.mocked(mcpServers.test).mockResolvedValueOnce({ status: "error", error_code: "connect_failed", tools: [] });
    fireEvent.click(screen.getByRole("button", { name: "Test connection" }));
    const failed = await screen.findByTestId("mcp-test-error");
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
    expect(screen.getByTestId("mcp-server-form")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Disable" }));
    await waitFor(() => expect(within(screen.getByTestId("mcp-server-row")).getByText("Disabled")).toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
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

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete server" }));
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
