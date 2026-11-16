// Running one tool of an MCP server: POST /api/v1/mcp-servers/{id}/tools/{tool}/runs records a run
// on the tool-run ledger (tool_id `mcp:<server id>/<tool>`), listed with the built-in tools' runs.
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import type { Run } from "./runs";
import { mapToolRunError, type ToolRunAccepted } from "./toolRuns";
import type { McpDiscoveredTool } from "./mcpServers";
import type { ToolDefinition } from "./tools";

export function mcpToolRunId(serverId: string, tool: string): string {
  return `mcp:${serverId}/${tool}`;
}

/** The tool as the shared tool runner takes it. */
export function mcpToolDefinition(serverId: string, tool: McpDiscoveredTool): ToolDefinition {
  return {
    id: mcpToolRunId(serverId, tool.name),
    title: tool.name,
    description: tool.description ?? "",
    side_effect: true, // an MCP server does not say; treat each Run as one real call
    external_idempotent: false,
    input_schema: tool.input_schema as ToolDefinition["input_schema"],
    allowed: true,
  };
}

const MCP_RUN_FALLBACKS: Record<number, string> = {
  404: "MCP server or tool not found",
  409: "This run id is already in use",
  503: "The run could not be started; try again",
};

export async function startMcpToolRun(
  serverId: string,
  tool: string,
  input: Record<string, unknown>,
  runId?: string,
): Promise<ToolRunAccepted> {
  const body: { input: Record<string, unknown>; run_id?: string } = { input };
  if (runId) body.run_id = runId;
  const response = await fetch(
    `${BASE_URL}/api/v1/mcp-servers/${encodeURIComponent(serverId)}/tools/${encodeURIComponent(tool)}/runs`,
    { method: "POST", headers: { "Content-Type": "application/json", ...apiHeaders() }, body: JSON.stringify(body) },
  );
  if (!response.ok) {
    let data: unknown = null;
    try {
      data = await response.json();
    } catch {
      // Non-JSON error body: fall back to the status message.
    }
    throw mapToolRunError(response.status, data, MCP_RUN_FALLBACKS);
  }
  return response.json();
}

/** The newest runs of one MCP tool (GET /runs?kind=tool&tool_id=...). */
export async function listMcpToolRuns(serverId: string, tool: string, limit = 10): Promise<Run[]> {
  const params = new URLSearchParams({ kind: "tool", tool_id: mcpToolRunId(serverId, tool), limit: String(limit) });
  const response = await fetch(`${BASE_URL}/api/v1/runs?${params.toString()}`, { headers: apiHeaders() });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()).items as Run[];
}
