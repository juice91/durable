// MCP catalog client (/api/v1/mcp-catalog) and the install call (/api/v1/mcp-servers/from-catalog).
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import type { McpServer } from "./mcpServers";

export interface CatalogEnvVar {
  name: string;
  description: string | null;
  required: boolean;
  secret: boolean;
}

export interface CatalogPackage {
  registry_type: "npm" | "pypi" | "oci" | string;
  identifier: string;
  version: string | null;
  transport: string | null;
  environment_variables: CatalogEnvVar[];
}

export interface CatalogRemote {
  type: string;
  url: string;
}

export interface CatalogEntry {
  name: string;
  title: string | null;
  description: string | null;
  version: string | null;
  status: string | null;
  licence: string | null;
  packages: CatalogPackage[];
  remotes: CatalogRemote[];
  local: boolean;
  remote: boolean;
  fetched_at: string | null;
}

export interface CatalogPage {
  items: CatalogEntry[];
  next_after: string | null;
}

export interface CatalogInstall {
  catalog_name: string;
  package_index?: number;
  remote_index?: number;
  name?: string;
  env?: Record<string, string>;
  headers?: Record<string, string>;
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}/api/v1${path}`, { ...init, headers: { "Content-Type": "application/json", ...apiHeaders() } });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export const mcpCatalog = {
  list: (params: { q?: string; limit?: number; after?: string | null } = {}) => {
    const query = new URLSearchParams();
    if (params.q?.trim()) query.set("q", params.q.trim());
    if (params.limit) query.set("limit", String(params.limit));
    if (params.after) query.set("after", params.after);
    const text = query.toString();
    return call<CatalogPage>(`/mcp-catalog${text ? `?${text}` : ""}`);
  },
  get: (name: string) => call<CatalogEntry>(`/mcp-catalog/${name.split("/").map(encodeURIComponent).join("/")}`),
  install: (body: CatalogInstall) => call<McpServer>("/mcp-servers/from-catalog", { method: "POST", body: JSON.stringify(body) }),
};

export function packageLabel(pkg: CatalogPackage): string {
  return `${pkg.registry_type}: ${pkg.identifier}${pkg.version ? `@${pkg.version}` : ""}`;
}
