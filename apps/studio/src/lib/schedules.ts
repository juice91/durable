// Workflow schedules (parity round 2, D2).
//
// GET /api/v1/workflows/schedules (all), GET|POST /workflows/{id}/schedules,
// PATCH|DELETE /workflows/schedules/{id}. A schedule fires the workflow's latest published version.
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import { revisionKey, track, trackAll, writeResource } from "./revisions";

export interface WorkflowSchedule {
  id: string;
  workflow_id: string;
  workflow_name: string | null;
  name: string;
  cron: string;
  timezone: string;
  input: unknown;
  active: boolean;
  /** Why the worker stopped the schedule ("input_schema_errors: ..."); null while healthy. */
  last_error: string | null;
  last_fired_at: string | null;
  next_fire_at: string | null;
  created_at: string;
  updated_at: string;
  /** The optimistic-concurrency token (`ETag`); see revisions.ts. */
  revision?: number;
}

export interface WorkflowScheduleCreate {
  name: string;
  cron: string;
  timezone: string;
  input: Record<string, unknown>;
}

export interface WorkflowSchedulePatch {
  name?: string;
  cron?: string;
  timezone?: string;
  input?: Record<string, unknown>;
  active?: boolean;
}

export interface SchedulePage {
  items: WorkflowSchedule[];
  next_before: string | null;
}

const enc = encodeURIComponent;

/** Readable copy for the API's schedule error codes; others (input_schema_errors) show the code and its list. */
const CODE_MESSAGES: Record<string, string> = {
  schedule_name_taken: "A schedule with this name already exists",
  invalid_timezone: "Unknown timezone (use an IANA name such as UTC)",
  invalid_cron: "Not a valid 5-field cron expression",
};

async function call(path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...apiHeaders(), ...(init.headers as Record<string, string> | undefined) },
  });
  if (!response.ok) {
    const error = await apiErrorFromResponse(response.clone());
    // Schedule errors carry `{detail: {code, message?, errors?}}`: show the code (and message) as the reason.
    try {
      const data = await response.json();
      const detail = data?.detail;
      if (detail && typeof detail === "object" && !Array.isArray(detail) && typeof detail.code === "string") {
        const errors = Array.isArray(detail.errors) ? ` ${detail.errors.map(String).join("; ")}` : "";
        error.message = typeof detail.message === "string" && detail.message ? detail.message : CODE_MESSAGES[detail.code] ?? `${detail.code}${errors ? `:${errors}` : ""}`;
      }
    } catch {
      // keep the status fallback
    }
    throw error;
  }
  return response;
}

/** One page of schedules, newest first: one workflow's, or every workflow's when `workflowId` is omitted. */
export async function listSchedules({ workflowId, before, limit, signal }: { workflowId?: string; before?: string; limit?: number; signal?: AbortSignal } = {}): Promise<SchedulePage> {
  const params = new URLSearchParams();
  if (limit) params.set("limit", String(limit));
  if (before) params.set("before", before);
  const query = params.toString();
  const path = workflowId ? `/api/v1/workflows/${enc(workflowId)}/schedules` : "/api/v1/workflows/schedules";
  const page: SchedulePage = await (await call(`${path}${query ? `?${query}` : ""}`, { signal })).json();
  trackAll(page.items, (item) => revisionKey.schedule(item.id));
  return page;
}

/** Create a schedule (201). Throws ApiError: 409 schedule_name_taken, 422 invalid_cron / invalid_timezone / input_schema_errors. */
export async function createSchedule(workflowId: string, body: WorkflowScheduleCreate): Promise<WorkflowSchedule> {
  const made: WorkflowSchedule = await (await call(`/api/v1/workflows/${enc(workflowId)}/schedules`, { method: "POST", body: JSON.stringify(body) })).json();
  return track(revisionKey.schedule(made.id), made);
}

/**
 * Patch a schedule; `{active: true}` resumes a stopped one (the API re-validates and clears last_error).
 * Sends the revision this tab last read as `If-Match`; a stale one throws ConflictError (412).
 */
export async function updateSchedule(id: string, patch: WorkflowSchedulePatch): Promise<WorkflowSchedule> {
  return writeResource(revisionKey.schedule(id), async (headers) =>
    (await call(`/api/v1/workflows/schedules/${enc(id)}`, { method: "PATCH", headers, body: JSON.stringify(patch) })).json());
}

export async function deleteSchedule(id: string): Promise<void> {
  await call(`/api/v1/workflows/schedules/${enc(id)}`, { method: "DELETE" });
}

/** "Active", "Paused" or "Stopped: <reason>". */
export function scheduleState(schedule: Pick<WorkflowSchedule, "active" | "last_error">): string {
  if (schedule.active) return "Active";
  return schedule.last_error ? `Stopped: ${schedule.last_error}` : "Paused";
}
