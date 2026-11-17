// Task 21: the Config panel's Scorers section, from the agent's attached scorers.
import React from "react";
import "@testing-library/jest-dom";
import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/knowledge", () => ({ listKnowledgeBases: vi.fn(async () => []), listEmbeddingModels: vi.fn() }));
vi.mock("@/lib/scorers", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/scorers")>()), listAgentScorers: vi.fn() }));

import type { Agent, AgentSpec } from "@/lib/api";
import { listAgentScorers } from "@/lib/scorers";
import { routes } from "@/lib/routes";
import { ConfigPanel } from "./ConfigPanel";

const SPEC = { spec_version: 1, name: "Bot", instructions: "Help", model: { provider: "test", name: "echo" }, tools: [] } as unknown as AgentSpec;
const agent: Agent = { id: "a1", name: "Bot", draft_spec: SPEC, latest_version: null };
const attached = (id: string, name: string, sampling_rate: number) => ({ id, name, evaluator: { kind: "schema_valid" as const }, sampling_rate });

beforeEach(() => vi.clearAllMocks());

describe("Config panel: Scorers", () => {
  it("lists the attached scorers as name · rate with a count and an edit link to the editor", async () => {
    vi.mocked(listAgentScorers).mockResolvedValue([attached("s1", "Valid JSON", 1), attached("s2", "Polite", 0.25)]);
    render(<ConfigPanel agent={agent} versions={[]} />);
    const section = screen.getByRole("region", { name: /^Scorers/ });
    expect(await within(section).findByText("Valid JSON · 100%")).toBeInTheDocument();
    expect(within(section).getByText("Polite · 25%")).toBeInTheDocument();
    expect(within(section).getByRole("heading", { name: "Scorers 2" })).toBeInTheDocument();
    expect(within(section).getByRole("link", { name: "Edit scorers" })).toHaveAttribute("href", routes.agentEditor("a1"));
    expect(listAgentScorers).toHaveBeenCalledWith("a1", expect.anything());
  });

  it("sits after Processors", async () => {
    vi.mocked(listAgentScorers).mockResolvedValue([]);
    render(<ConfigPanel agent={agent} versions={[]} />);
    const names = screen.getAllByRole("region").map((el) => el.getAttribute("aria-labelledby")!).map((id) => document.getElementById(id)!.textContent ?? "");
    const at = (prefix: string) => names.findIndex((n) => n.startsWith(prefix));
    expect(at("Scorers")).toBe(at("Processors") + 1);
  });

  it("says None when no scorer is attached", async () => {
    vi.mocked(listAgentScorers).mockResolvedValue([]);
    render(<ConfigPanel agent={agent} versions={[]} />);
    const section = screen.getByRole("region", { name: /^Scorers/ });
    await waitFor(() => expect(within(section).getByText("None")).toBeInTheDocument());
  });

  it("says so when the scorers cannot be loaded", async () => {
    vi.mocked(listAgentScorers).mockRejectedValue(new Error("boom"));
    render(<ConfigPanel agent={agent} versions={[]} />);
    const section = screen.getByRole("region", { name: /^Scorers/ });
    expect(await within(section).findByText("Could not load the scorers")).toBeInTheDocument();
  });
});
