// AC.13 acceptance: spec stability against a recorded fixture, step order and deep link, error
// mapping, the full editor flow (all four features, published through a mocked API), publish
// warnings and phone width. capabilitySteps.test.tsx (the implementer's) covers the step bodies.
import React from "react";
import "@testing-library/jest-dom";
import { createHash } from "node:crypto";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ search: "", replace: vi.fn() }));
vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(() => Promise.resolve([])), listEmbeddingModels: vi.fn(() => Promise.resolve([])) }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: nav.replace, back: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/agents/a1/editor",
  useSearchParams: () => new URLSearchParams(nav.search),
  useParams: () => ({ id: "a1" }),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { get: vi.fn(), update: vi.fn(), create: vi.fn() }, versions: { list: vi.fn(), create: vi.fn() } } };
});
vi.mock("@/lib/tools", () => ({ listTools: vi.fn() }));
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, list: vi.fn(() => Promise.resolve([])), test: vi.fn() } };
});
vi.mock("@/components/agents/specSections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/agents/specSections")>();
  return { ...actual, fetchProcessorCatalog: vi.fn(), fetchModels: vi.fn() };
});

import { api, type AgentSpec } from "@/lib/api";
import { listTools, type ToolDefinition } from "@/lib/tools";
import { fetchModels, fetchProcessorCatalog, stepForErrorMessage } from "@/components/agents/specSections";
import { modelsFixture, processorCatalogFixture } from "@/components/agents/specSections.fixtures";
import { setWindowWidth, TEST_WIDE_WIDTH } from "@/components/frame/frameTesting";
import { EditorTab } from "@/components/agentPage/EditorTab";
import { EDITOR_STEPS, stepForError } from "./registry";

const TOOLS: ToolDefinition[] = [
  { id: "clock.now", title: "Clock", description: "", side_effect: false, external_idempotent: true, input_schema: {}, allowed: true },
  { id: "tickets.create", title: "Create ticket", description: "", side_effect: true, external_idempotent: false, input_schema: {}, allowed: true },
];
const MODELS = [...modelsFixture, { id: "test:other", provider: "test", name: "other", wildcard: false, source: "allowlist", description: null }];
// A stored agent from before AC: none of the four features, keys in the order the API dumps them.
const LEGACY = {
  spec_version: 1,
  name: "Support bot",
  instructions: "Help people",
  model: { provider: "test", name: "echo" },
  tools: ["clock.now", "tickets.create"],
  model_settings: { temperature: null, max_tokens: null },
} as unknown as AgentSpec;
// Recorded from the pre-change editor (HEAD): the JSON an untouched save of LEGACY produces.
const LEGACY_JSON =
  '{"spec_version":1,"name":"Support bot","instructions":"Help people","model":{"provider":"test","name":"echo"},"tools":["clock.now","tickets.create"],"model_settings":{"temperature":null,"max_tokens":null}}';
const sha = (text: string) => createHash("sha256").update(text).digest("hex");
const agentWith = (spec: AgentSpec) => ({ id: "a1", name: spec.name, draft_spec: spec, latest_version: 1 });

let restoreWidth: () => void = () => undefined;
beforeEach(() => {
  vi.clearAllMocks();
  nav.search = "";
  restoreWidth = setWindowWidth(TEST_WIDE_WIDTH);
  vi.mocked(api.agents.get).mockResolvedValue(agentWith(LEGACY));
  vi.mocked(api.agents.update).mockImplementation(async (_id, spec) => agentWith(spec));
  vi.mocked(api.versions.list).mockResolvedValue([]);
  vi.mocked(listTools).mockResolvedValue(TOOLS);
  vi.mocked(fetchModels).mockResolvedValue(MODELS);
  vi.mocked(fetchProcessorCatalog).mockResolvedValue(processorCatalogFixture);
});
afterEach(() => restoreWidth());

async function renderEditor() {
  const view = render(<EditorTab agentId="a1" />);
  await screen.findByTestId("agent-stepper");
  await act(async () => undefined);
  return view;
}
const stepNav = () => screen.getByRole("navigation", { name: "Editor steps" });
const navButton = (label: RegExp) => within(stepNav()).getByRole("button", { name: label });
const region = (label: string) => screen.getByRole("region", { name: label });
const lastUpdated = () => vi.mocked(api.agents.update).mock.calls.at(-1)?.[1] as unknown as Record<string, unknown>;

