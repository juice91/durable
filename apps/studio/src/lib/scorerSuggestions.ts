// Suggested rules: AI-proposed scorers and suite evaluators for an agent or a workflow
// (`POST /agents|workflows/{id}/scorer-suggestions`), and the steps that turn one into a reusable
// scorer or a suite evaluator.
import { ApiError, apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import { createSuite, createWorkflowSuite, listSuites, listWorkflowSuites, updateSuite, MAX_SUITE_EVALUATORS, type EvalSuite, type EvalTargetRef, type Evaluator } from "./evals";
import { attachScorer, createScorer, type ScorerEvaluator } from "./scorers";

export type SuggestionScope = "scorer" | "evaluator" | "both";

export interface ScorerSuggestion {
  title: string;
  rationale: string;
  scope: SuggestionScope;
  spec: Evaluator;
  evidence: string;
}

export interface ScorerSuggestions {
  suggestions: ScorerSuggestion[];
  model: string | null;
  dropped: number;
}

const NAME_MAX = 100;

/** The 409 the API answers when no model is allowed for the built-in suggester. */
export const NO_MODEL_MESSAGE = "No model is available for suggestions. Allow a model first (Settings, models).";

export async function suggestRules(
  target: EvalTargetRef,
  opts: { max?: number; includeRuns?: boolean; signal?: AbortSignal } = {},
): Promise<ScorerSuggestions> {
  const kind = target.kind === "workflow" ? "workflows" : "agents";
  const response = await fetch(`${BASE_URL}/api/v1/${kind}/${encodeURIComponent(target.id)}/scorer-suggestions`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ max: opts.max ?? 8, include_runs: opts.includeRuns ?? true }),
    signal: opts.signal,
  });
  if (response.status === 409) {
    const body = (await response.clone().json().catch(() => null)) as { code?: unknown } | null;
    if (body?.code === "no_model") throw new ApiError(409, NO_MODEL_MESSAGE);
  }
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as ScorerSuggestions;
}

/** Whether the suggestion can be a reusable scorer on this target (agents only; scope scorer or both). */
export function canBeScorer(target: EvalTargetRef, suggestion: ScorerSuggestion): boolean {
  return target.kind === "agent" && suggestion.scope !== "evaluator";
}

function scorerName(title: string, attempt: number): string {
  const suffix = attempt === 0 ? "" : ` (${attempt + 1})`;
  return `${title.trim().slice(0, NAME_MAX - suffix.length)}${suffix}`;
}

/** Create a reusable scorer named from the title and attach it to the agent at 100% sampling. A name
 * already in use gets a numeric suffix (up to 5 tries). Returns the name used. */
export async function addAsScorer(agentId: string, suggestion: ScorerSuggestion): Promise<string> {
  const { name: _name, ...evaluator } = suggestion.spec as Evaluator & { name?: string | null };
  for (let attempt = 0; attempt < 5; attempt += 1) {
    const name = scorerName(suggestion.title, attempt);
    try {
      const scorer = await createScorer({ name, description: suggestion.rationale, evaluator: evaluator as ScorerEvaluator });
      await attachScorer(agentId, scorer.id, 1);
      return name;
    } catch (reason: unknown) {
      if (!(reason instanceof ApiError && reason.status === 409 && attempt < 4)) throw reason;
    }
  }
  throw new Error("The scorer could not be created");
}

export function listTargetSuites(target: EvalTargetRef, signal?: AbortSignal): Promise<EvalSuite[]> {
  return (target.kind === "workflow" ? listWorkflowSuites : listSuites)(target.id, { signal });
}

export function defaultRulesSuiteName(target: EvalTargetRef): string {
  return `${target.name} rules`.slice(0, NAME_MAX);
}

/** Append the suggestion's evaluator to `suite`, or (suite null) create the "<name> rules" suite with
 * it. Returns the suite's name. A full suite is refused before any request. */
export async function addToSuite(target: EvalTargetRef, suggestion: ScorerSuggestion, suite: EvalSuite | null): Promise<string> {
  const { name: _name, ...rest } = suggestion.spec;
  const evaluator = rest as Evaluator;
  if (suite === null) {
    const name = defaultRulesSuiteName(target);
    const body = { name, description: null, evaluators: [evaluator] };
    return (await (target.kind === "workflow" ? createWorkflowSuite : createSuite)(target.id, body)).name;
  }
  if (suite.evaluators.length >= MAX_SUITE_EVALUATORS) throw new Error(`“${suite.name}” already has the most evaluators a suite can hold (${MAX_SUITE_EVALUATORS}).`);
  await updateSuite(suite.id, { evaluators: [...suite.evaluators, evaluator] });
  return suite.name;
}
