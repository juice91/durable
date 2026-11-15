// Which PydanticOS agents and workflows this example serves, read from the server's environment.
// Server-only: the API key never leaves this process (see app/api/copilotkit/route.ts).
//
//   PYDANTICOS_API_URL      the API origin, e.g. http://127.0.0.1:8000 (no /api/v1)
//   PYDANTICOS_API_KEY      a tenant API key, pos_<id>_<secret> (Studio > Settings > API keys)
//   PYDANTICOS_AGENT_ID     the published agent to chat with, served as "pydanticosAgent"
//   PYDANTICOS_WORKFLOW_ID  optional: a published workflow, served as "pydanticosWorkflow"
//   PYDANTICOS_AGENTS       optional: more agents, "name=agentId,name=agentId" (names are the
//                           CopilotKit agent ids, letters, digits and _ only)

export type TargetKind = "agent" | "workflow";

export interface Target {
  /** The CopilotKit agent id (the key in CopilotRuntime's `agents` map). */
  name: string;
  kind: TargetKind;
  id: string;
}

const NAME = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;

function env(name: string): string | undefined {
  const value = process.env[name]?.trim();
  return value ? value : undefined;
}

/** The agents and workflows to serve, in display order. Throws on a malformed entry. */
export function configuredTargets(): Target[] {
  const targets: Target[] = [];
  const agentId = env("PYDANTICOS_AGENT_ID");
  if (agentId) targets.push({ name: "pydanticosAgent", kind: "agent", id: agentId });
  const workflowId = env("PYDANTICOS_WORKFLOW_ID");
  if (workflowId) targets.push({ name: "pydanticosWorkflow", kind: "workflow", id: workflowId });
  for (const entry of (env("PYDANTICOS_AGENTS") ?? "").split(",")) {
    const item = entry.trim();
    if (!item) continue;
    const [name, id, ...rest] = item.split("=").map((part) => part.trim());
    if (!name || !id || rest.length > 0 || !NAME.test(name)) {
      throw new Error(`PYDANTICOS_AGENTS: expected name=agentId, got ${JSON.stringify(item)}`);
    }
    if (targets.some((target) => target.name === name)) throw new Error(`PYDANTICOS_AGENTS: ${name} is listed twice`);
    targets.push({ name, kind: "agent", id });
  }
  return targets;
}

/** `POST /api/v1/ag-ui/agents/{id}` or `/api/v1/ag-ui/workflows/{id}`. */
export function endpointUrl(target: Target): string {
  const base = (env("PYDANTICOS_API_URL") ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
  return `${base}/api/v1/ag-ui/${target.kind === "agent" ? "agents" : "workflows"}/${encodeURIComponent(target.id)}`;
}
