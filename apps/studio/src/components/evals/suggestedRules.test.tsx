import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import type { EvalTargetRef } from "@/lib/evals";
import { SuggestedRules } from "./SuggestedRules";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const T = "2026-11-19T10:00:00Z";
const AGENT = { kind: "agent" as const, id: "a1", name: "Support" };
const WORKFLOW = { kind: "workflow" as const, id: "w1", name: "Triage" };

const SUGGESTIONS = {
  model: "test:echo",
  dropped: 2,
  suggestions: [
    { title: "No refund promises", rationale: "The instructions forbid promising refunds.", scope: "both", evidence: "instructions line 2", spec: { kind: "regex", pattern: "(?i)refund", negate: true } },
    { title: "Stay cheap", rationale: "Runs cost cents.", scope: "scorer", evidence: "run 1", spec: { kind: "max_cost", usd: "0.05" } },
    { title: "Answers in time", rationale: "Support is interactive.", scope: "evaluator", evidence: "", spec: { kind: "max_duration", seconds: 30 } },
  ],
};
const SUITE = { id: "s1", agent_id: "a1", name: "Smoke", description: null, evaluators: [{ kind: "equals_expected" }], case_count: 2, archived: false, archived_at: null, created_at: T, updated_at: T };

let calls: { method: string; path: string; body: any }[];
let suggestionsReply: () => Response;
let suites: unknown[];

beforeEach(() => {
  calls = [];
  suites = [SUITE];
  suggestionsReply = () => json(SUGGESTIONS);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const path = url.pathname.replace("/api/v1", "");
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ method, path, body });
    if (path.endsWith("/scorer-suggestions")) return suggestionsReply();
    if (path === "/scorers" && method === "POST") return json({ id: "sc1", name: body.name, description: body.description, evaluator: body.evaluator, agent_ids: [], created_at: T, updated_at: T }, 201);
    if (path === "/agents/a1/scorers/sc1" && method === "PUT") return json({ items: [] });
    if (path === "/agents/a1/eval-suites" && method === "GET") return json({ items: suites });
    if (path === "/workflows/w1/evals/suites" && method === "GET") return json({ items: [] });
    if (path === "/agents/a1/eval-suites" && method === "POST") return json({ ...SUITE, id: "s2", name: body.name, evaluators: body.evaluators }, 201);
    if (path === "/workflows/w1/evals/suites" && method === "POST") return json({ ...SUITE, id: "s3", agent_id: null, name: body.name, evaluators: body.evaluators }, 201);
    if (path === "/eval-suites/s1" && method === "PATCH") return json({ ...SUITE, evaluators: body.evaluators });
    return json({ detail: "missing" }, 404);
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

async function suggest(target: EvalTargetRef = AGENT, onChanged = vi.fn()) {
  const view = render(<SuggestedRules target={target} onChanged={onChanged} />);
  fireEvent.click(screen.getByTestId("suggest-rules"));
  await screen.findAllByTestId("suggested-rule");
  return { ...view, onChanged };
}
const card = (title: string) => screen.getAllByTestId("suggested-rule").find((c) => within(c).queryByRole("heading", { name: title }))!;

