"use client";
import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { Agent } from "@/lib/api";
import { AgentsListUI } from "@/components/agents/AgentsList";

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
    router.push("/agents/new");
  };

  const handleAgentSelect = (id: string) => {
    router.push(`/agents/${id}`);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <p className="text-card-foreground/60">Loading agents...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center py-12">
        <p className="text-red-500">{error}</p>
      </div>
    );
  }

  return (
    <>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold text-card-foreground">Agents</h1>
        <button
          onClick={handleNewAgent}
          className="inline-flex items-center px-4 py-2 bg-accent text-accent-foreground rounded-md hover:bg-accent/90 focus:outline-none focus:ring-2 focus:ring-accent-foreground focus:ring-offset-2"
        >
          New agent
        </button>
      </div>
      <AgentsListUI agents={agents} onSelect={handleAgentSelect} />
    </>
  );
}
