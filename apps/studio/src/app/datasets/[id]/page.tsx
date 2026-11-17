"use client";

import { Suspense } from "react";
import { useParams } from "next/navigation";
import { DatasetDetail } from "@/components/evals/overview/DatasetDetail";

/** `/datasets/:id`: one dataset (a test suite of an agent or a workflow) with its items. */
export default function DatasetRoute() {
  const params = useParams<{ id: string }>();
  // The suite view reads its URL state (the editor Sheet), which needs a Suspense boundary.
  return (
    <Suspense fallback={null}>
      <DatasetDetail suiteId={String(params.id)} />
    </Suspense>
  );
}
