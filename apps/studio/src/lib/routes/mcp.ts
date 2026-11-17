import { seg } from "./query";

export const mcpRoutes = {
  /** `/mcp`: the MCP servers list. */
  mcp: (): string => "/mcp",
  /** `/mcp/browse`: the catalog of installable MCP servers. */
  mcpBrowse: (): string => "/mcp/browse",
  /** `/mcp/:id`: one MCP server's detail page (connection, tools, used by, delete). */
  mcpServer: (id: string): string => `/mcp/${seg(id)}`,
};
