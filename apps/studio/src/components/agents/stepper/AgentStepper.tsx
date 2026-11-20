"use client";

import React, { useState } from "react";
import { Check, ChevronLeft, ChevronRight, History } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Button, focusRing } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import type { StepStatus } from "../specSections";
import type { EditorLayout } from "./editorFit";
import { neighbourSteps } from "./registry";
import type { EditorStepDefinition } from "./types";
import { Select } from "@/components/ui/Select";

export const VERSIONS_TITLE = "Versions";

export interface AgentStepperProps {
  steps: readonly EditorStepDefinition[];
  /** Each step's status line and validity, by id. */
  statuses: Record<string, StepStatus>;
  /** Steps holding the last save or publish error. */
  errorSteps: readonly string[];
  activeStep: string;
  onStepChange: (id: string) => void;
  layout: EditorLayout;
  /** Header actions (Save draft, Test draft, Publish; Create agent). */
  actions: React.ReactNode;
  /** Messages under the header (published, conflicts, "save before publishing"). */
  notices?: React.ReactNode;
  /** The versions list: a column when the layout has room, else a header "Versions" Sheet. */
  versions?: React.ReactNode;
  /** The step bodies (all mounted; only the active one shown). */
  children: React.ReactNode;
}

const ICON = { strokeWidth: ICON_STROKE_WIDTH, "aria-hidden": true } as const;

function statusTone(valid: boolean, hasError: boolean): string {
  if (hasError) return "text-destructive-text";
  return valid ? "text-muted-foreground" : "text-warning-text";
}

/**
 * The stepped editor's frame (decision "Editor as a stepped form"): a numbered step nav with status
 * lines and checks, the active step, Previous / Next named after the neighbouring steps, and the
 * versions column. The layout comes from the fit rule (`editorFit.ts`): three columns, nav + step
 * with versions in a Sheet, or a step select (phones).
 */
export function AgentStepper({ steps, statuses, errorSteps, activeStep, onStepChange, layout, actions, notices, versions, children }: AgentStepperProps) {
  const [versionsOpen, setVersionsOpen] = useState(false);
  const { previous, next } = neighbourSteps(activeStep, steps);
  const activeIndex = Math.max(
    0,
    steps.findIndex((step) => step.id === activeStep),
  );
  const versionsInColumn = layout === "columns" && versions !== undefined;
  const versionsInSheet = layout !== "columns" && versions !== undefined;

  const statusOf = (step: EditorStepDefinition) => statuses[step.id] ?? { text: "", valid: true };

  const nav =
    layout === "compact" ? null : (
      <nav aria-label="Editor steps" data-testid="stepper-nav" className="min-w-0 border-b border-border p-2 md:border-b-0 md:border-r">
        <ol className="space-y-0.5">
          {steps.map((step, index) => {
            const status = statusOf(step);
            const hasError = errorSteps.includes(step.id);
            const active = step.id === activeStep;
            const done = status.valid && !hasError;
            return (
              <li key={step.id}>
                <button
                  type="button"
                  aria-current={active ? "step" : undefined}
                  onClick={() => onStepChange(step.id)}
                  className={cn(
                    "flex w-full min-w-0 items-start gap-2 rounded-md px-2 py-1.5 text-left hover:bg-fill-hover",
                    active && "bg-fill-selected",
                    focusRing,
                  )}
                >
                  <span
                    aria-hidden="true"
                    className={cn(
                      "mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full border text-badge tabular-nums",
                      done ? "border-transparent bg-primary text-primary-foreground" : "border-border-strong text-muted-foreground",
                      hasError && "border-destructive text-destructive-text",
                    )}
                  >
                    {done ? <Check className="size-3" {...ICON} /> : index + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-label font-medium text-foreground">{step.label}</span>
                    <span className={cn("block truncate text-caption", statusTone(status.valid, hasError))}>{hasError ? "Has an error" : status.text}</span>
                  </span>
                  <span className="sr-only">{hasError ? ", has an error" : done ? ", complete" : ", needs attention"}</span>
                </button>
              </li>
            );
          })}
        </ol>
      </nav>
    );

  const select =
    layout === "compact" ? (
      <div className="mb-4">
        <label htmlFor="editor-step-select" className="mb-1 block text-caption text-muted-foreground">
          Step {activeIndex + 1} of {steps.length}
        </label>
        <Select
          id="editor-step-select"
          data-testid="stepper-select"
          value={activeStep}
          onValueChange={(v) => onStepChange(v)}
          className={cn("w-full min-w-0 rounded-md border border-input bg-surface px-3 py-2 text-body text-foreground", focusRing)}
        >
          {steps.map((step, index) => {
            const status = statusOf(step);
            const hasError = errorSteps.includes(step.id);
            return (
              <option key={step.id} value={step.id}>
                {`${index + 1}. ${step.label} — ${hasError ? "Has an error" : status.text}`}
              </option>
            );
          })}
        </Select>
      </div>
    ) : null;

  const gridClass = versionsInColumn ? "md:grid-cols-[220px_minmax(0,1fr)_260px]" : layout === "compact" ? "grid-cols-1" : "md:grid-cols-[220px_minmax(0,1fr)]";

  return (
    <div data-testid="agent-stepper" data-layout={layout} className="flex min-w-0 flex-col">
      <div className="flex min-w-0 flex-wrap items-center justify-end gap-2 border-b border-border px-4 py-2">
        {versionsInSheet ? (
          <Button size="sm" variant="ghost" onClick={() => setVersionsOpen(true)} data-testid="versions-sheet-toggle">
            <History {...ICON} />
            {VERSIONS_TITLE}
          </Button>
        ) : null}
        {actions}
      </div>
      {notices ? <div className="min-w-0 space-y-2 px-4 pt-3">{notices}</div> : null}
      <div className={cn("grid min-w-0 grid-cols-1", gridClass)}>
        {nav}
        <div className="min-w-0 px-4 py-4">
          {select}
          {children}
          {/* `relative` on the buttons: their sr-only labels (position: absolute) stay inside them, not
              at an ancestor outside the scroll area, where they added a page scroll at 390 px. */}
          <div className="mt-6 flex min-w-0 flex-wrap justify-between gap-2 border-t border-border pt-4">
            {previous ? (
              <Button variant="secondary" onClick={() => onStepChange(previous.id)} data-testid="stepper-previous" className="relative max-w-full">
                <ChevronLeft {...ICON} />
                <span className="sr-only">Previous step: </span>
                <span className="truncate">{previous.label}</span>
              </Button>
            ) : (
              <span />
            )}
            {next ? (
              <Button variant="secondary" onClick={() => onStepChange(next.id)} data-testid="stepper-next" className="relative max-w-full">
                <span className="sr-only">Next step: </span>
                <span className="truncate">{next.label}</span>
                <ChevronRight {...ICON} />
              </Button>
            ) : null}
          </div>
        </div>
        {versionsInColumn ? (
          <aside aria-label={VERSIONS_TITLE} data-testid="versions-column" className="min-w-0 border-l border-border p-3">
            {versions}
          </aside>
        ) : null}
      </div>
      {versionsInSheet ? (
        <Sheet open={versionsOpen} onOpenChange={setVersionsOpen}>
          <SheetContent size="md" aria-describedby={undefined} data-testid="versions-sheet">
            <SheetHeader title={VERSIONS_TITLE} />
            <SheetBody>{versions}</SheetBody>
          </SheetContent>
        </Sheet>
      ) : null}
    </div>
  );
}
