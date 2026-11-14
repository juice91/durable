"use client";

import React, { useEffect, useMemo, useRef } from "react";
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
import { Maximize, ZoomIn, ZoomOut } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { matchesNow } from "@/components/frame/useMediaQuery";
import { buildWorkflowGraph, NODE_HEIGHT, NODE_WIDTH, type GraphEdge } from "@/lib/workflowGraph";
import { NODE_STATES, type NodeState } from "@/lib/workflowRunState";
import type { WorkflowSpec, WorkflowStep } from "@/lib/workflows";

export const NODE_STATE_LABELS: Record<NodeState, string> = {
  pending: "Pending",
  running: "Running",
  waiting: "Waiting",
  completed: "Completed",
  failed: "Failed",
  skipped: "Skipped",
  needs_attention: "Needs attention",
};

export const NODE_STATE_CLASSES: Record<NodeState, string> = {
  pending: "border-border bg-card text-card-foreground",
  running: "border-primary bg-accent text-accent-foreground ring-2 ring-primary/40 animate-pulse motion-reduce:animate-none",
  waiting: "border-warning bg-warning/10 text-warning-text",
  completed: "border-success bg-success/10 text-success-text",
  failed: "border-destructive bg-destructive/10 text-destructive-text",
  skipped: "border-dashed border-border bg-muted text-muted-foreground opacity-60",
  needs_attention: "border-2 border-warning bg-warning/15 text-warning-text",
};

export const STEP_TYPE_LABELS: Record<WorkflowStep["type"], string> = {
  agent: "Agent",
  tool: "Tool",
  branch: "Branch",
  parallel: "Parallel",
  approval: "Approval",
};

function stepSubtitle(step: WorkflowStep): string {
  switch (step.type) {
    case "agent":
      return `agent ${step.agent_id.slice(0, 8)}${step.version ? ` v${step.version}` : ""}`;
    case "tool":
      return step.tool_id;
    case "approval":
      return `timeout ${step.timeout_hours}h`;
    case "branch":
      return `${step.cases.length} case${step.cases.length === 1 ? "" : "s"} + default`;
    case "parallel":
      return `${step.branches.length} branches, join ${step.join}`;
  }
}

type StepNodeData = {
  step: WorkflowStep;
  state: NodeState | null;
  selected: boolean;
};

function StepNode({ data }: NodeProps<Node<StepNodeData>>) {
  const { step, state, selected } = data;
  const tone = NODE_STATE_CLASSES[state ?? "pending"];
  return (
    <div
      data-testid={`workflow-node-${step.id}`}
      data-state={state ?? "definition"}
      data-step-type={step.type}
      style={{ width: NODE_WIDTH, height: NODE_HEIGHT }}
      className={cn("flex flex-col justify-center rounded-md border-2 px-3 py-1 text-left shadow-raised", tone, selected && "outline outline-2 outline-offset-2 outline-ring")}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-label font-semibold">{step.id}</span>
        <span className="shrink-0 rounded-sm bg-background/70 px-1.5 py-0.5 text-badge font-medium uppercase tracking-wide text-foreground">{STEP_TYPE_LABELS[step.type]}</span>
      </div>
      <div className="flex items-center justify-between gap-2 text-caption">
        <span className="truncate opacity-80">{stepSubtitle(step)}</span>
        {state ? <span className="shrink-0 font-medium">{NODE_STATE_LABELS[state]}</span> : null}
      </div>
      <Handle type="source" position={Position.Bottom} isConnectable={false} />
    </div>
  );
}

const nodeTypes = { step: StepNode };

