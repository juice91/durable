"use client";

import React, { useEffect, useState } from "react";
import { fetchModels, type ModelOption } from "@/components/agents/specSections";
import { Badge } from "@/components/ui/Badge";
import { Combobox, type ComboboxOption } from "@/components/ui/Combobox";
import { fetchProviders, PROVIDER_STATE_LABELS, PROVIDER_STATE_VARIANTS, type ProviderStatus } from "@/lib/providers";

const labelClass = "mb-1 block text-label font-medium text-foreground";
const fieldClass =
  "w-full min-w-0 rounded-md border border-input bg-surface px-3 py-2 text-body text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
const hintClass = "mt-1 text-caption text-muted-foreground";
const problemClass = "mt-1 text-caption text-destructive-text";

/** The provider statuses (`GET /providers`), loaded once per mount. A failure leaves them null: the picker then shows no status. */
export function useProviderStatuses(enabled = true): ProviderStatus[] | null {
  const [providers, setProviders] = useState<ProviderStatus[] | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => fetchProviders())
      .then((items) => {
        if (!cancelled) setProviders(items);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return providers;
}

/** The model list (`GET /models`), loaded once per mount. `models` is null while loading or after an error. */
export function useModelList(enabled = true): { models: ModelOption[] | null; error: string | null } {
  const [state, setState] = useState<{ models: ModelOption[] | null; error: string | null }>({ models: null, error: null });
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => fetchModels())
      .then((items) => {
        if (!cancelled) setState({ models: Array.isArray(items) ? items : [], error: null });
      })
      .catch((reason: unknown) => {
        if (!cancelled) setState({ models: null, error: reason instanceof Error ? reason.message : "Failed to load models" });
      });
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return state;
}

export interface ModelPickerProps {
  /** Element ids of the two controls (a "focus this field" jump targets them). */
  providerId: string;
  modelId: string;
  providerLabel?: string;
  modelLabel?: string;
  provider: string;
  name: string;
  onChange: (next: { provider: string; name: string }) => void;
  /** The `GET /models` items; null while loading (or when they could not be loaded: see `loadError`). */
  models: ModelOption[] | null;
  loadError?: string | null;
  /** Provider statuses; omit to load them here. */
  providers?: ProviderStatus[] | null;
  disabled?: boolean;
  required?: boolean;
  /** A problem with the model, shown under the model field. */
  problem?: string | null;
  problemId?: string;
  hint?: string;
}

function providerOptionText(id: string, status: ProviderStatus | undefined): string {
  if (!status) return id;
  const detail = status.status === "ok" ? `${PROVIDER_STATE_LABELS.ok}, ${status.model_count} models` : PROVIDER_STATE_LABELS[status.status];
  return `${id} (${detail})`;
}

/**
 * A model provider select (with each provider's status) and a searchable model combobox filtered by
 * that provider. The lists come from `GET /models`. Free text is the fallback only when a list is
 * unavailable: models that could not be loaded, a provider whose model list could not be discovered
 * (a wildcard entry), or a wildcard provider that lists nothing. A stored value the list no longer
 * offers stays selected and is marked "not listed".
 */
