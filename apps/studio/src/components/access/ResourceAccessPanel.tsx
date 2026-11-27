"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { addAccessGrant, getResourceAccess, kindWord, removeAccessGrant, type AccessGrant, type AccessKind, type AccessView } from "@/lib/access";
import { api, ApiError } from "@/lib/api";
import { hasAccessTab } from "@/lib/iam";
import { listMembers, MEMBER_ROLES, type Member } from "@/lib/members";
import { routes } from "@/lib/routes";
import { subjectLabel } from "@/lib/subjects";
import { useCapabilities } from "@/lib/useIam";
import { listWorkflows } from "@/lib/workflows";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";

const relationLabel = (relation: string) => relation.charAt(0).toUpperCase() + relation.slice(1);

function holder(grant: AccessGrant): string {
  if (grant.subject_type !== "role") return subjectLabel(grant.subject);
  return grant.subject === "admin" ? "Everyone with the admin role" : `Everyone with the ${grant.subject} role or above`;
}

/** The names of the agents and workflows that use a secret or server, best effort (the id shows without one). */
function useNames(usedBy: AccessView["used_by"]): Record<string, string> {
  const [names, setNames] = useState<Record<string, string>>({});
  const needed = usedBy.length > 0;
  useEffect(() => {
    if (!needed) return;
    let live = true;
    const add = (items: { id: string; name: string }[]) => live && setNames((old) => ({ ...old, ...Object.fromEntries(items.map((i) => [i.id, i.name])) }));
    Promise.resolve().then(() => api.agents.list()).then(add).catch(() => undefined);
    Promise.resolve().then(() => listWorkflows()).then(add).catch(() => undefined);
    return () => {
      live = false;
    };
  }, [needed]);
  return names;
}

/**
 * The Access tab of an agent, workflow, knowledge base, secret or MCP server: who holds which of the policy model's
 * relations on it. Everyone who may read the resource sees the list; an admin adds a member or a tenant role and removes
 * grants. Under `openaccess` it only says that fine-grained access is off.
 */
