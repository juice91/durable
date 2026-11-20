import { rememberRevision, revisionFromEtag, revisionKey, track, trackAll, writeResource } from "./revisions";

/** An MCP tool an agent may call: `server` is the tenant MCP server's NAME, `tool` the tool's name on it. */
export interface McpToolRef {
  server: string;
  tool: string;
}

export interface AgentSpec {
  spec_version: 1;
  name: string;
  instructions: string;
  model: {
    provider: string;
    name: string;
  };
  tools: string[];
  /** Left out entirely when empty, so the spec hash does not change for agents without MCP tools. */
  mcp_tools?: McpToolRef[];
  model_settings?: {
    temperature?: number | null;
    max_tokens?: number | null;
    /** Left out of the stored spec while unset (the hash of older specs stays the same). */
    top_p?: number | null;
    top_k?: number | null;
    frequency_penalty?: number | null;
    presence_penalty?: number | null;
    seed?: number | null;
    max_steps?: number | null;
    max_retries?: number | null;
  };
}

export interface AgentSpecSummary {
  version: number;
  model: { provider: string | null; name: string | null };
  tool_count: number;
  spec_hash: string;
}

export interface Agent {
  id: string;
  name: string;
  draft_spec: AgentSpec;
  latest_version: number | null;
  /** What the latest published version runs; null for a draft-only agent. */
  latest_spec_summary?: AgentSpecSummary | null;
  /** True when a published version exists and the draft differs from it. */
  draft_dirty?: boolean;
  /** Archived agents are hidden from `list`, and refuse new runs, conversations and publishes (409). */
  archived?: boolean;
  /** A built-in agent (the Architect and its helpers). */
  builtin?: boolean;
  /** "architect" for the Architect, "drafter" for its system helpers, null for every other agent. */
  builtin_role?: "architect" | "drafter" | null;
  /** The optimistic-concurrency token (`ETag`): a save sends it as `If-Match` (see revisions.ts). */
  revision?: number;
}

export interface AgentVersion {
  agent_id: string;
  version: number;
  spec: AgentSpec;
  spec_hash: string;
  created_at: string;
  created_by: string;
  /** Set when this version was published by restoring an older one. */
  restored_from?: number | null;
}

/** One item of a 422 `detail` list (pydantic): where (`loc`) and what (`msg`). */
export interface ApiErrorDetail {
  loc: (string | number)[];
  msg: string;
}

export class ApiError extends Error {
  /** A 422's `detail` items, in order (empty for other errors). `message` joins their `msg`s. */
  public details: ApiErrorDetail[];

  constructor(public status: number, message: string, details: ApiErrorDetail[] = []) {
    super(message);
    this.name = "ApiError";
    this.details = details;
  }
}

export const BASE_URL = process.env.NEXT_PUBLIC_EAP_API_URL || "http://localhost:8000";

export function apiHeaders(extra: HeadersInit = {}): HeadersInit {
  const token = process.env.NEXT_PUBLIC_EAP_DEV_TOKEN;
  if (!token) {
    throw new ApiError(0, "No API credentials configured");
  }
  return { Authorization: token, ...extra };
}

export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  let message = `HTTP ${response.status}`;
  let details: ApiErrorDetail[] = [];
  try {
    const data = await response.json();
    if (response.status === 412 && data?.code === "revision_mismatch" && typeof data.current_revision === "number") {
      return new ConflictError(typeof data.detail === "string" ? data.detail : "This content was changed by someone else", data.current_revision, data.current);
    }
    if (response.status === 422 && Array.isArray(data.detail)) {
      message = data.detail.map((e: { msg?: unknown }) => String(e.msg ?? "")).join(", ");
      details = data.detail.map((e: { loc?: unknown; msg?: unknown }) => ({
        loc: Array.isArray(e.loc) ? e.loc.filter((part): part is string | number => typeof part === "string" || typeof part === "number") : [],
        msg: String(e.msg ?? ""),
      }));
    } else if (typeof data.detail === "string") {
      message = data.detail;
    } else if (data.detail && typeof data.detail === "object" && typeof data.detail.message === "string") {
      message = data.detail.message;
    }
  } catch {
    // Keep the HTTP status fallback when the response is not JSON.
  }
  return new ApiError(response.status, message, details);
}

/**
 * 412 `revision_mismatch`: the resource changed since this client read it. Carries the current resource
 * (as GET returns it) and revision, so the page can offer to reload it or overwrite it. `key` names the
 * remembered revision (revisions.ts) the write used; the conflict hook needs it to overwrite.
 */
