/**
 * The stepped editor's form state (`EditorDraft`) and its conversion to and from an `AgentSpec`.
 *
 * Saving an untouched draft gives back the same JSON, byte for byte: the saved spec keeps the stored
 * spec's key order and any keys the editor has no field for (they are carried over, not dropped), so
 * a later step can own them through `extra` without touching the built-in steps.
 */
import type { AgentSpec, McpToolRef } from "@/lib/api";
import { withMcpTools } from "@/lib/mcpServers";
import { EXTRA_SETTING_KEYS, settingsProblems, settingsText, type ModelSettingKey, type ModelSettingsText } from "@/lib/modelSettings";
import { canonicalJson, sectionsOf, withClientIds, withSpecSections, type ContextField, type EditableProcessor } from "../specSections";

export interface EditorDraft {
  name: string;
  instructions: string;
  provider: string;
  modelName: string;
  /** Built-in tool ids. */
  tools: string[];
  mcpTools: McpToolRef[];
  /** As typed: "" saves null. */
  temperature: string;
  maxTokens: string;
  /** The other model settings as typed (top_p, top_k, penalties, seed, max_steps, max_retries): "" leaves one out of the spec. */
  modelExtras: Pick<ModelSettingsText, (typeof EXTRA_SETTING_KEYS)[number]>;
  inputProcessors: EditableProcessor[];
  stepProcessors: EditableProcessor[];
  streamProcessors: EditableProcessor[];
  outputProcessors: EditableProcessor[];
  contextFields: ContextField[];
  /**
   * Top-level spec keys set by steps that edit the spec directly (later steps: knowledge, memory,
   * output). A value replaces the stored key; `undefined` removes it. Keys not named here keep their
   * stored value.
   */
  extra: Record<string, unknown>;
}

/** The form state for a stored spec (or an empty form). */
export function draftFromSpec(spec: AgentSpec | undefined): EditorDraft {
  const sections = sectionsOf(spec);
  return {
    name: spec?.name || "",
    instructions: spec?.instructions || "",
    provider: spec?.model.provider || "",
    modelName: spec?.model.name || "",
    tools: spec?.tools || [],
    mcpTools: spec?.mcp_tools || [],
    temperature: spec?.model_settings?.temperature?.toString() || "",
    maxTokens: spec?.model_settings?.max_tokens?.toString() || "",
    modelExtras: extrasFromSettings(spec?.model_settings),
    inputProcessors: withClientIds(sections.input_processors),
    stepProcessors: withClientIds(sections.step_processors),
    streamProcessors: withClientIds(sections.stream_processors),
    outputProcessors: withClientIds(sections.output_processors),
    contextFields: sections.context_fields,
    extra: {},
  };
}

function extrasFromSettings(settings: AgentSpec["model_settings"] | undefined): EditorDraft["modelExtras"] {
  const text = settingsText(settings);
  return Object.fromEntries(EXTRA_SETTING_KEYS.map((key) => [key, text[key]])) as EditorDraft["modelExtras"];
}

/** `value`'s keys in `like`'s order first, then its own new keys (top level only). */
function orderLike<T extends object>(value: T, like: object | undefined): T {
  if (!like) return value;
  const out: Record<string, unknown> = {};
  const source = value as Record<string, unknown>;
  for (const key of Object.keys(like)) if (key in source) out[key] = source[key];
  for (const key of Object.keys(source)) if (!(key in out)) out[key] = source[key];
  return out as T;
}

/**
 * The spec the editor saves. `base` is the stored spec: its key order and the keys the editor has
 * no field for are kept. `mcp_tools`, the processor lists and `context_fields` are left out entirely
 * when empty, so the spec hash stays stable for agents that do not use them.
 */
