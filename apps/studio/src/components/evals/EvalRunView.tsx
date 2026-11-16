"use client";

import React, { useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, GitCompare } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { useUrlSheet } from "@/components/ui/useUrlSheet";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { StatusPill } from "@/components/runs/StatusPill";
import { TraceLink } from "@/components/traces/TraceLink";
import { PayloadBlock } from "@/components/traces/PayloadBlock";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { formatCost, formatDuration } from "@/lib/format";
import { routes } from "@/lib/routes";
import type { RunStatus } from "@/lib/runs";
import {
  assertionCounts,
  failureReason,
  formatRate,
  isFinishedStatus,
  runCompareParam,
  runTargetLabel,
  type EvalCaseResult,
  type EvalOutcome,
  type EvalRun,
  type EvalTargetKind,
} from "@/lib/evals";
import { useEvalRun } from "./useEvalRun";
import { CaseStatusBadge, Kpi, ProgressBar, alertClass, cardClass, fieldClass, hintClass, kpiGridClass, labelClass, sectionTitleClass } from "./evalsUi";

/** The result Sheet's query parameter (`?result=<case id>`). */
export const RESULT_PARAM = "result";

interface EvalRunViewProps {
  /** The agent's or the workflow's id (see `kind`). */
  agentId: string;
  /** What the run is against; default an agent. A workflow run has no compare view. */
  kind?: EvalTargetKind;
  suiteId: string;
  runId: string;
  /** Other runs of the suite, newest first, for "Compare with". */
  otherRuns: EvalRun[];
  /** Called when the run, seen running, finishes (the tab refreshes its run history). */
  onFinished?: () => void;
}

function scoresText(result: EvalCaseResult): string {
  const entries = Object.entries(result.scores ?? {});
  if (entries.length === 0) return "—";
  return entries.map(([name, s]) => `${name} ${typeof s.value === "number" ? Math.round(s.value * 100) / 100 : String(s.value)}`).join(", ");
}

/** One eval run: progress while it runs, the aggregates, and the per-case results (a row opens its
 * details: every evaluator's outcome and reason, the output, and the case's trace). */
