import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentSpec, AgentVersion } from "@/lib/api";
import { agentTabOf, routes } from "@/lib/routes";
import {
  AGENTS_VIEW_KEY,
  LAST_AGENT_KEY,
  agentLayoutKey,
  agentPurpose,
  chatUrlFor,
  configCounts,
  effectiveConfig,
  instructionSegments,
  memorySummary,
  playgroundFallbackTarget,
  readAgentsView,
  readLastAgent,
  writeAgentsView,
  writeLastAgent,
} from "./agentPageModel";

const spec = (name: string, extra: object = {}): AgentSpec =>
  ({ spec_version: 1, name, instructions: "Help", model: { provider: "openai", name: "gpt-4.1-mini" }, tools: [], ...extra }) as AgentSpec;
const version = (n: number, s: AgentSpec, restored_from: number | null = null): AgentVersion => ({
  agent_id: "a1",
  version: n,
  spec: s,
  spec_hash: `h${n}`,
  created_at: "2026-11-14T00:00:00Z",
  created_by: "dev",
  restored_from,
});

afterEach(() => {
  window.localStorage.clear();
  vi.restoreAllMocks();
});

describe("agent routes", () => {
  it("builds the chat URL with the conversation in the path and options in a fixed order", () => {
    expect(routes.agentChat("a1")).toBe("/agents/a1/chat");
    expect(routes.agentChat("a1", { conversation: "c1" })).toBe("/agents/a1/chat/c1");
    expect(routes.agentChat("a:1", { conversation: "c/1", context: "preset", model: "openai:gpt-4.1", version: "draft" })).toBe(
      "/agents/a%3A1/chat/c%2F1?version=draft&model=openai%3Agpt-4.1&context=preset",
    );
    expect(routes.agentChat("a1", { conversation: null, version: null, model: "" })).toBe("/agents/a1/chat");
    expect(routes.agentEditor("a1", { step: "tools", compare: "3..draft" })).toBe("/agents/a1/editor?step=tools&compare=3..draft");
  });

  it("keeps the tab when switching agent, and reads the tab from a path", () => {
    expect(routes.agentTab("b", "chat")).toBe(routes.agentChat("b"));
    expect(routes.agentTab("b", "traces")).toBe("/agents/b/traces");
    expect(routes.agentTab("b", "editor")).toBe("/agents/b/editor");
    expect(agentTabOf("/agents/a1/chat/c1")).toBe("chat");
    expect(agentTabOf("/agents/a1/traces")).toBe("traces");
    expect(agentTabOf("/agents/a1/editor")).toBe("editor");
    expect(agentTabOf("/agents/a1")).toBeNull();
    expect(agentTabOf("/agents/new")).toBeNull();
    expect(agentTabOf(null)).toBeNull();
  });
});

describe("effectiveConfig: what the Config panel shows", () => {
  const agent = { draft_spec: spec("Draft"), latest_version: 2 };
  const versions = [version(2, spec("V2"), 1), version(1, spec("V1"))];

  it("defaults to the latest published version", () => {
    const config = effectiveConfig(agent, versions);
    expect(config.spec?.name).toBe("V2");
    expect(config.versionLabel).toBe("v2 (latest)");
    expect(config.restoredFrom).toBe(1);
    expect(config.isDraft).toBe(false);
  });

  it("shows the draft for ?version=draft and for an agent with nothing published", () => {
    expect(effectiveConfig(agent, versions, "draft")).toMatchObject({ isDraft: true, versionLabel: "Draft", spec: { name: "Draft" } });
    expect(effectiveConfig({ draft_spec: spec("Only"), latest_version: null }, [], 3)).toMatchObject({ isDraft: true, spec: { name: "Only" } });
  });

  it("shows an older version, and falls back to the latest for one the agent does not have", () => {
    expect(effectiveConfig(agent, versions, 1)).toMatchObject({ versionLabel: "v1", spec: { name: "V1" }, restoredFrom: null });
    expect(effectiveConfig(agent, versions, 9)).toMatchObject({ versionLabel: "v2 (latest)", spec: { name: "V2" } });
  });

  it("has no spec while the versions load (except the draft)", () => {
    expect(effectiveConfig(agent, null).spec).toBeNull();
    expect(effectiveConfig(agent, null, "draft").spec?.name).toBe("Draft");
  });

  it("counts tools (built-in and MCP), context fields and processors", () => {
    const full = spec("x", {
      tools: ["a", "b"],
      mcp_tools: [{ server: "s", tool: "t" }],
      context_fields: [{ name: "plan", type: "string" }],
      input_processors: [{ type: "max_length", config: {} }],
      output_processors: [{ type: "regex_blocklist", config: {} }],
      step_processors: [{ type: "pii_redact", config: {} }],
      stream_processors: [{ type: "pii_redact", config: {} }],
    });
    expect(configCounts(full)).toEqual({ tools: 3, contextFields: 1, processors: 4 });
    expect(configCounts(null)).toEqual({ tools: 0, contextFields: 0, processors: 0 });
  });
});

