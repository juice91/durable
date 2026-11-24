/**
 * Agent spec helpers for agents as tools (supervisor pattern): the `agent_tools` key the editor's
 * "Agents as tools" step writes, its client-side checks, status line and server-error ownership. Wire
 * shapes and limits follow eap_schemas `agent_tools.py` (`AgentToolRef`).
 *
 * Hash stability (the spec contract): the key is left out of the spec while empty, and inside every
 * ref a default is left out (`name`, `description`, `version: "latest"`, `timeout_seconds: 300`,
 * `max_calls_per_run: 5`), so a saved and reloaded draft is the same JSON as the one sent and the API's
 * canonical dump.
 */
import type { AgentSpec } from "@/lib/api";

export const AGENT_TOOLS_SPEC_KEYS = ["agent_tools"] as const;
export const MAX_AGENT_TOOLS_PER_AGENT = 8;
export const AGENT_TOOL_NAME_PATTERN = /^[a-z][a-z0-9_]{0,40}$/;
export const DEFAULT_TIMEOUT_SECONDS = 300;
export const MAX_TIMEOUT_SECONDS = 3600;
export const DEFAULT_MAX_CALLS_PER_RUN = 5;
export const MAX_CALLS_PER_RUN_LIMIT = 20;
export const MAX_DESCRIPTION_CHARS = 1000;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** One ref as stored: defaults are absent. While typing, a field may hold text the checks flag. */
export interface AgentToolRefSpec {
  agent_id: string;
  name?: string;
  description?: string;
  /** "latest" is left out; a number pins that published version. */
  version?: number | string;
  timeout_seconds?: number | string;
  max_calls_per_run?: number | string;
}

export type AgentSpecWithAgentTools = AgentSpec & { agent_tools?: AgentToolRefSpec[] };

export function agentToolsOf(spec: AgentSpec | null | undefined): AgentToolRefSpec[] {
  const list = (spec as AgentSpecWithAgentTools | null | undefined)?.agent_tools;
  return Array.isArray(list) ? list : [];
}

