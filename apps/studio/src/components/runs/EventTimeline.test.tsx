import { describe, it, expect, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { EventTimeline } from "./EventTimeline";
import { type RunEvent, type PartialTextMap } from "@/lib/runs";

describe("EventTimeline", () => {
  const events: RunEvent[] = [
    {
      seq: 1,
      type: "run_started",
      data: { input: "Test input" },
      ts: "2026-11-12T00:00:00Z",
    },
    {
      seq: 2,
      type: "model_message",
      data: {
        text: "Created **TCK-12345** for the issue.",
      },
      ts: "2026-11-12T00:00:01Z",
    },
    {
      seq: 3,
      type: "tool_call",
      data: {
        tool_name: "test_tool",
        args: { key: "value" },
      },
      ts: "2026-11-12T00:00:02Z",
    },
  ];

  it("renders no events message when empty", () => {
    render(<EventTimeline events={[]} currentPartialText={{}} />);
    expect(screen.getByText(/no events yet/i)).toBeInTheDocument();
  });

  it("renders event items", () => {
    render(<EventTimeline events={events} currentPartialText={{}} />);
    expect(screen.getByText("seq 1")).toBeInTheDocument();
    expect(screen.getByText("seq 2")).toBeInTheDocument();
    expect(screen.getByText("seq 3")).toBeInTheDocument();
    expect(screen.getByText("run_started")).toBeInTheDocument();
    expect(screen.getByText("model_message")).toBeInTheDocument();
    expect(screen.getByText("tool_call")).toBeInTheDocument();
  });

  it("renders markdown in model messages", () => {
    render(<EventTimeline events={events} currentPartialText={{}} />);
    const strongText = screen.getByText("TCK-12345");
    expect(strongText.tagName).toBe("STRONG");
    expect(screen.queryByText(/\*\*TCK-12345\*\*/)).not.toBeInTheDocument();
  });

  it("renders partial text section when present", () => {
    const partialText: PartialTextMap = {
      "1": { text: "Partial text", attempt: "1" },
    };
    render(<EventTimeline events={events} currentPartialText={partialText} />);
    expect(screen.getByText("Live partial text")).toBeInTheDocument();
    expect(screen.getByText("Request #1")).toBeInTheDocument();
    expect(screen.getByText("Partial text")).toBeInTheDocument();
  });
});