export function EvalRunView({ agentId, kind = "agent", suiteId, runId, otherRuns, onFinished }: EvalRunViewProps) {
  const isWorkflow = kind === "workflow";
  const router = useRouter();
  const { run, results, total, error, reload } = useEvalRun(agentId, runId, onFinished, kind);
  const sheet = useUrlSheet(RESULT_PARAM, { history: "push" });
  const [compareWith, setCompareWith] = useState("");
  const others = otherRuns.filter((r) => r.run_id !== runId);
  const selected = useMemo(() => results?.find((r) => r.case_id === sheet.value) ?? null, [results, sheet.value]);
  const finished = run ? isFinishedStatus(run.status) : false;
  const agg = run?.aggregates;

  const columns: DataListColumn<EvalCaseResult>[] = [
    { id: "case", header: "Case", cell: (r) => r.case_name, sortable: true, sortValue: (r) => r.case_name, className: "min-w-0 max-w-[16rem] truncate" },
    { id: "status", header: "Status", cell: (r) => <CaseStatusBadge status={r.status} />, sortable: true, sortValue: (r) => r.status },
    {
      id: "assertions",
      header: "Assertions",
      cell: (r) => {
        const c = assertionCounts(r);
        return c.total === 0 ? "—" : `${c.passed} / ${c.total}`;
      },
      className: "tabular-nums",
    },
    { id: "scores", header: "Scores", cell: scoresText, className: "hidden md:table-cell tabular-nums" },
    { id: "reason", header: "Why", cell: (r) => <span className="line-clamp-2 break-words text-muted-foreground" data-testid="eval-result-reason">{failureReason(r) ?? "—"}</span>, className: "hidden min-w-0 max-w-[24rem] lg:table-cell" },
    { id: "duration", header: "Duration", cell: (r) => formatDuration(r.duration_ms), sortable: true, sortValue: (r) => r.duration_ms ?? null, align: "end", className: "tabular-nums" },
    { id: "cost", header: "Cost", cell: (r) => formatCost(r.cost_usd) ?? "—", sortable: true, sortValue: (r) => (r.cost_usd === null ? null : Number(r.cost_usd)), align: "end", className: "hidden sm:table-cell tabular-nums" },
  ];

  return (
    <div className="min-w-0 space-y-4" data-testid="eval-run-view">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Button asChild variant="ghost" size="sm">
          <Link href={isWorkflow ? routes.workflowTests(agentId, { suite: suiteId, run: null }) : routes.agentTests(agentId, { suite: suiteId })} data-testid="eval-run-back">
            <ArrowLeft strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            Suite
          </Link>
        </Button>
        <h2 className={`${sectionTitleClass} min-w-0 flex-1 truncate`}>
          Eval run {run ? `· ${runTargetLabel(run)}` : ""}
        </h2>
        {run ? <StatusPill status={run.display_status as RunStatus} /> : null}
        <TraceLink runId={runId} className="text-label text-foreground underline-offset-4 hover:underline" data-testid="eval-run-trace">
          Open trace
        </TraceLink>
      </div>

      {error ? (
        <div role="alert" className={alertClass} data-testid="eval-run-load-error">
          {error}{" "}
          <Button size="sm" variant="ghost" onClick={reload}>Retry</Button>
        </div>
      ) : null}

      {run && !finished ? <ProgressBar done={results?.length ?? 0} total={total ?? Math.max(results?.length ?? 0, agg?.cases ?? 0)} /> : null}
      {run?.status === "failed" && run.error ? <p className={alertClass} data-testid="eval-run-failed">The eval run failed: {run.error}</p> : null}

      {agg ? (
        <div className={kpiGridClass} data-testid="eval-run-aggregates">
          <Kpi label="Pass rate" value={formatRate(agg.pass_rate)} testId="eval-kpi-pass-rate" />
          <Kpi label="Passed" value={`${agg.passed} / ${agg.cases}`} testId="eval-kpi-passed" />
          <Kpi label="Failed · errors" value={`${agg.failed} · ${agg.errors}`} testId="eval-kpi-failed" />
          <Kpi label="Median duration" value={formatDuration(agg.duration_ms_p50)} testId="eval-kpi-p50" />
          <Kpi label="Cost (agent + judge)" value={formatCost(agg.cost_usd) ?? "—"} testId="eval-kpi-cost" />
          {Object.entries(agg.scores_avg).map(([name, value]) => (
            <Kpi key={name} label={`Avg ${name}`} value={Math.round(value * 1000) / 1000} testId={`eval-kpi-score-${name}`} />
          ))}
        </div>
      ) : null}

      {others.length > 0 && !isWorkflow ? (
        <div className="flex min-w-0 flex-wrap items-end gap-2">
          <div className="min-w-0 flex-1 space-y-1 sm:max-w-xs">
            <label htmlFor="eval-compare-with" className={labelClass}>Compare with</label>
            <select id="eval-compare-with" className={fieldClass} value={compareWith} onChange={(e) => setCompareWith(e.target.value)} data-testid="eval-compare-with">
              <option value="">Choose a run</option>
              {others.map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {runTargetLabel(r)} · {new Date(r.created_at).toLocaleString()} · {formatRate(r.aggregates?.pass_rate)}
                </option>
              ))}
            </select>
          </div>
          <Button
            disabled={compareWith === ""}
            onClick={() => router.push(routes.agentTests(agentId, { suite: suiteId, compare: runCompareParam(compareWith, runId) }))}
            data-testid="eval-compare-go"
          >
            <GitCompare strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            Compare
          </Button>
        </div>
      ) : null}

      <DataList<EvalCaseResult>
        label="Case results"
        rows={results ?? []}
        columns={columns}
        getRowId={(r) => r.case_id}
        onRowOpen={(r) => sheet.open(r.case_id)}
        getRowLabel={(r) => `Case ${r.case_name}`}
        getRowTestId={() => "eval-result-row"}
        selectedRowId={sheet.value}
        loading={results === null && !error}
        searchPlaceholder="Search cases"
        filterRow={(r, q) => r.case_name.toLowerCase().includes(q.toLowerCase())}
        empty={{ title: finished ? "No results" : "No case has finished yet", description: finished ? "The run recorded no case results." : "Results appear here as each case finishes." }}
        testId="eval-results"
      />

      <Sheet open={sheet.isOpen && selected !== null} onOpenChange={sheet.onOpenChange}>
        {selected ? <ResultSheetContent result={selected} /> : null}
      </Sheet>
    </div>
  );
}

