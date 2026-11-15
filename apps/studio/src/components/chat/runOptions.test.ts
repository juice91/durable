import { afterEach, describe, expect, it, vi } from "vitest";
import type { AgentSpec, AgentVersion } from "@/lib/api";
import type { ModelOption } from "@/lib/metrics";
import type { RunEvent, RunEventData } from "@/lib/runs";
import {
  NO_OPTIONS,
  applyUrlState,
  choiceFromModelId,
  classifyTurnError,
  contextToSend,
  effectiveVersionRef,
  hasOutputProcessors,
  loadPlaygroundContext,
  modelPickerOptions,
  optionsFromRun,
  overrideFor,
  parseUrlState,
  resolveModelChoice,
  savePlaygroundContext,
  targetSpec,
  turnBlock,
  turnBody,
  turnLiveText,
  turnModelId,
  turnUsage,
  versionFromSelectValue,
  versionSelectValue,
} from "./runOptions";

const spec = (model: string, extra: object = {}): AgentSpec => {
  const [provider, name] = model.split(":");
  return { spec_version: 1, name: "a", instructions: "", model: { provider, name }, tools: [], ...extra } as AgentSpec;
};
const version = (n: number, s: AgentSpec): AgentVersion => ({
  agent_id: "a", version: n, spec: s, spec_hash: "h", created_at: "", created_by: "",
});
const models: ModelOption[] = [
  { id: "openai:gpt-4.1-mini", provider: "openai", name: "gpt-4.1-mini", wildcard: false, source: "allowlist", description: null },
  { id: "anthropic:*", provider: "anthropic", name: "*", wildcard: true, source: "allowlist", description: null },
  { id: "test:echo", provider: "test", name: "echo", wildcard: false, source: "scripted", description: "Echoes" },
];
const ev = (seq: number, type: RunEvent["type"], data: object): RunEvent => ({
  seq, type, data: data as RunEventData, ts: "",
});

