// Playground run options (P0.19): the per-turn version or draft, model override and request
// context; how they persist across a reload; how a refused turn start (422) maps onto the controls;
// and what each turn's badge and notices show (model, tokens, cost, output check, block).
//
// Persistence follows the existing conventions: what identifies the view goes in the URL
// (`?agent=&version=&model=&conversation=`, replaceState). The context value edited here is kept
// per agent in sessionStorage (this tab only, gone when it closes; scoped by credential, try/catch
// everywhere); a preset outlives the tab only when saved with the explicit button (P0.16 storage).
import type { AgentSpec, AgentVersion } from "@/lib/api";
import type { ModelOption } from "@/lib/metrics";
import {
  OPEN_CONTEXT_VALUE,
  PRESET_CONTEXT_PARAM,
  PRESET_CONTEXT_VALUE,
  credentialScope,
  mapContextErrors,
  type RequestContextValue,
} from "@/lib/requestContext";
import type { RunEvent } from "@/lib/runs";

/** A turn's target: a published version, the draft, or null for the latest published version. */
export type VersionRef = number | "draft" | null;

export interface ModelRefBody {
  provider: string;
  name: string;
}

/** The options sent with one turn (POST /conversations/{id}/runs). */
export interface TurnOptions {
  version: VersionRef;
  /** null: the version's own model (no override). */
  model: ModelRefBody | null;
  /** null: no context supplied. */
  request_context: RequestContextValue | null;
}

export const NO_OPTIONS: TurnOptions = { version: null, model: null, request_context: null };

/** The JSON body of a turn start; null options are left out (the API treats absent as null). */
export function turnBody(runId: string, input: string, options: TurnOptions): Record<string, unknown> {
  const body: Record<string, unknown> = { run_id: runId, input };
  if (options.version !== null) body.version = options.version;
  if (options.model !== null) body.model = options.model;
  if (options.request_context !== null) body.request_context = options.request_context;
  return body;
}

// ------------------------------------------------------------------------------ URL state

export interface PlaygroundUrlState {
  agent: string | null;
  conversation: string | null;
  version: VersionRef;
  /** The model override as `provider:name`, or null for the version's model. */
  model: string | null;
  /** `context=preset`: the newest saved preset wins over the tab's remembered context. */
  preferPreset: boolean;
  /** `context=open` (the old Request Context page's links): the request context popover opens. */
  openContext: boolean;
}

const MODEL_ID_RE = /^[A-Za-z0-9_.-]+:\S.{0,199}$/;

export function parseVersionParam(raw: string | null): VersionRef {
  if (raw === "draft") return "draft";
  if (raw && /^[1-9]\d{0,8}$/.test(raw)) return Number(raw);
  return null;
}

export function parseModelParam(raw: string | null): string | null {
  if (!raw) return null;
  const value = raw.trim();
  return MODEL_ID_RE.test(value) && !value.endsWith(":*") ? value : null;
}

export function parseUrlState(search: string): PlaygroundUrlState {
  const params = new URLSearchParams(search);
  return {
    agent: params.get("agent") || null,
    conversation: params.get("conversation") || null,
    version: parseVersionParam(params.get("version")),
    model: parseModelParam(params.get("model")),
    preferPreset: params.get(PRESET_CONTEXT_PARAM) === PRESET_CONTEXT_VALUE,
    openContext: params.get(PRESET_CONTEXT_PARAM) === OPEN_CONTEXT_VALUE,
  };
}

/** Removes the one-shot `context=preset` / `context=open` flag from the URL so a reload or later edits are not overridden. */
export function stripPreferPresetParam(): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (!url.searchParams.has(PRESET_CONTEXT_PARAM)) return;
  url.searchParams.delete(PRESET_CONTEXT_PARAM);
  window.history.replaceState(window.history.state, "", url);
}

