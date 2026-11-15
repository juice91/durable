// AC.13: the editor's capability steps: the Output step (builder, raw tab, citations, mode), the
// Identity step's limits and fallback models, the Tools step's approval toggles, and the Config panel.
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/agents/a1/editor",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({ id: "a1" }),
}));
vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(() => Promise.resolve([])), listEmbeddingModels: vi.fn(() => Promise.resolve([])) }));
vi.mock("@/lib/tools", () => ({ listTools: vi.fn() }));
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, list: vi.fn(() => Promise.resolve([])), test: vi.fn() } };
});
vi.mock("@/components/agents/specSections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/agents/specSections")>();
  return { ...actual, fetchProcessorCatalog: vi.fn(), fetchModels: vi.fn() };
});

import type { Agent, AgentSpec } from "@/lib/api";
import { listTools, type ToolDefinition } from "@/lib/tools";
import { mcpServers } from "@/lib/mcpServers";
import { fetchModels, fetchProcessorCatalog } from "@/components/agents/specSections";
import { modelsFixture, processorCatalogFixture } from "@/components/agents/specSections.fixtures";
import { ConfigPanel } from "@/components/agentPage/ConfigPanel";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { AgentEditor } from "../AgentEditor";

const TOOLS: ToolDefinition[] = [
  { id: "clock.now", title: "Clock", description: "", side_effect: false, external_idempotent: true, input_schema: {}, allowed: true },
  { id: "tickets.create", title: "Create ticket", description: "", side_effect: true, external_idempotent: false, input_schema: {}, allowed: true },
];

const PLAIN = {
  spec_version: 1,
  name: "Support bot",
  instructions: "Help people",
  model: { provider: "test", name: "echo" },
  tools: ["clock.now", "tickets.create"],
  mcp_tools: [{ server: "docs", tool: "search" }],
  model_settings: { temperature: null, max_tokens: null },
} as unknown as AgentSpec;

const MODELS = [...modelsFixture, { id: "test:other", provider: "test", name: "other", wildcard: false, source: "allowlist", description: null }];

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(mcpServers.list).mockResolvedValue([]);
  vi.mocked(listTools).mockResolvedValue(TOOLS);
  vi.mocked(fetchModels).mockResolvedValue(MODELS);
  vi.mocked(fetchProcessorCatalog).mockResolvedValue(processorCatalogFixture);
});

function Host({ initial, onSubmit, start, serverError = null, locations = [] }: { initial: AgentSpec; onSubmit: (spec: AgentSpec) => Promise<void>; start: string; serverError?: string | null; locations?: (string | number)[][] }) {
  const [step, setStep] = React.useState(start);
  return (
    <div>
      <span data-testid="active-step">{step}</span>
      <AgentEditor
        initialData={initial}
        onSubmit={onSubmit}
        submitLabel="Save draft"
        serverError={serverError}
        serverErrorLocations={locations}
        stepper={{ activeStep: step, onStepChange: setStep, layout: "nav" }}
      />
    </div>
  );
}

async function setup(initial: AgentSpec, start: string, props: Partial<React.ComponentProps<typeof Host>> = {}) {
  const saved: Record<string, unknown>[] = [];
  const onSubmit = vi.fn(async (spec: AgentSpec) => {
    saved.push(spec as unknown as Record<string, unknown>);
  });
  const view = render(<Host initial={initial} onSubmit={onSubmit} start={start} {...props} />);
  await act(async () => undefined);
  return { ...view, saved, onSubmit };
}

const save = async () => {
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  await act(async () => undefined);
};
const stepRegion = (id: string) => document.querySelector(`section[data-step="${id}"]`) as HTMLElement;

describe("untouched agents", () => {
  it("an agent without the four features saves the same JSON from the new steps", async () => {
    const { saved } = await setup(PLAIN, "output");
    await save();
    expect(JSON.stringify(saved[0])).toBe(JSON.stringify(PLAIN));
  });

  it("an agent with all four saves the same JSON", async () => {
    const stored = {
      ...PLAIN,
      output: { json_schema: { type: "object", properties: { a: { type: "string" } }, required: ["a"] }, mode: "native", max_retries: 3 },
      approvals: { tools: ["tickets.create"], timeout_hours: 4 },
      limits: { max_requests: 10, max_cost_usd: "0.5" },
      fallback_models: [{ provider: "test", name: "other" }],
    } as unknown as AgentSpec;
    const { saved } = await setup(stored, "identity");
    await save();
    expect(JSON.stringify(saved[0])).toBe(JSON.stringify(stored));
  });
});