afterEach(() => {
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("turnBody", () => {
  it("leaves null options out", () => {
    expect(turnBody("r", "hi", NO_OPTIONS)).toEqual({ run_id: "r", input: "hi" });
    expect(turnBody("r", "hi", { version: "draft", model: { provider: "test", name: "echo" }, request_context: { a: 1 } })).toEqual({
      run_id: "r", input: "hi", version: "draft", model: { provider: "test", name: "echo" }, request_context: { a: 1 },
    });
  });
});

describe("URL state", () => {
  it("parses valid values and ignores invalid ones", () => {
    expect(parseUrlState("?agent=a&conversation=c&version=3&model=test:echo")).toEqual({
      agent: "a", conversation: "c", version: 3, model: "test:echo", preferPreset: false, openContext: false,
    });
    expect(parseUrlState("?agent=a&context=preset").preferPreset).toBe(true);
    expect(parseUrlState("?agent=a&context=other").preferPreset).toBe(false);
    expect(parseUrlState("?agent=a&context=open")).toMatchObject({ preferPreset: false, openContext: true });
    expect(parseUrlState("?agent=a&context=other").openContext).toBe(false);
    expect(parseUrlState("?version=draft").version).toBe("draft");
    for (const bad of ["0", "-1", "1.5", "abc", ""]) expect(parseUrlState(`?version=${bad}`).version).toBeNull();
    for (const bad of ["noprovider", "openai:*", ":x", "a b:c"]) expect(parseUrlState(`?model=${encodeURIComponent(bad)}`).model).toBeNull();
  });

  it("writes only the given keys and removes nulls", () => {
    const url = new URL("http://x/playground?conversation=c&other=1&model=test:echo");
    applyUrlState(url, { agent: "a", version: "draft", model: null });
    expect(url.searchParams.get("agent")).toBe("a");
    expect(url.searchParams.get("version")).toBe("draft");
    expect(url.searchParams.has("model")).toBe(false);
    expect(url.searchParams.get("conversation")).toBe("c");
    expect(url.searchParams.get("other")).toBe("1");
  });
});

describe("version selection", () => {
  it("round-trips select values", () => {
    for (const ref of [null, "draft", 4] as const) expect(versionFromSelectValue(versionSelectValue(ref))).toBe(ref);
  });

  it("forces the draft without a published version and falls back from an unknown version", () => {
    expect(effectiveVersionRef(3, null, null)).toBe("draft");
    expect(effectiveVersionRef(7, 2, [version(1, spec("test:echo")), version(2, spec("test:echo"))])).toBeNull();
    expect(effectiveVersionRef(7, 2, null)).toBe(7);
    expect(effectiveVersionRef(1, 2, [version(1, spec("test:echo"))])).toBe(1);
  });

  it("finds the target spec", () => {
    const draft = spec("test:draft");
    const versions = [version(1, spec("test:one")), version(2, spec("test:two"))];
    expect(targetSpec("draft", draft, 2, versions)).toBe(draft);
    expect(targetSpec(null, draft, 2, versions)?.model.name).toBe("two");
    expect(targetSpec(1, draft, 2, versions)?.model.name).toBe("one");
    expect(targetSpec(null, draft, 2, null)).toBeNull();
  });

  it("detects output processors", () => {
    expect(hasOutputProcessors(spec("test:a", { output_processors: [{ type: "max_length" }] }))).toBe(true);
    expect(hasOutputProcessors(spec("test:a", { output_processors: [] }))).toBe(false);
    expect(hasOutputProcessors(spec("test:a"))).toBe(false);
    expect(hasOutputProcessors(null)).toBe(false);
  });
});

describe("turnLiveText", () => {
  const plain = [version(1, spec("test:a"))];
  const guarded = [version(1, spec("test:a", { output_processors: [{ type: "max_length" }] }))];
  it("keeps the value recorded at start", () => {
    expect(turnLiveText(true, { draft: false, version: 1 }, null, 1, guarded)).toBe(true);
    expect(turnLiveText(false, { draft: false, version: 1 }, null, 1, plain)).toBe(false);
  });
  it("works it out from the turn's version or draft, failing closed while unknown", () => {
    expect(turnLiveText(null, { draft: false, version: 1 }, null, 1, null)).toBe(false);
    expect(turnLiveText(null, { draft: false, version: 9 }, null, 1, plain)).toBe(false);
    expect(turnLiveText(null, { draft: false, version: 1 }, null, 1, plain)).toBe(true);
    expect(turnLiveText(null, { draft: false, version: 1 }, null, 1, guarded)).toBe(false);
    expect(turnLiveText(null, { draft: true, version: null }, spec("test:a"), 1, null)).toBe(true);
    expect(turnLiveText(null, { draft: true, version: null }, spec("test:a", { output_processors: [{}] }), 1, null)).toBe(false);
    expect(turnLiveText(null, { draft: true, version: null }, null, 1, plain)).toBe(false);
  });
});

describe("model picker", () => {
  it("offers alternatives without the default and wildcards as free text", () => {
    expect(modelPickerOptions(models, "openai:gpt-4.1-mini")).toEqual([
      { value: "anthropic:*", label: "anthropic: other model…", wildcard: true },
      { value: "test:echo", label: "test:echo (Echoes)", wildcard: false },
    ]);
  });

  it("maps a model id onto the picker", () => {
    expect(choiceFromModelId("test:echo", models, null)).toEqual({ choice: { choice: "test:echo", customName: "" }, dropped: false });
    expect(choiceFromModelId("anthropic:claude-x", models, null)).toEqual({
      choice: { choice: "anthropic:*", customName: "claude-x" }, dropped: false,
    });
    expect(choiceFromModelId("openai:gpt-5", models, null).dropped).toBe(true);
    expect(choiceFromModelId("openai:gpt-4.1-mini", models, "openai:gpt-4.1-mini")).toEqual({
      choice: { choice: "", customName: "" }, dropped: false,
    });
  });

  it("resolves a choice or explains what is missing", () => {
    expect(resolveModelChoice({ choice: "", customName: "" })).toEqual({ modelId: null });
    expect(resolveModelChoice({ choice: "test:echo", customName: "" })).toEqual({ modelId: "test:echo" });
    expect(resolveModelChoice({ choice: "anthropic:*", customName: " claude-x " })).toEqual({ modelId: "anthropic:claude-x" });
    expect(resolveModelChoice({ choice: "anthropic:*", customName: "" })).toEqual({ error: "Enter a model name for anthropic" });
    expect(resolveModelChoice({ choice: "anthropic:*", customName: "a b" })).toHaveProperty("error");
  });

  it("sends an override only when it differs from the version's model", () => {
    expect(overrideFor(null, "test:echo")).toBeNull();
    expect(overrideFor("test:echo", "test:echo")).toBeNull();
    expect(overrideFor("litellm:org/model:v1", "test:echo")).toEqual({ provider: "litellm", name: "org/model:v1" });
  });
});

describe("context", () => {
  it("drops empty values", () => {
    expect(contextToSend({ a: "", b: null, c: undefined })).toBeNull();
    expect(contextToSend({ a: "x", b: 0, c: false })).toEqual({ a: "x", b: 0, c: false });
  });

  it("remembers the playground context per agent in sessionStorage and survives unavailable storage", () => {
    expect(savePlaygroundContext("agent-1", { tier: "gold" })).toBe(true);
    expect(loadPlaygroundContext("agent-1")).toEqual({ tier: "gold" });
    expect(loadPlaygroundContext("agent-2")).toBeNull();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(1);
    window.sessionStorage.setItem(Object.keys(window.sessionStorage)[0], "not json");
    expect(loadPlaygroundContext("agent-1")).toBeNull();
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("quota");
    });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    expect(savePlaygroundContext("agent-1", { tier: "gold" })).toBe(false);
    expect(loadPlaygroundContext("agent-1")).toBeNull();
  });
});

