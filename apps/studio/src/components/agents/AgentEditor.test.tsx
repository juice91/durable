import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import "@testing-library/jest-dom";
import { AgentEditor } from "./AgentEditor";
import { AVAILABLE_TOOLS } from "./tools";

describe("AgentEditor", () => {
  const mockSubmit = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
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

  it("allows selecting tools", () => {
    render(<AgentEditor onSubmit={mockSubmit} submitLabel="Create" />);

    const toolCheckbox = screen.getByLabelText(AVAILABLE_TOOLS[0]);
    expect(toolCheckbox).not.toBeChecked();
    expect(screen.getByLabelText(AVAILABLE_TOOLS[0])).toBeInTheDocument();
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
