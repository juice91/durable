"use client";

import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { FlaskConical, Plus } from "lucide-react";
import { api, type Agent, type AgentVersion } from "@/lib/api";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { formatRate, listEvalRuns, listSuites, listWorkflowSuites, parseRunCompare, runTargetLabel, createSuite, createWorkflowSuite, MAX_DESCRIPTION_CHARS, MAX_NAME_CHARS, type EvalRun, type EvalSuite, type EvalTargetRef } from "@/lib/evals";
import { routes } from "@/lib/routes";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { useCreateAction } from "@/components/shortcuts/ShortcutsProvider";
import { useAgentPage } from "@/components/agentPage/AgentPageContext";
import { TraceSheetHost } from "@/components/traces/TraceSheetHost";
import { agentHasMemory } from "@/components/memory/memoryModel";
import { CompareView } from "./CompareView";
import { EvalRunView } from "./EvalRunView";
import { SuiteView } from "./SuiteView";
import { MemoryNote, alertClass, fieldClass, hintClass, labelClass } from "./evalsUi";

/** The agent and its versions: from the agent page, or loaded here when the tab renders alone. */
function useTestsAgent(agentId: string): { agent: Agent | null; versions: AgentVersion[] | null; error: string | null } {
  const page = useAgentPage();
  const shared = page !== null && page.agentId === agentId;
  const [agent, setAgent] = useState<Agent | null>(null);
  const [versions, setVersions] = useState<AgentVersion[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (shared) return;
    let cancelled = false;
    Promise.all([api.agents.get(agentId), api.versions.list(agentId).catch(() => [] as AgentVersion[])])
      .then(([loaded, items]) => {
        if (cancelled) return;
        setAgent(loaded);
        setVersions(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        if (!cancelled) setError("Failed to load the agent.");
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, shared]);
  if (shared) return { agent: page.agent, versions: page.versions, error: page.loadError };
  return { agent, versions, error };
}

/**
 * Eval runs, newest first, one page: one suite's (`suiteId`, so a suite's older runs are not lost
 * behind the agent's newest 50) or the agent's. Reloadable; also reloaded when the browser tab
 * becomes visible again.
 */
function useEvalRuns(agentId: string, suiteId: string | null): { runs: EvalRun[] | null; error: string | null; reload: () => void } {
  const [runs, setRuns] = useState<EvalRun[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [key, setKey] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    listEvalRuns(agentId, { suiteId, signal: controller.signal })
      .then((page) => {
        if (!controller.signal.aborted) setRuns(page.items);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setRuns([]);
        setError(reason instanceof Error && reason.message ? reason.message : "Eval runs could not be loaded");
      });
    return () => controller.abort();
  }, [agentId, suiteId, key]);
  const reload = useCallback(() => setKey((k) => k + 1), []);
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") reload();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [reload]);
  return { runs, error, reload };
}

/**
 * The agent page's Tests tab (`/agents/:id/tests`, AC.16). URL state: `?suite=` opens a suite,
 * `&run=` one of its eval runs (`&result=<case id>` its result Sheet), `&compare=<base>..<target>`
 * two runs compared, `&edit=` the suite or case editor Sheet. An agent with memory shows the note
 * that cases run as single turns without memory.
 */
export function TestsTab({ agentId }: { agentId: string }) {
  const search = useSearchParams();
  const suiteId = search?.get("suite") || null;
  const runId = search?.get("run") || null;
  const compare = parseRunCompare(search?.get("compare"));
  const { agent, versions, error: agentError } = useTestsAgent(agentId);
  const evalRuns = useEvalRuns(agentId, suiteId);
  const hasMemory = agentHasMemory(agent, versions);
  // Coming back to the suite or the list (from a run or a compare) re-reads the runs: a run watched
  // to its end, or one that finished meanwhile, shows its final status and aggregates.
  const view = `${runId ?? ""}|${search?.get("compare") ?? ""}`;
  const firstView = useRef(true);
  const { reload: reloadRuns } = evalRuns;
  useEffect(() => {
    if (firstView.current) {
      firstView.current = false;
      return;
    }
    reloadRuns();
  }, [view, reloadRuns]);

  let body: React.ReactNode;
  if (agentError && !agent) {
    body = <p role="alert" className={alertClass}>{agentError}</p>;
  } else if (!agent || versions === null) {
    body = <p className={hintClass} role="status">Loading tests…</p>;
  } else if (suiteId && compare) {
    body = <CompareView agentId={agentId} suiteId={suiteId} base={compare.base} target={compare.target} />;
  } else if (suiteId && runId) {
    body = (
      <EvalRunView
        key={runId}
        agentId={agentId}
        suiteId={suiteId}
        runId={runId}
        otherRuns={(evalRuns.runs ?? []).filter((r) => r.suite_id === suiteId)}
        onFinished={evalRuns.reload}
      />
    );
  } else if (suiteId) {
    body = <SuiteView agent={agent} versions={versions} suiteId={suiteId} hasMemory={hasMemory} runs={evalRuns.runs} runsError={evalRuns.error} reloadRuns={evalRuns.reload} />;
  } else {
    body = <SuiteList target={{ kind: "agent", id: agentId, name: agent.name }} runs={evalRuns.runs} />;
  }

  return (
    // The agent page hosts the trace Sheet; this nested host only matters when the tab renders alone.
    <TraceSheetHost>
      <div className="mx-auto w-full min-w-0 max-w-6xl space-y-4 p-4 md:p-6" data-testid="tests-tab">
        {hasMemory ? <MemoryNote /> : null}
        {body}
      </div>
    </TraceSheetHost>
  );
}

// ---------------------------------------------------------------------------- suite list

/** The suites of an agent or a workflow, with the newest run of each (agent runs only: a workflow's
 * runs are read on Experiments). A row opens the suite on the target's Tests tab. */
export function SuiteList({ target, runs }: { target: EvalTargetRef; runs: EvalRun[] | null }) {
  const router = useRouter();
  const isWorkflow = target.kind === "workflow";
  const testsHref = (suite?: string) => (isWorkflow ? routes.workflowTests(target.id, { suite }) : routes.agentTests(target.id, { suite }));
  const [suites, setSuites] = useState<EvalSuite[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [key, setKey] = useState(0);
  const [creating, setCreating] = useState(false);
  useCreateAction(() => setCreating(true));

  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    (isWorkflow ? listWorkflowSuites : listSuites)(target.id, { signal: controller.signal })
      .then((items) => {
        if (!controller.signal.aborted) setSuites(items);
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(reason instanceof Error && reason.message ? reason.message : "Test suites could not be loaded");
      });
    return () => controller.abort();
  }, [target.id, isWorkflow, key]);

  // The newest run of each suite (runs come newest first).
  const lastRun = new Map<string, EvalRun>();
  for (const run of runs ?? []) if (run.suite_id && !lastRun.has(run.suite_id)) lastRun.set(run.suite_id, run);

  const columns: DataListColumn<EvalSuite>[] = [
    { id: "name", header: "Suite", cell: (s) => s.name, sortable: true, sortValue: (s) => s.name, className: "min-w-0 max-w-[16rem] truncate" },
    { id: "description", header: "Description", cell: (s) => <span className="line-clamp-2 break-words text-muted-foreground">{s.description || "—"}</span>, className: "hidden min-w-0 max-w-[24rem] md:table-cell" },
    { id: "cases", header: "Cases", cell: (s) => s.case_count, sortable: true, sortValue: (s) => s.case_count, align: "end", className: "tabular-nums" },
    { id: "evaluators", header: "Evaluators", cell: (s) => s.evaluators.length, align: "end", className: "hidden sm:table-cell tabular-nums" },
    {
      id: "last",
      header: "Last run",
      cell: (s) => {
        const run = lastRun.get(s.id);
        return run ? `${runTargetLabel(run)} · ${formatRate(run.aggregates?.pass_rate)}` : "—";
      },
      className: "hidden sm:table-cell tabular-nums",
    },
  ];

  const newSuiteButton = (
    <Button variant="primary" size="sm" onClick={() => setCreating(true)} data-testid="eval-suite-new">
      <Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
      New suite
    </Button>
  );

  return (
    <div className="min-w-0 space-y-3" data-testid="eval-suite-list">
      <DataList<EvalSuite>
        label="Test suites"
        rows={suites ?? []}
        columns={columns}
        getRowId={(s) => s.id}
        getRowHref={(s) => testsHref(s.id)}
        getRowTestId={() => "eval-suite-row"}
        loading={suites === null && !error}
        error={error}
        onRetry={() => setKey((k) => k + 1)}
        searchPlaceholder="Search suites"
        filterRow={(s, q) => `${s.name}\n${s.description ?? ""}`.toLowerCase().includes(q.toLowerCase())}
        toolbar={newSuiteButton}
        empty={{
          title: "No test suites yet",
          description: isWorkflow
            ? "A suite holds cases (the JSON input of a run and the output you expect) and the evaluators that score the workflow's output. Run it against a published version."
            : "A suite holds cases (an input and the output you expect) and the evaluators that score the agent's answers. Run it against a version or the draft, then compare runs.",
          action: newSuiteButton,
        }}
        testId="eval-suites"
      />
      {creating ? (
        <NewSuiteDialog
          target={target}
          onClose={() => setCreating(false)}
          onCreated={(suite) => {
            setCreating(false);
            router.push(testsHref(suite.id));
          }}
        />
      ) : null}
    </div>
  );
}

