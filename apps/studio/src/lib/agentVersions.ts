// Version compare and restore (GET /agents/{id}/versions/compare, POST /agents/{id}/versions/{v}/restore).
import { rememberRevision, revisionFromEtag, revisionKey } from "./revisions";
import { BASE_URL, apiErrorFromResponse, apiHeaders } from "@/lib/api";
import type { AgentSpec, AgentVersion } from "@/lib/api";

/** One side of a compare: a published version number, or the agent's draft. */
export type CompareRef = number | "draft";

export interface CompareSide {
  version: number | "draft";
  spec: AgentSpec;
  spec_hash: string | null;
  created_at: string | null;
  created_by: string | null;
  restored_from: number | null;
}

export interface SpecChange {
  path: string;
  kind: "added" | "removed" | "changed";
  before: unknown;
  after: unknown;
}

export interface InstructionsDiffLine {
  op: "equal" | "added" | "removed";
  text: string;
  base_line: number | null;
  target_line: number | null;
}

export interface VersionCompareResult {
  agent_id: string;
  base: CompareSide;
  target: CompareSide;
  identical: boolean;
  changes: SpecChange[];
  truncated: boolean;
  instructions_diff: InstructionsDiffLine[];
  /** The instructions were too large for a line diff: `instructions_diff` is then a whole-block
   * replace (every base line removed, then every target line added) or empty. Absent on older APIs. */
  instructions_diff_truncated?: boolean;
}

export function compareRefLabel(ref: CompareRef): string {
  return ref === "draft" ? "Draft" : `v${ref}`;
}

export async function compareVersions(agentId: string, base: CompareRef, target: CompareRef): Promise<VersionCompareResult> {
  const query = new URLSearchParams({ base: String(base), target: String(target) });
  const response = await fetch(
    `${BASE_URL}/api/v1/agents/${encodeURIComponent(agentId)}/versions/compare?${query.toString()}`,
    { headers: apiHeaders() },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

/** Publishes a new version whose spec equals `version`; resolves to the new version (restored_from = version). */
export async function restoreVersion(agentId: string, version: number, options: { discardDraft?: boolean } = {}): Promise<AgentVersion> {
  const response = await fetch(
    `${BASE_URL}/api/v1/agents/${encodeURIComponent(agentId)}/versions/${version}/restore`,
    {
      method: "POST",
      headers: apiHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ discard_draft: options.discardDraft === true }),
    },
  );
  if (!response.ok) throw await apiErrorFromResponse(response);
  // A restore rewrites the draft and moves the agent's revision: the next save must send the new ETag.
  rememberRevision(revisionKey.agent(agentId), revisionFromEtag(response.headers?.get?.("ETag")));
  return response.json();
}

/** The 409 a restore gets when the draft has unpublished changes (retry with discardDraft). */
export function isDirtyDraftConflict(status: number, message: string): boolean {
  return status === 409 && /draft has unpublished changes/i.test(message);
}
