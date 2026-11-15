"use client";

import React from "react";
import { Info, TriangleAlert } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import {
  CASE_STATUS_LABELS,
  CHANGE_LABELS,
  CHANGE_VARIANTS,
  KNOWLEDGE_NOTE,
  MEMORY_NOTE,
  STATUS_VARIANTS,
  deltaTone,
  type CaseChange,
  type EvalCaseStatus,
} from "@/lib/evals";

/** Inputs, selects and textareas on the Tests tab (theme tokens only). */
export const fieldClass =
  "w-full min-w-0 rounded-md border border-input bg-surface px-3 py-1.5 text-label text-foreground outline-none placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
export const monoFieldClass = cn(fieldClass, "font-mono");
export const labelClass = "block text-label font-medium text-foreground";
export const hintClass = "text-caption text-muted-foreground";
export const alertClass = "rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text";
export const warningClass = "rounded-md border border-warning/30 bg-warning/10 p-3 text-body text-warning-text";
export const cardClass = "min-w-0 rounded-lg border border-border bg-surface";
export const sectionTitleClass = "text-title font-semibold text-foreground";
/** KPI grid (layout rule 3). */
export const kpiGridClass = "grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(min(100%,11rem),1fr))]";

export function CaseStatusBadge({ status }: { status: EvalCaseStatus | null }) {
  if (!status) return <span className="text-muted-foreground">—</span>;
  return (
    <Badge variant={STATUS_VARIANTS[status] ?? "neutral"} data-testid="eval-case-status" data-status={status}>
      {CASE_STATUS_LABELS[status] ?? status}
    </Badge>
  );
}

export function ChangeBadge({ change }: { change: CaseChange }) {
  return (
    <Badge variant={CHANGE_VARIANTS[change] ?? "neutral"} data-testid="eval-compare-change" data-change={change}>
      {CHANGE_LABELS[change] ?? change}
    </Badge>
  );
}

/** A delta coloured by whether it is an improvement (lower is better for cost and time). */
export function Delta({ value, text, lowerIsBetter = false, testId }: { value: number | string | null | undefined; text: string; lowerIsBetter?: boolean; testId?: string }) {
  const tone = deltaTone(value, lowerIsBetter);
  return (
    <span
      data-testid={testId}
      data-tone={tone}
      className={cn("tabular-nums", tone === "better" ? "text-success-text" : tone === "worse" ? "text-destructive-text" : "text-muted-foreground")}
    >
      {text}
    </span>
  );
}

/** The memory note shown on an agent with memory (cases run as single turns without memory). */
export function MemoryNote({ className }: { className?: string }) {
  return (
    <p data-testid="eval-memory-note" className={cn("flex min-w-0 items-start gap-2 rounded-md border border-border bg-muted/40 p-3 text-body text-foreground", className)}>
      <Info strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className="mt-0.5 size-icon shrink-0 text-muted-foreground" />
      <span className="min-w-0">{MEMORY_NOTE}.</span>
    </p>
  );
}

/** The knowledge note in the run dialog, for a target whose spec has knowledge bases. */
export function KnowledgeNote({ className }: { className?: string }) {
  return (
    <p data-testid="eval-knowledge-note" className={cn("flex min-w-0 items-start gap-2 rounded-md border border-border bg-muted/40 p-3 text-body text-foreground", className)}>
      <Info strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className="mt-0.5 size-icon shrink-0 text-muted-foreground" />
      <span className="min-w-0">{KNOWLEDGE_NOTE}.</span>
    </p>
  );
}

export function WarningIcon() {
  return <TriangleAlert strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className="mt-0.5 size-icon shrink-0" />;
}

/** One KPI tile. */
export function Kpi({ label, value, testId, children }: { label: string; value: React.ReactNode; testId?: string; children?: React.ReactNode }) {
  return (
    <div className={cn(cardClass, "p-3")} data-testid={testId}>
      <p className="text-caption text-muted-foreground">{label}</p>
      <p className="mt-1 text-title font-semibold tabular-nums text-foreground">{value}</p>
      {children}
    </div>
  );
}

/** "a / b" progress bar for a running eval run. */
export function ProgressBar({ done, total }: { done: number; total: number }) {
  const percent = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
  return (
    <div className="min-w-0" data-testid="eval-progress">
      <div className="mb-1 flex items-center justify-between gap-2 text-label">
        <span className="text-foreground">Running cases</span>
        <span className="tabular-nums text-muted-foreground" data-testid="eval-progress-count">
          {done} of {total}
        </span>
      </div>
      <div
        role="progressbar"
        aria-label="Cases finished"
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={done}
        className="h-2 w-full overflow-hidden rounded-full bg-muted"
      >
        <div className="h-full bg-primary transition-[width] motion-reduce:transition-none" style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}
