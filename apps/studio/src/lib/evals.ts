// The test suites (evals) client for the agent page's Tests tab (AC.16): the AC.12 routes
// (suites, cases, import and export, eval runs, results, compare, side-effect warnings), their
// shapes, and the pure rules the tab uses (evaluator forms, expected output, labels, deltas).
import { apiErrorFromResponse, apiHeaders, BASE_URL, ApiError } from "./api";

// ---------------------------------------------------------------------------- shapes

export type EvaluatorKind =
  | "equals_expected"
  | "contains"
  | "regex"
  | "schema_valid"
  | "field_equals"
  | "max_duration"
  | "max_cost"
  | "llm_judge";

export interface ModelRefValue {
  provider: string;
  name: string;
}

/** One evaluator as the API stores it (`eap_schemas.evals`, discriminated by `kind`). */
export type Evaluator =
  | { kind: "equals_expected"; name?: string | null }
  | { kind: "contains"; name?: string | null; value?: unknown; case_sensitive?: boolean }
  | { kind: "regex"; name?: string | null; pattern: string }
  | { kind: "schema_valid"; name?: string | null; json_schema?: Record<string, unknown> | null }
  | { kind: "field_equals"; name?: string | null; path: string; value: unknown }
  | { kind: "max_duration"; name?: string | null; seconds: number }
  | { kind: "max_cost"; name?: string | null; usd: string | number }
  | {
      kind: "llm_judge";
      name?: string | null;
      rubric: string;
      model: ModelRefValue;
      include_input?: boolean;
      include_expected?: boolean;
      pass_threshold?: number | null;
    };

