import { redirect } from "next/navigation";
import { legacyTarget, RouteIdError, routes } from "@/lib/routes";

/**
 * `/agents/:id` opens the agent's chat (SL.6; it was the editor, now `/agents/:id/editor`). The rule
 * is the SL.6 row of `lib/routes/legacy.ts`.
 */
export default async function AgentIndexPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { id } = await params;
  const search = await searchParams;
  let target: string;
  try {
    target = legacyTarget(routes.agentIndex(id), search) ?? routes.agentChat(id);
  } catch (error) {
    // An id no path can carry ("." or ".."): the agents list.
    if (!(error instanceof RouteIdError)) throw error;
    target = routes.agents();
  }
  redirect(target);
}
