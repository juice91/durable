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
  };
}

export interface Agent {
  id: string;
  name: string;
  draft_spec: AgentSpec;
  latest_version: number | null;
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
    if (response.status === 422 && Array.isArray(data.detail)) {
      message = data.detail.map((e: { msg?: unknown }) => String(e.msg ?? "")).join(", ");
      details = data.detail.map((e: { loc?: unknown; msg?: unknown }) => ({
        loc: Array.isArray(e.loc) ? e.loc.filter((part): part is string | number => typeof part === "string" || typeof part === "number") : [],
        msg: String(e.msg ?? ""),
      }));
    } else if (typeof data.detail === "string") {
      message = data.detail;
    }
  } catch {
    // Keep the HTTP status fallback when the response is not JSON.
  }
  return new ApiError(response.status, message, details);
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

  return response.json();
}

export const api = {
  agents: {
    /** `init` passes a `signal` (say, a timeout) through to fetch. */
    list: (init?: RequestInit) => request<Agent[]>("/api/v1/agents", init),
    create: (spec: AgentSpec) => request<Agent>("/api/v1/agents", { method: "POST", body: JSON.stringify({ spec }) }),
    get: (id: string) => request<Agent>(`/api/v1/agents/${id}`),
    update: (id: string, spec: AgentSpec) => request<Agent>(`/api/v1/agents/${id}`, { method: "PATCH", body: JSON.stringify({ spec }) }),
  },
  versions: {
    list: (agentId: string) => request<AgentVersion[]>(`/api/v1/agents/${agentId}/versions`),
    /** One published version (`GET /agents/{id}/versions/{n}`). */
    get: (agentId: string, version: number, init?: RequestInit) =>
      request<AgentVersion>(`/api/v1/agents/${encodeURIComponent(agentId)}/versions/${encodeURIComponent(String(version))}`, init),
    create: (agentId: string) => request<AgentVersion>(`/api/v1/agents/${agentId}/versions`, { method: "POST" }),
  },
};
