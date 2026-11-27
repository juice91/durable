// Inbound webhook triggers (/api/v1/webhook-triggers, /api/v1/webhook-deliveries).
//
// A trigger holds secret REFERENCES (`secret:<name>`), never a value. The one value Studio ever sees is
// `generated_secret` in the answer of a create or rotate with `generate: true`; the page keeps it in the open dialog's
// state only (never storage, URL, toast or log) and no read can show it again.
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";

export type WebhookStatus = "disabled" | "pending_enable" | "enabled" | "archived";
export type WebhookPreset = "github" | "standard_webhooks" | "custom";

export interface WebhookSecretRef {
  ref: string;
  added_at: string | null;
  last_matched_at: string | null;
}

export interface WebhookWarning {
  code: string;
  message: string;
}

export interface WebhookTrigger {
  id: string;
  name: string;
  description: string;
  workflow_id: string;
  workflow_version: number | null;
  /** The number, or "latest". */
  version: number | "latest";
  preset: WebhookPreset;
  preset_config: Record<string, unknown>;
  secrets: WebhookSecretRef[];
  tolerance_seconds: number;
  actions: { start: { payload_schema: Record<string, unknown>; input_map: Record<string, unknown>; run_name: string | null } };
  limits: { rate_per_minute: number; max_concurrent_runs: number; max_runs_per_day: number; max_body_bytes: number };
  status: WebhookStatus;
  /** What a person approves: the enable call echoes it. */
  config_hash: string;
  approved: boolean;
  enable_requested_by: string | null;
  enable_requested_at: string | null;
  enabled_by: string | null;
  enabled_at: string | null;
  last_delivery_at: string | null;
  last_outcome: string | null;
  revision: number;
  created_by: string;
  created_at: string;
  updated_at: string;
  url: string | null;
  warnings?: WebhookWarning[];
  /** Refusal counters by reason since the API process started (unauthenticated noise is counted, not stored). */
  refusals_since_start?: Record<string, number>;
}

export interface WebhookCreated extends WebhookTrigger {
  /** Shown once. */
  generated_secret?: string;
}

export interface WebhookEvent {
  id: string;
  at: string;
  actor: string;
  kind: string;
  detail: Record<string, unknown>;
}

export interface WebhookDelivery {
  id: string;
  trigger_id: string;
  received_at: string;
  outcome: string;
  reason: string | null;
  key_kind: string | null;
  provider_delivery_id: string | null;
  event_label: string | null;
  body_bytes: number | null;
  timestamp_skew_seconds: number | null;
  secret_slot: number | null;
  run_id: string | null;
}

export interface WebhookCreate {
  name: string;
  description?: string;
  workflow_id: string;
  version: number | "latest";
  preset: WebhookPreset;
  preset_config?: Record<string, unknown>;
  /** Existing secret references, or `generate: true` (Studio only). */
  secrets?: string[];
  generate?: boolean;
  tolerance_seconds?: number;
  actions: { start: { payload_schema: Record<string, unknown>; input_map: Record<string, unknown> } };
  limits?: Partial<WebhookTrigger["limits"]>;
}

export const STATUS_LABELS: Record<WebhookStatus, string> = {
  disabled: "Disabled",
  pending_enable: "Waiting for approval",
  enabled: "Enabled",
  archived: "Archived",
};

export const PRESET_LABELS: Record<WebhookPreset, string> = {
  github: "GitHub",
  standard_webhooks: "Standard Webhooks",
  custom: "Custom HMAC",
};

/** The headers a sender must set for a preset (shown beside the URL). */
export const PRESET_HEADERS: Record<WebhookPreset, string> = {
  github: "X-Hub-Signature-256: sha256=<hex>",
  standard_webhooks: "webhook-id, webhook-timestamp, webhook-signature: v1,<base64>",
  custom: "the header named in the preset config",
};

export const OUTCOME_LABELS: Record<string, string> = {
  accepted: "Run started",
  stale: "Stale timestamp",
  body_invalid: "Body invalid",
  rejected_schema: "Payload rejected",
  rejected_concurrency: "Too many runs",
  rejected_rate: "Daily limit",
  workflow_unavailable: "Workflow unavailable",
  unavailable: "Hand-off failed",
  processing: "Processing",
};

async function request<T>(path: string, init: RequestInit = {}, ifMatch?: number): Promise<T> {
  const response = await fetch(`${BASE_URL}/api/v1${path}`, {
    ...init,
    cache: "no-store", // a create answer may hold the secret: never let a cache keep it
    headers: { "Content-Type": "application/json", ...apiHeaders(ifMatch === undefined ? {} : { "If-Match": `"${ifMatch}"` }) },
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

const post = (body?: unknown) => ({ method: "POST", body: body === undefined ? undefined : JSON.stringify(body) }) satisfies RequestInit;

export const webhooks = {
  list: async (): Promise<WebhookTrigger[]> => (await request<{ items: WebhookTrigger[] }>("/webhook-triggers")).items,
  get: (id: string) => request<WebhookTrigger>(`/webhook-triggers/${encodeURIComponent(id)}`),
  create: (body: WebhookCreate) => request<WebhookCreated>("/webhook-triggers", post(body)),
  update: (id: string, body: Record<string, unknown>, revision: number) =>
    request<WebhookTrigger>(`/webhook-triggers/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) }, revision),
  /** A person enables it, echoing the config hash the card showed. */
  enable: (id: string, configHash: string) =>
    request<WebhookTrigger>(`/webhook-triggers/${encodeURIComponent(id)}/enable`, post({ config_hash: configHash, confirm: true })),
  decide: (id: string, decision: "approve" | "reject", configHash: string) =>
    request<WebhookTrigger>(`/webhook-triggers/${encodeURIComponent(id)}/decision`, post({ decision, config_hash: configHash })),
  disable: (id: string) => request<WebhookTrigger>(`/webhook-triggers/${encodeURIComponent(id)}/disable`, post()),
  archive: (id: string) => request<WebhookTrigger>(`/webhook-triggers/${encodeURIComponent(id)}/archive`, post()),
  rotate: (id: string, body: { add: string } | { retire: string } | { generate: true }) =>
    request<WebhookCreated>(`/webhook-triggers/${encodeURIComponent(id)}/rotate`, post(body)),
  events: async (id: string): Promise<WebhookEvent[]> => (await request<{ items: WebhookEvent[] }>(`/webhook-triggers/${encodeURIComponent(id)}/events`)).items,
  deliveries: async (id: string): Promise<WebhookDelivery[]> =>
    (await request<{ items: WebhookDelivery[] }>(`/webhook-deliveries?trigger_id=${encodeURIComponent(id)}&limit=50`)).items,
};

/** A JSON object from a textarea, or the reason it is not one. */
export function parseJsonObject(label: string, text: string): { ok: true; value: Record<string, unknown> } | { ok: false; message: string } {
  try {
    const value: unknown = JSON.parse(text);
    if (value === null || typeof value !== "object" || Array.isArray(value)) return { ok: false, message: `${label} must be a JSON object.` };
    return { ok: true, value: value as Record<string, unknown> };
  } catch {
    return { ok: false, message: `${label} is not valid JSON.` };
  }
}

export function webhookWhen(iso: string | null): string {
  if (!iso) return "Never";
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t).toLocaleString();
}
