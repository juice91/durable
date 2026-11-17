import type { CatalogEntry } from "@/lib/mcpCatalog";

export const LOCAL_ENTRY: CatalogEntry = {
  name: "io.example/files",
  title: "Files",
  description: "Read and write files.",
  version: "1.2.0",
  status: "active",
  licence: "MIT",
  packages: [
    {
      registry_type: "npm",
      identifier: "@example/files",
      version: "1.2.0",
      transport: "stdio",
      environment_variables: [
        { name: "ROOT_DIR", description: "Directory to expose", required: true, secret: false },
        { name: "API_TOKEN", description: "Token for the backend", required: true, secret: true },
        { name: "LOG_LEVEL", description: null, required: false, secret: false },
      ],
    },
    { registry_type: "pypi", identifier: "example-files", version: "1.2.0", transport: "stdio", environment_variables: [] },
  ],
  remotes: [{ type: "streamable-http", url: "https://files.example.test/mcp" }],
  local: true,
  remote: true,
  fetched_at: "2026-11-17T10:00:00Z",
  source: "official",
  tier: null,
  tags: [],
  network: null,
  tool_count: 0,
};

export const REMOTE_ENTRY: CatalogEntry = {
  name: "io.example/search",
  title: "Search",
  description: "Web search.",
  version: "0.4.0",
  status: "deprecated",
  licence: null,
  packages: [],
  remotes: [{ type: "streamable-http", url: "https://search.example.test/mcp" }],
  local: false,
  remote: true,
  fetched_at: "2026-11-17T10:00:00Z",
  source: "official",
  tier: null,
  tags: [],
  network: null,
  tool_count: 0,
};

export const TOOLHIVE_ENTRY: CatalogEntry = {
  name: "io.github.stacklok/fetch",
  title: "Fetch",
  description: "Allows you to fetch content from the web",
  version: "1.0.0",
  status: "active",
  licence: null,
  packages: [
    { registry_type: "oci", identifier: "ghcr.io/stackloklabs/gofetch/server:1.0.6", version: null, transport: "streamable-http", environment_variables: [] },
  ],
  remotes: [],
  local: true,
  remote: false,
  fetched_at: "2026-11-18T10:00:00Z",
  source: "toolhive",
  tier: "Official",
  tags: ["web", "fetch"],
  network: { allow_host: [], allow_port: [443], insecure_allow_all: true },
  tool_count: 1,
};
