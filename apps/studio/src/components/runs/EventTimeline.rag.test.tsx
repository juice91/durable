import { describe, expect, it } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { EventTimeline } from "./EventTimeline";
import type { RunEvent } from "@/lib/runs";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

// RM.19: the Events tab rows for the RAG and memory event types. They show allowlisted counts,
// ids, model and cost; a text field an event should never carry is not shown even if it does.

const SECRET = "zq-memory-text-marker";
let seq = 0;
const ev = (type: RunEvent["type"], data: Record<string, unknown>): RunEvent => ({
  seq: ++seq, type, data: { ...data, content: SECRET, text: SECRET } as RunEvent["data"], ts: "2026-11-14T09:00:00Z",
});

const EVENTS: RunEvent[] = [
  ev("embedding", { model_id: "test:hash", inputs: 1, input_tokens: 3, cost_usd: "0.00003", dimensions: 1024, stage: "recall", hits: 2, turns: 1, messages: 2 }),
  ev("embedding", { model_id: "test:hash", inputs: 1, input_tokens: 2, cost_usd: "0.00002", dimensions: 1024 }),
  ev("embedding", { stage: "index", status: "failed", error_code: "embedding_timeout", model_id: "test:hash", inputs: 2, input_tokens: 0 }),
  ev("memory_updated", { scope: "resource", format: "json", version: 4, changed_keys: ["name"], redacted: 0 }),
  ev("memory_updated", { stage: "observe", kind: "observation", covers_from_turn: 0, covers_to_turn: 2, notes: 3, model_id: "test:observer", usage: { input_tokens: 50, output_tokens: 20 }, cost_usd: "0.002" }),
  ev("memory_observe_failed", { stage: "reflect", error_code: "observer_output_invalid", covers_from_turn: 0, covers_to_turn: 2 }),
  ev("ingest_progress", { phase: "chunked", generation: 1, embedded: 0, total: 3, pending_tokens: 900 }),
  ev("ingest_progress", { phase: "embedding", batch: 0, chunks: 3, embedded: 3, total: 3, model_id: "test:hash", input_tokens: 900, cost_usd: "0.0009" }),
];

describe("EventTimeline RAG and memory rows", () => {
  it("gives each new event type its own row", () => {
    render(<EventTimeline events={EVENTS} currentPartialText={{}} />);
    const embeddings = screen.getAllByTestId("event-embedding");
    expect(embeddings).toHaveLength(3);
    expect(embeddings[0]).toHaveTextContent("Semantic recall");
    expect(embeddings[0]).toHaveTextContent("hits: 2");
    expect(embeddings[1]).toHaveTextContent(/^Embedding/);
    expect(embeddings[1]).toHaveTextContent("dimensions: 1,024");
    expect(embeddings[1]).toHaveTextContent("tokens: 2");
    expect(embeddings[2]).toHaveTextContent("Memory index failed");
    expect(embeddings[2]).toHaveTextContent("error: embedding_timeout");
    expect(screen.getByTestId("event-memory-working")).toHaveTextContent("Working memory updated");
    expect(screen.getByTestId("event-memory-working")).toHaveTextContent("changed: name");
    const observer = screen.getAllByTestId("event-memory-observer");
    expect(observer[0]).toHaveTextContent("Observations added");
    expect(observer[0]).toHaveTextContent("turns: 0 to 2");
    expect(observer[0]).toHaveTextContent("tokens out: 20");
    expect(observer[1]).toHaveTextContent("Reflector failed");
    expect(observer[1]).toHaveTextContent("error: observer_output_invalid");
    const ingest = screen.getAllByTestId("event-ingest-progress");
    expect(ingest[0]).toHaveTextContent("Document chunked");
    expect(ingest[1]).toHaveTextContent("Embedding batch");
    expect(ingest[1]).toHaveTextContent("embedded: 3 of 3 sections");
  });

  it("never shows a text field these events should not carry", () => {
    const { container } = render(<EventTimeline events={EVENTS} currentPartialText={{}} />);
    expect(container).not.toHaveTextContent(SECRET);
  });

  it("has no axe violations", async () => {
    const { container } = render(<EventTimeline events={EVENTS} currentPartialText={{}} />);
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
