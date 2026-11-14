/**
 * Pure rules of the memory inspector (RM.18): which spec says what, what an edit may do (mirroring
 * eap_api.routes.memory, which checks the agent's latest PUBLISHED version), and the browser
 * storage of the memory card and "Act as user".
 */
import type { Agent, AgentSpec, AgentVersion } from "@/lib/api";
import { credentialScope } from "@/lib/requestContext";
import { readStorage, writeStorage } from "@/lib/safeStorage";
import { DEFAULT_WORKING_MEMORY_TEMPLATE, memoryOf, type WorkingSpec } from "@/components/agents/knowledgeMemory";
import type { WorkingMemoryScope } from "@/lib/memory";

/** Whether the memory card is expanded into the inspector ("open" | "closed"), for every agent. */
export const MEMORY_CARD_KEY = "studio.memory-card.v1";

/** sessionStorage: the "Act as user" id per agent in this tab (`…:<credential scope>:<agentId>`). */
export const ACT_AS_PREFIX = "eap.studio.actAs.v1:";

export function readMemoryCardOpen(): boolean {
  return readStorage(MEMORY_CARD_KEY) === "open";
}

export function writeMemoryCardOpen(open: boolean): void {
  writeStorage(MEMORY_CARD_KEY, open ? "open" : "closed");
}

function session(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

export function actAsKey(agentId: string): string {
  return `${ACT_AS_PREFIX}${credentialScope()}:${agentId}`;
}

/** The "Act as user" id this tab remembers for the agent, or "" (the API's default: you). */
export function readActAs(agentId: string): string {
  try {
    return session()?.getItem(actAsKey(agentId)) ?? "";
  } catch {
    return "";
  }
}

export function writeActAs(agentId: string, resourceId: string): void {
  try {
    const storage = session();
    if (!storage) return;
    if (resourceId) storage.setItem(actAsKey(agentId), resourceId);
    else storage.removeItem(actAsKey(agentId));
  } catch {
    // Not remembered: the chip still applies to this page.
  }
}

/** The latest published version's spec, or null for an unpublished agent. */
export function latestPublishedSpec(agent: Agent | null | undefined, versions: readonly AgentVersion[] | null | undefined): AgentSpec | null {
  if (!agent || agent.latest_version === null) return null;
  return versions?.find((item) => item.version === agent.latest_version)?.spec ?? null;
}

/**
 * Whether the agent has memory: its draft or its latest published version (decision "Agent page URL
 * and tabs": the Memory tab and "Act as user" show only then).
 */
export function agentHasMemory(agent: Agent | null | undefined, versions: readonly AgentVersion[] | null | undefined): boolean {
  if (!agent) return false;
  return memoryOf(agent.draft_spec) !== null || memoryOf(latestPublishedSpec(agent, versions)) !== null;
}

export function workingOf(spec: AgentSpec | null | undefined): WorkingSpec | null {
  return memoryOf(spec)?.working ?? null;
}

/** `markdown` (template mode) or `json` (the working memory has a JSON schema). */
export function workingFormat(working: WorkingSpec | null | undefined): "markdown" | "json" {
  return working?.json_schema != null ? "json" : "markdown";
}

/** What "Create" starts from: `{}` in schema mode, the template (or the default one). */
export function workingStarter(working: WorkingSpec | null | undefined): string {
  if (workingFormat(working) === "json") return "{}";
  return typeof working?.template === "string" ? working.template : DEFAULT_WORKING_MEMORY_TEMPLATE;
}

export function workingScope(working: WorkingSpec | null | undefined): WorkingMemoryScope {
  return working?.scope ?? "resource";
}

/**
 * Why the API would refuse an edit of a `scope` note (it validates against the latest published
 * `memory.working`), or null: no published working memory, or the other scope.
 */
export function editBlockedReason(published: AgentSpec | null | undefined, scope: WorkingMemoryScope): string | null {
  const working = workingOf(published);
  if (!working) return "The latest published version has no working memory. Publish a version with working memory to edit it.";
  if (workingScope(working) !== scope) {
    return workingScope(working) === "resource"
      ? "This agent's working memory is per user: edit it on the Memory tab or for a conversation's user."
      : "This agent's working memory is per conversation: edit it from a conversation's memory card in Chat.";
  }
  return null;
}

/** The unsaved working memory edit's guard copy (the editor's guard dialog, other words). */
export const WORKING_MEMORY_GUARD_COPY = {
  title: "Discard the working memory edit?",
  description: "The working memory edit is not saved. Leaving discards it.",
  confirmLabel: "Discard edit",
} as const;
