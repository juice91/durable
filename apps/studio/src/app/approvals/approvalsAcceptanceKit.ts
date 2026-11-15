// Shared fixtures for the AC.15 acceptance tests (tests only; not imported by app code).
import type { Approval } from "@/lib/approvals";

export const RUN = "11111111-2222-3333-4444-555555555555";
export const WF_RUN = "22222222-2222-3333-4444-555555555555";
export const CHILD_RUN = "33333333-2222-3333-4444-555555555555";
export const AGENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
export const AGENT2 = "bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee";

export const toolItem: Approval = {
  run_id: RUN,
  kind: "tool",
  approval_key: "call_1",
  tool_call_id: "call_1",
  step_id: null,
  root_run_id: RUN,
  agent_id: AGENT,
  workflow_id: null,
  tool_name: "send_email",
  args: { to: "a@example.com" },
  prompt: null,
  status: "pending",
  requested_at: "2026-11-15T10:00:00+00:00",
  deadline: "2099-11-16T10:00:00+00:00",
  decided_at: null,
  decision: null,
  decided_by: null,
  run_status: "running",
};

export const stepItem: Approval = {
  ...toolItem,
  run_id: WF_RUN,
  kind: "step",
  approval_key: "review",
  tool_call_id: null,
  step_id: "review",
  root_run_id: WF_RUN,
  agent_id: null,
  workflow_id: "WF1",
  tool_name: null,
  args: null,
  prompt: "Ship it?",
};

export const decidedTool: Approval = {
  ...toolItem,
  approval_key: "call_d",
  tool_call_id: "call_d",
  tool_name: "refund",
  status: "decided",
  decision: "approved",
  decided_by: "alice",
  decided_at: "2026-11-15T11:00:00+00:00",
};

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}
