import type React from "react";
import type { AgentSpec } from "@/lib/api";
import type { ErrorTarget, StepStatus } from "../specSections";
import type { EditorDraft } from "./editorDraft";

/** A field that blocks saving: empty but required, or invalid. The save opens `step` and focuses it. */
export interface MissingField {
  step: string;
  /** The input's element id. */
  fieldId: string;
  /** Shown as the field's validity message; the browser's own "required" message when absent. */
  message?: string;
}

/** The last save or publish error, as a step sees it. */
export interface StepErrors {
  /** The API message, or null. */
  serverError: string | null;
  /** Where the message points (`errorTarget`), or null. */
  target: ErrorTarget;
}

/** Shared editor services a step may use (the instructions box is on one step, its inserter on another). */
export interface StepServices {
  /** The instructions textarea (step "instructions"). */
  instructionsRef: React.RefObject<HTMLTextAreaElement | null>;
  /** Inserts `{{context.<name>}}` at the instructions caret. */
  insertPlaceholder: (fieldName: string) => void;
}

/**
 * What every step component receives.
 *
 * Extension rules for a new step:
 * - Edit the built-in fields (name, model, tools, processors, context fields...) only through
 *   `onChange`.
 * - Edit spec keys the built-in form has no field for (a later step's `memory`, `knowledge`,
 *   `output`) through `onSpecChange`, which writes `draft.extra`. Keys the form owns
 *   (`FORM_OWNED_KEYS` in editorDraft.ts) are refused: an error outside production, a console
 *   warning and no change in production.
 * - Read the current value from `spec` (what the editor would save).
 */
export interface StepProps {
  draft: EditorDraft;
  /** The spec the editor would save now (read-only). */
  spec: AgentSpec;
  onChange: (patch: Partial<EditorDraft>) => void;
  /** Sets top-level spec keys (`undefined` removes one). */
  onSpecChange: (patch: Record<string, unknown>) => void;
  errors: StepErrors;
  services: StepServices;
}

/**
 * One editor step. Adding a step is a new file exporting one of these plus one entry in
 * `registry.ts`; nothing else changes.
 */
export interface EditorStepDefinition {
  /** The `?step=` value; stable (Config panel links and bookmarks use it). */
  id: string;
  /** The nav label and the step's heading ("Identity and model"). */
  label: string;
  Component: React.ComponentType<StepProps>;
  /** The nav status line and check, from the spec the editor would save. */
  status: (spec: AgentSpec) => StepStatus;
  /**
   * Whether a save or publish error message belongs to this step (the editor then opens it and shows
   * the message there). Match the API's message prefix for this step's keys only, so steps do not
   * compete for a message.
   */
  ownsError: (message: string) => boolean;
  /** Fields that block saving (required and empty, or invalid), in the step's field order. */
  missing?: (draft: EditorDraft, spec: AgentSpec) => MissingField[];
}
