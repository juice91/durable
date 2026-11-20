"use client";

import React from "react";
import { Cpu } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import type { ModelOption } from "@/lib/metrics";
import { focusRing } from "@/components/ui/Button";
import { isWildcardChoice, modelPickerOptions, type ModelChoice } from "@/components/chat/runOptions";
import { ComposerChipSelect } from "./ComposerChip";

export const MODEL_ERROR_ID = "playground-model-error";

export interface ComposerModelChipProps {
  /** The target spec's own model, `provider:name`; null while unknown. */
  defaultModelId: string | null;
  models: ModelOption[];
  choice: ModelChoice;
  onChange: (choice: ModelChoice) => void;
  error: string | null;
  disabled?: boolean;
}

/** The chip's text: the effective model's name (the provider is in the menu). */
export function modelChipLabel(choice: ModelChoice, defaultModelId: string | null): string {
  if (choice.choice === "") return defaultModelId ? defaultModelId.slice(defaultModelId.indexOf(":") + 1) : "Model";
  if (isWildcardChoice(choice.choice)) return choice.customName.trim() || `${choice.choice.slice(0, -2)}: …`;
  return choice.choice.slice(choice.choice.indexOf(":") + 1);
}

/**
 * The composer's model chip: "Use version's model" (no override), each `GET /models` entry, and
 * "<provider>: other model…" for wildcard providers, which adds a free-text name field.
 */
export function ComposerModelChip({ defaultModelId, models, choice, onChange, error, disabled = false }: ComposerModelChipProps) {
  const pickerOptions = modelPickerOptions(models, defaultModelId);
  const wildcard = isWildcardChoice(choice.choice);
  // A choice the list no longer offers (e.g. models still loading) stays visible as itself.
  const choiceListed = choice.choice === "" || pickerOptions.some((option) => option.value === choice.choice);
  return (
    <>
      <ComposerChipSelect
        id="playground-model"
        testId="composer-chip-model"
        label="Model"
        display={modelChipLabel(choice, defaultModelId)}
        icon={<Cpu strokeWidth={ICON_STROKE_WIDTH} />}
        invalid={Boolean(error)}
        errorId={MODEL_ERROR_ID}
        value={choice.choice}
        disabled={disabled}
        onValueChange={(v) => onChange({ choice: v, customName: "" })}
      >
        <option value="">{defaultModelId ? `Use version's model (${defaultModelId})` : "Use version's model"}</option>
        {pickerOptions.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
        {!choiceListed ? <option value={choice.choice}>{choice.choice}</option> : null}
      </ComposerChipSelect>
      {wildcard ? (
        <input
          aria-label="Model name"
          placeholder="model name"
          className={cn("h-7 w-36 min-w-0 rounded-full border border-border bg-surface px-3 text-label text-foreground", focusRing)}
          value={choice.customName}
          disabled={disabled}
          onChange={(event) => onChange({ choice: choice.choice, customName: event.target.value })}
        />
      ) : null}
    </>
  );
}
