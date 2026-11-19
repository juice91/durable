// The state of every step of a workflow run, derived from the run's durable events (pure).
//
// Events are merged by seq (duplicates dropped, any arrival order) and replayed in seq order, so a
// reconnect, a replay or an events refetch can never move a step backwards or count it twice.
// A step is `skipped` when it can no longer run: every edge into it is dead (its source was
// skipped, or its source is a branch that chose another target). A parallel's join stays alive
// through the parallel's own `join` edge. When the run completes, steps still pending are skipped.
//
// Loops (LP.12, loops D4): an event whose data carries a non-empty `scope` belongs to a loop body
// instance. It never touches the top-level step of the same id: it fills that iteration's own
// details (`iterations`, keyed by the iteration's scope string `L.i` / `outer.2/inner.0`). The
// `loop_iteration_*` events give each iteration its own state. `resolveRunStates` then picks one
// iteration per loop (the pager's choice, else the latest) and gives the graph, the step list and
// the Timeline the states and details of that iteration's body steps.
import type { RunEvent, RunStatus } from "./runs";
import { stepEdges } from "./workflowGraph";
import { isLoopStep, type LoopKindStep, type StepType, type WorkflowSpec, type WorkflowStep } from "./workflows";

export type NodeState = "pending" | "running" | "waiting" | "completed" | "failed" | "skipped" | "needs_attention";

export const NODE_STATES: NodeState[] = ["pending", "running", "waiting", "completed", "failed", "skipped", "needs_attention"];

export interface StepDecision {
  kind: "approved" | "rejected" | "timed_out";
  decidedBy: string | null;
  comment: string | null;
}

export interface StepDetail {
  stepId: string;
  state: NodeState;
  stepType: StepType | null;
  started: boolean;
  input?: unknown;
  hasOutput: boolean;
  output?: unknown;
  error: string | null;
  errorCode: string | null;
  childRunId: string | null;
  agentId: string | null;
  toolId: string | null;
  prompt: string | null;
  deadline: string | null;
  timeoutHours: number | null;
  /** An input step's JSON Schema (from its waiting event); null otherwise. */
  resumeSchema?: Record<string, unknown> | null;
  /** A step-by-step pause (`<step>_pause`, from the waiting event's `pause: true`). */
  pause?: boolean;
  decision: StepDecision | null;
  /** Why a `skipped` step was skipped: an untaken branch arm, the run completed before reaching it
   * (the completion sweep), or (a loop body step) its iteration ended without it. Null unless
   * state is `skipped`. */
  skipReason: "branch_not_taken" | "run_ended" | "iteration_ended" | null;
  startedAt: string | null;
  endedAt: string | null;
  /** AC.9: structured-field refs (paths, never values) that resolved to nothing in this step. */
  unresolvedRefs?: string[];
  /** `on_error: continue`: the tool's error code, from a step_completed with `outcome: "error_kept"`; null otherwise. */
  errorKept?: string | null;
}

/** One frame of a loop scope (loops D4): the loop step's id and the 0-based iteration index. */
export interface ScopeFrame {
  loop: string;
  index: number;
}

/** The state of one loop iteration: `running` until its `loop_iteration_completed|failed`. */
export type IterationState = "running" | "completed" | "failed" | "needs_attention";

export interface IterationDetail {
  loopId: string;
  /** 0-based (the pager shows index + 1). */
  index: number;
  /** The iteration's own scope string: `each.3`, `outer.2/inner.0`. */
  scope: string;
  /** The scope string of the enclosing iteration; "" for a top-level loop. */
  parentScope: string;
  state: IterationState;
  startedAt: string | null;
  endedAt: string | null;
  error: string | null;
  errorCode: string | null;
  /** The body step that failed the iteration (loop_iteration_failed `failed_step_id`). */
  failedStepId: string | null;
  /** The loop state the iteration started with (loop steps; absent for foreach). */
  stateIn?: unknown;
  /** The state carried out of the iteration (loop_iteration_completed `state`). */
  stateOut?: unknown;
  /** An until loop's verdict after this iteration: "true" ends the loop; "unknown" is a MISSING
   * operand (the loop goes on); null for a while loop or a foreach. */
  until: "true" | "false" | "unknown" | null;
  /** The body steps of this iteration, every one of the loop's body steps (pending until it runs). */
  steps: Record<string, StepDetail>;
  /** Body edge ids that this iteration can no longer take (an untaken branch arm). */
  deadEdges: Set<string>;
}

