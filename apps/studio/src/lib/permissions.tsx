"use client";

// What the signed-in person may do, for Studio to offer only those controls. The answer is the server's own
// (`GET /api/v1/iam/explain`: the caller's decision per action, on one resource, a collection or the tenant), so no role
// name is decided here. The server stays the enforcer: when permissions cannot be read, the control is offered and a
// refusal explains itself.
import React, { useEffect, useState } from "react";
import { explainAccess, type Explanation } from "./access";
import type { IamCapabilities } from "./iam";
import { canExplain, useCapabilities } from "./useIam";

export type PermissionKind = "agent" | "workflow" | "knowledge_base" | "secret" | "mcp_server" | "schedule" | "api_key" | "tenant";

export interface Permissions {
  /** The answer is not there yet: offer nothing, so a control never flashes and then vanishes. */
  loading: boolean;
  can: (action: string) => boolean;
}

/** An answer is shared by everything asking the same question, and asked again after this long. */
const FRESH_MS = 30_000;
// Keyed by the capabilities answer: a new sign-in or a refresh forgets every decision.
const answers = new WeakMap<IamCapabilities, Map<string, { at: number; answer: Promise<Explanation> }>>();

function ask(caps: IamCapabilities, kind: string, id: string | null, actions: string[]): Promise<Explanation> {
  let known = answers.get(caps);
  if (!known) answers.set(caps, (known = new Map()));
  const key = JSON.stringify([kind, id, actions]);
  const found = known.get(key);
  if (found && Date.now() - found.at < FRESH_MS) return found.answer;
  const answer = explainAccess(kind, id, actions);
  known.set(key, { at: Date.now(), answer });
  answer.catch(() => known.delete(key));
  return answer;
}

const NOTHING: Permissions = { loading: true, can: () => false };
const EVERYTHING: Permissions = { loading: false, can: () => true };

/**
 * The caller's decisions on `actions` (at most 8). `id` null asks about creating one (or about the tenant);
 * `undefined` means the resource is not loaded yet.
 */
export function usePermissions(kind: PermissionKind, id: string | null | undefined, actions: readonly string[]): Permissions {
  const caps = useCapabilities();
  const wanted = actions.join(",");
  const question = JSON.stringify([kind, id ?? null, wanted]);
  const [state, setState] = useState<{ question: string; refused: ReadonlySet<string> } | null>(null);
  const explains = canExplain(caps);

  useEffect(() => {
    if (!caps || !explains || id === undefined || wanted === "") return;
    let live = true;
    const done = (refused: string[]) => live && setState({ question, refused: new Set(refused) });
    Promise.resolve(ask(caps, kind, id, wanted.split(","))).then(
      (answer) => done((answer?.decisions ?? []).filter((d) => !d.allowed).map((d) => d.action)),
      () => done([]),
    );
    return () => {
      live = false;
    };
  }, [caps, explains, kind, id, wanted, question]);

  if (caps === undefined) return NOTHING;
  if (!explains) return EVERYTHING;
  if (state?.question !== question) return NOTHING;
  return { loading: false, can: (action) => !state.refused.has(action) };
}

/** Whether the caller may do one thing; false while that is not known. */
export function useCan(kind: PermissionKind, id: string | null | undefined, action: string): boolean {
  return usePermissions(kind, id, [action]).can(action);
}

/** `children` for someone who may do it, `fallback` for someone who may not, nothing while that is not known. */
export function Can({ kind, id = null, action, children, fallback = null }: { kind: PermissionKind; id?: string | null; action: string; children: React.ReactNode; fallback?: React.ReactNode }) {
  const permissions = usePermissions(kind, id, [action]);
  if (permissions.loading) return null;
  return <>{permissions.can(action) ? children : fallback}</>;
}
