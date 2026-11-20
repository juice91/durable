"use client";

import Link from "next/link";
import React, { useMemo, useState } from "react";
import { ChevronDown, ChevronUp, Copy, Download, Gauge, Maximize2, Minimize2 } from "lucide-react";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { buildLogsLink, buildTraceLink } from "@/lib/grafana";
import { routes } from "@/lib/routes";
import { TRACE_TABS, type TraceTab } from "@/lib/routes/observability";
import { spanLabel, type TraceRun } from "@/lib/traces";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/Tabs";
import { toast } from "@/components/ui/Toaster";
import { useFrame } from "@/components/frame/FrameContext";
import { useViewportWidth } from "@/components/frame/useMediaQuery";
import { RunEventsPanel } from "@/components/runs/RunEventsPanel";
import { useKnowledgeBaseNames } from "@/components/knowledge/useKnowledgeBaseNames";
import { runKind } from "@/components/runs/RunSubject";
import type { RunKind } from "@/lib/runs";
import { RecoveredBadge } from "@/components/runs/RecoveredBadge";
import { StatusPill } from "@/components/runs/StatusPill";
import { TraceLogsPanel } from "./TraceLogsPanel";
import { TraceSheetFrameContext } from "./TraceLink";
import { TraceSpans } from "./TraceSpans";
import { TraceFeedback } from "./TraceFeedback";
import { TraceScores, type PendingScores } from "./TraceScores";
import { ScoreTraceDialog } from "./ScoreTraceDialog";
import { downloadJson } from "@/lib/download";
import { AddToDataset } from "./AddToDataset";
import type { SpanTreeMode } from "./TraceWaterfall";
import { renderedSheetWidth, spanDetailBeside, traceSheetWidth } from "./traceSheetModel";
import { useNames, useTrace } from "./useTrace";
import { copyText } from "@/lib/clipboard";

const TAB_LABELS: Record<TraceTab, string> = { spans: "Spans", events: "Events", scores: "Scores", logs: "Logs" };
const KIND_LABELS: Record<RunKind, string> = { agent: "Agent run", tool: "Tool run", workflow: "Workflow run", kb_ingest: "Knowledge ingest run", eval: "Eval run", kb_source_sync: "KB source sync run" };
const icon = (Icon: typeof Copy) => <Icon strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />;

export interface TraceSheetNavigation {
  prevId: string | null;
  nextId: string | null;
  /** 1-based position in the hosting list. */
  position: number;
  total: number;
  onNavigate: (runId: string) => void;
}

export interface TraceSheetProps {
  runId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  tab: TraceTab;
  onTabChange: (tab: TraceTab) => void;
  /** 0: the Sheet from the URL; 1: a child run stacked on top. */
  depth?: 0 | 1;
  /** Prev / next within the hosting list (only when the run is in it). */
  navigation?: TraceSheetNavigation | null;
  /**
   * Where focus goes when this Sheet closes, given the run it shows then; null keeps the default
   * (whatever opened it). The host returns the list row of a run reached with prev / next.
   */
  closeFocusTarget?: (runId: string) => HTMLElement | null;
  /** The span to select on open (`?span=`, from the Spans list). */
  initialSpanId?: string | null;
}

/** The "Open agent" / "Open workflow" / "Open knowledge base" link for a run. A kb_ingest run links
 * to its knowledge base's Ingest runs tab, named when `knowledgeBaseNames` has it (RM.19). */
export function subjectLink(
  run: TraceRun,
  knowledgeBaseNames: Record<string, string> = {},
): { href: string; label: string } | null {
  const kind = runKind(run);
  if (kind === "workflow" && run.workflow_id) return { href: routes.workflowRun(run.workflow_id, run.run_id), label: "Open workflow" };
  if (kind === "agent" && run.agent_id) return { href: routes.agentChat(run.agent_id), label: "Open agent" };
  if (kind === "kb_ingest" && run.knowledge_base_id) {
    const name = knowledgeBaseNames[run.knowledge_base_id];
    return {
      href: routes.knowledgeBase(run.knowledge_base_id, { tab: "runs" }),
      label: name ? `Open knowledge base: ${name}` : "Open knowledge base",
    };
  }
  return null;
}

/**
 * One run's trace in a wide Sheet (SL.12): header with the run id (copy), prev / next within the
 * hosting list, Expand to full width and Close; then the run's status, kind and "Open agent" or
 * "Open workflow"; then tabs Spans (waterfall or tree with the span detail), Events (the run's
 * event timeline) and Logs (narrowed to the selected span). Inside it, trace links stack a second
 * Sheet (`TraceLink`).
 */