export class ConflictError extends ApiError {
  public key = "";

  constructor(message: string, public currentRevision: number, public current: unknown) {
    super(412, message);
    this.name = "ConflictError";
  }
}

/** A published workflow that runs an agent as a step (POST /agents/{id}/archive's 409). */
export interface ReferencingWorkflow {
  workflow_id: string;
  workflow_name: string;
  versions: number[];
  latest_version: number;
}

/** Archive refused (409): published workflows run the agent; `force` archives anyway. */
export class AgentArchiveBlocked extends ApiError {
  constructor(message: string, public workflows: ReferencingWorkflow[]) {
    super(409, message);
    this.name = "AgentArchiveBlocked";
  }
}

async function archiveAgent(id: string, force: boolean): Promise<Agent> {
  const response = await fetch(`${BASE_URL}/api/v1/agents/${encodeURIComponent(id)}/archive${force ? "?force=true" : ""}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...apiHeaders() },
  });
  if (response.status === 409) {
    const data = await response.clone().json().catch(() => null);
    if (data && Array.isArray(data.workflows)) {
      throw new AgentArchiveBlocked(typeof data.detail === "string" ? data.detail : "Published workflows run this agent", data.workflows);
    }
  }
  if (!response.ok) throw await apiErrorFromResponse(response);
  return track(revisionKey.agent(id), (await response.json()) as Agent);
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${BASE_URL}${path}`;
  const headers: HeadersInit = {
    "Content-Type": "application/json",
    ...apiHeaders(options?.headers || {}),
  };

  const response = await fetch(url, { ...options, headers });

  if (!response.ok) {
    throw await apiErrorFromResponse(response);
  }
  // Publish, restore and archive answer the agent's new ETag: this tab's next save must send it.
  const agentPath = /^\/api\/v1\/agents\/([^/?]+)\//.exec(path);
  if (agentPath && options?.method && options.method !== "GET") {
    rememberRevision(revisionKey.agent(decodeURIComponent(agentPath[1])), revisionFromEtag(response.headers?.get?.("ETag")));
  }

  return response.json();
}

export const api = {
  agents: {
    /** `init` passes a `signal` (say, a timeout) through to fetch; `includeSystem` also lists the
     * Architect's drafter helpers (`?include_system=true`). */
    list: (init?: RequestInit, options: { includeSystem?: boolean } = {}) =>
      request<Agent[]>(options.includeSystem ? "/api/v1/agents?include_system=true" : "/api/v1/agents", init).then((items) =>
        trackAll(items, (agent) => revisionKey.agent(agent.id))),
    create: (spec: AgentSpec) =>
      request<Agent>("/api/v1/agents", { method: "POST", body: JSON.stringify({ spec }) }).then((agent) => track(revisionKey.agent(agent.id), agent)),
    get: (id: string) => request<Agent>(`/api/v1/agents/${id}`).then((agent) => track(revisionKey.agent(id), agent)),
    /** Saves the draft with the revision this tab last read (`If-Match`); a stale one throws ConflictError (412). */
    update: (id: string, spec: AgentSpec) =>
      writeResource(revisionKey.agent(id), (headers) =>
        request<Agent>(`/api/v1/agents/${id}`, { method: "PATCH", headers, body: JSON.stringify({ spec }) })),
    /** The archived agents only (`GET /agents?archived=true`). */
    listArchived: (init?: RequestInit) =>
      request<Agent[]>("/api/v1/agents?archived=true", init).then((items) => trackAll(items, (agent) => revisionKey.agent(agent.id))),
    /** Idempotent; answers the agent with its new `archived` flag. Throws AgentArchiveBlocked
     * (409, with the workflows) when published workflows run it, unless `force`. */
    archive: (id: string, options: { force?: boolean } = {}) => archiveAgent(id, options.force === true),
    unarchive: (id: string) => request<Agent>(`/api/v1/agents/${encodeURIComponent(id)}/unarchive`, { method: "POST" }),
  },
  versions: {
    list: (agentId: string) => request<AgentVersion[]>(`/api/v1/agents/${agentId}/versions`),
    /** One published version (`GET /agents/{id}/versions/{n}`). */
    get: (agentId: string, version: number, init?: RequestInit) =>
      request<AgentVersion>(`/api/v1/agents/${encodeURIComponent(agentId)}/versions/${encodeURIComponent(String(version))}`, init),
    create: (agentId: string) => request<AgentVersion>(`/api/v1/agents/${agentId}/versions`, { method: "POST" }),
  },
};
