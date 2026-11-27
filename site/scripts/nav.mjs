// What the documentation site shows, and where. Sources are paths under the repository's docs/ folder; a page is
// published only when it is listed here (design notes and maintainer documents stay on GitHub, and links to them
// point there). `slug` is the URL under /docs/ ("" is the documentation home).

const page = (source, slug, label) => ({ source, slug, label });

export const NAV = [
  {
    label: "Getting started",
    items: [
      page("overview.md", "", "Overview"),
      page("quickstart-compose.md", "quickstart", "Quick start"),
      page("admin-account.md", "admin-account", "Admin account"),
      page("expose.md", "expose", "Expose Studio and the API"),
    ],
  },
  {
    label: "Concepts",
    items: [
      page("concepts/architecture.md", "concepts/architecture", "How Durable works"),
      page("concepts/durable-execution.md", "concepts/durable-execution", "Durable execution"),
      page("workflows/proving-durability.md", "concepts/proving-durability", "Proving durability"),
      page("concurrency.md", "concepts/concurrency", "Concurrent edits"),
    ],
  },
  {
    label: "Agents",
    items: [
      page("guides/agents.md", "guides/agents", "Agents"),
      page("agents/agent-tools.md", "guides/agents-as-tools", "Agents as tools"),
      page("architect.md", "guides/architect", "Architect"),
      page("evals.md", "guides/eval-rules", "Eval rules"),
    ],
  },
  {
    label: "Workflows",
    items: [
      page("guides/workflows.md", "guides/workflows", "Workflows"),
      page("workflows/loops.md", "guides/workflow-loops", "Loops"),
      page("workflows/authoring.md", "guides/workflow-authoring", "Authoring conventions"),
      page("guides/schedules.md", "guides/schedules", "Schedules"),
      page("guides/approvals.md", "guides/approvals", "Approvals"),
      page("webhooks.md", "guides/webhooks", "Inbound webhooks"),
    ],
  },
  {
    label: "Knowledge and connections",
    items: [
      page("guides/knowledge-bases.md", "guides/knowledge-bases", "Knowledge bases"),
      page("knowledge-import.md", "guides/knowledge-import", "Import a directory"),
      page("providers/github.md", "guides/github-source", "GitHub source"),
      page("mcp-servers.md", "guides/mcp-servers", "MCP servers"),
      page("mcp-control.md", "guides/mcp-control", "Operate over MCP"),
      page("external-access.md", "guides/external-agents", "External agents"),
      page("ag-ui.md", "guides/ag-ui", "AG-UI frontends"),
    ],
  },
  {
    label: "Operate",
    items: [
      page("iam.md", "guides/identity-and-access", "Identity and access"),
      page("guides/model-providers.md", "guides/model-providers", "Model providers"),
      page("providers/anthropic-models.md", "guides/anthropic-models", "Anthropic models"),
      page("providers/cloudflare.md", "guides/cloudflare-workers-ai", "Cloudflare Workers AI"),
      page("guides/observability.md", "guides/observability", "Observability"),
      page("observability/external-traces.md", "guides/external-traces", "External traces"),
    ],
  },
  {
    label: "Reference",
    items: [
      page("configuration.md", "reference/configuration", "Configuration"),
      {
        label: "Built-in tools",
        collapsed: true,
        items: [
          page("tools/index.md", "reference/tools", "All tools"),
          page("tools/http-request.md", "reference/tools/http-request", "http.request"),
          page("tools/feed-parse.md", "reference/tools/feed-parse", "feed.parse"),
          page("tools/html-extract.md", "reference/tools/html-extract", "html.extract"),
          page("tools/json-query.md", "reference/tools/json-query", "json.query"),
          page("tools/text-check.md", "reference/tools/text-check", "text.check"),
          page("tools/atproto-richtext.md", "reference/tools/atproto-richtext", "atproto.richtext"),
          page("tools/workflow-history.md", "reference/tools/workflow-history", "workflow.history"),
          page("tools/clock.md", "reference/tools/clock", "clock"),
        ],
      },
      // Replaced by the pages generated from the API's OpenAPI schema (scripts/openapi.mjs).
      { api: true, label: "HTTP API", collapsed: true },
    ],
  },
  {
    label: "Contributing",
    items: [
      page("contributing.md", "contributing", "Contributing"),
      page("api-layering.md", "contributing/api-layering", "API layering"),
      page("ci.md", "contributing/ci", "CI"),
    ],
  },
];

/** Every listed page, in navigation order. */
export function pages(items = NAV) {
  return items.flatMap((item) => (item.items ? pages(item.items) : item.source ? [item] : []));
}

/** The route of a page below the site's base path, e.g. "docs/quickstart/". */
export function route(slug) {
  return slug ? `docs/${slug}/` : "docs/";
}

/** Starlight's sidebar; `apiPages` are the generated API reference pages. */
export function sidebar(apiPages = [], items = NAV) {
  return items.flatMap((item) => {
    if (item.api) {
      return apiPages.length ? [{ label: item.label, collapsed: true, items: apiPages.map((p) => ({ label: p.label, slug: `docs/${p.slug}` })) }] : [];
    }
    if (item.items) return [{ label: item.label, collapsed: item.collapsed ?? false, items: sidebar(apiPages, item.items) }];
    return [{ label: item.label, slug: item.slug ? `docs/${item.slug}` : "docs" }];
  });
}
