"use client";
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/api";
import { AgentsListUI } from "@/components/agents/AgentsList";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";
import { Button } from "@/components/ui/Button";

export default function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadAgents = useCallback(async () => {
    try {
      setLoading(true);
      const data = await api.agents.list();
      setAgents(data);
      setError(null);
    } catch {
      setError("Failed to load agents");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadAgents();
  }, [loadAgents]);

  return (
    <>
      <PageHeader
        crumbs={[navCrumb("agents", { current: true })]}
        actions={
          <Button asChild variant="primary" size="sm">
            <Link href={routes.agentNew()}>New agent</Link>
          </Button>
        }
      />
      <div className="min-w-0 px-4 py-6 md:px-6">
        <AgentsListUI agents={agents} loading={loading} error={error} onRetry={() => void loadAgents()} />
      </div>
    </>
  );
}
