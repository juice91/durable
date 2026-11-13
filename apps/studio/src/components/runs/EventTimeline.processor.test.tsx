import { describe, it, expect } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { EventTimeline, formatMatchCounts } from "./EventTimeline";
import { type RunEvent } from "@/lib/runs";

const processor = (seq: number, data: RunEvent["data"]): RunEvent => ({
  seq, type: "processor_result", data, ts: "2026-11-13T00:00:00Z",
});

describe("EventTimeline processor_result", () => {
  it("renders phase, processor type, action and match counts, never text", () => {
    render(
      <EventTimeline
        currentPartialText={{}}
        events={[
          processor(1, {
            phase: "input", index: 0, processor_type: "pii_redact", action: "redact",
            match_counts: { email: 2, phone: 0, ipv4: 1 }, text: "SECRET a@b.c",
          }),
        ]}
      />,
    );
    const line = screen.getByTestId("processor-result");
    expect(line).toHaveTextContent("Input processor");
    expect(line).toHaveTextContent("pii_redact");
    expect(line).toHaveTextContent("redact");
    expect(line).toHaveTextContent("2 email, 1 ipv4");
    expect(line).not.toHaveTextContent("phone");
    expect(screen.queryByText(/SECRET/)).toBeNull();
    expect(screen.queryByText(/a@b\.c/)).toBeNull();
  });

  it("shows a block with its reason and a moderation verdict's categories", () => {
    render(
      <EventTimeline
        currentPartialText={{}}
        events={[
          processor(1, { phase: "output", index: 1, processor_type: "moderation", action: "block",
                         match_counts: {}, categories: ["violence"], reason: "verdict_invalid" }),
          processor(2, { phase: "output", index: 0, processor_type: "max_length", action: "truncate",
                         match_counts: {}, model_request: 3 }),
        ]}
      />,
    );
    const [moderation, maxLength] = screen.getAllByTestId("processor-result");
    expect(moderation).toHaveTextContent("Output processor");
    expect(moderation).toHaveTextContent("moderation");
    expect(moderation).toHaveTextContent("flagged: violence; reason: verdict_invalid");
    expect(screen.getByText("block")).toHaveClass("text-red-700");
    expect(maxLength).toHaveTextContent("truncate");
    expect(maxLength).toHaveTextContent("(request #3)");
  });

  it("formats match counts", () => {
    expect(formatMatchCounts({ email: 1, iban: 3 })).toBe("1 email, 3 iban");
    expect(formatMatchCounts({})).toBe("");
    expect(formatMatchCounts(null)).toBe("");
    expect(formatMatchCounts(["x"])).toBe("");
  });
});
