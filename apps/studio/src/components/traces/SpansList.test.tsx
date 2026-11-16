import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SpansList } from "./SpansList";
import { listSpans, type SpanRow, parseSpanFilters, spanFiltersToParams } from "@/lib/spans";

vi.mock("@/lib/spans", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/spans")>()), listSpans: vi.fn() }));
const list = vi.mocked(listSpans);

const span = (n: number): SpanRow => ({
  run_id: `run-${n}`, span_id: `span-${n}`, parent_id: null, kind: "tool_call" as const, name: `tool-${n}`,
  start: "2026-11-16T10:00:00Z", end: "2026-11-16T10:00:01Z", status: "ok" as const, agent_id: null, workflow_id: null, model: null,
});
const EMPTY = parseSpanFilters(new URLSearchParams(""));

describe("SpansList", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    list.mockResolvedValue({ items: [span(1), span(2)], next_before: "cursor-1" });
  });

  it("round-trips span_kind through the URL filters", () => {
    const f = parseSpanFilters(new URLSearchParams("span_kind=tool_call&status=failed&span_kind_bad=1"));
    expect(f.span_kind).toBe("tool_call");
    expect(spanFiltersToParams(f).get("span_kind")).toBe("tool_call");
    expect(parseSpanFilters(new URLSearchParams("span_kind=nonsense")).span_kind).toBeNull();
  });

  it("lists spans with Start, Kind, Name, Run and Status columns", async () => {
    render(<SpansList filters={EMPTY} onOpen={vi.fn()} />);
    expect(await screen.findByText("tool-1")).toBeInTheDocument();
    for (const header of ["Start", "Kind", "Name", "Run", "Status"]) {
      expect(screen.getByRole("columnheader", { name: header })).toBeInTheDocument();
    }
    expect(list.mock.calls[0][1]).toMatchObject({ limit: 50 });
  });

  it("pages with the opaque cursor verbatim", async () => {
    render(<SpansList filters={EMPTY} onOpen={vi.fn()} />);
    await screen.findByText("tool-1");
    list.mockResolvedValueOnce({ items: [span(3)], next_before: null });
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("tool-3");
    expect(list.mock.calls[1][1]).toMatchObject({ before: "cursor-1" });
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("opening a row reports the run and span", async () => {
    const onOpen = vi.fn();
    render(<SpansList filters={EMPTY} onOpen={onOpen} />);
    const row = (await screen.findByText("tool-2")).closest("[data-row-id]") as HTMLElement;
    fireEvent.click(row.querySelector("[data-datalist-primary]") as HTMLElement);
    await waitFor(() => expect(onOpen).toHaveBeenCalledWith("run-2", "span-2"));
  });
});
