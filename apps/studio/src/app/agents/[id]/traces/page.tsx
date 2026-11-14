"use client";

import { useParams } from "next/navigation";
import { TracesTab } from "@/components/agentPage/TracesTab";

/** `/agents/:id/traces`: the agent's runs. */
export default function AgentTracesPage() {
  const params = useParams<{ id: string }>();
  return <TracesTab agentId={String(params.id)} />;
}
