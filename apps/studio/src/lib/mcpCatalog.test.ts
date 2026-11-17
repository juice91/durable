import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mcpCatalog } from "./mcpCatalog";

describe("mcpCatalog client", () => {
  beforeEach(() => vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "dev-token"));
  afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });
  it("builds the list query, escapes slashes in get and posts the install", async () => {
    const fetchMock = vi.fn(async () => new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await mcpCatalog.list({ q: " files ", limit: 30, after: "c1" });
    await mcpCatalog.list({});
    await mcpCatalog.get("io.example/files");
    await mcpCatalog.install({ catalog_name: "io.example/files", package_index: 0 });
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    const paths = calls.map((c) => c[0].replace(/^.*\/api\/v1/, ""));
    expect(paths).toEqual(["/mcp-catalog?q=files&limit=30&after=c1", "/mcp-catalog", "/mcp-catalog/io.example/files", "/mcp-servers/from-catalog"]);
    expect(calls[3][1].method).toBe("POST");
    expect(JSON.parse(String(calls[3][1].body))).toEqual({ catalog_name: "io.example/files", package_index: 0 });
  });
});
