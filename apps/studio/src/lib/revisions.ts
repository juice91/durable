// Optimistic concurrency (ETag / If-Match) for the definition-class resources: agents, workflows,
// schedules, eval suites and cases, connections, kb sources, scorers and MCP servers.
//
// The API serves each resource's `revision` (body field, and `ETag` on single-resource reads and
// writes). This module remembers the last one this tab saw per resource and sends it as `If-Match` on
// every PATCH; a stale write answers 412 and surfaces as ConflictError (see useConflict.ts and
// components/shared/ConflictNotice.tsx for the page side). A resource nobody has read yet is written
// without the header (the API allows that for now).
import type { ConflictError } from "./api";

// Duck-typed on purpose: many tests mock "@/lib/api" partially, and an `instanceof` against a class that
// mock does not define would throw.
export function isConflict(error: unknown): error is ConflictError {
  return typeof error === "object" && error !== null && (error as { status?: unknown }).status === 412 && typeof (error as { currentRevision?: unknown }).currentRevision === "number";
}

const remembered = new Map<string, number>();

/** The keys the helpers use: one per resource, so a list, a GET and a PATCH of it share a revision. */
export const revisionKey = {
  agent: (id: string) => `agent:${id}`,
  workflow: (id: string) => `workflow:${id}`,
  schedule: (id: string) => `schedule:${id}`,
  suite: (id: string) => `suite:${id}`,
  evalCase: (suiteId: string, id: string) => `case:${suiteId}:${id}`,
  connection: (id: string) => `connection:${id}`,
  source: (id: string) => `source:${id}`,
  scorer: (id: string) => `scorer:${id}`,
  mcpServer: (id: string) => `mcp:${id}`,
};

export function rememberRevision(key: string, revision: unknown): void {
  if (typeof revision === "number" && Number.isInteger(revision) && revision >= 1) remembered.set(key, revision);
}

export function recallRevision(key: string): number | undefined {
  return remembered.get(key);
}

export function forgetRevisions(): void {
  remembered.clear();
}

/** `ETag` header value -> revision (`"3"`, `W/"3"` or `3`), or undefined. */
export function revisionFromEtag(etag: string | null | undefined): number | undefined {
  const found = /^(?:W\/)?"?(\d{1,9})"?$/.exec((etag ?? "").trim());
  return found ? Number(found[1]) : undefined;
}

/** Remember a resource's `revision` under `key` and hand the resource back (use on every read). */
export function track<T>(key: string, resource: T): T {
  if (resource && typeof resource === "object") rememberRevision(key, (resource as { revision?: unknown }).revision);
  return resource;
}

/** Remember every item of a list under `keyOf(item)`. */
export function trackAll<T extends { revision?: number }>(items: T[], keyOf: (item: T) => string): T[] {
  for (const item of items) rememberRevision(keyOf(item), item.revision);
  return items;
}

/** The `If-Match` header for `key`, or nothing when this tab has not read the resource. */
export function ifMatch(key: string): Record<string, string> {
  const revision = remembered.get(key);
  return revision === undefined ? {} : { "If-Match": `"${revision}"` };
}

/**
 * Send one write of the resource `key`: `send` receives the `If-Match` header to put on the request and
 * returns the saved resource, whose new `revision` is remembered. A 412 rethrows as ConflictError
 * carrying `key`, so `useConflict` can resend with the current revision (overwrite) or reload.
 */
export async function writeResource<T>(key: string, send: (headers: Record<string, string>) => Promise<T>): Promise<T> {
  try {
    return track(key, await send(ifMatch(key)));
  } catch (error) {
    if (isConflict(error)) error.key = key;
    throw error;
  }
}
