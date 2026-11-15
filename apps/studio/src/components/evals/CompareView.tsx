"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, FileDiff } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { formatCost, formatDuration } from "@/lib/format";
import { compareParam, type CompareRouteRef } from "@/lib/routes/agents";
import {
  compareEvalRuns,
  formatRate,
  formatRateDelta,
  formatSigned,
  targetLabel,
  type CompareCase,
  type CompareSide,
  type EvalCompare,
} from "@/lib/evals";
import { routes as allRoutes } from "@/lib/routes";
import { CaseStatusBadge, ChangeBadge, Delta, Kpi, WarningIcon, alertClass, kpiGridClass, sectionTitleClass, warningClass } from "./evalsUi";

interface CompareViewProps {
  agentId: string;
  suiteId: string;
  base: string;
  target: string;
}

function sideRef(side: CompareSide): CompareRouteRef {
  return side.draft || side.agent_version === null ? "draft" : side.agent_version;
}

/**
 * The editor's version compare for two eval runs' targets (`/agents/:id/editor?compare=a..b`), or
 * null when both ran the same version (or both the draft): there is no spec diff to show.
 */
export function specDiffHref(agentId: string, compare: Pick<EvalCompare, "base" | "target">): string | null {
  const base = sideRef(compare.base);
  const target = sideRef(compare.target);
  if (base === target) return null;
  return allRoutes.agentEditor(agentId, { compare: compareParam(base, target) });
}

/** A score average as the run view shows it: 3 decimal places at most. */
function roundScore(value: number | null | undefined): string {
  return typeof value === "number" && Number.isFinite(value) ? String(Math.round(value * 1000) / 1000) : "—";
}

function assertionText(c: CompareCase): string {
  const flips = Object.entries(c.assertions).filter(([, v]) => v.base !== v.target);
  if (flips.length === 0) return "—";
  return flips.map(([name, v]) => `${name}: ${v.base === true ? "pass" : v.base === false ? "fail" : "—"} → ${v.target === true ? "pass" : v.target === false ? "fail" : "—"}`).join(", ");
}

function scoreDeltaText(c: CompareCase): React.ReactNode {
  const entries = Object.entries(c.score_deltas).filter(([, d]) => d !== null && d !== 0);
  if (entries.length === 0) return "—";
  return entries.map(([name, d]) => (
    <span key={name} className="mr-2 whitespace-nowrap">
      {name} <Delta value={d} text={formatSigned(d)} />
    </span>
  ));
}

/** Two eval runs case by case: what was fixed or regressed, assertion flips, score, duration and cost
 * deltas; the summary deltas; and "View spec diff" to the Editor's version compare. */