function OutcomeTable({ title, outcomes, kind, testId }: { title: string; outcomes: Record<string, EvalOutcome>; kind: "assertion" | "score" | "label"; testId: string }) {
  const entries = Object.entries(outcomes ?? {});
  if (entries.length === 0) return null;
  return (
    <section className="min-w-0 space-y-2" data-testid={testId}>
      <h3 className={sectionTitleClass}>{title}</h3>
      <ul className="space-y-2">
        {entries.map(([name, outcome]) => (
          <li key={name} className={`${cardClass} p-3`} data-testid="eval-outcome" data-name={name}>
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="min-w-0 flex-1 break-words font-mono text-label text-foreground">{name}</span>
              {kind === "assertion" ? (
                <CaseStatusBadge status={outcome.value === true ? "passed" : "failed"} />
              ) : (
                <span className="tabular-nums text-label text-foreground">{typeof outcome.value === "number" ? Math.round(outcome.value * 1000) / 1000 : String(outcome.value)}</span>
              )}
            </div>
            {outcome.reason ? <p className="mt-1 whitespace-pre-wrap break-words text-label text-muted-foreground" data-testid="eval-outcome-reason">{outcome.reason}</p> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

/** The result Sheet: every evaluator's outcome and reason (judge reasons included), evaluator
 * failures, the output, and the case's child run trace. */
function ResultSheetContent({ result }: { result: EvalCaseResult }) {
  const reason = failureReason(result);
  return (
    <SheetContent size="md" data-testid="eval-result-sheet" aria-describedby={undefined}>
      <SheetHeader title={`Case ${result.case_name}`} actions={<CaseStatusBadge status={result.status} />} />
      <SheetBody className="space-y-4">
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-label">
          <dt className="text-muted-foreground">Duration</dt>
          <dd className="tabular-nums">{formatDuration(result.duration_ms)}</dd>
          <dt className="text-muted-foreground">Cost</dt>
          <dd className="tabular-nums">{formatCost(result.cost_usd) ?? "—"}</dd>
          {result.error_code ? (
            <>
              <dt className="text-muted-foreground">Error</dt>
              <dd className="font-mono" data-testid="eval-result-error-code">{result.error_code}</dd>
            </>
          ) : null}
        </dl>
        {reason ? <p className={alertClass} data-testid="eval-result-why">{reason}</p> : null}
        {result.child_run_id ? (
          <TraceLink runId={result.child_run_id} className="inline-block text-label text-foreground underline underline-offset-4" data-testid="eval-result-trace">
            Open the case&apos;s trace
          </TraceLink>
        ) : null}
        <OutcomeTable title="Assertions" outcomes={result.assertions} kind="assertion" testId="eval-result-assertions" />
        <OutcomeTable title="Scores" outcomes={result.scores} kind="score" testId="eval-result-scores" />
        <OutcomeTable title="Labels" outcomes={result.labels} kind="label" testId="eval-result-labels" />
        {result.evaluator_failures?.length ? (
          <section className="min-w-0 space-y-2" data-testid="eval-result-failures">
            <h3 className={sectionTitleClass}>Evaluator failures</h3>
            <p className={hintClass}>These evaluators could not run; they count as not passed.</p>
            <ul className="space-y-2">
              {result.evaluator_failures.map((f, i) => (
                <li key={`${f.name}-${i}`} className={`${cardClass} p-3 text-label`}>
                  <span className="font-mono">{f.name}</span> <span className="text-muted-foreground">({f.kind})</span>
                  <p className="mt-1 break-words text-destructive-text">{f.error}</p>
                </li>
              ))}
            </ul>
          </section>
        ) : null}
        {result.output !== null && result.output !== undefined ? <PayloadBlock label="Output" value={result.output} /> : null}
      </SheetBody>
    </SheetContent>
  );
}
