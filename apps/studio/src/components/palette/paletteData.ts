import { api } from "@/lib/api";
import { mcpServers } from "@/lib/mcpServers";
import { routes } from "@/lib/routes";
import { listTools } from "@/lib/tools";
import { listWorkflows } from "@/lib/workflows";

/**
 * What the command palette searches besides pages and commands: the entities from the list
 * endpoints Studio already calls, fetched when the palette opens and cached for 60 s.
 */

export type EntityKind = "agents" | "workflows" | "tools" | "mcp";

export interface PaletteEntity {
  kind: EntityKind;
  id: string;
  label: string;
  /** A second line (description, URL). */
  detail?: string;
  href: string;
}

export interface EntitySource {
  kind: EntityKind;
  /** Group heading and scope label. */
  title: string;
  /** "Couldn't load {noun}." */
  noun: string;
  /** The test id of its "Couldn't load" row. */
  testId: string;
  load: () => Promise<PaletteEntity[]>;
}

/** Where an entity row goes. One place, so the moves in SL.6 (agent chat), SL.8 (MCP detail) and SL.13 (tool Sheet) edit one line each. */
export const entityHref = {
  // The agent page's Chat tab (SL.6).
  agent: (id: string): string => routes.agentChat(id),
  workflow: (id: string): string => routes.workflow(id),
  // The tools page with that tool's Sheet open (SL.13).
  tool: (id: string): string => routes.tool(id),
  // The server's detail page (SL.8).
  mcp: (id: string): string => routes.mcpServer(id),
};

export const ENTITY_SOURCES: readonly EntitySource[] = [
  {
    kind: "agents",
    testId: "command-palette-error-agents",
    title: "Agents",
    noun: "agents",
    load: async () =>
      (await api.agents.list()).map((agent) => ({
        kind: "agents" as const,
        id: agent.id,
        label: agent.name,
        detail: agent.draft_spec?.model ? `${agent.draft_spec.model.provider}:${agent.draft_spec.model.name}` : undefined,
        href: entityHref.agent(agent.id),
      })),
  },
  {
    kind: "workflows",
    testId: "command-palette-error-workflows",
    title: "Workflows",
    noun: "workflows",
    load: async () =>
      (await listWorkflows()).map((workflow) => ({
        kind: "workflows" as const,
        id: workflow.id,
        label: workflow.name,
        href: entityHref.workflow(workflow.id),
      })),
  },
  {
    kind: "tools",
    testId: "command-palette-error-tools",
    title: "Tools",
    noun: "tools",
    load: async () =>
      (await listTools()).map((tool) => ({
        kind: "tools" as const,
        id: tool.id,
        label: tool.title || tool.id,
        detail: tool.title && tool.title !== tool.id ? tool.id : undefined,
        href: entityHref.tool(tool.id),
      })),
  },
  {
    kind: "mcp",
    testId: "command-palette-error-mcp",
    title: "MCP servers",
    noun: "MCP servers",
    load: async () =>
      (await mcpServers.list()).map((server) => ({
        kind: "mcp" as const,
        id: server.id,
        label: server.name,
        detail: server.url,
        href: entityHref.mcp(server.id),
      })),
  },
];

export const PALETTE_CACHE_MS = 60_000;

/** Without a query, each entity kind shows at most this many rows; typing searches all of them. */
export const PALETTE_ENTITY_LIMIT = 50;

const cache = new Map<EntityKind, { at: number; items: PaletteEntity[] }>();
// A load in flight, shared by a quick close and reopen so the list is fetched once.
const inflight = new Map<EntityKind, Promise<PaletteEntity[]>>();

/** The cached entities of `kind` when fetched less than 60 s before `now`, else null. */
export function cachedEntities(kind: EntityKind, now: number = Date.now()): PaletteEntity[] | null {
  const entry = cache.get(kind);
  return entry && now - entry.at < PALETTE_CACHE_MS ? entry.items : null;
}

/**
 * Loads one source through the cache. A load already in flight is reused. Failures are not cached,
 * so the next open retries.
 */
export function loadEntities(source: EntitySource, now: () => number = Date.now): Promise<PaletteEntity[]> {
  const cached = cachedEntities(source.kind, now());
  if (cached) return Promise.resolve(cached);
  const pending = inflight.get(source.kind);
  if (pending) return pending;
  let started: Promise<PaletteEntity[]>;
  try {
    started = Promise.resolve(source.load());
  } catch (error) {
    started = Promise.reject(error);
  }
  const promise = started.then(
      (items) => {
        cache.set(source.kind, { at: now(), items });
        inflight.delete(source.kind);
        return items;
      },
      (error: unknown) => {
        inflight.delete(source.kind);
        throw error;
      },
    );
  inflight.set(source.kind, promise);
  return promise;
}

/** Tests only: forget every cached list and every load in flight. */
export function resetPaletteCache(): void {
  cache.clear();
  inflight.clear();
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** The run id in the query when it looks like one (a UUID, surrounding spaces ignored), else null. */
export function runIdFromQuery(query: string): string | null {
  const trimmed = query.trim();
  return UUID.test(trimmed) ? trimmed.toLowerCase() : null;
}

export type PaletteScope = "all" | "pages" | EntityKind | "commands";

export const PALETTE_SCOPES: readonly { value: PaletteScope; label: string }[] = [
  { value: "all", label: "All" },
  { value: "pages", label: "Pages" },
  { value: "agents", label: "Agents" },
  { value: "workflows", label: "Workflows" },
  { value: "tools", label: "Tools" },
  { value: "mcp", label: "MCP servers" },
  { value: "commands", label: "Commands" },
];

export function scopeShows(scope: PaletteScope, group: PaletteScope): boolean {
  return scope === "all" || scope === group;
}
