// W1.1: the agent editor's model settings fields (top_p, top_k, penalties, seed, max_steps, max_retries).
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen } from "@testing-library/react";
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

import type { AgentSpec } from "@/lib/api";
import { listTools } from "@/lib/tools";
import { mcpServers } from "@/lib/mcpServers";
import { fetchModels, fetchProcessorCatalog } from "@/components/agents/specSections";
import { modelsFixture, processorCatalogFixture } from "@/components/agents/specSections.fixtures";
import { AgentEditor } from "../AgentEditor";
import { draftFromSpec, draftToSpec } from "./editorDraft";

const PLAIN = {
  spec_version: 1,
  name: "Support bot",
  instructions: "Help people",
  model: { provider: "test", name: "echo" },
  tools: [],
  model_settings: { temperature: null, max_tokens: null },
} as unknown as AgentSpec;

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(mcpServers.list).mockResolvedValue([]);
  vi.mocked(listTools).mockResolvedValue([]);
  vi.mocked(fetchModels).mockResolvedValue(modelsFixture);
  vi.mocked(fetchProcessorCatalog).mockResolvedValue(processorCatalogFixture);
});

function Host({ initial, onSubmit }: { initial: AgentSpec; onSubmit: (spec: AgentSpec) => Promise<void> }) {
  const [step, setStep] = React.useState("identity");
  return <AgentEditor initialData={initial} onSubmit={onSubmit} submitLabel="Save draft" stepper={{ activeStep: step, onStepChange: setStep, layout: "nav" }} />;
}

async function setup(initial: AgentSpec) {
  const saved: Record<string, unknown>[] = [];
  const onSubmit = vi.fn(async (spec: AgentSpec) => {
    saved.push(spec as unknown as Record<string, unknown>);
  });
  render(<Host initial={initial} onSubmit={onSubmit} />);
  await act(async () => undefined);
  return { saved, onSubmit };
}
const save = async () => {
  fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
  await act(async () => undefined);
};
const type = (label: RegExp, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe("model settings in the editor", () => {
  it("an untouched spec without the new settings saves exactly the same JSON (hash stays stable)", async () => {
    const { saved } = await setup(PLAIN);
    await save();
    expect(JSON.stringify(saved[0])).toBe(JSON.stringify(PLAIN));
    expect(saved[0].model_settings).toEqual({ temperature: null, max_tokens: null });
  });

  it("shows a labelled field for each new setting, filled from the spec", async () => {
    const stored = { ...PLAIN, model_settings: { temperature: 0.2, max_tokens: null, top_p: 0.9, top_k: 40, frequency_penalty: -1, presence_penalty: 0.5, seed: 0, max_steps: 6, max_retries: 2 } } as unknown as AgentSpec;
    await setup(stored);
    expect(screen.getByLabelText(/^Top P/)).toHaveValue(0.9);
    expect(screen.getByLabelText(/^Top K/)).toHaveValue(40);
    expect(screen.getByLabelText(/^Frequency penalty/)).toHaveValue(-1);
    expect(screen.getByLabelText(/^Presence penalty/)).toHaveValue(0.5);
    expect(screen.getByLabelText(/^Seed/)).toHaveValue(0);
    expect(screen.getByLabelText(/^Max steps/)).toHaveValue(6);
    expect(screen.getByLabelText(/^Max retries/)).toHaveValue(2);
  });

  it("saves the typed settings as numbers and leaves emptied ones out", async () => {
    const { saved } = await setup(PLAIN);
    type(/^Top P/, "0.8");
    type(/^Top K/, "25");
    type(/^Seed/, "11");
    type(/^Max steps/, "4");
    type(/^Max retries/, "0");
    await save();
    expect(saved[0].model_settings).toEqual({ temperature: null, max_tokens: null, top_p: 0.8, top_k: 25, seed: 11, max_steps: 4, max_retries: 0 });
    type(/^Top P/, "");
    await save();
    expect(saved[1].model_settings).not.toHaveProperty("top_p");
  });

  it.each([
    [/^Top P/, "1.5", "Top P must be 0 to 1"],
    [/^Top K/, "0", "Top K must be 1 or more"],
    [/^Frequency penalty/, "3", "Frequency penalty must be -2 to 2"],
    [/^Presence penalty/, "-2.5", "Presence penalty must be -2 to 2"],
    [/^Seed/, "1.5", "Seed must be a whole number"],
    [/^Max steps/, "0", "Max steps must be 1 to 200"],
    [/^Max retries/, "11", "Max retries must be 0 to 10"],
  ])("an out-of-range value shows an inline message and nothing is saved (%s)", async (label, value, message) => {
    const { onSubmit } = await setup(PLAIN);
    type(label, value);
    const input = screen.getByLabelText(label);
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(input).toHaveAccessibleDescription(expect.stringContaining(message));
    await save();
    expect(onSubmit).not.toHaveBeenCalled();
  });
});

describe("editor draft conversion", () => {
  it("round-trips the new settings and drops them when empty", () => {
    const stored = { ...PLAIN, model_settings: { temperature: null, max_tokens: null, seed: 3 } } as unknown as AgentSpec;
    const draft = draftFromSpec(stored);
    expect(draft.modelExtras.seed).toBe("3");
    expect(draftToSpec(draft, stored).model_settings).toEqual({ temperature: null, max_tokens: null, seed: 3 });
    expect(draftToSpec({ ...draft, modelExtras: { ...draft.modelExtras, seed: "" } }, stored).model_settings).toEqual({ temperature: null, max_tokens: null });
  });
});
