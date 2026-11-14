import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { AgentEditor } from "./AgentEditor";
import { listTools } from "@/lib/tools";
import { mcpServers, type McpServer } from "@/lib/mcpServers";
import type { AgentSpec } from "@/lib/api";

// RM.17: the Knowledge and Memory steps load these when shown; never reach the network.
vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(() => Promise.resolve([])), listEmbeddingModels: vi.fn(() => Promise.resolve([])) }));
vi.mock("@/lib/tools", () => ({ listTools: vi.fn() }));
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, list: vi.fn(), test: vi.fn() } };
});

describe("AgentEditor", () => {
  const mockSubmit = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listTools).mockReturnValue(new Promise<never>(() => undefined));
    vi.mocked(mcpServers.list).mockReturnValue(new Promise<never>(() => undefined));
  });

  it("renders all form fields with labels", () => {
    render(<AgentEditor onSubmit={mockSubmit} submitLabel="Create" />);

    // Check that all labels exist in the DOM
    expect(document.body.innerHTML).toContain('for="name"');
    expect(document.body.innerHTML).toContain('for="instructions"');
    expect(document.body.innerHTML).toContain('for="provider"');
    expect(document.body.innerHTML).toContain('for="modelName"');
    expect(document.body.innerHTML).toContain('for="temperature"');
    expect(document.body.innerHTML).toContain('for="maxTokens"');
    expect(screen.getByText("Create")).toBeInTheDocument();
  });

  it("lists tools from the API and allows selecting an allowed tool", async () => {
    vi.mocked(listTools).mockResolvedValue([
      { id: "clock.now", title: "Current time", description: "Reads the clock", side_effect: false, external_idempotent: true, input_schema: { type: "object", properties: {} }, allowed: true },
    ]);
    render(<AgentEditor onSubmit={mockSubmit} submitLabel="Create" />);

    const toolCheckbox = await screen.findByLabelText("Current time (clock.now)");
    expect(toolCheckbox).not.toBeChecked();
    fireEvent.click(toolCheckbox);
    expect(toolCheckbox).toBeChecked();
  });

  it("shows disallowed tools disabled with a reason", async () => {
    vi.mocked(listTools).mockResolvedValue([
      { id: "tickets.create", title: "Create ticket", description: "Disabled by policy", side_effect: true, external_idempotent: false, input_schema: { type: "object", properties: {} }, allowed: false },
    ]);
    render(<AgentEditor onSubmit={mockSubmit} submitLabel="Create" />);

    expect(await screen.findByLabelText("Create ticket (tickets.create)")).toBeDisabled();
    expect(screen.getByText("Disabled by platform policy")).toBeInTheDocument();
  });

  it("handles temperature input", () => {
    render(<AgentEditor onSubmit={mockSubmit} submitLabel="Create" />);

    const tempInput = screen.getByLabelText(/temperature/i);
    expect(tempInput).toBeInTheDocument();
    expect(tempInput).toHaveAttribute("type", "number");
  });

  it("handles max tokens input", () => {
    render(<AgentEditor onSubmit={mockSubmit} submitLabel="Create" />);

    const maxTokensInput = screen.getByLabelText(/max tokens/i);
    expect(maxTokensInput).toBeInTheDocument();
    expect(maxTokensInput).toHaveAttribute("type", "number");
  });

  it("calls onSubmit when clicked", async () => {
    render(<AgentEditor onSubmit={mockSubmit} submitLabel="Create" />);

    // Use form element to submit
    const form = document.querySelector("form");
    const nameInput = document.getElementById("name") as HTMLInputElement;
    if (form && nameInput) {
      nameInput.value = "Test Agent";
      nameInput.dispatchEvent(new Event("input", { bubbles: true }));
      form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    }

    expect(mockSubmit).toHaveBeenCalled();
  });

  it("pre-fills with initial data", () => {
    const initialData = {
      spec_version: 1 as const,
      name: "Existing Agent",
      instructions: "Existing instructions",
      model: { provider: "anthropic", name: "claude-3" },
      tools: ["clock.now"],
      model_settings: { temperature: 0.5, max_tokens: 500 },
    };

    render(<AgentEditor initialData={initialData} onSubmit={mockSubmit} submitLabel="Save" />);

    expect(screen.getByDisplayValue("Existing Agent")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Existing instructions")).toBeInTheDocument();
    expect(screen.getByDisplayValue("anthropic")).toBeInTheDocument();
    expect(screen.getByDisplayValue("claude-3")).toBeInTheDocument();
    expect(screen.getByDisplayValue("0.5")).toBeInTheDocument();
    expect(screen.getByDisplayValue("500")).toBeInTheDocument();
  });

  describe("MCP tools", () => {
    const spec: AgentSpec = {
      spec_version: 1,
      name: "Agent",
      instructions: "Do things",
      model: { provider: "test", name: "m" },
      tools: [],
    };
    const docs: McpServer = {
      id: "srv-1", name: "docs", url: "https://mcp.example.com/mcp", transport: "streamable_http", enabled: true,
      header_names: [], created_at: null, updated_at: null, last_checked_at: null, last_status: null, last_error_code: null,
    };

    it("omits mcp_tools from the payload when none are selected", async () => {
      mockSubmit.mockResolvedValue(undefined);
      render(<AgentEditor initialData={spec} onSubmit={mockSubmit} submitLabel="Save" />);
      fireEvent.click(screen.getByText("Save"));
      await waitFor(() => expect(mockSubmit).toHaveBeenCalled());
      expect("mcp_tools" in mockSubmit.mock.calls[0][0]).toBe(false);
    });

    it("keeps existing mcp_tools when saving", async () => {
      mockSubmit.mockResolvedValue(undefined);
      render(<AgentEditor initialData={{ ...spec, mcp_tools: [{ server: "docs", tool: "search" }] }} onSubmit={mockSubmit} submitLabel="Save" />);
      expect(screen.getByText("docs/search")).toBeInTheDocument();
      fireEvent.click(screen.getByText("Save"));
      await waitFor(() => expect(mockSubmit).toHaveBeenCalled());
      expect(mockSubmit.mock.calls[0][0].mcp_tools).toEqual([{ server: "docs", tool: "search" }]);
    });

    it("picks a discovered tool, or a typed one when listing fails", async () => {
      mockSubmit.mockResolvedValue(undefined);
      vi.mocked(mcpServers.list).mockResolvedValue([docs, { ...docs, id: "srv-2", name: "off", enabled: false }]);
      vi.mocked(mcpServers.test).mockResolvedValueOnce({ status: "ok", tools: [{ name: "search", description: null, input_schema: {} }] });
      render(<AgentEditor initialData={spec} onSubmit={mockSubmit} submitLabel="Save" />);

      fireEvent.click(await screen.findByRole("button", { name: "Load tools" }));
      fireEvent.click(await screen.findByLabelText("docs/search"));
      fireEvent.change(screen.getByLabelText("Tool name on docs"), { target: { value: "fetch_page" } });
      fireEvent.click(screen.getByRole("button", { name: "Add" }));
      // A disabled server gets no picker.
      expect(screen.queryByLabelText("Tool name on off")).not.toBeInTheDocument();

      fireEvent.click(screen.getByText("Save"));
      await waitFor(() => expect(mockSubmit).toHaveBeenCalled());
      expect(mockSubmit.mock.calls[0][0].mcp_tools).toEqual([
        { server: "docs", tool: "search" },
        { server: "docs", tool: "fetch_page" },
      ]);
    });
  });
});
