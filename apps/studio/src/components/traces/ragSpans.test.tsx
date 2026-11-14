import { describe, expect, it } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen, within } from "@testing-library/react";
import { SpanDetail } from "./SpanDetail";
import { TraceWaterfall } from "./TraceWaterfall";
import { TraceTotals } from "./TraceTotals";
import { subjectLink } from "./TraceSheet";
import { formatScore } from "./RagSpanSections";
import { KIND_BADGE, KIND_ICON, KIND_TONE } from "./spanStyles";
import {
  KIND_LABELS,
  MEMORY_SPAN_KINDS,
  RAG_SPAN_KINDS,
  SPAN_KINDS,
  flattenSpans,
  spanLabel,
  traceWindow,
  type TraceRun,
  type TraceSpan,
  type TraceTotals as Totals,
} from "@/lib/traces";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

// RM.19: the RAG and memory span kinds in the trace view (labels, icons, colours, detail sections),
// the kb_ingest subject link and the totals' embedding hint.

const T = (s: number) => new Date(Date.UTC(2026, 10, 14, 9, 0, s)).toISOString();

function span(kind: TraceSpan["kind"], attributes: TraceSpan["attributes"], extra: Partial<TraceSpan> = {}): TraceSpan {
  return {
    id: `s-${kind}`, parent_id: null, kind, name: kind, start: T(1), end: T(2), status: "ok",
    attributes, input: null, output: null, truncated: {}, ...extra,
  };
}

const RETRIEVE = span(
  "retrieve",
  {
    tool_call_id: "c1", tool_name: "retrieve", query: "how to install", knowledge_base: "Docs", top_k: 5, hit_count: 3,
    knowledge_bases: [{ id: "kb1", top_k: 5, min_score: 0.3 }],
    hits: [
      { n: 1, title: "Install", url: "https://docs.example/install#setup", ref: "https://docs.example/install#setup", kb_id: "kb1", chunk_id: "ch1", doc_version: 2, score: 0.91234 },
      { n: 2, title: "FAQ", url: null, ref: "kb:faq", kb_id: "kb1", chunk_id: "ch2", doc_version: 1, score: 0.7 },
      { n: 3, title: "Evil", url: "javascript:alert(1)", ref: "javascript:alert(1)", kb_id: "kb1", chunk_id: "ch3", score: 0.5 },
    ],
  },
  { input: { search_query: "how to install" }, output: "[1] Install ..." },
);

const EMBEDDING = span("embedding", {
  model_id: "litellm:qwen-embed", dimensions: 1024, inputs: 1, input_type: "query", requests: 1,
  usage: { input_tokens: 7, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, usage_measured: true },
  cost_usd: "0.0000070000", request_key: "embed:retrieve:c1",
});

describe("span kinds (lib/traces, spanStyles)", () => {
  it("every kind has a label, badge, icon and tone", () => {
    for (const kind of SPAN_KINDS) {
      expect(KIND_LABELS[kind], kind).toBeTruthy();
      expect(KIND_BADGE[kind], kind).toBeTruthy();
      expect(KIND_ICON[kind], kind).toBeTruthy();
      expect(KIND_TONE[kind], kind).toMatch(/^text-/);
    }
  });

  it("colours RAG and memory spans with chart tokens only, the rest muted", () => {
    for (const kind of SPAN_KINDS) {
      const tone = KIND_TONE[kind];
      if (RAG_SPAN_KINDS.has(kind)) expect(tone).toBe("text-[color:var(--viz-series-1-color)]");
      else if (MEMORY_SPAN_KINDS.has(kind)) expect(tone).toBe("text-[color:var(--viz-series-2-color)]");
      else expect(tone).toBe("text-muted-foreground");
    }
  });

  it("labels the new kinds", () => {
    expect(spanLabel(RETRIEVE)).toBe("Retrieve from Docs (3 hits)");
    expect(spanLabel(EMBEDDING)).toBe("Embedding: litellm:qwen-embed");
    expect(spanLabel(span("embedding", { batch: 2, model_id: "test:hash" }))).toBe("Embedding batch 2: test:hash");
    expect(spanLabel(span("memory_recall", { hits: 1 }))).toBe("Semantic recall (1 hit)");
    expect(spanLabel(span("memory_index", {}))).toBe("Memory index");
    expect(spanLabel(span("observer", {}))).toBe("Observer");
    expect(spanLabel(span("reflector", {}))).toBe("Reflector");
    expect(spanLabel(span("working_memory", { version: 4 }))).toBe("Working memory update (v4)");
    expect(spanLabel(span("ingest", {}, { name: "chunked" }))).toBe("Ingest: chunked");
  });

  it("names a kb_ingest run by its knowledge base when known", () => {
    const run = span("run", { run_kind: "kb_ingest", knowledge_base_id: "kb1" });
    expect(spanLabel(run)).toBe("Knowledge ingest");
    expect(spanLabel(run, { knowledgeBaseNames: { kb1: "Product docs" } })).toBe("Knowledge ingest: Product docs");
  });
});