/** Writes the given keys onto `url` (null removes one); other parameters are kept. */
export function applyUrlState(url: URL, state: Partial<PlaygroundUrlState>): URL {
  const set = (key: string, value: string | null) => {
    if (value === null || value === "") url.searchParams.delete(key);
    else url.searchParams.set(key, value);
  };
  if ("agent" in state) set("agent", state.agent ?? null);
  if ("conversation" in state) set("conversation", state.conversation ?? null);
  if ("version" in state) set("version", state.version === null || state.version === undefined ? null : String(state.version));
  if ("model" in state) set("model", state.model ?? null);
  return url;
}

// ------------------------------------------------------------------------- version select

/** `<select>` value of a version ref: "latest", "draft" or "v<n>". */
export function versionSelectValue(ref: VersionRef): string {
  if (ref === null) return "latest";
  if (ref === "draft") return "draft";
  return `v${ref}`;
}

export function versionFromSelectValue(value: string): VersionRef {
  if (value === "draft") return "draft";
  const match = /^v([1-9]\d*)$/.exec(value);
  return match ? Number(match[1]) : null;
}

/**
 * The version a turn actually targets: an agent without a published version can only run its
 * draft; a version number the agent does not have (an old URL) falls back to the latest.
 */
export function effectiveVersionRef(
  requested: VersionRef,
  latestVersion: number | null,
  versions: readonly AgentVersion[] | null,
): VersionRef {
  if (latestVersion === null) return "draft";
  if (typeof requested === "number" && versions !== null && !versions.some((v) => v.version === requested)) {
    return null;
  }
  return requested;
}

/** The spec a version ref runs: the draft, a listed version, or the latest version; null if unknown yet. */
export function targetSpec(
  ref: VersionRef,
  draftSpec: AgentSpec | null | undefined,
  latestVersion: number | null,
  versions: readonly AgentVersion[] | null,
): AgentSpec | null {
  if (ref === "draft") return draftSpec ?? null;
  const number = ref ?? latestVersion;
  if (number === null) return null;
  return versions?.find((v) => v.version === number)?.spec ?? null;
}

export function specModelId(spec: AgentSpec | null | undefined): string | null {
  return spec ? `${spec.model.provider}:${spec.model.name}` : null;
}

/** Whether a spec declares output processors (P0.17): its runs stream no live text. */
export function hasOutputProcessors(spec: AgentSpec | null | undefined): boolean {
  const list = (spec as { output_processors?: unknown } | null | undefined)?.output_processors;
  return Array.isArray(list) && list.length > 0;
}

/**
 * Whether a turn may show live (unchecked) text: only when its target spec is known and has no
 * output processors. Fails closed: while the spec is unknown (versions not loaded, unknown version)
 * no delta is shown. `recorded` is the value fixed when the turn started (null: work it out now,
 * e.g. a turn rebuilt on reload, from its own version or draft).
 */
export function turnLiveText(
  recorded: boolean | null,
  turn: { draft: boolean; version: number | null },
  draftSpec: AgentSpec | null | undefined,
  latestVersion: number | null,
  versions: readonly AgentVersion[] | null,
): boolean {
  if (recorded !== null) return recorded;
  if (!turn.draft && versions === null) return false;
  const spec = targetSpec(turn.draft ? "draft" : turn.version, draftSpec, latestVersion, versions);
  return spec !== null && !hasOutputProcessors(spec);
}

// --------------------------------------------------------------------------- model picker

/** Picker state: `choice` is "" (the version's model), a listed model id, or `<provider>:*`. */
export interface ModelChoice {
  choice: string;
  /** The typed name when `choice` is a wildcard provider. */
  customName: string;
}

export const DEFAULT_MODEL_CHOICE: ModelChoice = { choice: "", customName: "" };

export interface PickerOption {
  value: string;
  label: string;
  wildcard: boolean;
}

