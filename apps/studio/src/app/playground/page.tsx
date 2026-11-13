import { redirect } from "next/navigation";
import { legacyTarget, routes } from "@/lib/routes";
import { PlaygroundRedirect } from "@/components/agentPage/PlaygroundRedirect";

/**
 * `/playground` has no page of its own since SL.6: chat lives on the agent page. With `?agent=` the
 * server redirects to that agent's chat with the same conversation and options (the SL.6 row of
 * `lib/routes/legacy.ts`); without one, the client picks the last-used agent, else the first.
 */
export default async function PlaygroundPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const target = legacyTarget(routes.playground(), await searchParams);
  if (target) redirect(target);
  return <PlaygroundRedirect />;
}
