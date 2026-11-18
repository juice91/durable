// Workflow definitions, runs and approval decisions (milestone 3, tasks 3.7 and 3.8).
//
// Definitions: GET /api/v1/workflows, /workflows/{id}, /workflows/{id}/versions[/{n}].
// Runs: POST /api/v1/workflows/{id}/runs (202, same idempotent run_id semantics as tool runs),
// then GET /runs/{run_id}, /events and /stream like any run. Approval: POST
// /api/v1/workflows/runs/{run_id}/steps/{step_id}/approve|reject with {"comment"?}.
// Cancel (loops D12, LP.12): POST /api/v1/runs/{run_id}/cancel, no body.
import { ApiError, apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import type { Run, RunStatus } from "./runs";
import type { JsonSchema } from "./tools";
import { mapToolRunError } from "./toolRuns";
import { createRunLedger } from "./toolRunLedger";

export type ConditionOp = "eq" | "ne" | "gt" | "lt" | "contains" | "exists";

/** One comparison (a bare comparison keeps its JSON). */
export interface Condition {
  op: ConditionOp;
  left: unknown;
  right?: unknown;
}

/** Combinators over conditions (loops milestone D9): at most 8 operands, nesting depth at most 4. */
export interface AllCondition {
  all: Expression[];
}
export interface AnyCondition {
  any: Expression[];
}
export interface NotCondition {
  not: Expression;
}
export type Expression = Condition | AllCondition | AnyCondition | NotCondition;

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

/** A step that waits for a person to submit data matching `resume_schema` (a JSON Schema object). */
export interface InputStep extends StepBase {
  type: "input";
  prompt: string;
  timeout_hours: number;
  resume_schema: JsonSchema;
  next?: string | null;
}

export interface BranchStep extends StepBase {
  type: "branch";
  cases: { when: Expression; next: string }[];
  default: string;
}

export interface ParallelStep extends StepBase {
  type: "parallel";
  branches: string[];
  join: string;
}

/** A loop's body: its own graph of steps with its own start (step ids are unique across the spec). */
export interface LoopBody {
  start: string;
  steps: WorkflowStep[];
}

/** Run `body` once per item of the list at `items` (a $ref), in item order. Output: a list. */
export interface ForeachStep extends StepBase {
  type: "foreach";
  items: { $ref: string };
  max_items: number;
  /** 1 to 8; 1 when omitted. */
  concurrency?: number;
  /** "fail" when omitted. */
  on_item_error?: "fail" | "collect";
  body: LoopBody;
  next?: string | null;
}

/** Repeat `body` while `while` holds, or until `until` holds (exactly one is set). */
export interface LoopStep extends StepBase {
  type: "loop";
  while?: Expression;
  until?: Expression;
  max_iterations: number;
  /** "fail" when omitted. */
  on_max_iterations?: "fail" | "exit";
  state?: Record<string, unknown>;
  carry?: Record<string, unknown>;
  body: LoopBody;
  next?: string | null;
}

/** One evaluator of an evaluate step (the eval suites' evaluator union, by `kind`). */
export interface EvaluatorSpec {
  kind: string;
  name?: string | null;
  [field: string]: unknown;
}

/** Score `value` (a $ref) with 1 to 8 evaluators. Output: {passed, score, reason, results}. */
export interface EvaluateStep extends StepBase {
  type: "evaluate";
  value: { $ref: string };
  expected?: unknown;
  evaluators: EvaluatorSpec[];
  /** "all" when omitted. */
  pass?: "all" | "any";
  next?: string | null;
}

export type WorkflowStep =
  | AgentStep
  | ToolStep
  | ApprovalStep
  | InputStep
  | BranchStep
  | ParallelStep
  | ForeachStep
  | LoopStep
  | EvaluateStep;
export type StepType = WorkflowStep["type"];
export type LoopKindStep = ForeachStep | LoopStep;

export function isLoopStep(step: { type: string }): step is LoopKindStep {
  return step.type === "foreach" || step.type === "loop";
}

/** Run-wide limits; omitted while empty. */
export interface WorkflowLimits {
  /** USD as a decimal string or number (0.0001 to 1000). */
  max_cost_usd?: string | number | null;
}

export interface WorkflowSpec {
  spec_version: 1;
  name: string;
  description?: string;
  input_schema: JsonSchema;
  output_schema?: Record<string, unknown> | null;
  steps: WorkflowStep[];
  start: string;
  limits?: WorkflowLimits | null;
}

export interface Workflow {
  id: string;
  name: string;
  draft_spec: WorkflowSpec | null;
  latest_version: number | null;
  /** Archived workflows leave the list and refuse new runs and schedules (409 `workflow_archived`). */
  archived?: boolean;
  archived_at?: string | null;
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

/** The active workflows, or with `archived: true` the archived ones only (`?archived=true`). */
export function listWorkflows({ archived = false }: { archived?: boolean } = {}): Promise<Workflow[]> {
  return getJson<Workflow[]>(archived ? "/api/v1/workflows?archived=true" : "/api/v1/workflows");
}

/** Archive refused (409 `runs_active`): the workflow has queued or running runs; `force` archives anyway. */
export class WorkflowArchiveBlocked extends ApiError {
  constructor(message: string, public activeRuns: number) {
    super(409, message);
    this.name = "WorkflowArchiveBlocked";
  }
}

async function postArchive(id: string, body: { archived: boolean; force?: boolean }): Promise<Workflow> {
  const response = await fetch(`${BASE_URL}/api/v1/workflows/${enc(id)}/archive`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...apiHeaders() },
    body: JSON.stringify(body),
  });
  if (response.status === 409) {
    const data = await response.clone().json().catch(() => null);
    if (data && data.code === "runs_active") {
      throw new WorkflowArchiveBlocked(typeof data.detail === "string" ? data.detail : "The workflow has active runs", Number(data.active_runs) || 0);
    }
  }
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

/** Idempotent; answers the workflow with its new `archived` flag. Throws WorkflowArchiveBlocked
 * (409 `runs_active`) while runs are queued or running, unless `force`. Archiving pauses its schedules. */
export function archiveWorkflow(id: string, options: { force?: boolean } = {}): Promise<Workflow> {
  return postArchive(id, { archived: true, force: options.force === true });
}

/** Idempotent. Its paused schedules stay paused. */
export function unarchiveWorkflow(id: string): Promise<Workflow> {
  return postArchive(id, { archived: false });
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
  { version, runId, rerunOf, pauseAfterEachStep }: { version?: number | null; runId?: string; rerunOf?: string | null; pauseAfterEachStep?: boolean } = {},
): Promise<WorkflowRunAccepted> {
  const body: { input: Record<string, unknown>; version?: number; run_id?: string; rerun_of?: string; pause_after_each_step?: boolean } = { input };
  if (version != null) body.version = version;
  if (runId) body.run_id = runId;
  if (rerunOf) body.rerun_of = rerunOf;
  if (pauseAfterEachStep) body.pause_after_each_step = true;
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

/** Answer a waiting input step with `data` (POST .../steps/{id}/resume). Throws ApiError (404, 409
 * not waiting or already decided, 422 with `detail` [{loc, msg, type}]). */
export async function resumeStep(
  runId: string,
  stepId: string,
  data: Record<string, unknown>,
  comment?: string,
): Promise<ApprovalDecisionResult> {
  const trimmed = comment?.trim();
  const response = await fetch(`${BASE_URL}/api/v1/workflows/runs/${enc(runId)}/steps/${enc(stepId)}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...apiHeaders() },
    body: JSON.stringify(trimmed ? { data, comment: trimmed } : { data }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

/** 202 answer of POST /runs/{run_id}/cancel (RunCancelResponse). */
export interface RunCancelResult {
  run_id: string;
  kind: "workflow";
  /** The run's status after the cancel, as its `end` event fixes it. */
  status: string;
  display_status: RunStatus;
  error_code: string | null;
  /** True when the run's `end` is the cancel's (`run_cancelled`); false when it ended by itself first. */
  cancelled: boolean;
  requested_by: string | null;
  approvals_closed: number;
}

/**
 * Cancel a workflow run and its whole tree (idempotent: a retry answers 202 again). Throws ApiError
 * with the server's detail: 404 unknown run, 409 already ended or not a workflow run, 503 recorded
 * but not finished (it completes automatically; retrying finishes it at once).
 */
export async function cancelRun(runId: string): Promise<RunCancelResult> {
  const response = await fetch(`${BASE_URL}/api/v1/runs/${enc(runId)}/cancel`, {
    method: "POST",
    headers: apiHeaders(),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

/**
 * Workflow runs whose start answer the page has not seen: same semantics as tool runs (a lost
 * response reuses the run id, no automatic retry), in its own sessionStorage slot. The ledger's
 * `toolId` is the workflow id and `inputKey` covers the input AND the version.
 */
export const workflowRunLedger = createRunLedger("eap.studio.workflowRuns.v1");
