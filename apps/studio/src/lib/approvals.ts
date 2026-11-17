// The Approvals inbox client (AC.15): `GET /approvals` (both kinds, from the pending_approvals
// projection), the tool-call decision routes, the URL filters of `/approvals`, and the shared
// pending count behind the sidebar badge (one poll every 30 s, whatever reads it).
//
// A workflow step is decided through the existing workflow routes (`decideApproval` in
// lib/workflows); a gated tool call through `POST /runs/{run_id}/tool-approvals/{id}/approve|deny`.
import { useSyncExternalStore } from "react";
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import { routes } from "./routes";
import type { JsonSchema } from "./tools";

export type ApprovalKind = "tool" | "step";
export type ApprovalStatus = "pending" | "decided";
/** A recorded decision; a rejected workflow step is `denied`. */
export type ApprovalDecision = "approved" | "denied" | "timed_out";

export const APPROVAL_KINDS: readonly ApprovalKind[] = ["tool", "step"];
export const APPROVAL_STATUSES: readonly ApprovalStatus[] = ["pending", "decided"];

export const APPROVAL_KIND_LABELS: Record<ApprovalKind, string> = { tool: "Tool call", step: "Workflow step" };
export const APPROVAL_DECISION_LABELS: Record<ApprovalDecision, string> = { approved: "Approved", denied: "Denied", timed_out: "Timed out" };

/** One inbox item, as `GET /approvals` serves it. */
export interface Approval {
  run_id: string;
  kind: ApprovalKind;
  /** The tool call id (kind tool) or the step id (kind step). */
  approval_key: string;
  tool_call_id: string | null;
  step_id: string | null;
  /** The workflow run for a workflow agent step's child run, else the run itself. */
  root_run_id: string;
  agent_id: string | null;
  workflow_id: string | null;
  tool_name: string | null;
  /** What the call would do, in words (a tool with a summariser, such as architect.*); else null. */
  summary?: string | null;
  /** The tool call's arguments (≤ 8 KiB, else `{truncated: true}`); null for a step. */
  args: unknown;
  prompt: string | null;
  status: ApprovalStatus;
  requested_at: string | null;
  deadline: string | null;
  decided_at: string | null;
  decision: ApprovalDecision | null;
  decided_by: string | null;
  run_status: string | null;
  /** An input step: the JSON Schema of the data it waits for; null for any other approval. */
  resume_schema?: JsonSchema | null;
  /** A step-by-step pause (the run waits after a step, id `<step>_pause`): Continue, not Approve. */
  pause?: boolean;
  /** The decision's audit (single read, decided steps): the comment and the submitted data. */
  comment?: string | null;
  resume_data?: unknown;
}

export interface ApprovalPage {
  items: Approval[];
  next_cursor: string | null;
}

/** The tool decision routes' comment cap (the API answers 422 above it). */
export const MAX_TOOL_APPROVAL_COMMENT = 500;
/** One page of the inbox. */
export const APPROVALS_PAGE_LIMIT = 50;

/** `/approvals` filters, all in the URL: `?status=decided&kind=tool&agent=<id>`. */
export interface ApprovalFilters {
  status: ApprovalStatus;
  kind: ApprovalKind | null;
  agent_id: string | null;
}

export const EMPTY_APPROVAL_FILTERS: ApprovalFilters = { status: "pending", kind: null, agent_id: null };

/** The URL parameters the filters use (the Sheet's own parameters are separate). */
export const APPROVAL_FILTER_PARAMS = ["status", "kind", "agent"] as const;

interface ParamsLike {
  get(name: string): string | null;
}

export function parseApprovalFilters(params: ParamsLike | null | undefined): ApprovalFilters {
  const status = params?.get("status");
  const kind = params?.get("kind");
  const agent = params?.get("agent")?.trim();
  return {
    status: status === "decided" ? "decided" : "pending",
    kind: kind === "tool" || kind === "step" ? kind : null,
    agent_id: agent ? agent : null,
  };
}

/** The filters as URL parameters; defaults (`pending`, no kind, no agent) are left out. */
export function approvalFiltersToParams(filters: ApprovalFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.status !== "pending") params.set("status", filters.status);
  if (filters.kind) params.set("kind", filters.kind);
  if (filters.agent_id) params.set("agent", filters.agent_id);
  return params;
}

export function hasActiveApprovalFilters(filters: ApprovalFilters): boolean {
  return filters.kind !== null || filters.agent_id !== null;
}

