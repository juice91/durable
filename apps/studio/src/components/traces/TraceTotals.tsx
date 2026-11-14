import React from "react";
import { formatCost, formatTokens } from "@/lib/format";
import { formatDuration, usageSplit, type TraceTotals as Totals, type UsageGroup } from "@/lib/traces";

function Stat({ label, value, hint, testId }: { label: string; value: string; hint?: string; testId: string }) {
  return (
    <div className="rounded-md border border-border bg-card px-3 py-2" data-testid={testId}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="font-mono text-base font-semibold text-card-foreground">{value}</p>
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

// Literal ids (the test-id inventory finds them in the source).
const TOTALS_GROUP_TEST_ID = {
  memory: { testId: "totals-usage-memory" },
  moderation: { testId: "totals-usage-moderation" },
  embedding: { testId: "totals-usage-embedding" },
} as const;

function groupLine(group: UsageGroup): string {
  const show = (v: number | null) => (v === null ? "not recorded" : formatTokens(v));
  const tokens = group.outputTokens === null && group.id === "embedding" ? `${show(group.inputTokens)} input tokens` : `${show(group.inputTokens)} in, ${show(group.outputTokens)} out`;
  return group.costUsd === undefined ? tokens : `${tokens}, ${formatCost(group.costUsd) ?? "no price"}`;
}

/** Memory, moderation and embedding usage, each on its own line (never added to the chat model's). */
function OtherUsage({ groups }: { groups: UsageGroup[] }) {
  return (
    <div className="col-span-full rounded-md border border-border bg-card px-3 py-2" data-testid="totals-other-usage">
      <p className="text-xs text-muted-foreground">Other model usage</p>
      <dl className="mt-1 grid gap-x-4 gap-y-0.5 text-sm sm:grid-cols-[auto_1fr]">
        {groups.map((group) => (
          <div key={group.id} className="contents" data-testid={TOTALS_GROUP_TEST_ID[group.id].testId}>
            <dt className="text-muted-foreground">{group.label}</dt>
            <dd className="font-mono text-card-foreground">{groupLine(group)}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** Trace totals: duration, counts, tokens and cost (with "no price" / "not recorded" spelled out). */
export function TraceTotals({ totals }: { totals: Totals }) {
  const cost = formatCost(totals.cost_usd);
  let costValue = cost ?? "-";
  let costHint: string | undefined;
  if (cost === null && totals.model_requests > 0) {
    if (totals.unrecorded_requests === totals.model_requests) {
      costValue = "not recorded";
    } else if (totals.unpriced_requests > 0 || totals.unrecorded_requests > 0) {
      costValue = "no price";
      const parts = [];
      if (totals.unpriced_requests) parts.push(`${totals.unpriced_requests} unpriced`);
      if (totals.unrecorded_requests) parts.push(`${totals.unrecorded_requests} not recorded`);
      costHint = `${parts.join(", ")} of ${totals.model_requests} requests`;
    }
  }
  // RM.19: embedding and observer / reflector requests are among the model requests; say how many,
  // and what the embeddings cost, only when there are any (older traces render as before).
  const embeddings = totals.embedding_requests ?? 0;
  const memory = totals.memory_requests ?? 0;
  const requestExtras = [
    embeddings ? `${embeddings} embedding` : null,
    memory ? `${memory} observer or reflector` : null,
  ].filter(Boolean).join(", ");
  const embeddingCost = embeddings ? formatCost(totals.embedding_cost_usd ?? null) : null;
  // The split by operation (absent from older APIs): the token cards then show the chat model's
  // tokens, and memory, moderation and embedding usage get their own lines below.
  const split = usageSplit(totals as unknown as Record<string, unknown>);
  if (embeddings && !split) {
    const embeddingHint = `embeddings ${embeddingCost ?? "no price"}, ${formatTokens(totals.embedding_input_tokens ?? 0)} tokens`;
    costHint = costHint ? `${costHint}; ${embeddingHint}` : embeddingHint;
  }
  const tokens = (v: number | null) => (v === null ? (totals.model_requests > 0 ? "not recorded" : "-") : formatTokens(v));
  const cache = (totals.cache_read_tokens ?? 0) + (totals.cache_write_tokens ?? 0);
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6" aria-label="Trace totals" role="group">
      <Stat testId="totals-duration" label="Duration" value={formatDuration(totals.duration_ms)} />
      <Stat
        testId="totals-requests"
        label="Model requests"
        value={formatTokens(totals.model_requests)}
        hint={`${totals.tool_calls} tool call${totals.tool_calls === 1 ? "" : "s"}, ${totals.runs} run${totals.runs === 1 ? "" : "s"}${requestExtras ? `; ${requestExtras}` : ""}`}
      />
      {split ? (
        <>
          <Stat
            testId="totals-input-tokens"
            label="Chat input tokens"
            value={tokens(split.chat.inputTokens)}
            hint={split.others.length ? `${tokens(totals.input_tokens)} with every request` : undefined}
          />
          <Stat
            testId="totals-output-tokens"
            label="Chat output tokens"
            value={tokens(split.chat.outputTokens)}
            hint={split.others.length ? `${tokens(totals.output_tokens)} with every request` : undefined}
          />
        </>
      ) : (
        <>
          <Stat testId="totals-input-tokens" label="Input tokens" value={tokens(totals.input_tokens)} />
          <Stat testId="totals-output-tokens" label="Output tokens" value={tokens(totals.output_tokens)} />
        </>
      )}
      <Stat
        testId="totals-cache-tokens"
        label="Cache tokens"
        value={totals.cache_read_tokens === null && totals.cache_write_tokens === null ? "-" : formatTokens(cache)}
        hint={totals.cache_read_tokens !== null ? `${formatTokens(totals.cache_read_tokens)} read, ${formatTokens(totals.cache_write_tokens)} write` : undefined}
      />
      <Stat
        testId="totals-cost"
        label="Cost"
        value={costValue}
        hint={split && split.others.length ? [`chat ${formatCost(split.chat.costUsd ?? null) ?? "no price"}`, costHint].filter(Boolean).join("; ") : costHint}
      />
      {split && split.others.length ? <OtherUsage groups={split.others} /> : null}
    </div>
  );
}
