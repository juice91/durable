// Provenance of the Architect's drafts and publish cards (system-aware Architect, phase C):
// GET /api/v1/definitions/{kind}/{id}/provenance. Hashes, versions and tool-call names only.
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";

export type ProvenanceKind = "agent" | "workflow";

export interface ProvenanceToolCall {
  name: string;
  args_hash: string;
  outcome: string;
}

export interface ProvenanceRow {
  id: string;
  kind: ProvenanceKind;
  definition_id: string;
  spec_hash: string;
  version: number | null;
  run_id: string | null;
  plan_id: string | null;
  created_at: string;
  card: "draft" | "publish";
  platform_version: string | null;
  manifest_version: number | null;
  manifest_hash: string | null;
  capability_snapshot_hash: string | null;
  registry_version: string | null;
  tool_calls: ProvenanceToolCall[];
  degraded: string[];
  truncated: string[];
  model: string | null;
}

export interface ProvenanceChange {
  id: string;
  was: string | null;
  now: string | null;
}

export interface Provenance {
  definition_id: string;
  kind: ProvenanceKind;
  items: ProvenanceRow[];
  capabilities_changed: ProvenanceChange[];
}

export async function getProvenance(kind: ProvenanceKind, definitionId: string, card?: "draft" | "publish"): Promise<Provenance> {
  const response = await fetch(`${BASE_URL}/api/v1/definitions/${kind}/${encodeURIComponent(definitionId)}/provenance`, {
    headers: apiHeaders(),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  const data = (await response.json()) as Provenance;
  return card ? { ...data, items: data.items.filter((row) => row.card === card) } : data;
}

/** The first characters of a hash, for the one-line footer. */
export function shortHash(hash: string | null | undefined, length = 8): string {
  return hash ? hash.slice(0, length) : "unknown";
}

/** "Platform 0.1.0+a1b2c3d · capabilities snapshot 9f3c1a2b", the collapsed footer line. */
export function footerLine(row: ProvenanceRow): string {
  return `Platform ${row.platform_version ?? "unknown"} · capabilities snapshot ${shortHash(row.capability_snapshot_hash)}`;
}

/** The capabilities-changed notice, or null when nothing changed. */
export function changedNotice(changes: readonly ProvenanceChange[]): string | null {
  if (changes.length === 0) return null;
  return `${changes.length} ${changes.length === 1 ? "capability" : "capabilities"} changed since this was drafted`;
}
