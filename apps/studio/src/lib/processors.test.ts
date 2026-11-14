import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Agent } from "./api";
import {
  defaultConfig,
  describeSchemaType,
  describeValidationDetail,
  formatMatchCounts,
  isModerationRefusal,
  loadAgentProcessorSpecs,
  processors,
  processorUsage,
  resultsQuery,
} from "./processors";

describe("resultsQuery", () => {
  it("leaves out empty filters and keeps the API's parameter names", () => {
    expect(resultsQuery({})).toBe("");
    expect(resultsQuery({ agent_id: "", type: "", action: "block", phase: "input", run_id: "  ", limit: 50, before: null })).toBe(
      "action=block&phase=input&limit=50",
    );
    const query = new URLSearchParams(resultsQuery({ type: "pii_redact", from: "2026-11-13T10:00:00.000Z", to: "2026-11-14T10:00:00.000Z", before: "abc" }));
    expect(query.get("type")).toBe("pii_redact");
    expect(query.get("from")).toBe("2026-11-13T10:00:00.000Z");
    expect(query.get("to")).toBe("2026-11-14T10:00:00.000Z");
    expect(query.get("before")).toBe("abc");
  });
});

describe("helpers", () => {
  it("formats match counts without any matched text", () => {
    expect(formatMatchCounts({ email: 2, phone: 1 })).toBe("email ×2, phone ×1");
    expect(formatMatchCounts({ email: 0 })).toBe("No matches");
    expect(formatMatchCounts(undefined)).toBe("No matches");
    expect(formatMatchCounts({ email: "2", phone: 1 })).toBe("phone ×1");
    expect(formatMatchCounts(["x"])).toBe("No matches");
    expect(formatMatchCounts({}, "")).toBe("");
  });

  it("builds a default config from the schema defaults (copied, not shared)", () => {
    const kinds = ["email", "phone"];
    const config = defaultConfig({ type: "object", properties: { kinds: { type: "array", default: kinds }, action: { type: "string", default: "redact" }, max_chars: { type: "integer" } } });
    expect(config).toEqual({ kinds: ["email", "phone"], action: "redact" });
    expect(config.kinds).not.toBe(kinds);
  });

  it("describes schema types", () => {
    expect(describeSchemaType({ type: "string", enum: ["block", "redact"] })).toBe("one of: block, redact");
    expect(describeSchemaType({ type: "integer", minimum: 1, maximum: 10 })).toBe("integer (≥ 1, ≤ 10)");
    expect(describeSchemaType({ type: "array", items: { type: "string" }, minItems: 1, maxItems: 20 })).toBe("list of string (at least 1, at most 20)");
    expect(describeSchemaType({ type: "object", properties: { provider: {}, name: {} } })).toBe("object { provider, name }");
  });

  it("recognises the moderation refusal", () => {
    expect(isModerationRefusal("moderation calls a model and cannot run in the test console; test it in the playground")).toBe(true);
    expect(isModerationRefusal("Moderation calls a model, so it cannot run in the test console. Test it in an agent's chat.")).toBe(true);
    expect(isModerationRefusal("text is larger than 32768 bytes (32 KiB)")).toBe(false);
  });

});

describe("describeValidationDetail", () => {
  it("keeps each 422 entry's field location without the body prefix", () => {
    expect(describeValidationDetail([
      { loc: ["body", "processors", 0, "config", "max_chars"], msg: "Input should be greater than or equal to 1" },
      { loc: ["body", "text"], msg: "Field required" },
    ])).toBe("processors.0.config.max_chars: Input should be greater than or equal to 1; text: Field required");
    expect(describeValidationDetail([{ loc: ["query", "run_id"], msg: "bad uuid" }])).toBe("run_id: bad uuid");
    expect(describeValidationDetail("text is larger than 32768 bytes (32 KiB)")).toBe("text is larger than 32768 bytes (32 KiB)");
    expect(describeValidationDetail(undefined)).toBeNull();
  });
});

