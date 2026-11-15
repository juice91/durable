/**
 * Resolve a `github.com/mcp/<slug>` URL to a prefilled MCP server registration so a Studio user
 * can paste from the GitHub MCP registry (<https://github.com/mcp>) without retyping. Resolution
 * is purely client-side: no network call, no credentials. A tenant always enters their own auth
 * token in the Headers section; the registry never carries secrets (docs/plans/2026-11-15-milestone-mcp-catalog.md).
 *
 * Prefilled URLs are best-effort via a small in-source allowlist of well-known remote endpoints.
 * A slug not in the allowlist still prefills a safe-slugged `name` and leaves `url` null, so the
 * user types the endpoint themselves.
 */

export interface RegistryMatch {
  /** The slug from github.com/mcp/<slug>, lowercased. */
  slug: string;
  /** A name that matches the backend NAME_PATTERN `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`. */
  name: string;
  /** A prefilled URL from the allowlist, or null when the slug is not known. */
  url: string | null;
}

/**
 * Well-known remote MCP endpoints. Starting points to speed up registration; the tenant can edit
 * every field. An unlisted slug lands with `url: null`.
 */
const ALLOWLIST: Readonly<Record<string, string>> = {
  github: "https://api.githubcopilot.com/mcp/",
  gitlab: "https://gitlab.com/api/v4/mcp",
  jira: "https://mcp.atlassian.com/v1/sse",
  notion: "https://mcp.notion.com/mcp",
  slack: "https://mcp.slack.com/mcp",
};

/** The registry home, used by the Studio "Browse the GitHub MCP registry" link. */
export const MCP_REGISTRY_URL = "https://github.com/mcp";

/**
 * Parse a pasted URL. Returns null when the input is not a `https://github.com/mcp/<slug>` link
 * or when no character survives the slug safe-slugger, so the caller keeps the user's own input.
 */
export function parseRegistryUrl(raw: string): RegistryMatch | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return null;
  }
  if (parsed.protocol !== "https:") return null;
  if (parsed.host !== "github.com") return null;
  const parts = parsed.pathname.split("/").filter(Boolean);
  if (parts.length < 2 || parts[0] !== "mcp") return null;
  const slug = parts[1].toLowerCase();
  const name = toServerName(slug);
  if (!name) return null;
  return { slug, name, url: ALLOWLIST[slug] ?? null };
}

/**
 * Safe-slug `raw` to match the backend's server-name regex: must start with a letter or digit,
 * then up to 63 more of letters, digits, `_`, `.` or `-`. Empty when no character survives.
 */
export function toServerName(raw: string): string {
  return raw
    .replace(/[^A-Za-z0-9_.-]+/g, "-")
    .replace(/^[^A-Za-z0-9]+/, "")
    .slice(0, 64);
}
