"use client";

/**
 * The Identity step's "Limits and fallback models" group (AC.13): per-run usage caps (`spec.limits`)
 * and up to three fallback models (`spec.fallback_models`). Collapsed until opened; always open while
 * either is set, so a field the save check points at is on screen. Tokens only.
 */
import React, { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Plus, X } from "lucide-react";
import type { AgentSpec } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import {
  costProblem,
  fallbackModelsOf,
  fallbackModelsValue,
  fallbackProblems,
  LIMIT_FIELDS,
  limitsOf,
  limitsProblems,
  limitsValue,
  MAX_COST_USD,
  MAX_FALLBACK_MODELS,
  MIN_COST_USD,
  normaliseUsd,
  type LimitsValue,
  type ModelRefValue,
} from "../agentCapabilities";
import { fetchModels, type ModelOption } from "../specSections";
import { fieldClass, labelClass } from "./fieldStyles";
import { AllowlistSelect, FieldProblem, groupClass, hintClass, legendClass, NumberField, type AllowlistOption } from "./knowledgeMemoryFields";

/** Element id of a limits field (`limits-<key>`). */
export const limitsFieldId = (key: string) => `limits-${key}`;
/** Element id of fallback model row `index` (its select). */
export const fallbackFieldId = (index: number) => `fallback-${index}`;

export const LIMITS_HELP =
  "Caps on one run (a chat turn, a workflow step, an eval case). Requests, tool calls and tokens count the agent's own model calls; " +
  "the cost limit also counts this turn's input moderation and retrieval embeddings. Memory work after the answer is never counted.";

function chatOptions(models: ModelOption[]): AllowlistOption[] {
  return models.map((m) => ({
    id: m.id,
    provider: m.provider,
    name: m.name,
    wildcard: m.wildcard,
    label: m.wildcard ? `${m.provider}: any model (type a name)` : `${m.id}${m.description ? ` — ${m.description}` : ""}`,
  }));
}

/**
 * The models fallback row `index` may pick: the allowlist without the agent's own model and without
 * models other rows already use (no duplicates). Wildcard entries stay (their names are checked).
 * The row's current model always stays listed, even when it now duplicates another row or the
 * primary, so it is not shown as "not in the allowlist"; its real problem is shown under the row.
 */
export function fallbackOptions(options: AllowlistOption[], spec: AgentSpec, index: number): AllowlistOption[] {
  const models = fallbackModelsOf(spec);
  const own = models[index] ? `${models[index].provider}:${models[index].name}` : "";
  const primary = `${spec.model.provider}:${spec.model.name}`;
  const taken = new Set(models.filter((_, i) => i !== index).map((m) => `${m.provider}:${m.name}`));
  return options.filter((option) => option.wildcard || option.id === own || (option.id !== primary && !taken.has(option.id)));
}

