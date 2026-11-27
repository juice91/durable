// Every API client that words its own errors must explain a refusal too (not only `apiErrorFromResponse`).
import { afterEach, describe, expect, it, vi } from "vitest";
import { iamAnswer } from "@/test/iamFixtures";
import { startTurnWithOptions } from "@/components/chat/turnApi";
import { api } from "./api";
import { draftWorkflow } from "./architect-workflow";
import { saveAgent } from "./architect";
import { loadCapabilities, resetCapabilitiesForTests } from "./iam";
import { createSource } from "./kbSources";
import { startMcpToolRun } from "./mcpToolRuns";
import { putWorkingMemory } from "./memory";
import { createSchedule } from "./schedules";
import { putSecret, deleteSecret } from "./secrets";
import { startRun } from "./runs";
import { startToolRun } from "./toolRuns";
import { publishWorkflow, startWorkflowRun } from "./workflows";
import { processors } from "./processors";

const REFUSED = { action: "run", kind: "agent", id: "a1" };
const SENTENCE = "Your role (viewer) can't run this agent. Ask an admin for the editor role or a runner grant on it.";

async function refuseAll() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const iam = iamAnswer("viewer", String(url));
      // The explanation answers about whatever was asked; the refused body here is always the same one.
      if (iam) return iam;
      return new Response(JSON.stringify({ detail: "not allowed", code: "forbidden", refused: REFUSED }), { status: 403, headers: { "Content-Type": "application/json" } });
    }),
  );
  await loadCapabilities();
}

afterEach(() => {
  resetCapabilitiesForTests();
  vi.unstubAllGlobals();
});

describe("a refusal reads the same from every client", () => {
  const spec = {} as never;
  it.each<[string, () => Promise<unknown>]>([
    ["publish an agent", () => api.versions.create("a1")],
    ["save an agent", () => api.agents.update("a1", spec)],
    ["start a run", () => startRun("a1", "r1", "hi")],
    ["start a turn", () => startTurnWithOptions("c1", "r1", "hi", spec)],
    ["start a workflow run", () => startWorkflowRun("w1", {})],
    ["publish a workflow", () => publishWorkflow("w1")],
    ["run a tool", () => startToolRun("t", {})],
    ["run an MCP tool", () => startMcpToolRun("s", "t", {})],
    ["add a secret", () => putSecret("n", "obviously-fake-value")],
    ["delete a secret", () => deleteSecret("n")],
    ["create a source", () => createSource("k1", spec)],
    ["create a schedule", () => createSchedule("w1", spec)],
    ["save through the Architect", () => saveAgent(spec, { publish: true })],
    ["draft a workflow", () => draftWorkflow("d")],
    ["write memory", () => putWorkingMemory({ conversationId: "c1" } as never, "x", 0)],
    ["test a processor", () => processors.test([], "text")],
  ])("%s", async (_name, call) => {
    await refuseAll();
    await expect(call()).rejects.toMatchObject({ status: 403, message: SENTENCE });
  });
});