/** Alternatives to the version's model: every listed model except that one; wildcard providers last-in-order as listed. */
export function modelPickerOptions(models: readonly ModelOption[], defaultModelId: string | null): PickerOption[] {
  const seen = new Set<string>();
  const options: PickerOption[] = [];
  for (const model of models) {
    if (seen.has(model.id)) continue;
    seen.add(model.id);
    if (model.wildcard) {
      options.push({ value: `${model.provider}:*`, label: `${model.provider}: other model…`, wildcard: true });
    } else if (model.id !== defaultModelId) {
      options.push({
        value: model.id,
        label: model.description ? `${model.id} (${model.description})` : model.id,
        wildcard: false,
      });
    }
  }
  return options;
}

export function isWildcardChoice(choice: string): boolean {
  return choice.endsWith(":*");
}

/**
 * The picker state for a model id (from the URL or a run): a listed model, or a wildcard provider
 * with the name filled in. `dropped` is true when the model is not offered at all.
 */
export function choiceFromModelId(
  modelId: string | null,
  models: readonly ModelOption[],
  defaultModelId: string | null,
): { choice: ModelChoice; dropped: boolean } {
  if (modelId === null || modelId === defaultModelId) return { choice: DEFAULT_MODEL_CHOICE, dropped: false };
  if (models.some((m) => !m.wildcard && m.id === modelId)) {
    return { choice: { choice: modelId, customName: "" }, dropped: false };
  }
  const [provider, ...rest] = modelId.split(":");
  const name = rest.join(":");
  if (name && models.some((m) => m.wildcard && m.provider === provider)) {
    return { choice: { choice: `${provider}:*`, customName: name }, dropped: false };
  }
  return { choice: DEFAULT_MODEL_CHOICE, dropped: true };
}

/** The chosen model id (null: the version's model), or an error to show under the picker. */
export function resolveModelChoice(choice: ModelChoice): { modelId: string | null } | { error: string } {
  if (choice.choice === "") return { modelId: null };
  if (!isWildcardChoice(choice.choice)) return { modelId: choice.choice };
  const provider = choice.choice.slice(0, -2);
  const name = choice.customName.trim();
  if (!name) return { error: `Enter a model name for ${provider}` };
  if (/\s/.test(name)) return { error: "A model name has no spaces" };
  if (name.length > 200) return { error: "A model name is at most 200 characters" };
  return { modelId: `${provider}:${name}` };
}

export function modelRef(modelId: string): ModelRefBody {
  const index = modelId.indexOf(":");
  return { provider: modelId.slice(0, index), name: modelId.slice(index + 1) };
}

/** The `model` to send: only an override that differs from the version's model. */
export function overrideFor(modelId: string | null, defaultModelId: string | null): ModelRefBody | null {
  if (modelId === null || modelId === defaultModelId) return null;
  return modelRef(modelId);
}

// ------------------------------------------------------------------- context persistence

export const PLAYGROUND_CONTEXT_PREFIX = "eap.playground.context.v1:";

// The scope is the API credential's fingerprint because Studio has no notion of the signed-in user
// yet. Once per-user login lands, this key (and the P0.16 preset key) must be scoped by the
// principal (tenant and user), so two people sharing a browser never see each other's context.
export function playgroundContextKey(agentId: string): string {
  return `${PLAYGROUND_CONTEXT_PREFIX}${credentialScope()}:${agentId}`;
}

/** sessionStorage: the auto-remembered context lives only as long as the tab. */
function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The context last edited in the chat for this agent (this tab), or null. */
export function loadPlaygroundContext(agentId: string): RequestContextValue | null {
  try {
    const raw = storage()?.getItem(playgroundContextKey(agentId));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    return isRecord(parsed) && isRecord(parsed.value) ? parsed.value : null;
  } catch {
    return null;
  }
}

/** Forgets the context remembered for this agent in this tab. */
export function clearPlaygroundContext(agentId: string): void {
  try {
    storage()?.removeItem(playgroundContextKey(agentId));
  } catch {
    // Storage unavailable: nothing remembered.
  }
}

