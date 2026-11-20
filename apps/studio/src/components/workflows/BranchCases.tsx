import React from "react";
import { conditionTree, type ConditionNode } from "@/lib/workflowGraph";
import type { BranchStep } from "@/lib/workflows";
import type { StepWarning } from "@/lib/workflowRunState";

/** Which case a finished branch took: its output's `case` (an index, null for the default) and `next`. */
export interface BranchTaken {
  case: number | null;
  next: string;
}

/** The taken case from a branch step's output, or null when it has none (not run yet, or an odd shape). */
export function branchTaken(output: unknown): BranchTaken | null {
  if (!output || typeof output !== "object") return null;
  const o = output as Record<string, unknown>;
  if (typeof o.next !== "string") return null;
  return { case: typeof o.case === "number" ? o.case : null, next: o.next };
}

function clauseLines(nodes: ConditionNode[], indent: string): string[] {
  return nodes.flatMap((c) => (c.kind === "leaf" ? [`${indent}${c.text}`] : [`${indent}${c.kind}(`, ...clauseLines(c.clauses, `${indent}  `)]));
}

/** A branch's cases as plain text, one per line (`1  condition  →  target`), for copying. */
export function branchCasesText(step: BranchStep): string {
  const lines = step.cases.flatMap((c, i) => {
    const tree = conditionTree(c.when);
    const head = `${i + 1}  `;
    if (tree.kind === "leaf") return [`${head}${tree.text}  →  ${c.next}`];
    return [`${head}${tree.kind}(`, ...clauseLines(tree.clauses, "    "), `    →  ${c.next}`];
  });
  return [...lines, `default  →  ${step.default}`].join("\n");
}

function Clauses({ nodes, depth }: { nodes: ConditionNode[]; depth: number }) {
  return (
    <span className="mt-0.5 block space-y-0.5 border-l border-border pl-2" style={{ marginLeft: depth * 4 }}>
      {nodes.map((c, i) => (
        <Clause key={i} node={c} depth={depth + 1} />
      ))}
    </span>
  );
}

function Clause({ node, depth }: { node: ConditionNode; depth: number }) {
  if (node.kind === "leaf") return <span data-testid="branch-clause" className="block break-words font-mono">{node.text}</span>;
  return (
    <span className="block">
      <span className="font-mono text-muted-foreground">{node.kind}(</span>
      <Clauses nodes={node.clauses} depth={depth} />
    </span>
  );
}

/**
 * A branch step's cases as numbered rows (`1  condition  →  target`), `all(…)` / `any(…)` as an
 * indented list of their clauses, and the default last. The taken case is highlighted; a
 * `condition_unknown` warning shows as an amber note on its case. Spans only, so it can sit inside
 * the step list's button.
 */
export function BranchCases({ step, taken, warnings }: { step: BranchStep; taken?: BranchTaken | null; warnings?: readonly StepWarning[] }) {
  const unknown = new Set((warnings ?? []).filter((w) => w.code === "condition_unknown").map((w) => w.condition));
  const row = (key: string, index: number | null, isTaken: boolean, body: React.ReactNode, target: string, note: boolean) => (
    <span
      key={key}
      data-testid="branch-case"
      data-case={index === null ? "default" : String(index + 1)}
      data-taken={isTaken ? "true" : undefined}
      className={`block rounded-sm border px-1.5 py-1 text-caption ${isTaken ? "border-success bg-success/10 text-foreground" : "border-transparent text-muted-foreground"}`}
    >
      <span className="flex min-w-0 items-baseline gap-2">
        <span className="w-4 shrink-0 text-right font-mono tabular-nums">{index === null ? "" : index + 1}</span>
        <span className="min-w-0 flex-1 break-words">{body}</span>
        <span className="shrink-0 font-mono">→ {target}</span>
      </span>
      {note ? (
        <span data-testid="branch-case-unknown" className="mt-0.5 block rounded-sm border border-warning/40 bg-warning/10 px-1 text-warning-text">
          An input was not given, so this case was not taken.
        </span>
      ) : null}
    </span>
  );
  return (
    <span data-testid="branch-cases" className="mt-1 block space-y-0.5">
      {step.cases.map((c, i) => {
        const tree = conditionTree(c.when);
        const body = tree.kind === "leaf" ? <span className="font-mono">{tree.text}</span> : (
          <span><span className="font-mono">{tree.kind}(</span><Clauses nodes={tree.clauses} depth={0} /></span>
        );
        return row(`c${i}`, i, taken?.case === i, body, c.next, unknown.has(`cases.${i}`));
      })}
      {row("default", null, taken != null && taken.case === null, <span className="font-mono">default</span>, step.default, false)}
    </span>
  );
}