export function CompareView({ agentId, suiteId, base, target }: CompareViewProps) {
  const [data, setData] = useState<EvalCompare | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setData(null);
    setError(null);
    compareEvalRuns(agentId, base, target, controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setData(result);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error && reason.message ? reason.message : "The runs could not be compared");
      });
    return () => controller.abort();
  }, [agentId, base, target]);

  const columns: DataListColumn<CompareCase>[] = [
    { id: "case", header: "Case", cell: (c) => c.name, sortable: true, sortValue: (c) => c.name, className: "min-w-0 max-w-[16rem] truncate" },
    { id: "change", header: "Change", cell: (c) => <ChangeBadge change={c.change} />, sortable: true, sortValue: (c) => c.change },
    {
      id: "status",
      header: "Status",
      cell: (c) => (
        <span className="inline-flex items-center gap-1">
          <CaseStatusBadge status={c.base_status} />
          <span aria-hidden="true" className="text-muted-foreground">→</span>
          <span className="sr-only">to</span>
          <CaseStatusBadge status={c.target_status} />
        </span>
      ),
    },
    { id: "assertions", header: "Assertions", cell: (c) => <span className="break-words text-muted-foreground">{assertionText(c)}</span>, className: "hidden min-w-0 max-w-[20rem] lg:table-cell" },
    { id: "scores", header: "Scores", cell: scoreDeltaText, className: "hidden md:table-cell" },
    { id: "duration", header: "Duration", cell: (c) => <Delta value={c.duration_ms_delta} lowerIsBetter text={c.duration_ms_delta === null ? "—" : `${c.duration_ms_delta > 0 ? "+" : c.duration_ms_delta < 0 ? "−" : "±"}${formatDuration(Math.abs(c.duration_ms_delta))}`} />, align: "end", className: "hidden sm:table-cell" },
    { id: "cost", header: "Cost", cell: (c) => <Delta value={c.cost_usd_delta} lowerIsBetter text={formatSigned(c.cost_usd_delta, "$")} />, align: "end", className: "hidden sm:table-cell" },
  ];

  const diffHref = data ? specDiffHref(agentId, data) : null;
  const s = data?.summary;

  return (
    <div className="min-w-0 space-y-4" data-testid="eval-compare-view">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Button asChild variant="ghost" size="sm">
          <Link href={allRoutes.agentTests(agentId, { suite: suiteId })} data-testid="eval-compare-back">
            <ArrowLeft strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            Suite
          </Link>
        </Button>
        <h2 className={`${sectionTitleClass} min-w-0 flex-1 truncate`} data-testid="eval-compare-title">
          {data ? `${targetLabel(data.base.agent_version, data.base.draft)} → ${targetLabel(data.target.agent_version, data.target.draft)}` : "Compare runs"}
        </h2>
        {diffHref ? (
          <Button asChild size="sm">
            <Link href={diffHref} data-testid="eval-compare-spec-diff">
              <FileDiff strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
              View spec diff
            </Link>
          </Button>
        ) : null}
      </div>

      {diffHref && data && (data.base.draft || data.target.draft) ? (
        <p className="text-caption text-muted-foreground" data-testid="eval-compare-draft-hint">
          The spec diff shows the current draft, which may have changed since the draft run.
        </p>
      ) : null}
      {error ? <p role="alert" className={alertClass} data-testid="eval-compare-error">{error}</p> : null}
      {data?.warnings.map((w) => (
        <p key={w} className={`${warningClass} flex min-w-0 items-start gap-2`} data-testid="eval-compare-warning">
          <WarningIcon />
          <span className="min-w-0">{w}</span>
        </p>
      ))}

      {data && s ? (
        <div className={kpiGridClass} data-testid="eval-compare-summary">
          <Kpi label="Pass rate" value={`${formatRate(data.base.aggregates.pass_rate)} → ${formatRate(data.target.aggregates.pass_rate)}`} testId="eval-compare-pass-rate">
            <Delta value={s.pass_rate_delta} text={formatRateDelta(s.pass_rate_delta)} testId="eval-compare-pass-rate-delta" />
          </Kpi>
          <Kpi label="Fixed · regressed" value={`${s.fixed} · ${s.regressed}`} testId="eval-compare-counts">
            <span className="text-caption text-muted-foreground">
              {s.unchanged} unchanged{s.added ? ` · ${s.added} added` : ""}{s.removed ? ` · ${s.removed} removed` : ""}
            </span>
          </Kpi>
          <Kpi label="Median duration" value={formatDuration(data.target.aggregates.duration_ms_p50)} testId="eval-compare-p50">
            <Delta value={s.duration_ms_p50_delta} lowerIsBetter text={s.duration_ms_p50_delta === null ? "—" : `${s.duration_ms_p50_delta > 0 ? "+" : s.duration_ms_p50_delta < 0 ? "−" : "±"}${formatDuration(Math.abs(s.duration_ms_p50_delta))}`} />
          </Kpi>
          <Kpi label="Cost" value={formatCost(data.target.aggregates.cost_usd) ?? "—"} testId="eval-compare-cost">
            <Delta value={s.cost_usd_delta} lowerIsBetter text={formatSigned(s.cost_usd_delta, "$")} testId="eval-compare-cost-delta" />
          </Kpi>
          {Object.entries(s.scores_avg_delta).map(([name, d]) => (
            <Kpi key={name} label={`Avg ${name}`} value={roundScore(data.target.aggregates.scores_avg[name])} testId={`eval-compare-score-${name}`}>
              <Delta value={d} text={formatSigned(d)} />
            </Kpi>
          ))}
        </div>
      ) : null}

      <DataList<CompareCase>
        label="Cases compared"
        rows={data?.cases ?? []}
        columns={columns}
        getRowId={(c) => `${c.base_case_id ?? ""}:${c.target_case_id ?? ""}:${c.name}`}
        getRowTestId={() => "eval-compare-row"}
        loading={data === null && !error}
        searchPlaceholder="Search cases"
        filterRow={(c, q) => c.name.toLowerCase().includes(q.toLowerCase())}
        empty={{ title: "No cases to compare", description: "Neither run recorded a case result." }}
        testId="eval-compare-cases"
      />
    </div>
  );
}
