"use client";
import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
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
  const router = useRouter();

  useEffect(() => {
    const loadAgents = async () => {
      try {
        setLoading(true);
        const data = await api.agents.list();
        setAgents(data);
        setError(null);
      } catch (err) {
        setError("Failed to load agents");
      } finally {
        setLoading(false);
      }
    };
    loadAgents();
  }, []);

  const handleNewAgent = () => {
    router.push(routes.agentNew());
  };

  const handleAgentSelect = (id: string) => {
    router.push(routes.agentEditor(id));
  };

  const header = (
    <PageHeader
      crumbs={[navCrumb("agents", { current: true })]}
      actions={
        <Button variant="primary" size="sm" onClick={handleNewAgent}>
          New agent
        </Button>
      }
    />
  );

  if (loading) {
    return (
      <>
        {header}
        <div className="flex items-center justify-center py-12">
          <p className="text-card-foreground/60">Loading agents...</p>
        </div>
      </>
    );
  }

  if (error) {
    return (
      <>
        {header}
        <div className="flex items-center justify-center py-12">
          <p className="text-destructive-text">{error}</p>
        </div>
      </>
    );
  }

  return (
    <>
      {header}
      <div className="px-4 py-6 md:px-6">
        <AgentsListUI agents={agents} onSelect={handleAgentSelect} />
      </div>
    </>
  );
}
