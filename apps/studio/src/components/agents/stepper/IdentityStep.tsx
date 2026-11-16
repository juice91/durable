"use client";

import React from "react";
import { ModelPicker, useModelList } from "@/components/shared/ModelPicker";
import { fallbackProblems, limitsProblems } from "../agentCapabilities";
import { stepForErrorMessage, stepStatus } from "../specSections";
import { ModelSettingsFields } from "@/components/shared/ModelSettingsFields";
import { EXTRA_SETTING_KEYS, type ModelSettingKey } from "@/lib/modelSettings";
import { extraSettingProblems, numberProblems, TEMPERATURE_MAX, TEMPERATURE_MIN, type EditorDraft } from "./editorDraft";
import { fieldClass, labelClass } from "./fieldStyles";
import { LimitsFields, limitsFieldId } from "./LimitsFields";
import type { EditorStepDefinition, MissingField, StepProps } from "./types";

// Polite and persistent, like the instructions' placeholder checks.
const problemClass = "mt-1 text-caption text-destructive-text";

/** Element ids of the other model settings' inputs (camelCase of the spec key). */
export const EXTRA_SETTING_IDS: Record<(typeof EXTRA_SETTING_KEYS)[number], string> = {
  top_p: "topP",
  top_k: "topK",
  frequency_penalty: "frequencyPenalty",
  presence_penalty: "presencePenalty",
  seed: "seed",
  max_steps: "maxSteps",
  max_retries: "maxRetries",
};

/**
 * Step 1: the agent's name, model and model settings, then the collapsible "Limits and fallback
 * models" group (AC.13: `spec.limits` and `spec.fallback_models`, edited through `onSpecChange`).
 */
export function IdentityStep({ draft, spec, onChange, onSpecChange }: StepProps) {
  const problems = numberProblems(draft);
  const catalog = useModelList();
  return (
    <div className="space-y-4">
      <div>
        <label htmlFor="name" className={labelClass}>
          Name
        </label>
        <input id="name" type="text" value={draft.name} onChange={(e) => onChange({ name: e.target.value })} className={fieldClass} required />
      </div>

      <ModelPicker
        providerId="provider"
        modelId="modelName"
        provider={draft.provider}
        name={draft.modelName}
        models={catalog.models}
        loadError={catalog.error}
        required
        onChange={({ provider, name }) => onChange({ provider, modelName: name })}
      />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="min-w-0">
          <label htmlFor="temperature" className={labelClass}>
            Temperature (0–2)
          </label>
          <input
            id="temperature"
            type="number"
            min={TEMPERATURE_MIN}
            max={TEMPERATURE_MAX}
            step={0.1}
            value={draft.temperature}
            onChange={(e) => onChange({ temperature: e.target.value })}
            aria-invalid={problems.temperature ? true : undefined}
            aria-describedby="temperature-problem"
            className={fieldClass}
          />
          <p id="temperature-problem" aria-live="polite" data-testid="temperature-problem" className={problemClass}>
            {problems.temperature ?? ""}
          </p>
        </div>
        <div className="min-w-0">
          <label htmlFor="maxTokens" className={labelClass}>
            Max tokens (optional)
          </label>
          <input
            id="maxTokens"
            type="number"
            min={1}
            // An explicit max (the largest whole number the editor accepts): without one the field
            // was announced with a max below its min of 1.
            max={Number.MAX_SAFE_INTEGER}
            step={1}
            value={draft.maxTokens}
            onChange={(e) => onChange({ maxTokens: e.target.value })}
            aria-invalid={problems.maxTokens ? true : undefined}
            aria-describedby="maxTokens-problem"
            className={fieldClass}
          />
          <p id="maxTokens-problem" aria-live="polite" data-testid="maxTokens-problem" className={problemClass}>
            {problems.maxTokens ?? ""}
          </p>
        </div>
      </div>

      <fieldset className="space-y-3">
        <legend className={labelClass}>More model settings (optional)</legend>
        <p className="text-caption text-muted-foreground">
          Left empty, a setting is not sent and the provider&apos;s default applies. Some providers refuse some settings; publishing says which.
        </p>
        <ModelSettingsFields
          keys={EXTRA_SETTING_KEYS}
          values={draft.modelExtras}
          idFor={(key) => EXTRA_SETTING_IDS[key as (typeof EXTRA_SETTING_KEYS)[number]]}
          onChange={(key: ModelSettingKey, value) => onChange({ modelExtras: { ...draft.modelExtras, [key]: value } })}
        />
      </fieldset>

      <LimitsFields spec={spec} onSpecChange={onSpecChange} />
    </div>
  );
}

export const identityStep: EditorStepDefinition = {
  id: "identity",
  label: "Identity and model",
  Component: IdentityStep,
  status: (spec) => stepStatus("identity", spec)!,
  ownsError: (message) => stepForErrorMessage(message) === "identity",
  missing: (draft: EditorDraft, spec): MissingField[] => {
    const out: MissingField[] = [];
    if (!draft.name.trim()) out.push({ step: "identity", fieldId: "name" });
    if (!draft.provider.trim()) out.push({ step: "identity", fieldId: "provider" });
    if (!draft.modelName.trim()) out.push({ step: "identity", fieldId: "modelName" });
    const problems = numberProblems(draft);
    if (problems.temperature) out.push({ step: "identity", fieldId: "temperature", message: problems.temperature });
    if (problems.maxTokens) out.push({ step: "identity", fieldId: "maxTokens", message: problems.maxTokens });
    for (const [key, message] of Object.entries(extraSettingProblems(draft))) {
      out.push({ step: "identity", fieldId: EXTRA_SETTING_IDS[key as (typeof EXTRA_SETTING_KEYS)[number]], message });
    }
    for (const problem of limitsProblems(spec)) out.push({ step: "identity", fieldId: limitsFieldId(problem.field), message: problem.message });
    // The problem fields are the element ids already: `fallback-<i>` (fallbackFieldId) or `fallback-add`.
    for (const problem of fallbackProblems(spec)) out.push({ step: "identity", fieldId: problem.field, message: problem.message });
    return out;
  },
};