/** The inbox row and Sheet id: `<run_id>:<approval_key>` (a run id never holds a colon). */
export function approvalId(approval: Pick<Approval, "run_id" | "approval_key">): string {
  return `${approval.run_id}:${approval.approval_key}`;
}

/** Splits an approval id at its first colon; null when either side is empty. */
export function parseApprovalId(value: string | null | undefined): { runId: string; key: string } | null {
  if (!value) return null;
  const at = value.indexOf(":");
  if (at <= 0 || at === value.length - 1) return null;
  return { runId: value.slice(0, at), key: value.slice(at + 1) };
}

/** The request query for one page of `GET /approvals`. */
export function approvalsQuery(filters: ApprovalFilters, { cursor = null, limit = APPROVALS_PAGE_LIMIT }: { cursor?: string | null; limit?: number } = {}): URLSearchParams {
  const params = new URLSearchParams();
  params.set("status", filters.status);
  if (filters.kind) params.set("kind", filters.kind);
  if (filters.agent_id) params.set("agent_id", filters.agent_id);
  if (cursor) params.set("cursor", cursor);
  params.set("limit", String(limit));
  return params;
}

export async function listApprovals(
  filters: ApprovalFilters,
  options: { cursor?: string | null; limit?: number; signal?: AbortSignal } = {},
): Promise<ApprovalPage> {
  const response = await fetch(`${BASE_URL}/api/v1/approvals?${approvalsQuery(filters, options).toString()}`, {
    headers: apiHeaders(),
    signal: options.signal,
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

/** One approval, pending or decided (`GET /approvals/{run_id}/{kind}/{key}`); null on 404. */
export async function getApproval(runId: string, kind: ApprovalKind, key: string, { signal }: { signal?: AbortSignal } = {}): Promise<Approval | null> {
  const response = await fetch(
    `${BASE_URL}/api/v1/approvals/${encodeURIComponent(runId)}/${kind}/${encodeURIComponent(key)}`,
    { headers: apiHeaders(), signal },
  );
  if (response.status === 404) return null;
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

/**
 * The approval a Sheet id names. With a known `kind` (the inbox row, or the item just decided) it
 * is one request for that kind. A Sheet id from a URL has no kind: a run holds approvals of one
 * kind only (steps on workflow runs, tool calls on agent runs, including a workflow agent step's
 * child run), so the run's own kind (`GET /runs/{id}`) picks the route, and the other kind's
 * route is never asked (it would answer 404). Null when the run or the approval does not exist
 * (or is another tenant's); other errors are thrown.
 */
export async function findApproval(
  runId: string,
  key: string,
  { kind, signal }: { kind?: ApprovalKind; signal?: AbortSignal } = {},
): Promise<Approval | null> {
  const resolved = kind ?? (await approvalKindOfRun(runId, signal));
  if (resolved === null) return null;
  return getApproval(runId, resolved, key, { signal });
}

/** The approval kind a run holds: `step` on a workflow run, else `tool`; null for an unknown run. */
async function approvalKindOfRun(runId: string, signal?: AbortSignal): Promise<ApprovalKind | null> {
  const response = await fetch(`${BASE_URL}/api/v1/runs/${encodeURIComponent(runId)}`, { headers: apiHeaders(), signal });
  // 404 an unknown (or another tenant's) run; 422 a malformed run id.
  if (response.status === 404 || response.status === 422) return null;
  if (!response.ok) throw await apiErrorFromResponse(response);
  const run: { kind?: string | null } = await response.json();
  return run.kind === "workflow" ? "step" : "tool";
}

/** How often an open Sheet re-reads a decided approval until its run ends (as the trace Sheet). */
export const APPROVAL_FOLLOW_POLL_MS = 3_000;
/** A failed re-read waits twice as long as the last one, up to this. */
export const APPROVAL_FOLLOW_MAX_DELAY_MS = 30_000;
/** Consecutive failed re-reads after which the Sheet stops following and says so. */
export const APPROVAL_FOLLOW_MAX_FAILURES = 3;

export interface ToolApprovalDecisionResult {
  run_id: string;
  tool_call_id: string;
  decision: "approved" | "denied";
  decided_by: string | null;
  comment: string | null;
  notified: boolean;
}

/**
 * Approve or deny a gated tool call. 404: unknown run or a call that never waited; 409: already
 * decided (the body then names the recorded decision) or the run no longer waits; 422: comment
 * over 500 characters. Errors are thrown as ApiError.
 */
export async function decideToolApproval(
  runId: string,
  toolCallId: string,
  decision: "approve" | "deny",
  comment?: string,
): Promise<ToolApprovalDecisionResult> {
  const trimmed = comment?.trim();
  const response = await fetch(
    `${BASE_URL}/api/v1/runs/${encodeURIComponent(runId)}/tool-approvals/${encodeURIComponent(toolCallId)}/${decision}`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", ...apiHeaders() },
      body: JSON.stringify(trimmed ? { comment: trimmed } : {}),
    },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

// --- Decisions made on a card ----------------------------------------------------------------

const decisionListeners = new Set<(decision: "approved" | "denied") => void>();

/** Calls `listener` after any tool approval card records a decision (the Architect page refreshes
 * what it created). Returns the unsubscribe. */
export function onToolDecided(listener: (decision: "approved" | "denied") => void): () => void {
  decisionListeners.add(listener);
  return () => {
    decisionListeners.delete(listener);
  };
}

export function announceToolDecided(decision: "approved" | "denied"): void {
  decisionListeners.forEach((listener) => listener(decision));
}

// --- The pending count (sidebar badge) -------------------------------------------------------

/** The badge polls `GET /approvals?status=pending` this often. */
export const PENDING_POLL_MS = 30_000;
/** The count request's page size; a next cursor (more pages) shows as "99+". */
export const PENDING_COUNT_LIMIT = 100;
/** Counts above this show as "99+". */
export const PENDING_BADGE_MAX = 99;

/** The pending count: `count` items on the first page, `more` when there are further pages. */
export interface PendingCount {
  count: number;
  more: boolean;
}

let pending: PendingCount | null = null;
let timer: ReturnType<typeof setInterval> | null = null;
let inFlight: Promise<void> | null = null;
/** A refresh asked for while a fetch was in flight: one follow-up fetch for all such requests. */
let rerun: Promise<void> | null = null;
/**
 * The number of the latest fetch whose answer may be shown. A refresh asked for during a fetch
 * bumps it, so that fetch's answer (asked before, say, a decision) is ignored as stale.
 */
let seq = 0;
const subscribers = new Set<() => void>();

function emitPending() {
  subscribers.forEach((listener) => listener());
}

function fetchPendingCount(): Promise<void> {
  const mine = ++seq;
  const request: Promise<void> = listApprovals(EMPTY_APPROVAL_FILTERS, { limit: PENDING_COUNT_LIMIT })
    .then((page) => {
      if (mine !== seq) return; // a newer refresh was asked for: this answer is stale
      const next = { count: page.items.length, more: page.next_cursor !== null };
      if (!pending || pending.count !== next.count || pending.more !== next.more) {
        pending = next;
        emitPending();
      }
    })
    .catch(() => {
      // Keep the last known count: a failed poll must not flash the badge away.
    })
    .finally(() => {
      if (inFlight === request) inFlight = null;
    });
  inFlight = request;
  return request;
}

/**
 * Fetch the pending count now (also called after a decision, so the badge follows at once). While
 * a fetch is in flight, its answer is discarded and one follow-up fetch serves every request made
 * meanwhile.
 */
export function refreshPendingApprovals(): Promise<void> {
  if (!inFlight) return fetchPendingCount();
  seq += 1;
  if (!rerun) {
    const after = inFlight;
    const next: Promise<void> = after.then(() => {
      if (rerun === next) rerun = null;
      return fetchPendingCount();
    });
    rerun = next;
  }
  return rerun;
}

function pageHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

function startPolling() {
  if (timer === null) timer = setInterval(() => void refreshPendingApprovals(), PENDING_POLL_MS);
}

function stopPolling() {
  if (timer !== null) clearInterval(timer);
  timer = null;
}

/** A hidden tab stops polling; on becoming visible it catches up once and polls again. */
function onVisibilityChange() {
  if (subscribers.size === 0) return;
  if (pageHidden()) {
    stopPolling();
  } else {
    void refreshPendingApprovals();
    startPolling();
  }
}

function subscribePending(listener: () => void): () => void {
  subscribers.add(listener);
  if (subscribers.size === 1) {
    if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVisibilityChange);
    if (!pageHidden()) {
      void refreshPendingApprovals();
      startPolling();
    }
  }
  return () => {
    subscribers.delete(listener);
    if (subscribers.size === 0) {
      stopPolling();
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVisibilityChange);
    }
  };
}

/** The shared pending count, or null before the first answer (or when the API is unreachable). */
export function usePendingApprovals(): PendingCount | null {
  return useSyncExternalStore(subscribePending, () => pending, () => null);
}

/** The badge text: null at 0 (hidden), "99+" above the cap. */
export function pendingBadgeLabel(value: PendingCount | null | undefined): string | null {
  if (!value || value.count <= 0) return null;
  if (value.more || value.count > PENDING_BADGE_MAX) return `${PENDING_BADGE_MAX}+`;
  return String(value.count);
}

/** Tests: the shared count as the hook would read it now. */
export function pendingSnapshotForTests(): PendingCount | null {
  return pending;
}

/** Tests: whether the badge poll's interval is running. */
export function isPendingPollingForTests(): boolean {
  return timer !== null;
}

/** Tests: forget the shared count and stop polling. */
export function resetPendingApprovalsForTests(): void {
  seq += 1;
  pending = null;
  inFlight = null;
  rerun = null;
  stopPolling();
  subscribers.clear();
  if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVisibilityChange);
}

// --- Inbox helpers ---------------------------------------------------------------------------

/** The row after a decision made from the inbox: decided now, by this user (the API's answer). */
export function applyDecision(
  approval: Approval,
  decision: "approved" | "denied",
  { decidedAt = new Date().toISOString(), decidedBy = null }: { decidedAt?: string; decidedBy?: string | null } = {},
): Approval {
  return { ...approval, status: "decided", decision, decided_at: decidedAt, decided_by: decidedBy };
}

/** The row's subject: the gated tool's name, or the workflow step id. */
export function approvalSubject(approval: Approval): string {
  return approval.kind === "tool" ? approval.tool_name || approval.approval_key : approval.step_id || approval.approval_key;
}

/** Whether the item is still decidable here: pending, on a run that has not ended. */
export function isDecidable(approval: Approval): boolean {
  return approval.status === "pending" && approval.decision === null && !TERMINAL_RUN_STATUSES.has(approval.run_status ?? "");
}

/** Run statuses after which nothing waits any more (eap_run_store TERMINAL_RUN_STATUSES). */
export const TERMINAL_RUN_STATUSES: ReadonlySet<string> = new Set(["completed", "failed", "needs_attention"]);

export interface ApprovalLinks {
  /** The agent conversation the waiting turn belongs to (a tool call outside a workflow). */
  conversation: string | null;
  /** The workflow run view: the step's run, or the workflow run that owns the agent step's child run. */
  workflowRun: string | null;
}

/**
 * Where an approval leads. `conversationId` comes from the run (`GET /runs/{id}`); a tool call
 * inside a workflow agent step links to the workflow run instead (its child run has no
 * conversation). The trace Sheet link is the run id itself (`TraceLink`).
 */
export function approvalLinks(approval: Approval, { conversationId = null }: { conversationId?: string | null } = {}): ApprovalLinks {
  const workflowRun = approval.workflow_id ? routes.workflowRun(approval.workflow_id, approval.root_run_id) : null;
  const conversation =
    approval.kind === "tool" && approval.agent_id && conversationId && !workflowRun
      ? routes.agentChat(approval.agent_id, { conversation: conversationId })
      : null;
  return { conversation, workflowRun };
}

function noticeTime(iso: string | null): string {
  if (!iso) return "";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? ` at ${iso}` : ` at ${date.toLocaleString()}`;
}

/**
 * What the Sheet says after this user's decision lost a race (409), from the item as re-fetched:
 * who decided first and when, a timeout, or the run ending. Always ends by saying this decision
 * was not recorded, so nobody reads the other person's decision as their own.
 */
export function raceNotice(fresh: Approval | null): string {
  const notRecorded = "Your decision was not recorded.";
  if (!fresh) return `This approval is no longer available. ${notRecorded}`;
  if (fresh.decision === "timed_out") return `No one decided in time: it timed out${noticeTime(fresh.decided_at)}. ${notRecorded}`;
  if (fresh.decision === "approved" || fresh.decision === "denied") {
    const by = fresh.decided_by ? ` by ${fresh.decided_by}` : "";
    return `Already ${fresh.decision}${by}${noticeTime(fresh.decided_at)}. ${notRecorded}`;
  }
  if (TERMINAL_RUN_STATUSES.has(fresh.run_status ?? "")) return `The run ended (${(fresh.run_status ?? "").replace(/_/g, " ")}) before a decision. ${notRecorded}`;
  return `This approval is no longer waiting. ${notRecorded}`;
}