export interface EvalSuite {
  id: string;
  agent_id: string;
  name: string;
  description: string | null;
  evaluators: Evaluator[];
  case_count: number;
  archived: boolean;
  archived_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface EvalCase {
  id: string;
  suite_id: string;
  name: string;
  input: string;
  expected_output: unknown;
  metadata: Record<string, unknown>;
  evaluators: Evaluator[];
  position: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface EvalSuiteWithCases extends EvalSuite {
  cases: EvalCase[];
}

export interface EvalAggregates {
  cases: number;
  passed: number;
  failed: number;
  errors: number;
  pass_rate: number | null;
  scores_avg: Record<string, number>;
  duration_ms_p50: number | null;
  cost_usd: string | null;
}

/** An eval run (`GET .../evals/runs[/{id}]`): the run shape without `input`, plus the suite and aggregates. */
export interface EvalRun {
  run_id: string;
  kind?: string;
  agent_id: string | null;
  version: number | null;
  status: string;
  display_status: string;
  error: string | null;
  created_at: string;
  updated_at: string;
  trace_id: string | null;
  suite_id: string | null;
  suite_hash: string | null;
  aggregates?: EvalAggregates;
  [key: string]: unknown;
}

export interface EvalRunPage {
  items: EvalRun[];
  next_cursor: string | null;
}

export interface EvalOutcome {
  value: unknown;
  reason?: string | null;
}

export interface EvaluatorFailure {
  name: string;
  kind: string;
  error_type?: string | null;
  error: string;
  [key: string]: unknown;
}

export type EvalCaseStatus = "passed" | "failed" | "error";

export interface EvalCaseResult {
  case_id: string;
  case_name: string;
  position: number;
  child_run_id: string | null;
  status: EvalCaseStatus;
  output: unknown;
  assertions: Record<string, EvalOutcome>;
  scores: Record<string, EvalOutcome>;
  labels: Record<string, EvalOutcome>;
  evaluator_failures: EvaluatorFailure[];
  duration_ms: number | null;
  cost_usd: string | null;
  error_code: string | null;
  created_at: string | null;
}

export interface EvalResultPage {
  items: EvalCaseResult[];
  next_cursor: string | null;
}

export interface SideEffectWarning {
  code: "tool_side_effect" | "mcp_tool_side_effect" | string;
  tool: string;
  kind: "builtin" | "mcp" | string;
  message: string;
}

export interface EvalWarnings {
  version: number | null;
  draft: boolean;
  warnings: SideEffectWarning[];
}

/** What the run is against: the latest version (null), a version number, or the draft. */
export type EvalTarget = number | "draft" | null;

export interface StartedEvalRun {
  run_id: string;
  status: string;
  display_status: string;
  agent_id: string;
  version: number | null;
  draft: boolean;
  suite_id: string;
  suite_hash: string;
  cases: number;
  max_concurrency: number;
  trace_id: string | null;
  warnings: SideEffectWarning[];
}

export type CaseChange = "fixed" | "regressed" | "unchanged" | "added" | "removed";

export interface CompareSide {
  run_id: string;
  agent_id: string;
  agent_version: number | null;
  draft: boolean;
  suite_id: string | null;
  suite_hash: string | null;
  status: string;
  aggregates: EvalAggregates;
}

export interface CompareCase {
  name: string;
  base_case_id: string | null;
  target_case_id: string | null;
  base_status: EvalCaseStatus | null;
  target_status: EvalCaseStatus | null;
  change: CaseChange;
  assertions: Record<string, { base: unknown; target: unknown }>;
  score_deltas: Record<string, number | null>;
  duration_ms_delta: number | null;
  cost_usd_delta: string | null;
}

export interface EvalCompare {
  base: CompareSide;
  target: CompareSide;
  same_suite: boolean;
  warnings: string[];
  summary: {
    fixed: number;
    regressed: number;
    unchanged: number;
    added: number;
    removed: number;
    pass_rate_delta: number | null;
    scores_avg_delta: Record<string, number | null>;
    cost_usd_delta: string | null;
    duration_ms_p50_delta: number | null;
  };
  cases: CompareCase[];
  base_version: number | null;
  target_version: number | null;
}

export interface ImportResult {
  created: number;
  updated: number;
  suite: EvalSuite;
}

export type ExportFormat = "yaml" | "json";

/** The body of a case create or update. */
export interface EvalCaseBody {
  name: string;
  input: string;
  expected_output: unknown;
  metadata: Record<string, unknown>;
  evaluators: Evaluator[];
}

export interface EvalSuiteBody {
  name: string;
  description: string | null;
  evaluators: Evaluator[];
}

// ---------------------------------------------------------------------------- limits (eap_schemas.evals)

export const MAX_SUITE_EVALUATORS = 10;
export const MAX_CASE_EVALUATORS = 5;
export const MAX_SUITE_CASES = 500;
export const MAX_NAME_CHARS = 100;
export const MAX_DESCRIPTION_CHARS = 2000;
export const MAX_CASE_INPUT_BYTES = 32 * 1024;
export const MAX_REGEX_PATTERN_CHARS = 200;
export const MAX_RUBRIC_BYTES = 4 * 1024;
export const MAX_CASE_METADATA_KEYS = 16;
export const DEFAULT_MAX_CONCURRENCY = 4;
export const MAX_MAX_CONCURRENCY = 8;
export const MAX_IMPORT_BYTES = 1024 * 1024;
/** Results asked for per page (the API caps it at 500). */
export const RESULTS_PAGE_LIMIT = 500;
export const RUNS_PAGE_LIMIT = 50;

/** The memory note (plan decision "Evals with memory and knowledge agents"). */
export const MEMORY_NOTE = "Evaluation runs each case as a single turn without memory";
/** The knowledge note: retrieval runs per case and its embedding cost is in the case cost. */
export const KNOWLEDGE_NOTE = "Each case runs its own retrieval, and its embedding cost counts in that case's cost";

// ---------------------------------------------------------------------------- requests

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: apiHeaders({ "Content-Type": "application/json", ...(init.headers as Record<string, string> | undefined) }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

const seg = encodeURIComponent;

export function listSuites(agentId: string, opts: { includeArchived?: boolean; signal?: AbortSignal } = {}): Promise<EvalSuite[]> {
  const query = opts.includeArchived ? "?include_archived=true" : "";
  return call<{ items: EvalSuite[] }>(`/api/v1/agents/${seg(agentId)}/eval-suites${query}`, { signal: opts.signal }).then((d) => d.items);
}

export function createSuite(agentId: string, body: EvalSuiteBody & { suite_id?: string }): Promise<EvalSuite> {
  return call<EvalSuite>(`/api/v1/agents/${seg(agentId)}/eval-suites`, { method: "POST", body: JSON.stringify(body) });
}

export function getSuite(suiteId: string, signal?: AbortSignal): Promise<EvalSuiteWithCases> {
  return call<EvalSuiteWithCases>(`/api/v1/eval-suites/${seg(suiteId)}`, { signal });
}

export function updateSuite(suiteId: string, body: Partial<EvalSuiteBody> & { archived?: boolean }): Promise<EvalSuite> {
  return call<EvalSuite>(`/api/v1/eval-suites/${seg(suiteId)}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function deleteSuite(suiteId: string): Promise<void> {
  return call<void>(`/api/v1/eval-suites/${seg(suiteId)}`, { method: "DELETE" });
}

export function addCase(suiteId: string, body: EvalCaseBody): Promise<EvalCase> {
  return call<EvalCase>(`/api/v1/eval-suites/${seg(suiteId)}/cases`, { method: "POST", body: JSON.stringify(body) });
}

export function updateCase(suiteId: string, caseId: string, body: Partial<EvalCaseBody>): Promise<EvalCase> {
  return call<EvalCase>(`/api/v1/eval-suites/${seg(suiteId)}/cases/${seg(caseId)}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function deleteCase(suiteId: string, caseId: string): Promise<void> {
  return call<void>(`/api/v1/eval-suites/${seg(suiteId)}/cases/${seg(caseId)}`, { method: "DELETE" });
}

/** The `pydantic_evals` dataset file: its text and the server's file name. */
export async function exportSuite(suiteId: string, format: ExportFormat): Promise<{ filename: string; text: string; type: string }> {
  const response = await fetch(`${BASE_URL}/api/v1/eval-suites/${seg(suiteId)}/export?format=${format}`, { headers: apiHeaders() });
  if (!response.ok) throw await apiErrorFromResponse(response);
  const text = await response.text();
  const disposition = response.headers.get("content-disposition") ?? "";
  const match = /filename="([^"]+)"/.exec(disposition);
  return { filename: match ? match[1] : `suite.${format}`, text, type: format === "json" ? "application/json" : "application/yaml" };
}

/** The file format from a file name (`.json` → json, else yaml). */
export function importFormatOf(filename: string): ExportFormat {
  return /\.json$/i.test(filename) ? "json" : "yaml";
}

/** Import a dataset file (raw body, ≤ 1 MiB): cases upserted by name, all or nothing. */
export async function importSuite(suiteId: string, text: string, format: ExportFormat): Promise<ImportResult> {
  if (new TextEncoder().encode(text).length > MAX_IMPORT_BYTES) {
    throw new ApiError(422, "the suite file must be at most 1 MiB");
  }
  const response = await fetch(`${BASE_URL}/api/v1/eval-suites/${seg(suiteId)}/import?format=${format}`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": format === "json" ? "application/json" : "application/yaml" }),
    body: text,
  });
  if (!response.ok) throw await importError(response);
  return response.json() as Promise<ImportResult>;
}

/** The import's 422 lists unstorable strings as `{loc, msg}` items or a string; both become a message. */
async function importError(response: Response): Promise<ApiError> {
  return apiErrorFromResponse(response);
}

export function getWarnings(agentId: string, target: EvalTarget, signal?: AbortSignal): Promise<EvalWarnings> {
  const query = target === null ? "" : `?version=${seg(String(target))}`;
  return call<EvalWarnings>(`/api/v1/agents/${seg(agentId)}/evals/warnings${query}`, { signal });
}

export function startEvalRun(
  agentId: string,
  body: { run_id: string; suite_id: string; version: EvalTarget; max_concurrency?: number },
): Promise<StartedEvalRun> {
  return call<StartedEvalRun>(`/api/v1/agents/${seg(agentId)}/evals/runs`, { method: "POST", body: JSON.stringify(body) });
}

export function listEvalRuns(
  agentId: string,
  opts: { suiteId?: string | null; limit?: number; before?: string | null; signal?: AbortSignal } = {},
): Promise<EvalRunPage> {
  const params = new URLSearchParams();
  if (opts.suiteId) params.set("suite_id", opts.suiteId);
  params.set("limit", String(opts.limit ?? RUNS_PAGE_LIMIT));
  if (opts.before) params.set("before", opts.before);
  return call<EvalRunPage>(`/api/v1/agents/${seg(agentId)}/evals/runs?${params}`, { signal: opts.signal });
}

export function getEvalRun(agentId: string, runId: string, signal?: AbortSignal): Promise<EvalRun> {
  return call<EvalRun>(`/api/v1/agents/${seg(agentId)}/evals/runs/${seg(runId)}`, { signal });
}

/** Every result of the run, by position (follows `next_cursor`). */
export async function listAllResults(agentId: string, runId: string, signal?: AbortSignal): Promise<EvalCaseResult[]> {
  const out: EvalCaseResult[] = [];
  let after: string | null = null;
  for (let page = 0; page < 10; page += 1) {
    const params = new URLSearchParams({ limit: String(RESULTS_PAGE_LIMIT) });
    if (after) params.set("after", after);
    const data: EvalResultPage = await call<EvalResultPage>(`/api/v1/agents/${seg(agentId)}/evals/runs/${seg(runId)}/results?${params}`, { signal });
    out.push(...data.items);
    if (!data.next_cursor) break;
    after = data.next_cursor;
  }
  return out;
}

export function compareEvalRuns(agentId: string, base: string, target: string, signal?: AbortSignal): Promise<EvalCompare> {
  const params = new URLSearchParams({ base, target });
  return call<EvalCompare>(`/api/v1/agents/${seg(agentId)}/evals/compare?${params}`, { signal });
}

/** A client-generated run id (idempotent POST). */
export function newRunId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // RFC 4122 v4 from Math.random (only where randomUUID is missing: old test environments).
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

// ---------------------------------------------------------------------------- labels

export const EVALUATOR_KINDS: readonly EvaluatorKind[] = [
  "equals_expected",
  "contains",
  "regex",
  "schema_valid",
  "field_equals",
  "max_duration",
  "max_cost",
  "llm_judge",
];

export const EVALUATOR_LABELS: Record<EvaluatorKind, string> = {
  equals_expected: "Equals expected",
  contains: "Contains",
  regex: "Matches regex",
  schema_valid: "Schema valid",
  field_equals: "Field equals",
  max_duration: "Max duration",
  max_cost: "Max cost",
  llm_judge: "LLM judge",
};

export const CASE_STATUS_LABELS: Record<EvalCaseStatus, string> = { passed: "Passed", failed: "Failed", error: "Error" };

export const CHANGE_LABELS: Record<CaseChange, string> = {
  fixed: "Fixed",
  regressed: "Regressed",
  unchanged: "Unchanged",
  added: "Added",
  removed: "Removed",
};

/** Badge variants (tokens only) for a change and a case status. */
export const CHANGE_VARIANTS: Record<CaseChange, "success" | "destructive" | "neutral" | "info" | "warning"> = {
  fixed: "success",
  regressed: "destructive",
  unchanged: "neutral",
  added: "info",
  removed: "warning",
};

export const STATUS_VARIANTS: Record<EvalCaseStatus, "success" | "destructive" | "warning"> = {
  passed: "success",
  failed: "destructive",
  error: "warning",
};

/** What an evaluator is called in results: its `name`, else its kind (the worker's default name). */
export function evaluatorLabel(evaluator: Evaluator): string {
  return evaluator.name ? evaluator.name : EVALUATOR_LABELS[evaluator.kind] ?? evaluator.kind;
}

/** One line describing an evaluator's settings ("contains “ok”", "judge test:judge"). */
export function evaluatorSummary(evaluator: Evaluator): string {
  switch (evaluator.kind) {
    case "equals_expected":
      return "Output equals the expected output";
    case "contains": {
      const value = evaluator.value === undefined || evaluator.value === null ? "the expected output" : `“${typeof evaluator.value === "string" ? evaluator.value : JSON.stringify(evaluator.value)}”`;
      return `Output contains ${value}${evaluator.case_sensitive === false ? " (any case)" : ""}`;
    }
    case "regex":
      return `Output matches /${evaluator.pattern}/`;
    case "schema_valid":
      return evaluator.json_schema ? "Output matches the given schema" : "Output matches the agent's output schema";
    case "field_equals":
      return `${evaluator.path} = ${JSON.stringify(evaluator.value)}`;
    case "max_duration":
      return `At most ${evaluator.seconds} s`;
    case "max_cost":
      return `At most $${evaluator.usd}`;
    case "llm_judge":
      return `Judged by ${evaluator.model.provider}:${evaluator.model.name}`;
  }
}

/** "v3", "Draft", or "Latest" for a run target. */
export function targetLabel(version: number | null | undefined, draft: boolean): string {
  if (draft) return "Draft";
  return typeof version === "number" ? `v${version}` : "Latest";
}

/** An eval run's target from its run shape: a recorded run always names its version, except a
 * draft run (version null). */
export function runTargetLabel(run: Pick<EvalRun, "version">): string {
  return targetLabel(run.version, run.version === null || run.version === undefined);
}

/** A 0..1 rate as a percent ("83%"); "—" when absent. */
export function formatRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return "—";
  return `${Math.round(rate * 1000) / 10}%`;
}

/** A signed delta of a 0..1 rate in percentage points ("+12.5 pts"); "—" when absent. */
export function formatRateDelta(delta: number | null | undefined): string {
  if (delta === null || delta === undefined || !Number.isFinite(delta)) return "—";
  const pts = Math.round(delta * 1000) / 10;
  return `${pts > 0 ? "+" : pts < 0 ? "−" : "±"}${Math.abs(pts)} pts`;
}

/** A signed number ("+0.25", "−3"), with an optional unit; "—" when absent. */
export function formatSigned(delta: number | string | null | undefined, unit = ""): string {
  if (delta === null || delta === undefined || delta === "") return "—";
  const n = typeof delta === "number" ? delta : Number(delta);
  if (!Number.isFinite(n)) return "—";
  const abs = typeof delta === "string" ? delta.replace(/^-/, "") : String(Math.round(Math.abs(n) * 1e6) / 1e6);
  return `${n > 0 ? "+" : n < 0 ? "−" : "±"}${unit}${abs}`;
}

/** Whether a delta is better (positive), worse (negative) or neutral, for the colour of a delta. For
 * cost and duration, lower is better. */
export function deltaTone(delta: number | string | null | undefined, lowerIsBetter = false): "better" | "worse" | "same" {
  const n = delta === null || delta === undefined || delta === "" ? 0 : Number(delta);
  if (!Number.isFinite(n) || n === 0) return "same";
  return (n > 0) !== lowerIsBetter ? "better" : "worse";
}

/** Assertions passed of the total ("2 / 3"). */
export function assertionCounts(result: Pick<EvalCaseResult, "assertions">): { passed: number; total: number } {
  const values = Object.values(result.assertions ?? {});
  return { passed: values.filter((a) => a.value === true).length, total: values.length };
}

/** A case's error_code when it was stopped, never run, because its agent was archived during the
 * eval run (the worker's CASE_AGENT_ARCHIVED; the eval run then fails `eval_agent_archived`). */
export const CASE_AGENT_ARCHIVED = "agent_archived";

/**
 * Why a case did not pass, in one line: that it was not run because the agent was archived, the
 * agent's error code, the first failed assertion (with its reason, which for a judge is the
 * judge's reason), or the first evaluator failure.
 */
export function failureReason(result: EvalCaseResult): string | null {
  if (result.status === "passed") return null;
  if (result.status === "error" && result.error_code === CASE_AGENT_ARCHIVED && !result.child_run_id) {
    return "Not run: the agent was archived during the eval run";
  }
  if (result.status === "error") return result.error_code ? `Agent run failed: ${result.error_code}` : "Agent run failed";
  for (const [name, outcome] of Object.entries(result.assertions ?? {})) {
    if (outcome.value !== true) return outcome.reason ? `${name}: ${outcome.reason}` : `${name} failed`;
  }
  const failure = result.evaluator_failures?.[0];
  if (failure) return `${failure.name}: ${failure.error}`;
  return null;
}

/** A run that will not produce more results. */
export function isFinishedStatus(status: string | null | undefined): boolean {
  return status === "completed" || status === "failed" || status === "needs_attention";
}

// ---------------------------------------------------------------------------- expected output

export type ExpectedMode = "none" | "text" | "json";

/** How a stored expected output is edited: absent, a string (text), or anything else as JSON. */
export function expectedModeOf(value: unknown): ExpectedMode {
  if (value === null || value === undefined) return "none";
  return typeof value === "string" ? "text" : "json";
}

/** The editor text for a stored expected output in `mode`. */
export function expectedText(value: unknown, mode: ExpectedMode): string {
  if (mode === "none" || value === null || value === undefined) return "";
  if (mode === "text") return typeof value === "string" ? value : JSON.stringify(value);
  return JSON.stringify(value, null, 2);
}

/** The expected output from the editor: null (none), the text, or parsed JSON (error on bad JSON). */
export function parseExpected(mode: ExpectedMode, text: string): { value: unknown } | { error: string } {
  if (mode === "none") return { value: null };
  if (mode === "text") return { value: text };
  if (!text.trim()) return { error: "Enter JSON, or choose None" };
  try {
    return { value: JSON.parse(text) };
  } catch {
    return { error: "Expected output is not valid JSON" };
  }
}

/** Case metadata from its JSON text: a flat object of at most 16 keys (empty text is `{}`). */
export function parseMetadata(text: string): { value: Record<string, unknown> } | { error: string } {
  if (!text.trim()) return { value: {} };
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { error: "Metadata is not valid JSON" };
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return { error: "Metadata must be a JSON object" };
  const entries = Object.entries(parsed as Record<string, unknown>);
  if (entries.length > MAX_CASE_METADATA_KEYS) return { error: `Metadata may have at most ${MAX_CASE_METADATA_KEYS} keys` };
  if (entries.some(([, v]) => v !== null && typeof v === "object")) return { error: "Metadata values must be strings, numbers, booleans or null" };
  return { value: parsed as Record<string, unknown> };
}

// ---------------------------------------------------------------------------- evaluator drafts

/**
 * An evaluator while it is edited: every field as text, so a half-typed number or JSON value is
 * kept until it is valid. `toEvaluator` turns it back into the API shape (or an error).
 */
export interface EvaluatorDraft {
  /** Stable key for the editor's list. */
  key: string;
  kind: EvaluatorKind;
  name: string;
  /** contains: the value ("" = the expected output); field_equals: the JSON value. */
  value: string;
  /** contains: `value` is JSON text (a number, boolean, list or object, as pydantic_evals Contains
   * allows), not a plain string. */
  valueIsJson: boolean;
  caseSensitive: boolean;
  pattern: string;
  /** schema_valid: JSON Schema text ("" = the agent's output schema). */
  jsonSchema: string;
  path: string;
  seconds: string;
  usd: string;
  rubric: string;
  /** "provider:name". */
  model: string;
  includeInput: boolean;
  includeExpected: boolean;
  passThreshold: string;
  /** The stored evaluator this draft was made from, and the draft's fields then: an untouched draft
   * saves the stored evaluator verbatim (its JSON types and number forms kept). */
  original?: Evaluator;
  originalForm?: string;
}

/** A draft's editable fields as one comparable string (not its key or origin). */
export function draftFormKey(draft: EvaluatorDraft): string {
  const { key: _key, original: _original, originalForm: _form, ...fields } = draft;
  return JSON.stringify(fields);
}

let draftCounter = 0;

export function blankEvaluatorDraft(kind: EvaluatorKind = "equals_expected", defaultJudgeModel = ""): EvaluatorDraft {
  draftCounter += 1;
  return {
    key: `ev-${draftCounter}`,
    kind,
    name: "",
    value: "",
    valueIsJson: false,
    caseSensitive: true,
    pattern: "",
    jsonSchema: "",
    path: "",
    seconds: "",
    usd: "",
    rubric: "",
    model: defaultJudgeModel,
    includeInput: false,
    includeExpected: false,
    passThreshold: "",
  };
}

export function modelRefString(ref: ModelRefValue | null | undefined): string {
  return ref ? `${ref.provider}:${ref.name}` : "";
}

/** "provider:name" → a ModelRef (the provider is the text before the first colon); null when malformed. */
export function parseModelRef(text: string): ModelRefValue | null {
  const at = text.indexOf(":");
  if (at <= 0 || at === text.length - 1) return null;
  return { provider: text.slice(0, at).trim(), name: text.slice(at + 1).trim() };
}

export function evaluatorToDraft(evaluator: Evaluator): EvaluatorDraft {
  const draft = blankEvaluatorDraft(evaluator.kind);
  draft.name = evaluator.name ?? "";
  switch (evaluator.kind) {
    case "contains":
      if (evaluator.value === undefined || evaluator.value === null) draft.value = "";
      else if (typeof evaluator.value === "string") draft.value = evaluator.value;
      else {
        draft.value = JSON.stringify(evaluator.value);
        draft.valueIsJson = true;
      }
      draft.caseSensitive = evaluator.case_sensitive !== false;
      break;
    case "regex":
      draft.pattern = evaluator.pattern;
      break;
    case "schema_valid":
      draft.jsonSchema = evaluator.json_schema ? JSON.stringify(evaluator.json_schema, null, 2) : "";
      break;
    case "field_equals":
      draft.path = evaluator.path;
      draft.value = JSON.stringify(evaluator.value);
      break;
    case "max_duration":
      draft.seconds = String(evaluator.seconds);
      break;
    case "max_cost":
      draft.usd = String(evaluator.usd);
      break;
    case "llm_judge":
      draft.rubric = evaluator.rubric;
      draft.model = modelRefString(evaluator.model);
      draft.includeInput = evaluator.include_input === true;
      draft.includeExpected = evaluator.include_expected === true;
      draft.passThreshold = evaluator.pass_threshold === null || evaluator.pass_threshold === undefined ? "" : String(evaluator.pass_threshold);
      break;
    default:
      break;
  }
  draft.original = evaluator;
  draft.originalForm = draftFormKey(draft);
  return draft;
}

function utf8Bytes(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** The API shape of a draft, or the first problem found (shown next to the evaluator). */
export function draftToEvaluator(draft: EvaluatorDraft): { evaluator: Evaluator } | { error: string } {
  // Untouched: the stored evaluator as it was (a Decimal in exponent form, a typed JSON value).
  if (draft.original && draft.originalForm === draftFormKey(draft)) return { evaluator: draft.original };
  const name = draft.name.trim();
  if (name.length > MAX_NAME_CHARS) return { error: `Name must be at most ${MAX_NAME_CHARS} characters` };
  const named = name ? { name } : {};
  switch (draft.kind) {
    case "equals_expected":
      return { evaluator: { kind: "equals_expected", ...named } };
    case "contains": {
      let value: unknown = draft.value;
      if (draft.valueIsJson && draft.value.trim() !== "") {
        try {
          value = JSON.parse(draft.value);
        } catch {
          return { error: "The value is not valid JSON (clear “Value is JSON” for plain text)" };
        }
      }
      return {
        evaluator: {
          kind: "contains",
          ...named,
          ...(draft.value === "" ? {} : { value }),
          ...(draft.caseSensitive ? {} : { case_sensitive: false }),
        },
      };
    }
    case "regex": {
      if (!draft.pattern) return { error: "Enter a pattern" };
      if (draft.pattern.length > MAX_REGEX_PATTERN_CHARS) return { error: `The pattern must be at most ${MAX_REGEX_PATTERN_CHARS} characters` };
      return { evaluator: { kind: "regex", ...named, pattern: draft.pattern } };
    }
    case "schema_valid": {
      if (!draft.jsonSchema.trim()) return { evaluator: { kind: "schema_valid", ...named } };
      try {
        const schema = JSON.parse(draft.jsonSchema);
        if (schema === null || typeof schema !== "object" || Array.isArray(schema)) return { error: "The schema must be a JSON object" };
        return { evaluator: { kind: "schema_valid", ...named, json_schema: schema } };
      } catch {
        return { error: "The schema is not valid JSON" };
      }
    }
    case "field_equals": {
      if (!draft.path.trim()) return { error: "Enter a field path" };
      let value: unknown;
      try {
        value = JSON.parse(draft.value);
      } catch {
        return { error: "The value must be JSON (a string in quotes)" };
      }
      return { evaluator: { kind: "field_equals", ...named, path: draft.path.trim(), value } };
    }
    case "max_duration": {
      const seconds = Number(draft.seconds);
      if (!draft.seconds.trim() || !Number.isFinite(seconds) || seconds <= 0 || seconds > 3600) return { error: "Seconds must be more than 0 and at most 3600" };
      return { evaluator: { kind: "max_duration", ...named, seconds } };
    }
    case "max_cost": {
      const usd = draft.usd.trim();
      // Plain or exponent form (the API may serialise a Decimal as 1E-7); the API checks the places.
      if (!/^\d+(\.\d+)?([eE][-+]?\d+)?$/.test(usd) || Number(usd) <= 0 || Number(usd) > 1000) return { error: "USD must be more than 0 and at most 1000" };
      return { evaluator: { kind: "max_cost", ...named, usd } };
    }
    case "llm_judge": {
      if (!draft.rubric.trim()) return { error: "Enter a rubric" };
      if (utf8Bytes(draft.rubric) > MAX_RUBRIC_BYTES) return { error: "The rubric must be at most 4 KiB" };
      const model = parseModelRef(draft.model);
      if (!model) return { error: "Choose a judge model" };
      let passThreshold: number | undefined;
      if (draft.passThreshold.trim()) {
        passThreshold = Number(draft.passThreshold);
        if (!Number.isFinite(passThreshold) || passThreshold < 0 || passThreshold > 1) return { error: "The pass threshold must be between 0 and 1" };
      }
      return {
        evaluator: {
          kind: "llm_judge",
          ...named,
          rubric: draft.rubric,
          model,
          include_input: draft.includeInput,
          include_expected: draft.includeExpected,
          ...(passThreshold === undefined ? {} : { pass_threshold: passThreshold }),
        },
      };
    }
  }
}

/** Every draft as an evaluator, or the first draft's error with its index; names must be unique. */
export function draftsToEvaluators(drafts: readonly EvaluatorDraft[]): { evaluators: Evaluator[] } | { error: string; index: number } {
  const evaluators: Evaluator[] = [];
  const names = new Set<string>();
  for (const [index, draft] of drafts.entries()) {
    const out = draftToEvaluator(draft);
    if ("error" in out) return { error: out.error, index };
    const name = out.evaluator.name;
    if (name) {
      if (names.has(name)) return { error: `The name “${name}” is used twice`, index };
      names.add(name);
    }
    evaluators.push(out.evaluator);
  }
  return { evaluators };
}

/** The ids of the judge models offered: allowlisted concrete models (wildcard entries are typed). */
export function judgeModelIds(models: readonly { id: string; wildcard: boolean }[]): string[] {
  return models.filter((m) => !m.wildcard).map((m) => m.id);
}

/** Starts a browser download of `text` as `filename` (export). */
export function downloadText(filename: string, text: string, type: string): void {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** `?compare=<base>..<target>` of two eval run ids (UUIDs hold no dots). */
export function parseRunCompare(value: string | null | undefined): { base: string; target: string } | null {
  const match = /^([0-9a-fA-F-]{1,64})\.\.([0-9a-fA-F-]{1,64})$/.exec(value ?? "");
  if (!match || match[1] === match[2]) return null;
  return { base: match[1], target: match[2] };
}

export function runCompareParam(base: string, target: string): string {
  return `${base}..${target}`;
}

/**
 * A 422's details split by evaluator: items whose `loc` is `[body,] evaluators, N, ...` go to row N
 * (their messages joined, with the field after the evaluator's kind when there is one), the rest
 * stay for the form's banner. Null when the error is not an ApiError with details.
 */
export function evaluatorApiErrors(
  reason: unknown,
): { rows: Map<number, string>; other: string[] } | null {
  if (!(reason instanceof ApiError)) return null;
  if (reason.details.length === 0) {
    // The API's own checks (an RE2 refusal, EvalSuiteInvalid) answer a string detail naming the path:
    // "the test suite is invalid at case.evaluators.0.pattern: <reason>".
    const match = /\binvalid at (?:case\.)?evaluators\.(\d+)(?:\.([\w.]+))?: ([\s\S]+)$/.exec(reason.message);
    if (!match) return null;
    const field = match[2] ? match[2].split(".").filter((part) => !(EVALUATOR_KINDS as readonly string[]).includes(part)).join(".") : "";
    return { rows: new Map([[Number(match[1]), field ? `${field}: ${match[3]}` : match[3]]]), other: [] };
  }
  const rows = new Map<number, string>();
  const other: string[] = [];
  for (const item of reason.details) {
    const loc = item.loc[0] === "body" ? item.loc.slice(1) : item.loc;
    if (loc[0] === "evaluators" && typeof loc[1] === "number") {
      // pydantic's union tag (the kind) comes before the field: `evaluators.1.regex.pattern`.
      const rest = loc.slice(2).filter((part) => !(EVALUATOR_KINDS as readonly (string | number)[]).includes(part));
      const message = rest.length > 0 ? `${rest.join(".")}: ${item.msg}` : item.msg;
      const previous = rows.get(loc[1]);
      rows.set(loc[1], previous ? `${previous}; ${message}` : message);
    } else {
      other.push(item.msg);
    }
  }
  return { rows, other };
}
