"use client";

import React from "react";
import Link from "next/link";
import { formatTokens } from "@/lib/format";
import {
  BREAKDOWN_OPERATIONS,
  formatCostWithPricing,
  formatCount,
  metricsOperationHref,
  operationLabel,
  type MetricsPageState,
  type MetricsSummary,
} from "@/lib/metrics";

export interface UsageByOperationProps {
  summary: MetricsSummary | null;
  state: MetricsPageState;
  headingLevel?: 2 | 3;
}

/**
 * RM.19: requests, tokens and cost per operation (chat, embedding, observer, reflector,
 * moderation and, AC.17, judge; they sum to the KPI totals) from the summary's `operations` split, so it always
 * matches the Overview. A row's name narrows the Metrics page to that operation. Nothing when the
 * summary has no split (an API before RM.19).
 */
export function UsageByOperation({ summary, state, headingLevel = 2 }: UsageByOperationProps) {
  const ops = summary?.current.operations;
  if (!ops) return null;
  const Heading = headingLevel === 3 ? "h3" : "h2";
  const rows = BREAKDOWN_OPERATIONS.map((op) => ({ op, values: ops[op] })).filter(
    (r): r is { op: (typeof BREAKDOWN_OPERATIONS)[number]; values: NonNullable<typeof r.values> } => !!r.values,
  );
  const th = "px-3 py-2 font-medium text-muted-foreground whitespace-nowrap";
  const td = "px-3 py-2 text-right whitespace-nowrap";
  return (
    <section
      className="min-w-0 rounded-lg border border-border bg-card text-card-foreground"
      aria-labelledby="usage-by-operation-title"
      data-testid="usage-by-operation"
    >
      <div className="px-4 pt-4 pb-2">
        <Heading id="usage-by-operation-title" className="text-sm font-semibold text-foreground">Usage by operation</Heading>
        <p className="mt-1 text-xs text-muted-foreground">
          Embedding covers knowledge base ingest, retrieval, semantic recall and indexing; observer and reflector are
          observational memory; judge is the LLM judge of test suite runs. Select an operation to narrow these metrics
          to it.
        </p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm tabular-nums">
          <thead className="bg-muted/50">
            <tr>
              <th scope="col" className={`${th} text-left`}>Operation</th>
              <th scope="col" className={`${th} text-right`}>Requests</th>
              <th scope="col" className={`${th} text-right`}>Input tokens</th>
              <th scope="col" className={`${th} text-right`}>Output tokens</th>
              <th scope="col" className={`${th} text-right`}>Cost</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {rows.map(({ op, values }) => {
              const name = operationLabel(op);
              return (
                <tr key={op} data-testid="usage-operation-row" data-operation={op}>
                  <th scope="row" className="px-3 py-2 text-left font-medium">
                    {state.operation === op ? (
                      <span className="text-foreground">{name}</span>
                    ) : (
                      <Link
                        href={metricsOperationHref(state, op)}
                        className="text-foreground hover:underline"
                        aria-label={`${name}: show this operation's metrics`}
                      >
                        {name}
                      </Link>
                    )}
                  </th>
                  <td className={td}>{formatCount(values.requests)}</td>
                  <td className={td}>{formatTokens(values.input_tokens)}</td>
                  <td className={td}>{formatTokens(values.output_tokens)}</td>
                  <td className={td}>{formatCostWithPricing(values)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
