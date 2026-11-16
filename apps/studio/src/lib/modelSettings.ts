// Model settings the agent editor and the chat's settings popover edit (eap_schemas `ModelSettings`).
// Fields are kept as typed text (empty = unset), validated here with the API's ranges, and sent as
// numbers only when set, so an unset field is left out of the spec and the run options.

export type ModelSettingKey =
  | "temperature"
  | "max_tokens"
  | "top_p"
  | "top_k"
  | "frequency_penalty"
  | "presence_penalty"
  | "seed"
  | "max_steps"
  | "max_retries";

export interface ModelSettingField {
  key: ModelSettingKey;
  label: string;
  /** Shown after the label, e.g. "0 to 1". */
  range: string;
  integer: boolean;
  min: number;
  max: number;
  step: number;
  hint: string;
}

// API limits (packages/shared-schemas ModelSettings): keep in step with it.
export const MAX_STEPS_MAX = 200;
export const MAX_RETRIES_MAX = 10;

export const MODEL_SETTING_FIELDS: readonly ModelSettingField[] = [
  { key: "temperature", label: "Temperature", range: "0 to 2", integer: false, min: 0, max: 2, step: 0.1, hint: "Higher values give more varied answers." },
  { key: "max_tokens", label: "Max tokens", range: "1 or more", integer: true, min: 1, max: Number.MAX_SAFE_INTEGER, step: 1, hint: "Upper bound on the length of one answer." },
  { key: "top_p", label: "Top P", range: "0 to 1", integer: false, min: 0, max: 1, step: 0.05, hint: "Nucleus sampling: only the most likely tokens that add up to this probability." },
  { key: "top_k", label: "Top K", range: "1 or more", integer: true, min: 1, max: Number.MAX_SAFE_INTEGER, step: 1, hint: "Only the K most likely tokens are considered." },
  { key: "frequency_penalty", label: "Frequency penalty", range: "-2 to 2", integer: false, min: -2, max: 2, step: 0.1, hint: "Positive values discourage repeating the same words." },
  { key: "presence_penalty", label: "Presence penalty", range: "-2 to 2", integer: false, min: -2, max: 2, step: 0.1, hint: "Positive values encourage new topics." },
  { key: "seed", label: "Seed", range: "a whole number", integer: true, min: Number.MIN_SAFE_INTEGER, max: Number.MAX_SAFE_INTEGER, step: 1, hint: "The same seed asks the model for the same answer where it supports that." },
  { key: "max_steps", label: "Max steps", range: `1 to ${MAX_STEPS_MAX}`, integer: true, min: 1, max: MAX_STEPS_MAX, step: 1, hint: "Most model requests one run may make." },
  { key: "max_retries", label: "Max retries", range: `0 to ${MAX_RETRIES_MAX}`, integer: true, min: 0, max: MAX_RETRIES_MAX, step: 1, hint: "How often the agent retries a tool call or an answer that fails validation." },
];

/** The fields the agent editor shows beside temperature and max tokens. */
export const EXTRA_SETTING_KEYS = ["top_p", "top_k", "frequency_penalty", "presence_penalty", "seed", "max_steps", "max_retries"] as const;
export type ExtraSettingKey = (typeof EXTRA_SETTING_KEYS)[number];

export type ModelSettingsText = Record<ModelSettingKey, string>;
export type ModelSettingsBody = Partial<Record<ModelSettingKey, number>>;

export const EMPTY_MODEL_SETTINGS_TEXT: ModelSettingsText = {
  temperature: "",
  max_tokens: "",
  top_p: "",
  top_k: "",
  frequency_penalty: "",
  presence_penalty: "",
  seed: "",
  max_steps: "",
  max_retries: "",
};

export function fieldFor(key: ModelSettingKey): ModelSettingField {
  return MODEL_SETTING_FIELDS.find((field) => field.key === key)!;
}

/** The inline message for one typed value, or null when it is empty or valid. */
export function settingProblem(key: ModelSettingKey, text: string): string | null {
  const raw = text.trim();
  if (raw === "") return null;
  const field = fieldFor(key);
  const value = Number(raw);
  if (!Number.isFinite(value)) return `${field.label} must be a number`;
  if (field.integer && !Number.isSafeInteger(value)) return `${field.label} must be a whole number`;
  if (value < field.min || value > field.max) return `${field.label} must be ${field.range}`;
  return null;
}

export function settingsProblems(text: Partial<ModelSettingsText>, keys: readonly ModelSettingKey[] = MODEL_SETTING_FIELDS.map((f) => f.key)): Partial<Record<ModelSettingKey, string>> {
  const out: Partial<Record<ModelSettingKey, string>> = {};
  for (const key of keys) {
    const problem = settingProblem(key, text[key] ?? "");
    if (problem) out[key] = problem;
  }
  return out;
}

/** The set settings as numbers (empty fields left out). Call only when there are no problems. */
export function settingsBody(text: Partial<ModelSettingsText>): ModelSettingsBody {
  const out: ModelSettingsBody = {};
  for (const field of MODEL_SETTING_FIELDS) {
    const raw = (text[field.key] ?? "").trim();
    if (raw !== "") out[field.key] = Number(raw);
  }
  return out;
}

export function hasSettings(body: ModelSettingsBody | null | undefined): boolean {
  return body !== null && body !== undefined && Object.keys(body).length > 0;
}

/** Typed text of a stored settings object (numbers back to text; null and undefined are unset). */
export function settingsText(settings: Partial<Record<ModelSettingKey, number | null>> | null | undefined): ModelSettingsText {
  const out = { ...EMPTY_MODEL_SETTINGS_TEXT };
  for (const field of MODEL_SETTING_FIELDS) {
    const value = settings?.[field.key];
    if (typeof value === "number") out[field.key] = String(value);
  }
  return out;
}
