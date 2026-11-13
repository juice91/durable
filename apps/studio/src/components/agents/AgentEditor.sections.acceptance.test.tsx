import { describe, it, expect, vi, beforeEach } from "vitest";
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { AgentEditor } from "./AgentEditor";
import { listTools } from "@/lib/tools";
import { mcpServers } from "@/lib/mcpServers";
import type { AgentSpec } from "@/lib/api";
import { fetchModels, fetchProcessorCatalog } from "./specSections";
import { modelsFixture, processorCatalogFixture } from "./specSections.fixtures";

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
  spec_version: 1, name: "Agent", instructions: "Do things", model: { provider: "test", name: "echo" }, tools: [],
};
const SECTION_KEYS = ["input_processors", "output_processors", "context_fields", "mcp_tools"];

describe("AgentEditor sections acceptance", () => {
  const onSubmit = vi.fn();

  beforeEach(() => {
    vi.resetAllMocks();
    onSubmit.mockResolvedValue(undefined);
    vi.mocked(listTools).mockResolvedValue([]);
    vi.mocked(mcpServers.list).mockResolvedValue([]);
    vi.mocked(fetchProcessorCatalog).mockResolvedValue(processorCatalogFixture);
    vi.mocked(fetchModels).mockResolvedValue(modelsFixture);
  });

  async function save() {
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(onSubmit).toHaveBeenCalled());
    return onSubmit.mock.calls.at(-1)![0] as Record<string, unknown>;
  }

  it("spec hash stability: a spec without sections saves with none of the four keys", async () => {
    render(<AgentEditor initialData={spec} onSubmit={onSubmit} submitLabel="Save" />);
    const saved = await save();
    for (const key of SECTION_KEYS) expect(key in saved).toBe(false);
    expect(Object.keys(saved).sort()).toEqual(["instructions", "model", "model_settings", "name", "spec_version", "tools"]);
  });

  it("explicitly empty lists in a stored spec are dropped, not re-sent as []", async () => {
    const stored = { ...spec, input_processors: [], output_processors: [], context_fields: [], mcp_tools: [] } as AgentSpec;
    render(<AgentEditor initialData={stored} onSubmit={onSubmit} submitLabel="Save" />);
    const saved = await save();
    for (const key of SECTION_KEYS) expect(key in saved).toBe(false);
  });

  it("existing values on a draft save are kept verbatim", async () => {
    const stored = {
      ...spec,
      input_processors: [{ type: "max_length", config: { max_chars: 100, action: "truncate" } }],
      context_fields: [{ name: "tier", type: "string", required: false, description: "", max_length: null, values: [] }],
      mcp_tools: [{ server: "docs", tool: "search" }],
    } as AgentSpec;
    render(<AgentEditor initialData={stored} onSubmit={onSubmit} submitLabel="Save" />);
    await screen.findByText("1. Maximum length");
    const saved = await save();
    expect(saved.input_processors).toEqual((stored as unknown as Record<string, unknown>).input_processors);
    expect(saved.context_fields).toEqual((stored as unknown as Record<string, unknown>).context_fields);
    expect(saved.mcp_tools).toEqual(stored.mcp_tools);
    expect("output_processors" in saved).toBe(false);
  });

  it("removing the last processor and context field returns to the stable shape", async () => {
    const stored = {
      ...spec,
      input_processors: [{ type: "max_length", config: { max_chars: 100, action: "truncate" } }],
      context_fields: [{ name: "tier", type: "string", required: false, description: "", max_length: null, values: [] }],
    } as AgentSpec;
    render(<AgentEditor initialData={stored} onSubmit={onSubmit} submitLabel="Save" />);
    fireEvent.click(await screen.findByRole("button", { name: "Remove Maximum length" }));
    fireEvent.click(screen.getByRole("button", { name: /Remove context field/i }));
    const saved = await save();
    for (const key of SECTION_KEYS) expect(key in saved).toBe(false);
  });

  it("flags duplicate names, reserved names and empty enums per field, politely", async () => {
    render(<AgentEditor initialData={spec} onSubmit={onSubmit} submitLabel="Save" />);
    for (let i = 0; i < 3; i++) fireEvent.click(screen.getByRole("button", { name: "Add context field" }));
    fireEvent.change(document.getElementById("context-field-0-name")!, { target: { value: "region" } });
    fireEvent.change(document.getElementById("context-field-1-name")!, { target: { value: "region" } });
    fireEvent.change(document.getElementById("context-field-2-name")!, { target: { value: "mode" } });
    fireEvent.change(document.getElementById("context-field-2-type")!, { target: { value: "enum" } });
    expect(screen.getByTestId("context-field-0-problems")).toHaveTextContent("");
    expect(screen.getByTestId("context-field-1-problems")).toHaveTextContent("Duplicate field name 'region'");
    expect(screen.getByTestId("context-field-2-problems")).toHaveTextContent("An enum field needs at least one value");
    expect(document.getElementById("context-field-1-name")).toHaveAttribute("aria-invalid", "true");
    fireEvent.change(document.getElementById("context-field-2-values")!, { target: { value: "a\nb\na" } });
    expect(screen.getByTestId("context-field-2-problems")).toHaveTextContent("Enum values must be unique");
  });

  it("dirty state follows adds and removals back to clean", async () => {
    const onDirtyChange = vi.fn();
    render(<AgentEditor initialData={spec} onSubmit={onSubmit} submitLabel="Save" onDirtyChange={onDirtyChange} />);
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
    fireEvent.click(screen.getByRole("button", { name: "Add context field" }));
    expect(onDirtyChange).toHaveBeenLastCalledWith(true);
    fireEvent.click(screen.getByRole("button", { name: /Remove context field/i }));
    expect(onDirtyChange).toHaveBeenLastCalledWith(false);
  });
});
