"use client";

import React from "react";
import { ScrollText } from "lucide-react";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { useFrame } from "@/components/frame/FrameContext";
import { useViewportWidth } from "@/components/frame/useMediaQuery";
import { TraceLink } from "@/components/traces/TraceLink";
import { logSheetWidth } from "@/components/traces/traceSheetModel";
import { LogLevelBadge } from "./LogLevelBadge";
import { formatLogTime, type LogItem } from "./logsQuery";

function Field({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="grid min-w-0 grid-cols-[7rem_minmax(0,1fr)] gap-2 py-1">
      <dt className="text-caption text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-all font-mono text-caption text-foreground" data-testid={testId}>{children}</dd>
    </div>
  );
}

/**
 * One log record in a Sheet (SL.12): time, level, service, run, trace and span ids, the whole
 * message and the raw record. "Open trace" stacks the run's trace Sheet over this one (on a page
 * hosting it); Escape then closes the trace, and again this Sheet. No URL state: log records have
 * no stable id.
 */
export function LogSheet({ item, onClose }: { item: LogItem | null; onClose: () => void }) {
  const frameState = useFrame();
  const viewport = useViewportWidth(item !== null);
  const width = logSheetWidth({ viewport, sidebarCollapsed: frameState?.sidebarCollapsed ?? false });
  const when = item ? formatLogTime(item.ts) : null;
  return (
    <Sheet
      open={item !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      {item && when ? (
        <SheetContent size="md" style={width === null ? undefined : { width }} data-testid="log-sheet">
          <SheetHeader title="Log record" description={`${when.date} ${when.time}`} />
          <SheetBody className="flex flex-col gap-4">
            <div className="flex flex-wrap items-center gap-2">
              <LogLevelBadge level={item.level} />
              {item.service ? <span className="text-label text-muted-foreground">{item.service}</span> : null}
              {item.run_id ? (
                <TraceLink
                  runId={item.run_id}
                  data-testid="log-sheet-open-trace"
                  className="ml-auto inline-flex h-7 items-center gap-1.5 rounded-md border border-border px-2 text-label font-medium text-foreground hover:bg-fill-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <ScrollText className="size-icon-dense" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                  Open trace
                </TraceLink>
              ) : (
                <span className="ml-auto text-caption text-muted-foreground">Not logged inside a run</span>
              )}
            </div>

            <section aria-label="Message">
              <h3 className="mb-1 text-caption font-medium text-muted-foreground">Message</h3>
              <pre data-testid="log-sheet-message" className="max-h-[50vh] overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 font-mono text-caption text-foreground [overflow-wrap:anywhere]">
                {item.message}
              </pre>
            </section>

            <section aria-label="Attributes">
              <h3 className="mb-1 text-caption font-medium text-muted-foreground">Attributes</h3>
              <dl className="divide-y divide-border">
                <Field label="Time">
                  <time dateTime={item.ts}>{item.ts}</time>
                </Field>
                <Field label="Level">{item.level ?? "-"}</Field>
                <Field label="Service">{item.service ?? "-"}</Field>
                <Field label="Run" testId="log-sheet-run">{item.run_id ?? "-"}</Field>
                <Field label="Trace id">{item.trace_id ?? "-"}</Field>
                <Field label="Span id">{item.span_id ?? "-"}</Field>
              </dl>
            </section>

            <details>
              <summary className="cursor-pointer text-caption text-muted-foreground hover:text-foreground">Raw record</summary>
              <pre className="mt-2 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 font-mono text-caption text-foreground [overflow-wrap:anywhere]">
                {JSON.stringify(item, null, 2)}
              </pre>
            </details>
          </SheetBody>
        </SheetContent>
      ) : null}
    </Sheet>
  );
}
