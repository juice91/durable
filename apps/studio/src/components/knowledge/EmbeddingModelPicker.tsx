"use client";

import React, { useEffect, useId, useState } from "react";
import { Button } from "@/components/ui/Button";
import { listEmbeddingModels, type EmbeddingModel } from "@/lib/knowledge";
import { fieldClass, hintClass, labelClass } from "./knowledgeUi";
import { Select } from "@/components/ui/Select";

/** The allowlisted embedding models (GET /embedding-models), loaded once per mount. */
export function useEmbeddingModels(): { models: EmbeddingModel[]; status: "loading" | "ready" | "error"; error: string | null; reload: () => void } {
  const [models, setModels] = useState<EmbeddingModel[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setStatus("loading");
    listEmbeddingModels().then(
      (items) => {
        if (cancelled) return;
        setModels(items);
        setStatus("ready");
        setError(null);
      },
      (reason: unknown) => {
        if (cancelled) return;
        setStatus("error");
        setError(reason instanceof Error ? reason.message : "Could not load the embedding models");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [attempt]);
  return { models, status, error, reload: () => setAttempt((n) => n + 1) };
}

/**
 * The chosen model id: a concrete model's id, or for a wildcard entry (`litellm:*`) the provider
 * plus the typed name. Null while nothing (or an empty name) is chosen.
 */
export function pickedModelId(models: readonly EmbeddingModel[], choice: string, wildcardName: string): string | null {
  const model = models.find((item) => item.id === choice);
  if (!model) return null;
  if (!model.wildcard) return model.id;
  const name = wildcardName.trim();
  return name ? `${model.provider}:${name}` : null;
}

export interface EmbeddingModelPickerProps {
  models: EmbeddingModel[];
  status: "loading" | "ready" | "error";
  error: string | null;
  onRetry: () => void;
  /** The selected entry's id (a wildcard entry's id is `provider:*`). */
  choice: string;
  onChoiceChange: (choice: string) => void;
  wildcardName: string;
  onWildcardNameChange: (name: string) => void;
  disabled?: boolean;
  label?: string;
  hint?: React.ReactNode;
  testId?: string;
  /** The wildcard name field's test id. */
  nameTestId?: string;
}

/** A select of the allowlisted embedding models, with a name field for a `provider:*` entry. */
export function EmbeddingModelPicker({
  models,
  status,
  error,
  onRetry,
  choice,
  onChoiceChange,
  wildcardName,
  onWildcardNameChange,
  disabled = false,
  label = "Embedding model",
  hint,
  testId = "knowledge-embedding-model",
  nameTestId,
}: EmbeddingModelPickerProps) {
  const id = useId();
  const chosen = models.find((item) => item.id === choice) ?? null;
  return (
    <div className="min-w-0 space-y-1">
      <label htmlFor={id} className={labelClass}>{label}</label>
      {status === "error" ? (
        <div role="alert" className="flex flex-wrap items-center gap-2 text-label text-destructive-text">
          <span className="min-w-0">{error ?? "Could not load the embedding models."}</span>
          <Button size="sm" onClick={onRetry}>Retry</Button>
        </div>
      ) : null}
      <Select
        id={id}
        data-testid={testId}
        value={choice}
        disabled={disabled || status !== "ready" || models.length === 0}
        onValueChange={(v) => onChoiceChange(v)}
        className={fieldClass}
        required
      >
        <option value="">{status === "loading" ? "Loading models…" : models.length === 0 ? "No embedding model is allowed" : "Choose a model"}</option>
        {models.map((model) => (
          <option key={model.id} value={model.id}>
            {model.wildcard ? `${model.provider}: any model (${model.dimensions} dims)` : `${model.id} (${model.dimensions} dims)`}
          </option>
        ))}
      </Select>
      {chosen?.wildcard ? (
        <div className="space-y-1 pt-1">
          <label htmlFor={`${id}-name`} className={labelClass}>Model name on {chosen.provider}</label>
          <input
            id={`${id}-name`}
            data-testid={nameTestId ?? "knowledge-embedding-model-name"}
            value={wildcardName}
            onChange={(event) => onWildcardNameChange(event.target.value)}
            placeholder="text-embedding-3-small"
            disabled={disabled}
            className={`${fieldClass} font-mono`}
            required
          />
        </div>
      ) : null}
      <p className={hintClass}>
        {hint ?? "Fixed for the knowledge base; changing it later re-embeds every section (Reindex)."}
        {status === "ready" && models.length === 0 ? " Set EAP_ALLOWED_EMBEDDING_MODELS on the API to allow one." : null}
      </p>
    </div>
  );
}
