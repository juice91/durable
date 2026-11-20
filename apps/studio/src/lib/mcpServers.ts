// MCP server registry client (/api/v1/mcp-servers) and the pure helpers its screens use.
//
// Header values are write-only: the API returns header NAMES only (`header_names`), and a PATCH
// with `headers` replaces the whole set. The Studio therefore never holds an existing value, never
// prefills one, and only sends `headers` when the user changed them.
import { apiErrorFromResponse, apiHeaders, BASE_URL, type AgentSpec, type McpToolRef } from "./api";
import { revisionKey, track, trackAll, writeResource } from "./revisions";

export interface McpServer {
  id: string;
  name: string;
  url: string;
  transport: "streamable_http";
  enabled: boolean;
  header_names: string[];
  created_at: string | null;
  updated_at: string | null;
  last_checked_at: string | null;
  last_status: "ok" | "error" | null;
  last_error_code: string | null;
  /** Local runtime fields (absent on servers from older APIs: treat as external). */
  runtime?: "external" | "managed";
  runtime_state?: McpRuntimeState | null;
  runtime_error?: string | null;
  catalog_name?: string | null;
  env_names?: string[];
  /** Managed servers: the idle sweep never stops the runtime. */
  keep_warm?: boolean;
  /** The optimistic-concurrency token (`ETag`); see revisions.ts. */
  revision?: number;
}

export type McpRuntimeState = "pending" | "starting" | "running" | "idle" | "stopping" | "stopped" | "failed";

export interface McpRuntime {
  state: McpRuntimeState;
  message: string | null;
  reachable: boolean;
  last_error: string | null;
  last_used_at: string | null;
  updated_at: string | null;
  image: string | null;
  catalog_name: string | null;
  package: { registry_type: string; identifier: string; version: string; transport: string } | null;
  /** Environment variable names; `ref` is `secret:<name>` when the value comes from a stored secret. Never a value. */
  env?: { name: string; ref: string | null }[];
}

export interface McpRuntimeLogs {
  lines: string[];
  tail: number;
}

export interface McpDiscoveredTool {
  name: string;
  description: string | null;
  input_schema: Record<string, unknown>;
}

/** What the Test button did to a managed server's runtime (absent for external servers). */
export interface McpTestRuntime {
  /** The runtime state before the test. */
  state: McpRuntimeState;
  /** The test had to bring the runtime up, and did. */
  started: boolean;
  waited_ms: number;
  /** Minutes since the runtime was last used, when it was stopped. */
  idle_minutes?: number | null;
  last_error?: string | null;
}

export type McpTestResult =
  | { status: "ok"; tools: McpDiscoveredTool[]; runtime?: McpTestRuntime }
  | { status: "error"; error_code: string; tools: []; runtime?: McpTestRuntime };

export interface McpServerCreate {
  name: string;
  url: string;
  enabled?: boolean;
  headers?: Record<string, string>;
}

export interface McpServerUpdate {
  name?: string;
  url?: string;
  enabled?: boolean;
  /** Managed servers only: the idle sweep never stops the runtime. */
  keep_warm?: boolean;
  headers?: Record<string, string>;
  /** Managed servers only: variable → `secret:<name>` (or a plain value). An env change restarts the server. */
  env?: Record<string, string>;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}/api/v1/mcp-servers${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...apiHeaders(), ...(init.headers as Record<string, string> | undefined) },
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  if (response.status === 204) return undefined as T;
  return response.json();
}

