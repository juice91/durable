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
  source: CatalogSource;
  tier: string | null;
  tags: string[];
  network: CatalogNetwork | null;
  tool_count: number;
}

export type CatalogSource = "official" | "toolhive";

export interface CatalogNetwork {
  allow_host: string[];
  allow_port: number[];
  insecure_allow_all: boolean;
}

/** What a ToolHive entry may reach, in words; null when the entry declares no network block. */
export function networkSummary(network: CatalogNetwork | null | undefined): string | null {
  if (!network) return null;
  if (network.insecure_allow_all) {
    return network.allow_port.length > 0 ? `any host on ${network.allow_port.join(", ")}` : "any host";
  }
  const hosts = network.allow_host.length;
  if (hosts === 0) return "no network";
  return hosts === 1 ? "1 listed host" : `${hosts} listed hosts`;
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
  list: (params: { q?: string; limit?: number; after?: string | null; source?: CatalogSource } = {}) => {
    const query = new URLSearchParams();
    if (params.q?.trim()) query.set("q", params.q.trim());
    if (params.limit) query.set("limit", String(params.limit));
    if (params.after) query.set("after", params.after);
    if (params.source) query.set("source", params.source);
    const text = query.toString();
    return call<CatalogPage>(`/mcp-catalog${text ? `?${text}` : ""}`);
  },
  get: (name: string) => call<CatalogEntry>(`/mcp-catalog/${name.split("/").map(encodeURIComponent).join("/")}`),
  install: (body: CatalogInstall) => call<McpServer>("/mcp-servers/from-catalog", { method: "POST", body: JSON.stringify(body) }),
};

export function packageLabel(pkg: CatalogPackage): string {
  return `${pkg.registry_type}: ${pkg.identifier}${pkg.version ? `@${pkg.version}` : ""}`;
}
