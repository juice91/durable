// Tenant secrets (/api/v1/secrets). Values are write-only: PUT sends one, nothing ever returns it,
// and Studio never stores, logs or echoes a typed value (errors here are built from the response's
// code, never from the request body). A secret is referenced elsewhere as `secret:<name>`.
import { refusedMessage } from "./access";
import { ApiError, apiHeaders, BASE_URL } from "./api";

/** A managed MCP server whose environment refers to a secret. */
export interface SecretUser {
  server_id: string;
  name: string;
}

export interface SecretInfo {
  name: string;
  created_at: string;
  updated_at: string;
  /** Managed servers referencing the secret (absent on older APIs: treat as none). */
  used_by?: SecretUser[];
}

const NAME_INVALID = "Secret names use lowercase letters, digits and dashes, starting with a letter.";
const NOT_FOUND = "That secret no longer exists.";

async function errorFor(response: Response): Promise<ApiError> {
  let code = "";
  let servers: SecretUser[] = [];
  try {
    const data = await response.json();
    const refused = await refusedMessage(response.status, data);
    if (refused) return new ApiError(response.status, refused);
    const detail = data?.detail ?? data;
    code = typeof detail === "string" ? detail : String(detail?.code ?? data?.code ?? "");
    const listed = detail?.servers ?? data?.servers;
    if (Array.isArray(listed)) servers = listed.filter((item: unknown): item is SecretUser => typeof (item as SecretUser)?.name === "string");
  } catch {
    // Not JSON: fall through to the status.
  }
  if (code === "secret_in_use" || response.status === 409) {
    const names = servers.map((server) => server.name).join(", ");
    return new ApiError(response.status, names ? `Still used by ${names}. Change those servers first.` : "Still used by an MCP server. Change those servers first.");
  }
  if (code === "secret_name_invalid" || (response.status === 422 && !code)) return new ApiError(response.status, NAME_INVALID);
  if (code === "secret_not_found" || response.status === 404) return new ApiError(response.status, NOT_FOUND);
  return new ApiError(response.status, `The secret could not be saved (HTTP ${response.status}); try again.`);
}

async function call(path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(`${BASE_URL}/api/v1/secrets${path}`, {
    ...init,
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...apiHeaders() },
  });
  if (!response.ok) throw await errorFor(response);
  return response;
}

export async function listSecrets(): Promise<SecretInfo[]> {
  return (await call("")).json();
}

/** Create or replace a secret's value. Returns the ids of managed servers restarted to pick it up. */
export async function putSecret(name: string, value: string): Promise<{ restarted: string[] }> {
  const response = await call(`/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify({ value }) });
  if (response.status === 204) return { restarted: [] };
  try {
    const data = await response.json();
    return { restarted: Array.isArray(data?.restarted) ? data.restarted.map(String) : [] };
  } catch {
    return { restarted: [] };
  }
}

/** 204; an unknown name is 404 and one still used by a server is 409 (thrown). */
export async function deleteSecret(name: string): Promise<void> {
  await call(`/${encodeURIComponent(name)}`, { method: "DELETE" });
}

/** The reference form agents and MCP headers use. */
export function secretRef(name: string): string {
  return `secret:${name}`;
}
