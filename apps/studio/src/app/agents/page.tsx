"use client";
import { Layout } from "@/components/layout/Layout";
import { EmptyState } from "@/components/shared/EmptyState";
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
      <Layout>
        <div className="flex items-center justify-center py-12">
          <p className="text-gray-500">Loading agents...</p>
        </div>
      </Layout>
    );
  }

  if (error) {
    return (
      <Layout>
        <div className="flex items-center justify-center py-12">
          <p className="text-red-500">{error}</p>
        </div>
      </Layout>
    );
  }

  return (
    <Layout>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">Agents</h1>
        <button
          onClick={handleNewAgent}
          className="inline-flex items-center px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2"
        >
          New agent
        </button>
      </div>
      <AgentsListUI agents={agents} onSelect={handleAgentSelect} />
    </Layout>
  );
}
