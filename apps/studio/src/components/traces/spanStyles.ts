import type { SpanKind, SpanStatus } from "@/lib/traces";

/** Bar and dot colours per span status: theme tokens only (success/warning are defined in globals.css). */
export const STATUS_BAR: Record<SpanStatus, string> = {
  ok: "bg-success",
  error: "bg-destructive",
  running: "bg-primary/60 animate-pulse",
  waiting: "bg-warning",
  incomplete: "bg-muted-foreground/50",
};

export const STATUS_TEXT: Record<SpanStatus, string> = {
  ok: "text-success",
  error: "text-destructive-text",
  running: "text-card-foreground",
  waiting: "text-warning",
  incomplete: "text-muted-foreground",
};

export const STATUS_LABELS: Record<SpanStatus, string> = {
  ok: "OK",
  error: "Error",
  running: "Running",
  waiting: "Waiting",
  incomplete: "Incomplete",
};

export const KIND_BADGE: Record<SpanKind, string> = {
  run: "RUN",
  child_run: "RUN",
  model_request: "LLM",
  tool_call: "TOOL",
  processor: "PROC",
  workflow_step: "STEP",
};
