import { describe, expect, it } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen, within } from "@testing-library/react";
import { SpanDetail } from "./SpanDetail";
import type { TraceSpan } from "@/lib/traces";

// Follow-up 1a: a moderation check's tokens belong to the moderation model, shown on its own span.

const T = (s: number) => new Date(Date.UTC(2026, 10, 15, 9, 0, s)).toISOString();

function processor(attributes: TraceSpan["attributes"]): TraceSpan {
  return {
    id: "r1:processor:input:0", parent_id: "run:r1", kind: "processor", name: "moderation", start: T(1), end: T(1),
    status: "ok", attributes, input: null, output: null, truncated: {},
  };
}

describe("moderation processor span", () => {
  it("shows the moderation model and its own tokens", () => {
    render(
      <SpanDetail
        span={processor({
          processor_key: "processor:input:0", phase: "input", index: 0, processor_type: "moderation", action: "allow",
          request_key: "moderation:input:0", model_id: "test:moderation", cost_usd: "0.0000010000", price_source: "override",
          usage: { input_tokens: 7, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0, usage_measured: true },
        })}
      />,
    );
    const usage = screen.getByTestId("span-usage");
    expect(within(usage).getByText("test:moderation")).toBeInTheDocument();
    expect(within(usage).getByTestId("span-input-tokens")).toHaveTextContent("7");
    expect(within(usage).getByTestId("span-output-tokens")).toHaveTextContent("1");
  });

  it("shows no usage section for a processor that called no model", () => {
    render(<SpanDetail span={processor({ processor_key: "processor:input:0", phase: "input", index: 0, processor_type: "pii", action: "allow" })} />);
    expect(screen.queryByTestId("span-usage")).toBeNull();
  });
});
