import type { ArchitectOptions, ArchitectState } from "@/lib/architect";

export const SEEDED_STATE: ArchitectState = {
  seeded: true,
  choose_model: false,
  architect_agent_id: "row-arch",
  drafter_agent_id: "row-draft",
  model: "litellm:qwen3.8",
  agents: [
    { id: "faq-bot", agent_id: "row-1", name: "FAQ bot", versions: 2, latest_created_via: "architect-page", draft: true, archived: false },
    { id: "old-bot", agent_id: "row-2", name: "Old bot", versions: 1, latest_created_via: null, draft: false, archived: true },
  ],
  workflows: [{ id: "triage", workflow_id: "row-w1", name: "Triage", versions: 1, latest_created_via: "architect:run1", draft: false, archived: false }],
};

export const OPTIONS: ArchitectOptions = {
  tools: [
    { id: "http.get", title: "HTTP GET", description: "Fetch a URL.", requires_approval: false },
    { id: "send_email", title: "Send email", description: "Send an email.", requires_approval: true },
  ],
  mcp_servers: [{ id: "m1", name: "docs", tools: ["search", "open"] }],
  knowledge_bases: [{ id: "11111111-1111-4111-8111-111111111111", name: "Product docs" }],
  models: [
    { id: "litellm:qwen3.8", provider: "litellm", capabilities: { image: false, pdf: false, source: "static" } },
    { id: "litellm:other", provider: "litellm", capabilities: { image: true, pdf: false, source: "static" } },
  ],
  agents: [{ id: "faq-bot", name: "FAQ bot" }],
  default_model: "litellm:qwen3.8",
};

export const DRAFT_RESPONSE = {
  spec: {
    name: "Refund helper",
    id: "refund-helper",
    description: "Answers refund questions.",
    instructions: "Help the customer with refunds.",
    model: "litellm:other",
    tools: ["http.get", "send_email"],
    mcp_tools: [{ server: "docs", tool: "search" }],
    knowledge_bases: ["11111111-1111-4111-8111-111111111111"],
    memory: true,
    model_settings: { temperature: 0.2, max_tokens: 800 },
  },
  assumptions: ["Refunds are allowed within 30 days.", "Replies are in English."],
  dropped: [{ kind: "tool", id: "shell.run" }],
  run_id: "run-1",
  model: "litellm:qwen3.8",
};

/** A fetch stub keyed by "METHOD /path"; a function value receives the parsed body. */
export function stubArchitectApi(routes: Record<string, unknown | ((body: unknown) => { status?: number; body: unknown })>) {
  const calls: { key: string; body: unknown }[] = [];
  const fetchMock = async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(typeof input === "string" ? input : (input as Request).url), "http://localhost");
    const method = init?.method ?? "GET";
    const key = `${method} ${url.pathname.replace(/^\/api\/v1/, "")}`;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ key, body });
    const handler = routes[key];
    if (handler === undefined) return new Response(JSON.stringify({ detail: `no stub for ${key}` }), { status: 404 });
    const result = typeof handler === "function" ? (handler as (b: unknown) => { status?: number; body: unknown })(body) : { body: handler };
    return new Response(JSON.stringify(result.body), { status: result.status ?? 200, headers: { "content-type": "application/json" } });
  };
  return { fetchMock, calls };
}
