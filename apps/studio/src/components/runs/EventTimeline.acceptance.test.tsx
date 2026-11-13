import { describe, it, expect } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { EventTimeline } from "./EventTimeline";
import { type RunEvent } from "@/lib/runs";

const longText = `打印机-${"x".repeat(5000)}-🔥`;

describe("EventTimeline acceptance", () => {
  it("renders a tool run whose input has nested objects, arrays, null, unicode, and a long string", () => {
    const events: RunEvent[] = [
      {
        seq: 0,
        type: "run_started",
        data: {
          kind: "tool",
          tool_id: "tickets.create",
          input: {
            title: "打印机着火了",
            tags: ["urgent", "café", null],
            nested: { location: { floor: 3, notes: ["a", { b: null }] } },
            note: null,
            blob: longText,
          },
        },
        ts: "2026-11-13T00:00:00Z",
      },
      {
        seq: 1,
        type: "tool_call",
        data: {
          tool_call_id: "tool_run",
          tool_id: "tickets.create",
          args: { items: [{ id: 1, name: "票" }, null], empty: [] },
        },
        ts: "2026-11-13T00:00:01Z",
      },
      {
        seq: 2,
        type: "tool_result",
        data: { tool_call_id: "tool_run", tool_id: "tickets.create", status: "ok", content: null },
        ts: "2026-11-13T00:00:02Z",
      },
    ];

    expect(() => render(<EventTimeline events={events} currentPartialText={{}} />)).not.toThrow();
    const input = screen.getByLabelText("Run input");
    expect(input).toHaveTextContent("打印机着火了");
    expect(input).toHaveTextContent("café");
    expect(input).toHaveTextContent("floor");
    expect(input.textContent).toContain(longText);
    expect(input).toHaveTextContent("null");
    expect(screen.getByLabelText("Tool arguments")).toHaveTextContent('"empty": []');
    expect(screen.getByLabelText("Tool result")).toHaveTextContent("null");
  });

  it("renders a null tool-run input without throwing", () => {
    const events = [
      {
        seq: 0,
        type: "run_started",
        data: { kind: "tool", tool_id: "clock.now", input: null },
        ts: "2026-11-13T00:00:00Z",
      },
    ] as unknown as RunEvent[];

    expect(() => render(<EventTimeline events={events} currentPartialText={{}} />)).not.toThrow();
    expect(screen.getByText("Tool: clock.now")).toBeInTheDocument();
    expect(screen.queryByLabelText("Run input")).not.toBeInTheDocument();
  });

  it("renders a run_failed event whose error is an object", () => {
    const events = [
      {
        seq: 4,
        type: "run_failed",
        data: {
          status: "failed",
          error: { code: "tool_input_invalid", detail: { field: "body", hint: "必填" } },
          error_code: "tool_input_invalid",
        },
        ts: "2026-11-13T00:00:04Z",
      },
    ] as unknown as RunEvent[];

    expect(() => render(<EventTimeline events={events} currentPartialText={{}} />)).not.toThrow();
    expect(screen.getByText("Run failed")).toBeInTheDocument();
    expect(screen.getByText(/tool_input_invalid/)).toBeInTheDocument();
    expect(screen.getByText(/必填/)).toBeInTheDocument();
  });

  it("renders an unknown event type with an object payload", () => {
    const events = [
      {
        seq: 9,
        type: "stream_gap",
        data: { payload: { nested: [1, null, "你好"], flag: true } },
        ts: "2026-11-13T00:00:09Z",
      },
    ] as unknown as RunEvent[];

    expect(() => render(<EventTimeline events={events} currentPartialText={{}} />)).not.toThrow();
    expect(screen.getByText("stream_gap")).toBeInTheDocument();
    expect(screen.getByText(/你好/)).toBeInTheDocument();
    expect(screen.getByText(/"flag": true/)).toBeInTheDocument();
  });
});
