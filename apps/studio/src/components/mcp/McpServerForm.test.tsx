import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { McpServerForm } from "./McpServerForm";
import { mcpServers, type McpServer } from "@/lib/mcpServers";
import { ApiError } from "@/lib/api";

vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, create: vi.fn(), update: vi.fn() } };
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

describe("McpServerForm", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(mcpServers.update).mockImplementation(async (_id, body) => ({ ...server, ...body, header_names: server.header_names }) as McpServer);
    vi.mocked(mcpServers.create).mockImplementation(async (body) => ({ ...server, name: body.name, url: body.url }));
  });

  it("shows existing headers as set and never prefills a value", () => {
    render(<McpServerForm server={server} onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByTestId("mcp-header-set")).toHaveTextContent("•••• set");
    expect(screen.queryByPlaceholderText("New value")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Replace authorization" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove authorization" })).toBeInTheDocument();
  });

  it("does not send headers when only the name changes", async () => {
    const onSaved = vi.fn();
    render(<McpServerForm server={server} onSaved={onSaved} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "docs2" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(mcpServers.update).toHaveBeenCalledWith("srv-1", { name: "docs2" });
  });

  it("sends a header value only after Replace", async () => {
    render(<McpServerForm server={server} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Replace authorization" }));
    const input = screen.getByLabelText("New value for authorization");
    expect(input).toHaveValue("");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.change(input, { target: { value: "Bearer new" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(mcpServers.update).toHaveBeenCalledWith("srv-1", { headers: { authorization: "Bearer new" } }));
  });

  it("Remove sends an empty header set", async () => {
    render(<McpServerForm server={server} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Remove authorization" }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(mcpServers.update).toHaveBeenCalledWith("srv-1", { headers: {} }));
  });

  it("refuses to save a partial header change that would drop a kept value", async () => {
    render(<McpServerForm server={server} onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Add header" }));
    fireEvent.change(screen.getByLabelText("Header 2 name"), { target: { value: "X-Team" } });
    fireEvent.change(screen.getByLabelText("Header 2 value"), { target: { value: "blue" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(await screen.findByTestId("mcp-form-error")).toHaveTextContent("Replace authorization");
    expect(mcpServers.update).not.toHaveBeenCalled();
  });

  it("creates without a headers key when none were entered and explains a policy refusal", async () => {
    vi.mocked(mcpServers.create).mockRejectedValueOnce(new ApiError(422, "url refused by the outbound policy: address_blocked"));
    render(<McpServerForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "local" } });
    fireEvent.change(screen.getByLabelText("URL"), { target: { value: "https://127.0.0.1/mcp" } });
    fireEvent.click(screen.getByRole("button", { name: "Register" }));

    expect(await screen.findByTestId("mcp-form-error")).toHaveTextContent(/private, loopback or link-local/);
    expect(mcpServers.create).toHaveBeenCalledWith({ name: "local", url: "https://127.0.0.1/mcp", enabled: true });
  });

  it("gives each existing header's buttons a header-specific accessible name", () => {
    render(<McpServerForm server={{ ...server, header_names: ["authorization", "x-api-key"] }} onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Replace x-api-key" })).toHaveTextContent("Replace");
    fireEvent.click(screen.getByRole("button", { name: "Replace authorization" }));
    expect(screen.getByRole("button", { name: "Undo changes to authorization" })).toHaveTextContent("Undo");
    expect(screen.getByRole("button", { name: "Remove authorization" })).toHaveTextContent("Remove");
    expect(screen.getByRole("button", { name: "Remove x-api-key" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Add header" }));
    expect(screen.getByRole("button", { name: "Remove header 3" })).toBeInTheDocument();
  });

  it("an unchanged Save sends nothing and reports null instead of the server captured at open", async () => {
    const onSaved = vi.fn();
    render(<McpServerForm server={server} onSaved={onSaved} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith(null));
    expect(mcpServers.update).not.toHaveBeenCalled();
  });
});

describe("McpServerForm — GitHub MCP registry paste-in", () => {
  const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const setClipboard = (value: unknown) =>
    Object.defineProperty(navigator, "clipboard", { configurable: true, value });

  afterEach(() => {
    if (originalClipboard) Object.defineProperty(navigator, "clipboard", originalClipboard);
    else delete (navigator as { clipboard?: unknown }).clipboard;
  });

  it("Browse link opens the registry in a new tab with safe rel", () => {
    render(<McpServerForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    const link = screen.getByTestId("mcp-registry-link");
    expect(link).toHaveAttribute("href", "https://github.com/mcp");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("paste happy path: an allowlisted slug prefills both name and URL", async () => {
    render(<McpServerForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    const paste = screen.getByLabelText("Paste from the GitHub MCP registry");
    fireEvent.change(paste, { target: { value: "https://github.com/mcp/notion" } });
    expect(screen.getByLabelText("Name")).toHaveValue("notion");
    expect(screen.getByLabelText("URL")).toHaveValue("https://mcp.notion.com/mcp");
    expect(screen.getByTestId("mcp-registry-resolution"))
      .toHaveTextContent("Prefilled from notion. Enter your own auth token below.");
  });

  it("a github.com/mcp URL whose slug is not allowlisted prefills the name and leaves the URL for the user", () => {
    render(<McpServerForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    const paste = screen.getByLabelText("Paste from the GitHub MCP registry");
    fireEvent.change(paste, { target: { value: "https://github.com/mcp/pagerduty" } });
    expect(screen.getByLabelText("Name")).toHaveValue("pagerduty");
    expect(screen.getByLabelText("URL")).toHaveValue("");
    expect(screen.getByTestId("mcp-registry-resolution"))
      .toHaveTextContent("Prefilled the name pagerduty");
  });

  it("the Paste-from-clipboard button shows a hint and does not throw when the clipboard refuses", async () => {
    setClipboard({ readText: vi.fn(async () => { throw new Error("NotAllowedError"); }) });
    render(<McpServerForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Paste from clipboard" }));
    const note = await screen.findByTestId("mcp-registry-clipboard-note");
    expect(note).toHaveTextContent("Could not read the clipboard");
    // The input is still usable after a denied read — the user can paste manually.
    expect(screen.getByLabelText("Paste from the GitHub MCP registry")).toHaveValue("");
    expect(screen.queryByTestId("mcp-registry-resolution")).toBeNull();
  });

  it("the Paste-from-clipboard button applies the clipboard URL when permitted", async () => {
    setClipboard({ readText: vi.fn(async () => "https://github.com/mcp/github") });
    render(<McpServerForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Paste from clipboard" }));
    await waitFor(() => expect(screen.getByLabelText("Name")).toHaveValue("github"));
    expect(screen.getByLabelText("URL")).toHaveValue("https://api.githubcopilot.com/mcp/");
    expect(screen.getByTestId("mcp-registry-resolution")).toBeInTheDocument();
  });

  it("the paste-in section and the auth-header hint only appear in create mode", () => {
    render(<McpServerForm server={server} onSaved={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.queryByTestId("mcp-registry-paste")).toBeNull();
    expect(screen.queryByTestId("mcp-auth-header-hint")).toBeNull();
  });
});

describe("McpServerForm secret refs", () => {
  it("offers stored secrets and fills a header value with secret:<name>", async () => {
    const secrets = await import("@/lib/secrets");
    const spy = vi.spyOn(secrets, "listSecrets").mockResolvedValue([{ name: "slack-token", created_at: "a", updated_at: "a" }]);
    render(<McpServerForm onSaved={vi.fn()} onCancel={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Add header" }));
    const picker = await screen.findByLabelText("Header 1 secret");
    await waitFor(() => expect(within(picker).getByRole("option", { name: "slack-token" })).toBeInTheDocument());
    fireEvent.change(picker, { target: { value: "slack-token" } });
    expect(screen.getByLabelText("Header 1 value")).toHaveValue("secret:slack-token");
    spy.mockRestore();
  });
});