describe("spec stability", () => {
  it("an untouched legacy agent saves byte-identical JSON (recorded fixture and hash)", async () => {
    expect(JSON.stringify(LEGACY)).toBe(LEGACY_JSON);
    await renderEditor();
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Support bot 2" } });
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "Support bot" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.agents.update).toHaveBeenCalled());
    const text = JSON.stringify(lastUpdated());
    expect(text).toBe(LEGACY_JSON);
    expect(sha(text)).toBe(sha(LEGACY_JSON));
    for (const key of ["output", "approvals", "limits", "fallback_models"]) expect(lastUpdated()).not.toHaveProperty(key);
  });
});

describe("step order and deep link", () => {
  it("identity … memory, output; output is last", () => {
    expect(EDITOR_STEPS.map((step) => step.id)).toEqual(["identity", "instructions", "tools", "request-context", "processors", "knowledge", "memory", "output"]);
  });

  it("?step=output opens Output; Previous is Memory and there is no Next", async () => {
    nav.search = "step=output";
    await renderEditor();
    expect(navButton(/^Output/)).toHaveAttribute("aria-current", "step");
    expect(region("Output")).toBeVisible();
    expect(screen.getByTestId("stepper-previous")).toHaveAccessibleName("Previous step: Memory");
    expect(screen.queryByTestId("stepper-next")).toBeNull();
  });

  it("Memory's Next is Output, and Output's status is Off", async () => {
    nav.search = "step=memory";
    await renderEditor();
    expect(screen.getByTestId("stepper-next")).toHaveAccessibleName("Next step: Output");
    expect(navButton(/^Output/)).toHaveTextContent("Off");
  });
});

describe("error mapping", () => {
  it.each([
    ["output.json_schema: unsupported keyword '$ref'", "output"],
    ["Value error, output.mode: must be one of tool, native, prompted", "output"],
    ["limits.max_requests: must be at most 200", "identity"],
    ["Value error, limits.max_cost_usd: must be positive", "identity"],
    ["fallback_models[0]: duplicates the primary model", "identity"],
    ["Value error, fallback_models[1]: not in the allowlist", "identity"],
    ["approvals.tools[0]: 'x' is not one of the agent's tools", "tools"],
    ["Value error, approvals.timeout_hours: must be at most 168", "tools"],
    // The API's and eap_schemas' own wording (review 2026-11-15).
    ["fallback_models[0]: model 'test:nope' is not allowed on this platform (not in the EAP_ALLOWED_MODELS allowlist)", "identity"],
    ["fallback_models[1]: model 'anthropic:claude' does not accept a temperature setting; remove model_settings.temperature or this fallback model", "identity"],
    ["Value error, fallback_models[0]: fallback model test:echo is the agent's primary model", "identity"],
    ["Value error, fallback_models[1]: duplicate fallback model: test:other", "identity"],
    ["Value error, approvals.tools[0]: 'gone.tool' is not a tool of this agent", "tools"],
    ["Value error, approvals.tools[0]: 'retrieve' is a platform tool and cannot require approval", "tools"],
    ["Value error, duplicate approval tool: tickets.create", "tools"],
    ["Value error, max_cost_usd must be between 0.0001 and 1000", "identity"],
  ])("a 422 %j opens the %s step", (message, step) => {
    expect(stepForError(message)).toBe(step);
    expect(stepForErrorMessage(message)).toBe(step === "output" ? null : step);
  });

  it.each([
    [["body", "spec", "output", "json_schema"], "output"],
    [["body", "spec", "limits", "max_requests"], "identity"],
    [["body", "spec", "fallback_models", 0], "identity"],
    [["body", "spec", "approvals", "tools", 0], "tools"],
  ])("a location %j opens %s", (location, step) => {
    expect(stepForError("Value error, anything", EDITOR_STEPS, [location as (string | number)[]])).toBe(step);
  });
});

