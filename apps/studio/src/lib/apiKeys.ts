// Tenant API keys (/api/v1/api-keys, AGUI.3; plan D16) and the "Connect (AG-UI)" snippets.
//
// A key's plaintext (`pos_<id>_<secret>`) is in the POST answer only, once. Studio keeps it in the
// create dialog's state while that dialog is open and nowhere else: never in storage, the URL, a
// toast or a log, and the list answer has no secret to show. The Connect snippets are built from
// the endpoint URL and ids only and name the key by an environment variable, never by value.
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";

export type ApiKeyStatus = "active" | "revoked" | "expired";

/** A key as listed: `prefix` (`pos_<id>_`) identifies it; there is no secret and no hash. */
export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  status: ApiKeyStatus;
  scopes: string[];
  /** null on both: tenant-wide. Otherwise each is an allowlist ([] means none). */
  agent_ids: string[] | null;
  workflow_ids: string[] | null;
  /** null: any origin. */
  allowed_origins: string[] | null;
  created_by: string;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

/** POST /api-keys: the key plus `key`, its plaintext, returned this once. */
export interface ApiKeyCreated extends ApiKey {
  key: string;
}

export interface ApiKeyCreate {
  name: string;
  /** Leave both out for a tenant-wide key; giving one restricts the key on both. */
  agent_ids?: string[];
  workflow_ids?: string[];
  allowed_origins?: string[];
  expires_at?: string;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}/api/v1/api-keys${path}`, {
    ...init,
    // The create answer holds the secret: never let a cache keep it.
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...apiHeaders() },
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  if (response.status === 204) return undefined as T;
  return response.json();
}

export const apiKeys = {
  list: async (): Promise<ApiKey[]> => (await request<{ items: ApiKey[] }>("")).items,
  create: (body: ApiKeyCreate) => request<ApiKeyCreated>("", { method: "POST", body: JSON.stringify(body) }),
  /** Idempotent (204); an unknown or another tenant's key is 404. */
  revoke: (id: string) => request<void>(`/${encodeURIComponent(id)}`, { method: "DELETE" }),
};

/** The listed fields of a created key, without its plaintext (what the list keeps). */
export function withoutSecret(created: ApiKeyCreated): ApiKey {
  const rest: ApiKey & { key?: string } = { ...created };
  delete rest.key;
  return rest;
}

// ---------------------------------------------------------------------------------------------
// The create form

export interface ApiKeyFormInput {
  name: string;
  /** "all": tenant-wide; "some": only the picked agents and workflows. */
  access: "all" | "some";
  agentIds: string[];
  workflowIds: string[];
  /** One origin per line (or comma-separated); empty: any origin. */
  origins: string;
  /** UTC ISO, or null for no expiry. */
  expiresAt: string | null;
}

export type ApiKeyFormResult = { ok: true; body: ApiKeyCreate } | { ok: false; message: string };

/** Splits a textarea of origins on newlines and commas, trimmed, without blanks or repeats. */
export function parseOrigins(text: string): string[] {
  return Array.from(new Set(text.split(/[\n,]/).map((item) => item.trim()).filter(Boolean)));
}

/** The POST body for the form, or why it cannot be sent. */
export function buildApiKeyCreate(input: ApiKeyFormInput, now: Date = new Date()): ApiKeyFormResult {
  const name = input.name.trim();
  if (!name) return { ok: false, message: "Give the key a name." };
  const body: ApiKeyCreate = { name };
  if (input.access === "some") {
    if (input.agentIds.length === 0 && input.workflowIds.length === 0) {
      return { ok: false, message: "Pick at least one agent or workflow, or allow all of them." };
    }
    body.agent_ids = [...input.agentIds];
    body.workflow_ids = [...input.workflowIds];
  }
  // Origins are checked by the server only (eap_run_store.api_keys.normalize_origin: http(s), no
  // path but "/", no query, fragment, credentials or wildcard); its 422 message is shown as is.
  const origins = parseOrigins(input.origins);
  if (origins.length > 0) body.allowed_origins = origins;
  if (input.expiresAt) {
    if (Date.parse(input.expiresAt) <= now.getTime()) return { ok: false, message: "The expiry must be in the future." };
    body.expires_at = input.expiresAt;
  }
  return { ok: true, body };
}

/** What the key may run, for the list: "All agents and workflows", or the allowlist counts. */
export function describeAccess(key: Pick<ApiKey, "agent_ids" | "workflow_ids">): string {
  if (key.agent_ids === null && key.workflow_ids === null) return "All agents and workflows";
  const count = (ids: string[] | null, one: string, many: string) => {
    const n = ids?.length ?? 0;
    return `${n} ${n === 1 ? one : many}`;
  };
  return `${count(key.agent_ids, "agent", "agents")}, ${count(key.workflow_ids, "workflow", "workflows")}`;
}

export const API_KEY_STATUS_LABELS: Record<ApiKeyStatus, string> = { active: "Active", revoked: "Revoked", expired: "Expired" };

// ---------------------------------------------------------------------------------------------
// Connect (AG-UI)

export type ConnectTarget = { kind: "agent" | "workflow"; id: string };

/** The environment variable the snippets read the key from. */
export const API_KEY_ENV_VAR = "PYDANTICOS_API_KEY";

function trimBase(baseUrl: string): string {
  return baseUrl.replace(/\/+$/, "");
}

/** `POST` endpoint of the target: `/api/v1/ag-ui/agents/{id}` or `/api/v1/ag-ui/workflows/{id}`. */
export function aguiEndpoint(target: ConnectTarget, baseUrl: string = BASE_URL): string {
  return `${trimBase(baseUrl)}/api/v1/ag-ui/${target.kind === "agent" ? "agents" : "workflows"}/${encodeURIComponent(target.id)}`;
}

/** The thread snapshot URL, with `{threadId}` left for the client to fill. */
export function aguiThreadUrl(target: ConnectTarget, baseUrl: string = BASE_URL): string {
  return `${aguiEndpoint(target, baseUrl)}/threads/{threadId}`;
}

/** The attach / resume URL, with `{threadId}` and `{runId}` left for the client to fill. */
export function aguiAttachUrl(target: ConnectTarget, baseUrl: string = BASE_URL): string {
  return `${aguiThreadUrl(target, baseUrl)}/runs/{runId}/stream`;
}

function jsString(value: string): string {
  return JSON.stringify(value);
}

/** A camelCase-safe name for the agent in CopilotKit's `agents` map. */
function agentKey(target: ConnectTarget): string {
  return target.kind === "agent" ? "pydanticosAgent" : "pydanticosWorkflow";
}

/**
 * The CopilotKit runtime route (Next.js app router) that proxies to our endpoint with the key from
 * the server's environment, so the key never reaches the browser.
 */
export function copilotKitSnippet(target: ConnectTarget, baseUrl: string = BASE_URL): string {
  const name = agentKey(target);
  return [
    "// app/api/copilotkit/route.ts: the key stays on the server.",
    'import { HttpAgent } from "@ag-ui/client";',
    "import {",
    "  CopilotRuntime,",
    "  ExperimentalEmptyAdapter,",
    "  copilotRuntimeNextJSAppRouterEndpoint,",
    '} from "@copilotkit/runtime";',
    'import type { NextRequest } from "next/server";',
    "",
    "const runtime = new CopilotRuntime({",
    "  agents: {",
    `    ${name}: new HttpAgent({`,
    `      url: ${jsString(aguiEndpoint(target, baseUrl))},`,
    `      headers: { Authorization: \`Bearer \${process.env.${API_KEY_ENV_VAR}}\` },`,
    "    }),",
    "  },",
    "});",
    "",
    "export const POST = async (req: NextRequest) => {",
    "  const { handleRequest } = copilotRuntimeNextJSAppRouterEndpoint({",
    "    runtime,",
    "    serviceAdapter: new ExperimentalEmptyAdapter(),",
    '    endpoint: "/api/copilotkit",',
    "  });",
    "  return handleRequest(req);",
    "};",
    "",
    "// In the page:",
    `// <CopilotKit runtimeUrl="/api/copilotkit" agent=${jsString(name)}>…</CopilotKit>`,
  ].join("\n");
}

/** A curl call of the endpoint: one user message on a new thread. */
export function curlSnippet(target: ConnectTarget, baseUrl: string = BASE_URL): string {
  const body =
    target.kind === "agent"
      ? {
          threadId: "my-thread",
          runId: "run-1",
          messages: [{ id: "u1", role: "user", content: "Hello" }],
          tools: [],
          context: [],
          state: {},
          forwardedProps: {},
        }
      : {
          threadId: "my-thread",
          runId: "run-1",
          messages: [],
          tools: [],
          context: [],
          state: {},
          forwardedProps: { pydanticos: { input: {} } },
        };
  return [
    `curl -N ${jsString(aguiEndpoint(target, baseUrl))} \\`,
    `  -H "Authorization: Bearer $${API_KEY_ENV_VAR}" \\`,
    '  -H "Content-Type: application/json" \\',
    '  -H "Accept: text/event-stream" \\',
    `  -d '${JSON.stringify(body)}'`,
  ].join("\n");
}
