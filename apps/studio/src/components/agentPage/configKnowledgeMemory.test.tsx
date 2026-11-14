// RM.17: the Config panel's Knowledge and Memory sections (with their editor "Edit" links) and the
// memory card, which reads the same spec helper as the editor's Memory step.
import React from "react";
import "@testing-library/jest-dom";
import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(), listEmbeddingModels: vi.fn() }));

import type { Agent, AgentSpec } from "@/lib/api";
import { listKnowledgeBases, type KnowledgeBase } from "@/lib/knowledge";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { ConfigPanel } from "./ConfigPanel";
import { MemoryCard } from "./MemoryCard";

const KB1 = "11111111-1111-4111-8111-111111111111";
const KB2 = "22222222-2222-4222-8222-222222222222";
const PLAIN = { spec_version: 1, name: "Bot", instructions: "Help", model: { provider: "test", name: "echo" }, tools: [] } as unknown as AgentSpec;
const FULL = {
  ...PLAIN,
  knowledge_bases: [{ id: KB1, top_k: 5, min_score: 0.3, filter: { lang: "en" } }, { id: KB2, top_k: 8 }],
  memory: {
    history: { last_turns: 12 },
    working: { scope: "resource", read_only: false },
    semantic_recall: { embedding_model: "test:hash", top_k: 4, message_range: 1, scope: "resource" },
  },
} as unknown as AgentSpec;
const agent = (spec: AgentSpec): Agent => ({ id: "a1", name: "Bot", draft_spec: spec, latest_version: null });

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(listKnowledgeBases).mockResolvedValue([{ id: KB1, name: "Product docs" } as KnowledgeBase]);
});

describe("Config panel", () => {
  it("shows None and Off without knowledge or memory, and fetches nothing", () => {
    render(<ConfigPanel agent={agent(PLAIN)} versions={[]} />);
    expect(within(screen.getByRole("region", { name: /^Knowledge/ })).getByText("None")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: /^Memory/ })).getByText("Off")).toBeInTheDocument();
    expect(listKnowledgeBases).not.toHaveBeenCalled();
  });

  it("lists the knowledge bases by name (a deleted one as such, with a short id) with their settings, and the memory features", async () => {
    render(<ConfigPanel agent={agent(FULL)} versions={[]} />);
    const knowledge = screen.getByRole("region", { name: /^Knowledge/ });
    expect(within(knowledge).getByRole("heading", { name: "Knowledge 2" })).toBeInTheDocument();
    expect(await within(knowledge).findByText("Product docs")).toBeInTheDocument();
    const deleted = within(knowledge).getByTestId("config-knowledge-deleted");
    expect(deleted).toHaveTextContent(`Deleted knowledge base ${KB2.slice(0, 8)}…`);
    expect(deleted).toHaveAttribute("title", KB2);
    expect(within(knowledge).getByText("Top 5 · min score 0.3 · filter lang=en")).toBeInTheDocument();
    const memory = within(screen.getByTestId("config-memory"));
    expect(memory.getByText("Last 12 turns")).toBeInTheDocument();
    expect(memory.getByText("Default template, per user")).toBeInTheDocument();
    expect(memory.getByText("test:hash, top 4, same user")).toBeInTheDocument();
    expect(memory.getByText("Same as input redaction")).toBeInTheDocument();
    expect(memory.queryByText("Observational")).toBeNull();
  });

  it("Edit links open the Knowledge and Memory steps", () => {
    render(<ConfigPanel agent={agent(FULL)} versions={[]} />);
    expect(screen.getByRole("link", { name: "Edit knowledge" })).toHaveAttribute("href", routes.agentEditor("a1", { step: "knowledge" }));
    expect(screen.getByRole("link", { name: "Edit memory" })).toHaveAttribute("href", routes.agentEditor("a1", { step: "memory" }));
  });

  it("has no axe violations with both sections filled", async () => {
    const { container } = render(<ConfigPanel agent={agent(FULL)} versions={[]} />);
    await screen.findByText("Product docs");
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

describe("memory card", () => {
  const card = () => screen.getByRole("region", { name: "Memory status" });
  const state = (label: string) => within(card()).getByText(label).closest("[data-state]")?.getAttribute("data-state");

  it("without memory: History (all turns) on, the rest off", () => {
    render(<MemoryCard spec={PLAIN} />);
    expect(state("History")).toBe("on");
    expect(state("Working memory")).toBe("off");
  });

  it("memory on with defaults shows the effective window (20 turns), as the Memory step does", () => {
    render(<MemoryCard spec={{ ...PLAIN, memory: {} } as unknown as AgentSpec} />);
    expect(state("History 20 turns")).toBe("on");
  });

  it("reads each feature from the spec", () => {
    render(<MemoryCard spec={FULL} />);
    expect([state("History 12 turns"), state("Working memory"), state("Observational"), state("Semantic recall")]).toEqual(["on", "on", "off", "on"]);
  });
});