describe("processorUsage", () => {
  const agent = (id: string, name: string, latest: number | null) => ({ id, name, latest_version: latest });

  it("lists an agent whose latest version could not be read as unknown under every type", () => {
    const usage = processorUsage([{ agent: agent("x", "Broken", 4), draft: { input_processors: [{ type: "pii_redact", config: {} }] }, latest: null, failed: true }]);
    for (const list of Object.values(usage)) {
      expect(list).toEqual([{ agent_id: "x", agent_name: "Broken", version: 4, phases: [], unknown: true }]);
    }
  });

  it("lists agents by latest version, falling back to draft-only usage", () => {
    const usage = processorUsage([
      {
        agent: agent("b", "Beta", 3),
        draft: null,
        latest: { input_processors: [{ type: "pii_redact", config: {} }], output_processors: [{ type: "pii_redact", config: {} }, { type: "moderation", config: {} }] },
      },
      { agent: agent("a", "Alpha", null), draft: { input_processors: [{ type: "pii_redact", config: {} }] }, latest: null },
      { agent: agent("c", "Gamma", 1), draft: { output_processors: [{ type: "max_length", config: {} }] }, latest: {} },
    ]);
    expect(usage.pii_redact).toEqual([
      { agent_id: "a", agent_name: "Alpha", version: null, phases: ["input"] },
      { agent_id: "b", agent_name: "Beta", version: 3, phases: ["input", "output"] },
    ]);
    expect(usage.moderation).toEqual([{ agent_id: "b", agent_name: "Beta", version: 3, phases: ["output"] }]);
    expect(usage.max_length).toEqual([{ agent_id: "c", agent_name: "Gamma", version: null, phases: ["output"] }]);
    expect(usage.regex_blocklist).toEqual([]);
  });
});

describe("API client", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "dev-token");
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("posts the processor list and sample to the test console", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ text: "x", blocked: false, results: [] }), { status: 200 }));
    await processors.test([{ type: "max_length", config: { max_chars: 5 } }], "hello world");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/api\/v1\/processors\/test$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual({ processors: [{ type: "max_length", config: { max_chars: 5 } }], text: "hello world" });
  });

  it("keeps the field location of a 422 validation error", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: [{ loc: ["body", "processors", 0, "config", "max_chars"], msg: "Input should be greater than or equal to 1" }] }), { status: 422 }));
    await expect(processors.test([{ type: "max_length", config: { max_chars: 0 } }], "x")).rejects.toMatchObject({
      status: 422,
      message: "processors.0.config.max_chars: Input should be greater than or equal to 1",
    });
  });

  it("surfaces the moderation 422 detail as the error message", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: "moderation calls a model and cannot run in the test console; test it in the playground" }), { status: 422 }));
    await expect(processors.test([{ type: "moderation", config: {} }], "x")).rejects.toMatchObject({ status: 422, message: expect.stringMatching(/test it in the playground/) });
  });

  it("requests results with filters", async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ items: [], next_before: null }), { status: 200 }));
    await processors.results({ action: "block", limit: 50 });
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/api\/v1\/processor-results\?action=block&limit=50$/);
  });

  it("loads the latest version spec of published agents only, tolerating failures", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/agents/p1/versions/2")) return new Response(JSON.stringify({ spec: { input_processors: [{ type: "pii_redact", config: {} }] } }), { status: 200 });
      return new Response(JSON.stringify({ detail: "Agent not found" }), { status: 404 });
    });
    const agents = [
      { id: "p1", name: "One", latest_version: 2, draft_spec: {} },
      { id: "p2", name: "Two", latest_version: 1, draft_spec: {} },
      { id: "d1", name: "Draft", latest_version: null, draft_spec: { output_processors: [] } },
    ] as unknown as Agent[];
    const specs = await loadAgentProcessorSpecs(agents);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(specs[0].latest).toEqual({ input_processors: [{ type: "pii_redact", config: {} }] });
    expect(specs[1]).toMatchObject({ latest: null, failed: true });
    expect(specs[2]).toMatchObject({ latest: null, draft: { output_processors: [] } });
    expect(specs[2].failed).toBeUndefined();
  });
});
