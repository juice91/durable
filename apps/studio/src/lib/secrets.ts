// Tenant secrets (/api/v1/secrets). Values are write-only: PUT sends one, nothing ever returns it,
// and Studio never stores, logs or echoes a typed value (errors here are built from the response's
// code, never from the request body). A secret is referenced elsewhere as `secret:<name>`.
import { ApiError, apiHeaders, BASE_URL } from "./api";

export interface SecretInfo {
  name: string;
  created_at: string;
  updated_at: string;
}

const NAME_INVALID = "Secret names use lowercase letters, digits and dashes, starting with a letter.";
const NOT_FOUND = "That secret no longer exists.";

async function errorFor(response: Response): Promise<ApiError> {
  let code = "";
  try {
    const data = await response.json();
    const detail = data?.detail ?? data;
    code = typeof detail === "string" ? detail : String(detail?.code ?? data?.code ?? "");
  } catch {
    // Not JSON: fall through to the status.
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

/** Create or replace a secret's value (204). */
export async function putSecret(name: string, value: string): Promise<void> {
  await call(`/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify({ value }) });
}

/** 204; an unknown name is 404 (thrown). */
export async function deleteSecret(name: string): Promise<void> {
  await call(`/${encodeURIComponent(name)}`, { method: "DELETE" });
}

/** The reference form agents and MCP headers use. */
export function secretRef(name: string): string {
  return `secret:${name}`;
}
