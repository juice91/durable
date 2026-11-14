"use client";

import React from "react";
import { useParams } from "next/navigation";
import { AgentLayout } from "@/components/agentPage/AgentLayout";

/** The agent page's shared header, tabs and Config panel: switching tabs keeps the loaded agent. */
export default function AgentIdLayout({ children }: { children: React.ReactNode }) {
  const params = useParams<{ id: string }>();
  return <AgentLayout agentId={String(params.id)}>{children}</AgentLayout>;
}
