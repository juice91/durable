/**
 * The native REST calls the smoke test needs for setup and cross-checks (AGUI.14). Setup uses the
 * dev bearer (`dev:<tenant>:<user>`); every AG-UI call uses a tenant API key created here.
 */

export class ApiError extends Error {
  constructor(
    readonly method: string,
    readonly path: string,
    readonly status: number,
    readonly payload: string,
  ) {
    super(`${method} ${path} -> ${status} ${payload.slice(0, 500)}`);
  }
}

export class Api {
  readonly devHeaders: Record<string, string>;

  constructor(
    readonly base: string,
    readonly tenant: string,
  ) {
    this.devHeaders = { Authorization: `Bearer dev:${tenant}:smoke-user`, "Content-Type": "application/json" };
  }

  async request<T = unknown>(method: string, path: string, body?: unknown, headers?: Record<string, string>): Promise<T> {
    const response = await fetch(this.base + path, {
      method,
      headers: headers ?? this.devHeaders,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await response.text();
    if (!response.ok) throw new ApiError(method, path, response.status, text);
    return (text ? JSON.parse(text) : undefined) as T;
  }

  /** Creates and publishes an agent; returns its id. */
  async publishAgent(spec: Record<string, unknown>): Promise<string> {
    const agent = await this.request<{ id: string }>("POST", "/agents", { spec: { spec_version: 1, ...spec } });
    await this.request("POST", `/agents/${agent.id}/versions`);
    return agent.id;
  }

  /** Creates and publishes a workflow; returns its id. */
  async publishWorkflow(spec: Record<string, unknown>): Promise<string> {
    const workflow = await this.request<{ id: string }>("POST", "/workflows", { spec });
    await this.request("POST", `/workflows/${workflow.id}/versions`);
    return workflow.id;
  }

  async registerMcpServer(name: string, url: string): Promise<string> {
    const server = await this.request<{ id: string }>("POST", "/mcp-servers", { name, url });
    return server.id;
  }

  /** A tenant-wide `agui:run` key; returns the plaintext `pos_...`. */
  async createApiKey(name: string): Promise<string> {
    const created = await this.request<{ key: string }>("POST", "/api-keys", { name });
    return created.key;
  }

  agentUrl(agentId: string): string {
    return `${this.base}/ag-ui/agents/${agentId}`;
  }

  workflowUrl(workflowId: string): string {
    return `${this.base}/ag-ui/workflows/${workflowId}`;
  }

  /** `GET /ag-ui/agents/{id}/threads/{threadId}` with the API key. */
  async thread(agentId: string, threadId: string, key: string): Promise<ThreadView> {
    return this.request<ThreadView>("GET", `/ag-ui/agents/${agentId}/threads/${encodeURIComponent(threadId)}`, undefined, {
      Authorization: `Bearer ${key}`,
    });
  }

  /** The native durable events of a run (dev bearer). */
  async runEvents(runId: string): Promise<NativeEvent[]> {
    return this.request<NativeEvent[]>("GET", `/runs/${runId}/events`);
  }

  async run(runId: string): Promise<Record<string, unknown>> {
    return this.request<Record<string, unknown>>("GET", `/runs/${runId}`);
  }
}

export interface ThreadView {
  threadId: string;
  conversationId: string | null;
  messages: Array<Record<string, unknown> & { id: string; role: string }>;
  runs: Array<Record<string, unknown>>;
  activeRunId: string | null;
}

export interface NativeEvent {
  seq: number;
  type: string;
  data?: Record<string, unknown>;
  ts: string;
}

/** Fetches MCP server's executed calls (`GET /calls` of mcp_server.py). */
export async function mcpCalls(mcpUrl: string): Promise<Array<{ tool: string; args: Record<string, unknown> }>> {
  const response = await fetch(mcpUrl.replace(/\/mcp\/?$/, "/calls"));
  if (!response.ok) throw new Error(`MCP /calls -> ${response.status}`);
  return (await response.json()) as Array<{ tool: string; args: Record<string, unknown> }>;
}