describe("SpanDetail for RAG and memory spans", () => {
  it("a retrieve span shows the query, top_k, the attached knowledge bases and the hits with scores", () => {
    render(<SpanDetail span={RETRIEVE} names={{ knowledgeBaseNames: { kb1: "Product docs" } }} />);
    expect(screen.getByTestId("span-retrieve-query")).toHaveTextContent("how to install");
    expect(screen.getByTestId("span-retrieve-top-k")).toHaveTextContent("5");
    expect(screen.getByTestId("span-retrieve-kbs")).toHaveTextContent("Product docs");
    expect(screen.getByTestId("span-retrieve-kbs")).toHaveTextContent("top 5, min score 0.3");
    const hits = screen.getAllByTestId("span-retrieve-hit");
    expect(hits).toHaveLength(3);
    expect(within(hits[0]).getByTestId("span-retrieve-hit-score")).toHaveTextContent("0.912");
    // Only http(s) URLs become links (lib/safeUrl); others show as text.
    const links = screen.getAllByTestId("span-retrieve-hit-link");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "https://docs.example/install#setup");
    expect(links[0]).toHaveAttribute("rel", "noopener noreferrer");
    expect(within(hits[2]).queryByRole("link")).toBeNull();
    expect(hits[1]).toHaveTextContent("kb:faq");
    // The section's attributes are not repeated in the generic list.
    expect(screen.getByTestId("span-attributes")).not.toHaveTextContent("hit_count");
  });

  it("top_k reads 'per knowledge base' when several are attached and none was chosen", () => {
    const many = span("retrieve", { ...RETRIEVE.attributes, top_k: null, knowledge_bases: [{ id: "a", top_k: 3, min_score: null }, { id: "b", top_k: 8, min_score: null }] });
    render(<SpanDetail span={many} />);
    expect(screen.getByTestId("span-retrieve-top-k")).toHaveTextContent("per knowledge base");
  });

  it("an embedding span shows the model, dimensions and tokens", () => {
    render(<SpanDetail span={EMBEDDING} />);
    expect(screen.getByTestId("span-usage")).toHaveTextContent("litellm:qwen-embed");
    expect(screen.getByTestId("span-input-tokens")).toHaveTextContent("7");
    expect(screen.getByTestId("span-embedding-dimensions")).toHaveTextContent("1,024");
    expect(screen.getByTestId("span-embedding-inputs")).toHaveTextContent("1");
  });

  it("a recall span shows its hits (counts only)", () => {
    render(<SpanDetail span={span("memory_recall", { hits: 4, turns: 2, groups: 1, messages: 3, model_id: "test:hash", dimensions: 768 })} />);
    expect(screen.getByTestId("span-recall-hits")).toHaveTextContent("4");
    expect(screen.getByTestId("span-embedding")).toHaveTextContent("Messages added");
  });

  it("observer, working memory and ingest spans show their sections", () => {
    const { unmount } = render(
      <SpanDetail span={span("observer", { memory_kind: "observation", covers_from_turn: 0, covers_to_turn: 3, notes: 2, model_id: "test:observer", usage: { input_tokens: 50, output_tokens: 9, cache_read_tokens: 0, cache_write_tokens: 0, usage_measured: true } })} />,
    );
    expect(screen.getByTestId("span-observer")).toHaveTextContent("0 to 3");
    expect(screen.getByTestId("span-output-tokens")).toHaveTextContent("9");
    unmount();
    const second = render(<SpanDetail span={span("working_memory", { scope: "resource", version: 4, changed_keys: ["name", "city"], other_changed_keys: 2 })} />);
    expect(screen.getByTestId("span-working-memory")).toHaveTextContent("name, city (+2 more)");
    expect(screen.queryByTestId("span-usage")).toBeNull();
    second.unmount();
    render(<SpanDetail span={span("ingest", { phase: "chunked", embedded: 0, total: 12, pending_tokens: 3000 })} />);
    expect(screen.getByTestId("span-ingest")).toHaveTextContent("0 of 12");
  });

  it("a kb_ingest run span links to its knowledge base", () => {
    render(<SpanDetail span={span("run", { run_kind: "kb_ingest", run_id: "r1", knowledge_base_id: "kb1" })} names={{ knowledgeBaseNames: { kb1: "Docs" } }} />);
    const link = screen.getByTestId("span-link-knowledge-base");
    expect(link).toHaveAttribute("href", routes.knowledgeBase("kb1", { tab: "runs" }));
    expect(link).toHaveTextContent("Open knowledge base: Docs");
  });

  it("formats scores to three places", () => {
    expect(formatScore(0.5)).toBe("0.500");
    expect(formatScore(null)).toBe("-");
  });

  it("has no axe violations for a retrieve span", async () => {
    const { container } = render(<SpanDetail span={RETRIEVE} />);
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

describe("TraceWaterfall kind icons", () => {
  it("draws an icon per row inside the chart-token scope", () => {
    const spans = [span("run", { run_kind: "agent" }, { id: "root" }), { ...RETRIEVE, parent_id: "root" }, { ...EMBEDDING, id: "e", parent_id: RETRIEVE.id }];
    const { container } = render(
      <TraceWaterfall rows={flattenSpans(spans)} window={traceWindow(spans, Date.parse(T(5)))} selectedId={null} onSelect={() => undefined} collapsed={new Set()} onToggle={() => undefined} now={Date.parse(T(5))} />,
    );
    expect(container.querySelector(".eap-viz")).not.toBeNull();
    const rows = screen.getAllByTestId("trace-span-row");
    expect(rows.map((r) => r.getAttribute("data-kind"))).toEqual(["run", "retrieve", "embedding"]);
    expect(screen.getAllByTestId("trace-span-kind-icon")).toHaveLength(3);
    expect(rows[1]).toHaveAttribute("aria-label", expect.stringMatching(/^RAG Retrieve from Docs/));
  });
});

describe("subjectLink (trace Sheet meta row)", () => {
  const run = (extra: Partial<TraceRun>): TraceRun => ({ run_id: "r1", agent_id: null, ...extra }) as TraceRun;

  it("opens the knowledge base of a kb_ingest run, named when known", () => {
    expect(subjectLink(run({ kind: "kb_ingest", knowledge_base_id: "kb1" }))).toEqual({
      href: routes.knowledgeBase("kb1", { tab: "runs" }),
      label: "Open knowledge base",
    });
    expect(subjectLink(run({ kind: "kb_ingest", knowledge_base_id: "kb1" }), { kb1: "Docs" })?.label).toBe("Open knowledge base: Docs");
    expect(subjectLink(run({ kind: "kb_ingest" }))).toBeNull();
    expect(subjectLink(run({ kind: "agent", agent_id: "a1" }))?.label).toBe("Open agent");
  });
});

describe("TraceTotals embedding hint", () => {
  const base: Totals = {
    runs: 1, model_requests: 4, tool_calls: 1, input_tokens: 100, output_tokens: 10, cache_read_tokens: 0,
    cache_write_tokens: 0, cost_usd: "0.0100000000", unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 1200,
    status: "completed",
  };

  it("adds embedding and memory counts and the embedding cost only when present", () => {
    const { unmount } = render(<TraceTotals totals={base} />);
    expect(screen.getByTestId("totals-requests")).toHaveTextContent("1 tool call, 1 run");
    expect(screen.getByTestId("totals-requests")).not.toHaveTextContent("embedding");
    unmount();
    render(<TraceTotals totals={{ ...base, embedding_requests: 2, memory_requests: 1, embedding_input_tokens: 30, embedding_cost_usd: "0.0000300000" }} />);
    expect(screen.getByTestId("totals-requests")).toHaveTextContent("1 tool call, 1 run; 2 embedding, 1 observer or reflector");
    expect(screen.getByTestId("totals-cost")).toHaveTextContent("embeddings");
    expect(screen.getByTestId("totals-cost")).toHaveTextContent("30 tokens");
  });
});
