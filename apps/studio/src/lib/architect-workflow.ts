// The Architect page's workflow side (Architect milestone, Task 8): the restricted workflow format the
// form edits, the drafter and save routes, the client-side preview compile and the mapping of a 422 onto
// the step row that owns the problem. The agent side lives in `architect.ts`.
//
// POST /api/v1/architect/draft-workflow {description, current?}  -> {spec, assumptions, dropped, run_id, model}
// POST /api/v1/architect/workflows {spec, expected_version?, publish} -> {id, workflow_id, version, published, warnings}
// Errors are flat `{detail, code, ...}`: 409 version_mismatch {latest_version}; 422 agent_not_allowed,
// tool_not_allowed, unknown_placeholder, placeholder_not_whole_value, parallel_needs_following_step,
// workflow_invalid, reserved_id; draft: 422 draft_invalid {raw, problems}, 502 draft_failed, 504 draft_timeout.
import { refusedMessage } from "./access";
import { apiHeaders, BASE_URL } from "./api";
import type { PublishWarning } from "@/components/agents/agentCapabilities";
import type { WorkflowSpec, WorkflowStep } from "./workflows";

export const MAX_WORKFLOW_STEPS = 8;
export const MIN_BRANCHES = 2;
export const MAX_BRANCHES = 4;

// ------------------------------------------------------------------------------ restricted format

export interface WfAgentStep {
  kind: "agent";
  id: string;
  /** An Architect-made agent's id (slug). */
  agent: string;
  prompt: string;
}

export interface WfParallelStep {
  kind: "parallel";
  id: string;
  branches: WfAgentStep[];
}

export interface WfToolStep {
  kind: "tool";
  id: string;
  tool: string;
  params: Record<string, unknown>;
}

export type WfStep = WfAgentStep | WfParallelStep | WfToolStep;

export interface WfSpec {
  name: string;
  id?: string | null;
  description: string;
  input_description: string;
  steps: WfStep[];
}

/** What the form needs from GET /architect/options. */
export interface WfOptions {
  tools: { id: string; title: string; description?: string; requires_approval?: boolean }[];
  agents: { id: string; name: string }[];
}

/** One item of GET /architect `agents` (what the form needs of it). */
export interface WfAgentListItem {
  id: string;
  name: string;
  versions?: number;
  archived?: boolean;
}

export interface WfSaved {
  id: string;
  workflow_id: string;
  version: number | null;
  published: boolean;
  warnings: PublishWarning[];
}

export interface WfDraftResult {
  spec: WfSpec;
  assumptions: string[];
  dropped: { kind: string; id: string }[];
  run_id: string;
  model: string | null;
}

export const PLACEHOLDER_INPUT = "{{input}}";
export const PLACEHOLDER_PREVIOUS = "{{previous}}";

export function emptySpec(): WfSpec {
  return { name: "", id: null, description: "", input_description: "The text to work on.", steps: [] };
}

/** A step id not yet used in `steps` (branches included): step_1, step_2, ... */
export function nextStepId(steps: readonly WfStep[], base = "step"): string {
  const used = new Set(allStepIds(steps));
  for (let n = 1; ; n += 1) {
    if (!used.has(`${base}_${n}`)) return `${base}_${n}`;
  }
}

export function allStepIds(steps: readonly WfStep[]): string[] {
  return steps.flatMap((step) => (step.kind === "parallel" ? [step.id, ...step.branches.map((b) => b.id)] : [step.id]));
}

export function newStep(kind: WfStep["kind"], steps: readonly WfStep[], defaultAgent = ""): WfStep {
  if (kind === "tool") return { kind, id: nextStepId(steps, "tool"), tool: "", params: {} };
  if (kind === "parallel") {
    const first = nextStepId(steps, "branch");
    const second = nextStepId([...steps, { kind: "agent", id: first, agent: "", prompt: "" }], "branch");
    return {
      kind,
      id: nextStepId(steps, "parallel"),
      branches: [
        { kind: "agent", id: first, agent: defaultAgent, prompt: PLACEHOLDER_INPUT },
        { kind: "agent", id: second, agent: defaultAgent, prompt: PLACEHOLDER_INPUT },
      ],
    };
  }
  return { kind: "agent", id: nextStepId(steps, "step"), agent: defaultAgent, prompt: steps.length === 0 ? PLACEHOLDER_INPUT : PLACEHOLDER_PREVIOUS };
}