describe("Output step", () => {
  it("turning it on adds output, off removes it", async () => {
    const { saved } = await setup(PLAIN, "output");
    fireEvent.click(screen.getByLabelText("Structured output"));
    await save();
    expect(saved[0].output).toEqual({ json_schema: { type: "object", properties: { answer: { type: "string", description: "The answer" } }, required: ["answer"] } });
    fireEvent.click(screen.getByLabelText("Structured output"));
    await save();
    expect(saved[1]).not.toHaveProperty("output");
  });

  it("builder edits write the schema; the raw tab shows them and its edits come back to the builder", async () => {
    const { saved } = await setup(PLAIN, "output");
    fireEvent.click(screen.getByLabelText("Structured output"));
    fireEvent.click(screen.getByRole("button", { name: "Add field" }));
    const rows = screen.getAllByTestId("output-field-row");
    fireEvent.change(within(rows[1]).getByLabelText("Name"), { target: { value: "score" } });
    fireEvent.change(within(rows[1]).getByLabelText("Type"), { target: { value: "number" } });
    // Radix tabs switch on mouse down.
    fireEvent.mouseDown(screen.getByRole("tab", { name: "JSON Schema" }));
    const raw = screen.getByRole("textbox", { name: "JSON Schema" }) as HTMLTextAreaElement;
    expect(JSON.parse(raw.value).properties.score).toEqual({ type: "number" });
    fireEvent.change(raw, { target: { value: JSON.stringify({ type: "object", properties: { x: { type: "boolean" } } }) } });
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Fields" }));
    expect(screen.getAllByTestId("output-field-row").map((row) => row.getAttribute("data-field-name"))).toEqual(["x"]);
    await save();
    expect((saved[0].output as { json_schema: unknown }).json_schema).toEqual({ type: "object", properties: { x: { type: "boolean" } } });
  });

  it("unsupported keywords are shown inline and block saving", async () => {
    const { onSubmit } = await setup(PLAIN, "output");
    fireEvent.click(screen.getByLabelText("Structured output"));
    fireEvent.mouseDown(screen.getByRole("tab", { name: "JSON Schema" }));
    fireEvent.change(screen.getByRole("textbox", { name: "JSON Schema" }), { target: { value: JSON.stringify({ type: "object", properties: { a: { $ref: "#/x" } } }) } });
    expect(screen.getByTestId("output-schema-problems")).toHaveTextContent("unsupported keyword '$ref'");
    expect(screen.getByRole("textbox", { name: "JSON Schema" })).toHaveAttribute("aria-invalid", "true");
    expect(document.getElementById("output-json_schema-problem")).toHaveTextContent("unsupported keyword '$ref'");
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Fields" }));
    expect(screen.getByTestId("output-builder-unavailable")).toBeInTheDocument();
    await save();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('"Add citations field" inserts the field once', async () => {
    const { saved } = await setup(PLAIN, "output");
    fireEvent.click(screen.getByLabelText("Structured output"));
    const add = screen.getByRole("button", { name: "Add citations field" });
    fireEvent.click(add);
    expect(add).toBeDisabled();
    await save();
    const schema = (saved[0].output as { json_schema: { properties: Record<string, unknown> } }).json_schema;
    expect(Object.keys(schema.properties)).toEqual(["answer", "citations"]);
  });

  it("mode tool is saved as no mode; native is saved", async () => {
    const { saved } = await setup(PLAIN, "output");
    fireEvent.click(screen.getByLabelText("Structured output"));
    fireEvent.click(screen.getByRole("radio", { name: /^Native/ }));
    await save();
    expect((saved[0].output as { mode?: string }).mode).toBe("native");
    fireEvent.click(screen.getByRole("radio", { name: /^Tool/ }));
    await save();
    expect(saved[1].output).not.toHaveProperty("mode");
  });

  it("a 422 at output.* opens the Output step", async () => {
    await setup(PLAIN, "identity", { serverError: "Value error, the JSON Schema is invalid", locations: [["body", "spec", "output", "json_schema"]] });
    expect(screen.getByTestId("active-step")).toHaveTextContent("output");
  });
});

describe("Identity step: limits and fallback models", () => {
  it("is collapsed for a plain agent", async () => {
    await setup(PLAIN, "identity");
    expect(screen.queryByLabelText(/^Model requests/)).toBeNull();
    expect(screen.getByRole("button", { name: /Limits and fallback models/ })).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(screen.getByRole("button", { name: /Limits and fallback models/ }));
    expect(screen.getByLabelText(/^Model requests/)).toBeInTheDocument();
  });

  it("limits are saved, and clearing every limit removes the key", async () => {
    const { saved } = await setup(PLAIN, "identity");
    fireEvent.click(screen.getByRole("button", { name: /Limits and fallback models/ }));
    fireEvent.change(screen.getByLabelText(/^Model requests/), { target: { value: "12" } });
    fireEvent.change(screen.getByLabelText(/^Cost \(USD/), { target: { value: "0.25" } });
    await save();
    expect(saved[0].limits).toEqual({ max_requests: 12, max_cost_usd: "0.25" });
    fireEvent.change(screen.getByLabelText(/^Model requests/), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText(/^Cost \(USD/), { target: { value: "" } });
    await save();
    expect(saved[1]).not.toHaveProperty("limits");
  });

  it("fallback pickers list allowlisted models only, without the primary or a model another row uses", async () => {
    const { saved } = await setup(PLAIN, "identity");
    fireEvent.click(screen.getByRole("button", { name: /Limits and fallback models/ }));
    await act(async () => undefined);
    fireEvent.click(screen.getByRole("button", { name: "Add fallback model" }));
    const first = screen.getByLabelText("Fallback 1") as HTMLSelectElement;
    const values = () => Array.from(first.options).map((o) => o.value).filter(Boolean);
    expect(values()).toEqual(["litellm:*", "test:moderation", "test:other"]);
    fireEvent.change(first, { target: { value: "test:other" } });
    fireEvent.click(screen.getByRole("button", { name: "Add fallback model" }));
    const second = screen.getByLabelText("Fallback 2") as HTMLSelectElement;
    expect(Array.from(second.options).map((o) => o.value).filter(Boolean)).toEqual(["litellm:*", "test:moderation"]);
    fireEvent.change(second, { target: { value: "test:moderation" } });
    await save();
    expect(saved[0].fallback_models).toEqual([{ provider: "test", name: "other" }, { provider: "test", name: "moderation" }]);
    expect(screen.getByRole("button", { name: /^Identity and model/ })).toHaveTextContent("fallback ×2");
  });

  it("an unchosen fallback blocks saving; removing it removes the key", async () => {
    const { saved, onSubmit } = await setup(PLAIN, "identity");
    fireEvent.click(screen.getByRole("button", { name: /Limits and fallback models/ }));
    fireEvent.click(screen.getByRole("button", { name: "Add fallback model" }));
    await save();
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Remove fallback 1" }));
    await save();
    expect(saved[0]).not.toHaveProperty("fallback_models");
  });

  it("a fallback_models 422 without a location opens Identity", async () => {
    await setup(PLAIN, "output", { serverError: "fallback_models[0]: model 'x:y' is not allowed on this platform" });
    expect(screen.getByTestId("active-step")).toHaveTextContent("identity");
  });
});

describe("Tools step: approvals", () => {
  it("each selected built-in and MCP tool row has its toggle; only built-ins with side effects are suggested", async () => {
    await setup(PLAIN, "tools");
    const toggles = screen.getAllByTestId("approval-toggle");
    expect(toggles.map((t) => t.getAttribute("data-tool-ref"))).toEqual(["clock.now", "tickets.create", "docs/search"]);
    // In the tool's own row: the built-in row and the MCP selected-tool row.
    expect(within(screen.getAllByTestId("builtin-tool-row")[1]).getByLabelText("Require approval for tickets.create")).toBeInTheDocument();
    expect(within(screen.getByTestId("mcp-selected-tool")).getByLabelText("Require approval for docs/search")).toBeInTheDocument();
    expect(within(toggles[0]).queryByTestId("approval-suggested")).toBeNull();
    expect(within(toggles[1]).getByTestId("approval-suggested")).toBeInTheDocument();
    // No annotations for the MCP tool (not loaded): no suggestion.
    expect(within(toggles[2]).queryByTestId("approval-suggested")).toBeNull();
    // A suggestion never ticks the box; no separate list and no timeout until a tool is gated.
    expect(screen.getByLabelText("Require approval for tickets.create")).not.toBeChecked();
    expect(screen.queryByTestId("approval-settings")).toBeNull();
  });

  it("an unselected built-in tool has no toggle", async () => {
    await setup({ ...PLAIN, tools: ["clock.now"] } as unknown as AgentSpec, "tools");
    expect(within(screen.getAllByTestId("builtin-tool-row")[1]).queryByTestId("approval-toggle")).toBeNull();
  });

  const DOCS_SERVER = { id: "s1", name: "docs", url: "https://docs.example", transport: "streamable_http" as const, enabled: true, header_names: [], created_at: null, updated_at: null, last_checked_at: null, last_status: null, last_error_code: null };

  it.each([
    ["destructiveHint", { destructiveHint: true }, "marked destructive"],
    ["no readOnlyHint", {}, "not marked read-only"],
  ])("an MCP tool whose loaded annotations say %s is suggested", async (_label, annotations, text) => {
    vi.mocked(mcpServers.list).mockResolvedValue([DOCS_SERVER]);
    vi.mocked(mcpServers.test).mockResolvedValue({ status: "ok", tools: [{ name: "search", description: null, input_schema: {}, annotations } as never] });
    await setup(PLAIN, "tools");
    expect(within(screen.getByTestId("mcp-selected-tool")).queryByTestId("approval-suggested")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Load tools" }));
    await act(async () => undefined);
    expect(within(screen.getByTestId("mcp-selected-tool")).getByTestId("approval-suggested")).toHaveTextContent(text);
  });

  it("an MCP tool whose loaded annotations say readOnlyHint is not suggested", async () => {
    vi.mocked(mcpServers.list).mockResolvedValue([DOCS_SERVER]);
    vi.mocked(mcpServers.test).mockResolvedValue({ status: "ok", tools: [{ name: "search", description: null, input_schema: {}, annotations: { readOnlyHint: true } } as never] });
    await setup(PLAIN, "tools");
    fireEvent.click(screen.getByRole("button", { name: "Load tools" }));
    await act(async () => undefined);
    expect(within(screen.getByTestId("mcp-selected-tool")).queryByTestId("approval-suggested")).toBeNull();
  });

  it("ticks write approvals with the timeout; unselecting the tool drops its entry and the key", async () => {
    const { saved } = await setup(PLAIN, "tools");
    fireEvent.click(screen.getByLabelText("Require approval for docs/search"));
    fireEvent.change(screen.getByLabelText(/^Approval timeout \(hours/), { target: { value: "6" } });
    await save();
    expect(saved[0].approvals).toEqual({ tools: ["docs/search"], timeout_hours: 6 });
    fireEvent.click(screen.getByRole("button", { name: "Remove docs/search" }));
    await save();
    expect(saved[1]).not.toHaveProperty("approvals");
    expect(saved[1]).not.toHaveProperty("mcp_tools");
  });

  it("a stale entry is listed with Remove", async () => {
    const stale = { ...PLAIN, approvals: { tools: ["gone.tool"] } } as unknown as AgentSpec;
    const { saved, onSubmit } = await setup(stale, "tools");
    expect(screen.getByTestId("approval-stale")).toHaveTextContent("gone.tool");
    await save();
    expect(onSubmit).not.toHaveBeenCalled();
    fireEvent.click(within(screen.getByTestId("approval-stale")).getByRole("button", { name: "Remove" }));
    await save();
    expect(saved[0]).not.toHaveProperty("approvals");
  });

  it("an approvals 422 opens Tools", async () => {
    await setup(PLAIN, "output", { serverError: "Value error, approvals.tools[0]: 'x' is not a tool of this agent", locations: [["body", "spec"]] });
    expect(screen.getByTestId("active-step")).toHaveTextContent("tools");
  });
});

describe("Config panel", () => {
  it("shows output, fallback models, limits and approval marks, each linking to its step", () => {
    const spec = {
      ...PLAIN,
      output: { json_schema: { type: "object", properties: { a: { type: "string" }, n: { type: ["integer", "null"] } }, required: ["a"] } },
      approvals: { tools: ["tickets.create", "docs/search"] },
      limits: { max_requests: 10, max_cost_usd: "0.5" },
      fallback_models: [{ provider: "test", name: "other" }],
    } as unknown as AgentSpec;
    const agent = { id: "a1", name: "Support bot", draft_spec: spec, latest_version: null } as Agent;
    render(<ConfigPanel agent={agent} versions={[]} />);
    expect(screen.getByTestId("config-output")).toHaveTextContent("Structured · Tool mode");
    expect(screen.getByTestId("config-output")).toHaveTextContent("a string, required");
    expect(screen.getByTestId("config-output")).toHaveTextContent("n integer or null");
    expect(screen.getByTestId("config-fallback-models")).toHaveTextContent("test:other");
    expect(screen.getByTestId("config-limits")).toHaveTextContent("Model requests ≤ 10");
    expect(screen.getByTestId("config-limits")).toHaveTextContent("Cost ≤ $0.5");
    expect(screen.getAllByTestId("config-approval-mark")).toHaveLength(2);
    expect(screen.getByRole("link", { name: "Edit output" })).toHaveAttribute("href", expect.stringContaining("step=output"));
  });

  it("an agent without them shows Text output and no marks", () => {
    const agent = { id: "a1", name: "Support bot", draft_spec: PLAIN, latest_version: null } as Agent;
    render(<ConfigPanel agent={agent} versions={[]} />);
    expect(screen.getByTestId("config-output-text")).toHaveTextContent("Text");
    expect(screen.queryByTestId("config-approval-mark")).toBeNull();
    expect(screen.queryByTestId("config-fallback-models")).toBeNull();
  });
});

describe("accessibility", () => {
  it("the three steps with every feature on have no axe violations", async () => {
    const spec = {
      ...PLAIN,
      output: { json_schema: { type: "object", properties: { a: { type: "string" }, o: { type: "object", properties: { b: { type: "integer" } } } } } },
      approvals: { tools: ["tickets.create"] },
      limits: { max_requests: 10 },
      fallback_models: [{ provider: "test", name: "other" }],
    } as unknown as AgentSpec;
    const { container } = await setup(spec, "output");
    await expectNoAxeViolations(stepRegion("output"));
    fireEvent.click(screen.getByRole("button", { name: /^Identity and model/ }));
    await act(async () => undefined);
    await expectNoAxeViolations(stepRegion("identity"));
    fireEvent.click(screen.getByRole("button", { name: /^Tools/ }));
    await expectNoAxeViolations(stepRegion("tools"));
    // Phone width: no fixed widths in the new surfaces.
    const offenders = Array.from(container.querySelectorAll('[data-testid="limits-fallback-group"] *, [data-testid="approval-toggle"] *, [data-testid="approval-settings"] *, section[data-step="output"] *'))
      .map((el) => el.getAttribute("class") ?? "")
      .filter((cls) => /(^|\s)(w|min-w)-\[\d+px\]/.test(cls) || /(^|\s)w-(64|72|80|96)(\s|$)/.test(cls));
    expect(offenders).toEqual([]);
  }, AXE_TIMEOUT_MS);
});

describe("review fixes (AC.13)", () => {
  it("a fallback row that duplicates the primary keeps its model listed, not as 'not in the allowlist'", async () => {
    const spec = { ...PLAIN, fallback_models: [{ provider: "test", name: "echo" }] } as unknown as AgentSpec;
    await setup(spec, "identity");
    await act(async () => undefined);
    const select = screen.getByLabelText("Fallback 1") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.textContent)).not.toContain("test:echo (not in the allowlist)");
    expect(select.value).toBe("test:echo");
    expect(document.getElementById("fallback-0-problem")).toHaveTextContent("cannot be the agent's own model");
  });

  it("builder checkboxes are named after their row", async () => {
    await setup(PLAIN, "output");
    fireEvent.click(screen.getByLabelText("Structured output"));
    expect(screen.getByRole("checkbox", { name: "Required: answer" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Can be null: answer" })).not.toBeChecked();
  });
});
