// The state of every step of a workflow run, derived from the run's durable events (pure).
//
// Events are merged by seq (duplicates dropped, any arrival order) and replayed in seq order, so a
// reconnect, a replay or an events refetch can never move a step backwards or count it twice.
// A step is `skipped` when it can no longer run: every edge into it is dead (its source was
// skipped, or its source is a branch that chose another target). A parallel's join stays alive
// through the parallel's own `join` edge. When the run completes, steps still pending are skipped.
import type { RunEvent, RunStatus } from "./runs";
import { stepEdges } from "./workflowGraph";
import type { StepType, WorkflowSpec } from "./workflows";

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
  decision: StepDecision | null;
  startedAt: string | null;
  endedAt: string | null;
}

export interface WorkflowRunView {
  steps: Record<string, StepDetail>;
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
    decision: null,
    startedAt: null,
    endedAt: null,
  };
}

/** The status carried by a terminal event, if any. */
function terminalStatus(event: RunEvent | null): RunStatus | null {
  if (!event) return null;
  if (event.type === "run_completed") return "completed";
  const s = str(event.data?.status);
  return s === "needs_attention" ? "needs_attention" : "failed";
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
  const waitingOrder: string[] = [];
  const chosen = new Map<string, string>(); // branch id -> chosen target
  let terminal: RunEvent | null = null;
  let runError: WorkflowRunView["runError"] = null;
  let output: unknown = undefined;

  const ordered = mergeEvents([], events);
  for (const event of ordered) {
    const data = event.data ?? {};
    const stepId = str(data.step_id);
    switch (event.type) {
      case "step_started": {
        if (!stepId) break;
        const d = get(stepId);
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
        if (!stepId) break;
        const d = get(stepId);
        d.started = true;
        if (d.state === "pending" || d.state === "running") d.state = "waiting";
        d.prompt = str(data.prompt) ?? d.prompt;
        d.deadline = str(data.deadline) ?? d.deadline;
        d.timeoutHours = typeof data.timeout_hours === "number" ? data.timeout_hours : d.timeoutHours;
        if (!waitingOrder.includes(stepId)) waitingOrder.push(stepId);
        break;
      }
      case "step_approved":
      case "step_rejected":
      case "step_timed_out": {
        if (!stepId) break;
        const d = get(stepId);
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
        if (!stepId) break;
        const d = get(stepId);
        d.started = true;
        d.state = "completed";
        if ("output" in data) {
          d.hasOutput = true;
          d.output = data.output;
        }
        d.endedAt = event.ts ?? d.endedAt;
        const out = data.output as Record<string, unknown> | null | undefined;
        if (d.stepType === "branch" && out && typeof out === "object" && typeof out.next === "string") {
          chosen.set(stepId, out.next);
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
        if (!stepId) break;
        const d = get(stepId);
        d.started = true;
        d.state = str(data.status) === "needs_attention" ? "needs_attention" : "failed";
        d.error = str(data.error) ?? d.error;
        d.errorCode = str(data.error_code) ?? d.errorCode;
        d.endedAt = event.ts ?? d.endedAt;
        break;
      }
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

  // A run that ended interrupts whatever was still running or waiting.
  if (isTerminal(effective) && effective !== "completed") {
    for (const d of Object.values(steps)) {
      if (d.state === "running" || d.state === "waiting") d.state = effective === "needs_attention" ? "needs_attention" : "failed";
    }
  }

  // Skipped arms: fixed point over the DAG (at most one pass per step).
  const deadEdges = new Set<string>();
  if (spec) {
    const edges = spec.steps.flatMap(stepEdges).filter((e) => steps[e.target]);
    const incoming = new Map<string, typeof edges>();
    for (const e of edges) {
      if (!incoming.has(e.target)) incoming.set(e.target, []);
      incoming.get(e.target)!.push(e);
    }
    const skipped = new Set<string>();
    let changed = true;
    let guard = spec.steps.length + 1;
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
        if (skipped.has(target) || target === spec.start) continue;
        const d = steps[target];
        if (d.started || d.state !== "pending") continue;
        if (ins.every((e) => deadEdges.has(e.id))) {
          skipped.add(target);
          d.state = "skipped";
          changed = true;
        }
      }
    }
    if (effective === "completed") {
      for (const d of Object.values(steps)) if (d.state === "pending") d.state = "skipped";
    }
  }

  const waiting = waitingOrder.filter((id) => steps[id]?.state === "waiting");
  return { steps, status: effective, terminal, waiting, deadEdges, runError, output };
}
