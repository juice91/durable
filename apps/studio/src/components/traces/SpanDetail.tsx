"use client";

import React from "react";
import { formatCost, formatTokens } from "@/lib/format";
import { buildTraceLink } from "@/lib/grafana";
import {
  KIND_LABELS,
  formatDuration,
  spanLabel,
  usageSplit,
  type NameMaps,
  type SpanUsage,
  type TraceSpan,
} from "@/lib/traces";
import { PayloadBlock, payloadText } from "./PayloadBlock";
import { STATUS_LABELS, STATUS_TEXT } from "./spanStyles";
import type { TraceTab } from "@/lib/routes/observability";
import { TraceLink } from "./TraceLink";
import { KnowledgeBaseLink, RAG_SHOWN, RagSpanSection, USAGE_KINDS } from "./RagSpanSections";
import { CAPABILITY_SHOWN, CAPABILITY_USAGE_KINDS, CapabilitySpanSection } from "./CapabilitySpanSections";
import { LOOP_SHOWN, LoopSpanSection } from "./LoopSpanSections";

// Attributes shown in their own sections rather than in the generic attribute list.
const SHOWN = new Set([
  "usage", "model_id", "provider_model_name", "cost_usd", "price_source", "error", "error_code",
  "input_tokens", "output_tokens", "approval", "decision",
  // The run span's usage split (shown in Run usage).
  "chat_requests", "chat_input_tokens", "chat_output_tokens", "memory_input_tokens", "memory_output_tokens",
  "moderation_input_tokens", "moderation_output_tokens", "embedding_input_tokens",
  // AC.17: an eval run's judge usage (shown in Run usage).
  "judge_requests", "judge_input_tokens", "judge_output_tokens",
]);

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function Row({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="flex justify-between gap-4 py-0.5" data-testid={testId}>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="text-right font-mono text-card-foreground break-all">{children}</dd>
    </div>
  );
}

function UsageSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const usage = (a.usage ?? null) as SpanUsage | null;
  const cost = formatCost(a.cost_usd ?? null);
  return (
    <section aria-label="Model and usage" data-testid="span-usage">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Model and usage</h4>
      <dl className="mt-1 text-sm">
        <Row label="Model">{str(a.model_id) ?? "-"}</Row>
        {str(a.provider_model_name) ? <Row label="Provider model">{a.provider_model_name as string}</Row> : null}
        {usage ? (
          <>
            <Row label="Input tokens" testId="span-input-tokens">{formatTokens(usage.input_tokens)}</Row>
            <Row label="Output tokens" testId="span-output-tokens">{formatTokens(usage.output_tokens)}</Row>
            <Row label="Cache read tokens">{formatTokens(usage.cache_read_tokens)}</Row>
            <Row label="Cache write tokens">{formatTokens(usage.cache_write_tokens)}</Row>
            {!usage.usage_measured ? (
              <p className="text-xs text-muted-foreground">The provider reported no usage; token counts are estimates.</p>
            ) : null}
          </>
        ) : (
          <Row label="Tokens" testId="span-input-tokens">not recorded</Row>
        )}
        <Row label="Cost" testId="span-cost">
          {cost ?? (usage ? "no price" : "not recorded")}
        </Row>
        {str(a.price_source) ? <Row label="Price source">{a.price_source as string}</Row> : null}
      </dl>
    </section>
  );
}

// Literal ids (the test-id inventory finds them in the source).
const SPAN_GROUP_TEST_ID = {
  memory: { testId: "span-usage-memory" },
  moderation: { testId: "span-usage-moderation" },
  embedding: { testId: "span-usage-embedding" },
  judge: { testId: "span-usage-judge" },
} as const;

function RunUsageSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const tokensIn = typeof a.input_tokens === "number" ? a.input_tokens : null;
  const tokensOut = typeof a.output_tokens === "number" ? a.output_tokens : null;
  const cost = formatCost(str(a.cost_usd));
  if (!str(a.model_id) && tokensIn === null && tokensOut === null && cost === null) return null;
  const split = usageSplit(a);
  const show = (v: number | null) => (v === null ? "not recorded" : formatTokens(v));
  return (
    <section aria-label="Run usage" data-testid="span-usage">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Run usage</h4>
      <dl className="mt-1 text-sm">
        {str(a.model_id) ? <Row label="Model">{a.model_id as string}</Row> : null}
        {split ? (
          <>
            {/* Next to the chat model: its own tokens only (older responses: the combined count). */}
            <Row label="Input tokens" testId="span-input-tokens">{show(split.chat.inputTokens)}</Row>
            <Row label="Output tokens" testId="span-output-tokens">{show(split.chat.outputTokens)}</Row>
            {split.others.map((group) => (
              <Row key={group.id} label={`${group.label} tokens`} testId={SPAN_GROUP_TEST_ID[group.id].testId}>
                {group.id === "embedding" ? show(group.inputTokens) : `${show(group.inputTokens)} in, ${show(group.outputTokens)} out`}
              </Row>
            ))}
          </>
        ) : (
          <>
            <Row label="Input tokens" testId="span-input-tokens">{show(tokensIn)}</Row>
            <Row label="Output tokens" testId="span-output-tokens">{show(tokensOut)}</Row>
          </>
        )}
        <Row label="Cost" testId="span-cost">{cost ?? (tokensIn === null ? "not recorded" : "no price")}</Row>
      </dl>
    </section>
  );
}