// ------------------------------------------------------------------------------ editable rows

/** A step as the form edits it: tool params are JSON text until saved. */
export type WfRow =
  | { key: string; kind: "agent"; id: string; agent: string; prompt: string }
  | { key: string; kind: "parallel"; id: string; branches: { key: string; id: string; agent: string; prompt: string }[] }
  | { key: string; kind: "tool"; id: string; tool: string; paramsText: string };

let rowSeq = 0;
const rowKey = () => `row-${(rowSeq += 1)}`;

export function rowsFromSteps(steps: readonly WfStep[]): WfRow[] {
  return steps.map((step): WfRow => {
    if (step.kind === "agent") return { key: rowKey(), kind: "agent", id: step.id, agent: step.agent, prompt: step.prompt };
    if (step.kind === "tool") {
      const empty = Object.keys(step.params ?? {}).length === 0;
      return { key: rowKey(), kind: "tool", id: step.id, tool: step.tool, paramsText: empty ? "" : JSON.stringify(step.params, null, 2) };
    }
    return {
      key: rowKey(),
      kind: "parallel",
      id: step.id,
      branches: step.branches.map((b) => ({ key: rowKey(), id: b.id, agent: b.agent, prompt: b.prompt })),
    };
  });
}

export interface RowsResult {
  steps: WfStep[];
  /** Row key -> message, for params that are not a JSON object. */
  errors: Record<string, string>;
}

/** The restricted steps of the rows. A tool row whose params are not a JSON object keeps `{}` and reports an error. */
export function stepsFromRows(rows: readonly WfRow[]): RowsResult {
  const errors: Record<string, string> = {};
  const steps = rows.map((row): WfStep => {
    if (row.kind === "agent") return { kind: "agent", id: row.id, agent: row.agent, prompt: row.prompt };
    if (row.kind === "parallel") {
      return { kind: "parallel", id: row.id, branches: row.branches.map((b) => ({ kind: "agent", id: b.id, agent: b.agent, prompt: b.prompt })) };
    }
    let params: Record<string, unknown> = {};
    if (row.paramsText.trim()) {
      try {
        const parsed: unknown = JSON.parse(row.paramsText);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) params = parsed as Record<string, unknown>;
        else errors[row.key] = "Parameters must be a JSON object.";
      } catch {
        errors[row.key] = "Parameters are not valid JSON.";
      }
    }
    return { kind: "tool", id: row.id, tool: row.tool, params };
  });
  return { steps, errors };
}

// ------------------------------------------------------------------------------ placeholders

/** The placeholders a step may use: {{input}}, {{previous}} when the step before it has an output, and
 *  {{steps.<id>}} for every earlier step (a parallel step's branches count, the parallel step itself does not). */
export function placeholdersFor(steps: readonly WfStep[], index: number): string[] {
  const out = [PLACEHOLDER_INPUT];
  const before = steps.slice(0, index);
  const last = before[before.length - 1];
  if (last && last.kind !== "parallel") out.push(PLACEHOLDER_PREVIOUS);
  for (const step of before) {
    if (step.kind === "parallel") out.push(...step.branches.map((b) => `{{steps.${b.id}}}`));
    else out.push(`{{steps.${step.id}}}`);
  }
  return out;
}

// ------------------------------------------------------------------------------ preview compile

export interface Preview {
  spec: WorkflowSpec;
  /** Rules the API would refuse (a parallel step with nothing after it). */
  problems: { stepId: string; message: string }[];
}

/** The workflow the restricted format compiles to, for the graph preview only (the API compiles the saved
 *  one): top-level steps run in order, a parallel step forks its branches and joins at the next step, so it
 *  cannot be last. Agent ids show the agent's slug. Pure. */
