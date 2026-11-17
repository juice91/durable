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
};
