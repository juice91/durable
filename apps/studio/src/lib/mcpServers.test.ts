import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  buildHeadersPayload,
  describeMcpApiMessage,
  describeMcpError,
  mcpServers,
  rowsFromServer,
  withMcpTools,
  type HeaderRow,
} from "./mcpServers";
import type { AgentSpec } from "./api";

const baseSpec: AgentSpec = {
  spec_version: 1,
  name: "a",
  instructions: "i",
  model: { provider: "test", name: "m" },
  tools: [],
};

describe("buildHeadersPayload (write-only headers)", () => {
  it("sends nothing when existing headers are untouched", () => {
    expect(buildHeadersPayload(rowsFromServer(["authorization", "x-api-key"]))).toEqual({ kind: "unchanged" });
  });

  it("never sends a value that was not replaced: kept headers block a partial change", () => {
    const rows = rowsFromServer(["authorization", "x-api-key"]);
    const changed: HeaderRow[] = rows.map((row) => (row.name === "x-api-key" ? { ...row, state: "replace", value: "new-key" } as HeaderRow : row));
    const result = buildHeadersPayload(changed);
    expect(result.kind).toBe("invalid");
    expect(result.kind === "invalid" && result.message).toContain("authorization");
  });

  it("sends only replaced and new values", () => {
    const rows = rowsFromServer(["authorization"]).map((row) => ({ ...row, state: "replace", value: "Bearer t" }) as HeaderRow);
    rows.push({ key: "n1", origin: "new", name: "X-Team", value: "blue" });
    expect(buildHeadersPayload(rows)).toEqual({ kind: "set", headers: { authorization: "Bearer t", "X-Team": "blue" } });
  });

  it("removing every header sends an empty set", () => {
    const rows = rowsFromServer(["authorization"]).map((row) => ({ ...row, state: "removed" }) as HeaderRow);
    expect(buildHeadersPayload(rows)).toEqual({ kind: "set", headers: {} });
  });

  it("on create, leaves headers out when there are none and ignores blank rows", () => {
    expect(buildHeadersPayload([], true)).toEqual({ kind: "unchanged" });
    expect(buildHeadersPayload([{ key: "n", origin: "new", name: "", value: "" }], true)).toEqual({ kind: "unchanged" });
  });

  it("rejects a new header without a value or a duplicate name", () => {
    expect(buildHeadersPayload([{ key: "n", origin: "new", name: "X-A", value: "" }], true).kind).toBe("invalid");
    expect(buildHeadersPayload([
      { key: "a", origin: "new", name: "X-A", value: "1" },
      { key: "b", origin: "new", name: "x-a", value: "2" },
    ], true).kind).toBe("invalid");
  });
});

describe("withMcpTools", () => {
  it("omits mcp_tools entirely when empty (spec hash stability)", () => {
    const spec = withMcpTools({ ...baseSpec, mcp_tools: [{ server: "s", tool: "t" }] } as AgentSpec, []);
    expect("mcp_tools" in spec).toBe(false);
    expect(JSON.stringify(spec)).not.toContain("mcp_tools");
  });

  it("sets the server-name/tool pairs when present", () => {
    expect(withMcpTools(baseSpec, [{ server: "docs", tool: "search" }]).mcp_tools).toEqual([{ server: "docs", tool: "search" }]);
  });
});

describe("error messages", () => {
  it("explains outbound policy refusals", () => {
    expect(describeMcpError("address_blocked")).toMatch(/private, loopback or link-local/);
    expect(describeMcpApiMessage("url refused by the outbound policy: address_blocked")).toMatch(/private, loopback/);
    expect(describeMcpApiMessage("an MCP server with this name already exists")).toBe("an MCP server with this name already exists");
    expect(describeMcpError("weird_code")).toContain("weird_code");
  });
});

describe("mcpServers client", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "dev-token");
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("toggles enabled with a PATCH that carries no headers", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ id: "1" }), { status: 200 }));
    await mcpServers.setEnabled("1", false);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/v1\/mcp-servers\/1$/);
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).toEqual({ enabled: false });
  });

  it("delete resolves on 204", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await expect(mcpServers.remove("1")).resolves.toBeUndefined();
  });
});
