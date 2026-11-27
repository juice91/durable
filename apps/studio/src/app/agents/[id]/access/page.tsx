"use client";

import { useParams } from "next/navigation";
import { AccessSection } from "@/components/access/AccessSection";
import { ResourceAccessPanel } from "@/components/access/ResourceAccessPanel";

/** `/agents/:id/access`: who holds which relation on the agent, and the MCP servers and secrets it may use. */
export default function AgentAccessPage() {
  const id = String(useParams<{ id: string }>().id);
  return (
    <div className="mx-auto flex w-full min-w-0 max-w-3xl flex-col gap-6 px-4 py-6">
      <ResourceAccessPanel kind="agent" id={id} />
      <AccessSection
        kind="agent"
        id={id}
        heading={false}
        wrapper={(content) => (
          <div className="min-w-0">
            <h2 id={`access-${id}-title`} className="mb-1.5 text-label font-semibold text-foreground">
              What this agent may use
            </h2>
            {content}
          </div>
        )}
      />
    </div>
  );
}
