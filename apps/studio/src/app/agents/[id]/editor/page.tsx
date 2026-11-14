"use client";

import { Suspense } from "react";
import { useParams } from "next/navigation";
import { EditorTab } from "@/components/agentPage/EditorTab";

/** `/agents/:id/editor?step=&compare=`: the stepped editor, versions and the compare Sheet (SL.11). */
export default function AgentEditorPage() {
  const params = useParams<{ id: string }>();
  // EditorTab reads `?step=` and `?compare=` (useSearchParams), which needs a Suspense boundary.
  return (
    <Suspense fallback={null}>
      <EditorTab agentId={String(params.id)} />
    </Suspense>
  );
}
