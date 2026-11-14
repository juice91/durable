import { redirect } from "next/navigation";
import { routes } from "@/lib/routes";
import { traceRedirectTarget } from "@/components/traces/traceRedirect";

/**
 * `/runs/:id` has no page of its own since SL.12: a run's details are the trace Sheet's Events tab
 * (`/traces?trace=:id&tab=events`), per the SL.12 row of the legacy table.
 */
export default async function RunDetailRedirect({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const [{ id }, search] = await Promise.all([params, searchParams]);
  redirect(traceRedirectTarget(() => routes.legacyRunPath(id), search));
}
