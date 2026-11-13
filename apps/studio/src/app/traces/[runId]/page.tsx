"use client";

import React from "react";
import { useParams } from "next/navigation";
import { TraceView } from "@/components/traces/TraceView";

export default function TraceDetailPage() {
  const params = useParams();
  const runId = typeof params.runId === "string" ? params.runId : undefined;
  if (!runId) return null;
  return (
    <div className="mx-auto max-w-7xl px-4 py-8">
      <TraceView runId={runId} />
    </div>
  );
}
