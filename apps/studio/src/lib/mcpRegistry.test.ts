import { describe, expect, it } from "vitest";
import { MCP_REGISTRY_URL, parseRegistryUrl, toServerName } from "./mcpRegistry";

describe("parseRegistryUrl", () => {
  it("returns an allowlisted template URL when the slug is known", () => {
    expect(parseRegistryUrl("https://github.com/mcp/github")).toEqual({
      slug: "github",
      name: "github",
      url: "https://api.githubcopilot.com/mcp/",
    });
  });

  it("returns a safe-slugged name and a null URL for an unknown slug", () => {
    expect(parseRegistryUrl("https://github.com/mcp/pagerduty")).toEqual({
      slug: "pagerduty",
      name: "pagerduty",
      url: null,
    });
  });

  it("lowercases the slug and ignores trailing paths and query strings", () => {
    expect(parseRegistryUrl("https://github.com/mcp/Notion/details?from=search"))
      .toEqual({ slug: "notion", name: "notion", url: "https://mcp.notion.com/mcp" });
  });

  it("trims leading and trailing whitespace before parsing", () => {
    expect(parseRegistryUrl("   https://github.com/mcp/gitlab   "))
      .toEqual({ slug: "gitlab", name: "gitlab", url: "https://gitlab.com/api/v4/mcp" });
  });

  it.each([
    ["an empty input", ""],
    ["plain text", "not a url"],
    ["http instead of https", "http://github.com/mcp/github"],
    ["another host", "https://registry.example.com/mcp/github"],
    ["a non-mcp path", "https://github.com/anthropics/mcp"],
    ["mcp with no slug", "https://github.com/mcp"],
    ["mcp with trailing slash and no slug", "https://github.com/mcp/"],
  ])("returns null for %s", (_label, input) => {
    expect(parseRegistryUrl(input)).toBeNull();
  });

  it("exposes the registry home URL for the Browse link", () => {
    expect(MCP_REGISTRY_URL).toBe("https://github.com/mcp");
  });
});

describe("toServerName", () => {
  it("keeps allowed characters and clamps to 64", () => {
    expect(toServerName("github")).toBe("github");
    expect(toServerName("a".repeat(100))).toBe("a".repeat(64));
  });

  it("replaces unsupported runs with a single dash and strips a non-alphanumeric head", () => {
    expect(toServerName("--foo bar/baz!qux")).toBe("foo-bar-baz-qux");
  });

  it("returns an empty string when nothing alphanumeric survives", () => {
    expect(toServerName("///...")).toBe("");
    expect(toServerName("   ")).toBe("");
  });
});
