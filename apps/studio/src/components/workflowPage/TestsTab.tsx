"use client";

import React, { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { listWorkflowVersions, type WorkflowVersion } from "@/lib/workflows";
import { SuiteList } from "@/components/evals/TestsTab";
import { SuiteView } from "@/components/evals/SuiteView";
import { EvalRunView } from "@/components/evals/EvalRunView";
import { alertClass, hintClass } from "@/components/evals/evalsUi";
import { listEvalRuns, type EvalRun } from "@/lib/evals";

/**
 * The workflow page's Tests tab (`/workflows/:id/tests?suite=`): the same suites, cases, imports and
 * Run dialog as an agent's Tests tab, against a published version of the workflow. A case's input is
 * JSON text of an object that must satisfy the workflow's input schema. A run opens in the same run view as an agent's.
 */
export function TestsTab({ workflowId, name }: { workflowId: string; name: string | null }) {
  const search = useSearchParams();
  const suiteId = search?.get("suite") || null;
  const runId = search?.get("run") || null;
  const [runs, setRuns] = useState<EvalRun[] | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [runsKey, setRunsKey] = useState(0);
  const reloadRuns = useCallback(() => setRunsKey((k) => k + 1), []);
  useEffect(() => {
    if (!suiteId) return;
    const controller = new AbortController();
    setRuns(null); // a suite change must not show the previous suite's runs
    setRunsError(null);
    listEvalRuns(workflowId, { suiteId, kind: "workflow", signal: controller.signal })
      .then((page) => {
        if (!controller.signal.aborted) setRuns(page.items);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setRuns([]);
        setRunsError(reason instanceof Error && reason.message ? reason.message : "Eval runs could not be loaded");
      });
    return () => controller.abort();
  }, [workflowId, suiteId, runsKey]);
  const [versions, setVersions] = useState<WorkflowVersion[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listWorkflowVersions(workflowId)
      .then((items) => {
        if (!cancelled) setVersions(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        if (!cancelled) setError("Failed to load the workflow's versions.");
      });
    return () => {
      cancelled = true;
    };
  }, [workflowId]);

  const target = { kind: "workflow" as const, id: workflowId, name: name ?? "Workflow" };
  let body: React.ReactNode;
  if (error && versions === null) body = <p role="alert" className={alertClass}>{error}</p>;
  else if (versions === null) body = <p className={hintClass} role="status">Loading tests…</p>;
  else if (suiteId && runId) {
    body = (
      <EvalRunView key={runId} agentId={workflowId} kind="workflow" suiteId={suiteId} runId={runId} otherRuns={runs ?? []} onFinished={reloadRuns} />
    );
  } else if (suiteId) {
    body = (
      <SuiteView
        target={target}
        versions={versions.map((v) => ({ version: v.version }))}
        suiteId={suiteId}
        hasMemory={false}
        runs={runs}
        runsError={runsError}
        reloadRuns={reloadRuns}
      />
    );
  } else body = <SuiteList target={target} runs={null} />;

  return (
    <div className="mx-auto w-full min-w-0 max-w-6xl space-y-4 p-4 md:p-6" data-testid="workflow-tests-tab">
      {body}
    </div>
  );
}
