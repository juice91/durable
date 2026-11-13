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

  it("renders a tool run's start, call, result and end events (object payloads)", () => {
    const toolEvents: RunEvent[] = [
      {
        seq: 0,
        type: "run_started",
        data: { kind: "tool", tool_id: "tickets.create", input: { title: "Printer", body: "on fire" } },
        ts: "2026-11-13T00:00:00Z",
      },
      {
        seq: 1,
        type: "tool_call",
        data: {
          tool_call_id: "tool_run",
          tool_name: "tickets__create",
          tool_id: "tickets.create",
          args: { title: "Printer", body: "on fire" },
        },
        ts: "2026-11-13T00:00:01Z",
      },
      {
        seq: 2,
        type: "tool_result",
        data: {
          tool_call_id: "tool_run",
          tool_name: "tickets__create",
          tool_id: "tickets.create",
          status: "ok",
          content: { ticket_id: "TCK-1", created: true },
        },
        ts: "2026-11-13T00:00:02Z",
      },
      {
        seq: 3,
        type: "run_completed",
        data: { output: '{"created":true,"ticket_id":"TCK-1"}', result: { ticket_id: "TCK-1", created: true } },
        ts: "2026-11-13T00:00:03Z",
      },
    ];
    render(<EventTimeline events={toolEvents} currentPartialText={{}} />);

    expect(screen.getByText("Tool: tickets.create")).toBeInTheDocument();
    expect(screen.getByLabelText("Run input")).toHaveTextContent('"title": "Printer"');
    expect(screen.getByLabelText("Tool arguments")).toHaveTextContent('"body": "on fire"');
    expect(screen.getByLabelText("Tool result")).toHaveTextContent('"ticket_id": "TCK-1"');
    expect(screen.getByText("Run completed successfully")).toBeInTheDocument();
    expect(screen.getByLabelText("Run result")).toHaveTextContent('"created": true');
  });

  it("renders a failed tool call's error and a string result", () => {
    const toolEvents: RunEvent[] = [
      {
        seq: 0,
        type: "run_started",
        data: { kind: "tool", tool_id: "clock.now", input: {} },
        ts: "2026-11-13T00:00:00Z",
      },
      {
        seq: 1,
        type: "tool_result",
        data: {
          tool_call_id: "tool_run",
          tool_id: "tickets.create",
          status: "error",
          error: "SideEffectOutcomeUnknown: check the external system",
          error_code: "side_effect_outcome_unknown",
        },
        ts: "2026-11-13T00:00:01Z",
      },
      {
        seq: 2,
        type: "run_completed",
        data: { output: '"2026-11-13T00:00:00+00:00"', result: "2026-11-13T00:00:00+00:00" },
        ts: "2026-11-13T00:00:02Z",
      },
    ];
    render(<EventTimeline events={toolEvents} currentPartialText={{}} />);
    expect(screen.getByLabelText("Run input")).toHaveTextContent("{}");
    expect(screen.getByText("Tool error:")).toBeInTheDocument();
    expect(screen.getByText("SideEffectOutcomeUnknown: check the external system")).toBeInTheDocument();
    expect(screen.getByLabelText("Run result")).toHaveTextContent("2026-11-13T00:00:00+00:00");
  });
});