describe("memorySummary", () => {
  it("reads four on/off badges from the spec's memory", () => {
    const badges = memorySummary(spec("m", { memory: { history: { last_turns: 8 }, semantic_recall: { embedding_model: "openai:x" } } }));
    expect(badges.map((b) => [b.id, b.label, b.on])).toEqual([
      ["history", "History 8 turns", true],
      ["working", "Working memory", false],
      ["observational", "Observational", false],
      ["semantic", "Semantic recall", true],
    ]);
  });

  it("shows plain history and nothing else for an agent without memory", () => {
    expect(memorySummary(spec("m")).filter((b) => b.on).map((b) => b.label)).toEqual(["History"]);
    expect(memorySummary(null)).toHaveLength(4);
  });
});

describe("instructionSegments", () => {
  it("splits out {{context.x}} placeholders, spaces allowed", () => {
    expect(instructionSegments("Hi {{context.plan}} and {{ context.user_tier }}.")).toEqual([
      { text: "Hi ", placeholder: false },
      { text: "{{context.plan}}", placeholder: true },
      { text: " and ", placeholder: false },
      { text: "{{ context.user_tier }}", placeholder: true },
      { text: ".", placeholder: false },
    ]);
    expect(instructionSegments("{{other.x}}")).toEqual([{ text: "{{other.x}}", placeholder: false }]);
  });
});

describe("agentPurpose", () => {
  it("uses a description, else the first non-empty line of the instructions, else a fallback", () => {
    expect(agentPurpose(spec("x", { description: " Answers billing " }))).toBe("Answers billing");
    expect(agentPurpose(spec("x", { instructions: "\n  First line\nSecond" }))).toBe("First line");
    expect(agentPurpose(spec("x", { instructions: "" }))).toBe("No instructions");
    expect(agentPurpose(undefined)).toBe("No instructions");
  });
});

describe("the bare /playground fallback", () => {
  const agents = [{ id: "a1" }, { id: "a2" }];
  it("prefers the last-used agent, then the first, then the agents list", () => {
    expect(playgroundFallbackTarget("a2", agents)).toBe(routes.agentChat("a2"));
    expect(playgroundFallbackTarget("gone", agents)).toBe(routes.agentChat("a1"));
    expect(playgroundFallbackTarget(null, agents, { version: 2, model: "test:echo" })).toBe(routes.agentChat("a1", { version: 2, model: "test:echo" }));
    expect(playgroundFallbackTarget("a2", [])).toBe(routes.agents());
  });
});

describe("chatUrlFor", () => {
  it("puts the conversation in the path and the options in the query, keeping other parameters", () => {
    expect(chatUrlFor("http://x/agents/a1/chat?context=open#h", "a1", { conversation: "c1", version: 2, model: "test:echo" })).toBe(
      "/agents/a1/chat/c1?version=2&model=test%3Aecho&context=open#h",
    );
    expect(chatUrlFor("http://x/agents/a1/chat/c1?version=2&agent=a1&conversation=c1", "a1", { conversation: null, version: null, model: null })).toBe(
      "/agents/a1/chat",
    );
  });
});

describe("browser storage", () => {
  it("remembers the last agent and the list view under their keys", () => {
    writeLastAgent("a7");
    expect(window.localStorage.getItem(LAST_AGENT_KEY)).toBe("a7");
    expect(readLastAgent()).toBe("a7");
    expect(readAgentsView()).toBe("list");
    writeAgentsView("compact");
    expect(window.localStorage.getItem(AGENTS_VIEW_KEY)).toBe("compact");
    expect(readAgentsView()).toBe("compact");
    expect(agentLayoutKey("a1")).toBe("studio.agent-layout.v1.a1");
  });

  it("survives storage that throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    expect(() => writeLastAgent("a1")).not.toThrow();
    expect(readLastAgent()).toBeNull();
    expect(readAgentsView()).toBe("list");
  });
});
