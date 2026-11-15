// Follow-up 3 / 1 / 4 acceptance (Studio): a turn's chat usage (turnUsage and the footer), the draft
// and moderation labels in traces, and the working memory probe (`?missing=null`).
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import "@testing-library/jest-dom";
import { turnUsage, type RunUsageRow } from "@/components/chat/runOptions";
import { TurnFooter, type TurnView } from "@/components/chat/TurnFooter";
import { SpanDetail } from "@/components/traces/SpanDetail";
import { spanLabel, type TraceSpan } from "@/lib/traces";
import { getWorkingMemory, listAgentWorkingMemories } from "@/lib/memory";
import type { RunEvent } from "@/lib/runs";

let seq = 0;
const ev = (type: string, data: Record<string, unknown>): RunEvent => ({ seq: ++seq, type, data, created_at: "2026-11-15T10:00:00Z" }) as unknown as RunEvent;
const usage = (i: number, o: number) => ({ input_tokens: i, output_tokens: o, cache_read_tokens: 0, cache_write_tokens: 0, measured: true });
const chat = (i: number, o: number, cost: string | null) => ev("model_message", { model_id: "test:chat", usage: usage(i, o), cost_usd: cost });
const moderation = ev("processor_result", { processor_type: "moderation", model_id: "test:mod", usage: usage(100, 9), cost_usd: "0.5" });
const memory = ev("observation_recorded", { usage: usage(500, 50), cost_usd: "0.9" });
const embedding = ev("embedding", { model_id: "test:hash", input_tokens: 300, cost_usd: "0.1" });

describe("turnUsage", () => {
  it("the recorded chat_* fields win over the run's all-operation totals, for tokens and cost", () => {
    const row: RunUsageRow = { input_tokens: 900, output_tokens: 90, cost_usd: "9", chat_input_tokens: 15, chat_output_tokens: 5, chat_cost_usd: "0.0012000000" };
    expect(turnUsage([chat(1, 1, "0.1")], row)).toMatchObject({ recorded: true, inputTokens: 15, outputTokens: 5, costUsd: 0.0012 });
  });

  it("moderation-only usage (0 / 0 / \"0\") is a recorded zero, not a fall back to the totals", () => {
    const row: RunUsageRow = { input_tokens: 107, output_tokens: 8, cost_usd: "0.7", chat_input_tokens: 0, chat_output_tokens: 0, chat_cost_usd: "0" };
    expect(turnUsage([], row)).toMatchObject({ recorded: true, inputTokens: 0, outputTokens: 0, costUsd: 0 });
  });

  it("an unpriced chat request has tokens and no cost; null chat_* falls back to the totals, then to events", () => {
    expect(turnUsage([], { chat_input_tokens: 10, chat_output_tokens: 2, chat_cost_usd: null })).toMatchObject({ inputTokens: 10, costUsd: null });
    expect(turnUsage([], { input_tokens: 40, output_tokens: 4, cost_usd: "0.3", chat_input_tokens: null, chat_output_tokens: null, chat_cost_usd: null }))
      .toMatchObject({ inputTokens: 40, outputTokens: 4, costUsd: 0.3 });
    expect(turnUsage([chat(3, 1, "0.1")], { chat_input_tokens: null, chat_output_tokens: null, chat_cost_usd: null })).toMatchObject({ recorded: true, inputTokens: 3 });
    expect(turnUsage([], null)).toMatchObject({ recorded: false });
  });

  it("a live turn sums the chat model's events only: not moderation, memory or embedding events", () => {
    const events = [moderation, chat(10, 2, "0.001"), memory, embedding, chat(5, 3, "0.0002")];
    const got = turnUsage(events, null);
    expect(got).toMatchObject({ recorded: true, inputTokens: 15, outputTokens: 5 });
    expect(got.costUsd).toBeCloseTo(0.0012, 10);
    expect(turnUsage([moderation, memory], null)).toMatchObject({ recorded: false, inputTokens: 0 });
    expect(turnUsage([chat(1, 1, "0.1"), chat(1, 1, null)], null).costUsd).toBeNull();
  });
});

