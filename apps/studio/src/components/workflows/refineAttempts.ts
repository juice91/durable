// LP.15: the views of an evaluate step's output ({passed, score, reason, results}, LP.14) and of a
// refine loop's attempts (loops D15: a `loop` whose body has an `evaluate` step). Pure: they read the
// step details the run view derived from the run's events, never anything else.
import type { IterationDetail, IterationState } from "@/lib/workflowRunState";
import type { EvaluateStep, WorkflowStep } from "@/lib/workflows";

/** One evaluator's entry in an evaluate step's `results`. */
export interface EvaluatorResultView {
  name: string;
  kind: string | null;
  /** null: skipped (it compares with an `expected` that is null) or not recorded. */
  passed: boolean | null;
  score: number | null;
  reason: string | null;
  skipped: boolean;
  /** A failed evaluator (it raised, or its judge request failed): its error code or type. */
  errorCode: string | null;
}

export interface EvaluationView {
  passed: boolean | null;
  score: number | null;
  reason: string | null;
  results: EvaluatorResultView[];
}

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function finite(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** An evaluate step's output as the panel shows it; null when the output is not that shape. */
export function evaluationView(output: unknown): EvaluationView | null {
  if (!output || typeof output !== "object" || Array.isArray(output)) return null;
  const o = output as Record<string, unknown>;
  if (typeof o.passed !== "boolean" && !Array.isArray(o.results)) return null;
  const results = (Array.isArray(o.results) ? o.results : [])
    .filter((r): r is Record<string, unknown> => !!r && typeof r === "object" && !Array.isArray(r))
    .map((r, i) => ({
      name: str(r.name) ?? `evaluator ${i + 1}`,
      kind: str(r.kind),
      passed: typeof r.passed === "boolean" ? r.passed : null,
      score: finite(r.score),
      reason: str(r.reason),
      skipped: r.skipped === true,
      errorCode: str(r.error_code) ?? str(r.error_type),
    }));
  return {
    passed: typeof o.passed === "boolean" ? o.passed : null,
    score: finite(o.score),
    reason: str(o.reason),
    results,
  };
}

/** One attempt of a refine loop: an iteration, what its evaluate step judged and its verdict. */
export interface RefineAttempt {
  /** 0-based iteration index (shown as index + 1). */
  index: number;
  scope: string;
  iterationState: IterationState;
  /** The judged value (the evaluate step's `value` read from this iteration); undefined when it
   * cannot be read here (a ref to the run input, an outer loop, or a step that did not finish). */
  draft?: unknown;
  /** Null while the iteration's evaluate step has not completed. */
  evaluation: EvaluationView | null;
  /** The evaluate step failed (e.g. `evaluate_failed`) or the cost cap stopped it. */
  errorCode: string | null;
}

function refsIn(value: unknown, out: string[], depth = 0): string[] {
  if (depth > 8 || !value || typeof value !== "object") return out;
  if (Array.isArray(value)) {
    for (const v of value) refsIn(v, out, depth + 1);
    return out;
  }
  const o = value as Record<string, unknown>;
  if (typeof o.$ref === "string") out.push(o.$ref);
  for (const v of Object.values(o)) refsIn(v, out, depth + 1);
  return out;
}

/**
 * The evaluate step a refine loop's attempts are judged by: the body's evaluate step the loop's
 * `until` (or `while`) reads, else its first evaluate step; null for a foreach or a loop without one.
 */
export function refineEvaluateStep(loop: WorkflowStep | null): EvaluateStep | null {
  if (!loop || loop.type !== "loop") return null;
  const evaluates = (loop.body?.steps ?? []).filter((s): s is EvaluateStep => s.type === "evaluate");
  if (evaluates.length === 0) return null;
  const read = new Set(
    refsIn(loop.until ?? loop.while ?? null, [])
      .map((ref) => /^steps\.([^.]+)\./.exec(ref)?.[1])
      .filter((id): id is string => !!id),
  );
  return evaluates.find((s) => read.has(s.id)) ?? evaluates[0];
}

function walk(value: unknown, path: readonly string[]): unknown {
  let cur = value;
  for (const seg of path) {
    if (cur === null || typeof cur !== "object") return undefined;
    if (Array.isArray(cur)) {
      if (!/^\d+$/.test(seg)) return undefined;
      cur = cur[Number(seg)];
    } else {
      if (!Object.prototype.hasOwnProperty.call(cur, seg)) return undefined;
      cur = (cur as Record<string, unknown>)[seg];
    }
  }
  return cur;
}

/** Reads `ref` in iteration `it` of loop `loopId`: a body step's output or this loop's state. */
export function readInIteration(ref: string, it: IterationDetail, loopId: string): unknown {
  const parts = ref.split(".");
  if (parts[0] === "steps" && parts.length >= 3 && parts[2] === "output") {
    const detail = it.steps[parts[1]];
    if (!detail || !detail.hasOutput) return undefined;
    return walk(detail.output, parts.slice(3));
  }
  if (parts[0] === "loop" && parts[1] === loopId && parts[2] === "state") return walk(it.stateIn, parts.slice(3));
  return undefined;
}

/** The attempts of a refine loop, one per iteration in index order; [] when `loop` is not one. */
export function refineAttempts(loop: WorkflowStep | null, iterations: readonly IterationDetail[] | null | undefined): RefineAttempt[] {
  const evaluate = refineEvaluateStep(loop);
  if (!evaluate || !loop || !iterations) return [];
  const ref = typeof evaluate.value?.$ref === "string" ? evaluate.value.$ref : null;
  return [...iterations]
    .sort((a, b) => a.index - b.index)
    .map((it) => {
      const detail = it.steps[evaluate.id];
      return {
        index: it.index,
        scope: it.scope,
        iterationState: it.state,
        draft: ref ? readInIteration(ref, it, loop.id) : undefined,
        evaluation: detail?.hasOutput ? evaluationView(detail.output) : null,
        errorCode: detail?.errorCode ?? null,
      };
    });
}
