import { seg } from "./query";

export const mcpRoutes = {
  /** `/mcp`: the MCP servers list. */
  mcp: (): string => "/mcp",
  /** `/mcp/:id`: one MCP server's detail page (connection, tools, used by, delete). */
  mcpServer: (id: string): string => `/mcp/${seg(id)}`,
};
