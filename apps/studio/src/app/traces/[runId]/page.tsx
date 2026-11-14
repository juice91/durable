import { redirect } from "next/navigation";
import { routes } from "@/lib/routes";
import { traceRedirectTarget } from "@/components/traces/traceRedirect";

/**
 * `/traces/:runId` has no page of its own since SL.12: the trace is a Sheet over the Traces list
 * (`/traces?trace=:runId`), per the SL.12 row of the legacy table.
 */
export default async function TraceDetailRedirect({
  params,
  searchParams,
}: {
  params: Promise<{ runId: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const [{ runId }, search] = await Promise.all([params, searchParams]);
  redirect(traceRedirectTarget(() => routes.legacyTracePath(runId), search));
}
