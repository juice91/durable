// Workflow specs shared by the workflow tests.
import type { WorkflowSpec } from "@/lib/workflows";

export const AGENT = "11111111-2222-3333-4444-555555555555";

export const demoSpec: WorkflowSpec = {
  spec_version: 1,
  name: "Triage",
  input_schema: { type: "object", properties: { text: { type: "string" }, urgent: { type: "boolean" } }, required: ["text"] },
  start: "classify",
  steps: [
    { id: "classify", type: "agent", agent_id: AGENT, input: { text: { $ref: "input.text" } }, next: "lookup" },
    { id: "lookup", type: "tool", tool_id: "http.get", input: {}, next: "route" },
    { id: "route", type: "branch", cases: [{ when: { op: "eq", left: { $ref: "input.urgent" }, right: true }, next: "approve" }], default: "summarize" },
    { id: "approve", type: "approval", prompt: "Escalate?", timeout_hours: 4, next: "summarize" },
    { id: "summarize", type: "agent", agent_id: AGENT, input: {}, next: null },
  ],
};

export const parallelSpec: WorkflowSpec = {
  spec_version: 1,
  name: "Fan out",
  input_schema: { type: "object" },
  start: "fork",
  steps: [
    { id: "fork", type: "parallel", branches: ["a", "b"], join: "merge" },
    { id: "a", type: "tool", tool_id: "x", next: "a2" },
    { id: "a2", type: "tool", tool_id: "x", next: "merge" },
    { id: "b", type: "tool", tool_id: "x", next: "merge" },
    { id: "merge", type: "agent", agent_id: AGENT, next: null },
  ],
};

/** A parallel whose first branch is an approval gate. */
export const parallelApprovalSpec: WorkflowSpec = {
  spec_version: 1,
  name: "Gated fan out",
  input_schema: { type: "object" },
  start: "fork",
  steps: [
    { id: "fork", type: "parallel", branches: ["gate", "b"], join: "merge" },
    { id: "gate", type: "approval", prompt: "Ship branch A?", timeout_hours: 2, next: "merge" },
    { id: "b", type: "tool", tool_id: "x", next: "merge" },
    { id: "merge", type: "agent", agent_id: AGENT, next: null },
  ],
};
