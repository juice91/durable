// The audit log: GET /api/v1/audit (design 9). One row per mutating call: who, what, on which resource, the outcome.
// Never a request body, a secret or a token: the API's `detail` is an allowlist.
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import { rememberSubject, subjectLabel } from "./subjects";

export type AuditOutcome = "success" | "denied" | "failed";

export interface AuditRow {
  id: string;
  at: string;
  actor_kind: string;
  actor_subject: string;
  on_behalf_of: string | null;
  action: string;
  resource_kind: string | null;
  resource_id: string | null;
  outcome: AuditOutcome;
  request_id: string | null;
  run_id: string | null;
  detail: Record<string, unknown> | null;
}

export interface AuditPage {
  items: AuditRow[];
  next_cursor: string | null;
  /** Days rows are kept; null: without limit. */
  retention_days: number | null;
}

export interface AuditFilters {
  actor: string;
  action: string;
  resource_kind: string;
  resource_id: string;
  outcome: "" | AuditOutcome;
  /** `datetime-local` values (the browser's zone); converted to ISO instants for the API. */
  since: string;
  until: string;
}

export const EMPTY_AUDIT_FILTERS: AuditFilters = {
  actor: "", action: "", resource_kind: "", resource_id: "", outcome: "", since: "", until: "",
};

function instant(local: string): string | null {
  if (!local) return null;
  const t = Date.parse(local);
  return Number.isNaN(t) ? null : new Date(t).toISOString();
}

/** The query string for a page: empty filters are left out. */
export function auditParams(filters: AuditFilters, cursor: string | null, limit = 50): URLSearchParams {
  const params = new URLSearchParams();
  for (const key of ["actor", "action", "resource_kind", "resource_id", "outcome"] as const) {
    const value = filters[key].trim();
    if (value) params.set(key, value);
  }
  const since = instant(filters.since);
  const until = instant(filters.until);
  if (since) params.set("since", since);
  if (until) params.set("until", until);
  if (cursor) params.set("cursor", cursor);
  params.set("limit", String(limit));
  return params;
}

export async function listAudit(filters: AuditFilters, cursor: string | null = null): Promise<AuditPage> {
  const response = await fetch(`${BASE_URL}/api/v1/audit?${auditParams(filters, cursor)}`, { headers: apiHeaders(), cache: "no-store" });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as AuditPage;
}

/** Who a row names: the principal, and the person it acted for when that differs. */
export function actorLabel(row: AuditRow): string {
  const detail = row.detail?.actor_email;
  rememberSubject(row.actor_subject, typeof detail === "string" ? detail : null); // a `zitadel:<sub>` actor shows as its e-mail
  const actor = subjectLabel(row.actor_subject);
  return row.on_behalf_of && row.on_behalf_of !== row.actor_subject ? `${actor} (for ${subjectLabel(row.on_behalf_of)})` : actor;
}
