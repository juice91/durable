// Seeds a demo tenant on a PydanticOS API in dev auth mode, then writes .env.local for the example.
//
//   node scripts/seed.mjs <api-origin> <mcp-url> [--scripted]
//
// With the dev bearer (`dev:<tenant>:<user>`) it registers the MCP server, publishes the agents
// and a workflow, and creates a tenant-wide API key (keys can only be created through a dev-mode
// API until enterprise auth exists). The key's plaintext goes into .env.local only.
//
// --scripted uses the platform's scripted `test:*` models (no LLM; what demo-stack.sh runs), one
// agent per scenario. Without it, set PYDANTICOS_SEED_MODEL (e.g. litellm:<name>) and every agent
// uses that model.
import { randomUUID } from "node:crypto";
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const [origin, mcpUrl, flag] = process.argv.slice(2);
if (!origin || !mcpUrl) {
  console.error("usage: node scripts/seed.mjs <api-origin> <mcp-url> [--scripted]");
  process.exit(2);
}
const scripted = flag === "--scripted";
const realModel = process.env.PYDANTICOS_SEED_MODEL;
if (!scripted && !realModel) {
  console.error("Set PYDANTICOS_SEED_MODEL (e.g. litellm:<name>) or pass --scripted");
  process.exit(2);
}
const base = `${origin.replace(/\/+$/, "")}/api/v1`;
const tenant = process.env.PYDANTICOS_SEED_TENANT ?? `ckdemo${randomUUID().replace(/-/g, "").slice(0, 8)}`;
const headers = { Authorization: `Bearer dev:${tenant}:demo-user`, "Content-Type": "application/json" };

async function call(method, path, body) {
  const response = await fetch(base + path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${path} -> ${response.status} ${text.slice(0, 400)}`);
  return text ? JSON.parse(text) : undefined;
}

function model(scriptedName) {
  if (scripted) return { provider: "test", name: scriptedName };
  const [provider, ...rest] = realModel.split(":");
  return { provider, name: rest.join(":") };
}

async function publishAgent(spec) {
  const agent = await call("POST", "/agents", { spec: { spec_version: 1, ...spec } });
  await call("POST", `/agents/${agent.id}/versions`);
  return agent.id;
}

await call("POST", "/mcp-servers", { name: "demo", url: mcpUrl });
const assistant = await publishAgent({
  name: "Demo assistant (MCP)",
  instructions: "Help with orders. Use lookup_order (or slow_lookup) to check an order.",
  model: model("mcp"),
  mcp_tools: [
    { server: "demo", tool: "lookup_order" },
    { server: "demo", tool: "slow_lookup" },
  ],
});
const browserTools = await publishAgent({
  name: "Demo browser tools",
  instructions: "Use the browser's tools: set_theme_color to change the page colour, confirm_with_user to ask the user.",
  model: model("frontend_tool"),
});
const gated = await publishAgent({
  name: "Demo gated tickets",
  instructions: "Create tickets when asked.",
  model: model("approval"),
  tools: ["tickets.create"],
  approvals: { tools: ["tickets.create"] },
});
const echo = await publishAgent({ name: "Demo echo", instructions: "Echo.", model: model("echo") });
const workflow = await call("POST", "/workflows", {
  spec: {
    name: "Demo approval workflow",
    input_schema: { type: "object", properties: { q: { type: "string" } }, required: ["q"], additionalProperties: false },
    start: "ask",
    steps: [
      { id: "ask", type: "agent", agent_id: echo, input: { prompt: { $ref: "input.q" } }, next: "appr" },
      { id: "appr", type: "approval", prompt: "Escalate this request?", timeout_hours: 1 },
    ],
  },
});
await call("POST", `/workflows/${workflow.id}/versions`);
const created = await call("POST", "/api-keys", { name: "CopilotKit example" });

const here = dirname(fileURLToPath(import.meta.url));
const envFile = join(here, "..", ".env.local");
writeFileSync(
  envFile,
  [
    `# Written by scripts/seed.mjs for tenant ${tenant}.`,
    `PYDANTICOS_API_URL=${origin.replace(/\/+$/, "")}`,
    `PYDANTICOS_API_KEY=${created.key}`,
    `PYDANTICOS_AGENT_ID=${assistant}`,
    `PYDANTICOS_WORKFLOW_ID=${workflow.id}`,
    `PYDANTICOS_AGENTS=browserTools=${browserTools},gatedTickets=${gated}`,
    "COPILOTKIT_TELEMETRY_DISABLED=true",
    "",
  ].join("\n"),
  { mode: 0o600 },
);
console.log(`Seeded tenant ${tenant}; wrote ${envFile} (the key is in that file only).`);
console.log(`  pydanticosAgent=${assistant}  browserTools=${browserTools}  gatedTickets=${gated}  pydanticosWorkflow=${workflow.id}`);
