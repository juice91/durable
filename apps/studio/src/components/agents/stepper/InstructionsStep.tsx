"use client";

import React from "react";
import { placeholderErrors, stepForErrorMessage, stepStatus } from "../specSections";
import { fieldClass, labelClass } from "./fieldStyles";
import type { EditorStepDefinition, StepProps } from "./types";
import { readableError } from "@/components/agents/readableError";

/** Step 2: the instructions, with `{{context.<name>}}` placeholder hints and checks. */
export function InstructionsStep({ draft, onChange, errors, services }: StepProps) {
  const { instructions, contextFields } = draft;
  const problems = placeholderErrors(instructions, contextFields);
  const { serverError, target } = errors;
  return (
    <div>
      <label htmlFor="instructions" className={labelClass}>
        Instructions
      </label>
      <textarea
        id="instructions"
        ref={services.instructionsRef}
        aria-invalid={problems.length > 0}
        aria-describedby="placeholder-hints instructions-problems"
        value={instructions}
        onChange={(e) => onChange({ instructions: e.target.value })}
        rows={12}
        className={fieldClass}
        required
      />
      <p id="placeholder-hints" data-testid="placeholder-hints" className="mt-1 text-caption text-muted-foreground">
        {contextFields.length > 0 ? (
          <>
            Context placeholders:{" "}
            {contextFields.map((field, i) => (
              <React.Fragment key={`${field.name}-${i}`}>
                {i > 0 ? ", " : null}
                <code className="font-mono">{`{{context.${field.name}}}`}</code>
              </React.Fragment>
            ))}
          </>
        ) : (
          <>
            Add request context fields to use <code className="font-mono">{"{{context.<name>}}"}</code> placeholders.
          </>
        )}
      </p>
      {/* Polite and persistent: announced when the problems change, not on every keystroke. */}
      <div id="instructions-problems" aria-live="polite">
        {problems.map((problem) => (
          <p key={problem} data-testid="placeholder-error" className="mt-1 text-caption text-destructive-text">
            {problem}
          </p>
        ))}
      </div>
      {target?.kind === "instructions" && serverError && !problems.some((p) => serverError.endsWith(p)) ? (
        <p role="alert" data-testid="instructions-server-error" className="mt-1 text-caption text-destructive-text">
          {readableError(serverError)}
        </p>
      ) : null}
    </div>
  );
}

export const instructionsStep: EditorStepDefinition = {
  id: "instructions",
  label: "Instructions",
  Component: InstructionsStep,
  status: (spec) => stepStatus("instructions", spec)!,
  ownsError: (message) => stepForErrorMessage(message) === "instructions",
  missing: (draft) => (draft.instructions.trim() ? [] : [{ step: "instructions", fieldId: "instructions" }]),
};