export const mcpServers = {
  list: () => request<McpServer[]>("").then((items) => trackAll(items, (s) => revisionKey.mcpServer(s.id))),
  get: (id: string) => request<McpServer>(`/${encodeURIComponent(id)}`).then((server) => track(revisionKey.mcpServer(id), server)),
  create: (body: McpServerCreate) => request<McpServer>("", { method: "POST", body: JSON.stringify(body) }),
  /** Sends the revision this tab last read as `If-Match`; a stale one throws ConflictError (412). */
  update: (id: string, body: McpServerUpdate) =>
    writeResource(revisionKey.mcpServer(id), (headers) =>
      request<McpServer>(`/${encodeURIComponent(id)}`, { method: "PATCH", headers, body: JSON.stringify(body) })),
  setEnabled: (id: string, enabled: boolean) =>
    writeResource(revisionKey.mcpServer(id), (headers) =>
      request<McpServer>(`/${encodeURIComponent(id)}`, { method: "PATCH", headers, body: JSON.stringify({ enabled }) })),
  remove: (id: string) => request<void>(`/${encodeURIComponent(id)}`, { method: "DELETE" }),
  test: (id: string) => request<McpTestResult>(`/${encodeURIComponent(id)}/test`, { method: "POST" }),
  runtime: (id: string) => request<McpRuntime>(`/${encodeURIComponent(id)}/runtime`),
  runtimeAction: (id: string, action: McpRuntimeAction) =>
    request<McpRuntime>(`/${encodeURIComponent(id)}/runtime/${action}`, { method: "POST" }),
  runtimeLogs: (id: string, tail = 200) => request<McpRuntimeLogs>(`/${encodeURIComponent(id)}/runtime/logs?tail=${tail}`),
};

export type McpRuntimeAction = "start" | "stop" | "restart";

export const RUNTIME_STATES: readonly McpRuntimeState[] = ["pending", "starting", "running", "idle", "stopping", "stopped", "failed"];

/** Which controls make sense in a state (the API still decides; a disabled server also refuses start and restart). */
export function runtimeActionsFor(state: McpRuntimeState): Record<McpRuntimeAction, boolean> {
  switch (state) {
    case "running":
    case "idle":
      return { start: false, stop: true, restart: true };
    case "stopped":
      return { start: true, stop: false, restart: false };
    case "failed":
      return { start: true, stop: true, restart: true };
    case "pending":
    case "starting":
      return { start: false, stop: true, restart: false };
    case "stopping":
      return { start: false, stop: false, restart: false };
  }
}

// ---------------------------------------------------------------------------------------------
// Error codes -> readable messages

const ERROR_MESSAGES: Record<string, string> = {
  address_blocked: "Refused by the outbound policy: the URL resolves to a private, loopback or link-local address.",
  url_scheme_not_allowed: "Refused by the outbound policy: only https URLs are allowed.",
  url_credentials: "Refused by the outbound policy: URLs may not contain a user name or password; use a header instead.",
  url_host_missing: "Refused by the outbound policy: the URL has no host.",
  url_invalid: "Refused by the outbound policy: the URL is not valid.",
  url_too_long: "Refused by the outbound policy: the URL is too long.",
  host_unresolvable: "The host name could not be resolved.",
  unauthorized: "The server refused the credentials (401/403). Check the header secrets.",
  redirect_refused: "The server answered with a redirect, which is not followed.",
  http_status: "The server answered with an HTTP error.",
  timeout: "The connection timed out.",
  tls_failed: "The TLS handshake failed.",
  connect_failed: "Could not connect to the server.",
  transport_error: "The connection failed while talking to the server.",
  protocol_error: "The server did not speak the MCP protocol as expected.",
  secret_unreadable: "A stored header secret could not be decrypted; replace the headers.",
  connection_failed: "The connection test failed.",
  runtime_starting: "The server is starting. Test again in a moment.",
  runtime_not_running: "The server's runtime is not running. Open the Runtime tab to see why.",
};

/** The success line of a test that had to start the server first (an idle sweep had stopped it). */
export function describeStartedTest(runtime: McpTestRuntime | undefined): string | null {
  if (!runtime?.started) return null;
  const idle = runtime.state === "stopped" && typeof runtime.idle_minutes === "number" ? ` (idle for ${runtime.idle_minutes} min)` : "";
  return `Started the server${idle} and tested it: OK`;
}