export interface WorkflowRunView {
  steps: Record<string, StepDetail>;
  /** Loop iterations by their scope string (`each.3`, `outer.2/inner.0`). */
  iterations: Record<string, IterationDetail>;
  /** A `cancel_requested` event arrived (the run is being cancelled, or was). */
  cancelRequested: boolean;
  /** Who asked for the cancel (`cancel_requested` `requested_by`). */
  cancelRequestedBy: string | null;
  /** The run ended with `run_cancelled` (shown as "Cancelled", not "Failed"). */
  cancelled: boolean;
  /** The run's status: the terminal event wins over a lagging status frame or run row. */
  status: RunStatus;
  terminal: RunEvent | null;
  /** Steps waiting for an approval decision, in the order they started waiting. */
  waiting: string[];
  /** Edge ids (workflowGraph edge ids) that can no longer be taken. */
  deadEdges: Set<string>;
  /** Run-level failure without a step (or for a step the spec does not have). */
  runError: { error: string | null; errorCode: string | null; stepId: string | null } | null;
  output: unknown;
}

const TERMINAL: ReadonlySet<string> = new Set(["completed", "failed", "needs_attention"]);

/** The error code a cancelled run ends with (loops D12). */
export const RUN_CANCELLED = "run_cancelled";

export function isTerminal(status: string | null | undefined): boolean {
  return Boolean(status && TERMINAL.has(status));
}

/**
 * Merge `incoming` into `existing`: one event per seq, sorted by seq. Returns `existing` itself
 * when nothing new arrived, so React state does not change.
 */
