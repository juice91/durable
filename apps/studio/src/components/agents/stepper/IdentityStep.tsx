"use client";

import React from "react";
import { stepForErrorMessage, stepStatus } from "../specSections";
import { numberProblems, TEMPERATURE_MAX, TEMPERATURE_MIN, type EditorDraft } from "./editorDraft";
import { fieldClass, labelClass } from "./fieldStyles";
import type { EditorStepDefinition, MissingField, StepProps } from "./types";

// Polite and persistent, like the instructions' placeholder checks.
const problemClass = "mt-1 text-caption text-destructive-text";

/** Step 1: the agent's name, model and model settings. (Limits and fallback models: AC.13.) */
export function IdentityStep({ draft, onChange }: StepProps) {
  const problems = numberProblems(draft);
  return (
    <div className="space-y-4">
      <div>
        <label htmlFor="name" className={labelClass}>
          Name
        </label>
        <input id="name" type="text" value={draft.name} onChange={(e) => onChange({ name: e.target.value })} className={fieldClass} required />
      </div>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="min-w-0">
          <label htmlFor="provider" className={labelClass}>
            Model provider
          </label>
          <input id="provider" type="text" value={draft.provider} onChange={(e) => onChange({ provider: e.target.value })} className={fieldClass} required />
        </div>
        <div className="min-w-0">
          <label htmlFor="modelName" className={labelClass}>
            Model name
          </label>
          <input id="modelName" type="text" value={draft.modelName} onChange={(e) => onChange({ modelName: e.target.value })} className={fieldClass} required />
        </div>
      </div>

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
    </div>
  );
}

export const identityStep: EditorStepDefinition = {
  id: "identity",
  label: "Identity and model",
  Component: IdentityStep,
  status: (spec) => stepStatus("identity", spec)!,
  ownsError: (message) => stepForErrorMessage(message) === "identity",
  missing: (draft: EditorDraft): MissingField[] => {
    const out: MissingField[] = [];
    if (!draft.name.trim()) out.push({ step: "identity", fieldId: "name" });
    if (!draft.provider.trim()) out.push({ step: "identity", fieldId: "provider" });
    if (!draft.modelName.trim()) out.push({ step: "identity", fieldId: "modelName" });
    const problems = numberProblems(draft);
    if (problems.temperature) out.push({ step: "identity", fieldId: "temperature", message: problems.temperature });
    if (problems.maxTokens) out.push({ step: "identity", fieldId: "maxTokens", message: problems.maxTokens });
    return out;
  },
};
