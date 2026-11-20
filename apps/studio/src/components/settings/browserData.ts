import { SIDE_PANEL_STORAGE_KEY } from "@/components/frame/frameStorage";
import { ARCHITECT_SHOW_ARCHIVED_KEY } from "@/components/architect/CreatedList";
import { PRESETS_PREFIX, SAVED_CONTEXT_PREFIX } from "@/lib/requestContext";
import { SIDEBAR_STORAGE_KEY } from "@/lib/sidebarPref";
import { THEME_STORAGE_KEY } from "@/lib/theme";
import { safeLocalStorage } from "@/lib/safeStorage";
import {
  AGENT_CONFIG_KEY_PREFIX,
  AGENT_LAYOUT_KEY_PREFIX,
  AGENTS_SHOW_SYSTEM_KEY,
  AGENTS_VIEW_KEY,
  LAST_AGENT_KEY,
} from "@/components/agentPage/agentPageModel";
import { WORKFLOW_LAYOUT_STORAGE_KEY } from "@/components/workflows/workflowLayout";
import { NOT_REACHED_OPEN_KEY } from "@/lib/notReached";
import { RUNS_SHOW_SKIPPED_KEY } from "@/lib/showSkipped";
import { MEMORY_CARD_KEY } from "@/components/memory/memoryModel";
import { TRACE_COLUMNS_STORAGE_KEY } from "@/lib/traces";
import { CHAT_PROTOCOL_EVENT, CHAT_PROTOCOL_STORAGE_KEY } from "@/components/chat/chatProtocol";

/**
 * What Studio keeps in this browser's localStorage, for the Settings "Data in this browser"
 * section. A task that adds a key adds it here, in the right clear group, so Settings lists it
 * and its Clear button removes it. Nothing here is encrypted.
 */

export type ClearGroup = "layout" | "presets";

export interface BrowserDataEntry {
  id: string;
  label: string;
  description: string;
  /** Exact keys. */
  keys: readonly string[];
  /** Key prefixes (one key per agent, per credential…). */
  prefixes: readonly string[];
  /** Which Clear button removes it; null: changed elsewhere (the theme, in Appearance). */
  clearGroup: ClearGroup | null;
}

/** Per-agent pane sizes on the agent page (SL.6); defined with the agent page's other keys. */
export const AGENT_LAYOUT_PREFIX = AGENT_LAYOUT_KEY_PREFIX;
export { AGENT_CONFIG_KEY_PREFIX, AGENTS_VIEW_KEY, LAST_AGENT_KEY };

export const BROWSER_DATA: readonly BrowserDataEntry[] = [
  {
    id: "theme",
    label: "Theme",
    description: "System, Light or Dark.",
    keys: [THEME_STORAGE_KEY],
    prefixes: [],
    clearGroup: null,
  },
  {
    id: "sidebar",
    label: "Sidebar",
    description: "Whether the sidebar is collapsed to icons.",
    keys: [SIDEBAR_STORAGE_KEY],
    prefixes: [],
    clearGroup: "layout",
  },
  {
    id: "side-panel",
    label: "Side panel width",
    description: "The width of the right-hand panel.",
    keys: [SIDE_PANEL_STORAGE_KEY],
    prefixes: [],
    clearGroup: "layout",
  },
  {
    id: "agent-layout",
    label: "Agent page layout",
    description: "Pane widths on each agent's page, and whether its Config panel was left open or closed.",
    keys: [],
    prefixes: [AGENT_LAYOUT_KEY_PREFIX, AGENT_CONFIG_KEY_PREFIX],
    clearGroup: "layout",
  },
  {
    id: "agents-view",
    label: "Agents list view",
    description: "List or Compact, and whether system agents are shown.",
    keys: [AGENTS_VIEW_KEY, AGENTS_SHOW_SYSTEM_KEY],
    prefixes: [],
    clearGroup: "layout",
  },
  {
    id: "architect-archived",
    label: "Architect list",
    description: "Whether the Architect page's created list shows archived agents and workflows.",
    keys: [ARCHITECT_SHOW_ARCHIVED_KEY],
    prefixes: [],
    clearGroup: "layout",
  },
  {
    id: "runs-show-skipped",
    label: "Skipped runs",
    description: "Whether the Traces list and a workflow's run history also list skipped runs (nothing to do).",
    keys: [RUNS_SHOW_SKIPPED_KEY],
    prefixes: [],
    clearGroup: "layout",
  },
  {
    id: "run-not-reached",
    label: "Steps not reached",
    description: "Whether a workflow run's step list shows the steps the run never reached (folded away unless opened).",
    keys: [NOT_REACHED_OPEN_KEY],
    prefixes: [],
    clearGroup: "layout",
  },
  {
    id: "workflow-layout",
    label: "Workflow page layout",
    description: "The workflow panel's width and whether it is collapsed, and whether the run Timeline is open and its height.",
    keys: [WORKFLOW_LAYOUT_STORAGE_KEY],
    prefixes: [],
    clearGroup: "layout",
  },
  {
    id: "trace-columns",
    label: "Trace list columns",
    description: "Which columns the Traces list shows.",
    keys: [TRACE_COLUMNS_STORAGE_KEY],
    prefixes: [],
    clearGroup: "layout",
  },
  {
    id: "memory-card",
    label: "Memory card",
    description: "Whether the chat's memory card is expanded into the memory inspector.",
    keys: [MEMORY_CARD_KEY],
    prefixes: [],
    clearGroup: "layout",
  },
  {
    id: "chat-protocol",
    label: "Chat protocol",
    description: "AG-UI or Native, for the agent Chat tab (Settings → Chat).",
    keys: [CHAT_PROTOCOL_STORAGE_KEY],
    prefixes: [],
    clearGroup: "layout",
  },
  {
    id: "last-agent",
    label: "Last agent",
    description: "The agent the chat reopens.",
    keys: [LAST_AGENT_KEY],
    prefixes: [],
    clearGroup: "layout",
  },
  {
    id: "request-context",
    label: "Request-context presets",
    description: "Named request-context presets for each agent (and the single saved value they were migrated from).",
    keys: [],
    prefixes: [PRESETS_PREFIX, SAVED_CONTEXT_PREFIX],
    clearGroup: "presets",
  },
];

