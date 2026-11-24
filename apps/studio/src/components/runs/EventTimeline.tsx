import { subjectLabel } from "@/lib/subjects";
import React from "react";
import { CopyableText } from "@/components/ui/CopyableText";
import { SafeMarkdown } from "@/components/markdown/SafeMarkdown";
import { formatMatchCounts } from "@/lib/processors";
import { type RunEvent, type RunDelta, type PartialTextMap, type RunStatus } from "@/lib/runs";
import { TraceLink } from "@/components/traces/TraceLink";
import { StructuredOutputView } from "@/components/chat/StructuredOutput";
import {
  attemptText,
  outputRetryOf,
  outputRetryText,
  turnCapabilityFailure,
  turnFallback,
} from "@/components/chat/turnCapabilities";

interface EventItemProps {
  event: RunEvent;
}

interface EventTimelineProps {
  events: RunEvent[];
  currentPartialText: PartialTextMap;
  /** The run's display status: a terminal run with no events gets an "ended" placeholder, not
   * "check back soon" (e.g. a superseded conversation turn that never started). */
  status?: RunStatus;
}

const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>(["completed", "skipped", "failed", "needs_attention"]);

/** Event payload values are JSON: text stays as is, anything else is shown as pretty JSON (a tool
 * run's input and result are objects, which React cannot render as children). */
export function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2) ?? String(value);
}

function JsonBlock({ value, label }: { value: unknown; label?: string }) {
  return (
    <CopyableText label={label?.toLowerCase() ?? "payload"} copyText={formatValue(value)} className="mt-1">
      <pre aria-label={label} className="text-xs text-muted-foreground bg-muted p-2 pr-9 rounded whitespace-pre-wrap break-words">
        {formatValue(value)}
      </pre>
    </CopyableText>
  );
}

function MarkdownContent({ text }: { text: string }) {
  return (
    <div className="prose dark:prose-invert max-w-none text-sm text-card-foreground/80">
      {/* Model text can carry tenant knowledge: images as alt text only, no raw HTML, safe links. */}
      <SafeMarkdown>{text}</SafeMarkdown>
    </div>
  );
}

