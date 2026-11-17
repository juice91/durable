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

describe("networkSummary", () => {
  it("words the network block", async () => {
    const { networkSummary } = await import("./mcpCatalog");
    expect(networkSummary(null)).toBeNull();
    expect(networkSummary({ allow_host: [], allow_port: [443], insecure_allow_all: true })).toBe("any host on 443");
    expect(networkSummary({ allow_host: [], allow_port: [], insecure_allow_all: true })).toBe("any host");
    expect(networkSummary({ allow_host: ["a.com", "b.com", ".c.com"], allow_port: [443], insecure_allow_all: false })).toBe("3 listed hosts");
    expect(networkSummary({ allow_host: [], allow_port: [], insecure_allow_all: false })).toBe("no network");
  });
});

describe("mcpCatalog.list source", () => {
  it("sends the source filter", async () => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "dev-token");
    const fetchMock = vi.fn(async (_url: string) => new Response(JSON.stringify({ items: [], next_after: null }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await mcpCatalog.list({ source: "toolhive" });
    expect(String(fetchMock.mock.calls[0][0])).toContain("/mcp-catalog?source=toolhive");
    vi.unstubAllGlobals();
  });
});
