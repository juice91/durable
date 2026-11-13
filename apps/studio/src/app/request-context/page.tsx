import { redirect } from "next/navigation";
import { legacyTarget, routes } from "@/lib/routes";

/**
 * `/request-context` has no page of its own since SL.10: values are set per run in the chat's request
 * context popover (with named presets) and fields are edited in the editor. `?agent=A&version=V` opens
 * A's chat with that version and the popover open; without an agent it goes to the agents list (the
 * SL.10 row of `lib/routes/legacy.ts`).
 */
export default async function RequestContextPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  redirect(legacyTarget(routes.requestContext(), await searchParams) ?? routes.agents());
}
