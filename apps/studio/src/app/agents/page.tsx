"use client";
import { Suspense, useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/api";
import { AgentsListUI, scopeFromParams, type AgentListItem, type AgentsScope } from "@/components/agents/AgentsList";
import { unarchiveAgent } from "@/components/agentPage/AgentArchiveMenu";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";
import { Button } from "@/components/ui/Button";
import { useCreateAction } from "@/components/shortcuts/ShortcutsProvider";

function AgentsPageBody() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const scope = scopeFromParams(searchParams ? new URLSearchParams(searchParams.toString()) : null);
  // `c` is "New agent" (decision "Keyboard shortcuts").
  useCreateAction(() => router.push(routes.agentNew()));
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [unarchivingId, setUnarchivingId] = useState<string | null>(null);

  const loadAgents = useCallback(async (which: AgentsScope, isCurrent: () => boolean = () => true) => {
    try {
      setLoading(true);
      const data = which === "archived" ? await api.agents.listArchived() : await api.agents.list();
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
    let current = true;
    setAgents([]);
    void loadAgents(scope, () => current);
    return () => {
      current = false;
    };
  }, [scope, loadAgents]);

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
          <Button asChild variant="primary" size="sm">
            <Link href={routes.agentNew()} aria-keyshortcuts="c">New agent</Link>
          </Button>
        }
      />
      <div className="min-w-0 px-4 py-6 md:px-6">
        <AgentsListUI
          agents={agents}
          loading={loading}
          error={error}
          onRetry={() => void loadAgents(scope)}
          scope={scope}
          onScopeChange={(next) => router.replace(routes.agents({ archived: next === "archived" }))}
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
