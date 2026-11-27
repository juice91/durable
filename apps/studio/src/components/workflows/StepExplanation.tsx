"use client";

import React from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import type { ExplanationControl } from "@/lib/workflowExplanations";

/**
 * 「說明」: the one place a step's AI explanation shows, in the selected step's panel (the Graph tab's
 * Step details and the run page's step I/O panel). Shows the cached note, or a 「產生說明」 button that
 * runs ONE explicit generation for the whole version (never automatic), then the step's note.
 * `published` false: a draft has no explanation.
 */
export function StepExplanation({ control, stepId, published }: { control: ExplanationControl; stepId: string; published: boolean }) {
  const { status, error, notes, generate } = control;
  let body: React.ReactNode;
  if (!published) {
    body = <p data-testid="step-explanation-draft" className="text-caption text-muted-foreground">發佈後可產生說明</p>;
  } else if (status === "generating") {
    body = (
      <p role="status" data-testid="step-explanation-loading" className="flex items-center gap-2 text-caption text-muted-foreground">
        <Loader2 aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-4 animate-spin" />
        正在產生…
      </p>
    );
  } else if (status === "loading") {
    body = <p role="status" className="text-caption text-muted-foreground">載入中…</p>;
  } else if (status === "error") {
    body = (
      <div className="space-y-2">
        <p role="status" data-testid="step-explanation-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-caption text-destructive-text">{error}</p>
        <Button size="sm" data-testid="step-explanation-retry" onClick={() => generate(false)}>重試</Button>
      </div>
    );
  } else if (status === "ready") {
    const note = notes.get(stepId);
    body = note
      ? <p data-testid="step-explanation-note" className="select-text whitespace-pre-wrap break-words text-caption text-foreground">{note}</p>
      : <p data-testid="step-explanation-empty" className="text-caption text-muted-foreground">這個步驟沒有說明</p>;
  } else {
    body = (
      <Button size="sm" data-testid="step-explanation-generate" onClick={() => generate(false)}>
        產生說明
      </Button>
    );
  }
  return (
    <div data-testid="step-explanation" className="min-w-0 space-y-1">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">說明</h4>
      {body}
    </div>
  );
}