export function ResourceAccessPanel({ kind, id }: { kind: AccessKind; id: string }) {
  const caps = useCapabilities();
  const enabled = hasAccessTab(caps ?? null);
  const [view, setView] = useState<AccessView | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [members, setMembers] = useState<Member[]>([]);
  const [who, setWho] = useState("");
  const [relation, setRelation] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        const answer = await getResourceAccess(kind, id, signal);
        if (signal?.aborted) return;
        setView(answer);
        setLoadError(null);
      } catch (caught) {
        if (signal?.aborted) return;
        setView(null);
        setLoadError(caught instanceof ApiError ? caught.message : "Access could not be read.");
      }
    },
    [kind, id],
  );
  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setView(null);
    void load(controller.signal);
    return () => controller.abort();
  }, [enabled, load]);

  const canManage = view?.can_manage === true;
  const hasMembership = Boolean(caps?.membership);
  useEffect(() => {
    if (!canManage || !hasMembership) return;
    let live = true;
    // A failure leaves the roles to pick from.
    Promise.resolve(listMembers()).then((list) => live && setMembers(list.items), () => undefined);
    return () => {
      live = false;
    };
  }, [canManage, hasMembership]);

  const names = useNames(view?.used_by ?? []);

  if (caps === undefined) return <p role="status" className="text-label text-muted-foreground">Loading access…</p>;
  if (!enabled || view?.supported === false) {
    return (
      <p data-testid="access-off" className="text-label text-muted-foreground">
        Fine-grained access is off: every signed-in person may do everything. It turns on with the OpenFGA policy.
      </p>
    );
  }
  if (loadError) return <p role="alert" data-testid="resource-access-load-error" className="text-label text-destructive-text">{loadError}</p>;
  if (!view) return <p role="status" className="text-label text-muted-foreground">Loading access…</p>;

  const role = who.startsWith("role:") ? who.slice(5) : null;
  const offered = view.relations.filter((r) => role === null || r.roles.includes(role));
  const picked = offered.some((r) => r.relation === relation) ? relation : "";
  const rolesOffered = MEMBER_ROLES.filter((r) => view.relations.some((rel) => rel.roles.includes(r)));

  const change = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await work();
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "The change was not saved.");
    } finally {
      setBusy(false);
    }
  };
  const add = () => {
    if (!who || !picked) return;
    const subject = role !== null ? { role } : { member_id: who.slice("member:".length) };
    setWho("");
    setRelation("");
    void change(() => addAccessGrant(kind, id, { relation: picked, ...subject }));
  };

  return (
    <section aria-label={`Who has access to this ${kindWord(kind)}`} data-testid="resource-access" className="flex min-w-0 flex-col gap-3">
      <h2 className="text-label font-semibold text-foreground">People and roles</h2>
      <p className="text-caption text-muted-foreground">
        Tenant roles already apply (admins manage everything here); these grants add rights on this {kindWord(kind)} only.
      </p>
      {view.grants.length === 0 ? (
        <p data-testid="resource-access-empty" className="text-label text-muted-foreground">No grants on this {kindWord(kind)}.</p>
      ) : (
        <ul aria-label="Grants" className="min-w-0 divide-y divide-border rounded-lg border border-border">
          {view.grants.map((grant) => (
            <li key={`${grant.relation}:${grant.subject_type}:${grant.subject}`} data-testid={`access-person-${grant.relation}-${grant.subject_type}-${grant.subject}`} className="flex min-w-0 flex-wrap items-center gap-2 px-3 py-2">
              <span className="min-w-0 flex-1 break-all text-label text-foreground">{holder(grant)}</span>
              <Badge variant="outline">{relationLabel(grant.relation)}</Badge>
              {canManage ? (
                <Button type="button" size="sm" variant="ghost" disabled={busy} aria-label={`Remove ${relationLabel(grant.relation)} for ${subjectLabel(grant.subject)}`} onClick={() => void change(() => removeAccessGrant(kind, id, grant))}>
                  Remove
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
      )}
      {canManage ? (
        <div data-testid="access-add" className="flex min-w-0 flex-wrap items-center gap-2">
          <Select value={who} onValueChange={setWho} aria-label="Who" placeholder="A member or a role…" disabled={busy} size="sm">
            {members.map((m) => (
              <option key={m.id} value={`member:${m.id}`}>{m.email ?? m.display_name ?? m.id}</option>
            ))}
            {rolesOffered.map((r) => (
              <option key={r} value={`role:${r}`}>{`Role: ${r}`}</option>
            ))}
          </Select>
          <Select value={picked} onValueChange={setRelation} aria-label="Relation" placeholder="Relation…" disabled={busy || who === ""} size="sm">
            {offered.map((r) => (
              <option key={r.relation} value={r.relation}>{relationLabel(r.relation)}</option>
            ))}
          </Select>
          <Button type="button" size="sm" disabled={busy || who === "" || picked === ""} onClick={add}>Add</Button>
        </div>
      ) : (
        <p data-testid="access-read-only" className="text-caption text-muted-foreground">Only an admin can change access.</p>
      )}
      {error ? <p role="alert" data-testid="resource-access-error" className="text-caption text-destructive-text">{error}</p> : null}
      {view.used_by.length > 0 ? (
        <div data-testid="access-used-by" className="flex min-w-0 flex-col gap-1">
          <h2 className="text-label font-semibold text-foreground">Agents and workflows that may use it</h2>
          <ul className="min-w-0">
            {view.used_by.map((user) => (
              <li key={`${user.kind}:${user.id}`} className="break-all text-label">
                <span className="text-muted-foreground">{user.kind === "agent" ? "Agent" : "Workflow"} </span>
                <Link href={user.kind === "agent" ? routes.agentTab(user.id, "access") : routes.workflow(user.id, { tab: "access" })} className="text-foreground underline underline-offset-4 hover:no-underline">
                  {names[user.id] ?? user.id}
                </Link>
              </li>
            ))}
          </ul>
          <p className="text-caption text-muted-foreground">Granted on the agent or workflow itself (its Access tab).</p>
        </div>
      ) : null}
    </section>
  );
}
