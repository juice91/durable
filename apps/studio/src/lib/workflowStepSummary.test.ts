import { describe, expect, it } from "vitest";
import { stepSummary } from "./workflowStepSummary";
import type { WorkflowStep } from "./workflows";

const tool = (tool_id: string, input: Record<string, unknown>, extra: object = {}): WorkflowStep => ({ id: "s", type: "tool", tool_id, input, ...extra }) as WorkflowStep;

describe("stepSummary", () => {
  it("http.request with extract_html: method, host/path and the reducer mode", () => {
    const s = stepSummary(tool("http.request", { method: "GET", url: "https://example.com/news/list?page=2", extract_html: { mode: "links" } }));
    expect(s?.lead).toBe("http.request");
    expect(s?.detail).toBe("GET example.com/news/list · extract_html links");
  });

  it("http.request with a $ref url shows the path of the reference", () => {
    const s = stepSummary(tool("http.request", { method: "POST", url: { $ref: "$steps.feed.output.json" } }));
    expect(s?.detail).toBe("POST $steps.feed.output.json");
  });

  it("json.query shows the expression, cut when long", () => {
    expect(stepSummary(tool("json.query", { expression: "items[0].title" }))?.detail).toBe("items[0].title");
    const long = stepSummary(tool("json.query", { expression: "a".repeat(100) }))?.detail ?? "";
    expect(long).toHaveLength(48);
    expect(long.endsWith("…")).toBe(true);
  });

  it("workflow.return shows its value keys and outcome", () => {
    const s = stepSummary(tool("workflow.return", { value: { status: "x", url: "y" }, outcome: "skipped" }));
    expect(s?.detail).toBe("returns {status, url} · outcome skipped");
    expect(stepSummary(tool("workflow.return", { value: { $template: { a: 1, b: 2 } } }))?.detail).toBe("returns {a, b}");
  });

  it("text.check lists its rule names; another tool lists the input keys; $template is template(...)", () => {
    expect(stepSummary(tool("text.check", { text: "t", rules: { max_graphemes: 300, forbid_terms: ["x"] } }))?.detail).toBe("max_graphemes, forbid_terms");
    expect(stepSummary(tool("other.tool", { a: 1, b: { $template: "x" } }))?.detail).toBe("a, b");
  });

  it("an agent: name, pinned version or latest, input keys", () => {
    const names = { "agent-1": "Writer" };
    const pinned = stepSummary({ id: "w", type: "agent", agent_id: "agent-1", version: 3, input: { topic: "x" } }, names);
    expect(pinned).toMatchObject({ lead: "Writer", agentId: "agent-1", detail: "v3 · input: topic" });
    const latest = stepSummary({ id: "w", type: "agent", agent_id: "agent-1" }, names);
    expect(latest?.detail).toBe("latest");
    expect(stepSummary({ id: "w", type: "agent", agent_id: "agent-9" })?.lead).toBe("agent-9");
  });

  it("a branch has no summary", () => {
    expect(stepSummary({ id: "b", type: "branch", cases: [], default: "x" } as unknown as WorkflowStep)).toBeNull();
  });
});
