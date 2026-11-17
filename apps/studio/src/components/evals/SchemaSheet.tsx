"use client";

import React, { useEffect, useState } from "react";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { evaluatorLabel, evaluatorSummary, type EvalSuite } from "@/lib/evals";
import { getWorkflow, getWorkflowVersion } from "@/lib/workflows";
import { cardClass, hintClass, sectionTitleClass } from "./evalsUi";

/** The latest published version's input schema of a workflow (null while loading or when it has none). */
function useWorkflowInputSchema(workflowId: string | null): { schema: Record<string, unknown> | null; error: string | null; loading: boolean } {
  const [state, setState] = useState<{ schema: Record<string, unknown> | null; error: string | null; loading: boolean }>({
    schema: null,
    error: null,
    loading: workflowId !== null,
  });
  useEffect(() => {
    if (workflowId === null) return;
    let cancelled = false;
    getWorkflow(workflowId)
      .then(async (workflow) => {
        if (workflow.latest_version == null) return { schema: null as Record<string, unknown> | null };
        const version = await getWorkflowVersion(workflowId, workflow.latest_version);
        return { schema: version.spec.input_schema as unknown as Record<string, unknown> };
      })
      .then(({ schema }) => {
        if (!cancelled) setState({ schema, error: null, loading: false });
      })
      .catch((reason: unknown) => {
        if (!cancelled) setState({ schema: null, error: reason instanceof Error && reason.message ? reason.message : "The input schema could not be loaded", loading: false });
      });
    return () => {
      cancelled = true;
    };
  }, [workflowId]);
  return state;
}

/**
 * What a dataset checks and what its items must look like: the suite's evaluators and, for a
 * workflow suite, the input schema every item's input (JSON text of an object) is validated against.
 */
export function SchemaSheet({ suite, onClose }: { suite: EvalSuite; onClose: () => void }) {
  const workflowId = suite.workflow_id ?? null;
  const input = useWorkflowInputSchema(workflowId);
  return (
    <Sheet open onOpenChange={(open) => { if (!open) onClose(); }}>
      <SheetContent size="md" data-testid="eval-schema-sheet" aria-describedby={undefined}>
        <SheetHeader title={`Schema of ${suite.name}`} />
        <SheetBody className="space-y-6">
          {workflowId ? (
            <section className="min-w-0 space-y-2" aria-labelledby="eval-schema-input-title">
              <h3 id="eval-schema-input-title" className={sectionTitleClass}>Input schema</h3>
              <p className={hintClass}>Each item&apos;s input is JSON text of an object that must satisfy the workflow&apos;s input schema (its latest published version).</p>
              {input.loading ? <p className={hintClass} role="status">Loading the input schema…</p> : null}
              {input.error ? <p role="alert" className="text-body text-destructive-text">{input.error}</p> : null}
              {!input.loading && !input.error && input.schema === null ? <p className={hintClass}>The workflow has no published version yet.</p> : null}
              {input.schema ? (
                <pre className={`${cardClass} overflow-auto p-3 font-mono text-caption`} data-testid="eval-schema-input">{JSON.stringify(input.schema, null, 2)}</pre>
              ) : null}
            </section>
          ) : (
            <p className={hintClass}>An agent dataset&apos;s item input is the text the agent receives.</p>
          )}
          <section className="min-w-0 space-y-2" aria-labelledby="eval-schema-evaluators-title">
            <h3 id="eval-schema-evaluators-title" className={sectionTitleClass}>Evaluators</h3>
            {suite.evaluators.length === 0 ? (
              <p className={hintClass} data-testid="eval-schema-no-evaluators">No suite evaluators: items pass unless their own evaluators fail.</p>
            ) : (
              <ul className="space-y-2" data-testid="eval-schema-evaluators">
                {suite.evaluators.map((e, i) => (
                  <li key={`${e.kind}-${i}`} className={`${cardClass} p-3`}>
                    <p className="truncate text-label font-medium text-foreground">{evaluatorLabel(e)}</p>
                    <p className="break-words text-caption text-muted-foreground">{evaluatorSummary(e)}</p>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </SheetBody>
      </SheetContent>
    </Sheet>
  );
}
