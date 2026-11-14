import { withQuery } from "./query";

/** The tool Sheet's tabs; "overview" is the default and stays out of the URL. */
export type ToolTab = "overview" | "run";

export interface ToolRouteOptions {
  tab?: ToolTab;
  /** The MCP server for an MCP tool; absent for a built-in tool. */
  server?: string | null;
}

export const toolRoutes = {
  /** `/tools`: the tools page. */
  tools: (): string => "/tools",
  /** `/tools?tool=<name>[&server=<mcp server>][&tab=run]`: the tools page with one tool's Sheet open. */
  tool: (name: string, { tab, server }: ToolRouteOptions = {}): string =>
    withQuery("/tools", { tool: name, server: server ?? undefined, tab: tab === "run" ? "run" : undefined }),
};
