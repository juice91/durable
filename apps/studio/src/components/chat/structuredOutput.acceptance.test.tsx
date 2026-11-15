// AC.14 tester: the structured-output tree view (nesting, long strings, copy, citations, safety).
import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));

import { copyText } from "@/lib/clipboard";
import { StructuredOutputView } from "./StructuredOutput";
import type { CitationSource } from "./citations";

const sources = new Map<number, CitationSource>([
  [1, { n: 1, numbering: "run", title: "Disk guide", url: "https://docs.example.com/disk" } as CitationSource],
  [2, { n: 2, numbering: "run", title: "Net guide", url: "https://docs.example.com/net" } as CitationSource],
]);

beforeEach(() => vi.clearAllMocks());

describe("StructuredOutputView: tree", () => {
  it("renders nested objects and arrays with paths", () => {
    const value = { a: { b: { c: "deep" } }, list: [{ id: 1 }, { id: 2 }], flag: true, none: null, n: 3 };
    render(<StructuredOutputView value={value} />);
    const paths = screen.getAllByTestId("structured-output-field").map((f) => f.getAttribute("data-path"));
    expect(paths).toEqual(expect.arrayContaining(["a", "a.b", "a.b.c", "list", "list[0].id", "list[1].id", "flag", "none", "n"]));
    expect(screen.getAllByTestId("structured-output-item")).toHaveLength(2);
    expect(screen.getByText("deep")).toBeInTheDocument();
    expect(screen.getByText("true")).toBeInTheDocument();
  });

  it("renders empty containers and a root array", () => {
    render(<StructuredOutputView value={[{}, []]} />);
    expect(screen.getByText("{}")).toBeInTheDocument();
    expect(screen.getByText("[]")).toBeInTheDocument();
  });

  it("collapses a long string leaf behind a toggle", () => {
    const long = "word ".repeat(400).trim();
    render(<StructuredOutputView value={{ summary: long }} />);
    const field = screen.getByTestId("structured-output-field");
    // A long string must not be shown in full by default: a toggle expands it.
    expect(field.textContent!.length).toBeLessThan(long.length);
    const toggle = within(field).getByRole("button");
    fireEvent.click(toggle);
    expect(field.textContent).toContain(long);
  });
});

describe("StructuredOutputView: copy", () => {
  it("copies canonical (2-space) JSON of the original value, not the rendered text", async () => {
    const value = { z: 1, a: ["x [1]", { k: null }], s: "<b>x</b>" };
    render(<StructuredOutputView value={value} sources={sources} />);
    fireEvent.click(screen.getByTestId("structured-output-copy"));
    await waitFor(() => expect(copyText).toHaveBeenCalledTimes(1));
    const copied = vi.mocked(copyText).mock.calls[0][0];
    expect(copied).toBe(JSON.stringify(value, null, 2));
    expect(JSON.parse(copied)).toEqual(value);
  });

  it("reports when copying is unavailable", async () => {
    vi.mocked(copyText).mockResolvedValueOnce(false);
    render(<StructuredOutputView value={{ a: 1 }} />);
    fireEvent.click(screen.getByTestId("structured-output-copy"));
    expect(await screen.findByTestId("structured-output-copy-status")).toHaveTextContent("not available");
  });

  it("raw toggle shows the JSON and toggles back", () => {
    render(<StructuredOutputView value={{ a: 1 }} />);
    const toggle = screen.getByTestId("structured-output-raw-toggle");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("structured-output-json")).toHaveTextContent('"a": 1');
    fireEvent.click(toggle);
    expect(screen.queryByTestId("structured-output-json")).toBeNull();
  });
});
