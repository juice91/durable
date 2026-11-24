// Agents as tools: the Tools step's "Agents as tools" section: add a published agent, edit its settings,
// round trip, inline and server errors, the nav status.
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  usePathname: () => "/agents/a1/editor",
  useSearchParams: () => new URLSearchParams(),
  useParams: () => ({ id: "a1" }),
}));
vi.mock("@/lib/tools", () => ({ listTools: vi.fn(() => Promise.resolve([])) }));
vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(() => Promise.resolve([])), listEmbeddingModels: vi.fn(() => Promise.resolve([])) }));
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, list: vi.fn(() => Promise.resolve([])), test: vi.fn() } };
});
vi.mock("@/components/agents/specSections", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/components/agents/specSections")>();
  return { ...actual, fetchProcessorCatalog: vi.fn(), fetchModels: vi.fn() };
});
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, agents: { ...actual.api.agents, list: vi.fn() } } };
});

import { api, type Agent, type AgentSpec } from "@/lib/api";
import { fetchModels, fetchProcessorCatalog } from "@/components/agents/specSections";
import { modelsFixture, processorCatalogFixture } from "@/components/agents/specSections.fixtures";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { chooseValue } from "@/test/select";
import { AgentEditor } from "../AgentEditor";

const RESEARCH = "11111111-1111-4111-8111-111111111111";
const SUPPORT = "22222222-2222-4222-8222-222222222222";
const DRAFT_ONLY = "33333333-3333-4333-8333-333333333333";
const ARCHIVED = "44444444-4444-4444-8444-444444444444";
const GONE = "55555555-5555-4555-8555-555555555555";

const agent = (id: string, name: string, extra: Partial<Agent> = {}) =>
  ({ id, name, draft_spec: {} as AgentSpec, latest_version: 3, archived: false, ...extra }) as Agent;
const AGENTS = [
  agent(RESEARCH, "Research Helper"),
  agent(SUPPORT, "Support", { latest_version: 1 }),
  agent(DRAFT_ONLY, "Draft only", { latest_version: null }),
  agent(ARCHIVED, "Old one", { archived: true }),
  agent("a1", "Boss itself"), // the agent being edited (useParams mocks id "a1")
];

const PLAIN = {
  spec_version: 1,
  name: "Boss",
  instructions: "Delegate",
  model: { provider: "test", name: "echo" },
  tools: [],
  model_settings: { temperature: null, max_tokens: null },
} as unknown as AgentSpec;

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(api.agents.list).mockResolvedValue(AGENTS);
  vi.mocked(fetchModels).mockResolvedValue(modelsFixture);
  vi.mocked(fetchProcessorCatalog).mockResolvedValue(processorCatalogFixture);
});

function Host({ initial, onSubmit, serverError = null }: { initial: AgentSpec; onSubmit: (spec: AgentSpec) => Promise<void>; serverError?: string | null }) {
  const [step, setStep] = React.useState("tools");
  return <AgentEditor initialData={initial} onSubmit={onSubmit} submitLabel="Save draft" serverError={serverError} stepper={{ activeStep: step, onStepChange: setStep, layout: "nav" }} />;
}

function setup(initial: AgentSpec, props: Partial<React.ComponentProps<typeof Host>> = {}) {
  const saved: AgentSpec[] = [];
  const onSubmit = vi.fn(async (spec: AgentSpec) => {
    saved.push(spec);
  });
  const view = render(<Host initial={initial} onSubmit={onSubmit} {...props} />);
  return { ...view, saved, onSubmit };
}

const stepNav = () => screen.getByRole("navigation", { name: "Editor steps" });
const navButton = (label: RegExp) => within(stepNav()).getByRole("button", { name: label });
const save = async () => {
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  await act(async () => undefined);
};
const toolsOf = (spec: AgentSpec) => (spec as unknown as { agent_tools?: unknown[] }).agent_tools;

/** Picks an agent in the "Add an agent" combobox by its name. */
async function addAgent(name: string) {
  fireEvent.click(await screen.findByRole("combobox", { name: "Add an agent" }));
  fireEvent.click(await screen.findByRole("option", { name: new RegExp(name) }));
}

