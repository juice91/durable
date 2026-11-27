"use client";

import React, { useEffect, useState } from "react";
import { ACCESS_KINDS, askFor, explainAccess, getResourceAccess, type AccessKind, type Decision } from "@/lib/access";
import { hasAccessTab } from "@/lib/iam";
import { subjectLabel } from "@/lib/subjects";
import { canExplain, useCapabilities } from "@/lib/useIam";

/**
 * "Why can't I?" on a resource's page: its owner, and for each of `actions` the caller is refused, why and what to ask an
 * admin for (the policy engine's own answer, about the caller only). `id` null asks about creating one. Renders nothing
 * under `openaccess`, while loading, or when it cannot be read.
 */
export function PermissionNotice({ kind, id, actions, className }: { kind: AccessKind | "api_key" | "tenant" | "schedule"; id: string | null; actions: readonly string[]; className?: string }) {
  const caps = useCapabilities();
  const [refused, setRefused] = useState<Decision[]>([]);
  const [owners, setOwners] = useState<string[]>([]);
  const [roles, setRoles] = useState<string[]>([]);
  const explains = canExplain(caps);
  const grants = hasAccessTab(caps ?? null);
  const wanted = actions.join(",");

  useEffect(() => {
    setRefused([]);
    setOwners([]);
    if (!explains) return;
    const controller = new AbortController();
    explainAccess(kind, id, wanted.split(","), controller.signal).then(
      (answer) => {
        if (controller.signal.aborted) return;
        setRoles(answer.roles ?? []);
        setRefused(answer.decisions.filter((d) => !d.allowed));
      },
      () => undefined,
    );
    if (grants && id !== null && (ACCESS_KINDS as readonly string[]).includes(kind)) {
      getResourceAccess(kind as AccessKind, id, controller.signal).then(
        (view) => {
          if (controller.signal.aborted) return;
          const names = (view.grants ?? []).filter((g) => g.relation === "owner" && g.subject_type === "user").map((g) => subjectLabel(g.subject));
          setOwners([...new Set(names)]);
        },
        () => undefined,
      );
    }
    return () => controller.abort();
  }, [explains, grants, kind, id, wanted]);

  if (!explains || (refused.length === 0 && owners.length === 0)) return null;
  // An unavailable engine refuses every action with one sentence: say it once.
  const lines = [...new Set(refused.map((d) => askFor(d, kind, roles)))];
  return (
    <div className={className ?? "flex min-w-0 flex-col gap-2"}>
      {owners.length > 0 ? (
        <p data-testid="resource-owner" className="break-words text-caption text-muted-foreground">
          Owner: {owners.join(", ")}
        </p>
      ) : null}
      {lines.length > 0 ? (
        <ul data-testid="why-not" aria-label="Why some actions are not available" className="space-y-1 rounded-md border border-border bg-muted/40 p-3 text-label text-foreground">
          {lines.map((line) => (
            <li key={line} className="break-words">
              {line}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
