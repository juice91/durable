import React from "react";
import { RUN_KIND_LABELS, type Run, type RunKind } from "@/lib/runs";
import { routes } from "@/lib/routes";
import { TraceLink } from "@/components/traces/TraceLink";

export type { RunKind };
export { RUN_KIND_LABELS };

/** The run's kind. Rows from before kinds existed are agent runs; a row without `kind` but with a
 * workflow or a tool (and no agent) is classified by those. */
export function runKind(run: Pick<Run, "kind" | "agent_id" | "tool_id" | "workflow_id">): RunKind {
  if (run.kind) return run.kind;
  if (run.workflow_id) return "workflow";
  if (run.agent_id === null && run.tool_id) return "tool";
  if (run.agent_id === null && run.workflow_id === null) return "eval";
  return "agent";
}

function short(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}...` : id;
}

interface RunSubjectProps {
  run: Run;
  agentNames?: Record<string, string>;
  workflowNames?: Record<string, string>;
  /** Knowledge base id → name, for kb_ingest runs (the id's link works without it). */
  knowledgeBaseNames?: Record<string, string>;
  /** Show the agent version (the run detail header); lists leave it out. */
  showVersion?: boolean;
}

/** What a run executed: an agent (and, for a workflow's agent step, the workflow run it belongs
 * to), a tool, a workflow (linking to its execution view), a knowledge base ingest (linking to the
 * knowledge base), a knowledge base source sync (linking to the knowledge base and source), or an
 * eval run (showing the target agent or workflow). */
export function RunSubject({ run, agentNames = {}, workflowNames = {}, knowledgeBaseNames = {}, showVersion = false }: RunSubjectProps) {
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
    // "Open knowledge base": its page on the Ingest runs tab. A row without the id (older API) stays text.
    const kbId = run.knowledge_base_id ?? null;
    const name = kbId ? knowledgeBaseNames[kbId] : undefined;
    const label = name ? `${RUN_KIND_LABELS.kb_ingest}: ${name}` : RUN_KIND_LABELS.kb_ingest;
    return (
      <span data-testid="run-subject" data-kind="kb_ingest">
        {kbId ? (
          <a
            href={routes.knowledgeBase(kbId, { tab: "runs" })}
            data-testid="run-subject-knowledge-base"
            title="Open knowledge base"
            className="text-primary underline-offset-4 hover:underline"
          >
            {label}
          </a>
        ) : label}
      </span>
    );
  }
  if (kind === "kb_source_sync") {
    // A knowledge-base source sync run (0029_kb_sources): link to the knowledge base's Ingest runs tab.
    // Uses source_name from the run's input JSON, or the short run id if not available.
    const kbId = run.knowledge_base_id ?? null;
    const name = kbId ? knowledgeBaseNames[kbId] : undefined;
    // The source_name is in the run's input JSON (parsed by the client).
    const sourceLabel = run.source_name ? ` (${run.source_name})` : ` ${short(run.run_id)}`;
    // The label format depends on whether the knowledge base name is available:
    // - With kb name: "KB source sync: <kb name> (<source_name> or <short_run_id>)"
    // - Without kb name but with source_name: "KB source sync: (<source_name>)" → simplify to "KB source sync (<source_name>)"
    // - Without kb name and without source_name: "KB source sync <short_run_id>"
    const label = name
      ? `${RUN_KIND_LABELS.kb_source_sync}: ${name}${sourceLabel}`
      : run.source_name
      ? `${RUN_KIND_LABELS.kb_source_sync} (${run.source_name})`
      : `${RUN_KIND_LABELS.kb_source_sync} ${short(run.run_id)}`;
    return (
      <span data-testid="run-subject" data-kind="kb_source_sync">
        {kbId ? (
          <a
            href={routes.knowledgeBase(kbId, { tab: "runs" })}
            data-testid="run-subject-knowledge-base"
            title="Open knowledge base"
            className="text-primary underline-offset-4 hover:underline"
          >
            {label}
          </a>
        ) : label}
      </span>
    );
  }
  if (kind === "eval") {
    // An eval run: target is a workflow or agent; show "Test run: <target>"
    const id = run.workflow_id ?? run.agent_id ?? null;
    const names = run.workflow_id ? workflowNames : agentNames;
    const prefix = run.workflow_id ? "Workflow" : "Agent";
    const name = id ? (names[id] ?? `${prefix} ${short(id)}`) : "unknown";
    return (
      <span data-testid="run-subject" data-kind="eval">
        Test run: {name}
      </span>
    );
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
