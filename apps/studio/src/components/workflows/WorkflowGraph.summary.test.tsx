import React from "react";
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkflowCanvas, WorkflowStepList } from "./WorkflowGraph";
import type { WorkflowSpec } from "@/lib/workflows";

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));

const spec = {
  name: "Sync",
  steps: [
    { id: "fetch", type: "tool", tool_id: "http.request", on_error: "continue", input: { method: "GET", url: "https://example.com/news", extract_html: { mode: "links" } }, next: "write" },
    { id: "write", type: "agent", agent_id: "agent-1", version: 3, input: { links: { $ref: "$steps.fetch.output.text" } }, next: "pick" },
    { id: "pick", type: "agent", agent_id: "agent-2", next: "route" },
    { id: "route", type: "branch", cases: [{ when: { path: "steps.fetch.output.ok", op: "eq", value: true }, next: "done" }], default: "done" },
    { id: "done", type: "tool", tool_id: "workflow.return", input: { value: { status: "x" }, outcome: "skipped" } },
  ],
} as unknown as WorkflowSpec;

beforeEach(() => { vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; }); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("step summaries", () => {
  it("the step list says what each tool and agent step does, with the agent name as a link", () => {
    render(<WorkflowStepList spec={spec} agentNames={{ "agent-1": "Writer" }} />);
    const fetch = screen.getByTestId("step-summary-fetch");
    expect(fetch).toHaveTextContent("http.request");
    expect(fetch).toHaveTextContent("GET example.com/news · extract_html links");
    expect(screen.getByTestId("step-on-error-fetch")).toHaveTextContent("on_error: continue");
    const write = screen.getByTestId("step-summary-write");
    expect(within(write).getByRole("link", { name: "Writer" })).toHaveAttribute("href", "/agents/agent-1");
    expect(write).toHaveTextContent("v3 · input: links");
    // No name in the list: the id, and "latest".
    expect(screen.getByTestId("step-summary-pick")).toHaveTextContent("agent-2latest");
    expect(screen.getByTestId("step-summary-done")).toHaveTextContent("returns {status} · outcome skipped");
    // A branch keeps its cases and gets no summary line.
    expect(screen.queryByTestId("step-summary-route")).toBeNull();
    expect(screen.getByTestId("workflow-step-route")).toHaveTextContent("default→ done");
  });

  it("the canvas node's subtitle is the summary, one line with the full text in title", () => {
    render(<div style={{ width: 800, height: 600 }}><WorkflowCanvas spec={spec} agentNames={{ "agent-1": "Writer" }} /></div>);
    const subtitle = screen.getByTestId("workflow-node-subtitle-fetch");
    expect(subtitle).toHaveTextContent("http.request GET example.com/news · extract_html links");
    expect(subtitle).toHaveAttribute("title", "http.request GET example.com/news · extract_html links");
    expect(subtitle).toHaveClass("truncate");
    expect(screen.getByTestId("workflow-node-subtitle-write")).toHaveTextContent("Writer v3 · input: links");
    expect(screen.getByTestId("workflow-node-subtitle-route")).toHaveTextContent("1 case + default");
  });
});