function Links({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  // In-Studio links are TraceLinks: on a page hosting the trace Sheet a child run stacks a second
  // Sheet, and "Run details" opens the Events tab; elsewhere they go to `/traces?trace=`.
  const links: Array<{ runId?: string; tab?: TraceTab; href?: string; label: string; testId: string }> = [];
  const runId = str(a.run_id);
  if ((span.kind === "run" || span.kind === "child_run") && runId) {
    if (span.kind === "child_run") links.push({ runId, label: "Open child run trace", testId: "span-link-child-trace" });
    links.push({ runId, tab: "events", label: "Run details", testId: "span-link-run" });
  }
  const childRun = str(a.child_run_id);
  if (span.kind === "workflow_step" && childRun) {
    links.push({ runId: childRun, label: "Open child run trace", testId: "span-link-child-trace" });
  }
  const traceId = str(a.trace_id);
  if (traceId) links.push({ href: buildTraceLink(traceId), label: "Open in Grafana", testId: "span-link-grafana" });
  if (!links.length) return null;
  const linkClass = "text-primary underline-offset-4 hover:underline";
  return (
    <div className="flex flex-wrap gap-3 text-sm">
      {links.map((l) =>
        l.runId ? (
          <TraceLink key={l.testId} runId={l.runId} tab={l.tab ?? null} data-testid={l.testId} className={linkClass}>
            {l.label}
          </TraceLink>
        ) : (
          <a key={l.testId} href={l.href} data-testid={l.testId} target="_blank" rel="noopener noreferrer" className={linkClass}>
            {l.label}
          </a>
        ),
      )}
    </div>
  );
}

function attrValue(value: unknown): string {
  if (value === null || value === undefined) return "-";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return payloadText(value);
}

interface SpanDetailProps {
  span: TraceSpan | null;
  names?: NameMaps;
}

/** The selected span: status, timing, error, model usage and cost, links, input, output and the
 * remaining attributes. */
export function SpanDetail({ span, names }: SpanDetailProps) {
  if (!span) {
    return (
      <div className="rounded-md border border-border bg-card p-4 text-sm text-muted-foreground">
        Select a span to see its details.
      </div>
    );
  }
  const a = span.attributes ?? {};
  const durationMs = span.start && span.end ? Math.max(0, Date.parse(span.end) - Date.parse(span.start)) : null;
  const error = str(a.error);
  const errorCode = str(a.error_code);
  const approval = a.approval as Record<string, unknown> | undefined;
  const decision = a.decision as Record<string, unknown> | undefined;
  const ragShown = new Set([
    ...(RAG_SHOWN[span.kind] ?? []), ...(CAPABILITY_SHOWN[span.kind] ?? []), ...(LOOP_SHOWN[span.kind] ?? []),
  ]);
  const others = Object.entries(a).filter(([k, v]) => !SHOWN.has(k) && !ragShown.has(k) && v !== null && v !== undefined);
  // A model request always shows its usage; the RAG and memory kinds and processors when they carry a model.
  // A moderation processor is a model call of its own (its model, not the agent's chat model).
  const showUsage =
    span.kind === "model_request" ||
    ((USAGE_KINDS.has(span.kind) || CAPABILITY_USAGE_KINDS.has(span.kind) || span.kind === "processor") &&
      (str(a.model_id) !== null || a.usage != null));

  return (
    <aside aria-label="Span detail" data-testid="span-detail" className="space-y-4 rounded-md border border-border bg-card p-4">
      <header>
        <p className="text-xs uppercase tracking-wide text-muted-foreground">{KIND_LABELS[span.kind]}</p>
        <h3 className="mt-0.5 break-words text-base font-semibold text-card-foreground">{spanLabel(span, names)}</h3>
        <p className={`mt-1 text-sm font-medium ${STATUS_TEXT[span.status]}`} data-testid="span-status">
          {STATUS_LABELS[span.status]}
          {typeof a.run_status === "string" && a.run_status !== span.status ? ` (run ${a.run_status.replace("_", " ")})` : ""}
        </p>
      </header>

      <dl className="text-sm">
        <Row label="Start">{span.start ? new Date(span.start).toLocaleString() : "-"}</Row>
        <Row label="End">{span.end ? new Date(span.end).toLocaleString() : !span.end ? "not finished" : "-"}</Row>
        <Row label="Duration" testId="span-duration">{durationMs === null ? "-" : formatDuration(durationMs)}</Row>
      </dl>

      {error || errorCode ? (
        <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-sm text-card-foreground" data-testid="span-error">
          {errorCode ? <p className="font-mono text-xs">{errorCode}</p> : null}
          {error ? <p>{error}</p> : null}
        </div>
      ) : null}

      {showUsage ? <UsageSection span={span} /> : null}
      <CapabilitySpanSection span={span} />
      <LoopSpanSection span={span} />
      <RagSpanSection span={span} names={names} />
      {span.kind === "run" || span.kind === "child_run" ? <RunUsageSection span={span} /> : null}

      {approval ? (
        <section aria-label="Approval">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Approval</h4>
          <dl className="mt-1 text-sm">
            {str(approval.prompt) ? <Row label="Prompt">{approval.prompt as string}</Row> : null}
            {str(approval.deadline) ? <Row label="Deadline">{approval.deadline as string}</Row> : null}
            {decision ? (
              <>
                <Row label="Decision">{String(decision.type ?? "-").replace("step_", "")}</Row>
                {str(decision.decided_by) ? <Row label="Decided by">{decision.decided_by as string}</Row> : null}
                {str(decision.comment) ? <Row label="Comment">{decision.comment as string}</Row> : null}
              </>
            ) : (
              <Row label="Decision">pending</Row>
            )}
          </dl>
        </section>
      ) : null}

      <Links span={span} />
      <KnowledgeBaseLink span={span} names={names} />

      <PayloadBlock label="Input" value={span.input} truncatedBytes={span.truncated?.input} />
      <PayloadBlock label="Output" value={span.output} truncatedBytes={span.truncated?.output} />

      {others.length ? (
        <section aria-label="Attributes">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Attributes</h4>
          <dl className="mt-1 text-sm" data-testid="span-attributes">
            {others.map(([k, v]) => (
              <Row key={k} label={k}>{attrValue(v)}</Row>
            ))}
          </dl>
        </section>
      ) : null}
    </aside>
  );
}
