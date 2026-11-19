"use client";

import React, { useCallback, useEffect, useState } from "react";
import { PageHeader } from "@/components/frame/PageHeader";
import { SuiteView } from "@/components/evals/SuiteView";
import { agentHasMemory } from "@/components/memory/memoryModel";
import { MemoryNote, alertClass, hintClass } from "@/components/evals/evalsUi";
import { api, ApiError, type Agent, type AgentVersion } from "@/lib/api";
import { getSuite, listEvalRuns, suiteTarget, type EvalRun, type EvalSuiteWithCases, type EvalTargetRef } from "@/lib/evals";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import { listWorkflowVersions } from "@/lib/workflows";

interface Loaded {
  suite: EvalSuiteWithCases;
  target: EvalTargetRef;
  agent: Agent | null;
  versions: Array<Pick<AgentVersion, "version"> & Partial<Pick<AgentVersion, "spec">>>;
}

/**
 * `/datasets/:id`: one dataset, i.e. a test suite of an agent or a workflow. The header names the
 * dataset; below it the suite view has New item, Import CSV, Import JSON and Schema, the evaluators,
 * the items, the Run action and (for an agent) the run history.
 */
export function DatasetDetail({ suiteId }: { suiteId: string }) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [runs, setRuns] = useState<EvalRun[] | null>(null);
  const [runsError, setRunsError] = useState<string | null>(null);
  const [runsKey, setRunsKey] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    setNotFound(false);
    (async () => {
      const suite = await getSuite(suiteId, controller.signal);
      const target = suiteTarget(suite);
      if (target.kind === "workflow") {
        const versions = await listWorkflowVersions(target.id).catch(() => []);
        return { suite, target, agent: null, versions: (Array.isArray(versions) ? versions : []).map((v) => ({ version: v.version })) } satisfies Loaded;
      }
      const [agent, versions] = await Promise.all([
        api.agents.get(target.id),
        api.versions.list(target.id).catch(() => [] as AgentVersion[]),
      ]);
      return { suite, target: { ...target, name: target.name || agent.name }, agent, versions: Array.isArray(versions) ? versions : [] } satisfies Loaded;
    })()
      .then((value) => {
        if (!controller.signal.aborted) setLoaded(value);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        if (reason instanceof ApiError && reason.status === 404) setNotFound(true);
        else setError(reason instanceof Error && reason.message ? reason.message : "The test suite could not be loaded");
      });
    return () => controller.abort();
  }, [suiteId]);

  const agentId = loaded?.target.id ?? null;
  const runKind = loaded?.target.kind ?? "agent";
  useEffect(() => {
    if (agentId === null) return;
    const controller = new AbortController();
    setRunsError(null);
    listEvalRuns(agentId, { suiteId, kind: runKind, signal: controller.signal })
      .then((page) => {
        if (!controller.signal.aborted) setRuns(page.items);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setRuns([]);
        setRunsError(reason instanceof Error && reason.message ? reason.message : "Eval runs could not be loaded");
      });
    return () => controller.abort();
  }, [agentId, runKind, suiteId, runsKey]);
  const reloadRuns = useCallback(() => setRunsKey((k) => k + 1), []);

  const name = loaded?.suite.name ?? null;
  return (
    <>
      <PageHeader
        crumbs={[navCrumb("datasets"), { label: name ?? "Test suite", testId: "dataset-title" }]}
        documentTitle={name ? `${name} · Test suites` : "Test suite"}
      />
      <div className="mx-auto w-full min-w-0 max-w-6xl space-y-4 p-4 md:p-6" data-testid="dataset-detail">
        {notFound ? <p role="alert" className={alertClass}>Test suite not found.</p> : null}
        {error ? <p role="alert" className={alertClass}>{error}</p> : null}
        {!loaded && !notFound && !error ? <p className={hintClass} role="status">Loading test suite…</p> : null}
        {loaded ? (
          <>
            <p className={hintClass} data-testid="dataset-target-line">
              Target: {loaded.target.kind} · {loaded.target.name || "unknown"}
            </p>
            {loaded.agent && agentHasMemory(loaded.agent, loaded.versions as AgentVersion[]) ? <MemoryNote /> : null}
            <SuiteView
              target={loaded.target}
              agent={loaded.agent}
              versions={loaded.versions}
              suiteId={suiteId}
              dataset
              backHref={routes.datasets()}
              hasMemory={loaded.agent ? agentHasMemory(loaded.agent, loaded.versions as AgentVersion[]) : false}
              runs={runs}
              runsError={runsError}
              reloadRuns={reloadRuns}
            />
          </>
        ) : null}
      </div>
    </>
  );
}