export function draftToSpec(draft: EditorDraft, base?: AgentSpec): AgentSpec {
  const stored = (base ?? {}) as Partial<AgentSpec>;
  // The newer settings are left out of the spec while empty (as the API leaves them out), so a spec
  // that never used them keeps its exact JSON and hash.
  const settings: Record<string, unknown> = {
    ...(stored.model_settings ?? {}),
    // Number(), not parseInt: an invalid value is refused before saving (`numberProblems`), never truncated.
    temperature: draft.temperature ? Number(draft.temperature) : null,
    max_tokens: draft.maxTokens ? Number(draft.maxTokens) : null,
  };
  for (const key of EXTRA_SETTING_KEYS) {
    const raw = draft.modelExtras[key].trim();
    if (raw === "") delete settings[key];
    else settings[key] = Number(raw);
  }
  const core = {
    ...(stored as Record<string, unknown>),
    spec_version: 1 as const,
    name: draft.name,
    instructions: draft.instructions,
    model: orderLike({ ...(stored.model ?? {}), provider: draft.provider, name: draft.modelName }, stored.model),
    tools: draft.tools,
    model_settings: orderLike(settings, stored.model_settings),
  } as AgentSpec;
  const spec = withSpecSections(withMcpTools(core, draft.mcpTools), {
    input_processors: draft.inputProcessors,
    step_processors: draft.stepProcessors,
    stream_processors: draft.streamProcessors,
    output_processors: draft.outputProcessors,
    context_fields: draft.contextFields,
  }) as unknown as Record<string, unknown>;
  for (const [key, value] of Object.entries(draft.extra)) {
    if (FORM_OWNED_KEYS.has(key)) continue; // refused by `checkExtraKeys`; never overwrite the form's keys
    if (value === undefined) delete spec[key];
    else spec[key] = value;
  }
  return orderLike(spec, base) as unknown as AgentSpec;
}

/** The draft would save something other than `base` (dirty-state tracking). */
export function draftDiffers(draft: EditorDraft, base: AgentSpec | undefined): boolean {
  return canonicalJson(draftToSpec(draft, base)) !== canonicalJson(draftToSpec(draftFromSpec(base), base));
}

/** Spec keys the built-in form writes; `extra` (a later step's `onSpecChange`) may not set them. */
export const FORM_OWNED_KEYS: ReadonlySet<string> = new Set([
  "spec_version",
  "name",
  "instructions",
  "model",
  "tools",
  "mcp_tools",
  "model_settings",
  "input_processors",
  "step_processors",
  "stream_processors",
  "output_processors",
  "context_fields",
]);

/**
 * Refuses `extra` keys the form owns: throws outside production (a step bug to fix), and in
 * production drops them with a console warning. Returns the patch without them.
 */
export function checkExtraKeys(patch: Record<string, unknown>): Record<string, unknown> {
  const owned = Object.keys(patch).filter((key) => FORM_OWNED_KEYS.has(key));
  if (owned.length === 0) return patch;
  const message = `onSpecChange cannot set ${owned.join(", ")}: the built-in steps own them (use onChange)`;
  if (process.env.NODE_ENV !== "production") throw new Error(message);
  console.warn(message);
  return Object.fromEntries(Object.entries(patch).filter(([key]) => !FORM_OWNED_KEYS.has(key)));
}

/** API limits (eap_schemas `ModelSettings`): temperature 0 to 2, max_tokens an integer above 0. */
export const TEMPERATURE_MIN = 0;
export const TEMPERATURE_MAX = 2;

/** Inline messages for the other model settings as typed, by key. */
export function extraSettingProblems(draft: Pick<EditorDraft, "modelExtras">): Partial<Record<ModelSettingKey, string>> {
  return settingsProblems(draft.modelExtras, EXTRA_SETTING_KEYS);
}

/** Inline messages for the number fields as typed (empty is fine: it saves null). */
export function numberProblems(draft: Pick<EditorDraft, "temperature" | "maxTokens">): { temperature?: string; maxTokens?: string } {
  const problems: { temperature?: string; maxTokens?: string } = {};
  if (draft.temperature.trim() !== "") {
    const value = Number(draft.temperature);
    if (!Number.isFinite(value) || value < TEMPERATURE_MIN || value > TEMPERATURE_MAX) problems.temperature = "Temperature must be a number from 0 to 2";
  }
  if (draft.maxTokens.trim() !== "") {
    const value = Number(draft.maxTokens);
    if (!Number.isSafeInteger(value) || value < 1) problems.maxTokens = "Max tokens must be a whole number of at least 1";
  }
  return problems;
}
