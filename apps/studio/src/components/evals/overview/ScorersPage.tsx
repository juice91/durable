"use client";

import React from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/Badge";
import { PageHeader } from "@/components/frame/PageHeader";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { listAllScorers, scorerKey, type Scorer, type ScorerPoint } from "@/lib/evalOverview";
import { EVALUATOR_LABELS, formatRate, type EvaluatorKind } from "@/lib/evals";
import { formatScore } from "@/lib/format";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import { LoadMore } from "./LoadMore";
import { ReusableScorers } from "./ReusableScorers";
import { usePagedList } from "./usePagedList";

function pointText(point: ScorerPoint): string {
  return point.metric === "pass_rate" ? formatRate(point.value) : formatScore(point.value);
}

/** The recent values, oldest to newest left to right, as a compact text trail ("50% → 80%"). */
function History({ scorer }: { scorer: Scorer }) {
  if (scorer.history.length === 0) return <span className="text-muted-foreground" data-testid="scorer-history">No runs yet</span>;
  const points = [...scorer.history].reverse();
  const label = scorer.history[0].metric === "pass_rate" ? "Pass rate" : "Mean score";
  return (
    <span className="inline-flex min-w-0 flex-col" data-testid="scorer-history">
      <span className="tabular-nums" title={`${label} per run, oldest first`}>
        {points.map((point) => pointText(point)).join(" → ")}
      </span>
      <span className="text-caption text-muted-foreground">{label}, {points.length} {points.length === 1 ? "run" : "runs"}</span>
    </span>
  );
}

const columns: DataListColumn<Scorer>[] = [
  {
    id: "name",
    header: "Scorer",
    sortable: true,
    sortValue: (s) => s.name,
    cell: (s) => <span className="truncate font-mono">{s.name}</span>,
  },
  {
    id: "kind",
    header: "Type",
    sortable: true,
    sortValue: (s) => s.kind,
    cell: (s) => <Badge variant="outline">{EVALUATOR_LABELS[s.kind as EvaluatorKind] ?? s.kind}</Badge>,
  },
  {
    id: "suites",
    header: "Used by",
    cell: (s) => (
      <span className="flex min-w-0 flex-wrap gap-x-2 gap-y-1" data-testid="scorer-suites">
        {s.suites.slice(0, 3).map((suite) => (
          <Link
            key={suite.id}
            href={suite.workflow_id ? routes.workflowTests(suite.workflow_id, { suite: suite.id }) : routes.agentTests(suite.agent_id ?? "", { suite: suite.id })}
            className="max-w-48 truncate underline underline-offset-4"
            title={`${suite.name} (${suite.target_kind === "workflow" ? "workflow" : "agent"} ${suite.target_name ?? (suite.target_kind === "workflow" ? "Unknown workflow" : "Unknown agent")})`}
          >
            {suite.name}
          </Link>
        ))}
        {s.suite_count > 3 ? <span className="text-muted-foreground">+{s.suite_count - 3} more</span> : null}
      </span>
    ),
  },
  { id: "history", header: "Recent scores", cell: (s) => <History scorer={s} /> },
];

/** Scorers: the evaluators in use across suites, the suites using each, and their recent values. */
export function ScorersPage() {
  const list = usePagedList((after, signal) => listAllScorers({ after, signal }), "Failed to load scorers");
  return (
    <>
      <PageHeader crumbs={[navCrumb("scorers", { current: true })]} />
      <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-4 px-4 py-6">
        <ReusableScorers />
        <p className="text-body text-muted-foreground">The evaluators your test suites use, with the suites that use them and how they scored in recent runs.</p>
        <DataList<Scorer>
          label="Scorers"
          testId="scorers-list"
          rows={list.rows}
          columns={columns}
          getRowId={scorerKey}
          onRowOpen={undefined}
          getRowTestId={() => "scorer-row"}
          searchPlaceholder="Search by scorer or type"
          filterRow={(s, q) => `${s.name}\n${s.kind}`.toLowerCase().includes(q.toLowerCase())}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          empty={{
            title: "No scorers yet",
            description: "Add evaluators to a test suite in an agent's Tests tab. They are listed here once a suite uses them.",
          }}
        />
        <LoadMore list={list} label="scorers" />
      </div>
    </>
  );
}
