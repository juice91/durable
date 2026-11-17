import { BASE_URL, apiErrorFromResponse, apiHeaders } from "@/lib/api";

export type ProviderState = "ok" | "unreachable" | "not_configured";

/** An entry of `GET /api/v1/providers`: a model provider the server can reach. Never carries a key. */
export interface ProviderStatus {
  id: string;
  label: string;
  configured: boolean;
  /** The NAME of the server environment variable that holds the key. */
  env_var: string;
  /** `host[:port]` of the provider's API; no scheme, path or credentials. */
  base_url_host: string | null;
  status: ProviderState;
  model_count: number;
  checked_at: string | null;
  error: string | null;
}

export const PROVIDER_STATE_LABELS: Record<ProviderState, string> = {
  ok: "Connected",
  unreachable: "Unreachable",
  not_configured: "Not configured",
};

export const PROVIDER_STATE_VARIANTS: Record<ProviderState, "success" | "warning" | "neutral"> = {
  ok: "success",
  unreachable: "warning",
  not_configured: "neutral",
};

function isProviderStatus(value: unknown): value is ProviderStatus {
  const row = value as Partial<ProviderStatus> | null;
  return (
    typeof row === "object" && row !== null && typeof row.id === "string" && typeof row.label === "string" &&
    (row.status === "ok" || row.status === "unreachable" || row.status === "not_configured")
  );
}

async function send<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, { ...init, headers: apiHeaders({ "Content-Type": "application/json" }) });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json() as Promise<T>;
}

/** `GET /api/v1/providers` (server-cached about 5 minutes). Rows of another shape are dropped. */
export async function fetchProviders(): Promise<ProviderStatus[]> {
  const data = await send<{ items?: unknown }>("/api/v1/providers");
  return Array.isArray(data?.items) ? data.items.filter(isProviderStatus) : [];
}

/** `POST /api/v1/providers/{id}/test`: a fresh check that bypasses the server's cache. */
export async function testProvider(id: string): Promise<ProviderStatus> {
  const data = await send<unknown>(`/api/v1/providers/${encodeURIComponent(id)}/test`, { method: "POST" });
  if (!isProviderStatus(data)) throw new Error("Unexpected answer from the provider test");
  return data;
}
