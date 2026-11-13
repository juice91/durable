"use client";

import { useParams } from "next/navigation";
import { ChatTab } from "@/components/agentPage/ChatTab";

/** `/agents/:id/chat`: a new conversation with the agent. */
export default function AgentChatPage() {
  const params = useParams<{ id: string }>();
  return <ChatTab agentId={String(params.id)} />;
}
