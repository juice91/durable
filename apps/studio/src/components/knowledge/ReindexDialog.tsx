"use client";

import React, { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { formatCost, formatTokens } from "@/lib/format";
import { estimateReindex, startReindex, type IngestRunResult, type KnowledgeBase, type ReindexEstimate } from "@/lib/knowledge";
import { EmbeddingModelPicker, pickedModelId, useEmbeddingModels } from "./EmbeddingModelPicker";
import { alertClass } from "./knowledgeUi";

interface ReindexDialogProps {
  kb: KnowledgeBase;
  onStarted: (result: IngestRunResult) => void;
  onClose: () => void;
}

/**
 * Reindex: re-embed every section with a chosen embedding model into a new generation. Shows the dry
 * run's estimate (sections, approximate tokens, cost when the API can price the model, and whether it
 * is over the per-run budget) before anything starts. Retrieval keeps the current generation until
 * the run switches over.
 */
export function ReindexDialog({ kb, onStarted, onClose }: ReindexDialogProps) {
  const { models, status, error: modelsError, reload } = useEmbeddingModels();
  const [choice, setChoice] = useState("");
  const [wildcardName, setWildcardName] = useState("");
  const [estimate, setEstimate] = useState<ReindexEstimate | null>(null);
  const [estimating, setEstimating] = useState(false);
  const [estimateError, setEstimateError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Preselect the knowledge base's current model when it is listed as such.
  useEffect(() => {
    if (status !== "ready" || choice) return;
    if (models.some((model) => model.id === kb.embedding_model && !model.wildcard)) setChoice(kb.embedding_model);
  }, [status, models, kb.embedding_model, choice]);

  const modelId = pickedModelId(models, choice, wildcardName);

  useEffect(() => {
    setEstimate(null);
    setEstimateError(null);
    if (!modelId) return;
    let cancelled = false;
    setEstimating(true);
    const timer = setTimeout(() => {
      estimateReindex(kb.id, modelId).then(
        (result) => {
          if (!cancelled) setEstimate(result);
        },
        (reason: unknown) => {
          if (!cancelled) setEstimateError(reason instanceof Error ? reason.message : "Could not estimate the reindex");
        },
      ).finally(() => {
        if (!cancelled) setEstimating(false);
      });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [kb.id, modelId]);

  const start = async () => {
    if (!modelId || !estimate || estimate.exceeds_budget || starting) return;
    setStarting(true);
    setError(null);
    try {
      onStarted(await startReindex(kb.id, modelId));
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "The reindex could not be started");
      setStarting(false);
    }
  };

  const cost = estimate ? formatCost(estimate.estimated_cost_usd) : null;

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !starting) onClose(); }}>
      <DialogContent className="md:max-w-xl" data-testid="knowledge-reindex-dialog">
        <DialogHeader>
          <DialogTitle>Reindex {kb.name}</DialogTitle>
          <DialogDescription>
            Re-embeds every section with the chosen model. Search keeps using the current embeddings ({kb.embedding_model}) until the reindex finishes.
          </DialogDescription>
        </DialogHeader>
        <div className="flex min-w-0 flex-col gap-4">
          <EmbeddingModelPicker
            models={models}
            status={status}
            error={modelsError}
            onRetry={reload}
            choice={choice}
            onChoiceChange={setChoice}
            wildcardName={wildcardName}
            onWildcardNameChange={setWildcardName}
            disabled={starting}
            label="New embedding model"
            hint="Its dimensions may differ from the current model's; the switch is atomic when the run finishes."
            testId="knowledge-reindex-model"
            nameTestId="knowledge-reindex-model-name"
          />
          <div data-testid="knowledge-reindex-estimate" data-state={estimating ? "loading" : estimate ? "ready" : estimateError ? "error" : "idle"} aria-live="polite" className="rounded-md border border-border bg-surface p-3 text-label">
            {!modelId ? <p className="text-muted-foreground">Choose a model to see the estimate.</p> : null}
            {modelId && estimating ? <p className="text-muted-foreground">Estimating…</p> : null}
            {estimateError ? <p role="alert" className="text-destructive-text">{estimateError}</p> : null}
            {estimate && !estimating ? (
              <dl className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1">
                <dt className="text-muted-foreground">Sections</dt>
                <dd className="tabular-nums" data-testid="knowledge-reindex-chunks">{estimate.chunks.toLocaleString("en-US")}</dd>
                <dt className="text-muted-foreground">Approx. tokens</dt>
                <dd className="tabular-nums" data-testid="knowledge-reindex-tokens">{formatTokens(estimate.approx_tokens)} <span className="text-muted-foreground">of {formatTokens(estimate.max_ingest_tokens)} allowed per run</span></dd>
                <dt className="text-muted-foreground">Estimated cost</dt>
                <dd data-testid="knowledge-reindex-cost">
                  {cost ?? <span className="text-muted-foreground">Unknown{estimate.cost_note ? ` (${estimate.cost_note})` : ""}</span>}
                  {cost && estimate.price_source ? <span className="text-muted-foreground"> · {estimate.price_source}</span> : null}
                </dd>
              </dl>
            ) : null}
          </div>
          {estimate?.exceeds_budget ? (
            <p role="alert" data-testid="knowledge-reindex-over-budget" className={alertClass}>
              This would embed more tokens than one run may (EAP_KB_MAX_INGEST_TOKENS): the run would fail before calling the provider. Raise the limit on the worker first.
            </p>
          ) : null}
          {estimate?.reindex_pending ? (
            <p className="text-caption text-muted-foreground" data-testid="knowledge-reindex-pending">A reindex is already pending; starting one with its model retries it, another model is refused until it is cancelled.</p>
          ) : null}
          {error ? <p role="alert" data-testid="knowledge-reindex-error" className={alertClass}>{error}</p> : null}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={starting}>Cancel</Button>
          <Button variant="primary" onClick={() => void start()} disabled={!estimate || estimating || estimate.exceeds_budget || starting} data-testid="knowledge-reindex-start">
            {starting ? "Starting…" : "Start reindex"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
