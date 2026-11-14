import { describe, expect, it } from "vitest";
import { runOptionLabel } from "./logsQuery";
import { spanLabel, type TraceRun, type TraceSpan } from "@/lib/traces";

// Follow-up 1c: a draft run says "Draft" where a published run shows its version.

const AT = "2026-11-15T10:00:00Z";

function run(extra: Partial<TraceRun>): TraceRun {
  return {
    run_id: "r1", kind: "agent", agent_id: "a1", version: 3, status: "completed", display_status: "completed", input: "hi",
    output: "ok", error: null, heartbeat_at: null, created_at: AT, updated_at: AT, trace_id: null, conversation_id: null,
    turn: null, ...extra,
  } as TraceRun;
}

function root(attributes: TraceSpan["attributes"]): TraceSpan {
  return { id: "run:r1", parent_id: null, kind: "run", name: "agent", start: AT, end: AT, status: "ok", attributes, input: null, output: null, truncated: {} };
}

describe("draft runs on the Logs page", () => {
  it("the run picker names the version, or Draft", () => {
    const when = new Date(AT).toLocaleString();
    expect(runOptionLabel(run({}))).toBe(`${when} · v3 · completed`);
    expect(runOptionLabel(run({ version: null, draft: true }))).toBe(`${when} · Draft · completed`);
    expect(runOptionLabel(run({ kind: "workflow", agent_id: null, version: null }))).toBe(`${when} · completed`);
  });

  it("the trace opened from a record labels a draft run Draft, not a blank version", () => {
    const names = { agentNames: { a1: "Support" } };
    expect(spanLabel(root({ run_kind: "agent", agent_id: "a1", agent_version: null, draft: true }), names)).toBe("Agent: Support · Draft");
    expect(spanLabel(root({ run_kind: "agent", agent_id: "a1", agent_version: 3, draft: false }), names)).toBe("Agent: Support v3");
    expect(spanLabel(root({ run_kind: "agent", agent_id: "a1", agent_version: null, draft: true, turn: 2 }), names)).toBe("Agent: Support · Draft (turn 2)");
  });
});