describe("editor flow: all four features, saved and published", () => {
  it("sets output, limits, a fallback and an approval, saves the expected spec, then publishes with warnings", async () => {
    const warning = { code: "cost_cap_unpriced_model", path: "model", message: "model: 'test:echo' has no price" };
    vi.mocked(api.versions.create).mockResolvedValueOnce({ agent_id: "a1", version: 1, spec: LEGACY, spec_hash: "h1", created_at: "2026-11-15T00:00:00Z", created_by: "x", warnings: [warning] } as never);
    await renderEditor();
    // Identity: a limit, a cost cap and one fallback model.
    fireEvent.click(screen.getByRole("button", { name: /Limits and fallback models/ }));
    fireEvent.change(screen.getByLabelText(/^Model requests/), { target: { value: "8" } });
    fireEvent.change(screen.getByLabelText(/^Cost \(USD/), { target: { value: "0.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Add fallback model" }));
    await act(async () => undefined);
    fireEvent.change(screen.getByLabelText("Fallback 1"), { target: { value: "test:other" } });
    // Tools: gate the side-effect tool with a timeout.
    fireEvent.click(navButton(/^Tools/));
    fireEvent.click(await screen.findByLabelText("Require approval for tickets.create"));
    fireEvent.change(screen.getByLabelText(/^Approval timeout/), { target: { value: "6" } });
    // Output: on, with citations, native mode.
    fireEvent.click(navButton(/^Output/));
    fireEvent.click(screen.getByLabelText("Structured output"));
    fireEvent.click(screen.getByRole("button", { name: "Add citations field" }));
    fireEvent.click(screen.getByRole("radio", { name: /^Native/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(api.agents.update).toHaveBeenCalledTimes(1));
    expect(lastUpdated()).toEqual({
      ...(LEGACY as unknown as Record<string, unknown>),
      output: {
        json_schema: {
          type: "object",
          properties: {
            answer: { type: "string", description: "The answer" },
            citations: { type: "array", description: "Source numbers [n] used", items: { type: "integer" } },
          },
          required: ["answer"],
        },
        mode: "native",
      },
      approvals: { tools: ["tickets.create"], timeout_hours: 6 },
      limits: { max_requests: 8, max_cost_usd: "0.5" },
      fallback_models: [{ provider: "test", name: "other" }],
    });
    // Publish is disabled until the saved draft is clean again.
    await waitFor(() => expect(screen.getByRole("button", { name: "Publish" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    expect(await screen.findByText("Version 1 published")).toBeInTheDocument();
    expect(api.versions.create).toHaveBeenCalledWith("a1");
    expect(screen.getByTestId("publish-warnings")).toHaveTextContent(warning.message);
    expect(screen.getByTestId("publish-warnings").querySelector(`li[data-warning-code="cost_cap_unpriced_model"]`)).not.toBeNull();
  });

  it("a publish without warnings shows no warnings box", async () => {
    vi.mocked(api.versions.create).mockResolvedValueOnce({ agent_id: "a1", version: 1, spec: LEGACY, spec_hash: "h1", created_at: "2026-11-15T00:00:00Z", created_by: "x" } as never);
    await renderEditor();
    fireEvent.click(await screen.findByRole("button", { name: "Publish" }));
    expect(await screen.findByText("Version 1 published")).toBeInTheDocument();
    expect(screen.queryByTestId("publish-warnings")).toBeNull();
  });
});

describe("phone width (375 px)", () => {
  it("the Output step, limits group and approvals open from the step select without fixed widths", async () => {
    restoreWidth();
    restoreWidth = setWindowWidth(375);
    vi.mocked(api.agents.get).mockResolvedValue(
      agentWith({ ...LEGACY, output: { json_schema: { type: "object", properties: { a: { type: "string" } } } }, approvals: { tools: ["tickets.create"] }, limits: { max_requests: 5 } } as unknown as AgentSpec),
    );
    await renderEditor();
    const select = screen.getByTestId("stepper-select") as HTMLSelectElement;
    expect(Array.from(select.options).map((option) => option.value).at(-1)).toBe("output");
    expect(screen.getByText("Step 1 of 8")).toBeInTheDocument();
    expect(select.options[7].textContent).toContain("Tool mode");
    fireEvent.change(select, { target: { value: "output" } });
    expect(screen.getByText("Step 8 of 8")).toBeInTheDocument();
    const surfaces = document.querySelectorAll('[data-testid="limits-fallback-group"] *, [data-testid="approval-toggle"] *, [data-testid="approval-settings"] *, section[data-step="output"] *');
    expect(surfaces.length).toBeGreaterThan(0);
    const offenders = Array.from(surfaces)
      .map((el) => el.getAttribute("class") ?? "")
      .filter((cls) => /(^|\s)(w|min-w)-\[\d+px\]/.test(cls) || /(^|\s)w-(64|72|80|96)(\s|$)/.test(cls));
    expect(offenders).toEqual([]);
  });
});
