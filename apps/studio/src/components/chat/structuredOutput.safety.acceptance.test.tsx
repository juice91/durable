// AC.14 tester: citations and injection safety in the structured-output view.
import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));

import { StructuredOutputView } from "./StructuredOutput";
import type { CitationSource } from "./citations";

const sources = new Map<number, CitationSource>([
  [1, { n: 1, numbering: "run", title: "Disk guide", url: "https://docs.example.com/disk" } as CitationSource],
  [2, { n: 2, numbering: "run", title: "Net guide", url: "https://docs.example.com/net" } as CitationSource],
]);

describe("citations", () => {
  it("links [n] in nested string leaves and in a citations list to the sources", () => {
    render(<StructuredOutputView value={{ a: { note: "see [2]" }, items: ["x [1]"], citations: [1, 2] }} sources={sources} />);
    const hrefs = screen.getAllByTestId("citation-link").map((a) => a.getAttribute("href"));
    expect(hrefs.sort()).toEqual(
      [
        "https://docs.example.com/disk",
        "https://docs.example.com/disk",
        "https://docs.example.com/net",
        "https://docs.example.com/net",
      ].sort(),
    );
    expect(within(screen.getByTestId("structured-output-citations")).getAllByTestId("citation-link")).toHaveLength(2);
  });

  it("leaves an unknown [n] as text and never links a URL typed by the model", () => {
    render(<StructuredOutputView value={{ t: "see [9] and [1](https://evil.example) and https://evil.example/y" }} sources={sources} />);
    const hrefs = screen.queryAllByTestId("citation-link").map((a) => a.getAttribute("href"));
    expect(hrefs).not.toContain("https://evil.example");
    expect(screen.getByTestId("structured-output").textContent).toContain("[9]");
  });
});

describe("no HTML injection", () => {
  const evil = {
    a: "<script>window.__pwned=1</script><img src=x onerror=window.__pwned=2><iframe src=//evil></iframe>",
    b: ["<a href=\"javascript:alert(1)\">x</a>", "[click](javascript:alert(1))", "<style>*{display:none}</style>"],
    "<b>key</b>": "v",
  };
  it("renders model strings and keys as inert content", () => {
    const { container } = render(<StructuredOutputView value={evil} sources={sources} />);
    expect(container.querySelector("script, img, iframe, style")).toBeNull();
    for (const a of Array.from(container.querySelectorAll("a"))) {
      expect(a.getAttribute("href") ?? "").not.toMatch(/^javascript:/i);
    }
    expect(container.querySelector("b")).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("shows the raw JSON as text only", () => {
    const { container } = render(<StructuredOutputView value={evil} />);
    fireEvent.click(screen.getByTestId("structured-output-raw-toggle"));
    expect(container.querySelector("script, img, iframe")).toBeNull();
  });
});