export function ModelPicker({
  providerId,
  modelId,
  providerLabel = "Model provider",
  modelLabel = "Model name",
  provider,
  name,
  onChange,
  models,
  loadError = null,
  providers: providersProp,
  disabled = false,
  required = false,
  problem = null,
  problemId,
  hint,
}: ModelPickerProps) {
  const loaded = useProviderStatuses(providersProp === undefined);
  const providers = providersProp === undefined ? loaded : providersProp;
  const statusOf = (id: string) => providers?.find((p) => p.id === id);

  // Focus is inside the free-text fields. While it is, they stay in place even when the list arrives,
  // so the user keeps focus and the text they are typing; the list replaces them once focus leaves.
  const [editingFreeText, setEditingFreeText] = useState(false);

  const providerIds = Array.from(new Set((models ?? []).map((m) => m.provider)));
  const listAvailable = models !== null && providerIds.length > 0;
  const describe = (hasHint: boolean) =>
    [hasHint ? `${modelId}-hint` : null, problem ? (problemId ?? `${modelId}-problem`) : null].filter(Boolean).join(" ") || undefined;

  if (!listAvailable || (editingFreeText && !disabled)) {
    const why = listAvailable
      ? "The model list has loaded. It replaces these fields when you leave them."
      : loadError
      ? `The model list could not be loaded (${loadError}). Type the provider and model.`
      : models === null
        ? "Loading the model list…"
        : "No model list is available. Type the provider and model.";
    return (
      <div
        className="min-w-0 space-y-1"
        data-testid="model-picker"
        data-picker={modelId}
        data-mode="free-text"
        onFocus={() => setEditingFreeText(true)}
        onBlur={(e) => {
          // Moving between the two fields keeps the hold; leaving the picker releases it.
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setEditingFreeText(false);
        }}
      >
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div className="min-w-0">
            <label htmlFor={providerId} className={labelClass}>{providerLabel}</label>
            <input id={providerId} type="text" value={provider} disabled={disabled} required={required} onChange={(e) => onChange({ provider: e.target.value, name })} className={fieldClass} />
          </div>
          <div className="min-w-0">
            <label htmlFor={modelId} className={labelClass}>{modelLabel}</label>
            <input
              id={modelId}
              type="text"
              value={name}
              disabled={disabled}
              required={required}
              aria-invalid={problem ? true : undefined}
              aria-describedby={describe(true)}
              onChange={(e) => onChange({ provider, name: e.target.value })}
              className={fieldClass}
            />
          </div>
        </div>
        <p id={`${modelId}-hint`} className={hintClass} data-testid="model-picker-hint" role={loadError && !listAvailable ? "alert" : undefined}>{why}{hint ? ` ${hint}` : ""}</p>
        {problem ? <p id={problemId ?? `${modelId}-problem`} className={problemClass}>{problem}</p> : null}
      </div>
    );
  }

  const ofProvider = (models ?? []).filter((m) => m.provider === provider);
  const wildcard = ofProvider.find((m) => m.wildcard);
  const concrete = ofProvider.filter((m) => !m.wildcard);
  const providerListed = providerIds.includes(provider);
  const status = provider ? statusOf(provider) : undefined;
  const modelListed = concrete.some((m) => m.name === name);

  const modelOptions: ComboboxOption[] = concrete.map((m) => ({ value: m.name, label: m.name, description: m.description ?? undefined }));
  if (name && !modelListed) modelOptions.unshift({ value: name, label: `${name} (not listed)` });

  const changeProvider = (next: string) => {
    const keep = (models ?? []).some((m) => m.provider === next && !m.wildcard && m.name === name);
    onChange({ provider: next, name: keep ? name : "" });
  };

  let modelHint = hint;
  if (provider && wildcard) {
    modelHint = wildcard.discovery_error
      ? `The model list for ${provider} is unavailable (${wildcard.discovery_error}). Type a model name.`
      : `Any model name on ${provider} is allowed. Type one.`;
  } else if (provider && concrete.length > 0 && !hint) {
    modelHint = `${concrete.length} ${concrete.length === 1 ? "model" : "models"} on ${provider}.`;
  }

  const describedBy = describe(Boolean(modelHint));
  return (
    <div className="min-w-0 space-y-1" data-testid="model-picker" data-picker={modelId} data-mode={wildcard ? "free-text" : "list"}>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="min-w-0">
          <label htmlFor={providerId} className={labelClass}>{providerLabel}</label>
          <select
            id={providerId}
            data-testid="model-picker-provider"
            value={provider}
            disabled={disabled}
            required={required}
            onChange={(e) => changeProvider(e.target.value)}
            className={fieldClass}
          >
            <option value="" disabled>Select a provider…</option>
            {provider && !providerListed ? <option value={provider}>{`${provider} (not in the allowlist)`}</option> : null}
            {providerIds.map((id) => (
              <option key={id} value={id}>{providerOptionText(id, statusOf(id))}</option>
            ))}
          </select>
          {status ? (
            <p className="mt-1 flex min-w-0 flex-wrap items-center gap-1.5 text-caption text-muted-foreground" data-testid="model-picker-status">
              <Badge variant={PROVIDER_STATE_VARIANTS[status.status]}>{PROVIDER_STATE_LABELS[status.status]}</Badge>
              {status.status === "ok" ? <span>{status.model_count} models</span> : status.error ? <span className="min-w-0 break-words">{status.error}</span> : null}
            </p>
          ) : null}
        </div>
        <div className="min-w-0">
          <label htmlFor={modelId} className={labelClass}>{modelLabel}</label>
          {provider && wildcard ? (
            <>
              <input
                id={modelId}
                type="text"
                list={concrete.length > 0 ? `${modelId}-suggestions` : undefined}
                value={name}
                disabled={disabled}
                required={required}
                aria-invalid={problem ? true : undefined}
                aria-describedby={describedBy}
                onChange={(e) => onChange({ provider, name: e.target.value })}
                className={fieldClass}
              />
              {concrete.length > 0 ? (
                <datalist id={`${modelId}-suggestions`}>
                  {concrete.map((m) => <option key={m.id} value={m.name} />)}
                </datalist>
              ) : null}
            </>
          ) : (
            <Combobox
              id={modelId}
              testId="model-picker-model"
              label={modelLabel}
              options={modelOptions}
              value={name || null}
              onValueChange={(next) => onChange({ provider, name: next })}
              placeholder={provider ? "Select a model…" : "Select a provider first"}
              searchPlaceholder={`Search ${provider || "models"}…`}
              emptyText="No matching model."
              disabled={disabled || !provider}
              invalid={Boolean(problem)}
              describedBy={describedBy}
              className="h-auto w-full justify-between border border-input bg-surface px-3 py-2 text-body font-normal text-foreground"
              contentClassName="w-[var(--radix-popover-trigger-width)] min-w-56 max-w-[var(--radix-popover-content-available-width)]"
            />
          )}
        </div>
      </div>
      {modelHint ? <p id={`${modelId}-hint`} className={hintClass} data-testid="model-picker-hint">{modelHint}</p> : null}
      {problem ? <p id={problemId ?? `${modelId}-problem`} className={problemClass}>{problem}</p> : null}
    </div>
  );
}
