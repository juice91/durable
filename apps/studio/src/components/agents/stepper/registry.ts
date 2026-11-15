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
import { outputStep } from "./OutputStep";

/**
 * The top-level spec keys each built-in step owns, for routing a 422 by its first `detail[].loc`
 * (a step file may declare its own `specKeys`; these are attached here so the built-in steps keep
 * their files). Together they are the form-owned keys, so a location in one of them is never
 * claimed by a later step's message matcher.
 */
const CORE_SPEC_KEYS: Record<string, readonly string[]> = {
  // AC.13: the Identity step's "Limits and fallback models" group and the Tools step's approvals.
  identity: ["name", "model", "model_settings", "limits", "fallback_models"],
  instructions: ["instructions"],
  tools: ["tools", "mcp_tools", "approvals"],
  "request-context": ["context_fields"],
  processors: ["input_processors", "output_processors"],
};

function withSpecKeys(step: EditorStepDefinition): EditorStepDefinition {
  return step.specKeys || !CORE_SPEC_KEYS[step.id] ? step : { ...step, specKeys: CORE_SPEC_KEYS[step.id] };
}

export const EDITOR_STEPS: readonly EditorStepDefinition[] = [
  withSpecKeys(identityStep),
  withSpecKeys(instructionsStep),
  withSpecKeys(toolsStep),
  withSpecKeys(requestContextStep),
  withSpecKeys(processorsStep),
  // RM.17: steps 6 and 7.
  knowledgeStep,
  memoryStep,
  // AC.13: step 8.
  outputStep,
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
 * The step a save or publish error belongs to, or null when no step claims it. A 422's first
 * location decides when a step's `specKeys` owns its key (the message's first part is about that
 * location, and later parts never pull it to another step); otherwise the message text
 * (`ownsError`). `steps` without `specKeys` (a caller's own list) get the built-in keys.
 */
export function stepForError(
  message: string | null | undefined,
  steps: readonly EditorStepDefinition[] = EDITOR_STEPS,
  locations: readonly (readonly (string | number)[])[] = [],
): string | null {
  if (!message) return null;
  const first = locations[0];
  const key = first ? specKeyOfLocation(first) : null;
  if (key) {
    const owner = stepOwningKey(key, steps);
    if (owner) return owner.id;
  }
  // A message without a location that starts with a spec path (`output.json_schema: ...`,
  // `Value error, fallback_models[1]: ...`) belongs to the step owning that top-level key: the most
  // specific match, before any step's looser text matcher can claim it.
  const pathKey = /^(?:Value error, )?([a-z_]+)(?:[.[]|:)/.exec(message)?.[1];
  const pathOwner = pathKey ? stepOwningKey(pathKey, steps) : undefined;
  if (pathOwner) return pathOwner.id;
  return steps.find((step) => step.ownsError(message))?.id ?? null;
}

/** The step whose spec keys (its own `specKeys`, or the built-in ones) include `key`. */
function stepOwningKey(key: string, steps: readonly EditorStepDefinition[]): EditorStepDefinition | undefined {
  return steps.find((step) => (step.specKeys ?? CORE_SPEC_KEYS[step.id])?.includes(key));
}

/** Every step's blocking fields (`missing`), in step order. */
export function missingFields(draft: EditorDraft, spec: AgentSpec, steps: readonly EditorStepDefinition[] = EDITOR_STEPS): MissingField[] {
  return steps.flatMap((step) => step.missing?.(draft, spec) ?? []);
}
