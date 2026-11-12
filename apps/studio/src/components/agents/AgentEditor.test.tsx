import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";
import { AgentEditor } from "./AgentEditor";
import { listTools } from "@/lib/tools";

vi.mock("@/lib/tools", () => ({ listTools: vi.fn() }));

describe("AgentEditor", () => {
  const mockSubmit = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listTools).mockReturnValue(new Promise<never>(() => undefined));
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
      spec_version: 1,
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
});