export function LimitsFields({ spec, onSpecChange }: { spec: AgentSpec; onSpecChange: (patch: Record<string, unknown>) => void }) {
  const limits = limitsOf(spec);
  const fallbacks = fallbackModelsOf(spec);
  const hasValues = Boolean(limits) || fallbacks.length > 0;
  const [userOpen, setUserOpen] = useState(false);
  const open = userOpen || hasValues;
  const [models, setModels] = useState<ModelOption[] | null>(null);
  const [modelsError, setModelsError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || models) return;
    let cancelled = false;
    Promise.resolve()
      .then(() => fetchModels())
      .then((items) => {
        if (!cancelled) setModels(items ?? []);
      })
      .catch((error: unknown) => {
        if (!cancelled) setModelsError(error instanceof Error ? error.message : "Failed to load models");
      });
    return () => {
      cancelled = true;
    };
  }, [open, models]);

  const problems = [...limitsProblems(spec), ...fallbackProblems(spec)];
  const problemFor = (field: string) => problems.find((p) => p.field === field)?.message ?? null;
  const setLimit = (key: keyof LimitsValue, value: unknown) => onSpecChange({ limits: limitsValue({ ...(limits ?? {}), [key]: value }) });
  const writeFallbacks = (next: ModelRefValue[]) => onSpecChange({ fallback_models: fallbackModelsValue(next) });
  const options = models ? chatOptions(models) : null;
  const canAdd = fallbacks.length < MAX_FALLBACK_MODELS;
  const costId = limitsFieldId("max_cost_usd");
  const storedCost = typeof limits?.max_cost_usd === "number" ? String(limits.max_cost_usd) : typeof limits?.max_cost_usd === "string" ? limits.max_cost_usd : "";
  // The cost field keeps the text as typed ("0.50") while the draft holds the server's normal form
  // ("0.5"), so a saved draft is not dirty against the stored spec. A change from outside (a reload,
  // another value) resets the text.
  const [costText, setCostText] = useState(storedCost);
  const emittedCost = useRef(storedCost);
  useEffect(() => {
    if (storedCost !== emittedCost.current) {
      emittedCost.current = storedCost;
      setCostText(storedCost);
    }
  }, [storedCost]);
  const changeCost = (text: string) => {
    const next = text.trim() === "" ? undefined : normaliseUsd(text.trim());
    setCostText(text);
    emittedCost.current = next ?? "";
    setLimit("max_cost_usd", next);
  };

  return (
    <div data-testid="limits-fallback-group" className="min-w-0 rounded-md border border-border">
      <button
        type="button"
        aria-expanded={open}
        aria-controls="limits-fallback-content"
        // While limits or fallbacks are set the group stays open (the save check may point into it).
        onClick={() => setUserOpen((value) => (hasValues ? true : !value))}
        className="flex w-full min-w-0 items-center gap-2 rounded-md px-3 py-2 text-left text-label font-semibold text-foreground hover:bg-fill-hover focus:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        {open ? <ChevronDown aria-hidden="true" className="size-icon shrink-0" /> : <ChevronRight aria-hidden="true" className="size-icon shrink-0" />}
        <span className="min-w-0 flex-1 break-words">Limits and fallback models</span>
        {!open ? <span className="text-caption font-normal text-muted-foreground">Optional</span> : null}
      </button>

      {open ? (
        <div id="limits-fallback-content" className="min-w-0 space-y-4 px-3 pb-3">
          <fieldset className={groupClass}>
            <legend className={legendClass}>Limits per run</legend>
            <p className={hintClass}>{LIMITS_HELP}</p>
            <div className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2">
              {LIMIT_FIELDS.map(({ key, label, max }) => (
                <NumberField
                  key={key}
                  id={limitsFieldId(key)}
                  label={`${label} (1–${max.toLocaleString("en-US")})`}
                  value={limits?.[key]}
                  min={1}
                  max={max}
                  placeholder="No limit"
                  onChange={(value) => setLimit(key, value)}
                  problem={problemFor(key)}
                />
              ))}
              <div className="min-w-0">
                <label htmlFor={costId} className={labelClass}>
                  Cost (USD, {MIN_COST_USD}–{MAX_COST_USD})
                </label>
                <input
                  id={costId}
                  type="text"
                  inputMode="decimal"
                  placeholder="No limit"
                  value={costText}
                  onChange={(e) => changeCost(e.target.value)}
                  aria-invalid={costProblem(limits?.max_cost_usd) ? true : undefined}
                  aria-describedby={`${costId}-hint ${costId}-problem`}
                  className={fieldClass}
                />
                <p id={`${costId}-hint`} className={hintClass}>
                  Unpriced models count as $0; publishing warns about them.
                </p>
                <FieldProblem id={`${costId}-problem`} message={problemFor("max_cost_usd")} />
              </div>
            </div>
          </fieldset>

          <fieldset className={groupClass} data-testid="fallback-models">
            <legend className={legendClass}>Fallback models</legend>
            <p className={hintClass}>
              When the model fails with a provider error, the next one answers, in this order. A model chosen for a single chat turn turns
              fallback off for that turn.
            </p>
            {fallbacks.length === 0 ? <p className="text-caption text-muted-foreground">None</p> : null}
            <ol className="min-w-0 space-y-3">
              {fallbacks.map((model, index) => (
                <li key={index} data-testid="fallback-row" className="flex min-w-0 items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <AllowlistSelect
                      id={fallbackFieldId(index)}
                      label={`Fallback ${index + 1}`}
                      options={options ? fallbackOptions(options, spec, index) : null}
                      loadError={modelsError}
                      provider={model.provider}
                      name={model.name}
                      onChange={(provider, name) => writeFallbacks(fallbacks.map((m, i) => (i === index ? { provider, name } : m)))}
                      problem={problemFor(fallbackFieldId(index))}
                    />
                  </div>
                  <IconButton
                    label={`Remove fallback ${index + 1}`}
                    icon={<X aria-hidden="true" />}
                    tooltip={false}
                    className="mt-6"
                    onClick={() => writeFallbacks(fallbacks.filter((_, i) => i !== index))}
                  />
                </li>
              ))}
            </ol>
            <Button
              id="fallback-add"
              size="sm"
              disabled={!canAdd}
              onClick={() => writeFallbacks([...fallbacks, { provider: "", name: "" }])}
            >
              <Plus aria-hidden="true" />
              Add fallback model
            </Button>
            {!canAdd ? <p className={hintClass}>At most {MAX_FALLBACK_MODELS} fallback models.</p> : null}
          </fieldset>
        </div>
      ) : null}
    </div>
  );
}
