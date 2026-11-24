import { describe, expect, it } from "vitest";
import type { AgentSpec } from "@/lib/api";
import {
  agentToolProblems,
  agentToolsOf,
  agentToolsValue,
  defaultToolName,
  effectiveToolName,
  isAgentToolsError,
  normalizeAgentToolRef,
  typedInt,
} from "./agentTools";

const A = "6f0c5e5e-6d0a-4e0e-9d1e-0123456789ab";
const B = "7a1d6f6f-7e1b-4f1f-8e2f-1234567890bc";
const spec = (agent_tools: unknown) => ({ spec_version: 1, name: "Boss", instructions: "", model: { provider: "test", name: "echo" }, tools: [], agent_tools }) as unknown as AgentSpec;

describe("defaultToolName (mirrors eap_schemas.agent_tools.agent_tool_default_name)", () => {
  it.each([
    ["Research Helper", "research_helper"],
    ["  Café -- Desk!! ", "caf_desk"],
    ["2nd line", "agent_2nd_line"],
    ["x".repeat(80), "x".repeat(41)],
  ])("%s", (name, expected) => {
    expect(defaultToolName(name, A)).toBe(expected);
  });

  it.each([null, undefined, "", "!!!", "日本語"])("falls back to the id for %s", (name) => {
    expect(defaultToolName(name, A)).toBe("agent_6f0c5e5e");
  });

  it("an explicit name wins", () => {
    expect(effectiveToolName({ agent_id: A, name: "ask" }, "Whatever")).toBe("ask");
    expect(effectiveToolName({ agent_id: A }, "Research Helper")).toBe("research_helper");
  });
});

describe("the dump shape", () => {
  it("leaves every default and empty text out, in the model's key order", () => {
    expect(
      normalizeAgentToolRef({ max_calls_per_run: 5, timeout_seconds: 300, version: "latest", description: "  ", name: "", agent_id: A }),
    ).toEqual({ agent_id: A });
    const full = normalizeAgentToolRef({ max_calls_per_run: 2, timeout_seconds: 60, version: 3, description: "Help", name: "helper", agent_id: A });
    expect(Object.keys(full)).toEqual(["agent_id", "name", "description", "version", "timeout_seconds", "max_calls_per_run"]);
  });

  it("the key is removed while the list is empty", () => {
    expect(agentToolsValue([])).toBeUndefined();
    expect(agentToolsValue([{ agent_id: A, timeout_seconds: 300 }])).toEqual([{ agent_id: A }]);
  });

  it("typedInt: empty is the default, a number a number, other text is kept for the checks", () => {
    expect(typedInt("")).toBeUndefined();
    expect(typedInt("12")).toBe(12);
    expect(typedInt("abc")).toBe("abc");
  });
});

describe("agentToolProblems", () => {
  it("none for a good list or no list", () => {
    expect(agentToolProblems(spec(undefined))).toEqual([]);
    expect(agentToolProblems(spec([{ agent_id: A }, { agent_id: B, name: "other", version: 2, timeout_seconds: 10, max_calls_per_run: 20 }]))).toEqual([]);
  });

  it("flags a bad id, a duplicate agent, a bad or repeated name", () => {
    const problems = agentToolProblems(spec([{ agent_id: "nope" }, { agent_id: A, name: "Bad Name" }, { agent_id: A }, { agent_id: B, name: "x" }, { agent_id: "7a1d6f6f-7e1b-4f1f-8e2f-1234567890bd", name: "x" }]));
    expect(problems.map((p) => p.field)).toEqual(["0-agent_id", "1-name", "2-agent_id", "4-name"]);
  });

  it("flags version, timeout and max calls out of range or not whole", () => {
    const problems = agentToolProblems(spec([{ agent_id: A, version: 0, timeout_seconds: 3601, max_calls_per_run: "x" }, { agent_id: B, version: 1.5, timeout_seconds: 0, max_calls_per_run: 21 }]));
    expect(problems.map((p) => p.field)).toEqual([
      "0-version", "0-timeout_seconds", "0-max_calls_per_run", "1-version", "1-timeout_seconds", "1-max_calls_per_run",
    ]);
  });

  it("at most 8 refs", () => {
    const ids = Array.from({ length: 9 }, (_, i) => ({ agent_id: `6f0c5e5e-6d0a-4e0e-9d1e-01234567890${i}` }));
    expect(agentToolProblems(spec(ids)).map((p) => p.field)).toContain("list");
  });
});

describe("reading and error ownership", () => {
  it("agentToolsOf reads a list or nothing", () => {
    expect(agentToolsOf(spec(undefined))).toEqual([]);
    expect(agentToolsOf(spec("nope"))).toEqual([]);
    expect(agentToolsOf(spec([{ agent_id: A }]))).toEqual([{ agent_id: A }]);
  });

  it.each([
    "agent tool: agent 'X' is archived; unarchive it or remove the tool",
    "agent tool cycle: A → B → A (this publish would make it a loop)",
    "agent tool name 'x' (agent 'X') is already used by 'y'; set a different name",
    "Value error, duplicate agent tool: 6f0c5e5e",
    "Value error, at most 8 agent tools per agent",
    "agent_tools[0]: whatever",
  ])("owns %s", (message) => {
    expect(isAgentToolsError(message)).toBe(true);
  });

  it("does not claim other messages", () => {
    expect(isAgentToolsError("MCP server 'x' is not registered in this tenant")).toBe(false);
    expect(isAgentToolsError("tools[0]: bad")).toBe(false);
  });
});