describe("classifyTurnError", () => {
  it("routes a 422 list by location", () => {
    const errors = classifyTurnError(422, [
      { loc: ["body", "request_context", "tier"], msg: "bad tier", type: "x" },
      { loc: ["body", "request_context"], msg: "too large", type: "x" },
      { loc: ["body", "model", "name"], msg: "bad name", type: "x" },
      { loc: ["body", "version"], msg: "bad version", type: "x" },
      { loc: ["body", "input"], msg: "too long", type: "x" },
    ], NO_OPTIONS);
    expect(errors.context).toEqual({ banner: "too large", fieldErrors: { tier: "bad tier" } });
    expect(errors.model).toBe("bad name");
    expect(errors.version).toBe("bad version");
    expect(errors.general).toBe("input: too long");
  });

  it("routes a 422 string: the override's refusal, draft refusals, then general", () => {
    const override = { ...NO_OPTIONS, model: { provider: "x", name: "y" } };
    expect(classifyTurnError(422, "model 'x:y' is not allowed on this platform", override).model).toMatch(/not allowed/);
    // Without an override, or naming another model, it is not the picker's problem.
    expect(classifyTurnError(422, "model 'x:y' is not allowed on this platform", NO_OPTIONS)).toMatchObject({
      model: null, general: "model 'x:y' is not allowed on this platform",
    });
    expect(classifyTurnError(422, "model 'a:b' is not allowed on this platform", { ...override, version: "draft" })).toMatchObject({
      model: null, version: "The draft cannot run: model 'a:b' is not allowed on this platform",
    });
    expect(classifyTurnError(422, "tools: unknown tool", { ...NO_OPTIONS, version: "draft" }).version).toBe(
      "The draft cannot run: tools: unknown tool",
    );
    expect(classifyTurnError(422, "something", NO_OPTIONS).general).toBe("something");
    expect(classifyTurnError(409, null, NO_OPTIONS).general).toBe("The request failed (HTTP 409)");
  });
});