export function mergeEvents(existing: RunEvent[], incoming: RunEvent[]): RunEvent[] {
  const bySeq = new Map<number, RunEvent>();
  for (const event of existing) bySeq.set(event.seq, event);
  let added = false;
  for (const event of incoming) {
    if (!event || typeof event.seq !== "number" || !Number.isInteger(event.seq)) continue;
    if (bySeq.has(event.seq)) continue;
    bySeq.set(event.seq, event);
    added = true;
  }
  if (!added) return existing;
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

function str(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function emptyDetail(stepId: string, stepType: StepType | null): StepDetail {
  return {
    stepId,
    state: "pending",
    stepType,
    started: false,
    hasOutput: false,
    error: null,
    errorCode: null,
    childRunId: null,
    agentId: null,
    toolId: null,
    prompt: null,
    deadline: null,
    timeoutHours: null,
    resumeSchema: null,
    decision: null,
    skipReason: null,
    startedAt: null,
    endedAt: null,
    unresolvedRefs: [],
  };
}

/** The status carried by a terminal event, if any. */
function terminalStatus(event: RunEvent | null): RunStatus | null {
  if (!event) return null;
  if (event.type === "run_completed") return "completed";
  const s = str(event.data?.status);
  return s === "needs_attention" ? "needs_attention" : "failed";
}

/** `outer.2/inner.0`; "" for the empty scope (D4's scope string). */
export function scopeString(scope: readonly ScopeFrame[]): string {
  return scope.map((frame) => `${frame.loop}.${frame.index}`).join("/");
}

/** The scope in event data (`[{loop, index}]`), or null when it is malformed. */
export function scopeFromData(value: unknown): ScopeFrame[] | null {
  if (!Array.isArray(value)) return null;
  const frames: ScopeFrame[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object") return null;
    const { loop, index } = item as Record<string, unknown>;
    if (typeof loop !== "string" || loop === "" || typeof index !== "number" || !Number.isInteger(index) || index < 0) return null;
    frames.push({ loop, index });
  }
  return frames;
}

function* walkSteps(steps: readonly WorkflowStep[] | undefined, depth = 0): Generator<WorkflowStep> {
  // A spec nests at most 2 loops deep; the bound only guards a malformed spec.
  if (!Array.isArray(steps) || depth > 8) return;
  for (const step of steps) {
    yield step;
    if (isLoopStep(step)) yield* walkSteps(step.body?.steps, depth + 1);
  }
}

/** The step with `id` at any depth of the spec (a loop body step too), or null. */
export function findStep(spec: WorkflowSpec | null, id: string): WorkflowStep | null {
  for (const step of walkSteps(spec?.steps)) if (step.id === id) return step;
  return null;
}

/** Every loop step (foreach and loop) of the spec at any depth, by id. */
export function loopStepsById(spec: WorkflowSpec | null): Map<string, LoopKindStep> {
  const out = new Map<string, LoopKindStep>();
  for (const step of walkSteps(spec?.steps)) if (isLoopStep(step)) out.set(step.id, step);
  return out;
}

/** Apply one step_* event to a step's detail (top-level or one body instance). */
function applyStepEvent(d: StepDetail, event: RunEvent, chosen: Map<string, string>, waitingOrder: string[] | null): void {
  const data = event.data ?? {};
  switch (event.type) {
    case "step_started": {
      d.started = true;
      if (d.state === "pending") d.state = "running";
      d.stepType = (str(data.step_type) as StepType | null) ?? d.stepType;
      if ("input" in data) d.input = data.input;
      d.childRunId = str(data.child_run_id) ?? d.childRunId;
      d.agentId = str(data.agent_id) ?? d.agentId;
      d.toolId = str(data.tool_id) ?? d.toolId;
      d.startedAt = event.ts ?? d.startedAt;
      break;
    }
    case "step_waiting": {
      d.started = true;
      if (d.state === "pending" || d.state === "running") d.state = "waiting";
      d.prompt = str(data.prompt) ?? d.prompt;
      d.deadline = str(data.deadline) ?? d.deadline;
      if (data.resume_schema && typeof data.resume_schema === "object" && !Array.isArray(data.resume_schema)) {
        d.resumeSchema = data.resume_schema as Record<string, unknown>;
      }
      d.timeoutHours = typeof data.timeout_hours === "number" ? data.timeout_hours : d.timeoutHours;
      if (data.pause === true) d.pause = true;
      if (waitingOrder && !waitingOrder.includes(d.stepId)) waitingOrder.push(d.stepId);
      break;
    }
    case "step_approved":
    case "step_rejected":
    case "step_timed_out": {
      d.decision = {
        kind: event.type === "step_approved" ? "approved" : event.type === "step_rejected" ? "rejected" : "timed_out",
        decidedBy: str(data.decided_by),
        comment: str(data.comment),
      };
      // Decided: the step is finishing (completed or failed follows).
      if (d.state === "waiting") d.state = "running";
      break;
    }
    case "step_completed": {
      d.started = true;
      d.state = "completed";
      if ("output" in data) {
        d.hasOutput = true;
        d.output = data.output;
      }
      d.endedAt = event.ts ?? d.endedAt;
      if (data.outcome === "error_kept") d.errorKept = str(data.error_code) ?? "error";
      if (Array.isArray(data.unresolved_refs)) {
        d.unresolvedRefs = data.unresolved_refs.filter((ref): ref is string => typeof ref === "string");
      }
      const out = data.output as Record<string, unknown> | null | undefined;
      if (d.stepType === "branch" && out && typeof out === "object" && typeof out.next === "string") {
        chosen.set(d.stepId, out.next);
      }
      if (d.stepType === "approval" && out && typeof out === "object" && !d.decision) {
        d.decision = {
          kind: out.approved === false ? "rejected" : "approved",
          decidedBy: str(out.decided_by),
          comment: str(out.comment),
        };
      }
      break;
    }
    case "step_failed": {
      d.started = true;
      d.state = str(data.status) === "needs_attention" ? "needs_attention" : "failed";
      d.error = str(data.error) ?? d.error;
      d.errorCode = str(data.error_code) ?? d.errorCode;
      d.endedAt = event.ts ?? d.endedAt;
      break;
    }
    default:
      break;
  }
}

/**
 * Mark the steps of one graph level (the top level, or one iteration's body) that can no longer
 * run as skipped: a fixed point over its edges (at most one pass per step). Returns the dead edges.
 */
function sweepSkipped(
  levelSteps: readonly WorkflowStep[],
  start: string,
  details: Record<string, StepDetail>,
  chosen: Map<string, string>,
): Set<string> {
  const deadEdges = new Set<string>();
  const edges = levelSteps.flatMap(stepEdges).filter((e) => details[e.target]);
  const incoming = new Map<string, typeof edges>();
  for (const e of edges) {
    if (!incoming.has(e.target)) incoming.set(e.target, []);
    incoming.get(e.target)!.push(e);
  }
  const skipped = new Set<string>();
  let changed = true;
  let guard = levelSteps.length + 1;
  while (changed && guard-- > 0) {
    changed = false;
    for (const e of edges) {
      if (deadEdges.has(e.id)) continue;
      const pick = chosen.get(e.source);
      if (skipped.has(e.source) || (pick !== undefined && e.target !== pick && (e.kind === "case" || e.kind === "default"))) {
        deadEdges.add(e.id);
        changed = true;
      }
    }
    for (const [target, ins] of incoming) {
      if (skipped.has(target) || target === start) continue;
      const d = details[target];
      if (d.started || d.state !== "pending") continue;
      if (ins.every((e) => deadEdges.has(e.id))) {
        skipped.add(target);
        d.state = "skipped";
        d.skipReason = "branch_not_taken";
        changed = true;
      }
    }
  }
  return deadEdges;
}

function untilOutcome(value: unknown): IterationDetail["until"] {
  if (value === true || value === "true") return "true";
  if (value === false || value === "false") return "false";
  return value === "unknown" ? "unknown" : null;
}

export function deriveRunView(
  spec: WorkflowSpec | null,
  events: RunEvent[],
  status: RunStatus,
): WorkflowRunView {
  const steps: Record<string, StepDetail> = {};
  for (const step of spec?.steps ?? []) steps[step.id] = emptyDetail(step.id, step.type);
  const get = (id: string): StepDetail => {
    if (!steps[id]) steps[id] = emptyDetail(id, null);
    return steps[id];
  };
  const loops = loopStepsById(spec);
  const iterations: Record<string, IterationDetail> = {};
  const iterationChosen = new Map<string, Map<string, string>>();
  /** The iteration a scope names, created with its loop's body steps (and its enclosing ones). */
  const iterationFor = (frames: readonly ScopeFrame[]): IterationDetail => {
    for (let n = 1; n < frames.length; n++) iterationFor(frames.slice(0, n));
    const key = scopeString(frames);
    let it = iterations[key];
    if (!it) {
      const last = frames[frames.length - 1];
      it = {
        loopId: last.loop,
        index: last.index,
        scope: key,
        parentScope: scopeString(frames.slice(0, -1)),
        state: "running",
        startedAt: null,
        endedAt: null,
        error: null,
        errorCode: null,
        failedStepId: null,
        until: null,
        steps: {},
        deadEdges: new Set(),
      };
      for (const body of loops.get(last.loop)?.body?.steps ?? []) it.steps[body.id] = emptyDetail(body.id, body.type);
      iterations[key] = it;
      iterationChosen.set(key, new Map());
    }
    return it;
  };
  const waitingOrder: string[] = [];
  const chosen = new Map<string, string>(); // branch id -> chosen target (top level)
  let terminal: RunEvent | null = null;
  let runError: WorkflowRunView["runError"] = null;
  let output: unknown = undefined;
  let cancelRequested = false;
  let cancelRequestedBy: string | null = null;

  const ordered = mergeEvents([], events);
  for (const event of ordered) {
    const data = event.data ?? {};
    const stepId = str(data.step_id);
    const type = event.type;
    if (type === "cancel_requested") {
      cancelRequested = true;
      cancelRequestedBy = str(data.requested_by) ?? cancelRequestedBy;
      continue;
    }
    if (type === "loop_iteration_started" || type === "loop_iteration_completed" || type === "loop_iteration_failed") {
      const outer = scopeFromData(data.scope ?? []);
      const index = data.index;
      if (!stepId || !outer || typeof index !== "number" || !Number.isInteger(index) || index < 0) continue;
      const it = iterationFor([...outer, { loop: stepId, index }]);
      if (type === "loop_iteration_started") {
        it.startedAt = event.ts ?? it.startedAt;
        if ("state" in data) it.stateIn = data.state;
      } else if (type === "loop_iteration_completed") {
        it.state = "completed";
        it.endedAt = event.ts ?? it.endedAt;
        if ("state" in data) it.stateOut = data.state;
        it.until = untilOutcome(data.until);
      } else {
        it.state = str(data.status) === "needs_attention" ? "needs_attention" : "failed";
        it.endedAt = event.ts ?? it.endedAt;
        it.error = str(data.error) ?? it.error;
        it.errorCode = str(data.error_code) ?? it.errorCode;
        it.failedStepId = str(data.failed_step_id) ?? it.failedStepId;
      }
      continue;
    }
    if (type.startsWith("step_") && stepId && "scope" in data && !(Array.isArray(data.scope) && data.scope.length === 0)) {
      // A loop body instance: its own iteration's detail, never the top-level step of that id.
      const frames = scopeFromData(data.scope);
      if (!frames || frames.length === 0) continue;
      const it = iterationFor(frames);
      if (!it.steps[stepId]) it.steps[stepId] = emptyDetail(stepId, null);
      applyStepEvent(it.steps[stepId], event, iterationChosen.get(it.scope)!, null);
      continue;
    }
    switch (event.type) {
      case "step_started":
      case "step_waiting":
      case "step_approved":
      case "step_rejected":
      case "step_timed_out":
      case "step_completed":
      case "step_failed":
        if (stepId) applyStepEvent(get(stepId), event, chosen, waitingOrder);
        break;
      case "run_completed":
        terminal = event;
        output = "result" in data ? data.result : data.output;
        break;
      case "run_failed": {
        terminal = event;
        const attention = str(data.status) === "needs_attention";
        if (stepId && steps[stepId]) {
          const d = steps[stepId];
          if (d.state !== "failed" && d.state !== "needs_attention") {
            d.state = attention ? "needs_attention" : "failed";
            d.error = d.error ?? str(data.error);
            d.errorCode = d.errorCode ?? str(data.error_code);
          }
        }
        runError = { error: str(data.error), errorCode: str(data.error_code), stepId };
        break;
      }
      default:
        break;
    }
  }

  const effective: RunStatus = terminalStatus(terminal) ?? status;
  const interrupted: NodeState = effective === "needs_attention" ? "needs_attention" : "failed";
  const endedBadly = isTerminal(effective) && effective !== "completed";

  // A run that ended interrupts whatever was still running or waiting, in every iteration too.
  if (endedBadly) {
    for (const d of Object.values(steps)) {
      if (d.state === "running" || d.state === "waiting") d.state = interrupted;
    }
  }
  for (const it of Object.values(iterations)) {
    if (endedBadly) {
      if (it.state === "running") it.state = interrupted;
      for (const d of Object.values(it.steps)) if (d.state === "running" || d.state === "waiting") d.state = interrupted;
    }
    if (!it.startedAt) {
      const starts = Object.values(it.steps).map((d) => d.startedAt).filter((t): t is string => Boolean(t)).sort();
      it.startedAt = starts[0] ?? null;
    }
    const body = loops.get(it.loopId)?.body;
    if (body && Array.isArray(body.steps)) it.deadEdges = sweepSkipped(body.steps, body.start, it.steps, iterationChosen.get(it.scope)!);
    if (it.state === "completed") {
      for (const d of Object.values(it.steps)) {
        if (d.state !== "pending") continue;
        d.state = "skipped";
        d.skipReason = "iteration_ended";
      }
    }
  }

  let deadEdges = new Set<string>();
  if (spec) {
    deadEdges = sweepSkipped(spec.steps, spec.start, steps, chosen);
    if (effective === "completed") {
      for (const d of Object.values(steps)) {
        if (d.state !== "pending") continue;
        d.state = "skipped";
        d.skipReason = "run_ended";
      }
    }
  }

  const waiting = waitingOrder.filter((id) => steps[id]?.state === "waiting");
  const cancelled = terminal?.type === "run_failed" && str(terminal.data?.error_code) === RUN_CANCELLED;
  return { steps, iterations, cancelRequested, cancelRequestedBy, cancelled, status: effective, terminal, waiting, deadEdges, runError, output };
}

// --------------------------------------------------------------------------- iterations shown

/** The iterations of loop `loopId` inside the iteration `parentScope` ("" at the top level), by index. */
export function loopIterations(view: WorkflowRunView, loopId: string, parentScope: string): IterationDetail[] {
  return Object.values(view.iterations)
    .filter((it) => it.loopId === loopId && it.parentScope === parentScope)
    .sort((a, b) => a.index - b.index);
}

/**
 * The pager's choice: iteration index per loop instance key (`loopInstanceKey`: the loop id at the
 * top level, `outer.1/inner` inside outer's iteration 2), absent = follow the latest iteration.
 * Keying by instance means a choice made for inner in one outer iteration never carries to another.
 */
export type IterationSelection = Readonly<Record<string, number>>;

/** The key of one loop instance: `loopId` at the top level, else `{parentScope}/{loopId}`. */
export function loopInstanceKey(parentScope: string, loopId: string): string {
  return parentScope ? `${parentScope}/${loopId}` : loopId;
}

/** The loop instance key of the loop an iteration scope belongs to (`outer.1/inner.2` → `outer.1/inner`). */
export function instanceKeyOfIteration(scope: string): string {
  return scope.replace(/\.\d+$/, "");
}

export interface LoopPager {
  /** The loop instance key (`loopInstanceKey`): what the selection is keyed by. */
  key: string;
  loopId: string;
  /** The enclosing iteration's scope string; "" for a top-level loop. */
  parentScope: string;
  /** 0 for a top-level loop, 1 for a loop in a loop body. */
  depth: number;
  iterations: IterationDetail[];
  /** The iteration whose states the body shows; null while none has started. */
  selected: IterationDetail | null;
  /** No explicit choice (or the chosen index does not exist here): the latest iteration is shown. */
  following: boolean;
}

export interface ResolvedRunStates {
  /** Node state per step id at every depth: a body step's state in its loop's shown iteration. */
  states: Record<string, NodeState>;
  /** Step detail per step id, from the same iterations. */
  details: Record<string, StepDetail>;
  /** The iteration scope a body step's detail belongs to (null at the top level or with none shown). */
  scopes: Record<string, string | null>;
  /** One pager per loop that is reachable through the shown iterations, outer loops first. */
  pagers: LoopPager[];
  /** "×n" iterations started, per loop that started (the graph's badge). */
  badges: Record<string, string>;
  /** The top-level dead edges plus those of every shown iteration. */
  deadEdges: Set<string>;
}

type LevelSource = { kind: "top" } | { kind: "iteration"; it: IterationDetail } | { kind: "none"; state: NodeState };

/**
 * The run view's states for one iteration choice: top-level steps as `deriveRunView` has them, and
 * each loop's body from its selected iteration (else its latest). A loop with no iteration shown
 * leaves its body pending (skipped once the loop is done or skipped).
 */
export function resolveRunStates(spec: WorkflowSpec | null, view: WorkflowRunView, selection: IterationSelection = {}): ResolvedRunStates {
  const states: Record<string, NodeState> = {};
  const details: Record<string, StepDetail> = {};
  const scopes: Record<string, string | null> = {};
  const pagers: LoopPager[] = [];
  const badges: Record<string, string> = {};
  const deadEdges = new Set(view.deadEdges);

  const walk = (levelSteps: readonly WorkflowStep[] | undefined, source: LevelSource, depth: number) => {
    if (!Array.isArray(levelSteps) || depth > 8) return;
    for (const step of levelSteps) {
      let detail: StepDetail;
      if (source.kind === "top") detail = view.steps[step.id] ?? emptyDetail(step.id, step.type);
      else if (source.kind === "iteration") detail = source.it.steps[step.id] ?? emptyDetail(step.id, step.type);
      else detail = { ...emptyDetail(step.id, step.type), state: source.state, skipReason: source.state === "skipped" ? "iteration_ended" : null };
      details[step.id] = detail;
      states[step.id] = detail.state;
      scopes[step.id] = source.kind === "iteration" ? source.it.scope : null;
      if (!isLoopStep(step)) continue;
      const parentScope = source.kind === "top" ? "" : source.kind === "iteration" ? source.it.scope : null;
      if (parentScope === null) {
        walk(step.body?.steps, source, depth + 1);
        continue;
      }
      const its = loopIterations(view, step.id, parentScope);
      const key = loopInstanceKey(parentScope, step.id);
      const chosen = selection[key];
      const picked = chosen === undefined ? undefined : its.find((it) => it.index === chosen);
      const selected = picked ?? its[its.length - 1] ?? null;
      pagers.push({ key, loopId: step.id, parentScope, depth, iterations: its, selected, following: picked === undefined });
      if (its.length > 0 || detail.started) badges[step.id] = `×${its.length}`;
      if (selected) {
        for (const id of selected.deadEdges) deadEdges.add(id);
        walk(step.body?.steps, { kind: "iteration", it: selected }, depth + 1);
      } else {
        walk(step.body?.steps, { kind: "none", state: detail.state === "skipped" || detail.state === "completed" ? "skipped" : "pending" }, depth + 1);
      }
    }
  };
  walk(spec?.steps, { kind: "top" }, 0);
  // Steps the spec does not have (events of an older or newer version) keep their top-level detail.
  for (const [id, d] of Object.entries(view.steps)) {
    if (details[id]) continue;
    details[id] = d;
    states[id] = d.state;
    scopes[id] = null;
  }
  return { states, details, scopes, pagers, badges, deadEdges };
}

/** True for an iteration that ended badly (the pager's "failed only"). */
export function iterationFailed(it: IterationDetail): boolean {
  return it.state === "failed" || it.state === "needs_attention";
}

/**
 * The index the pager moves to from `current` (-1 / +1 in index order), skipping iterations that
 * did not fail when `failedOnly`; null when there is none that way.
 */
export function adjacentIteration(iterations: readonly IterationDetail[], current: number | null, direction: -1 | 1, failedOnly = false): number | null {
  const pool = failedOnly ? iterations.filter(iterationFailed) : iterations;
  if (pool.length === 0) return null;
  if (current === null) return (direction === 1 ? pool[0] : pool[pool.length - 1]).index;
  if (direction === 1) return pool.find((it) => it.index > current)?.index ?? null;
  for (let i = pool.length - 1; i >= 0; i--) if (pool[i].index < current) return pool[i].index;
  return null;
}