/** Remembers the playground's context for this agent in this tab; false when storage is unavailable. */
export function savePlaygroundContext(agentId: string, value: RequestContextValue): boolean {
  try {
    const store = storage();
    if (!store) return false;
    store.setItem(playgroundContextKey(agentId), JSON.stringify({ value }));
    return true;
  } catch {
    return false;
  }
}

/** The context to send: undefined, null and "" entries left out; null when nothing is left. */
export function contextToSend(value: RequestContextValue): RequestContextValue | null {
  const result: RequestContextValue = {};
  for (const [key, item] of Object.entries(value)) {
    if (item === undefined || item === null || item === "") continue;
    result[key] = item;
  }
  return Object.keys(result).length > 0 ? result : null;
}

// ---------------------------------------------------------------------- refused turn start

/** Where a refused turn start's messages go: next to the control they are about. */
export interface TurnOptionErrors {
  context: { banner: string | null; fieldErrors: Record<string, string> } | null;
  model: string | null;
  version: string | null;
  general: string | null;
}

export const NO_OPTION_ERRORS: TurnOptionErrors = { context: null, model: null, version: null, general: null };

/**
 * Maps a turn start's error body onto the controls. A 422 list puts `body.request_context.*`
 * under the context fields and `body.model` / `body.version` under their pickers. A 422 string is
 * a model refusal only when the turn carried an override and the message names that override's
 * `provider:name` (allowlist, provider rules); a draft's own model being refused is a problem of the
 * draft, so on a draft turn any other string is a draft refusal; else general.
 */
export function classifyTurnError(status: number, detail: unknown, options: TurnOptions): TurnOptionErrors {
  const errors: TurnOptionErrors = { ...NO_OPTION_ERRORS };
  if (Array.isArray(detail)) {
    const contextItems: unknown[] = [];
    const general: string[] = [];
    for (const item of detail) {
      const loc = isRecord(item) && Array.isArray(item.loc) ? item.loc.map(String) : [];
      const msg = isRecord(item) && typeof item.msg === "string" ? item.msg : "is invalid";
      const area = loc[0] === "body" ? loc[1] : undefined;
      if (area === "request_context") contextItems.push(item);
      else if (area === "model") errors.model = errors.model ? `${errors.model}; ${msg}` : msg;
      else if (area === "version") errors.version = errors.version ? `${errors.version}; ${msg}` : msg;
      else general.push(loc.length > 0 ? `${loc.filter((part) => part !== "body").join(".")}: ${msg}` : msg);
    }
    if (contextItems.length > 0) {
      const mapped = mapContextErrors(status, contextItems);
      errors.context = { banner: mapped.banner, fieldErrors: mapped.fieldErrors };
    }
    if (general.length > 0) errors.general = general.join("; ");
    return errors;
  }
  const message = typeof detail === "string" && detail ? detail : `The request failed (HTTP ${status})`;
  const override = options.model ? `'${options.model.provider}:${options.model.name}'` : null;
  if (status === 422 && override !== null && message.includes(override)) errors.model = message;
  else if (status === 422 && options.version === "draft") errors.version = `The draft cannot run: ${message}`;
  else errors.general = message;
  return errors;
}

// ------------------------------------------------------------------- turn badge and notices

/** Usage fields of a run row (P0.1) and its run options (P0.8). */
export interface RunUsageRow {
  model_id?: string | null;
  model_overridden?: boolean;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cost_usd?: string | null;
  draft?: boolean;
  version?: number | null;
  request_context?: RequestContextValue | null;
}

export interface TurnUsage {
  /** False when nothing was recorded (no model request yet, or a run from before usage existed). */
  recorded: boolean;
  inputTokens: number;
  outputTokens: number;
  /** USD; null when some request had no price. */
  costUsd: number | null;
  /** False when some request's provider reported no usage (`usage.measured === false`): its zeros are not real counts. */
  measured: boolean;
}

