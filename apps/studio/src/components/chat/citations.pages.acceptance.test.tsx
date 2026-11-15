// RAG P1.A5 acceptance (tester): page labels in chat and the retrieve span, from the worker's
// page/page_end, with the anchor as the fallback.
import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@assistant-ui/react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@assistant-ui/react")>()),
  useAuiState: (selector: (state: { message: { id: string } }) => unknown) => selector({ message: { id: "run-1:assistant-1" } }),
}));

import { MarkdownText, ToolCallInfoContext } from "./messageParts";
import { parseSources } from "./citations";
import type { ToolCallInfo } from "./toolCalls";
import { SpanDetail } from "@/components/traces/SpanDetail";
import { AXE_TIMEOUT_MS, expectNoAxeViolations } from "@/test/a11y";
import type { TraceSpan } from "@/lib/traces";

afterEach(() => cleanup());

function renderText(raw: unknown[], text: string) {
  const map = new Map<string, ToolCallInfo>([["run-1:call-1", { status: "ok", durationMs: 5, sources: parseSources(raw) }]]);
  return render(
    <ToolCallInfoContext.Provider value={map}>
      <MarkdownText {...({ type: "text", text, status: { type: "complete" } } as unknown as React.ComponentProps<typeof MarkdownText>)} />
    </ToolCallInfoContext.Provider>,
  );
}

describe("citations in chat", () => {
  it("a worker source with page 212 and no anchor shows p. 212 and links #page=212", () => {
    renderText([{ n: 1, numbering: "run", title: "TRM", url: "https://ti.example/trm.pdf", page: 212 }], "Divisor [1].");
    const link = screen.getByTestId("citation-link");
    expect(link).toHaveAttribute("href", "https://ti.example/trm.pdf#page=212");
    expect(screen.getByTestId("citation-page")).toHaveTextContent("p. 212");
  });

  it("a range shows pp. 12–14 in the label and the accessible name; the link opens the first page", () => {
    renderText([{ n: 1, numbering: "run", title: "TRM", url: "https://ti.example/trm.pdf", page: 12, page_end: 14 }], "See [1].");
    expect(screen.getByTestId("citation-page")).toHaveTextContent("pp. 12–14");
    expect(screen.getByTestId("citation-link")).toHaveAccessibleName("Source 1: TRM, pp. 12–14");
    expect(screen.getByTestId("citation-link")).toHaveAttribute("href", "https://ti.example/trm.pdf#page=12");
  });

  it("when source.page is missing, the anchor is the fallback (and a non-page anchor shows nothing)", () => {
    renderText([
      { n: 1, numbering: "run", title: "A", url: "https://ti.example/a.pdf#page=7" },
      { n: 2, numbering: "run", title: "B", url: "https://ti.example/b.html#intro" },
    ], "One [1] two [2].");
    expect(screen.getAllByTestId("citation-page").map((e) => e.textContent)).toEqual(["p. 7"]);
    expect(screen.getAllByTestId("citation-link")[1]).toHaveAttribute("href", "https://ti.example/b.html#intro");
  });

  it("the page label passes axe", async () => {
    document.title = "Chat";
    document.documentElement.lang = "en";
    const view = renderText([{ n: 1, numbering: "run", title: "TRM", url: "https://ti.example/trm.pdf", page: 3, page_end: 5 }], "Hi [1].");
    await expectNoAxeViolations(view.container);
  }, AXE_TIMEOUT_MS);
});

describe("RagSpanSections", () => {
  const span = (hits: unknown[]): TraceSpan => ({
    id: "s", parent_id: null, kind: "retrieve", name: "retrieve", start: "2026-11-15T10:00:00Z", end: "2026-11-15T10:00:00Z", status: "ok",
    input: null, output: null, truncated: {}, attributes: { query: "q", hits },
  }) as TraceSpan;

  it("shows p. N from page and pp. N–M from page and page_end, and nothing without a page", () => {
    render(<SpanDetail span={span([
      { n: 1, title: "A", url: "https://x/a.pdf", kb_id: "k", chunk_id: "c1", score: 0.9, page: 212 },
      { n: 2, title: "B", url: "https://x/b.pdf", kb_id: "k", chunk_id: "c2", score: 0.8, page: 12, page_end: 14 },
      { n: 3, title: "C", url: "https://x/c.md", kb_id: "k", chunk_id: "c3", score: 0.7 },
    ])} />);
    const metas = screen.getAllByTestId("span-retrieve-hit-meta");
    expect(metas.map((m) => m.getAttribute("data-page"))).toEqual(["p. 212", "pp. 12–14", null]);
    expect(metas[1]).toHaveTextContent("pp. 12–14");
    expect(metas[2]).not.toHaveTextContent(/pp?\. \d/);
  });
});
