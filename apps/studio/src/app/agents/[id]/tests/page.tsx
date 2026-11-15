"use client";

import { Suspense } from "react";
import { useParams } from "next/navigation";
import { TestsTab } from "@/components/evals/TestsTab";

/** `/agents/:id/tests?suite=&run=&compare=`: the agent's test suites (AC.16). */
export default function AgentTestsPage() {
  const params = useParams<{ id: string }>();
  // TestsTab reads its URL state (useSearchParams), which needs a Suspense boundary.
  return (
    <Suspense fallback={null}>
      <TestsTab agentId={String(params.id)} />
    </Suspense>
  );
}