/** A readable message for a connection-test `error_code` or an outbound-policy code. */
export function describeMcpError(code: string | null | undefined): string {
  if (!code) return "Unknown error";
  return ERROR_MESSAGES[code] ?? `The connection test failed (${code}).`;
}

/** Turn API error details such as "url refused by the outbound policy: address_blocked" into text. */
export function describeMcpApiMessage(message: string): string {
  const match = /^url refused by the outbound policy: ([a-z_]+)$/.exec(message.trim());
  return match ? describeMcpError(match[1]) : message;
}

export function isOutboundPolicyCode(code: string | null | undefined): boolean {
  return Boolean(code && (code === "address_blocked" || code.startsWith("url_")));
}

// ---------------------------------------------------------------------------------------------
// Write-only header rows

/**
 * One header row in the form. `existing` rows came from the API (`header_names`) and have no value;
 * the user can Replace one (it becomes `replace` with a fresh value) or Remove it.
 */
export type HeaderRow =
  | { key: string; origin: "existing"; name: string; state: "kept" | "replace" | "removed"; value: string }
  | { key: string; origin: "new"; name: string; value: string };

let rowCounter = 0;
export function newRowKey(): string {
  rowCounter += 1;
  return `header-${rowCounter}`;
}

export function rowsFromServer(headerNames: string[]): HeaderRow[] {
  return headerNames.map((name) => ({ key: newRowKey(), origin: "existing", name, state: "kept", value: "" }));
}

export type HeadersPayload =
  | { kind: "unchanged" }
  | { kind: "set"; headers: Record<string, string> }
  | { kind: "invalid"; message: string };

/**
 * What to send for the headers. On create (`isCreate`), only new rows exist and `headers` is left out
 * when there are none. On edit, `headers` is left out unless something changed; because a PATCH
 * replaces the whole set and existing values are never readable, any change requires a fresh value
 * for every header that is kept.
 */
export function buildHeadersPayload(rows: HeaderRow[], isCreate = false): HeadersPayload {
  const active = rows.filter((row) => !(row.origin === "new" && row.name.trim() === "" && row.value === ""));
  const changed = active.some((row) => row.origin === "new" || row.state !== "kept");
  if (!changed) return { kind: "unchanged" };

  const headers: Record<string, string> = {};
  const seen = new Set<string>();
  const missing: string[] = [];
  for (const row of active) {
    if (row.origin === "existing" && row.state === "removed") continue;
    const name = row.name.trim();
    if (!name) return { kind: "invalid", message: "Every header needs a name" };
    const lowered = name.toLowerCase();
    if (seen.has(lowered)) return { kind: "invalid", message: `Header ${name} appears twice (names are case-insensitive)` };
    seen.add(lowered);
    if (!row.value) {
      if (row.origin === "existing") missing.push(name);
      else return { kind: "invalid", message: `Enter a value for header ${name}` };
      continue;
    }
    headers[name] = row.value;
  }
  if (missing.length > 0) {
    return {
      kind: "invalid",
      message: `Saving header changes replaces all headers, and stored values cannot be read back. Replace ${missing.join(", ")} with its value too, or remove it.`,
    };
  }
  if (isCreate && Object.keys(headers).length === 0) return { kind: "unchanged" };
  return { kind: "set", headers };
}

// ---------------------------------------------------------------------------------------------
// Agent spec MCP tools

export function mcpToolKey(ref: McpToolRef): string {
  return `${ref.server}/${ref.tool}`;
}

/** The spec with `mcp_tools` set, or with the key left out entirely when there are none (spec hash stability). */
export function withMcpTools<T extends Omit<AgentSpec, "mcp_tools">>(spec: T, refs: McpToolRef[]): T & { mcp_tools?: McpToolRef[] } {
  const { mcp_tools: _ignored, ...rest } = spec as T & { mcp_tools?: McpToolRef[] };
  void _ignored;
  if (refs.length === 0) return rest as T;
  return { ...(rest as T), mcp_tools: refs.map((ref) => ({ server: ref.server, tool: ref.tool })) };
}
