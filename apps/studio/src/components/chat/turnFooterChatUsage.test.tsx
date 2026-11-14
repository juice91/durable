import { describe, expect, it } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { TurnFooter, type TurnView } from "./TurnFooter";
import { turnUsage } from "./runOptions";

// Follow-up 3: next to the chat model, the footer shows the chat model's own tokens.

function view(row: Parameters<typeof turnUsage>[1]): TurnView {
  return {
    runId: "r1", status: "completed", modelId: "test:chat", modelOverridden: false, draft: false, version: 2,
    usage: turnUsage([], row), checking: false, block: null, liveText: true, announce: false,
  };
}

describe("TurnFooter usage", () => {
  it("shows chat_* tokens, not the run's combined totals", () => {
    render(<TurnFooter turn={view({ input_tokens: 1200, output_tokens: 300, cost_usd: "0.5", chat_input_tokens: 800, chat_output_tokens: 120, chat_cost_usd: "0.25" })} />);
    const footer = screen.getByTestId("turn-footer");
    expect(footer).toHaveTextContent("test:chat");
    expect(footer).toHaveTextContent("800 in · 120 out tokens");
    expect(footer).not.toHaveTextContent("1,200 in");
  });

  it("falls back to the totals from an API without chat_*", () => {
    render(<TurnFooter turn={view({ input_tokens: 1200, output_tokens: 300, cost_usd: "0.5" })} />);
    expect(screen.getByTestId("turn-footer")).toHaveTextContent("1,200 in · 300 out tokens");
  });
});
