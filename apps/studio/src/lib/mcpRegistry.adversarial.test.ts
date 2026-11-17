import { describe, expect, it } from "vitest";
import { parseRegistryUrl, toServerName } from "./mcpRegistry";

// Must match apps/api/src/eap_api/routes/mcp_servers.py NAME_PATTERN.
const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

describe("parseRegistryUrl — URL shape", () => {
  it("ignores a trailing slash after the slug", () => {
    expect(parseRegistryUrl("https://github.com/mcp/notion/"))
      .toEqual({ slug: "notion", name: "notion", url: "https://mcp.notion.com/mcp" });
  });

  it("ignores a query string", () => {
    expect(parseRegistryUrl("https://github.com/mcp/github?from=search&utm=x"))
      .toEqual({ slug: "github", name: "github", url: "https://api.githubcopilot.com/mcp/" });
  });

  it("ignores a fragment", () => {
    expect(parseRegistryUrl("https://github.com/mcp/slack#overview"))
      .toEqual({ slug: "slack", name: "slack", url: "https://mcp.slack.com/mcp" });
  });

  it("rejects an http (non-https) URL", () => {
    expect(parseRegistryUrl("http://github.com/mcp/github")).toBeNull();
  });

  it("rejects another host, even with the same /mcp path", () => {
    expect(parseRegistryUrl("https://evil.example.com/mcp/github")).toBeNull();
    expect(parseRegistryUrl("https://api.github.com/mcp/github")).toBeNull();
    expect(parseRegistryUrl("https://github.com.evil.com/mcp/github")).toBeNull();
  });
});

describe("parseRegistryUrl — slug shape", () => {
  it("resolves `../` naturally: `/mcp/../foo` collapses to `/foo`, no longer under /mcp", () => {
    expect(parseRegistryUrl("https://github.com/mcp/../foo")).toBeNull();
    expect(parseRegistryUrl("https://github.com/mcp/foo/../bar")).toEqual({
      slug: "bar",
      name: "bar",
      url: null,
    });
  });

  it("lowercases an uppercase slug and matches the allowlist case-insensitively", () => {
    const out = parseRegistryUrl("https://github.com/mcp/GITHUB");
    expect(out).not.toBeNull();
    expect(out!.slug).toBe("github");
    expect(out!.url).toBe("https://api.githubcopilot.com/mcp/");
    expect(NAME_PATTERN.test(out!.name)).toBe(true);
  });

  it("safe-slugs a unicode slug (`café`) via its percent-encoded form", () => {
    const out = parseRegistryUrl("https://github.com/mcp/café");
    expect(out).not.toBeNull();
    expect(out!.url).toBeNull();
    expect(NAME_PATTERN.test(out!.name)).toBe(true);
    expect(out!.name).not.toMatch(/[^A-Za-z0-9_.-]/);
  });

  it("clamps a 100+ character slug to 64 characters and still matches NAME_PATTERN", () => {
    const long = "a".repeat(150);
    const out = parseRegistryUrl(`https://github.com/mcp/${long}`);
    expect(out).not.toBeNull();
    expect(out!.name.length).toBeLessThanOrEqual(64);
    expect(NAME_PATTERN.test(out!.name)).toBe(true);
  });

  it("rejects an empty or missing slug (`/mcp`, `/mcp/`)", () => {
    expect(parseRegistryUrl("https://github.com/mcp")).toBeNull();
    expect(parseRegistryUrl("https://github.com/mcp/")).toBeNull();
    expect(parseRegistryUrl("https://github.com/mcp//notion")).toEqual({
      slug: "notion",
      name: "notion",
      url: "https://mcp.notion.com/mcp",
    });
  });

  it("returns null when safe-slugging empties the name (slug is only hyphens or punctuation)", () => {
    expect(parseRegistryUrl("https://github.com/mcp/---")).toBeNull();
    expect(parseRegistryUrl("https://github.com/mcp/...")).toBeNull();
  });

  it("strips a leading hyphen so a slug that starts with `-` is still a safe name", () => {
    const out = parseRegistryUrl("https://github.com/mcp/-pager-duty");
    expect(out).not.toBeNull();
    expect(out!.name).toBe("pager-duty");
    expect(NAME_PATTERN.test(out!.name)).toBe(true);
  });
});

describe("toServerName — NAME_PATTERN compatibility", () => {
  it.each([
    ["github", "github"],
    ["linear_prod", "linear_prod"],
    ["gitlab.com", "gitlab.com"],
    ["jira-cloud", "jira-cloud"],
    ["Mixed.Case_Slug-1", "Mixed.Case_Slug-1"],
  ])("%s stays intact and matches NAME_PATTERN", (input, expected) => {
    const name = toServerName(input);
    expect(name).toBe(expected);
    expect(NAME_PATTERN.test(name)).toBe(true);
  });

  it("returns empty for inputs that have no alphanumeric character", () => {
    expect(toServerName("")).toBe("");
    expect(toServerName("---")).toBe("");
    expect(toServerName("///")).toBe("");
    expect(toServerName("...")).toBe("");
    expect(toServerName("$$$")).toBe("");
  });

  it("never emits a name containing a character NAME_PATTERN forbids", () => {
    const names = [
      "a b/c?d:e;f#g",
      "/forward/slashes/",
      "colon:name",
      "\n\twhitespace\n",
      "abc" + "é".repeat(50),
    ].map((s) => toServerName(s));
    for (const n of names) {
      if (!n) continue; // empty is the documented graceful case
      expect(NAME_PATTERN.test(n)).toBe(true);
    }
  });
});

describe("parseRegistryUrl — allowlist coverage", () => {
  it.each([
    ["github", "https://api.githubcopilot.com/mcp/"],
    ["gitlab", "https://gitlab.com/api/v4/mcp"],
    ["jira", "https://mcp.atlassian.com/v1/sse"],
    ["notion", "https://mcp.notion.com/mcp"],
    ["slack", "https://mcp.slack.com/mcp"],
  ])("the five documented slugs prefill a URL (%s)", (slug, url) => {
    expect(parseRegistryUrl(`https://github.com/mcp/${slug}`)).toEqual({ slug, name: slug, url });
  });

  it("a sixth, unlisted slug returns the name only (url: null)", () => {
    expect(parseRegistryUrl("https://github.com/mcp/pagerduty")).toEqual({
      slug: "pagerduty",
      name: "pagerduty",
      url: null,
    });
    expect(parseRegistryUrl("https://github.com/mcp/linear")).toEqual({
      slug: "linear",
      name: "linear",
      url: null,
    });
  });
});