/** localStorage, or null when it is unavailable or throws. */
export const browserStorage = safeLocalStorage;

function storedKeys(storage: Storage): string[] {
  const keys: string[] = [];
  for (let index = 0; index < storage.length; index += 1) {
    const key = storage.key(index);
    if (key !== null) keys.push(key);
  }
  return keys;
}

/** The keys of `entry` present in `storage`, sorted. */
export function keysFor(entry: BrowserDataEntry, storage: Storage | null = browserStorage()): string[] {
  if (!storage) return [];
  try {
    const exact = entry.keys.filter((key) => storage.getItem(key) !== null);
    const prefixed = entry.prefixes.length > 0 ? storedKeys(storage).filter((key) => entry.prefixes.some((prefix) => key.startsWith(prefix))) : [];
    return Array.from(new Set([...exact, ...prefixed])).sort();
  } catch {
    return [];
  }
}

/** How many keys each entry has stored now, by entry id. */
export function browserDataCounts(storage: Storage | null = browserStorage()): Record<string, number> {
  return Object.fromEntries(BROWSER_DATA.map((entry) => [entry.id, keysFor(entry, storage).length]));
}

export interface ClearResult {
  /** Keys removed. */
  removed: string[];
  /** Keys left in place because storage threw (the first failure and every key after it). */
  failed: string[];
}

/** Tell this tab's open listeners about cleared keys they follow (the `storage` event reaches only
 * other tabs): the Chat protocol switch and the Chat tab re-read their now-default value. */
function notifyCleared(removed: readonly string[]): void {
  if (!removed.includes(CHAT_PROTOCOL_STORAGE_KEY) || typeof window === "undefined") return;
  try {
    window.dispatchEvent(new CustomEvent(CHAT_PROTOCOL_EVENT, { detail: null }));
  } catch {
    // No CustomEvent: the next mount reads storage.
  }
}

/**
 * Removes every stored key of the entries in `group` and nothing else, one key at a time. If
 * storage throws partway, it stops there and reports what was removed and what was not. Null when
 * storage is unavailable.
 */
export function clearBrowserData(group: ClearGroup, storage: Storage | null = browserStorage()): ClearResult | null {
  if (!storage) return null;
  const keys = BROWSER_DATA.filter((entry) => entry.clearGroup === group).flatMap((entry) => keysFor(entry, storage));
  const removed: string[] = [];
  for (let index = 0; index < keys.length; index += 1) {
    try {
      storage.removeItem(keys[index]);
    } catch {
      notifyCleared(removed);
      return { removed, failed: keys.slice(index) };
    }
    removed.push(keys[index]);
  }
  notifyCleared(removed);
  return { removed, failed: [] };
}
