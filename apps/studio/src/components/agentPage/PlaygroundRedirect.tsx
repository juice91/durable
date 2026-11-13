"use client";

import React, { useEffect } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { getConversation } from "@/lib/conversations";
import { routes, type AgentChatRouteOptions } from "@/lib/routes";
import { parseVersionParam } from "@/components/playground/runOptions";
import { playgroundFallbackTarget, readLastAgent } from "./agentPageModel";

/**
 * Where an old `/playground` link with no `agent` goes now (the server handles `?agent=`): with a
 * `conversation`, that conversation's agent's chat; otherwise the last-used agent's chat, else the
 * first agent's, else the agents list. Version, model and context options are carried.
 */
export async function playgroundClientTarget(search: string): Promise<string> {
  const params = new URLSearchParams(search);
  const options: Omit<AgentChatRouteOptions, "conversation"> = {
    version: parseVersionParam(params.get("version")),
    model: params.get("model"),
    context: params.get("context"),
  };
  const conversation = params.get("conversation");
  if (conversation) {
    try {
      const found = await getConversation(conversation);
      return routes.agentChat(found.agent_id, { ...options, conversation: found.id });
    } catch {
      // Gone or unreadable: fall back to an agent's new conversation.
    }
  }
  try {
    const agents = await api.agents.list();
    return playgroundFallbackTarget(readLastAgent(), Array.isArray(agents) ? agents : [], options);
  } catch {
    return routes.agents();
  }
}

export function PlaygroundRedirect() {
  const router = useRouter();
  useEffect(() => {
    let cancelled = false;
    void playgroundClientTarget(window.location.search).then((target) => {
      if (!cancelled) router.replace(target);
    });
    return () => {
      cancelled = true;
    };
  }, [router]);
  return (
    <div className="flex items-center justify-center py-12">
      <p role="status" data-testid="playground-redirect" className="text-muted-foreground">
        Opening the agent chat…
      </p>
    </div>
  );
}
