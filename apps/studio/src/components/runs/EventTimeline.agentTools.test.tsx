import { describe, expect, it } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { render, screen } from "@testing-library/react";
import { EventTimeline } from "./EventTimeline";
import { SpanDetail } from "@/components/traces/SpanDetail";
import type { RunEvent } from "@/lib/runs";
import type { TraceSpan } from "@/lib/traces";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

// Agents as tools: the Events tab shows each agent-tool call with a link to the child run, and the
// trace's tool-call span of such a call links to it too.

const CHILD = "0d6c7b2e-1f4a-5b3c-8d9e-aabbccddeeff";
let seq = 0;
const ev = (type: RunEvent["type"], data: Record<string, unknown>): RunEvent => ({ seq: ++seq, type, data: data as RunEvent["data"], ts: "2026-11-22T09:00:00Z" });

const EVENTS: RunEvent[] = [
  ev("agent_tool_started", { tool_call_id: "c1", tool_name: "research_helper", agent_id: "a", version: 3, child_run_id: CHILD, depth: 1, timeout_seconds: 300 }),
  ev("agent_tool_completed", { tool_call_id: "c1", tool_name: "research_helper", child_run_id: CHILD, status: "completed" }),
  ev("agent_tool_completed", { tool_call_id: "c2", tool_name: "research_helper", status: "refused", error_code: "agent_tool_max_calls" }),
  ev("agent_tool_completed", { tool_call_id: "c3", tool_name: "research_helper", child_run_id: CHILD, status: "timeout", error_code: "agent_tool_timeout" }),
];

describe("EventTimeline agent tool rows", () => {
  it("shows the call, its facts and a link to the child run", () => {
    render(<EventTimeline events={EVENTS} currentPartialText={{}} />);
    const started = screen.getByTestId("event-agent-tool-started");
    expect(started).toHaveTextContent("Agent call started");
    expect(started).toHaveTextContent("tool: research_helper");
    expect(started).toHaveTextContent("version: 3");
    expect(started).toHaveTextContent("depth: 1");
    expect(started).toHaveTextContent("timeout: 300 s");
    const links = screen.getAllByTestId("event-child-run-link");
    expect(links).toHaveLength(3); // started, completed and timed out have a child; the refused call has none
    expect(links[0]).toHaveAttribute("href", expect.stringContaining(CHILD));
  });

  it("names each outcome", () => {
    render(<EventTimeline events={EVENTS} currentPartialText={{}} />);
    const done = screen.getAllByTestId("event-agent-tool-completed");
    expect(done[0]).toHaveTextContent("Agent call completed");
    expect(done[1]).toHaveTextContent("Agent call refused");
    expect(done[1]).toHaveTextContent("error: agent_tool_max_calls");
    expect(done[2]).toHaveTextContent("Agent call timed out");
    expect(done[2]).toHaveTextContent("error: agent_tool_timeout");
  });

  it("has no axe violations", async () => {
    const { container } = render(<EventTimeline events={EVENTS} currentPartialText={{}} />);
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});

describe("SpanDetail of an agent tool call", () => {
  const span = (attributes: TraceSpan["attributes"]): TraceSpan => ({
    id: "s", parent_id: null, kind: "tool_call", name: "research_helper", start: null, end: null, status: "ok",
    attributes, input: null, output: null, truncated: {},
  });

  it("links to the child run's trace", () => {
    render(<SpanDetail span={span({ tool_call_id: "c1", agent_tool: true, child_run_id: CHILD, agent_tool_status: "completed" })} names={{}} runId="r" />);
    expect(screen.getByTestId("span-link-child-trace")).toHaveAttribute("href", expect.stringContaining(CHILD));
  });

  it("an ordinary tool call has no such link", () => {
    render(<SpanDetail span={span({ tool_call_id: "c1" })} names={{}} runId="r" />);
    expect(screen.queryByTestId("span-link-child-trace")).toBeNull();
  });
});
