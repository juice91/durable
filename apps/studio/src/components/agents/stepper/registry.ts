/**
 * The editor's steps, in order. **To add a step** (RM.17 Knowledge and Memory, AC.13 Output): write
 * `components/agents/stepper/<Name>Step.tsx` exporting a component taking `StepProps` and an
 * `EditorStepDefinition`, then add it to `EDITOR_STEPS` below. The nav, `?step=`, Previous / Next,
 * the status lines and error-to-step mapping all follow from this list.
 */
import type { AgentSpec } from "@/lib/api";
import type { EditorDraft } from "./editorDraft";
import type { EditorStepDefinition, MissingField } from "./types";
import { identityStep } from "./IdentityStep";
import { instructionsStep } from "./InstructionsStep";
import { toolsStep } from "./ToolsStep";
import { requestContextStep } from "./RequestContextStep";
import { processorsStep } from "./ProcessorsStep";
import { knowledgeStep } from "./KnowledgeStep";
import { memoryStep } from "./MemoryStep";

export const EDITOR_STEPS: readonly EditorStepDefinition[] = [
  identityStep,
  instructionsStep,
  toolsStep,
  requestContextStep,
  processorsStep,
  // RM.17: steps 6 and 7.
  knowledgeStep,
  memoryStep,
];

/** The first step: where the editor opens without (or with an unknown) `?step=`. */
export const FIRST_STEP_ID = EDITOR_STEPS[0].id;

/** The step with `id`, or undefined. */
export function editorStep(id: string | null | undefined, steps: readonly EditorStepDefinition[] = EDITOR_STEPS): EditorStepDefinition | undefined {
  return steps.find((step) => step.id === id);
}

/** A `?step=` value made valid: an unknown or missing id is the first step. */
export function normalizeStepId(id: string | null | undefined, steps: readonly EditorStepDefinition[] = EDITOR_STEPS): string {
  return editorStep(id, steps)?.id ?? steps[0].id;
}

/** The steps before and after `id` (null at the ends). */
export function neighbourSteps(
  id: string,
  steps: readonly EditorStepDefinition[] = EDITOR_STEPS,
): { previous: EditorStepDefinition | null; next: EditorStepDefinition | null } {
  const index = steps.findIndex((step) => step.id === id);
  return { previous: index > 0 ? steps[index - 1] : null, next: index >= 0 && index < steps.length - 1 ? steps[index + 1] : null };
}

/** The top-level spec key a 422 location points into (`["body", "spec", "memory", ...]` -> "memory"). */
export function specKeyOfLocation(loc: readonly (string | number)[]): string | null {
  let i = 0;
  if (loc[i] === "body") i += 1;
  if (loc[i] === "spec") i += 1;
  else if (i > 0) return null; // a body field that is not the spec
  const key = loc[i];
  return typeof key === "string" ? key : null;
}

/**
 * The step a save or publish error belongs to, or null when no step claims it. With the 422's
 * locations, the first one inside a step's `specKeys` decides; otherwise the message text
 * (`ownsError`).
 */
export function stepForError(
  message: string | null | undefined,
  steps: readonly EditorStepDefinition[] = EDITOR_STEPS,
  locations: readonly (readonly (string | number)[])[] = [],
): string | null {
  if (!message) return null;
  for (const loc of locations) {
    const key = specKeyOfLocation(loc);
    const owner = key ? steps.find((step) => step.specKeys?.includes(key)) : undefined;
    if (owner) return owner.id;
  }
  return steps.find((step) => step.ownsError(message))?.id ?? null;
}

/** Every step's blocking fields (`missing`), in step order. */
export function missingFields(draft: EditorDraft, spec: AgentSpec, steps: readonly EditorStepDefinition[] = EDITOR_STEPS): MissingField[] {
  return steps.flatMap((step) => step.missing?.(draft, spec) ?? []);
}
