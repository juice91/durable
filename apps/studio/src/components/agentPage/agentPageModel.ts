/**
 * The agent page's pure logic and browser storage (SL.6): which spec the Config panel shows, the
 * memory card's summary, instruction placeholders, and where a bare `/playground` goes.
 */
import type { AgentSpec, AgentVersion } from "@/lib/api";
import { routes, type AgentChatRouteOptions } from "@/lib/routes";
import { sectionsOf } from "@/components/agents/specSections";
import { effectiveVersionRef, targetSpec, type VersionRef } from "@/components/playground/runOptions";
import { readStorage, writeStorage } from "@/lib/safeStorage";

/** The agent whose chat was opened last (`/playground` without an agent goes there). */
export const LAST_AGENT_KEY = "studio.lastAgent";
/** Pane sizes of one agent's chat (threads | chat), as react-resizable-panels percentages. */
export const AGENT_LAYOUT_KEY_PREFIX = "studio.agent-layout.v1.";
/** The agents list view, "list" or "compact". */
export const AGENTS_VIEW_KEY = "studio.agents.view";

/** Whether the user left one agent's Config panel open or closed on the Chat tab ("open" | "closed"). */
export const AGENT_CONFIG_KEY_PREFIX = "studio.agent-config.v1.";

export function readConfigOpen(agentId: string): boolean | null {
  const value = read(`${AGENT_CONFIG_KEY_PREFIX}${agentId}`);
  return value === "open" ? true : value === "closed" ? false : null;
}

export function writeConfigOpen(agentId: string, open: boolean): void {
  write(`${AGENT_CONFIG_KEY_PREFIX}${agentId}`, open ? "open" : "closed");
}

export function agentLayoutKey(agentId: string): string {
  return `${AGENT_LAYOUT_KEY_PREFIX}${agentId}`;
}

const read = readStorage;
// Not remembered on failure (private mode, a full quota): nothing depends on it.
const write = (key: string, value: string): void => void writeStorage(key, value);

export function readLastAgent(): string | null {
  return read(LAST_AGENT_KEY) || null;
}

export function writeLastAgent(agentId: string): void {
  if (agentId) write(LAST_AGENT_KEY, agentId);
}

export type AgentsView = "list" | "compact";

export function readAgentsView(): AgentsView {
  return read(AGENTS_VIEW_KEY) === "compact" ? "compact" : "list";
}

export function writeAgentsView(view: AgentsView): void {
  write(AGENTS_VIEW_KEY, view);
}

/**
 * Where a bare `/playground` goes: the last-used agent's chat when that agent still exists, else the
 * first agent's, else the agents list.
 */
export function playgroundFallbackTarget(
  lastAgent: string | null,
  agents: readonly { id: string }[],
  options: Omit<AgentChatRouteOptions, "conversation"> = {},
): string {
  const last = lastAgent ? agents.find((agent) => agent.id === lastAgent) : undefined;
  const chosen = last ?? agents[0];
  return chosen ? routes.agentChat(chosen.id, options) : routes.agents();
}

export interface EffectiveConfig {
  /** The version ref the chat runs (an unpublished agent is always "draft"). */
  ref: VersionRef;
  /** The spec shown; null while the versions load. */
  spec: AgentSpec | null;
  /** "Draft", "v3", or "v3 (latest)". */
  versionLabel: string;
  isDraft: boolean;
  /** The published version number shown, when not the draft. */
  version: number | null;
  /** Set when that version was published by restoring an older one. */
  restoredFrom: number | null;
}

/**
 * The spec the Config panel describes: the version the chat targets (latest published by default,
 * the draft for `?version=draft` or an agent with no published version).
 */