function edgeStyle(edge: GraphEdge, dead: boolean): Partial<Edge> {
  const dashed = edge.kind === "join" || edge.kind === "default";
  return {
    type: "smoothstep",
    label: edge.label,
    labelBgPadding: [4, 2],
    labelBgBorderRadius: 4,
    labelStyle: { fontSize: 11, fill: "hsl(var(--foreground))" },
    labelBgStyle: { fill: "var(--viz-surface-color)" },
    markerEnd: { type: MarkerType.ArrowClosed, color: "var(--viz-axis-color)" },
    animated: false,
    style: {
      stroke: "var(--viz-axis-color)",
      strokeDasharray: dashed ? "6 4" : undefined,
      opacity: dead ? 0.3 : 1,
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
function ZoomControls({ className }: { className?: string }) {
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
    </div>
  );
}

export interface WorkflowCanvasProps {
  spec: WorkflowSpec;
  /** Live states per step id; omitted for a plain definition view. */
  states?: Record<string, NodeState>;
  deadEdges?: Set<string>;
  selectedStepId?: string | null;
  onSelectStep?: (stepId: string) => void;
  className?: string;
  /** Extra space under the zoom controls (the run view's dock bar sits below the canvas, not over it). */
  controlsClassName?: string;
  /** Floating panels drawn over the canvas (the run panel, approvals, the step detail). */
  children?: React.ReactNode;
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

function CanvasInner({ spec, states, deadEdges, selectedStepId = null, onSelectStep, className, controlsClassName, children }: WorkflowCanvasProps) {
  const graph = useMemo(() => buildWorkflowGraph(spec), [spec]);
  const graphRef = useRef<HTMLDivElement | null>(null);
  const graphLabel = `Execution graph of ${spec.name} with ${graph.nodes.length} steps`;
  useGraphImage(graphRef, graphLabel);

  const nodes: Node<StepNodeData>[] = useMemo(
    () => graph.nodes.map((n) => ({
      id: n.id,
      type: "step",
      position: { x: n.x, y: n.y },
      data: { step: n.step, state: states ? states[n.id] ?? "pending" : null, selected: n.id === selectedStepId },
      draggable: false,
      connectable: false,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    })),
    [graph, states, selectedStepId],
  );
  const edges: Edge[] = useMemo(
    () => graph.edges.map((e) => ({
      id: e.id,
      source: e.source,
      target: e.target,
      data: { kind: e.kind },
      ...edgeStyle(e, Boolean(deadEdges?.has(e.id))),
    })),
    [graph, deadEdges],
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
          onNodeClick={onSelectStep ? (_, node) => onSelectStep(node.id) : undefined}
          minZoom={0.2}
          // React Flow's own credit, visible (hiding it needs a Pro licence, and warns). It is a
          // sibling of the drawing, which alone is the role="img" (useGraphImage).
          attributionPosition="top-right"
        >
          <Background gap={16} color="var(--viz-grid-color)" />
        </ReactFlow>
      </div>
      {children}
      <ZoomControls className={cn("absolute bottom-3 right-3 z-10", controlsClassName)} />
    </div>
  );
}

export interface WorkflowStepListProps {
  spec: WorkflowSpec;
  states?: Record<string, NodeState>;
  selectedStepId?: string | null;
  onSelectStep?: (stepId: string) => void;
  /** Show the node state legend (the run view). */
  legend?: boolean;
}

/** The graph's steps as a list (buttons when steps can be selected), with each step's targets. */
export function WorkflowStepList({ spec, states, selectedStepId = null, onSelectStep, legend = false }: WorkflowStepListProps) {
  const graph = useMemo(() => buildWorkflowGraph(spec), [spec]);
  const outgoing = (stepId: string) => graph.edges.filter((e) => e.source === stepId);
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
        {graph.nodes.map((n) => {
          const state = states ? states[n.id] ?? "pending" : null;
          const targets = outgoing(n.id);
          const body = (
            <>
              <span className="flex min-w-0 items-center justify-between gap-2">
                <span className="truncate font-mono text-label font-medium text-foreground">{n.id}</span>
                <span className="shrink-0 text-caption text-muted-foreground">{STEP_TYPE_LABELS[n.step.type]}{state ? ` · ${NODE_STATE_LABELS[state]}` : ""}</span>
              </span>
              {targets.length > 0 ? (
                <span className="mt-0.5 block break-words text-caption text-muted-foreground">
                  {targets.map((e) => `→ ${e.target}${e.label ? ` (${e.label})` : ""}`).join(", ")}
                </span>
              ) : <span className="mt-0.5 block text-caption text-muted-foreground">ends</span>}
            </>
          );
          const cls = cn(
            "block w-full min-w-0 rounded-md border px-2 py-1.5 text-left",
            n.id === selectedStepId ? "border-foreground bg-fill-selected" : "border-border bg-surface",
          );
          return (
            <li key={n.id} data-testid={`workflow-step-${n.id}`} data-state={state ?? "definition"} data-step-type={n.step.type}>
              {onSelectStep ? (
                <button
                  type="button"
                  aria-pressed={n.id === selectedStepId}
                  aria-label={`Step ${n.id}, ${STEP_TYPE_LABELS[n.step.type]}${state ? `, ${NODE_STATE_LABELS[state]}` : ""}`}
                  onClick={() => onSelectStep(n.id)}
                  className={cn(cls, "outline-none hover:bg-fill-hover focus-visible:ring-2 focus-visible:ring-ring")}
                >
                  {body}
                </button>
              ) : <div className={cls}>{body}</div>}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