/** Whether every usage-carrying event of the turn was measured by the provider. */
function usageMeasured(events: readonly RunEvent[]): boolean {
  return !events.some((event) => isRecord(event.data.usage) && event.data.usage.measured === false);
}

/**
 * A turn's usage: the run's totals once recorded, else the sum over its events that carry usage
 * (each final `model_message`, and a moderation `processor_result`), which is what the totals sum.
 */
export function turnUsage(events: readonly RunEvent[], row?: RunUsageRow | null): TurnUsage {
  if (row && typeof row.input_tokens === "number" && typeof row.output_tokens === "number") {
    const cost = row.cost_usd === null || row.cost_usd === undefined ? null : Number(row.cost_usd);
    return {
      recorded: true,
      inputTokens: row.input_tokens,
      outputTokens: row.output_tokens,
      costUsd: cost !== null && Number.isFinite(cost) ? cost : null,
      measured: usageMeasured(events),
    };
  }
  let requests = 0;
  let unpriced = 0;
  let inputTokens = 0;
  let outputTokens = 0;
  let cost = 0;
  for (const event of events) {
    const usage = event.data.usage;
    if (!isRecord(usage)) continue;
    requests += 1;
    inputTokens += typeof usage.input_tokens === "number" ? usage.input_tokens : 0;
    outputTokens += typeof usage.output_tokens === "number" ? usage.output_tokens : 0;
    const price = typeof event.data.cost_usd === "string" ? Number(event.data.cost_usd) : NaN;
    if (Number.isFinite(price)) cost += price;
    else unpriced += 1;
  }
  return {
    recorded: requests > 0,
    inputTokens,
    outputTokens,
    costUsd: requests > 0 && unpriced === 0 ? cost : null,
    measured: usageMeasured(events),
  };
}

/** The model that answered: the run's recorded model, else the latest model named by an event. */
export function turnModelId(events: readonly RunEvent[], row?: RunUsageRow | null): string | null {
  if (row?.model_id) return row.model_id;
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event.type === "model_message" && typeof event.data.model_id === "string") return event.data.model_id;
  }
  return null;
}

export type BlockCode = "input_blocked" | "output_blocked";

/** A turn blocked by the agent's processors (P0.17), from its run_failed event. */
export function turnBlock(events: readonly RunEvent[]): { code: BlockCode; message: string | null } | null {
  for (const event of events) {
    if (event.type !== "run_failed") continue;
    const code = event.data.error_code;
    if (code === "input_blocked" || code === "output_blocked") {
      return { code, message: typeof event.data.error === "string" ? event.data.error : null };
    }
  }
  return null;
}

/** The options a recorded turn was started with, for an idempotent re-post of that same turn. */
export function optionsFromRun(run: RunUsageRow): TurnOptions {
  const context = run.request_context && Object.keys(run.request_context).length > 0 ? run.request_context : null;
  return {
    version: run.draft ? "draft" : (run.version ?? null),
    model: run.model_overridden && run.model_id && run.model_id.includes(":") ? modelRef(run.model_id) : null,
    request_context: context,
  };
}

export function versionLabel(draft: boolean, version: number | null | undefined): string | null {
  if (draft) return "Draft";
  return typeof version === "number" ? `v${version}` : null;
}

// ------------------------------------------------------------------------- draft vs latest

/** JSON with object keys sorted, so two equal specs compare equal whatever their key order. */
function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value)
      .filter((key) => value[key] !== undefined)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

/**
 * Whether the draft holds changes the latest published version lacks (the version chip then lists
 * Draft first). True for an agent with no published version; false while the versions load.
 */
export function draftDiffersFromLatest(
  draftSpec: AgentSpec | null | undefined,
  latestVersion: number | null,
  versions: readonly AgentVersion[] | null,
): boolean {
  if (latestVersion === null) return true;
  if (!draftSpec || versions === null) return false;
  const latest = versions.find((item) => item.version === latestVersion);
  return latest ? canonical(draftSpec) !== canonical(latest.spec) : false;
}
