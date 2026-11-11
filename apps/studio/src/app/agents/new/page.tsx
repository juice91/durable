"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { AgentSpec } from "@/lib/api";
import { AgentEditor } from "@/components/agents/AgentEditor";

export default function NewAgentPage() {
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();

  const handleSubmit = async (spec: AgentSpec) => {
    try {
      const agent = await api.agents.create(spec);
      router.push(`/agents/${agent.id}`);
    } catch (err: unknown) {
      setError(err instanceof Error && err.message ? err.message : "Failed to create agent");
    }
  };

  return (
    <div className="max-w-3xl mx-auto">
        <h1 className="text-2xl font-bold mb-6">New Agent</h1>
        <div className="mb-4">
          {error && (
            <div className="p-3 bg-red-50 border border-red-200 rounded-md text-red-700 text-sm">
              {error}
            </div>
          )}
        </div>
        <AgentEditor onSubmit={handleSubmit} submitLabel="Create" />
    </div>
  );
}
