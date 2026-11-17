import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import { AgentEditor } from "./AgentEditor";
import { listTools } from "@/lib/tools";
import { mcpServers } from "@/lib/mcpServers";
import type { AgentSpec } from "@/lib/api";
import { fetchModels, fetchProcessorCatalog } from "./specSections";
import { modelsFixture, processorCatalogFixture } from "./specSections.fixtures";

// RM.17: the Knowledge and Memory steps load these when shown; never reach the network.
vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(() => Promise.resolve([])), listEmbeddingModels: vi.fn(() => Promise.resolve([])) }));
vi.mock("@/lib/tools", () => ({ listTools: vi.fn() }));
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, list: vi.fn(), test: vi.fn() } };
});
vi.mock("./specSections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./specSections")>();
  return { ...actual, fetchProcessorCatalog: vi.fn(), fetchModels: vi.fn() };
});

const spec: AgentSpec = {
  spec_version: 1,
  name: "Agent",
  instructions: "Do things",
  model: { provider: "test", name: "echo" },
  tools: [],
};

const fullSections = {
  input_processors: [
    { type: "pii_redact", config: { kinds: ["email", "iban"], action: "block" } },
    { type: "regex_blocklist", config: { patterns: ["secret\\d+", "forbidden"], action: "redact" } },
  ],
  output_processors: [
    { type: "max_length", config: { max_chars: 500, action: "truncate" } },
    { type: "moderation", config: { model: { provider: "test", name: "moderation" }, categories: ["hate", "violence"], action: "block" } },
  ],
  context_fields: [
    { name: "s", type: "string", required: true, description: "a string", max_length: 40, values: [] },
    { name: "i", type: "integer", required: false, description: "", max_length: null, values: [] },
    { name: "n", type: "number", required: false, description: "", max_length: null, values: [] },
    { name: "b", type: "boolean", required: false, description: "", max_length: null, values: [] },
    { name: "e", type: "enum", required: false, description: "", max_length: null, values: ["x", "y"] },
  ],
};

