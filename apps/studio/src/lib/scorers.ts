// Reusable scorers (W2.3): `/scorers` CRUD, an agent's attachments (`/agents/{id}/scorers`), and the
// `score` events the worker records on a run (read through the run's events).
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import { listEvents } from "./runs";

export const SCORER_KINDS = ["contains", "regex", "schema_valid", "field_equals", "max_cost"] as const;
export type ScorerKind = (typeof SCORER_KINDS)[number];

export const SCORER_KIND_LABELS: Record<ScorerKind, string> = {
  contains: "Contains text",
  regex: "Matches regex",
  schema_valid: "Valid against the output schema",
  field_equals: "Field equals",
  max_cost: "Max cost",
};

export type ScorerEvaluator =
  | { kind: "contains"; value: string; case_sensitive?: boolean }
  | { kind: "regex"; pattern: string }
  | { kind: "schema_valid"; json_schema?: Record<string, unknown> | null }
  | { kind: "field_equals"; path: string; value: unknown }
  | { kind: "max_cost"; usd: string | number };

export interface ReusableScorer {
  id: string;
  name: string;
  description: string | null;
  evaluator: ScorerEvaluator;
  /** The agents the scorer is attached to. */
  agent_ids: string[];
  created_at: string;
  updated_at: string;
}

export interface AttachedScorer {
  id: string;
  name: string;
  evaluator: ScorerEvaluator;
  sampling_rate: number;
}

/** The `score` run event's data. */
export interface ScoreEvent {
  seq: number;
  ts: string;
  scorer: string;
  scorer_id: string;
  score: number | null;
  passed: boolean;
  reason: string | null;
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...apiHeaders(init.headers || {}) },
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

export async function listReusableScorers(signal?: AbortSignal): Promise<ReusableScorer[]> {
  return (await call<{ items: ReusableScorer[] }>("/api/v1/scorers", { signal })).items;
}

export function createScorer(body: { name: string; description?: string | null; evaluator: ScorerEvaluator }): Promise<ReusableScorer> {
  return call("/api/v1/scorers", { method: "POST", body: JSON.stringify(body) });
}

export function deleteScorer(id: string): Promise<void> {
  return call(`/api/v1/scorers/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export async function listAgentScorers(agentId: string, signal?: AbortSignal): Promise<AttachedScorer[]> {
  return (await call<{ items: AttachedScorer[] }>(`/api/v1/agents/${encodeURIComponent(agentId)}/scorers`, { signal })).items;
}

export async function attachScorer(agentId: string, scorerId: string, samplingRate = 1): Promise<AttachedScorer[]> {
  const path = `/api/v1/agents/${encodeURIComponent(agentId)}/scorers/${encodeURIComponent(scorerId)}`;
  return (await call<{ items: AttachedScorer[] }>(path, { method: "PUT", body: JSON.stringify({ sampling_rate: samplingRate }) })).items;
}

export function detachScorer(agentId: string, scorerId: string): Promise<void> {
  return call(`/api/v1/agents/${encodeURIComponent(agentId)}/scorers/${encodeURIComponent(scorerId)}`, { method: "DELETE" });
}

/** The run's `score` events (written by the worker after the run), oldest first. */
export async function listRunScores(runId: string, signal?: AbortSignal): Promise<ScoreEvent[]> {
  const events = await listEvents(runId, undefined, signal);
  const scores: ScoreEvent[] = [];
  for (const event of events) {
    if ((event.type as string) !== "score") continue;
    const data = event.data as Record<string, unknown>;
    scores.push({
      seq: event.seq,
      ts: event.ts,
      scorer: String(data.scorer ?? ""),
      scorer_id: String(data.scorer_id ?? ""),
      score: typeof data.score === "number" ? data.score : null,
      passed: data.passed === true,
      reason: typeof data.reason === "string" ? data.reason : null,
    });
  }
  return scores;
}

/** The evaluator as a short line for lists. */
export function describeEvaluator(evaluator: ScorerEvaluator): string {
  switch (evaluator.kind) {
    case "contains": return `contains “${evaluator.value}”`;
    case "regex": return `matches /${evaluator.pattern}/`;
    case "schema_valid": return "valid against the output schema";
    case "field_equals": return `${evaluator.path} equals ${JSON.stringify(evaluator.value)}`;
    case "max_cost": return `costs at most $${evaluator.usd}`;
  }
}

/** A form's fields as an evaluator, or an error message. */
export function evaluatorFromForm(kind: ScorerKind, primary: string, secondary: string): ScorerEvaluator | string {
  const text = primary.trim();
  switch (kind) {
    case "contains": return text ? { kind, value: primary, case_sensitive: false } : "Enter the text to look for.";
    case "regex": return text ? { kind, pattern: text } : "Enter a pattern.";
    case "schema_valid": return { kind };
    case "max_cost": return Number(text) > 0 ? { kind, usd: text } : "Enter a cost above zero, in USD.";
    case "field_equals": {
      if (!text) return "Enter the field path.";
      try {
        return { kind, path: text, value: JSON.parse(secondary) as unknown };
      } catch {
        return "The value must be JSON (for example \"ok\", 3 or true).";
      }
    }
  }
}