export function TraceSheet({ runId, open, onOpenChange, tab, onTabChange, depth = 0, navigation = null, closeFocusTarget, initialSpanId = null }: TraceSheetProps) {
  const [expanded, setExpanded] = useState(false);
  const frameState = useFrame();
  const viewport = useViewportWidth(open);
  const frame = { viewport, sidebarCollapsed: frameState?.sidebarCollapsed ?? false };
  const width = traceSheetWidth(frame, expanded);
  const sheetFrame = useMemo(() => ({ depth, runId, onTabChange }), [depth, runId, onTabChange]);

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      {open ? (
        <SheetContent
          size={expanded ? "full" : "wide"}
          style={width === null ? undefined : { width }}
          data-testid="trace-sheet"
          data-trace-depth={depth}
          data-expanded={expanded || undefined}
          onCloseAutoFocus={(event) => {
            const target = closeFocusTarget?.(runId);
            if (target && target.isConnected) {
              event.preventDefault();
              target.focus();
            }
          }}
        >
          <TraceSheetFrameContext.Provider value={sheetFrame}>
            <TraceSheetBody
              key={runId}
              runId={runId}
              initialSpanId={initialSpanId}
              tab={tab}
              onTabChange={onTabChange}
              navigation={navigation}
              expanded={expanded}
              onToggleExpanded={() => setExpanded((v) => !v)}
              detailBeside={spanDetailBeside(renderedSheetWidth(frame, width))}
            />
          </TraceSheetFrameContext.Provider>
        </SheetContent>
      ) : null}
    </Sheet>
  );
}

/** The trace could not be loaded: the message and Retry. */
function TraceLoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-md border border-border bg-card p-6 text-center text-sm text-muted-foreground">
      <p role="alert" data-testid="trace-load-error">{message}</p>
      <Button size="sm" variant="secondary" onClick={onRetry}>Retry</Button>
    </div>
  );
}

interface TraceSheetBodyProps {
  runId: string;
  initialSpanId: string | null;
  tab: TraceTab;
  onTabChange: (tab: TraceTab) => void;
  navigation: TraceSheetNavigation | null;
  expanded: boolean;
  onToggleExpanded: () => void;
  detailBeside: boolean;
}

