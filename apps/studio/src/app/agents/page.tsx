"use client";
import { PermissionNotice } from "@/components/access/PermissionNotice";
import { useCan } from "@/lib/permissions";
import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/api";
import { AgentsListUI, scopeFromParams, type AgentListItem, type AgentsScope } from "@/components/agents/AgentsList";
import { readShowSystemAgents, writeShowSystemAgents } from "@/components/agentPage/agentPageModel";
import { unarchiveAgent } from "@/components/agentPage/AgentArchiveMenu";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";
import { Button } from "@/components/ui/Button";
import { useCreateAction } from "@/components/shortcuts/ShortcutsProvider";

/** What "why can't I?" asks about on the list. */
const CREATE_ACTIONS = ["create"] as const;

function AgentsPageBody() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const scope = scopeFromParams(searchParams ? new URLSearchParams(searchParams.toString()) : null);
  // `c` is "New agent" (decision "Keyboard shortcuts").
  const mayCreate = useCan("agent", null, "create");
  useCreateAction(mayCreate ? () => router.push(routes.agentNew()) : null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Read after mount: the server render cannot know the stored choice.
  const [showSystem, setShowSystem] = useState(false);
  const [showSystemRead, setShowSystemRead] = useState(false);
  useEffect(() => {
    setShowSystem(readShowSystemAgents());
    setShowSystemRead(true);
  }, []);
  const [unarchivingId, setUnarchivingId] = useState<string | null>(null);

  const loadAgents = useCallback(async (which: AgentsScope, system: boolean, isCurrent: () => boolean = () => true) => {
    try {
      setLoading(true);
      const data = which === "archived" ? await api.agents.listArchived() : await api.agents.list(undefined, system ? { includeSystem: true } : undefined);
      if (!isCurrent()) return;
      setAgents(data);
      setError(null);
    } catch {
      if (isCurrent()) setError(which === "archived" ? "Failed to load archived agents" : "Failed to load agents");
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!showSystemRead) return;
    let current = true;
    void loadAgents(scope, showSystem, () => current);
    return () => {
      current = false;
    };
  }, [scope, showSystem, showSystemRead, loadAgents]);

  const onUnarchive = async (agent: AgentListItem) => {
    setUnarchivingId(agent.id);
    const ok = await unarchiveAgent(agent, () => undefined);
    setUnarchivingId(null);
    if (ok) setAgents((list) => list.filter((a) => a.id !== agent.id));
  };

  return (
    <>
      <PageHeader
        crumbs={[navCrumb("agents", { current: true })]}
        actions={
          mayCreate ? (
            <Button asChild variant="primary" size="sm">
              <Link href={routes.agentNew()} aria-keyshortcuts="c">New agent</Link>
            </Button>
          ) : null
        }
      />
      <div className="min-w-0 px-4 py-6 md:px-6">
        {mayCreate ? null : <PermissionNotice kind="agent" id={null} actions={CREATE_ACTIONS} className="mb-3 flex min-w-0 flex-col gap-2 empty:hidden" />}
        <AgentsListUI
          agents={agents}
          loading={loading}
          error={error}
          onRetry={() => void loadAgents(scope, showSystem)}
          scope={scope}
          onScopeChange={(next) => router.replace(routes.agents({ archived: next === "archived" }))}
          showSystem={showSystem}
          onShowSystemChange={(next) => {
            setShowSystem(next);
            writeShowSystemAgents(next);
          }}
          onUnarchive={(agent) => void onUnarchive(agent)}
          unarchivingId={unarchivingId}
        />
      </div>
    </>
  );
}

export default function AgentsPage() {
  return (
    <Suspense fallback={<PageHeader crumbs={[navCrumb("agents", { current: true })]} />}>
      <AgentsPageBody />
    </Suspense>
  );
}
