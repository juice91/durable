"use client";

import React, { useMemo } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
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
  running: "border-primary bg-accent text-accent-foreground ring-2 ring-primary/40 animate-pulse",
  waiting: "border-warning bg-warning/10 text-warning-text",
  completed: "border-success bg-success/10 text-success-text",
  failed: "border-destructive bg-destructive/10 text-destructive-text",
  skipped: "border-dashed border-border bg-muted text-muted-foreground opacity-60",
  needs_attention: "border-2 border-warning bg-warning/15 text-warning-text",
};

const TYPE_LABELS: Record<WorkflowStep["type"], string> = {
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
      className={`flex flex-col justify-center rounded-md border-2 px-3 py-1 text-left shadow-sm ${tone} ${selected ? "outline outline-2 outline-offset-2 outline-ring" : ""}`}
    >
      <Handle type="target" position={Position.Top} isConnectable={false} />
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-sm font-semibold">{step.id}</span>
        <span className="shrink-0 rounded bg-background/70 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-foreground">{TYPE_LABELS[step.type]}</span>
      </div>
      <div className="flex items-center justify-between gap-2 text-xs">
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
    labelBgStyle: { fill: "hsl(var(--card))" },
    markerEnd: { type: MarkerType.ArrowClosed },
    animated: false,
    style: {
      stroke: "hsl(var(--muted-foreground))",
      strokeDasharray: dashed ? "6 4" : undefined,
      opacity: dead ? 0.3 : 1,
    },
  };
}

interface WorkflowGraphProps {
  spec: WorkflowSpec;
  /** Live states per step id; omitted for a plain definition view. */
  states?: Record<string, NodeState>;
  deadEdges?: Set<string>;
  selectedStepId?: string | null;
  onSelectStep?: (stepId: string) => void;
  height?: number;
}

/**
 * A read-only execution graph of a workflow version (a visualizer, not an editor): nodes cannot be
 * dragged or connected. Next to the canvas, the same steps as an accessible list of buttons.
 */
export function WorkflowGraph({ spec, states, deadEdges, selectedStepId = null, onSelectStep, height = 460 }: WorkflowGraphProps) {
  const graph = useMemo(() => buildWorkflowGraph(spec), [spec]);

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

  const outgoing = (stepId: string) => graph.edges.filter((e) => e.source === stepId);

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_16rem]">
      <div
        data-testid="workflow-graph"
        role="img"
        aria-label={`Execution graph of ${spec.name} with ${graph.nodes.length} steps`}
        style={{ height }}
        className="rounded-lg border border-border bg-background"
      >
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          colorMode="system"
          fitView
          fitViewOptions={{ padding: 0.2 }}
          nodesDraggable={false}
          nodesConnectable={false}
          edgesFocusable={false}
          elementsSelectable={Boolean(onSelectStep)}
          onNodeClick={onSelectStep ? (_, node) => onSelectStep(node.id) : undefined}
          minZoom={0.2}
        >
          <Background gap={16} />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <div className="space-y-3">
        {states ? (
          <ul aria-label="Node state legend" className="flex flex-wrap gap-1.5 text-xs">
            {NODE_STATES.map((s) => (
              <li key={s} className={`rounded border px-1.5 py-0.5 ${NODE_STATE_CLASSES[s]}`}>{NODE_STATE_LABELS[s]}</li>
            ))}
          </ul>
        ) : null}
        <ol aria-label="Workflow steps" data-testid="workflow-step-list" className="space-y-1.5">
          {graph.nodes.map((n) => {
            const state = states ? states[n.id] ?? "pending" : null;
            const targets = outgoing(n.id);
            const body = (
              <>
                <span className="flex items-center justify-between gap-2">
                  <span className="font-mono text-sm font-medium text-foreground">{n.id}</span>
                  <span className="text-xs text-muted-foreground">{TYPE_LABELS[n.step.type]}{state ? ` · ${NODE_STATE_LABELS[state]}` : ""}</span>
                </span>
                {targets.length > 0 ? (
                  <span className="mt-0.5 block text-xs text-muted-foreground">
                    {targets.map((e) => `→ ${e.target}${e.label ? ` (${e.label})` : ""}`).join(", ")}
                  </span>
                ) : <span className="mt-0.5 block text-xs text-muted-foreground">ends</span>}
              </>
            );
            const cls = `block w-full rounded-md border px-2 py-1.5 text-left ${n.id === selectedStepId ? "border-primary bg-accent" : "border-border bg-card"}`;
            return (
              <li key={n.id} data-testid={`workflow-step-${n.id}`} data-state={state ?? "definition"} data-step-type={n.step.type}>
                {onSelectStep ? (
                  <button
                    type="button"
                    aria-pressed={n.id === selectedStepId}
                    aria-label={`Step ${n.id}, ${TYPE_LABELS[n.step.type]}${state ? `, ${NODE_STATE_LABELS[state]}` : ""}`}
                    onClick={() => onSelectStep(n.id)}
                    className={`${cls} hover:bg-accent/60`}
                  >
                    {body}
                  </button>
                ) : <div className={cls}>{body}</div>}
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}
