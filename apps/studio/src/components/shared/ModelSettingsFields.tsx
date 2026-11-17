"use client";

import React from "react";
import { fieldFor, settingProblem, type ModelSettingKey, type ModelSettingsText } from "@/lib/modelSettings";
import { fieldClass, labelClass } from "@/components/agents/stepper/fieldStyles";

const problemClass = "mt-1 min-h-4 text-caption text-destructive-text";

export interface ModelSettingsFieldsProps {
  /** The settings to show, in order. */
  keys: readonly ModelSettingKey[];
  values: Partial<ModelSettingsText>;
  onChange: (key: ModelSettingKey, value: string) => void;
  /** The element id of a field's input (its label points at it; `<id>-problem` holds its message). */
  idFor: (key: ModelSettingKey) => string;
  /** A message from the API for a field (shown when the typed value has no problem of its own). */
  serverProblems?: Partial<Record<ModelSettingKey, string>>;
  className?: string;
}

/**
 * Number inputs for model settings: an empty field is unset. Each has a label naming its range, a
 * hint, and a polite inline message (`aria-invalid` and `aria-describedby` on the input) when the
 * typed value is outside the API's range.
 */
export function ModelSettingsFields({ keys, values, onChange, idFor, serverProblems, className }: ModelSettingsFieldsProps) {
  return (
    <div className={className ?? "grid grid-cols-1 gap-4 sm:grid-cols-2"}>
      {keys.map((key) => {
        const field = fieldFor(key);
        const id = idFor(key);
        const problem = settingProblem(key, values[key] ?? "") ?? serverProblems?.[key] ?? null;
        return (
          <div key={key} className="min-w-0">
            <label htmlFor={id} className={labelClass}>
              {field.label} ({field.range})
            </label>
            <input
              id={id}
              type="number"
              inputMode={field.integer ? "numeric" : "decimal"}
              min={field.min}
              max={field.max}
              step={field.step}
              value={values[key] ?? ""}
              onChange={(event) => onChange(key, event.target.value)}
              aria-invalid={problem ? true : undefined}
              aria-describedby={`${id}-hint ${id}-problem`}
              className={fieldClass}
            />
            <p id={`${id}-hint`} className="mt-1 text-caption text-muted-foreground">
              {field.hint}
            </p>
            <p id={`${id}-problem`} aria-live="polite" data-testid={`${id}-problem`} className={problemClass}>
              {problem ?? ""}
            </p>
          </div>
        );
      })}
    </div>
  );
}