/** eap_schemas.agent_tools.agent_tool_default_name: the tool name derived from the target agent's name. */
export function defaultToolName(agentName: string | null | undefined, agentId: string): string {
  const fallback = `agent_${agentId.replace(/-/g, "").slice(0, 8)}`;
  let slug = (agentName ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  if (!slug) return fallback;
  if (!/^[a-z]/.test(slug)) slug = `agent_${slug}`;
  slug = slug.slice(0, 41).replace(/_+$/, "");
  return AGENT_TOOL_NAME_PATTERN.test(slug) ? slug : fallback;
}

/** The tool name the model sees for a ref: its own `name`, else the derived one. */
export function effectiveToolName(ref: AgentToolRefSpec, agentName: string | null | undefined): string {
  return ref.name?.trim() ? ref.name : defaultToolName(agentName, ref.agent_id);
}

export function newAgentToolRef(agentId: string): AgentToolRefSpec {
  return { agent_id: agentId };
}

/** A number field as typed: "" is the default (left out), a number as a number, anything else as text (flagged). */
export function typedInt(raw: string): number | string | undefined {
  if (raw.trim() === "") return undefined;
  const value = Number(raw);
  return Number.isFinite(value) ? value : raw;
}

/** The ref in the API's dump shape: key order of the model, every default and empty text left out. */
export function normalizeAgentToolRef(ref: AgentToolRefSpec): AgentToolRefSpec {
  const out: AgentToolRefSpec = { agent_id: ref.agent_id };
  if (typeof ref.name === "string" && ref.name !== "") out.name = ref.name;
  if (typeof ref.description === "string" && ref.description.trim() !== "") out.description = ref.description;
  if (ref.version !== undefined && ref.version !== "latest") out.version = ref.version;
  if (ref.timeout_seconds !== undefined && ref.timeout_seconds !== DEFAULT_TIMEOUT_SECONDS) out.timeout_seconds = ref.timeout_seconds;
  if (ref.max_calls_per_run !== undefined && ref.max_calls_per_run !== DEFAULT_MAX_CALLS_PER_RUN) out.max_calls_per_run = ref.max_calls_per_run;
  return out;
}

/** The `agent_tools` value to write: undefined (key removed) when the list is empty. */
export function agentToolsValue(list: AgentToolRefSpec[]): AgentToolRefSpec[] | undefined {
  return list.length > 0 ? list.map(normalizeAgentToolRef) : undefined;
}

export interface AgentToolProblem {
  /** `list`, or `<index>-<field>` (`agent_id`, `name`, `version`, `timeout_seconds`, `max_calls_per_run`, `description`). */
  field: string;
  message: string;
}

function intProblem(value: unknown, label: string, min: number, max: number): string | null {
  if (value === undefined) return null;
  if (typeof value !== "number" || !Number.isInteger(value) || value < min || value > max) {
    return `${label} must be a whole number from ${min} to ${max}`;
  }
  return null;
}

/** Problems with `spec.agent_tools` as the API would refuse them on save (publish adds its own checks). */
export function agentToolProblems(spec: AgentSpec | null | undefined): AgentToolProblem[] {
  const refs = agentToolsOf(spec);
  const problems: AgentToolProblem[] = [];
  if (refs.length > MAX_AGENT_TOOLS_PER_AGENT) problems.push({ field: "list", message: `At most ${MAX_AGENT_TOOLS_PER_AGENT} agents per agent` });
  const ids = new Set<string>();
  const names = new Set<string>();
  refs.forEach((ref, index) => {
    const id = typeof ref?.agent_id === "string" ? ref.agent_id : "";
    if (!UUID_RE.test(id)) problems.push({ field: `${index}-agent_id`, message: "Agent id must be a UUID" });
    else if (ids.has(id.toLowerCase())) problems.push({ field: `${index}-agent_id`, message: "This agent is added twice" });
    ids.add(id.toLowerCase());
    if (ref.name !== undefined) {
      if (typeof ref.name !== "string" || !AGENT_TOOL_NAME_PATTERN.test(ref.name)) {
        problems.push({ field: `${index}-name`, message: "Name must start with a lower-case letter and use only a-z, 0-9 and _ (at most 41 characters)" });
      } else if (names.has(ref.name)) problems.push({ field: `${index}-name`, message: "Another agent tool has this name" });
      else names.add(ref.name);
    }
    if (typeof ref.description === "string" && ref.description.length > MAX_DESCRIPTION_CHARS) {
      problems.push({ field: `${index}-description`, message: `Description must be at most ${MAX_DESCRIPTION_CHARS} characters` });
    }
    if (ref.version !== undefined && ref.version !== "latest") {
      const version = intProblem(ref.version, "Version", 1, 1_000_000);
      if (version) problems.push({ field: `${index}-version`, message: version });
    }
    const timeout = intProblem(ref.timeout_seconds, "Timeout", 1, MAX_TIMEOUT_SECONDS);
    if (timeout) problems.push({ field: `${index}-timeout_seconds`, message: timeout });
    const calls = intProblem(ref.max_calls_per_run, "Max calls", 1, MAX_CALLS_PER_RUN_LIMIT);
    if (calls) problems.push({ field: `${index}-max_calls_per_run`, message: calls });
  });
  return problems;
}

/**
 * Whether a save or publish message is about agent tools: the API's publish refusals (`agent tool: ...`,
 * `agent tool name '...' ...`, `agent tool cycle: ...`) and the schema's (`agent_tools[0]: ...`,
 * `Value error, duplicate agent tool ...`, `at most 8 agent tools per agent`).
 */
export function isAgentToolsError(message: string): boolean {
  return /^(Value error, )?(agent_tools(\[\d+\])?[.:\s]|agent tool[ s:]|duplicate agent tool|at most \d+ agent tools|agent tool name)/.test(message);
}
