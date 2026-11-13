import { describe, expect, it } from "vitest";
import { RouteIdError, routes } from "./index";

describe("mcp routes", () => {
  it("builds the list and detail URLs, encoding the id", () => {
    expect(routes.mcp()).toBe("/mcp");
    expect(routes.mcpServer("srv-1")).toBe("/mcp/srv-1");
    expect(routes.mcpServer("a/b:c d")).toBe("/mcp/a%2Fb%3Ac%20d");
    expect(() => routes.mcpServer("..")).toThrow(RouteIdError);
  });
});
