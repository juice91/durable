import {
  Binary,
  Bot,
  Brain,
  CirclePlay,
  DatabaseZap,
  Eye,
  FileInput,
  NotebookPen,
  Search,
  ShieldCheck,
  Sparkles,
  Workflow,
  Wrench,
  type LucideIcon,
} from "lucide-react";
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
  retrieve: "RAG",
  embedding: "EMB",
  memory_recall: "MEM",
  memory_index: "MEM",
  observer: "MEM",
  reflector: "MEM",
  working_memory: "MEM",
  ingest: "KB",
};

/** One icon per span kind (decorative: the badge and label carry the meaning). */
export const KIND_ICON: Record<SpanKind, LucideIcon> = {
  run: CirclePlay,
  child_run: CirclePlay,
  model_request: Bot,
  tool_call: Wrench,
  processor: ShieldCheck,
  workflow_step: Workflow,
  retrieve: Search,
  embedding: Binary,
  memory_recall: Brain,
  memory_index: DatabaseZap,
  observer: Eye,
  reflector: Sparkles,
  working_memory: NotebookPen,
  ingest: FileInput,
};

/**
 * Icon colour per span kind, from the chart tokens only (chartTheme.css, inside an `.eap-viz`
 * scope): retrieval spans (retrieve, embedding, ingest) series 1, memory spans series 2; the other
 * kinds stay muted. Icons are graphics: the series meet >= 3:1 on the card surfaces (contrast.test.ts),
 * but the light theme's series 2 on a selected row (`bg-accent`) is about 2.9:1. They stay decorative
 * (aria-hidden) and colour is never the only cue (badge and label), so this is accepted.
 */
export const KIND_TONE: Record<SpanKind, string> = {
  run: "text-muted-foreground",
  child_run: "text-muted-foreground",
  model_request: "text-muted-foreground",
  tool_call: "text-muted-foreground",
  processor: "text-muted-foreground",
  workflow_step: "text-muted-foreground",
  retrieve: "text-[color:var(--viz-series-1-color)]",
  embedding: "text-[color:var(--viz-series-1-color)]",
  ingest: "text-[color:var(--viz-series-1-color)]",
  memory_recall: "text-[color:var(--viz-series-2-color)]",
  memory_index: "text-[color:var(--viz-series-2-color)]",
  observer: "text-[color:var(--viz-series-2-color)]",
  reflector: "text-[color:var(--viz-series-2-color)]",
  working_memory: "text-[color:var(--viz-series-2-color)]",
};