describe("AgentEditor processors and request context", () => {
  const onSubmit = vi.fn();

  beforeEach(() => {
    vi.clearAllMocks();
    onSubmit.mockResolvedValue(undefined);
    vi.mocked(listTools).mockResolvedValue([]);
    vi.mocked(mcpServers.list).mockResolvedValue([]);
    vi.mocked(fetchProcessorCatalog).mockResolvedValue(processorCatalogFixture);
    vi.mocked(fetchModels).mockResolvedValue(modelsFixture);
  });

  it("omits empty processor lists and context fields from the saved spec", async () => {
    render(<AgentEditor initialData={spec} onSubmit={onSubmit} submitLabel="Save" />);
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    const saved = onSubmit.mock.calls[0][0];
    expect("input_processors" in saved).toBe(false);
    expect("output_processors" in saved).toBe(false);
    expect("context_fields" in saved).toBe(false);
  });

  it("round-trips every processor type and field type unchanged on save", async () => {
    const onDirtyChange = vi.fn();
    render(<AgentEditor initialData={{ ...spec, ...fullSections } as AgentSpec} onSubmit={onSubmit} submitLabel="Save" onDirtyChange={onDirtyChange} />);
    await screen.findByText("1. PII redaction");
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    const saved = onSubmit.mock.calls[0][0];
    expect(saved.input_processors).toEqual(fullSections.input_processors);
    expect(saved.output_processors).toEqual(fullSections.output_processors);
    expect(saved.context_fields).toEqual(fullSections.context_fields);
  });

  it("builds a PII processor, a blocklist and two context fields, and tracks dirty state", async () => {
    const onDirtyChange = vi.fn();
    render(<AgentEditor initialData={spec} onSubmit={onSubmit} submitLabel="Save" onDirtyChange={onDirtyChange} />);
    await waitFor(() => expect(screen.getByLabelText("Input processor type")).not.toBeDisabled());
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);

    fireEvent.change(screen.getByLabelText("Input processor type"), { target: { value: "pii_redact" } });
    fireEvent.click(screen.getByRole("button", { name: "Add input processor" }));
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    // Untick IBAN and switch to block.
    fireEvent.click(document.getElementById("processor-input-0-kinds-iban")!);
    fireEvent.change(document.getElementById("processor-input-0-action")!, { target: { value: "block" } });

    fireEvent.change(screen.getByLabelText("Input processor type"), { target: { value: "regex_blocklist" } });
    fireEvent.click(screen.getByRole("button", { name: "Add input processor" }));
    const patterns = screen.getByLabelText("Patterns *");
    expect(within(screen.getByTestId("processor-input-1")).getByText("Patterns needs at least one entry")).toBeInTheDocument();
    fireEvent.change(patterns, { target: { value: "secret\\d+\n" } });
    expect(patterns).toHaveValue("secret\\d+\n");

    fireEvent.click(screen.getByRole("button", { name: "Add context field" }));
    fireEvent.change(screen.getByLabelText("Field name", { selector: "#context-field-0-name" }), { target: { value: "customer_tier" } });
    fireEvent.change(document.getElementById("context-field-0-type")!, { target: { value: "enum" } });
    fireEvent.change(document.getElementById("context-field-0-values")!, { target: { value: "gold\nsilver" } });
    fireEvent.click(screen.getByRole("button", { name: "Add context field" }));
    fireEvent.change(document.getElementById("context-field-1-name")!, { target: { value: "locale" } });
    fireEvent.click(document.getElementById("context-field-1-required")!);

    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    const saved = onSubmit.mock.calls[0][0];
    expect(saved.input_processors).toEqual([
      { type: "pii_redact", config: { kinds: ["email", "phone", "credit_card", "ipv4", "us_ssn"], action: "block" } },
      { type: "regex_blocklist", config: { patterns: ["secret\\d+"], action: "block" } },
    ]);
    expect("output_processors" in saved).toBe(false);
    expect(saved.context_fields).toEqual([
      { name: "customer_tier", type: "enum", required: false, description: "", max_length: null, values: ["gold", "silver"] },
      { name: "locale", type: "string", required: true, description: "", max_length: null, values: [] },
    ]);
  });

  it("orders and removes processors", async () => {
    render(<AgentEditor initialData={{ ...spec, input_processors: fullSections.input_processors } as AgentSpec} onSubmit={onSubmit} submitLabel="Save" />);
    fireEvent.click(await screen.findByRole("button", { name: "Move Regex blocklist up" }));
    fireEvent.click(screen.getByRole("button", { name: "Remove PII redaction" }));
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit.mock.calls[0][0].input_processors).toEqual([fullSections.input_processors[1]]);
  });

  it("adds a moderation output processor with the model picker and notes that streaming is off", async () => {
    render(<AgentEditor initialData={spec} onSubmit={onSubmit} submitLabel="Save" />);
    expect(screen.getByText(/any output processor holds the answer until it is checked/)).toBeInTheDocument();
    // Scope note (P0.21): tool results are outside the processors.
    expect(screen.getByText(/Request context values are not processed/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByLabelText("Output processor type")).not.toBeDisabled());
    await waitFor(() => expect(fetchModels).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Output processor type"), { target: { value: "moderation" } });
    fireEvent.click(screen.getByRole("button", { name: "Add output processor" }));
    // The shared picker preselects the first listed model: provider and model both shown.
    await waitFor(() => expect(screen.getByLabelText("Moderation provider *")).toHaveValue("test"));
    expect(screen.getByLabelText("Moderation model *")).toHaveTextContent("moderation");
    // The model is chosen from a searchable list of the provider's models.
    fireEvent.click(screen.getByLabelText("Moderation model *"));
    fireEvent.click(await screen.findByRole("option", { name: "echo" }));
    await waitFor(() => expect(screen.getByLabelText("Moderation model *")).toHaveTextContent("echo"));
    // A wildcard provider takes a typed name.
    fireEvent.change(screen.getByLabelText("Moderation provider *"), { target: { value: "litellm" } });
    fireEvent.change(screen.getByLabelText("Moderation model *"), { target: { value: "gpt-4o-mini" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit.mock.calls[0][0].output_processors).toEqual([
      { type: "moderation", config: { model: { provider: "litellm", name: "gpt-4o-mini" }, categories: processorCatalogFixture.moderation_categories, action: "block" } },
    ]);
  });

  it("shows placeholder hints and flags undeclared placeholders", async () => {
    render(<AgentEditor initialData={{ ...spec, instructions: "Hello {{context.tier}} from {{context.region}}", context_fields: [fullSections.context_fields[0]] } as AgentSpec} onSubmit={onSubmit} submitLabel="Save" />);
    expect(screen.getByTestId("placeholder-hints")).toHaveTextContent("{{context.s}}");
    expect(screen.getAllByTestId("placeholder-error").map((e) => e.textContent)).toEqual([
      "placeholder {{context.tier}} names no context field",
      "placeholder {{context.region}} names no context field",
    ]);
    expect(screen.getByText(/not used in the instructions/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Insert placeholder" }));
    expect((document.getElementById("instructions") as HTMLTextAreaElement).value).toContain("{{context.s}}");
    expect(screen.getByText(/used in the instructions/)).toHaveTextContent("{{context.s}} used in the instructions");
  });

  it("shows API errors next to the processor or section they name", async () => {
    const { rerender } = render(
      <AgentEditor initialData={{ ...spec, ...fullSections } as AgentSpec} onSubmit={onSubmit} submitLabel="Save" serverError="input_processors[1].config.patterns[0]: invalid RE2 pattern" />,
    );
    expect(await screen.findByTestId("processor-input-1-error")).toHaveTextContent("invalid RE2 pattern");
    rerender(<AgentEditor initialData={{ ...spec, ...fullSections } as AgentSpec} onSubmit={onSubmit} submitLabel="Save" serverError="processor model 'x:y' is not allowed on this platform" />);
    expect(screen.getByTestId("processors-error")).toHaveTextContent("not allowed");
    rerender(<AgentEditor initialData={{ ...spec, ...fullSections } as AgentSpec} onSubmit={onSubmit} submitLabel="Save" serverError="instructions: placeholder {{context.zz}} names no context field" />);
    expect(screen.getByTestId("instructions-server-error")).toHaveTextContent("{{context.zz}}");
  });

  it("keeps stored processors when the catalog cannot load", async () => {
    vi.mocked(fetchProcessorCatalog).mockRejectedValue(new Error("HTTP 503"));
    render(<AgentEditor initialData={{ ...spec, ...fullSections } as AgentSpec} onSubmit={onSubmit} submitLabel="Save" />);
    expect(await screen.findByText(/Could not load the processor catalog: HTTP 503/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit.mock.calls[0][0].output_processors).toEqual(fullSections.output_processors);
  });

  it("keeps a server error on the processor it named after reordering, and drops it on removal", async () => {
    render(
      <AgentEditor initialData={{ ...spec, ...fullSections } as AgentSpec} onSubmit={onSubmit} submitLabel="Save" serverError="output_processors[1].config.model: not allowed" />,
    );
    expect(await screen.findByTestId("processor-output-1-error")).toHaveTextContent("not allowed");
    // Move the moderation processor (index 1) up: the message moves with it to index 0.
    fireEvent.click(screen.getByRole("button", { name: "Move Moderation up" }));
    expect(within(screen.getByTestId("processor-output-0")).getByText("1. Moderation")).toBeInTheDocument();
    expect(screen.getByTestId("processor-output-0-error")).toHaveTextContent("not allowed");
    expect(screen.queryByTestId("processor-output-1-error")).not.toBeInTheDocument();
    // Removing it removes the message; it does not land on the processor now at that index.
    fireEvent.click(screen.getByRole("button", { name: "Remove Moderation" }));
    expect(screen.queryByTestId("processor-output-0-error")).not.toBeInTheDocument();
  });

  it("keys processors by a stable client id, so their form state moves with them", async () => {
    render(<AgentEditor initialData={{ ...spec, input_processors: fullSections.input_processors } as AgentSpec} onSubmit={onSubmit} submitLabel="Save" />);
    await screen.findByText("1. PII redaction");
    const blocklistItem = screen.getByTestId("processor-input-1");
    fireEvent.click(screen.getByRole("button", { name: "Move Regex blocklist up" }));
    // React moved the same DOM node rather than re-rendering another processor into it.
    expect(screen.getByTestId("processor-input-0")).toBe(blocklistItem);
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    expect(onSubmit.mock.calls[0][0].input_processors).toEqual([fullSections.input_processors[1], fullSections.input_processors[0]]);
    expect(JSON.stringify(onSubmit.mock.calls[0][0])).not.toContain("clientId");
  });

  it("announces validation problems politely through described-by regions, not alerts", async () => {
    render(<AgentEditor initialData={{ ...spec, instructions: "Hi {{context.nope}}" }} onSubmit={onSubmit} submitLabel="Save" />);
    const instructions = document.getElementById("instructions")!;
    expect(instructions).toHaveAttribute("aria-invalid", "true");
    expect(instructions.getAttribute("aria-describedby")).toContain("instructions-problems");
    const region = document.getElementById("instructions-problems")!;
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("names no context field");

    fireEvent.click(screen.getByRole("button", { name: "Add context field" }));
    fireEvent.change(document.getElementById("context-field-0-name")!, { target: { value: "user_id" } });
    const name = document.getElementById("context-field-0-name")!;
    expect(name).toHaveAttribute("aria-describedby", "context-field-0-problems");
    expect(screen.getByTestId("context-field-0-problems")).toHaveAttribute("aria-live", "polite");
    expect(screen.getByTestId("context-field-0-problems")).toHaveTextContent("reserved");
    expect(screen.queryAllByRole("alert").filter((el) => /reserved|names no context field/.test(el.textContent ?? ""))).toEqual([]);
  });
});
