// The tenant-wide eval lists behind the Datasets, Scorers and Experiments pages (W1.5):
// `GET /eval-suites`, `GET /evals/scorers` and `GET /evals/runs`, each paginated by an opaque cursor.
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import type { EvalAggregates, EvalRun, EvalSuite } from "./evals";

export interface SuiteLastRun {
  run_id: string;
  version: number | null;
  status: string;
  display_status: string;
  created_at: string;
  updated_at: string;
  aggregates: Pick<EvalAggregates, "cases" | "passed" | "failed" | "errors" | "pass_rate">;
}

/** A suite with its agent's name and its newest run (null: never run). */
export interface OverviewSuite extends EvalSuite {
  agent_name: string | null;
  agent_archived: boolean;
  last_run: SuiteLastRun | null;
}

export interface OverviewRun extends EvalRun {
  agent_name: string | null;
  suite_name: string | null;
}

export interface ScorerSuite {
  id: string;
  name: string;
  agent_id: string;
  agent_name: string | null;
  case_level_uses: number;
}

export interface ScorerPoint {
  run_id: string;
  suite_id: string;
  agent_id: string | null;
  created_at: string;
  /** `score`: the mean score; `pass_rate`: the share of passed assertions. */
  metric: "score" | "pass_rate";
  value: number;
}

export interface Scorer {
  kind: string;
  name: string;
  suite_count: number;
  suites: ScorerSuite[];
  /** Newest first. */
  history: ScorerPoint[];
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export const OVERVIEW_PAGE_LIMIT = 50;

async function get<T>(path: string, params: Record<string, string | number | null | undefined>, signal?: AbortSignal): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== null && value !== undefined && value !== "") query.set(key, String(value));
  const response = await fetch(`${BASE_URL}${path}?${query}`, { headers: apiHeaders(), signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json() as Promise<T>;
}

export function listAllSuites(opts: { after?: string | null; limit?: number; signal?: AbortSignal } = {}): Promise<Page<OverviewSuite>> {
  return get("/api/v1/eval-suites", { limit: opts.limit ?? OVERVIEW_PAGE_LIMIT, after: opts.after }, opts.signal);
}

export function listAllScorers(opts: { after?: string | null; limit?: number; signal?: AbortSignal } = {}): Promise<Page<Scorer>> {
  return get("/api/v1/evals/scorers", { limit: opts.limit ?? OVERVIEW_PAGE_LIMIT, after: opts.after }, opts.signal);
}

export function listAllEvalRuns(opts: { before?: string | null; limit?: number; signal?: AbortSignal } = {}): Promise<Page<OverviewRun>> {
  return get("/api/v1/evals/runs", { limit: opts.limit ?? OVERVIEW_PAGE_LIMIT, before: opts.before }, opts.signal);
}

/** A scorer's identity in lists and tests. */
export function scorerKey(scorer: Pick<Scorer, "kind" | "name">): string {
  return `${scorer.kind}:${scorer.name}`;
}