export function effectiveConfig(
  agent: { draft_spec: AgentSpec; latest_version: number | null },
  versions: readonly AgentVersion[] | null,
  requested: VersionRef = null,
): EffectiveConfig {
  const ref = effectiveVersionRef(requested, agent.latest_version, versions);
  const spec = targetSpec(ref, agent.draft_spec, agent.latest_version, versions);
  if (ref === "draft") return { ref, spec, versionLabel: "Draft", isDraft: true, version: null, restoredFrom: null };
  const version = ref ?? agent.latest_version;
  const row = versions?.find((v) => v.version === version);
  return {
    ref,
    spec,
    versionLabel: version === agent.latest_version ? `v${version} (latest)` : `v${version}`,
    isDraft: false,
    version,
    restoredFrom: row?.restored_from ?? null,
  };
}

/** The memory part of a spec (RM.13 and RM.14 shapes); absent on agents without memory. */
interface MemorySpec {
  history?: { last_turns?: number | null } | null;
  working?: object | null;
  observational?: object | null;
  semantic_recall?: object | null;
}

export interface MemoryBadge {
  id: "history" | "working" | "observational" | "semantic";
  label: string;
  on: boolean;
}

/** The memory card's four on/off badges, read from the effective spec. */
export function memorySummary(spec: AgentSpec | null | undefined): MemoryBadge[] {
  const memory = (spec as { memory?: MemorySpec | null } | null | undefined)?.memory ?? null;
  const turns = memory?.history?.last_turns;
  return [
    {
      id: "history",
      // Conversations replay their earlier turns without any memory config.
      label: typeof turns === "number" ? `History ${turns} turns` : "History",
      on: true,
    },
    { id: "working", label: "Working memory", on: Boolean(memory?.working) },
    { id: "observational", label: "Observational", on: Boolean(memory?.observational) },
    { id: "semantic", label: "Semantic recall", on: Boolean(memory?.semantic_recall) },
  ];
}

const PLACEHOLDER_SPLIT_RE = /(\{\{\s*context\.[a-z][a-z0-9_]{0,31}\s*\}\})/g;

export interface InstructionSegment {
  text: string;
  placeholder: boolean;
}

/** Instructions split so `{{context.x}}` placeholders can be highlighted. */
export function instructionSegments(text: string): InstructionSegment[] {
  return text
    .split(PLACEHOLDER_SPLIT_RE)
    .filter((part) => part !== "")
    .map((part) => ({ text: part, placeholder: /^\{\{\s*context\./.test(part) }));
}

export interface ConfigCounts {
  tools: number;
  contextFields: number;
  processors: number;
}

/** Section counts shown in the Config panel headings. */
export function configCounts(spec: AgentSpec | null | undefined): ConfigCounts {
  const sections = sectionsOf(spec ?? undefined);
  return {
    tools: (spec?.tools.length ?? 0) + (spec?.mcp_tools?.length ?? 0),
    contextFields: sections.context_fields.length,
    processors: sections.input_processors.length + sections.output_processors.length,
  };
}

/** The agents list's "Purpose": the spec description when there is one, else the instructions' first line. */
export function agentPurpose(spec: AgentSpec | null | undefined): string {
  const description = (spec as { description?: unknown } | null | undefined)?.description;
  if (typeof description === "string" && description.trim()) return description.trim();
  const first = spec?.instructions?.split("\n").find((line) => line.trim())?.trim();
  return first || "No instructions";
}

export interface ChatUrlState {
  conversation: string | null;
  version: VersionRef;
  model: string | null;
}

/** Query keys the chat owns (the rest of the query is kept); `agent` and `conversation` are legacy
 * playground keys that the path now carries. */
const CHAT_OWNED_KEYS = new Set(["version", "model", "agent", "conversation"]);

/**
 * The chat's address for its state: `/agents/:id/chat[/:conversation]?version=&model=`, keeping any
 * other query parameter (`context=…`) and the hash of `href`. Returned as a path, never another origin.
 */
export function chatUrlFor(href: string, agentId: string, state: ChatUrlState): string {
  const current = new URL(href, "http://studio.invalid");
  const next = new URL(routes.agentChat(agentId, state), "http://studio.invalid");
  current.searchParams.forEach((value, key) => {
    if (!CHAT_OWNED_KEYS.has(key)) next.searchParams.append(key, value);
  });
  return `${next.pathname}${next.search}${current.hash}`;
}
