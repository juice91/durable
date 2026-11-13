import type { SpanKind, SpanStatus } from "@/lib/traces";

/** Bar and dot colours per span status (theme tokens where one fits; status hues follow StatusPill). */
export const STATUS_BAR: Record<SpanStatus, string> = {
  ok: "bg-green-600 dark:bg-green-500",
  error: "bg-destructive",
  running: "bg-primary/60 animate-pulse",
  waiting: "bg-yellow-500 dark:bg-yellow-400",
  incomplete: "bg-muted-foreground/50",
};

export const STATUS_TEXT: Record<SpanStatus, string> = {
  ok: "text-green-700 dark:text-green-400",
  error: "text-destructive dark:text-red-400",
  running: "text-card-foreground",
  waiting: "text-yellow-700 dark:text-yellow-400",
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