export function compilePreview(spec: Pick<WfSpec, "name" | "description" | "steps">): Preview {
  const problems: Preview["problems"] = [];
  const steps: WorkflowStep[] = [];
  const top = spec.steps;
  top.forEach((step, i) => {
    const following = i + 1 < top.length ? top[i + 1].id : null;
    if (step.kind === "agent") {
      steps.push({ id: step.id, type: "agent", agent_id: step.agent || "(choose an agent)", input: {}, next: following });
    } else if (step.kind === "tool") {
      steps.push({ id: step.id, type: "tool", tool_id: step.tool || "(choose a tool)", input: {}, next: following });
    } else {
      if (following === null) problems.push({ stepId: step.id, message: "A parallel step joins at the step after it, so it cannot be the last step." });
      steps.push({ id: step.id, type: "parallel", branches: step.branches.map((b) => b.id), join: following ?? "" });
      for (const b of step.branches) steps.push({ id: b.id, type: "agent", agent_id: b.agent || "(choose an agent)", input: {}, next: null });
    }
  });
  return {
    spec: {
      spec_version: 1,
      name: spec.name || "Workflow",
      description: spec.description,
      input_schema: { type: "object", properties: { input: { type: "string" } }, required: ["input"] },
      steps,
      start: top[0]?.id ?? "",
    },
    problems,
  };
}

// ------------------------------------------------------------------------------ errors

export class ArchitectApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code: string | null = null,
    public body: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ArchitectApiError";
  }

  get latestVersion(): number | null {
    return typeof this.body.latest_version === "number" ? this.body.latest_version : null;
  }

  get problems(): string[] {
    return Array.isArray(this.body.problems) ? this.body.problems.map(String) : [];
  }

  get raw(): string | null {
    return typeof this.body.raw === "string" ? this.body.raw : null;
  }

  /** A pydantic 422's `detail` items, when the API refused the shape itself. */
  get locations(): { loc: (string | number)[]; msg: string }[] {
    const detail = this.body.detail;
    if (!Array.isArray(detail)) return [];
    return detail.map((e: { loc?: unknown; msg?: unknown }) => ({
      loc: Array.isArray(e.loc) ? e.loc.filter((p): p is string | number => typeof p === "string" || typeof p === "number") : [],
      msg: String(e.msg ?? ""),
    }));
  }
}

async function call(path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...apiHeaders() },
    body: JSON.stringify(body),
  });
  let data: Record<string, unknown> = {};
  try {
    data = await response.json();
  } catch {
    // not JSON: keep the status fallback
  }
  if (!response.ok) {
    const detail = data.detail;
    const said = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((e: { msg?: unknown }) => String(e.msg ?? "")).join(", ") : `HTTP ${response.status}`;
    const message = (await refusedMessage(response.status, data)) ?? said;
    throw new ArchitectApiError(response.status, message, typeof data.code === "string" ? data.code : null, data);
  }
  return data;
}

/** "Fill in the form": one drafter run. `current` makes it an edit of that definition. */
export async function draftWorkflow(description: string, current?: WfSpec | null): Promise<WfDraftResult> {
  const body: Record<string, unknown> = { description };
  if (current && current.steps.length > 0 && current.name) body.current = toApiSpec(current);
  return (await call("/api/v1/architect/draft-workflow", body)) as WfDraftResult;
}

/** Save the draft, or save and publish. `expectedVersion` is the latest version the page last saw (null: none). */
export async function saveWorkflow(spec: WfSpec, opts: { publish: boolean; expectedVersion: number | null }): Promise<WfSaved> {
  const body: Record<string, unknown> = { spec: toApiSpec(spec), publish: opts.publish };
  if (opts.expectedVersion !== null) body.expected_version = opts.expectedVersion;
  const saved = (await call("/api/v1/architect/workflows", body)) as WfSaved;
  return { ...saved, warnings: Array.isArray(saved.warnings) ? saved.warnings : [] };
}

/** The spec as the API takes it: an empty id is left out. */
export function toApiSpec(spec: WfSpec): Record<string, unknown> {
  const out: Record<string, unknown> = {
    name: spec.name.trim(),
    description: spec.description,
    input_description: spec.input_description,
    steps: spec.steps.map((step) => (step.kind === "tool" && Object.keys(step.params).length === 0 ? { kind: "tool", id: step.id, tool: step.tool } : step)),
  };
  if (spec.id && spec.id.trim()) out.id = spec.id.trim();
  return out;
}

// ------------------------------------------------------------------------------ which row owns a problem

