// Where the site is published and where its source lives. A custom domain needs no code change: the Pages workflow
// passes SITE_URL and SITE_BASE from the repository's Pages settings.
export const SITE_URL = process.env.SITE_URL || "https://juice91.github.io";

/** "/durable" for a project site, "" at the root of a domain. */
export function normalizeBase(value) {
  const trimmed = String(value ?? "/durable").replace(/^\/+|\/+$/g, "");
  return trimmed ? `/${trimmed}` : "";
}

export const BASE = normalizeBase(process.env.SITE_BASE);
export const REPO_URL = "https://github.com/juice91/durable";
