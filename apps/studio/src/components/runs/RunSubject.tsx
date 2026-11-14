import React from "react";
import { RUN_KIND_LABELS, type Run, type RunKind } from "@/lib/runs";
import { routes } from "@/lib/routes";
import { TraceLink } from "@/components/traces/TraceLink";

export type { RunKind };

/** The run's kind. Rows from before kinds existed are agent runs; a row without `kind` but with a
 * workflow or a tool (and no agent) is classified by those. */
export function runKind(run: Pick<Run, "kind" | "agent_id" | "tool_id" | "workflow_id">): RunKind {
  if (run.kind) return run.kind;
  if (run.workflow_id) return "workflow";
  if (run.agent_id === null && run.tool_id) return "tool";
  return "agent";
}

function short(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}...` : id;
}

interface RunSubjectProps {
  run: Run;
  agentNames?: Record<string, string>;
  workflowNames?: Record<string, string>;
  /** Show the agent version (the run detail header); lists leave it out. */
  showVersion?: boolean;
}

/** What a run executed: an agent (and, for a workflow's agent step, the workflow run it belongs
 * to), a tool, or a workflow (linking to its execution view). */
export function RunSubject({ run, agentNames = {}, workflowNames = {}, showVersion = false }: RunSubjectProps) {
  const kind = runKind(run);
  if (kind === "workflow") {
    const workflowId = run.workflow_id ?? null;
    const name = workflowId ? (workflowNames[workflowId] ?? short(workflowId)) : "unknown";
    const version = run.workflow_version != null ? ` v${run.workflow_version}` : "";
    const label = `Workflow: ${name}${version}`;
    return (
      <span data-testid="run-subject" data-kind="workflow">
        {workflowId ? (
          <a href={routes.workflowRun(workflowId, run.run_id)} className="text-primary underline-offset-4 hover:underline">
            {label}
          </a>
        ) : label}
      </span>
    );
  }
  if (kind === "tool") {
    return <span data-testid="run-subject" data-kind="tool">{`Tool: ${run.tool_id ?? "unknown"}`}</span>;
  }
  if (kind === "kb_ingest") {
    // No knowledge-base page to link to yet (RM.16 adds it).
    return <span data-testid="run-subject" data-kind="kb_ingest">{RUN_KIND_LABELS.kb_ingest}</span>;
  }
  const named = run.agent_id ? agentNames[run.agent_id] : undefined;
  const agent = named ?? (run.agent_id ? (showVersion ? run.agent_id : short(run.agent_id)) : "unknown");
  return (
    <span data-testid="run-subject" data-kind="agent">
      {showVersion ? `Agent: ${agent} | Version: ${run.version ?? "latest"}` : agent}
      {run.parent_run_id ? (
        <span className="block text-xs font-normal text-muted-foreground">
          Part of workflow run{" "}
          <TraceLink
            data-testid="run-parent-link"
            runId={run.parent_run_id}
            tab="events"
            className="font-mono text-primary underline-offset-4 hover:underline"
          >
            {short(run.parent_run_id)}
          </TraceLink>
          {run.parent_step_id ? <> (step <span className="font-mono">{run.parent_step_id}</span>)</> : null}
        </span>
      ) : null}
    </span>
  );
}
