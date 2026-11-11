export interface AgentSpec {
  spec_version: 1;
  name: string;
  instructions: string;
  model: {
    provider: string;
    name: string;
  };
  tools: string[];
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
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

const BASE_URL = process.env.NEXT_PUBLIC_EAP_API_URL || "http://localhost:8000";
const DEFAULT_TOKEN = "Bearer dev:acme:alice";
const TOKEN = process.env.NEXT_PUBLIC_EAP_DEV_TOKEN || DEFAULT_TOKEN;

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${BASE_URL}${path}`;
  const headers: HeadersInit = {
    "Content-Type": "application/json",
    Authorization: TOKEN,
    ...(options?.headers || {}),
  };

  const response = await fetch(url, { ...options, headers });

  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const data = await response.json();
      if (response.status === 422 && Array.isArray(data.detail)) {
        message = data.detail.map((e: { msg?: unknown }) => String(e.msg ?? "")).join(", ");
      } else if (response.status === 409 && typeof data.detail === "string") {
        message = data.detail;
      } else if (data.detail) {
        message = String(data.detail);
      }
    } catch {
      // ignore JSON parse errors
    }
    throw new ApiError(response.status, message);
  }

  return response.json();
}

export const api = {
  agents: {
    list: () => request<Agent[]>("/api/v1/agents"),
    create: (spec: AgentSpec) => request<Agent>("/api/v1/agents", { method: "POST", body: JSON.stringify({ spec }) }),
    get: (id: string) => request<Agent>(`/api/v1/agents/${id}`),
    update: (id: string, spec: AgentSpec) => request<Agent>(`/api/v1/agents/${id}`, { method: "PATCH", body: JSON.stringify({ spec }) }),
  },
  versions: {
    list: (agentId: string) => request<AgentVersion[]>(`/api/v1/agents/${agentId}/versions`),
    create: (agentId: string) => request<AgentVersion>(`/api/v1/agents/${agentId}/versions`, { method: "POST" }),
  },
};
