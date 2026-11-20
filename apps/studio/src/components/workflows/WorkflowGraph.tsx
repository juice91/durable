"use client";

import React, { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  Background,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
// The chart tokens (`--viz-*`): the canvas's grid, edges and label backgrounds use them.
import "@/components/metrics/chartTheme.css";
import { ChevronDown, ChevronRight, ChevronsDownUp, ChevronsUpDown, Maximize, ZoomIn, ZoomOut } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { matchesNow } from "@/components/frame/useMediaQuery";
import { buildWorkflowGraph, loopBadge, loopStepIds, loopSummary, type GraphEdge, type GraphNode } from "@/lib/workflowGraph";
import { NODE_STATES, type NodeState, type StepDetail } from "@/lib/workflowRunState";
import { readStorage, writeStorage } from "@/lib/safeStorage";
import { NOT_REACHED_OPEN_KEY } from "@/lib/notReached";
import { BranchCases, branchCasesText, branchTaken } from "./BranchCases";
import { CopyButton } from "@/components/ui/CopyButton";
import Link from "next/link";
import { routes } from "@/lib/routes";
import { stepSummary, type StepSummary } from "@/lib/workflowStepSummary";
import { isLoopStep, type LoopKindStep, type WorkflowSpec, type WorkflowStep } from "@/lib/workflows";

export const NODE_STATE_LABELS: Record<NodeState, string> = {
  pending: "Pending",
  running: "Running",
  waiting: "Waiting",
  completed: "Completed",
  failed: "Failed",
  // A step the run never executed. "Skipped" is the run status of a "nothing to do" exit, a different thing.
  skipped: "Not reached",
  needs_attention: "Needs attention",
};

export const NODE_STATE_CLASSES: Record<NodeState, string> = {
  pending: "border-border bg-card text-card-foreground",
  running: "border-primary bg-accent text-accent-foreground ring-2 ring-primary/40 animate-pulse motion-reduce:animate-none",
  waiting: "border-warning bg-warning/10 text-warning-text",
  completed: "border-success bg-success/10 text-success-text",
  failed: "border-destructive bg-destructive/10 text-destructive-text",
  skipped: "border-dashed border-border bg-muted text-muted-foreground opacity-40",
  needs_attention: "border-2 border-warning bg-warning/15 text-warning-text",
};

export const STEP_TYPE_LABELS: Record<WorkflowStep["type"], string> = {
  agent: "Agent",
  tool: "Tool",
  branch: "Branch",
  parallel: "Parallel",
  approval: "Approval",
  input: "Input",
  foreach: "Foreach",
  loop: "Loop",
  evaluate: "Evaluate",
};

/** A step kind's label; an unknown kind (a newer spec) shows its raw type. */
export function stepTypeLabel(type: string): string {
  return (STEP_TYPE_LABELS as Record<string, string | undefined>)[type] ?? type;
}

/** Why a step was not reached, as a tooltip. */
export function notReachedHint(reason: StepDetail["skipReason"] | undefined): string {
  if (reason === "run_ended") return "The run ended before this step.";
  if (reason === "iteration_ended") return "This step did not run in this iteration.";
  return "This branch arm was not taken.";
}

/** The canvas node's one-line subtitle: the step's "does" summary for a tool or agent. */
export function stepSubtitle(step: WorkflowStep, agentNames?: Readonly<Record<string, string>>): string {
  const summary = stepSummary(step, agentNames);
  if (summary) return summary.text;
  switch (step.type) {
    case "approval":
    case "input":
      return `timeout ${step.timeout_hours}h`;
    case "branch":
      return `${step.cases.length} case${step.cases.length === 1 ? "" : "s"} + default`;
    case "parallel":
      return `${step.branches.length} branches, join ${step.join}`;
    case "foreach":
    case "loop":
      // The bound (or the run view's count) is the node's badge; the subtitle says what it repeats.
      return loopSummary(step)[0];
    case "evaluate": {
      const count = Array.isArray(step.evaluators) ? step.evaluators.length : 0;
      return `${count} evaluator${count === 1 ? "" : "s"}, pass ${step.pass ?? "all"}`;
    }
    default:
      return "";
  }
}

type StepNodeData = {
  step: WorkflowStep;
  state: NodeState | null;
  selected: boolean;
  width: number;
  height: number;
  /** Loop steps only: whether the body is hidden, and the badge ("×50", or the run view's count). */
  collapsed: boolean;
  badge: string | null;
  skipReason?: StepDetail["skipReason"];
  agentNames?: Readonly<Record<string, string>>;
};

/** Marks the loop toggle inside a node: a click on it collapses or expands instead of selecting. */
const LOOP_TOGGLE_ATTR = "data-loop-toggle";

function TypeBadge({ type }: { type: string }) {
  return <span className="shrink-0 rounded-sm bg-background/70 px-1.5 py-0.5 text-badge font-medium uppercase tracking-wide text-foreground">{stepTypeLabel(type)}</span>;
}

function CountBadge({ step, badge }: { step: LoopKindStep; badge: string }) {
  return <span data-testid={`workflow-loop-badge-${step.id}`} className="shrink-0 rounded-sm border border-border bg-background/70 px-1 font-mono text-badge font-medium tabular-nums text-foreground">{badge}</span>;
}

/**
 * The collapse chevron of a loop node. The graph is an image to assistive tech (its children are
 * presentational), so this is a pointer convenience, not a control: keyboard users collapse one
 * loop from its "Steps of <id>" button in the step list (which shares the collapsed set), or every
 * loop with the canvas's "Collapse all loops" toggle.
 */
function LoopToggle({ collapsed }: { collapsed: boolean }) {
  const Icon = collapsed ? ChevronRight : ChevronDown;
  return (
    <span {...{ [LOOP_TOGGLE_ATTR]: "" }} aria-hidden="true" className="-ml-1 inline-flex size-4 shrink-0 cursor-pointer items-center justify-center rounded-sm hover:bg-fill-hover">
      <Icon className="size-3.5" strokeWidth={ICON_STROKE_WIDTH} />
    </span>
  );
}

function StepNode({ data }: NodeProps<Node<StepNodeData>>) {
  const { step, state, selected, width, height, collapsed, badge, skipReason, agentNames } = data;
  const subtitle = stepSubtitle(step, agentNames);
  const tone = NODE_STATE_CLASSES[state ?? "pending"];
  const loop = isLoopStep(step) ? step : null;
  return (
    <div
      data-testid={`workflow-node-${step.id}`}
      data-state={state ?? "definition"}
      data-step-type={step.type}
      {...(loop ? { "data-collapsed": String(collapsed) } : {})}
      style={{ width, height }}
      title={state === "skipped" ? notReachedHint(skipReason) : undefined}
      className={cn("flex flex-col justify-center rounded-md border-2 px-3 py-1 text-left shadow-raised", tone, selected && "outline outline-2 outline-offset-2 outline-ring")}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1">
          {loop ? <LoopToggle collapsed={collapsed} /> : null}
          <span className="truncate font-mono text-label font-semibold">{step.id}</span>
        </span>
        <span className="flex shrink-0 items-center gap-1">
          {loop && badge ? <CountBadge step={loop} badge={badge} /> : null}
          <TypeBadge type={step.type} />
        </span>
      </div>
      <div className="flex items-center justify-between gap-2 text-caption">
        <span data-testid={`workflow-node-subtitle-${step.id}`} title={subtitle} className="truncate opacity-80">{subtitle}</span>
        {state ? <span className="shrink-0 font-medium">{NODE_STATE_LABELS[state]}</span> : null}
      </div>
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}

/**
 * An expanded loop (foreach or loop): a frame round its body with a header naming the loop, its
 * kind, its `×n` badge and what it repeats over or until. The body's nodes are React Flow children
 * of this node (`parentId`), drawn on top of it.
 */
function LoopGroupNode({ data }: NodeProps<Node<StepNodeData>>) {
  const { step, state, selected, width, height, badge } = data;
  const loop = step as LoopKindStep;
  const tone = state ? NODE_STATE_CLASSES[state] : "border-border bg-muted/40 text-card-foreground";
  const [first, second] = loopSummary(loop);
  return (
    <div
      data-testid={`workflow-node-${step.id}`}
      data-state={state ?? "definition"}
      data-step-type={step.type}
      data-collapsed="false"
      data-group="true"
      style={{ width, height }}
      // A running state's pulse would animate the whole body: the group keeps a still frame.
      className={cn("rounded-lg border-2 border-dashed text-left", tone, "animate-none", selected && "outline outline-2 outline-offset-2 outline-ring")}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div className="flex flex-col gap-0.5 px-3 pt-2">
        <div className="flex items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-1">
            <LoopToggle collapsed={false} />
            <span className="truncate font-mono text-label font-semibold">{step.id}</span>
          </span>
          <span className="flex shrink-0 items-center gap-1">
            {state ? <span className="text-caption font-medium">{NODE_STATE_LABELS[state]}</span> : null}
            {badge ? <CountBadge step={loop} badge={badge} /> : null}
            <TypeBadge type={step.type} />
          </span>
        </div>
        <span data-testid={`workflow-loop-summary-${step.id}`} className="truncate font-mono text-caption opacity-80">{first}</span>
        <span className="truncate text-caption opacity-80">{second}</span>
      </div>
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}

const nodeTypes = { step: StepNode, loopGroup: LoopGroupNode };

function edgeStyle(edge: GraphEdge, dead: boolean): Partial<Edge> {
  const dashed = edge.kind === "join" || edge.kind === "default";
  return {
    type: "smoothstep",
    label: edge.label,
    labelBgPadding: [4, 2],
    labelBgBorderRadius: 4,
    labelStyle: { fontSize: 11, fill: "hsl(var(--foreground))", opacity: dead ? 0.4 : 1 },
    labelBgStyle: { fill: "var(--viz-surface-color)" },
    markerEnd: { type: MarkerType.ArrowClosed, color: "var(--viz-axis-color)" },
    animated: false,
    style: {
      stroke: "var(--viz-axis-color)",
      strokeDasharray: dashed ? "6 4" : undefined,
      opacity: dead ? 0.2 : 1,
    },
  };
}

/** React Flow's own colour variables, pointed at our tokens (its defaults are fixed greys). */
const FLOW_STYLE = {
  "--xy-background-color": "transparent",
  // The credit's own look is in globals.css (`.react-flow__attribution`), on our tokens.
  "--xy-attribution-background-color": "transparent",
} as React.CSSProperties;

const FIT_PADDING = 0.2;

/**
 * The graph drawing (React Flow's renderer) is the role="img", labelled; the wrapper is a group.
 * React Flow's attribution panel is the renderer's sibling, so its credit link stays a real link
 * outside the image (a link inside an image is a nested interactive control). React Flow renders
 * the renderer itself, so the attributes are set on it after each render that may add it.
 */
function useGraphImage(graphRef: React.RefObject<HTMLDivElement | null>, label: string) {
  useEffect(() => {
    const root = graphRef.current;
    if (!root) return;
    const mark = () => {
      const renderer = root.querySelector<HTMLElement>(".react-flow__renderer");
      if (!renderer) return;
      if (renderer.getAttribute("role") !== "img") renderer.setAttribute("role", "img");
      if (renderer.getAttribute("aria-label") !== label) renderer.setAttribute("aria-label", label);
    };
    mark();
    if (typeof MutationObserver === "undefined") return;
    const observer = new MutationObserver(mark);
    observer.observe(root, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [graphRef, label]);
}

function zoomDuration(): number {
  return matchesNow("(prefers-reduced-motion: reduce)") ? 0 : 150;
}

/**
 * Zoom in, zoom out, 100% and fit, each with an accessible name. Rendered beside the graph (not
 * inside its `role="img"` surface, whose children are presentational).
 */
interface LoopsToggle {
  /** Every loop is collapsed. */
  allCollapsed: boolean;
  onToggle: () => void;
}

function ZoomControls({ className, loops }: { className?: string; loops?: LoopsToggle | null }) {
  const flow = useReactFlow();
  const icon = (Icon: typeof ZoomIn) => <Icon strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />;
  return (
    <div
      role="group"
      aria-label="Zoom"
      data-testid="workflow-zoom-controls"
      className={cn("pointer-events-auto flex items-center gap-0.5 rounded-md border border-border bg-surface-raised p-0.5 shadow-raised", className)}
    >
      <IconButton label="Zoom out" title="Zoom out" size="sm" tooltip={false} icon={icon(ZoomOut)} data-testid="workflow-zoom-out" onClick={() => void flow.zoomOut({ duration: zoomDuration() })} />
      <Button
        variant="ghost"
        size="sm"
        aria-label="Zoom to 100%"
        title="Zoom to 100%"
        data-testid="workflow-zoom-reset"
        className="px-1.5 font-mono tabular-nums"
        onClick={() => void flow.zoomTo(1, { duration: zoomDuration() })}
      >
        100%
      </Button>
      <IconButton label="Zoom in" title="Zoom in" size="sm" tooltip={false} icon={icon(ZoomIn)} data-testid="workflow-zoom-in" onClick={() => void flow.zoomIn({ duration: zoomDuration() })} />
      <IconButton
        label="Fit graph to view"
        title="Fit graph to view"
        size="sm"
        tooltip={false}
        icon={icon(Maximize)}
        data-testid="workflow-zoom-fit"
        onClick={() => void flow.fitView({ padding: FIT_PADDING, duration: zoomDuration() })}
      />
      {loops ? (
        <IconButton
          // One fixed name; aria-pressed carries the state (pressed = every loop collapsed).
          label="Collapse all loops"
          title="Collapse all loops"
          size="sm"
          tooltip={false}
          aria-pressed={loops.allCollapsed}
          icon={icon(loops.allCollapsed ? ChevronsUpDown : ChevronsDownUp)}
          data-testid="workflow-loops-toggle"
          onClick={loops.onToggle}
        />
      ) : null}
    </div>
  );
}

export interface WorkflowCanvasProps {
  spec: WorkflowSpec;
  /** Live states per step id; omitted for a plain definition view. */
  states?: Record<string, NodeState>;
  deadEdges?: Set<string>;
  /** Step details (the run view): the node tooltip says why a step was not reached. */
  details?: Record<string, StepDetail>;
  selectedStepId?: string | null;
  onSelectStep?: (stepId: string) => void;
  className?: string;
  /** Extra space under the zoom controls (the run view's dock bar sits below the canvas, not over it). */
  controlsClassName?: string;
  /** Floating panels drawn over the canvas (the run panel, approvals, the step detail). */
  children?: React.ReactNode;
  /**
   * Loops drawn collapsed (controlled: pages pass `useCollapsedLoops()`, the `?collapsed=` URL
   * state, and the same set to the step list); omitted, the canvas keeps its own set, every loop
   * expanded at first.
   */
  collapsedLoops?: ReadonlySet<string>;
  onCollapsedLoopsChange?: (next: Set<string>) => void;
  /** A loop's badge text by step id (the run view's "×7"); a loop without one shows its bound ("×50"). */
  loopBadges?: Record<string, string>;
  /** Agent names by agent id, for a step's subtitle; an agent not listed shows its id. */
  agentNames?: Readonly<Record<string, string>>;
}

/**
 * A read-only execution graph of a workflow version (a visualizer, not an editor), filling its
 * container: nodes cannot be dragged or connected. Zoom controls sit bottom right; `children` float
 * over the canvas. The same steps as an accessible list of buttons are `WorkflowStepList`.
 */
export function WorkflowCanvas(props: WorkflowCanvasProps) {
  return (
    <ReactFlowProvider>
      <CanvasInner {...props} />
    </ReactFlowProvider>
  );
}

/** `collapsed` with `id` added or removed. */
export function toggledLoops(collapsed: ReadonlySet<string>, id: string): Set<string> {
  const next = new Set(collapsed);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

/**
 * The collapsed set, controlled by `value`/`onChange` when `value` is given, else kept here (every
 * loop expanded at first). `onChange` is still called in the uncontrolled case.
 */
function useCollapsedSet(value: ReadonlySet<string> | undefined, onChange: ((next: Set<string>) => void) | undefined) {
  const [own, setOwn] = useState<ReadonlySet<string>>(() => new Set());
  const collapsed = value ?? own;
  const setCollapsed = useCallback(
    (next: Set<string>) => {
      onChange?.(next);
      if (value === undefined) setOwn(next);
    },
    [value, onChange],
  );
  return [collapsed, setCollapsed] as const;
}

/**
 * Re-fit the view when the collapsed set changes (not on the first render: React Flow's `fitView`
 * prop fits that one), once React Flow has the new nodes.
 */
function useRefitOnCollapse(collapsed: ReadonlySet<string>) {
  const flow = useReactFlow();
  const flowRef = useRef(flow);
  flowRef.current = flow;
  const key = [...collapsed].sort().join(",");
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    const fit = () => void flowRef.current.fitView({ padding: FIT_PADDING, duration: zoomDuration() });
    if (typeof requestAnimationFrame !== "function") {
      fit();
      return;
    }
    const frame = requestAnimationFrame(fit);
    return () => cancelAnimationFrame(frame);
  }, [key]);
}

function badgeFor(n: GraphNode, loopBadges: Record<string, string> | undefined): string | null {
  if (!isLoopStep(n.step)) return null;
  return loopBadges?.[n.id] ?? loopBadge(n.step);
}

function CanvasInner({
  spec,
  states,
  deadEdges,
  details,
  selectedStepId = null,
  onSelectStep,
  className,
  controlsClassName,
  children,
  collapsedLoops,
  onCollapsedLoopsChange,
  loopBadges,
  agentNames,
}: WorkflowCanvasProps) {
  const [collapsed, setCollapsed] = useCollapsedSet(collapsedLoops, onCollapsedLoopsChange);
  useRefitOnCollapse(collapsed);
  const loopIds = useMemo(() => loopStepIds(spec), [spec]);
  const graph = useMemo(() => buildWorkflowGraph(spec, { collapsed }), [spec, collapsed]);
  const fullCount = useMemo(() => (loopIds.length > 0 ? buildWorkflowGraph(spec).nodes.length : graph.nodes.length), [spec, loopIds, graph]);
  const graphRef = useRef<HTMLDivElement | null>(null);
  const loopCount = loopIds.length;
  const graphLabel = `Execution graph of ${spec.name} with ${fullCount} steps${loopCount > 0 ? `, ${loopCount} loop${loopCount === 1 ? "" : "s"}` : ""}`;
  useGraphImage(graphRef, graphLabel);

  const allCollapsed = loopCount > 0 && loopIds.every((id) => collapsed.has(id));
  const loopsToggle = loopCount > 0
    ? { allCollapsed, onToggle: () => setCollapsed(allCollapsed ? new Set() : new Set(loopIds)) }
    : null;
  const toggleLoop = (id: string) => setCollapsed(toggledLoops(collapsed, id));

  const nodes: Node<StepNodeData>[] = useMemo(
    () => graph.nodes.map((n) => ({
      id: n.id,
      type: n.kind === "group" ? "loopGroup" : "step",
      position: { x: n.x, y: n.y },
      ...(n.parentId ? { parentId: n.parentId, extent: "parent" as const } : {}),
      data: {
        step: n.step,
        state: states ? states[n.id] ?? "pending" : null,
        selected: n.id === selectedStepId,
        width: n.width,
        height: n.height,
        collapsed: n.collapsed,
        badge: badgeFor(n, loopBadges),
        skipReason: details?.[n.id]?.skipReason,
        agentNames,
      },
      draggable: false,
      connectable: false,
      width: n.width,
      height: n.height,
    })),
    [graph, states, details, selectedStepId, loopBadges, agentNames],
  );
  const edges: Edge[] = useMemo(
    () => graph.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      data: { kind: e.kind },
      // An untaken arm, or a hand-off to a step the run never reached, is dimmed; the taken path keeps full contrast.
      ...edgeStyle(e, Boolean(deadEdges?.has(e.id)) || states?.[e.target] === "skipped"),
    })),
    [graph, deadEdges, states],
  );

  return (
    <div data-testid="workflow-canvas" className={cn("eap-viz relative h-full min-h-0 w-full min-w-0 overflow-hidden bg-background", className)}>
      <div
        ref={graphRef}
        data-testid="workflow-graph"
        role="group"
        aria-label="Workflow graph"
        className="absolute inset-0"
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          style={FLOW_STYLE}
          fitView
          fitViewOptions={{ padding: FIT_PADDING }}
          nodesDraggable={false}
          nodesConnectable={false}
          edgesFocusable={false}
          // Keyboard users select steps from the step list: the graph is an image to them.
          nodesFocusable={false}
          elementsSelectable={Boolean(onSelectStep)}
          onNodeClick={(event, node) => {
            const target = event.target as Element | null;
            if (target?.closest?.(`[${LOOP_TOGGLE_ATTR}]`)) toggleLoop(node.id);
            else onSelectStep?.(node.id);
          }}
          minZoom={0.2}
          // React Flow's own credit, visible (hiding it needs a Pro licence, and warns). It is a
          // sibling of the drawing, which alone is the role="img" (useGraphImage).
          attributionPosition="top-right"
        >
          <Background gap={16} color="var(--viz-grid-color)" />
        </ReactFlow>
      </div>
      {children}
      <ZoomControls className={cn("absolute bottom-3 right-3 z-10", controlsClassName)} loops={loopsToggle} />
    </div>
  );
}

/** Whether the run view's "steps not reached" group is open, remembered in this browser (read after mount). */
export function useNotReachedOpen(): [boolean, (open: boolean) => void] {
  const [open, setOpen] = useState(false);
  useEffect(() => setOpen(readStorage(NOT_REACHED_OPEN_KEY) === "true"), []);
  const update = useCallback((next: boolean) => {
    setOpen(next);
    writeStorage(NOT_REACHED_OPEN_KEY, String(next));
  }, []);
  return [open, update];
}

/** One line saying what a tool or agent step does: the tool id (monospace) or the agent's name (a link), then its input summary. */
export function StepSummaryLine({ step, agentNames }: { step: WorkflowStep; agentNames?: Readonly<Record<string, string>> }) {
  const summary: StepSummary | null = stepSummary(step, agentNames);
  const onError = step.type === "tool" && step.on_error === "continue";
  if (!summary && !onError) return null;
  return (
    <span data-testid={`step-summary-${step.id}`} className="mt-0.5 flex min-w-0 flex-wrap items-baseline gap-x-1.5 text-caption text-foreground">
      {summary ? (
        <>
          {summary.kind === "agent" && summary.agentId ? (
            <Link href={routes.agentIndex(summary.agentId)} data-testid={`step-agent-link-${step.id}`} className="rounded-sm font-medium text-info-text underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring" onClick={(e) => e.stopPropagation()}>{summary.lead}</Link>
          ) : <span className="font-mono">{summary.lead}</span>}
          {summary.detail ? <span className="min-w-0 break-all text-muted-foreground">{summary.detail}</span> : null}
        </>
      ) : null}
      {onError ? <span data-testid={`step-on-error-${step.id}`} className="shrink-0 rounded-sm border border-border bg-muted px-1 text-badge text-muted-foreground">on_error: continue</span> : null}
    </span>
  );
}

export interface WorkflowStepListProps {
  spec: WorkflowSpec;
  states?: Record<string, NodeState>;
  selectedStepId?: string | null;
  onSelectStep?: (stepId: string) => void;
  /** Show the node state legend (the run view). */
  legend?: boolean;
  /** The collapsed loops, shared with the canvas (pass the same set and setter to both). */
  collapsedLoops?: ReadonlySet<string>;
  onCollapsedLoopsChange?: (next: Set<string>) => void;
  /** AI notes by step id (the cached explanation): shown under the step, labelled 「AI 註解」. */
  notes?: ReadonlyMap<string, string>;
  /** Step details (the run view): a branch's taken case and warnings, and why a step was not reached. */
  details?: Record<string, StepDetail>;
  /**
   * The run view: top-level steps the run never reached fold into one "N steps not reached" row,
   * closed at first (remembered in this browser). A selected step in the group opens it.
   */
  foldNotReached?: boolean;
  /** Agent names by agent id, for an agent step's summary. */
  agentNames?: Readonly<Record<string, string>>;
}

/**
 * The graph's steps as a list (buttons when steps can be selected), with each step's targets. A
 * loop's body steps are a nested, indented list inside the loop's item, in the body's layout order.
 * Each loop has a "Steps of <id>" disclosure button (aria-expanded) that hides its body here and
 * collapses it in the graph: the keyboard path to per-loop collapse.
 */
export function WorkflowStepList({
  spec,
  states,
  selectedStepId = null,
  onSelectStep,
  legend = false,
  collapsedLoops,
  onCollapsedLoopsChange,
  notes,
  details,
  foldNotReached = false,
  agentNames,
}: WorkflowStepListProps) {
  const [collapsed, setCollapsed] = useCollapsedSet(collapsedLoops, onCollapsedLoopsChange);
  const [notReachedOpen, setNotReachedOpen] = useNotReachedOpen();
  const baseId = useId();
  const graph = useMemo(() => buildWorkflowGraph(spec), [spec]);
  const outgoing = (stepId: string) => graph.edges.filter((e) => e.source === stepId);
  const children = useMemo(() => {
    const map = new Map<string | null, GraphNode[]>();
    for (const n of graph.nodes) {
      if (!map.has(n.parentId)) map.set(n.parentId, []);
      map.get(n.parentId)!.push(n);
    }
    return map;
  }, [graph]);

  const renderItem = (n: GraphNode): React.ReactNode => {
    const state = states ? states[n.id] ?? "pending" : null;
    const targets = outgoing(n.id);
    const loop = isLoopStep(n.step) ? n.step : null;
    const label = stepTypeLabel(n.step.type);
    const branch = n.step.type === "branch" ? n.step : null;
    const desc = (
      <>
        {loop ? <span className="mt-0.5 block break-words font-mono text-caption text-muted-foreground">{loopSummary(loop).join(" · ")}</span> : null}
        <StepSummaryLine step={n.step} agentNames={agentNames} />
        {branch ? (
          <BranchCases step={branch} taken={branchTaken(details?.[n.id]?.output)} warnings={details?.[n.id]?.warnings} />
        ) : targets.length > 0 ? (
          <span className="mt-0.5 block break-words text-caption text-muted-foreground">
            {targets.map((e) => `→ ${e.target}${e.label ? ` (${e.label})` : ""}`).join(", ")}
          </span>
        ) : <span className="mt-0.5 block text-caption text-muted-foreground">{n.parentId ? "ends the iteration" : "ends"}</span>}
      </>
    );
    const cls = cn(
      "group/row relative block w-full min-w-0 rounded-md border px-2 py-1.5 text-left",
      n.id === selectedStepId ? "border-foreground bg-fill-selected" : "border-border bg-surface",
      onSelectStep ? "cursor-pointer hover:bg-fill-hover" : null,
    );
    const inner = children.get(n.id) ?? [];
    const shut = Boolean(loop) && collapsed.has(n.id);
    const bodyId = `${baseId}-body-${n.id}`;
    const head = (
      <span className="flex min-w-0 items-center justify-between gap-2">
        <span className="truncate font-mono text-label font-medium text-foreground">{n.id}</span>
        <span className="shrink-0 text-caption text-muted-foreground">
          {loop ? `${label} ${loopBadge(loop)}` : label}{state ? " · " : ""}
          {state ? <span title={state === "skipped" ? notReachedHint(details?.[n.id]?.skipReason) : undefined}>{NODE_STATE_LABELS[state]}</span> : null}
        </span>
      </span>
    );
    const copyBody = [
      `${n.id} (${label})`,
      stepSummary(n.step, agentNames)?.text ?? "",
      branch ? branchCasesText(branch) : loop ? loopSummary(loop).join(" · ") : targets.map((e) => `→ ${e.target}${e.label ? ` (${e.label})` : ""}`).join(", "),
    ].filter(Boolean).join("\n");
    // The row is a div, not one big button: the description is selectable text with a copy button, and a
    // button cannot hold either. The head is the button (name, state, keyboard); a click anywhere else
    // on the row selects too, except on the description and a drag that ended in a selection.
    const select = (
      <div
        className={cls}
        data-copy-text={copyBody}
        onClick={onSelectStep ? () => { if (!window.getSelection()?.toString()) onSelectStep(n.id); } : undefined}
      >
        {onSelectStep ? (
          <button
            type="button"
            aria-pressed={n.id === selectedStepId}
            aria-label={`Step ${n.id}, ${label}${loop ? `, up to ${loopBadge(loop).slice(1)} times` : ""}${state ? `, ${NODE_STATE_LABELS[state]}` : ""}`}
            className="block w-full min-w-0 rounded-sm text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {head}
          </button>
        ) : head}
        <span
          data-testid={`step-row-text-${n.id}`}
          className="block min-w-0 cursor-text select-text pr-6"
          onMouseDown={(e) => e.stopPropagation()}
          onClick={(e) => e.stopPropagation()}
        >
          {desc}
        </span>
        <span className="absolute bottom-1 right-1 opacity-0 focus-within:opacity-100 group-hover/row:opacity-100 group-focus-within/row:opacity-100">
          <CopyButton text={copyBody} label={`step ${n.id}`} />
        </span>
      </div>
    );
    const note = notes?.get(n.id);
    const aiNote = note ? (
      <p data-testid={`workflow-step-ai-note-${n.id}`} className="mt-1 min-w-0 select-text break-words rounded-sm bg-muted/40 px-2 py-1 text-caption text-foreground">
        <span className="mr-1 font-semibold text-muted-foreground">AI 註解</span>
        {note}
      </p>
    ) : null;
    return (
      <li key={n.id} data-testid={`workflow-step-${n.id}`} data-state={state ?? "definition"} data-step-type={n.step.type} data-depth={n.depth} {...(loop ? { "data-collapsed": String(shut) } : {})}>
        {loop && inner.length > 0 ? (
          <div className="flex min-w-0 items-start gap-1">
            <div className="min-w-0 flex-1">{select}</div>
            <IconButton
              label={`Steps of ${n.id}`}
              title={`Steps of ${n.id}`}
              size="sm"
              tooltip={false}
              aria-expanded={!shut}
              aria-controls={bodyId}
              data-testid={`workflow-loop-toggle-${n.id}`}
              icon={shut ? <ChevronRight strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" /> : <ChevronDown strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
              onClick={() => setCollapsed(toggledLoops(collapsed, n.id))}
            />
          </div>
        ) : select}
        {aiNote}
        {inner.length > 0 ? (
          <ol
            id={bodyId}
            hidden={shut}
            aria-label={`Body of ${n.id}`}
            data-testid={`workflow-step-body-${n.id}`}
            className="mt-1.5 ml-3 space-y-1.5 border-l-2 border-dashed border-border pl-2"
          >
            {inner.map(renderItem)}
          </ol>
        ) : null}
      </li>
    );
  };

  const all = children.get(null) ?? [];
  const isUnreached = (n: GraphNode) => foldNotReached && states?.[n.id] === "skipped";
  const top = all.filter((n) => !isUnreached(n));
  const unreached = all.filter(isUnreached);
  const groupOpen = notReachedOpen || (selectedStepId !== null && unreached.some((n) => n.id === selectedStepId));

  return (
    <div className="min-w-0 space-y-3">
      {legend && states ? (
        <ul aria-label="Node state legend" className="flex flex-wrap gap-1.5 text-caption">
          {NODE_STATES.map((s) => (
            <li key={s} className={cn("rounded-sm border px-1.5 py-0.5", NODE_STATE_CLASSES[s])}>{NODE_STATE_LABELS[s]}</li>
          ))}
        </ul>
      ) : null}
      <ol aria-label="Workflow steps" data-testid="workflow-step-list" className="space-y-1.5">
        {top.map(renderItem)}
        {unreached.length > 0 ? (
          <li data-testid="workflow-step-not-reached-group" data-open={String(groupOpen)}>
            <button
              type="button"
              aria-expanded={groupOpen}
              aria-controls={`${baseId}-not-reached`}
              data-testid="workflow-step-not-reached-toggle"
              onClick={() => setNotReachedOpen(!notReachedOpen)}
              className="flex w-full items-center gap-1 rounded-md border border-dashed border-border px-2 py-1.5 text-left text-caption text-muted-foreground outline-none hover:bg-fill-hover focus-visible:ring-2 focus-visible:ring-ring"
            >
              {groupOpen ? <ChevronDown className="size-3.5" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" /> : <ChevronRight className="size-3.5" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
              {unreached.length} step{unreached.length === 1 ? "" : "s"} not reached
            </button>
            <ol id={`${baseId}-not-reached`} hidden={!groupOpen} aria-label="Steps not reached" className="mt-1.5 space-y-1.5">
              {unreached.map(renderItem)}
            </ol>
          </li>
        ) : null}
      </ol>
    </div>
  );
}
