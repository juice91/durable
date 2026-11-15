import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import "@testing-library/jest-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MCP_REGISTRY_URL, parseRegistryUrl, toServerName } from "./mcpRegistry";
import { McpServerForm } from "@/components/mcp/McpServerForm";

vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return {
    ...actual,
    mcpServers: { ...actual.mcpServers, create: vi.fn(), update: vi.fn() },
  };
});

// Must match apps/api/src/eap_api/routes/mcp_servers.py NAME_PATTERN.
const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

const el = (props: React.ComponentProps<typeof McpServerForm> = { onSaved: vi.fn(), onCancel: vi.fn() }) =>
  React.createElement(McpServerForm, props);

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

describe("Browse link — target/rel/href", () => {
  it("opens the registry URL in a new tab with a safe rel pair", () => {
    render(el());
    const link = screen.getByTestId("mcp-registry-link");
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute("href", MCP_REGISTRY_URL);
    expect(link).toHaveAttribute("href", "https://github.com/mcp");
    expect(link).toHaveAttribute("target", "_blank");
    const rel = link.getAttribute("rel") ?? "";
    expect(rel).toMatch(/\bnoopener\b/);
    expect(rel).toMatch(/\bnoreferrer\b/);
  });

  it("the link is hidden in edit mode (not rendered at all)", () => {
    const existing = {
      id: "srv-1",
      name: "docs",
      url: "https://mcp.example.com/mcp",
      transport: "streamable_http" as const,
      enabled: true,
      header_names: [] as string[],
      created_at: null,
      updated_at: null,
      last_checked_at: null,
      last_status: null,
      last_error_code: null,
    };
    render(el({ server: existing, onSaved: vi.fn(), onCancel: vi.fn() }));
    expect(screen.queryByTestId("mcp-registry-link")).toBeNull();
    expect(screen.queryByTestId("mcp-registry-paste")).toBeNull();
    expect(screen.queryByTestId("mcp-registry-paste-input")).toBeNull();
    expect(screen.queryByTestId("mcp-registry-paste-button")).toBeNull();
    expect(screen.queryByTestId("mcp-auth-header-hint")).toBeNull();
  });
});

describe("Paste-from-clipboard — adversarial inputs", () => {
  const originalClipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const setClipboard = (value: unknown) =>
    Object.defineProperty(navigator, "clipboard", { configurable: true, value });

  afterEach(() => {
    if (originalClipboard) Object.defineProperty(navigator, "clipboard", originalClipboard);
    else delete (navigator as { clipboard?: unknown }).clipboard;
  });

  it("clipboard permission denied: shows the fallback note and leaves Name/URL empty", async () => {
    setClipboard({
      readText: vi.fn(async () => {
        throw new Error("NotAllowedError");
      }),
    });
    render(el());
    fireEvent.click(screen.getByRole("button", { name: "Paste from clipboard" }));
    const note = await screen.findByTestId("mcp-registry-clipboard-note");
    expect(note).toHaveTextContent("Could not read the clipboard");
    expect(screen.getByLabelText("Name")).toHaveValue("");
    expect(screen.getByLabelText("URL")).toHaveValue("");
    expect(screen.queryByTestId("mcp-registry-resolution")).toBeNull();
  });

  it("clipboard empty: no error, no resolution, no fallback note", async () => {
    setClipboard({ readText: vi.fn(async () => "") });
    render(el());
    fireEvent.click(screen.getByRole("button", { name: "Paste from clipboard" }));
    // wait a tick for the async readText promise chain to settle
    await waitFor(() => expect(screen.getByLabelText("Paste from the GitHub MCP registry")).toHaveValue(""));
    expect(screen.queryByTestId("mcp-registry-resolution")).toBeNull();
    expect(screen.queryByTestId("mcp-registry-clipboard-note")).toBeNull();
    expect(screen.getByLabelText("Name")).toHaveValue("");
    expect(screen.getByLabelText("URL")).toHaveValue("");
  });

  it("clipboard holds non-URL text: input is filled verbatim, but nothing is prefilled", async () => {
    setClipboard({ readText: vi.fn(async () => "hello world; not a URL") });
    render(el());
    fireEvent.click(screen.getByRole("button", { name: "Paste from clipboard" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Paste from the GitHub MCP registry")).toHaveValue(
        "hello world; not a URL",
      ),
    );
    expect(screen.queryByTestId("mcp-registry-resolution")).toBeNull();
    expect(screen.getByLabelText("Name")).toHaveValue("");
    expect(screen.getByLabelText("URL")).toHaveValue("");
  });

  it("clipboard holds a non-github.com URL: the paste box echoes it, but nothing prefills", async () => {
    setClipboard({ readText: vi.fn(async () => "https://evil.example.com/mcp/github") });
    render(el());
    fireEvent.click(screen.getByRole("button", { name: "Paste from clipboard" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Paste from the GitHub MCP registry")).toHaveValue(
        "https://evil.example.com/mcp/github",
      ),
    );
    expect(screen.queryByTestId("mcp-registry-resolution")).toBeNull();
    expect(screen.getByLabelText("Name")).toHaveValue("");
    expect(screen.getByLabelText("URL")).toHaveValue("");
  });
});

describe("Registry paste-in — safety invariants", () => {
  it("renders the create-mode section without console.error/warn", () => {
    const errSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    try {
      render(el());
      fireEvent.change(screen.getByLabelText("Paste from the GitHub MCP registry"), {
        target: { value: "https://github.com/mcp/github" },
      });
      expect(screen.getByLabelText("URL")).toHaveValue("https://api.githubcopilot.com/mcp/");
      expect(errSpy).not.toHaveBeenCalled();
      expect(warnSpy).not.toHaveBeenCalled();
    } finally {
      errSpy.mockRestore();
      warnSpy.mockRestore();
    }
  });

  it("pasting a URL never writes anything to localStorage", () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    try {
      render(el());
      fireEvent.change(screen.getByLabelText("Paste from the GitHub MCP registry"), {
        target: { value: "https://github.com/mcp/github" },
      });
      // Also try a header-ish-looking string in case anything is tempted to persist it.
      fireEvent.change(screen.getByLabelText("Paste from the GitHub MCP registry"), {
        target: { value: "Bearer secret-token-9f" },
      });
      expect(setItem).not.toHaveBeenCalled();
    } finally {
      setItem.mockRestore();
    }
  });

  it("the paste-in testids used by this file are all rendered (no dead testids added)", () => {
    render(el());
    // Every testid this file queries must resolve on a happy-path create render.
    for (const id of [
      "mcp-registry-paste",
      "mcp-registry-link",
      "mcp-registry-paste-input",
      "mcp-registry-paste-button",
      "mcp-auth-header-hint",
    ]) {
      expect(screen.getByTestId(id)).toBeInTheDocument();
    }
  });
});
