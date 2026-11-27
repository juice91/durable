// External access (/api/v1/agent-access): external clients, their `run` keys, workflow exports, the enable card and the
// A2A endpoint.
//
// The one value Studio ever sees is the plaintext `key` of a new key; the page keeps it in the open sheet's state only
// (never storage, URL, toast or log) and no read can show it again.
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";

export type Protocol = "mcp" | "a2a";
export type ResultMode = "output" | "status_only";

export interface ExternalSettings {
  /** EAP_EXTERNAL_ACCESS_ENABLED on the API. */
  enabled: boolean;
  base_url: string | null;
  grants_enforced: boolean;
  can_manage: boolean;
  max_runtime_seconds: number;
}

export interface ExternalKey {
  id: string;
  name: string;
  prefix: string;
  status: "active" | "revoked" | "expired";
  scopes: string[];
  workflow_ids: string[];
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

export interface ExternalKeyCreated extends ExternalKey {
  /** Shown once. */
  key: string;
}

export interface ExternalClient {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  max_active_runs: number | null;
  max_runs_per_day: number | null;
  revision: number;
  created_at: string;
  keys: ExternalKey[];
}

export interface ExportKeyReach {
  id: string;
  prefix: string;
  name: string;
  status: string;
  client_id: string;
  client_name: string;
}

export interface WorkflowExport {
  id: string;
  name: string;
  title: string;
  description: string;
  tags: string[];
  examples: string[];
  workflow_id: string;
  workflow_name: string | null;
  version: number | "latest";
  resolved_version: number | null;
  protocols: Protocol[];
  result: ResultMode;
  client_inputs: boolean;
  max_runtime_seconds: number;
  require_idempotency_key: boolean;
  enabled: boolean;
  disabled_reason: string | null;
  enabled_by: string | null;
  enabled_at: string | null;
  /** What a person approves: the enable call echoes it. */
  approval_hash: string | null;
  input_schema: Record<string, unknown> | null;
  warnings: { code: string; message: string }[];
  keys: ExportKeyReach[];
  revision: number;
}

export interface ExportInput {
  name: string;
  title: string;
  description: string;
  workflow_id: string;
  version: number | "latest";
  protocols: Protocol[];
  result: ResultMode;
  max_runtime_seconds: number;
}

export const DISABLED_REASONS: Record<string, string> = {
  export_changed: "The export was changed. Review it and enable it again.",
  export_contract_changed: "A new workflow version changed the input or the side effects. Review it and enable it again.",
  disabled: "Disabled by a person.",
};

export interface A2AEndpoint {
  endpoint_id: string;
  /** What the public Agent Card says, to anyone with the URL. */
  card_name: string;
  card_description: string;
  enabled: boolean;
  /** null until EAP_EXTERNAL_BASE_URL is set. */
  url: string | null;
  card_url: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

type EndpointAnswer = { endpoint: A2AEndpoint };

async function request<T>(path: string, init: RequestInit = {}, ifMatch?: number): Promise<T> {
  const response = await fetch(`${BASE_URL}/api/v1/agent-access${path}`, {
    ...init,
    cache: "no-store", // a key answer holds the secret: never let a cache keep it
    headers: { "Content-Type": "application/json", ...apiHeaders(ifMatch === undefined ? {} : { "If-Match": `"${ifMatch}"` }) },
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.status === 204 ? (undefined as T) : response.json();
}

const post = (body?: unknown) => ({ method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }) satisfies RequestInit;
const patch = (body: unknown) => ({ method: "PATCH", body: JSON.stringify(body) }) satisfies RequestInit;
const id = encodeURIComponent;

export const externalAccess = {
  settings: () => request<ExternalSettings>(""),
  clients: async (): Promise<ExternalClient[]> => (await request<{ items: ExternalClient[] }>("/clients")).items,
  createClient: (body: { name: string; description: string }) => request<ExternalClient>("/clients", post(body)),
  updateClient: (client: string, body: { enabled: boolean }, revision: number) =>
    request<ExternalClient>(`/clients/${id(client)}`, patch(body), revision),
  createKey: (client: string, body: { name: string; workflow_ids: string[]; expires_at?: string }) =>
    request<ExternalKeyCreated>(`/clients/${id(client)}/keys`, post(body)),
  revokeKey: (client: string, key: string) => request<void>(`/clients/${id(client)}/keys/${id(key)}`, { method: "DELETE" }),
  cancelRuns: (client: string) => request<{ cancelled: string[]; pending: string[] }>(`/clients/${id(client)}/cancel-runs`, post()),
  exports: async (): Promise<WorkflowExport[]> => (await request<{ items: WorkflowExport[] }>("/exports")).items,
  createExport: (body: ExportInput) => request<WorkflowExport>("/exports", post(body)),
  updateExport: (exportId: string, body: Partial<Omit<ExportInput, "workflow_id">>, revision: number) =>
    request<WorkflowExport>(`/exports/${id(exportId)}`, patch(body), revision),
  /** A person enables it, echoing the hash the card showed. */
  enable: (exportId: string, approvedHash: string) =>
    request<WorkflowExport>(`/exports/${id(exportId)}/enable`, post({ approved_hash: approvedHash, confirm: true })),
  disable: (exportId: string) => request<WorkflowExport>(`/exports/${id(exportId)}/disable`, post()),
  a2aEndpoint: async (): Promise<A2AEndpoint | null> => (await request<{ endpoint: A2AEndpoint | null }>("/a2a-endpoint")).endpoint,
  saveA2aEndpoint: async (body: { card_name: string; card_description: string; enabled: boolean }): Promise<A2AEndpoint> =>
    (await request<EndpointAnswer>("/a2a-endpoint", { method: "PUT", body: JSON.stringify(body) })).endpoint,
  rotateA2aEndpoint: async (): Promise<A2AEndpoint> => (await request<EndpointAnswer>("/a2a-endpoint/rotate", post())).endpoint,
};

/** When a key made today with `days` to live expires (ISO 8601). */
export function expiryAfter(days: number, from: Date = new Date()): string {
  return new Date(from.getTime() + days * 86_400_000).toISOString();
}
