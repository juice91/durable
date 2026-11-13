"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import type { AgentSpec } from "@/lib/api";
import { AgentEditor } from "@/components/agents/AgentEditor";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";

export default function NewAgentPage() {
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();

  const handleSubmit = async (spec: AgentSpec) => {
    try {
      const agent = await api.agents.create(spec);
      router.push(routes.agentEditor(agent.id));
    } catch (err: unknown) {
      setError(err instanceof Error && err.message ? err.message : "Failed to create agent");
    }
  };

  return (
    <>
      <PageHeader crumbs={[navCrumb("agents"), { label: "New Agent" }]} />
      <div className="max-w-3xl mx-auto px-4 py-6">
        <div className="mb-4">
          {error && (
            <div className="p-3 border rounded-md text-sm border-destructive/40 bg-destructive/10 text-destructive-text">
              {error}
            </div>
          )}
        </div>
        <AgentEditor onSubmit={handleSubmit} submitLabel="Create" />
      </div>
    </>
  );
}
