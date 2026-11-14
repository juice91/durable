"use client";

import React from "react";
import { ContextFieldsEditor } from "../ContextFieldsEditor";
import { placeholders, stepForErrorMessage, stepStatus } from "../specSections";
import type { EditorStepDefinition, StepProps } from "./types";

/** Step 4: the request context fields (values are set per run, in the chat's context popover). */
export function RequestContextStep({ draft, onChange, errors, services }: StepProps) {
  return (
    <ContextFieldsEditor
      value={draft.contextFields}
      onChange={(contextFields) => onChange({ contextFields })}
      usedNames={placeholders(draft.instructions)}
      onInsertPlaceholder={services.insertPlaceholder}
      sectionError={errors.target?.kind === "context_fields" ? errors.serverError : null}
    />
  );
}

export const requestContextStep: EditorStepDefinition = {
  id: "request-context",
  label: "Request context",
  Component: RequestContextStep,
  status: (spec) => stepStatus("request-context", spec)!,
  ownsError: (message) => stepForErrorMessage(message) === "request-context",
};
