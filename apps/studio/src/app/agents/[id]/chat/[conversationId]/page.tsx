"use client";

import { useParams } from "next/navigation";
import { ChatTab } from "@/components/agentPage/ChatTab";

/** `/agents/:id/chat/:conversationId`: one conversation, rebuilt from the API. */
export default function AgentConversationPage() {
  const params = useParams<{ id: string; conversationId: string }>();
  return <ChatTab agentId={String(params.id)} conversationId={String(params.conversationId)} />;
}