describe("turn badge data", () => {
  const usageEvent = (seq: number, cost: string | null, tokens: [number, number]) =>
    ev(seq, "model_message", { is_final: true, model_id: "test:echo", usage: { input_tokens: tokens[0], output_tokens: tokens[1] }, cost_usd: cost });

  it("sums the chat model's requests only, before the run row has usage (moderation apart)", () => {
    const events = [
      ev(0, "run_started", {}),
      usageEvent(1, "0.001", [10, 2]),
      ev(2, "processor_result", { usage: { input_tokens: 5, output_tokens: 1 }, cost_usd: "0.0005" }),
      ev(3, "model_message", { text: "no usage" }),
    ];
    const usage = turnUsage(events);
    expect(usage).toMatchObject({ recorded: true, inputTokens: 10, outputTokens: 2 });
    expect(usage.costUsd).toBeCloseTo(0.001);
  });

  it("prefers the run's chat_* fields over its combined totals (follow-up 3)", () => {
    const row = { input_tokens: 100, output_tokens: 50, cost_usd: "0.5", chat_input_tokens: 60, chat_output_tokens: 20, chat_cost_usd: "0.25" };
    expect(turnUsage([usageEvent(1, "1", [1, 1])], row)).toEqual({ recorded: true, inputTokens: 60, outputTokens: 20, costUsd: 0.25, measured: true });
    expect(turnUsage([], { ...row, chat_cost_usd: null }).costUsd).toBeNull();
    // Absent (older API) or null (nothing recorded yet): the totals, as before.
    expect(turnUsage([], { input_tokens: 100, output_tokens: 50, cost_usd: "0.5" }).inputTokens).toBe(100);
    expect(turnUsage([], { ...row, chat_input_tokens: null, chat_output_tokens: null }).inputTokens).toBe(100);
  });

  it("reports no price when any request is unpriced and nothing when no request was made", () => {
    expect(turnUsage([usageEvent(1, "0.001", [1, 1]), usageEvent(2, null, [1, 1])]).costUsd).toBeNull();
    expect(turnUsage([ev(0, "run_started", {})])).toEqual({
      recorded: false, inputTokens: 0, outputTokens: 0, costUsd: null, measured: true,
    });
  });

  it("flags usage the provider did not report", () => {
    const unmeasured = ev(1, "model_message", { usage: { input_tokens: 0, output_tokens: 0, measured: false }, cost_usd: "0" });
    expect(turnUsage([usageEvent(0, "0", [1, 1]), unmeasured]).measured).toBe(false);
    expect(turnUsage([unmeasured], { input_tokens: 0, output_tokens: 0, cost_usd: "0" }).measured).toBe(false);
    expect(turnUsage([usageEvent(0, "0", [1, 1])]).measured).toBe(true);
  });

  it("prefers the run's recorded totals", () => {
    expect(turnUsage([usageEvent(1, "1", [1, 1])], { input_tokens: 100, output_tokens: 50, cost_usd: "0.5" })).toEqual({
      recorded: true, inputTokens: 100, outputTokens: 50, costUsd: 0.5, measured: true,
    });
    expect(turnUsage([], { input_tokens: 100, output_tokens: 50, cost_usd: null }).costUsd).toBeNull();
  });

  it("names the model and the block", () => {
    expect(turnModelId([usageEvent(1, null, [1, 1])])).toBe("test:echo");
    expect(turnModelId([], { model_id: "openai:x" })).toBe("openai:x");
    expect(turnModelId([])).toBeNull();
    expect(turnBlock([ev(1, "run_failed", { error_code: "output_blocked", error: "blocked" })])).toEqual({
      code: "output_blocked", message: "blocked",
    });
    expect(turnBlock([ev(1, "run_failed", { error_code: "model_error", error: "x" })])).toBeNull();
  });

  it("rebuilds a recorded turn's options for an idempotent re-post", () => {
    expect(optionsFromRun({ version: 2, model_id: "test:echo", model_overridden: false, request_context: {} })).toEqual(NO_OPTIONS_WITH(2));
    expect(optionsFromRun({ version: null, draft: true, model_id: "test:echo", model_overridden: true, request_context: { a: 1 } })).toEqual({
      version: "draft", model: { provider: "test", name: "echo" }, request_context: { a: 1 },
    });
  });
});

function NO_OPTIONS_WITH(version: number) {
  return { ...NO_OPTIONS, version };
}
