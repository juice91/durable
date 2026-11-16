"use client";

import React, { useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { listWorkflowVersions, type WorkflowVersion } from "@/lib/workflows";
import { SuiteList } from "@/components/evals/TestsTab";
import { SuiteView } from "@/components/evals/SuiteView";
import { alertClass, hintClass } from "@/components/evals/evalsUi";

const NO_RUNS: never[] = [];
const noop = () => undefined;

/**
 * The workflow page's Tests tab (`/workflows/:id/tests?suite=`): the same suites, cases, imports and
 * Run dialog as an agent's Tests tab, against a published version of the workflow. A case's input is
 * JSON text of an object that must satisfy the workflow's input schema. The runs themselves are read
 * on Experiments.
 */
export function TestsTab({ workflowId, name }: { workflowId: string; name: string | null }) {
  const search = useSearchParams();
  const suiteId = search?.get("suite") || null;
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
  else if (suiteId) {
    body = (
      <SuiteView
        target={target}
        versions={versions.map((v) => ({ version: v.version }))}
        suiteId={suiteId}
        hasMemory={false}
        runs={NO_RUNS}
        runsError={null}
        reloadRuns={noop}
      />
    );
  } else body = <SuiteList target={target} runs={null} />;

  return (
    <div className="mx-auto w-full min-w-0 max-w-6xl space-y-4 p-4 md:p-6" data-testid="workflow-tests-tab">
      {body}
    </div>
  );
}