describe("SuggestedRules", () => {
  it("shows nothing until asked, then a card per suggestion with kind, scope and the rule in words", async () => {
    render(<SuggestedRules target={AGENT} />);
    expect(screen.queryByTestId("suggested-rule")).toBeNull();
    expect(calls).toEqual([]);
    fireEvent.click(screen.getByTestId("suggest-rules"));
    await screen.findAllByTestId("suggested-rule");
    expect(calls.find((c) => c.path === "/agents/a1/scorer-suggestions")?.body).toEqual({ max: 8, include_runs: true });
    const refund = card("No refund promises");
    expect(within(refund).getByTestId("suggested-rule-kind")).toHaveTextContent("Matches regex");
    expect(within(refund).getByTestId("suggested-rule-spec")).toHaveTextContent("Output must not match /(?i)refund/");
    expect(within(refund).getByText("The instructions forbid promising refunds.")).toBeInTheDocument();
    expect(within(refund).getByText(/instructions line 2/)).toBeInTheDocument();
    expect(within(card("Stay cheap")).getByTestId("suggested-rule-spec")).toHaveTextContent("At most $0.05");
    expect(screen.getByText(/2 suggestions were left out/)).toBeInTheDocument();
    expect(screen.getByTestId("suggest-rules")).toHaveTextContent("Suggest again");
  });

  it("Add as scorer creates a scorer named from the title, attaches it at 1.0 and marks the card Added", async () => {
    const { onChanged } = await suggest();
    fireEvent.click(within(card("Stay cheap")).getByTestId("suggested-rule-add-scorer"));
    await waitFor(() => expect(within(card("Stay cheap")).getByTestId("suggested-rule-added")).toHaveTextContent("Added as scorer “Stay cheap”"));
    const create = calls.find((c) => c.method === "POST" && c.path === "/scorers")!;
    expect(create.body).toEqual({ name: "Stay cheap", description: "Runs cost cents.", evaluator: { kind: "max_cost", usd: "0.05" } });
    expect(calls.find((c) => c.method === "PUT")).toMatchObject({ path: "/agents/a1/scorers/sc1", body: { sampling_rate: 1 } });
    expect(onChanged).toHaveBeenCalled();
    expect(within(card("Stay cheap")).queryByTestId("suggested-rule-add-scorer")).toBeNull();
  });

  it("an evaluator-only suggestion has no Add as scorer", async () => {
    await suggest();
    const slow = card("Answers in time");
    expect(within(slow).queryByTestId("suggested-rule-add-scorer")).toBeNull();
    expect(within(slow).getByTestId("suggested-rule-add-suite")).toBeInTheDocument();
  });

  it("Add to suite appends the evaluator to the chosen suite", async () => {
    await suggest();
    const refund = card("No refund promises");
    fireEvent.click(within(refund).getByTestId("suggested-rule-add-suite"));
    fireEvent.change(within(refund).getByTestId("suggested-rule-suite"), { target: { value: "s1" } });
    fireEvent.click(within(refund).getByTestId("suggested-rule-confirm"));
    await waitFor(() => expect(within(card("No refund promises")).getByTestId("suggested-rule-added")).toHaveTextContent("Added to suite “Smoke”"));
    expect(calls.find((c) => c.method === "PATCH")).toMatchObject({
      path: "/eval-suites/s1",
      body: { evaluators: [{ kind: "equals_expected" }, { kind: "regex", pattern: "(?i)refund", negate: true }] },
    });
  });

  it("Add to suite can create the “<name> rules” suite with the evaluator", async () => {
    await suggest();
    const refund = card("No refund promises");
    fireEvent.click(within(refund).getByTestId("suggested-rule-add-suite"));
    expect(within(refund).getByTestId("suggested-rule-suite")).toHaveValue("__new__");
    fireEvent.click(within(refund).getByTestId("suggested-rule-confirm"));
    await waitFor(() => expect(within(card("No refund promises")).getByTestId("suggested-rule-added")).toHaveTextContent("Added to suite “Support rules”"));
    expect(calls.find((c) => c.method === "POST" && c.path === "/agents/a1/eval-suites")?.body).toEqual({
      name: "Support rules", description: null, evaluators: [{ kind: "regex", pattern: "(?i)refund", negate: true }],
    });
  });

  it("a workflow's cards are evaluator-only and create the suite on the workflow", async () => {
    await suggest(WORKFLOW);
    expect(calls.find((c) => c.path === "/workflows/w1/scorer-suggestions")).toBeTruthy();
    expect(screen.queryByTestId("suggested-rule-add-scorer")).toBeNull();
    const refund = card("No refund promises");
    fireEvent.click(within(refund).getByTestId("suggested-rule-add-suite"));
    fireEvent.click(within(refund).getByTestId("suggested-rule-confirm"));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.path === "/workflows/w1/evals/suites")).toBe(true));
  });

  it("Dismiss removes the card", async () => {
    await suggest();
    fireEvent.click(within(card("Stay cheap")).getByTestId("suggested-rule-dismiss"));
    expect(screen.queryByRole("heading", { name: "Stay cheap" })).toBeNull();
    expect(screen.getAllByTestId("suggested-rule")).toHaveLength(2);
  });

  it("a scorer name already in use gets a numeric suffix", async () => {
    const original = fetch as unknown as ReturnType<typeof vi.fn>;
    let first = true;
    vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
      if (String(input).endsWith("/scorers") && init?.method === "POST" && first) {
        first = false;
        return json({ detail: "a scorer with this name already exists" }, 409);
      }
      return original(input, init);
    }));
    await suggest();
    fireEvent.click(within(card("Stay cheap")).getByTestId("suggested-rule-add-scorer"));
    await waitFor(() => expect(within(card("Stay cheap")).getByTestId("suggested-rule-added")).toHaveTextContent("“Stay cheap (2)”"));
  });

  it("says so when no model is allowed, and offers no cards", async () => {
    suggestionsReply = () => json({ detail: "No model is available for the scorer suggester.", code: "no_model" }, 409);
    render(<SuggestedRules target={AGENT} />);
    fireEvent.click(screen.getByTestId("suggest-rules"));
    expect(await screen.findByTestId("suggest-rules-error")).toHaveTextContent(/No model is available/);
    expect(screen.queryByTestId("suggested-rule")).toBeNull();
  });

  it("an empty answer explains itself", async () => {
    suggestionsReply = () => json({ suggestions: [], model: "test:echo", dropped: 0 });
    render(<SuggestedRules target={AGENT} />);
    fireEvent.click(screen.getByTestId("suggest-rules"));
    expect(await screen.findByTestId("suggest-rules-empty")).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    const { container } = await suggest();
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
