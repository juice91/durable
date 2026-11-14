"use client";

import { Suspense } from "react";
import { useParams } from "next/navigation";
import { MemoryTab } from "@/components/memory/MemoryTab";

/** `/agents/:id/memory?resource=`: the agent's memory per user (RM.18). */
export default function AgentMemoryPage() {
  const params = useParams<{ id: string }>();
  // MemoryTab reads `?resource=` (useSearchParams), which needs a Suspense boundary.
  return (
    <Suspense fallback={null}>
      <MemoryTab agentId={String(params.id)} />
    </Suspense>
  );
}
