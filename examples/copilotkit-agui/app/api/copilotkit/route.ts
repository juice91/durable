// The CopilotKit runtime: one HttpAgent per PydanticOS agent or workflow, each pointing at its
// AG-UI endpoint with the tenant API key from the server's environment. The browser talks only
// to this route, so the key never reaches it.
//
// HttpAgent comes from @ag-ui/client 1.0.2, the exact version @copilotkit/runtime 1.83.0 depends
// on: a different @ag-ui/client copy gives a type error on `agents` (two AbstractAgent classes).
import { HttpAgent } from "@ag-ui/client";
import { CopilotRuntime, ExperimentalEmptyAdapter, copilotRuntimeNextJSAppRouterEndpoint } from "@copilotkit/runtime";
import type { NextRequest } from "next/server";

import { configuredTargets, endpointUrl } from "@/lib/agents";

function buildRuntime(): CopilotRuntime {
  const key = process.env.PYDANTICOS_API_KEY?.trim();
  if (!key) throw new Error("Set PYDANTICOS_API_KEY (a pos_... tenant API key) in .env.local");
  const agents: Record<string, HttpAgent> = {};
  for (const target of configuredTargets()) {
    agents[target.name] = new HttpAgent({
      url: endpointUrl(target),
      headers: { Authorization: `Bearer ${key}` },
    });
  }
  if (Object.keys(agents).length === 0) throw new Error("Set PYDANTICOS_AGENT_ID (or PYDANTICOS_AGENTS) in .env.local");
  return new CopilotRuntime({ agents });
}

let runtime: CopilotRuntime | undefined;

export const POST = async (req: NextRequest) => {
  runtime ??= buildRuntime();
  const { handleRequest } = copilotRuntimeNextJSAppRouterEndpoint({
    runtime,
    // No LLM in this process: every agent is a remote AG-UI agent.
    serviceAdapter: new ExperimentalEmptyAdapter(),
    endpoint: "/api/copilotkit",
  });
  return handleRequest(req);
};