describe("TurnFooter", () => {
  const view = (row: RunUsageRow, events: RunEvent[] = []): TurnView =>
    ({ runId: "r1", status: "completed", modelId: "test:chat", modelOverridden: false, draft: false, version: 2, usage: turnUsage(events, row), checking: false, block: null, liveText: true, announce: false }) as TurnView;

  it("shows the chat model's tokens and cost beside its name, and 0 for moderation-only usage", () => {
    const { rerender } = render(<TurnFooter turn={view({ input_tokens: 99, output_tokens: 9, cost_usd: "5", chat_input_tokens: 1500, chat_output_tokens: 20, chat_cost_usd: "0.25" })} />);
    const footer = screen.getByTestId("turn-footer");
    expect(within(footer).getByTestId("turn-badge-tokens")).toHaveTextContent("1,500 in · 20 out tokens");
    expect(within(footer).getByTestId("turn-badge-cost")).toHaveTextContent("$0.25");
    rerender(<TurnFooter turn={view({ chat_input_tokens: 0, chat_output_tokens: 0, chat_cost_usd: "0" })} />);
    expect(screen.getByTestId("turn-badge-tokens")).toHaveTextContent("0 in · 0 out tokens");
    rerender(<TurnFooter turn={view({ chat_input_tokens: 5, chat_output_tokens: 1, chat_cost_usd: null })} />);
    expect(screen.getByTestId("turn-badge-cost")).toHaveTextContent("no price");
    rerender(<TurnFooter turn={view({})} />);
    expect(screen.getByTestId("turn-badge-tokens")).toHaveTextContent("usage not recorded");
  });
});

describe("trace labels", () => {
  const T = "2026-11-15T10:00:00Z";
  const span = (kind: TraceSpan["kind"], attributes: TraceSpan["attributes"], name = "moderation"): TraceSpan =>
    ({ id: "s1", parent_id: null, kind, name, start: T, end: T, status: "ok", attributes, input: null, output: null, truncated: {} }) as TraceSpan;
  const names = { agentNames: { a1: "Support" } };

  it("labels draft runs, published runs and an older API's runs without the draft field", () => {
    expect(spanLabel(span("run", { run_kind: "agent", agent_id: "a1", draft: true }), names)).toBe("Agent: Support · Draft");
    expect(spanLabel(span("child_run", { run_kind: "agent", agent_id: "a1", draft: true, agent_version: null }), names)).toBe("Agent: Support · Draft");
    expect(spanLabel(span("run", { run_kind: "agent", agent_id: "a1", draft: false, agent_version: 7 }), names)).toBe("Agent: Support v7");
    expect(spanLabel(span("run", { run_kind: "agent", agent_id: "a1", agent_version: 7 }), names)).toBe("Agent: Support v7");
    expect(spanLabel(span("run", { run_kind: "workflow", workflow_id: "w", draft: true }), { workflowNames: { w: "Flow" } })).toBe("Workflow: Flow");
  });

  it("a moderation span shows its model and tokens; a processor that called no model shows none", () => {
    const withModel = span("processor", { processor_type: "moderation", model_id: "test:mod", usage: { input_tokens: 7, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0, usage_measured: true } });
    render(<SpanDetail span={withModel} />);
    expect(within(screen.getByTestId("span-usage")).getByText("test:mod")).toBeInTheDocument();
    cleanup();
    render(<SpanDetail span={span("processor", { processor_type: "moderation", model_id: "test:mod" })} />);
    expect(within(screen.getByTestId("span-usage")).getByText("test:mod")).toBeInTheDocument();
    cleanup();
    render(<SpanDetail span={span("processor", { processor_type: "pii_redact", action: "allow" }, "pii_redact")} />);
    expect(screen.queryByTestId("span-usage")).toBeNull();
  });
});

describe("working memory probe", () => {
  let urls: string[];
  let answer: (url: string) => Response;
  const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
  beforeEach(() => {
    urls = [];
    answer = () => json(null);
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    vi.stubGlobal("fetch", vi.fn(async (input: string) => (urls.push(String(input)), answer(String(input)))));
  });
  afterEach(() => vi.unstubAllGlobals());

  it("asks both single-note routes with ?missing=null, and a null body is no note", async () => {
    expect(await getWorkingMemory({ scope: "conversation", conversationId: "c1" })).toBeNull();
    expect(await getWorkingMemory({ scope: "resource", agentId: "a1", resourceId: "user:7" })).toBeNull();
    expect(urls.map((u) => u.replace(/^https?:\/\/[^/]+/, ""))).toEqual([
      "/api/v1/conversations/c1/memory/working?missing=null",
      "/api/v1/agents/a1/memory/resources/user%3A7/working?missing=null",
    ]);
  });

  it("returns an existing note, treats an older API's 404 as none, and still throws other failures", async () => {
    answer = () => json({ content: "hi", version: 3 });
    expect(await getWorkingMemory({ scope: "conversation", conversationId: "c1" })).toMatchObject({ content: "hi", version: 3 });
    answer = () => json({ detail: "Conversation not found" }, 404);
    expect(await getWorkingMemory({ scope: "conversation", conversationId: "c1" })).toBeNull();
    answer = () => json({ detail: "boom" }, 500);
    await expect(getWorkingMemory({ scope: "conversation", conversationId: "c1" })).rejects.toMatchObject({ status: 500 });
  });

  it("does not add the probe to the list route", async () => {
    answer = () => json({ items: [] });
    await listAgentWorkingMemories("a1", "resource");
    expect(urls[0]).not.toContain("missing");
  });
});
