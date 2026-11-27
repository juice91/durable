// Draft vs published workflow: canonical compare, a step-level summary and a raw JSON line diff.
// Pure functions; the workflow page's notice, Review changes sheet and publish dialog use them.
import type { InstructionsDiffLine } from "@/lib/agentVersions";
import type { Workflow, WorkflowSpec, WorkflowStep, WorkflowVersion } from "@/lib/workflows";

/** JSON with object keys sorted at every depth, so key order never matters. */
export function canonicalJson(value: unknown, indent?: number): string {
  const sort = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(sort);
    if (v && typeof v === "object") {
      return Object.fromEntries(Object.keys(v as object).sort().map((k) => [k, sort((v as Record<string, unknown>)[k])]));
    }
    return v;
  };
  return JSON.stringify(sort(value), null, indent);
}

/** True when the draft differs from the latest published spec (a missing side counts as different). */
export function draftDiffers(draft: WorkflowSpec | null | undefined, live: WorkflowSpec | null | undefined): boolean {
  if (!draft) return false;
  if (!live) return true;
  return canonicalJson(draft) !== canonicalJson(live);
}

/** Every step of a spec by id, loop bodies included. */
export function stepsById(spec: { steps: WorkflowStep[] } | null | undefined): Map<string, WorkflowStep> {
  const out = new Map<string, WorkflowStep>();
  const visit = (steps: WorkflowStep[] | undefined) => {
    for (const step of steps ?? []) {
      out.set(step.id, step);
      const body = (step as { body?: { steps?: WorkflowStep[] } }).body;
      if (body?.steps) visit(body.steps);
    }
  };
  visit(spec?.steps);
  return out;
}

export interface StepChange {
  id: string;
  /** Top-level fields of the step that differ, each as "field (before → after)" for short values. */
  fields: string[];
}

export interface WorkflowChanges {
  added: string[];
  removed: string[];
  changed: StepChange[];
  /** Spec-level fields that differ (start, description, input_schema, output_schema, limits, name). */
  specFields: string[];
  total: number;
}

const SPEC_FIELDS = ["name", "description", "start", "input_schema", "output_schema", "limits"] as const;

function short(value: unknown): string {
  if (value === undefined || value === null) return "none";
  const text = typeof value === "string" ? value : canonicalJson(value);
  return text.length > 40 ? `${text.slice(0, 37)}…` : text;
}

function fieldDiffs(before: object, after: object): string[] {
  const a = before as Record<string, unknown>;
  const b = after as Record<string, unknown>;
  const keys = Array.from(new Set([...Object.keys(a), ...Object.keys(b)])).sort();
  const out: string[] = [];
  for (const key of keys) {
    if (canonicalJson(a[key]) === canonicalJson(b[key])) continue;
    const scalar = (v: unknown) => v === undefined || v === null || typeof v !== "object";
    out.push(scalar(a[key]) && scalar(b[key]) ? `${key} ${short(a[key])} → ${short(b[key])}` : `${key} changed`);
  }
  return out;
}

/** Steps added, removed and changed (by step id), and the spec-level fields that changed. */
export function summarizeChanges(live: WorkflowSpec | null | undefined, draft: WorkflowSpec): WorkflowChanges {
  const before = stepsById(live);
  const after = stepsById(draft);
  const added = [...after.keys()].filter((id) => !before.has(id));
  const removed = [...before.keys()].filter((id) => !after.has(id));
  const changed: StepChange[] = [];
  for (const [id, step] of after) {
    const old = before.get(id);
    if (!old) continue;
    // A loop's body steps are listed under their own ids, so leave them out of the loop's own diff.
    const strip = (s: WorkflowStep) => Object.fromEntries(Object.entries(s).filter(([k]) => k !== "body"));
    const loop = "body" in step || "body" in old;
    const fields = fieldDiffs(loop ? strip(old) : old, loop ? strip(step) : step);
    if (loop) {
      const ob = (old as { body?: { start?: string } }).body;
      const nb = (step as { body?: { start?: string } }).body;
      if (ob?.start !== nb?.start) fields.push(`body start ${short(ob?.start)} → ${short(nb?.start)}`);
    }
    if (fields.length > 0) changed.push({ id, fields });
  }
  const specFields = SPEC_FIELDS.filter((f) => canonicalJson(live?.[f]) !== canonicalJson(draft[f]));
  return { added, removed, changed, specFields: [...specFields], total: added.length + removed.length + changed.length + specFields.length };
}

/** A line diff of two texts (LCS), in the shape the agents' side-by-side rows take. */
export function lineDiff(base: string, target: string): InstructionsDiffLine[] {
  const a = base.split("\n");
  const b = target.split("\n");
  const n = a.length;
  const m = b.length;
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) for (let j = m - 1; j >= 0; j -= 1) lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
  const out: InstructionsDiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n || j < m) {
    if (i < n && j < m && a[i] === b[j]) { out.push({ op: "equal", text: a[i], base_line: i + 1, target_line: j + 1 }); i += 1; j += 1; }
    else if (j >= m || (i < n && lcs[i + 1][j] >= lcs[i][j + 1])) { out.push({ op: "removed", text: a[i], base_line: i + 1, target_line: null }); i += 1; }
    else { out.push({ op: "added", text: b[j], base_line: null, target_line: j + 1 }); j += 1; }
  }
  return out;
}

/** The latest published version of a workflow, from a versions list. */
export function latestVersionOf(workflow: Pick<Workflow, "latest_version">, versions: WorkflowVersion[]): WorkflowVersion | null {
  return versions.find((v) => v.version === workflow.latest_version) ?? versions[0] ?? null;
}