function NewSuiteDialog({ target, onCreated, onClose }: { target: EvalTargetRef; onCreated: (suite: EvalSuite) => void; onClose: () => void }) {
  const id = useId();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving || !name.trim()) return;
    setSaving(true);
    setError(null);
    try {
      const body = { name: name.trim(), description: description.trim() || null, evaluators: [{ kind: "equals_expected" as const }] };
      onCreated(await (target.kind === "workflow" ? createWorkflowSuite : createSuite)(target.id, body));
    } catch (reason: unknown) {
      setError(reason instanceof Error && reason.message ? reason.message : "The suite could not be created");
      setSaving(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !saving) onClose(); }}>
      <DialogContent className="md:max-w-lg" data-testid="eval-suite-create-dialog">
        <DialogHeader>
          <DialogTitle>New test suite</DialogTitle>
          <DialogDescription>It starts with an “Equals expected” evaluator; change the evaluators once it exists.</DialogDescription>
        </DialogHeader>
        <form onSubmit={(e) => void submit(e)} className="min-w-0 space-y-4" aria-label="New test suite">
          {error ? <p role="alert" className={alertClass} data-testid="eval-suite-create-error">{error}</p> : null}
          <div className="space-y-1">
            <label htmlFor={`${id}-name`} className={labelClass}>Name</label>
            <input id={`${id}-name`} className={fieldClass} value={name} maxLength={MAX_NAME_CHARS} required onChange={(e) => setName(e.target.value)} placeholder="Refund questions" data-testid="eval-suite-create-name" />
          </div>
          <div className="space-y-1">
            <label htmlFor={`${id}-description`} className={labelClass}>Description <span className="font-normal text-muted-foreground">(optional)</span></label>
            <textarea id={`${id}-description`} rows={3} className={fieldClass} value={description} maxLength={MAX_DESCRIPTION_CHARS} onChange={(e) => setDescription(e.target.value)} data-testid="eval-suite-create-description" />
          </div>
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose} disabled={saving}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={saving || !name.trim()} data-testid="eval-suite-create-submit">
              <FlaskConical strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
              {saving ? "Creating…" : "Create suite"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
