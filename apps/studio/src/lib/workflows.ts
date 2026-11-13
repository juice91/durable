// Workflow definitions, runs and approval decisions (milestone 3, tasks 3.7 and 3.8).
//
// Definitions: GET /api/v1/workflows, /workflows/{id}, /workflows/{id}/versions[/{n}].
// Runs: POST /api/v1/workflows/{id}/runs (202, same idempotent run_id semantics as tool runs),
// then GET /runs/{run_id}, /events and /stream like any run. Approval: POST
// /api/v1/workflows/runs/{run_id}/steps/{step_id}/approve|reject with {"comment"?}.
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import type { Run, RunStatus } from "./runs";
import type { JsonSchema } from "./tools";
import { mapToolRunError } from "./toolRuns";
import { createRunLedger } from "./toolRunLedger";

export type ConditionOp = "eq" | "ne" | "gt" | "lt" | "contains" | "exists";

export interface Condition {
  op: ConditionOp;
  left: unknown;
  right?: unknown;
}

interface StepBase {
  id: string;
}

export interface AgentStep extends StepBase {
  type: "agent";
  agent_id: string;
  version?: number | null;
  input?: Record<string, unknown>;
  next?: string | null;
}

export interface ToolStep extends StepBase {
  type: "tool";
  tool_id: string;
  input?: Record<string, unknown>;
  next?: string | null;
}

export interface ApprovalStep extends StepBase {
  type: "approval";
  prompt: string;
  timeout_hours: number;
  next?: string | null;
}

export interface BranchStep extends StepBase {
  type: "branch";
  cases: { when: Condition; next: string }[];
  default: string;
}

export interface ParallelStep extends StepBase {
  type: "parallel";
  branches: string[];
  join: string;
}

export type WorkflowStep = AgentStep | ToolStep | ApprovalStep | BranchStep | ParallelStep;
export type StepType = WorkflowStep["type"];

export interface WorkflowSpec {
  spec_version: 1;
  name: string;
  description?: string;
  input_schema: JsonSchema;
  output_schema?: Record<string, unknown> | null;
  steps: WorkflowStep[];
  start: string;
}

export interface Workflow {
  id: string;
  name: string;
  draft_spec: WorkflowSpec | null;
  latest_version: number | null;
}

export interface WorkflowVersion {
  workflow_id: string;
  version: number;
  spec: WorkflowSpec;
  spec_hash: string;
  created_at: string;
  created_by: string;
}

export interface WorkflowRunAccepted {
  run_id: string;
  kind: "workflow";
  workflow_id: string;
  version: number;
  status: string;
  display_status: RunStatus;
  trace_id: string | null;
}

export interface ApprovalDecisionResult {
  run_id: string;
  step_id: string;
  decision: "approved" | "rejected";
  decided_by: string;
  comment: string | null;
  notified: boolean;
}

export const MAX_APPROVAL_COMMENT = 2000;

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, { headers: apiHeaders() });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

const enc = encodeURIComponent;

export function listWorkflows(): Promise<Workflow[]> {
  return getJson<Workflow[]>("/api/v1/workflows");
}

export function getWorkflow(id: string): Promise<Workflow> {
  return getJson<Workflow>(`/api/v1/workflows/${enc(id)}`);
}

/** Published versions, newest first. */
export function listWorkflowVersions(id: string): Promise<WorkflowVersion[]> {
  return getJson<WorkflowVersion[]>(`/api/v1/workflows/${enc(id)}/versions`);
}

export function getWorkflowVersion(id: string, version: number): Promise<WorkflowVersion> {
  return getJson<WorkflowVersion>(`/api/v1/workflows/${enc(id)}/versions/${version}`);
}

/** Run history of one workflow, newest first (GET /runs?kind=workflow&workflow_id=...). */
export function listWorkflowRuns(
  workflowId: string,
  { limit = 50, before }: { limit?: number; before?: string } = {},
): Promise<{ items: Run[]; next_before: string | null }> {
  const params = new URLSearchParams({ kind: "workflow", workflow_id: workflowId, limit: String(limit) });
  if (before) params.append("before", before);
  return getJson(`/api/v1/runs?${params.toString()}`);
}

const WORKFLOW_RUN_FALLBACKS: Record<number, string> = {
  404: "Workflow or version not found",
  409: "This run id is already in use",
  503: "The run was recorded but could not be started; run again to retry it",
};

/**
 * Start a run of a published version. Errors are ToolRunError (banner + per-field 422 messages,
 * the same mapping as tool runs: loc ["body", "input", ...path]).
 */
export async function startWorkflowRun(
  workflowId: string,
  input: Record<string, unknown>,
  { version, runId }: { version?: number | null; runId?: string } = {},
): Promise<WorkflowRunAccepted> {
  const body: { input: Record<string, unknown>; version?: number; run_id?: string } = { input };
  if (version != null) body.version = version;
  if (runId) body.run_id = runId;
  const response = await fetch(`${BASE_URL}/api/v1/workflows/${enc(workflowId)}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...apiHeaders() },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let data: unknown = null;
    try {
      data = await response.json();
    } catch {
      // Non-JSON error body: the status fallback is used.
    }
    throw mapToolRunError(response.status, data, WORKFLOW_RUN_FALLBACKS);
  }
  return response.json();
}

/** Approve or reject a waiting approval step. Throws ApiError (404, 409 not waiting, 422). */
export async function decideApproval(
  runId: string,
  stepId: string,
  decision: "approve" | "reject",
  comment?: string,
): Promise<ApprovalDecisionResult> {
  const trimmed = comment?.trim();
  const response = await fetch(
    `${BASE_URL}/api/v1/workflows/runs/${enc(runId)}/steps/${enc(stepId)}/${decision}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...apiHeaders() },
      body: JSON.stringify(trimmed ? { comment: trimmed } : {}),
    },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

/**
 * Workflow runs whose start answer the page has not seen: same semantics as tool runs (a lost
 * response reuses the run id, no automatic retry), in its own sessionStorage slot. The ledger's
 * `toolId` is the workflow id and `inputKey` covers the input AND the version.
 */
export const workflowRunLedger = createRunLedger("eap.studio.workflowRuns.v1");