function TraceSheetBody({ runId, initialSpanId, tab, onTabChange, navigation, expanded, onToggleExpanded, detailBeside }: TraceSheetBodyProps) {
  const { trace, error, selectedId, setSelectedId, reload } = useTrace(runId, initialSpanId);
  const baseNames = useNames();
  const [mode, setMode] = useState<SpanTreeMode>("timeline");
  const [scoreOpen, setScoreOpen] = useState(false);
  const [requested, setRequested] = useState<PendingScores | null>(null);
  const root = trace?.spans[0] ?? null;
  const selected = trace?.spans.find((s) => s.id === selectedId) ?? null;
  const run = trace?.run ?? null;
  // Knowledge base names only when the trace shows one: a kb_ingest run or a retrieve span.
  const needsKbNames = Boolean(
    trace && (runKind(trace.run) === "kb_ingest" || trace.spans.some((s) => s.kind === "retrieve")),
  );
  const knowledgeBaseNames = useKnowledgeBaseNames(needsKbNames);
  const names = useMemo(() => ({ ...baseNames, knowledgeBaseNames }), [baseNames, knowledgeBaseNames]);
  const link = run ? subjectLink(run, knowledgeBaseNames) : null;

  const copyId = () => {
    void copyText(runId).then((copied) => toast(copied ? "Run id copied" : "Could not copy the run id"));
  };

  return (
    <>
      <SheetHeader
        title={root ? spanLabel(root, names) : "Trace"}
        description={<span className="font-mono" data-testid="trace-sheet-run-id">{`Run ${runId}`}</span>}
        actions={
          <>
            {/* No tooltips here: the first of these can take focus when the Sheet opens, and an
                open tooltip would take the first Escape (as on the kit's Close button). */}
            <IconButton label="Copy run id" size="sm" tooltip={false} icon={icon(Copy)} onClick={copyId} />
            {/* Scoring needs a completed run; a disabled button takes no hover, so the reason is the wrapper's title. */}
            <span title={run && run.status !== "completed" ? "Only completed runs can be scored" : undefined} className="inline-flex">
              <IconButton label="Score trace" size="sm" tooltip={false} icon={icon(Gauge)} disabled={!run || run.status !== "completed"} onClick={() => setScoreOpen(true)} />
            </span>
            <IconButton
              label="Download trace"
              size="sm"
              tooltip={false}
              icon={icon(Download)}
              disabled={!trace}
              onClick={() => trace && downloadJson(`trace-${runId}.json`, trace)}
            />
            {navigation ? (
              <>
                <IconButton
                  label="Previous trace"
                  size="sm"
                  tooltip={false}
                  icon={icon(ChevronUp)}
                  disabled={!navigation.prevId}
                  onClick={() => navigation.prevId && navigation.onNavigate(navigation.prevId)}
                />
                <span className="text-caption tabular-nums text-muted-foreground" data-testid="trace-sheet-position">
                  {navigation.position} / {navigation.total}
                </span>
                <IconButton
                  label="Next trace"
                  size="sm"
                  tooltip={false}
                  icon={icon(ChevronDown)}
                  disabled={!navigation.nextId}
                  onClick={() => navigation.nextId && navigation.onNavigate(navigation.nextId)}
                />
              </>
            ) : null}
            <IconButton
              label={expanded ? "Collapse to the side" : "Expand to full width"}
              aria-pressed={expanded}
              size="sm"
              tooltip={false}
              icon={icon(expanded ? Minimize2 : Maximize2)}
              onClick={onToggleExpanded}
              className="hidden md:inline-flex"
            />
          </>
        }
      />
      {run ? (
        <ScoreTraceDialog
          runId={runId}
          agentId={run.agent_id ?? null}
          open={scoreOpen}
          onOpenChange={setScoreOpen}
          onRequested={(request) => {
            setRequested({ key: Date.now(), runId, ...request });
            onTabChange("scores");
          }}
        />
      ) : null}
      <SheetBody className="flex flex-col gap-4">
        {run ? (
          <div className="flex min-w-0 flex-wrap items-center gap-2" data-testid="trace-sheet-meta">
            <StatusPill status={run.display_status} />
            <RecoveredBadge run={run} />
            <Badge variant="outline">{KIND_LABELS[runKind(run)]}</Badge>
            {link ? (
              <Link href={link.href} title={link.label} className="min-w-0 max-w-full truncate text-label text-info-text underline-offset-4 hover:underline" data-testid="trace-sheet-subject-link">
                {link.label}
              </Link>
            ) : null}
            {run.trace_id ? (
              <span className="ml-auto inline-flex gap-3">
                <a href={buildTraceLink(run.trace_id)} target="_blank" rel="noopener noreferrer" data-testid="trace-grafana-link" className="text-label text-info-text underline-offset-4 hover:underline">
                  Open in Grafana
                </a>
                <a href={buildLogsLink(run.trace_id)} target="_blank" rel="noopener noreferrer" className="text-label text-info-text underline-offset-4 hover:underline">
                  Grafana logs
                </a>
              </span>
            ) : null}
          </div>
        ) : null}

        {run ? (
          <div className="flex min-w-0 flex-col gap-3">
            {runKind(run) === "agent" && run.input ? <div className="self-start"><AddToDataset run={run} /></div> : null}
            <details className="rounded-md border border-border p-3" data-testid="trace-feedback-details">
              <summary className="cursor-pointer text-label font-medium">Trace feedback</summary>
              <TraceFeedback runId={runId} className="mt-3" />
            </details>
          </div>
        ) : null}

        <Tabs value={tab} onValueChange={(value) => onTabChange(value as TraceTab)} className="flex min-w-0 flex-col gap-4">
          <TabsList aria-label="Trace views">
            {TRACE_TABS.map((value) => (
              <TabsTrigger key={value} value={value}>
                {TAB_LABELS[value]}
              </TabsTrigger>
            ))}
          </TabsList>

          <TabsContent value="spans" className="flex flex-col gap-4">
            {error && !trace ? (
              <TraceLoadError message={error} onRetry={reload} />
            ) : !trace ? (
              <p className="py-8 text-center text-sm text-muted-foreground">Loading trace...</p>
            ) : (
              <>
                <SegmentedControl<SpanTreeMode>
                  label="Span view"
                  size="sm"
                  value={mode}
                  onValueChange={setMode}
                  options={[
                    { value: "timeline", label: "Timeline" },
                    { value: "tree", label: "Tree" },
                  ]}
                  className="self-start"
                />
                <TraceSpans trace={trace} names={names} selectedId={selectedId} onSelect={setSelectedId} error={error} mode={mode} detailBeside={detailBeside} />
              </>
            )}
          </TabsContent>

          <TabsContent value="events">
            <RunEventsPanel runId={runId} />
          </TabsContent>

          <TabsContent value="scores">
            <TraceScores runId={runId} requested={requested} />
          </TabsContent>

          <TabsContent value="logs">
            {trace ? (
              <TraceLogsPanel
                runId={runId}
                run={trace.run}
                selectedSpan={selected}
                window={{ start: selected?.start ?? null, end: selected?.end ?? null }}
              />
            ) : error ? (
              <TraceLoadError message={error} onRetry={reload} />
            ) : (
              <p className="py-8 text-center text-sm text-muted-foreground">Loading trace...</p>
            )}
          </TabsContent>
        </Tabs>
      </SheetBody>
    </>
  );
}