describe("Agents as tools section of the Tools step", () => {
  it("an agent without refs saves the same JSON, with no agent_tools key", async () => {
    const { saved } = setup(PLAIN);
    await screen.findByTestId("agent-tools-count");
    await save();
    expect(JSON.stringify(saved[0])).toBe(JSON.stringify(PLAIN));
    expect(navButton(/^Tools/)).toHaveTextContent("None");
  });

  it("offers only published, non-archived agents, adds one in the default shape and removing it restores the JSON", async () => {
    const { saved } = setup(PLAIN);
    fireEvent.click(await screen.findByRole("combobox", { name: "Add an agent" }));
    const options = (await screen.findAllByRole("option")).map((o) => o.textContent ?? "");
    expect(options.some((t) => t.includes("Research Helper"))).toBe(true);
    expect(options.some((t) => t.includes("Support"))).toBe(true);
    expect(options.some((t) => t.includes("Draft only"))).toBe(false);
    expect(options.some((t) => t.includes("Old one"))).toBe(false);
    expect(options.some((t) => t.includes("Boss itself"))).toBe(false); // an agent cannot call itself
    fireEvent.click(screen.getByRole("option", { name: /Research Helper/ }));

    expect(navButton(/^Tools/)).toHaveTextContent("1 tool");
    expect(screen.getByTestId("agent-tools-count")).toHaveTextContent("1 added (max 8)");
    expect(screen.getByRole("heading", { name: "Research Helper" })).toBeInTheDocument();
    // the placeholder names the derived tool name
    expect(screen.getByLabelText("Tool name")).toHaveAttribute("placeholder", "research_helper");
    await save();
    expect(toolsOf(saved[0])).toEqual([{ agent_id: RESEARCH }]);

    fireEvent.click(screen.getByRole("button", { name: "Remove Research Helper" }));
    expect(navButton(/^Tools/)).toHaveTextContent("None");
    await save();
    expect(JSON.stringify(saved[1])).toBe(JSON.stringify(PLAIN));
  });

  it("edits name, description, version, timeout and max calls; defaults stay out of the JSON", async () => {
    const { saved } = setup(PLAIN);
    await addAgent("Research Helper");
    fireEvent.change(screen.getByLabelText("Tool name"), { target: { value: "ask_research" } });
    fireEvent.change(screen.getByLabelText("Description (optional)"), { target: { value: "Ask for sources." } });
    chooseValue(screen.getByRole("combobox", { name: "Version" }), "pinned");
    expect(screen.getByLabelText("Pinned version")).toHaveValue(3); // the agent's latest published version
    fireEvent.change(screen.getByLabelText("Pinned version"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Timeout in seconds (1–3600)"), { target: { value: "60" } });
    fireEvent.change(screen.getByLabelText("Max calls per run (1–20)"), { target: { value: "2" } });
    await save();
    expect(JSON.stringify(toolsOf(saved[0]))).toBe(
      JSON.stringify([{ agent_id: RESEARCH, name: "ask_research", description: "Ask for sources.", version: 2, timeout_seconds: 60, max_calls_per_run: 2 }]),
    );

    // back to the defaults: latest, 300 s, 5 calls
    chooseValue(screen.getByRole("combobox", { name: "Version" }), "latest");
    fireEvent.change(screen.getByLabelText("Timeout in seconds (1–3600)"), { target: { value: "300" } });
    fireEvent.change(screen.getByLabelText("Max calls per run (1–20)"), { target: { value: "5" } });
    await save();
    expect(JSON.stringify(toolsOf(saved[1]))).toBe(JSON.stringify([{ agent_id: RESEARCH, name: "ask_research", description: "Ask for sources." }]));
  });

  it("an untouched stored list saves byte-identical JSON", async () => {
    const stored = { ...PLAIN, agent_tools: [{ agent_id: RESEARCH, version: 2, timeout_seconds: 60 }, { agent_id: SUPPORT }] } as unknown as AgentSpec;
    const { saved } = setup(stored);
    expect(await screen.findByRole("heading", { name: "Research Helper" })).toBeInTheDocument();
    expect(navButton(/^Tools/)).toHaveTextContent("2 tools");
    await save();
    expect(JSON.stringify(saved[0])).toBe(JSON.stringify(stored));
  });

  it("a bad name and an out-of-range timeout show inline, mark the step and block saving", async () => {
    const { onSubmit } = setup({ ...PLAIN, agent_tools: [{ agent_id: RESEARCH }] } as unknown as AgentSpec);
    const name = await screen.findByLabelText("Tool name");
    fireEvent.change(name, { target: { value: "Bad Name" } });
    expect(name).toHaveAccessibleDescription(/Name must start with a lower-case letter/);
    expect(name).toHaveAttribute("aria-invalid", "true");
    fireEvent.change(screen.getByLabelText("Timeout in seconds (1–3600)"), { target: { value: "9000" } });
    expect(screen.getByLabelText("Timeout in seconds (1–3600)")).toHaveAccessibleDescription(/Timeout must be a whole number from 1 to 3600/);
    expect(navButton(/^Tools/)).toHaveTextContent("2 problems");
    await save();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("names an agent that no longer exists, is archived or unpublished", async () => {
    setup({ ...PLAIN, agent_tools: [{ agent_id: GONE }, { agent_id: ARCHIVED }, { agent_id: DRAFT_ONLY }] } as unknown as AgentSpec);
    expect(await screen.findByRole("heading", { name: "Unknown agent" })).toBeInTheDocument();
    expect(screen.getByText(/does not exist in this tenant/)).toBeInTheDocument();
    expect(screen.getByText(/This agent is archived/)).toBeInTheDocument();
    expect(screen.getByText(/has no published version/)).toBeInTheDocument();
  });

  it("a publish refusal about agent tools opens the Tools step", async () => {
    setup(PLAIN, { serverError: "agent tool cycle: Boss → Helper → Boss (this publish would make it a loop)" });
    await act(async () => undefined);
    expect(navButton(/^Tools/)).toHaveAttribute("aria-current", "step");
  });

  it("says so when there is no published agent to add", async () => {
    vi.mocked(api.agents.list).mockResolvedValue([agent(DRAFT_ONLY, "Draft only", { latest_version: null })]);
    setup(PLAIN);
    expect(await screen.findByTestId("agent-tools-empty")).toHaveTextContent("No other published agents yet.");
  });

  it("shows a load error", async () => {
    vi.mocked(api.agents.list).mockRejectedValue(new Error("boom"));
    setup(PLAIN);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load agents: boom");
  });

  it("has no axe violations", async () => {
    const { container } = setup({ ...PLAIN, agent_tools: [{ agent_id: RESEARCH, version: 2 }] } as unknown as AgentSpec);
    await waitFor(() => expect(screen.getByRole("heading", { name: "Research Helper" })).toBeInTheDocument());
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