export function EventTimeline({ events, currentPartialText, status }: EventTimelineProps) {
  if (events.length === 0) {
    if (status !== undefined && TERMINAL_STATUSES.has(status)) {
      return (
        <div className="p-4 text-card-foreground bg-card rounded-lg text-center">
          No events were recorded. This run has ended.
        </div>
      );
    }
    return (
      <div className="p-4 text-card-foreground bg-card rounded-lg text-center">
        No events yet. Run started, check back soon.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {events.map((event) => (
        <EventItem key={event.seq} event={event} />
      ))}

      {/* Show current partial text under active model request */}
      {Object.entries(currentPartialText).length > 0 && (
        <div className="mt-4 p-4 bg-card border border-border rounded-lg">
          <h4 className="text-sm font-semibold text-card-foreground mb-2">Live partial text</h4>
          <div className="space-y-2">
            {Object.entries(currentPartialText).map(([modelRequest, partial]) => (
              <div key={modelRequest} className="text-sm text-card-foreground/80">
                <span className="font-mono text-xs text-muted-foreground mr-2">
                  Request #{modelRequest}
                </span>
                <span>{partial.text}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** One processor outcome as a compact line: phase, processor type, action, then the match counts,
 * flagged categories or reason. Only these fields are read; the event never carries text, and none
 * is shown even if it did. */
function ProcessorLine({ data }: { data: RunEvent["data"] }) {
  const phase = data.phase === "input" ? "Input" : data.phase === "output" ? "Output" : "";
  const type = typeof data.processor_type === "string" ? data.processor_type : "processor";
  const action = typeof data.action === "string" ? data.action : "";
  const counts = formatMatchCounts(data.match_counts, "");
  const categories = Array.isArray(data.categories)
    ? data.categories.filter((c): c is string => typeof c === "string").join(", ")
    : "";
  const reason = typeof data.reason === "string" ? data.reason : "";
  const detail = [counts, categories && `flagged: ${categories}`, reason && `reason: ${reason}`]
    .filter(Boolean)
    .join("; ");
  const blocked = action === "block";
  return (
    <div className="text-sm" data-testid="processor-result">
      <span className="font-semibold">{`${phase} processor`.trim()}</span>
      <span className="ml-1 font-mono">{type}</span>
      {action && (
        <span className={blocked ? "ml-1 font-semibold text-destructive-text" : "ml-1 text-muted-foreground"}>
          {action}
        </span>
      )}
      {typeof data.model_request === "number" && (
        <span className="ml-1 text-xs text-muted-foreground">(request #{data.model_request})</span>
      )}
      {detail && <span className="block mt-1 text-xs text-muted-foreground">{detail}</span>}
    </div>
  );
}

// ------------------------------------------------------------------ RAG and memory rows (RM.19)
//
// These events carry ids, counts, model and cost only (never memory, chunk or query text); the rows
// read an allowlist of fields, so nothing else is shown even if an event carried it.

type Fact = [label: string, value: string | null];

function factNumber(value: unknown): string | null {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("en-US") : null;
}

function factText(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function factCost(value: unknown): string | null {
  const text = factText(value);
  if (text === null) return null;
  const n = Number(text);
  return Number.isFinite(n) ? `$${n < 0.01 && n > 0 ? n.toPrecision(2) : n.toFixed(4)}` : null;
}

function Facts({ title, facts, tone = "normal", testId }: { title: string; facts: Fact[]; tone?: "normal" | "error"; testId: string }) {
  const shown = facts.filter((f): f is [string, string] => f[1] !== null);
  return (
    <div className="text-sm" data-testid={testId}>
      <span className={tone === "error" ? "font-semibold text-destructive-text" : "font-semibold text-card-foreground"}>{title}</span>
      {shown.length > 0 && (
        <span className="mt-1 block break-words text-xs text-muted-foreground">
          {shown.map(([label, value]) => `${label}: ${value}`).join(" · ")}
        </span>
      )}
    </div>
  );
}

const EMBEDDING_STAGE_TITLES: Record<string, string> = { recall: "Semantic recall", index: "Memory index" };

function EmbeddingLine({ data }: { data: RunEvent["data"] }) {
  const stage = factText(data.stage);
  const failed = data.status === "failed";
  const title = `${(stage && EMBEDDING_STAGE_TITLES[stage]) || "Embedding"}${failed ? " failed" : ""}`;
  return (
    <Facts
      testId="event-embedding"
      title={title}
      tone={failed ? "error" : "normal"}
      facts={[
        ["error", factText(data.error_code)],
        ["model", factText(data.model_id)],
        ["dimensions", factNumber(data.dimensions)],
        ["inputs", factNumber(data.inputs)],
        ["tokens", failed ? null : factNumber(data.input_tokens)],
        ["cost", factCost(data.cost_usd)],
        ["hits", factNumber(data.hits)],
        ["turns", factNumber(data.turns)],
        ["messages", factNumber(data.messages)],
        ["inserted", factNumber(data.inserted)],
        ["already indexed", factNumber(data.existing)],
        ["redacted", factNumber(data.redacted)],
      ]}
    />
  );
}

const INGEST_PHASE_TITLES: Record<string, string> = {
  chunked: "Document chunked",
  embedding: "Embedding batch",
  finalized: "Ingest finalized",
};

function IngestLine({ data }: { data: RunEvent["data"] }) {
  const phase = factText(data.phase);
  const progress =
    factNumber(data.embedded) !== null && factNumber(data.total) !== null
      ? `${factNumber(data.embedded)} of ${factNumber(data.total)} sections`
      : null;
  return (
    <Facts
      testId="event-ingest-progress"
      title={(phase && INGEST_PHASE_TITLES[phase]) || "Ingest progress"}
      facts={[
        ["batch", factNumber(data.batch)],
        ["embedded", progress],
        ["chunks", factNumber(data.chunks)],
        ["model", factText(data.model_id)],
        ["tokens", factNumber(data.input_tokens)],
        ["tokens to embed", factNumber(data.pending_tokens)],
        ["cost", factCost(data.cost_usd)],
        ["generation", factNumber(data.generation)],
      ]}
    />
  );
}

function MemoryLine({ data, failed }: { data: RunEvent["data"]; failed: boolean }) {
  const stage = factText(data.stage);
  const usage = data.usage && typeof data.usage === "object" ? (data.usage as Record<string, unknown>) : null;
  const covers =
    factNumber(data.covers_from_turn) !== null && factNumber(data.covers_to_turn) !== null
      ? `${factNumber(data.covers_from_turn)} to ${factNumber(data.covers_to_turn)}`
      : null;
  if (stage === "observe" || stage === "reflect") {
    const who = stage === "reflect" ? "Reflector" : "Observer";
    return (
      <Facts
        testId="event-memory-observer"
        title={failed ? `${who} failed` : stage === "reflect" ? "Observations reflected" : "Observations added"}
        tone={failed ? "error" : "normal"}
        facts={[
          ["error", factText(data.error_code)],
          ["turns", covers],
          ["notes", factNumber(data.notes)],
          ["approx. tokens", factNumber(data.approx_tokens)],
          ["superseded", factNumber(data.superseded)],
          ["redacted", factNumber(data.redacted)],
          ["model", factText(data.model_id)],
          ["tokens in", usage ? factNumber(usage.input_tokens) : null],
          ["tokens out", usage ? factNumber(usage.output_tokens) : null],
          ["cost", factCost(data.cost_usd)],
        ]}
      />
    );
  }
  const keys = Array.isArray(data.changed_keys) ? data.changed_keys.filter((k): k is string => typeof k === "string") : [];
  return (
    <Facts
      testId="event-memory-working"
      title={failed ? "Memory update failed" : "Working memory updated"}
      tone={failed ? "error" : "normal"}
      facts={[
        ["error", factText(data.error_code)],
        ["scope", factText(data.scope)],
        ["version", factNumber(data.version)],
        ["changed", keys.length ? keys.join(", ") : null],
        ["redacted", factNumber(data.redacted)],
      ]}
    />
  );
}

// ------------------------------------------------------------- agent capabilities rows (AC.14)

/** A model response's fallback facts (AC.5): requested model, fallback index, failed attempts. */
function FallbackFacts({ event }: { event: RunEvent }) {
  const fallback = turnFallback([event]);
  if (!fallback && typeof event.data.requested_model_id !== "string") return null;
  const index = typeof event.data.fallback_index === "number" ? event.data.fallback_index : null;
  return (
    <span data-testid="event-fallback" className="mt-1 block break-words text-xs text-muted-foreground">
      {[
        typeof event.data.requested_model_id === "string" ? `requested: ${event.data.requested_model_id}` : null,
        index !== null
          ? index > 0
            ? `answered by fallback #${index}${typeof event.data.answering_model === "string" && event.data.answering_model ? ` (${event.data.answering_model})` : ""}`
            : "answered by the primary"
          : null,
        fallback && fallback.attempts.length > 0 ? `failed: ${fallback.attempts.map(attemptText).join("; ")}` : null,
      ]
        .filter(Boolean)
        .join(" · ")}
    </span>
  );
}

const TOOL_DECISION_TITLES: Record<string, string> = {
  tool_approved: "Tool call approved",
  tool_denied: "Tool call denied",
  tool_approval_timed_out: "Tool approval timed out (denied)",
};

function ToolDecisionLine({ event }: { event: RunEvent }) {
  const decidedBy = factText(event.data.decided_by);
  const by = decidedBy === null ? null : subjectLabel(decidedBy);
  const comment = factText(event.data.comment);
  return (
    <div className="text-sm" data-testid="event-tool-decision" data-decision={event.type}>
      <span className={event.type === "tool_approved" ? "font-semibold text-success-text" : "font-semibold text-warning-text"}>
        {TOOL_DECISION_TITLES[event.type] ?? event.type}
      </span>
      <span className="mt-1 block break-words text-xs text-muted-foreground">
        {[`call: ${factText(event.data.tool_call_id) ?? "?"}`, by && `by ${by}`].filter(Boolean).join(" · ")}
      </span>
      {comment ? <span className="mt-1 block whitespace-pre-wrap text-xs text-muted-foreground">{comment}</span> : null}
    </div>
  );
}

// ------------------------------------------------------------- agents as tools

const AGENT_TOOL_STATUS_TITLES: Record<string, string> = {
  completed: "Agent call completed",
  refused: "Agent call refused",
  timeout: "Agent call timed out",
  failed: "Agent call failed",
};

/** `agent_tool_started` / `agent_tool_completed`: ids, version, depth and outcome, never text. The child
 * run's trace opens from the generic "Open child run trace" link under every event with a `child_run_id`. */
function AgentToolLine({ event }: { event: RunEvent }) {
  const data = event.data;
  if (event.type === "agent_tool_started") {
    return (
      <Facts
        testId="event-agent-tool-started"
        title="Agent call started"
        facts={[
          ["tool", factText(data.tool_name)],
          ["version", factNumber(data.version)],
          ["depth", factNumber(data.depth)],
          ["timeout", data.timeout_seconds !== undefined ? `${factNumber(data.timeout_seconds)} s` : null],
        ]}
      />
    );
  }
  const status = factText(data.status);
  const ok = status === "completed";
  return (
    <Facts
      testId="event-agent-tool-completed"
      title={(status && AGENT_TOOL_STATUS_TITLES[status]) || "Agent call ended"}
      tone={ok ? "normal" : "error"}
      facts={[
        ["tool", factText(data.tool_name)],
        ["error", factText(data.error_code)],
        ["output truncated", data.truncated === true ? "yes" : null],
      ]}
    />
  );
}

// ----------------------------------------------------------------------- eval runs (AC.11)
//
// Status, counts, duration and cost only: the events never carry case input or output text.

const EVAL_CASE_TITLES: Record<string, string> = { passed: "Case passed", failed: "Case failed", error: "Case error" };

function EvalCaseLine({ data }: { data: RunEvent["data"] }) {
  const status = factText(data.status);
  const assertions = data.assertions && typeof data.assertions === "object" ? (data.assertions as Record<string, unknown>) : null;
  const passed = assertions ? factNumber(assertions.passed) : null;
  const total = assertions ? factNumber(assertions.total) : null;
  return (
    <Facts
      testId="event-eval-case"
      title={(status && EVAL_CASE_TITLES[status]) || "Case result"}
      tone={status === "passed" ? "normal" : "error"}
      facts={[
        ["case", factText(data.case_id)],
        ["position", factNumber(data.position)],
        ["error", factText(data.error_code)],
        ["assertions", passed !== null && total !== null ? `${passed} of ${total} passed` : null],
        ["scores", factNumber(data.scores)],
        ["labels", factNumber(data.labels)],
        ["evaluator failures", data.evaluator_failures ? factNumber(data.evaluator_failures) : null],
        ["duration", typeof data.duration_ms === "number" ? `${factNumber(data.duration_ms)} ms` : null],
        ["cost", factCost(data.cost_usd)],
      ]}
    />
  );
}

function EvalStartLine({ data }: { data: RunEvent["data"] }) {
  return (
    <Facts
      testId="event-eval-start"
      title="Eval run started"
      facts={[
        ["suite", factText(data.suite_id)],
        ["agent version", data.draft === true ? "draft" : factNumber(data.agent_version)],
        ["cases", factNumber(data.cases)],
        ["concurrency", factNumber(data.max_concurrency)],
        ["suite hash", factText(data.suite_hash)?.slice(0, 12) ?? null],
      ]}
    />
  );
}

function EvalAggregatesLine({ data }: { data: Record<string, unknown> }) {
  const rate = typeof data.pass_rate === "number" && Number.isFinite(data.pass_rate) ? `${Math.round(data.pass_rate * 1000) / 10}%` : null;
  const scores = data.scores_avg && typeof data.scores_avg === "object" ? Object.entries(data.scores_avg as Record<string, unknown>) : [];
  return (
    <Facts
      testId="event-eval-aggregates"
      title="Eval run completed"
      facts={[
        ["cases", factNumber(data.cases)],
        ["passed", factNumber(data.passed)],
        ["failed", factNumber(data.failed)],
        ["errors", factNumber(data.errors)],
        ["pass rate", rate],
        ["p50 duration", typeof data.duration_ms_p50 === "number" ? `${factNumber(data.duration_ms_p50)} ms` : null],
        ["cost", factCost(data.cost_usd)],
        ["avg scores", scores.length ? scores.map(([k, v]) => `${k} ${typeof v === "number" ? Math.round(v * 100) / 100 : "—"}`).join(", ") : null],
      ]}
    />
  );
}

function EventItem({ event }: EventItemProps) {
  const renderContent = () => {
    switch (event.type) {
      case "run_started":
        if (event.data.kind === "eval") return <EvalStartLine data={event.data} />;
        return (
          <div className="text-sm text-card-foreground/70">
            Run started
            {typeof event.data.tool_id === "string" && (
              <span className="block mt-1 text-xs font-mono text-muted-foreground">
                Tool: {event.data.tool_id}
              </span>
            )}
            {typeof event.data.input === "string"
              ? event.data.input && (
                  <span className="block mt-1 text-xs font-mono text-muted-foreground">
                    Input: {event.data.input}
                  </span>
                )
              : event.data.input !== undefined &&
                event.data.input !== null && (
                  <div className="mt-1 text-xs text-muted-foreground">
                    Input:
                    <JsonBlock value={event.data.input} label="Run input" />
                  </div>
                )}
          </div>
        );
      case "model_message":
        return (
          <div className="text-sm text-card-foreground">
            <span className="font-semibold">Model response:</span>
            {event.data.output_candidate === true ? (
              <span data-testid="event-output-candidate" className="ml-2 rounded-full bg-info/10 px-2 py-0.5 text-xs text-info-text">
                output candidate
              </span>
            ) : null}
            <div className="mt-1">
              {event.data.output_candidate === true && event.data.text ? (
                // Native or prompted mode: the candidate is raw JSON text, shown as such.
                <JsonBlock value={event.data.text} label="Output candidate" />
              ) : (
                <MarkdownContent text={event.data.text ?? ""} />
              )}
            </div>
            <FallbackFacts event={event} />
          </div>
        );
      case "tool_call":
        return (
          <div className="text-sm">
            <span className="font-semibold text-primary">Tool call:</span>
            <span className="ml-1 font-mono text-muted-foreground">
              {formatValue(event.data.tool_id ?? event.data.tool_name)}
            </span>
            <JsonBlock value={event.data.args ?? {}} label="Tool arguments" />
          </div>
        );
      case "tool_result":
        return (
          <div className="text-sm">
            {event.data.status === "error" ? (
              <>
                <span className="font-semibold text-destructive-text">Tool error:</span>
                {event.data.content !== undefined ? (
                  <JsonBlock value={event.data.content} label="Tool result" />
                ) : (
                  <span className="block mt-1 text-destructive-text">
                    {formatValue(event.data.error ?? "Unknown error")}
                  </span>
                )}
              </>
            ) : event.data.status === "denied" ? (
              <>
                <span className="font-semibold text-warning-text">Tool denied:</span>
                <JsonBlock value={event.data.content ?? null} label="Tool result" />
              </>
            ) : (
              <>
                <span className="font-semibold text-success">Tool result:</span>
                <JsonBlock value={event.data.content ?? null} label="Tool result" />
              </>
            )}
          </div>
        );
      case "run_completed":
        if (event.data.aggregates && typeof event.data.aggregates === "object") {
          return <EvalAggregatesLine data={event.data.aggregates as Record<string, unknown>} />;
        }
        return (
          <div className="text-sm">
            {event.data.outcome === "skipped" ? (
              <span className="font-semibold text-muted-foreground">Run skipped (nothing to do)</span>
            ) : (
              <span className="text-success font-semibold">Run completed successfully</span>
            )}
            {/* A tool run's end event carries the tool result (any JSON value) */}
            {event.data.result !== undefined && <JsonBlock value={event.data.result} label="Run result" />}
            {/* AC.4: a structured agent's validated object. */}
            {event.data.output_json !== undefined && (
              <StructuredOutputView value={event.data.output_json} label="Output JSON" testId="event-output-json" />
            )}
          </div>
        );
      case "run_failed":
        return (
          <div className="text-sm text-destructive-text font-semibold">
            {event.data.status === "needs_attention" ? "Needs attention" : "Run failed"}
            <span className="block mt-1 text-destructive-text">
              {turnCapabilityFailure([event])?.message ?? formatValue(event.data.error || "Unknown error")}
            </span>
            {turnCapabilityFailure([event]) !== null ? (
              <span data-testid="event-error-code" className="block mt-1 text-xs font-normal font-mono text-muted-foreground">
                {String(event.data.error_code)}
              </span>
            ) : null}
          </div>
        );
      case "eval_case_result":
        return <EvalCaseLine data={event.data} />;
      case "output_retry":
        return (
          <div className="text-sm" data-testid="event-output-retry">
            <span className="font-semibold text-warning-text">Output retry</span>
            <span className="mt-1 block break-words text-xs text-muted-foreground">{outputRetryText(outputRetryOf(event.data))}</span>
          </div>
        );
      case "tool_approval_waiting":
        return (
          <div className="text-sm" data-testid="event-tool-approval-waiting">
            <span className="font-semibold text-warning-text">Waiting for approval:</span>
            <span className="ml-1 font-mono text-muted-foreground">{formatValue(event.data.tool_name ?? "tool")}</span>
            <span className="mt-1 block text-xs text-muted-foreground">
              Deadline: {factText(event.data.deadline) ? new Date(String(event.data.deadline)).toLocaleString() : "—"}
            </span>
            <JsonBlock value={event.data.args ?? {}} label="Tool arguments" />
          </div>
        );
      case "tool_approved":
      case "tool_denied":
      case "tool_approval_timed_out":
        return <ToolDecisionLine event={event} />;
      case "agent_tool_started":
      case "agent_tool_completed":
        return <AgentToolLine event={event} />;
      case "processor_result":
        return <ProcessorLine data={event.data} />;
      case "embedding":
        return <EmbeddingLine data={event.data} />;
      case "ingest_progress":
        return <IngestLine data={event.data} />;
      case "memory_updated":
        return <MemoryLine data={event.data} failed={false} />;
      case "memory_observe_failed":
        return <MemoryLine data={event.data} failed />;
      default:
        return <JsonBlock value={event.data} />;
    }
  };

  return (
    <div className="flex gap-3">
      <div className="flex-shrink-0 w-12 pt-1">
        <div className="text-xs text-muted-foreground font-mono">
          seq {event.seq}
        </div>
        <div className="text-xs text-muted-foreground/70 mt-0.5">
          {new Date(event.ts).toLocaleTimeString()}
        </div>
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-xs font-semibold text-card-foreground/60 mb-1 uppercase tracking-wide">
          {event.type}
        </div>
        {renderContent()}
        {/* A workflow step's child run: its trace stacks over this one where the trace Sheet is hosted. */}
        {typeof event.data.child_run_id === "string" && event.data.child_run_id ? (
          <TraceLink
            runId={event.data.child_run_id}
            data-testid="event-child-run-link"
            className="mt-1 inline-block text-xs text-primary underline-offset-4 hover:underline"
          >
            Open child run trace
          </TraceLink>
        ) : null}
      </div>
    </div>
  );
}
