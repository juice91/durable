// RAG P1.A5: PDF citations show "p. N" and open the PDF at `#page=N`.
import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@assistant-ui/react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@assistant-ui/react")>()),
  useAuiState: (selector: (state: { message: { id: string } }) => unknown) => selector({ message: { id: "run-1:assistant-1" } }),
}));

import { MarkdownText, ToolCallInfoContext } from "./messageParts";
import { citationHref, citationTitle, pageFromUrl, pageLabel, parseSources, sourcePages } from "./citations";
import type { ToolCallInfo } from "./toolCalls";

afterEach(() => cleanup());

describe("pages of a source", () => {
  it("pageFromUrl reads #page=N (with other open parameters), nothing else", () => {
    expect(pageFromUrl("https://ti.example/manual.pdf#page=212")).toBe(212);
    expect(pageFromUrl("https://ti.example/manual.pdf#zoom=100&page=7")).toBe(7);
    expect(pageFromUrl("https://ti.example/manual.pdf#page=7&zoom=100")).toBe(7);
    expect(pageFromUrl("https://ti.example/guide.html#installing")).toBeUndefined();
    expect(pageFromUrl("https://ti.example/x?page=3")).toBeUndefined();
    expect(pageFromUrl("https://ti.example/x#page=0")).toBeUndefined();
    expect(pageFromUrl("https://ti.example/x#subpage=3")).toBeUndefined();
    expect(pageFromUrl(null)).toBeUndefined();
    expect(pageFromUrl(42)).toBeUndefined();
  });

  it("sourcePages prefers page/page_end, then metadata.eap, then the URL; page_end only after page", () => {
    expect(sourcePages({ page: 5, page_end: 6, url: "https://x/a.pdf#page=9" })).toEqual({ page: 5, page_end: 6 });
    expect(sourcePages({ metadata: { eap: { extractor: "pdf", page: 212, page_end: 214 } }, url: null })).toEqual({ page: 212, page_end: 214 });
    expect(sourcePages({ url: "https://x/a.pdf#page=9" })).toEqual({ page: 9 });
    expect(sourcePages({ page: 4, page_end: 4 })).toEqual({ page: 4 });
    expect(sourcePages({ page: 4, page_end: 2 })).toEqual({ page: 4 });
    expect(sourcePages({ page: "4", url: undefined })).toEqual({});
  });

  it("pageLabel and citationTitle", () => {
    expect(pageLabel({ page: 212 })).toBe("p. 212");
    expect(pageLabel({ page: 212, page_end: 214 })).toBe("pp. 212–214");
    expect(pageLabel({})).toBeNull();
    expect(citationTitle({ title: "AM62x TRM", page: 212 })).toBe("AM62x TRM, p. 212");
    expect(citationTitle({ title: "Install" })).toBe("Install");
  });

  it("parseSources records the page from the anchor; citationHref adds #page=N only where the URL has no fragment", () => {
    const [pdf, rst] = parseSources([
      { n: 1, title: "TRM", url: "https://ti.example/trm.pdf#page=212" },
      { n: 2, title: "UART", url: "https://ti.example/uart.html#uart-config" },
    ]);
    expect(pdf).toMatchObject({ page: 212 });
    expect(pdf.page_end).toBeUndefined();
    expect(rst.page).toBeUndefined();
    expect(citationHref(pdf)).toBe("https://ti.example/trm.pdf#page=212");
    expect(citationHref(rst)).toBe("https://ti.example/uart.html#uart-config");
    const [given] = parseSources([{ n: 3, title: "TRM", url: "https://ti.example/trm.pdf", page: 40, page_end: 41 }]);
    expect(citationHref(given)).toBe("https://ti.example/trm.pdf#page=40");
  });

  it("the worker's page/page_end win over the anchor; a range reads pp. 12–14", () => {
    const [ranged] = parseSources([{ n: 1, title: "TRM", url: "https://ti.example/trm.pdf#page=12", page: 12, page_end: 14 }]);
    expect(ranged).toMatchObject({ page: 12, page_end: 14 });
    expect(pageLabel(ranged)).toBe("pp. 12–14");
    expect(citationTitle(ranged)).toBe("TRM, pp. 12–14");
    expect(citationHref(ranged)).toBe("https://ti.example/trm.pdf#page=12");
    const [mismatch] = parseSources([{ n: 2, title: "TRM", url: "https://ti.example/trm.pdf#page=99", page: 5 }]);
    expect(mismatch.page).toBe(5);
  });
});

describe("a PDF citation in chat", () => {
  it("shows p. N after the marker and links to #page=N; a source without a page shows no page", () => {
    const sources = parseSources([
      { n: 1, numbering: "run", title: "AM62x TRM", url: "https://ti.example/trm.pdf#page=212", kb_id: "kb1", document_key: "trm.pdf" },
      { n: 2, numbering: "run", title: "UART", url: "https://ti.example/uart.html#config", kb_id: "kb1", document_key: "uart.rst" },
    ]);
    const map = new Map<string, ToolCallInfo>([["run-1:call-1", { status: "ok", durationMs: 5, sources }]]);
    render(
      <ToolCallInfoContext.Provider value={map}>
        <MarkdownText {...({ type: "text", text: "Set the divisor [1], see also [2].", status: { type: "complete" } } as unknown as React.ComponentProps<typeof MarkdownText>)} />
      </ToolCallInfoContext.Provider>,
    );
    const [first, second] = screen.getAllByTestId("citation-link");
    expect(first).toHaveAttribute("href", "https://ti.example/trm.pdf#page=212");
    expect(first).toHaveTextContent("[1]p. 212");
    expect(first).toHaveAccessibleName("Source 1: AM62x TRM, p. 212");
    expect(first.getAttribute("title")).toContain("AM62x TRM, p. 212");
    expect(screen.getAllByTestId("citation-page").map((el) => el.textContent)).toEqual(["p. 212"]);
    expect(second).toHaveTextContent(/^\[2\]$/);
  });
});
