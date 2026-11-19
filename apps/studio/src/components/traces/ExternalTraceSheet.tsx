"use client";

import React, { useEffect, useState } from "react";
import { getExternalTrace, externalSpanRows, RUN_ID_ATTRIBUTE, type ExternalTraceDetail } from "@/lib/externalTraces";
import { formatDuration } from "@/lib/traces";
import { Badge } from "@/components/ui/Badge";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { TraceLink } from "./TraceLink";

const dim = "text-muted-foreground";

function Attributes({ title, attributes, testId }: { title: string; attributes: Record<string, unknown>; testId: string }) {
  const entries = Object.entries(attributes);
  return (
    <section className="flex min-w-0 flex-col gap-1" data-testid={testId}>
      <h3 className="text-label font-semibold text-foreground">{title}</h3>
      {entries.length === 0 ? (
        <p className={`text-label ${dim}`}>None.</p>
      ) : (
        <dl className="grid min-w-0 grid-cols-[minmax(8rem,auto)_1fr] gap-x-3 gap-y-1 text-caption">
          {entries.map(([key, value]) => (
            <React.Fragment key={key}>
              <dt className={`break-all font-mono ${dim}`}>{key}</dt>
              <dd className="min-w-0 break-all font-mono text-foreground">{typeof value === "string" ? value : JSON.stringify(value)}</dd>
            </React.Fragment>
          ))}
        </dl>
      )}
    </section>
  );
}

/**
 * One external OpenTelemetry trace (sent with an `otlp` key): its resource attributes, a link to the
 * platform run a span names with `pydanticos.run_id`, and the span tree with each span's attributes.
 * Values were redacted when the spans were stored.
 */
export function ExternalTraceSheet({ traceId, onClose }: { traceId: string; onClose: () => void }) {
  const [trace, setTrace] = useState<ExternalTraceDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setTrace(null);
    setError(null);
    getExternalTrace(traceId, controller.signal)
      .then(setTrace)
      .catch((err) => {
        if (!controller.signal.aborted) setError(err instanceof Error && err.message ? err.message : "Failed to load the trace");
      });
    return () => controller.abort();
  }, [traceId]);
  const rows = trace ? externalSpanRows(trace.spans) : [];
  return (
    <Sheet open onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent size="wide" data-testid="external-trace-sheet">
        <SheetHeader
          title={trace?.service_name ? `${trace.service_name} trace` : "External trace"}
          description={<span className="font-mono" data-testid="external-trace-id">{traceId}</span>}
        />
        <SheetBody className="flex flex-col gap-4">
          {error ? <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive-text">{error}</div> : null}
          {!trace && !error ? <p className={dim}>Loading trace...</p> : null}
          {trace ? (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant="info" data-testid="external-trace-service">{trace.service_name ?? "unknown service"}</Badge>
                <Badge variant="neutral">External</Badge>
                {trace.run_id ? (
                  <TraceLink runId={trace.run_id} data-testid="external-trace-run-link" className="text-label text-info-text underline-offset-4 hover:underline">
                    Platform run {trace.run_id.slice(0, 8)}
                  </TraceLink>
                ) : null}
              </div>
              <Attributes title="Resource attributes" attributes={trace.resource_attributes} testId="external-trace-resource" />
              <section className="flex min-w-0 flex-col gap-1" data-testid="external-trace-spans">
                <h3 className="text-label font-semibold text-foreground">Spans ({trace.spans.length}{trace.truncated ? "+" : ""})</h3>
                {rows.map(({ span, depth }) => {
                  const ms = span.end ? Math.max(0, new Date(span.end).getTime() - new Date(span.start).getTime()) : null;
                  const linked = typeof span.attributes[RUN_ID_ATTRIBUTE] === "string";
                  return (
                    <details key={span.span_id} data-testid="external-span" className="rounded-md border border-border px-2 py-1" style={{ marginLeft: depth * 16 }}>
                      <summary className="flex cursor-pointer flex-wrap items-center gap-2 text-label">
                        <span className="font-medium">{span.name}</span>
                        <Badge variant={span.status === "error" ? "destructive" : span.status === "ok" ? "success" : "neutral"}>{span.status}</Badge>
                        <span className={dim}>{formatDuration(ms)}</span>
                        {linked ? <span className={`font-mono text-caption ${dim}`}>{RUN_ID_ATTRIBUTE}</span> : null}
                      </summary>
                      <div className="pt-2">
                        <Attributes title="Attributes" attributes={span.attributes} testId="external-span-attributes" />
                      </div>
                    </details>
                  );
                })}
              </section>
            </>
          ) : null}
        </SheetBody>
      </SheetContent>
    </Sheet>
  );
}
