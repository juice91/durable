// A refused or unanswerable call, handed from the API client to whoever explains it (`RefusalNotice`, "why can't I?").
// The API names what the policy engine refused on a 403 (`refused`); a 503 `authz_unavailable` means it could not answer.

export type Refusal =
  | { type: "refused"; action: string; kind: string; id: string | null }
  | { type: "unavailable" };

const listeners = new Set<(refusal: Refusal) => void>();

export function onRefusal(listener: (refusal: Refusal) => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function reportRefusal(refusal: Refusal): void {
  listeners.forEach((listener) => listener(refusal));
}

/** What a 403's body names as refused by the policy engine, else null. */
export function refusedOf(status: number, body: unknown): { action: string; kind: string; id: string | null } | null {
  if (status !== 403 || !body || typeof body !== "object") return null;
  const refused = (body as { refused?: { action?: unknown; kind?: unknown; id?: unknown } }).refused;
  if (!refused || typeof refused.action !== "string" || typeof refused.kind !== "string") return null;
  return { action: refused.action, kind: refused.kind, id: typeof refused.id === "string" ? refused.id : null };
}

/** Reads an error answer's body for a refusal of the policy engine; anything else is ignored. */
export function reportFromBody(status: number, body: unknown): void {
  if (!body || typeof body !== "object") return;
  if (status === 503 && (body as { code?: unknown }).code === "authz_unavailable") reportRefusal({ type: "unavailable" });
  const refused = refusedOf(status, body);
  if (refused) reportRefusal({ type: "refused", ...refused });
}
