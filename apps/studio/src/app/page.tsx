import { redirect } from "next/navigation";
import { legacyTarget, routes } from "@/lib/routes";

/**
 * `/` has no page of its own since SL.7: the P0.18 dashboard folded into the Metrics page's Overview
 * header. A dashboard link (`range`, `from` or `to`) opens Metrics on the same range; a
 * bare `/` opens Agents. The rule is the SL.7 row of `lib/routes/legacy.ts`.
 */
export default async function RootPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  redirect(legacyTarget("/", await searchParams) ?? routes.agents());
}