export interface StepProblems {
  /** Step or branch id -> messages. */
  byStep: Record<string, string[]>;
  /** What no step owns (name, id, reserved id, workflow_invalid ...). */
  general: string[];
}

function findStepIds(steps: readonly WfStep[], match: (s: WfAgentStep | WfToolStep) => boolean): string[] {
  const ids: string[] = [];
  for (const step of steps) {
    if (step.kind === "parallel") ids.push(...step.branches.filter(match).map((b) => b.id));
    else if (match(step)) ids.push(step.id);
  }
  return ids;
}

/** Sort one refusal onto the step rows that own it. `agent_not_allowed: <slug>` lands on the steps that run
 *  that agent, `tool_not_allowed: <id>` on the tool steps with it, `unknown_placeholder: {{x}}` on the steps
 *  whose prompt or params use it, `placeholder_not_whole_value: param 'k'` on the tool step with that param,
 *  `parallel_needs_following_step` on the last step; a pydantic 422 by its `loc`. Anything else is general. */
export function problemsFor(error: ArchitectApiError, spec: WfSpec): StepProblems {
  const result: StepProblems = { byStep: {}, general: [] };
  const add = (ids: string[], message: string) => {
    if (ids.length === 0) result.general.push(message);
    for (const id of ids) (result.byStep[id] ??= []).push(message);
  };
  const steps = spec.steps;
  const message = error.message;
  const locations = error.locations;
  if (locations.length > 0) {
    for (const { loc, msg } of locations) {
      const at = loc.indexOf("steps");
      const index = at >= 0 && typeof loc[at + 1] === "number" ? (loc[at + 1] as number) : -1;
      const step = steps[index];
      if (!step) {
        result.general.push(`${loc.filter((p) => p !== "body" && p !== "spec").join(".")}: ${msg}`);
        continue;
      }
      const branchAt = loc.indexOf("branches");
      const branch = step.kind === "parallel" && branchAt >= 0 && typeof loc[branchAt + 1] === "number" ? step.branches[loc[branchAt + 1] as number] : undefined;
      add([branch?.id ?? step.id], msg);
    }
    return result;
  }
  const [head, ...rest] = message.split(": ");
  const detail = rest.join(": ");
  switch (error.code ?? head) {
    case "agent_not_allowed": {
      const slug = detail.trim();
      add(findStepIds(steps, (s) => s.kind === "agent" && s.agent === slug), `The agent "${slug}" is not available to workflows: it must be an Architect-made agent that is not archived.`);
      break;
    }
    case "agent_not_published": {
      // A draft may name a draft agent; going live needs every agent to have a published version.
      const named = Array.isArray(error.body.agents) ? (error.body.agents as { id?: string | null; name?: string }[]) : [];
      for (const agent of named) {
        const label = agent.id ?? agent.name ?? "";
        add(findStepIds(steps, (s) => s.kind === "agent" && s.agent === agent.id), `The agent "${label}" has no published version yet: publish it first, then go live.`);
      }
      if (named.length === 0) result.general.push(message);
      break;
    }
    case "tool_not_allowed": {
      const id = detail.trim();
      add(findStepIds(steps, (s) => s.kind === "tool" && s.tool === id), `The tool "${id}" is not offered here.`);
      break;
    }
    case "unknown_placeholder": {
      const token = /\{\{[^{}]*\}\}/.exec(detail)?.[0];
      add(
        token ? findStepIds(steps, (s) => (s.kind === "agent" ? s.prompt.includes(token) : JSON.stringify(s.params).includes(token))) : [],
        message.replace(/^unknown_placeholder: /, "Unknown placeholder: "),
      );
      break;
    }
    case "placeholder_not_whole_value": {
      const param = /param '([^']+)'/.exec(detail)?.[1];
      add(param ? findStepIds(steps, (s) => s.kind === "tool" && param in s.params) : [], "A parameter that uses a placeholder must be exactly that one placeholder, nothing else.");
      break;
    }
    case "parallel_needs_following_step": {
      const last = steps[steps.length - 1];
      add(last && last.kind === "parallel" ? [last.id] : [], "A parallel step joins at the step after it, so it cannot be the last step.");
      break;
    }
    default:
      result.general.push(message);
  }
  return result;
}
