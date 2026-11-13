import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  RequestContextValidationError,
  MIGRATED_PRESET_NAME,
  PRESETS_PREFIX,
  clearSavedContext,
  deletePreset,
  getRequestContext,
  latestPreset,
  listPresets,
  loadSavedContext,
  mapContextErrors,
  normalizeContext,
  parseContextJson,
  presetNameError,
  presetsKey,
  saveContext,
  savePreset,
  savedContextKey,
  validateRequestContext,
} from "@/lib/requestContext";
import { legacyTarget, routes } from "@/lib/routes";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("requestContext API client", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockReset();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("gets the fields of the latest version without a version query, and of the draft with one", async () => {
    const info = { agent_id: "a1", version: 2, draft: false, fields: [], schema: { type: "object", properties: {} } };
    fetchMock.mockResolvedValue(jsonResponse(200, info));
    await expect(getRequestContext("a1")).resolves.toEqual(info);
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/api\/v1\/agents\/a1\/request-context$/);
    fetchMock.mockResolvedValue(jsonResponse(200, { ...info, version: null, draft: true }));
    await getRequestContext("a1", "draft");
    expect(fetchMock.mock.calls[1][0]).toMatch(/\/request-context\?version=draft$/);
  });

  it("sends version and request_context to validate and returns the accepted context", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { valid: true, request_context: { tier: "gold" } }));
    await expect(validateRequestContext("a1", { tier: "gold" }, 3)).resolves.toEqual({ tier: "gold" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/agents\/a1\/request-context\/validate$/);
    expect(JSON.parse(init.body)).toEqual({ request_context: { tier: "gold" }, version: 3 });
  });

  it("omits version for the latest", async () => {
    fetchMock.mockResolvedValue(jsonResponse(200, { valid: true, request_context: {} }));
    await validateRequestContext("a1", {});
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ request_context: {} });
  });

  it("maps a 422 to field errors", async () => {
    fetchMock.mockResolvedValue(jsonResponse(422, {
      detail: [
        { loc: ["body", "request_context", "seats"], msg: "must be an integer", type: "int_type" },
        { loc: ["body", "request_context", "extra"], msg: "is not a context field of this agent", type: "extra_forbidden" },
      ],
    }));
    const error = await validateRequestContext("a1", { seats: "x", extra: 1 }).catch((reason: unknown) => reason);
    expect(error).toBeInstanceOf(RequestContextValidationError);
    expect((error as RequestContextValidationError).fieldErrors).toEqual({
      seats: "must be an integer",
      extra: "is not a context field of this agent",
    });
    expect((error as RequestContextValidationError).banner).toBeNull();
  });

  it("passes other errors through as ApiError", async () => {
    fetchMock.mockResolvedValue(jsonResponse(409, { detail: "Agent has no published version" }));
    await expect(validateRequestContext("a1", {})).rejects.toMatchObject({ name: "ApiError", status: 409, message: "Agent has no published version" });
  });
});

describe("mapContextErrors", () => {
  it("puts whole-context errors in the banner and joins repeated field messages", () => {
    const error = mapContextErrors(422, [
      { loc: ["body", "request_context"], msg: "request context must be at most 8192 bytes", type: "too_large" },
      { loc: ["body", "request_context", "a"], msg: "first", type: "x" },
      { loc: ["body", "request_context", "a"], msg: "second", type: "x" },
      { loc: ["body", "version"], msg: "bad version", type: "x" },
    ]);
    expect(error.banner).toBe("request context must be at most 8192 bytes; body.version: bad version");
    expect(error.fieldErrors).toEqual({ a: "first; second" });
  });

  it("uses a string detail as the banner", () => {
    expect(mapContextErrors(422, "The draft is not a valid agent spec").banner).toBe("The draft is not a valid agent spec");
  });
});

describe("normalizeContext and parseContextJson", () => {
  it("drops undefined, null and empty strings but keeps false and 0", () => {
    expect(normalizeContext({ a: "", b: null, c: undefined, d: false, e: 0, f: "x" })).toEqual({ d: false, e: 0, f: "x" });
  });

  it("keeps an explicit empty string when asked (JSON mode)", () => {
    expect(normalizeContext({ a: "", b: null }, { keepEmptyStrings: true })).toEqual({ a: "" });
  });

  it("accepts only JSON objects", () => {
    expect(parseContextJson('{"a": 1}')).toEqual({ value: { a: 1 } });
    expect(parseContextJson("[1]")).toEqual({ error: "The request context must be a JSON object" });
    expect(parseContextJson("{")).toEqual({ error: "Enter valid JSON" });
  });
});

describe("saved context storage", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it("saves, loads and clears per agent", () => {
    expect(loadSavedContext("a1")).toBeNull();
    const saved = saveContext("a1", { tier: "gold" }, "draft");
    expect(saved).toMatchObject({ value: { tier: "gold" }, version: "draft" });
    expect(loadSavedContext("a1")).toMatchObject({ value: { tier: "gold" }, version: "draft" });
    expect(loadSavedContext("a2")).toBeNull();
    expect(clearSavedContext("a1")).toBe(true);
    expect(loadSavedContext("a1")).toBeNull();
  });

  it("ignores corrupt entries and invalid versions", () => {
    window.localStorage.setItem(savedContextKey("a1"), "{not json");
    expect(loadSavedContext("a1")).toBeNull();
    window.localStorage.setItem(savedContextKey("a1"), JSON.stringify({ value: [1] }));
    expect(loadSavedContext("a1")).toBeNull();
    window.localStorage.setItem(savedContextKey("a1"), JSON.stringify({ value: { a: 1 }, version: -2 }));
    expect(loadSavedContext("a1")).toMatchObject({ value: { a: 1 }, version: null });
  });

  it("reports storage that throws instead of throwing", () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("QuotaExceededError"); });
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("SecurityError"); });
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => { throw new Error("SecurityError"); });
    expect(saveContext("a1", { a: 1 }, null)).toBeNull();
    expect(loadSavedContext("a1")).toBeNull();
    expect(clearSavedContext("a1")).toBe(false);
  });

  it("scopes the key by a fingerprint of the credential, never the credential itself", () => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer tenant-a");
    const keyA = savedContextKey("a1");
    expect(keyA).toMatch(/^eap\.requestContext\.v1:c[0-9a-f]{16}:a1$/);
    expect(keyA).not.toContain("tenant-a");
    saveContext("a1", { tier: "gold" }, null);
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer tenant-b");
    expect(savedContextKey("a1")).not.toBe(keyA);
    expect(loadSavedContext("a1")).toBeNull();
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer tenant-a");
    expect(loadSavedContext("a1")).toMatchObject({ value: { tier: "gold" } });
    vi.unstubAllEnvs();
  });

  it("the old \"Use in playground\" link lands in the agent's chat with the preset flag", () => {
    // SL.10 removed playgroundHref with the Request Context page; its links (`/playground?…&context=preset`)
    // still land in the chat, where `context=preset` applies the newest preset. One literal URL per case.
    const land = (href: string) => {
      const url = new URL(href, "http://studio.test");
      return legacyTarget(url.pathname, url.searchParams);
    };
    expect(land(routes.playground({ agent: "a 1" }))).toBe("/agents/a%201/chat");
    expect(land(routes.playground({ agent: "a1", version: 3, context: "preset" }))).toBe("/agents/a1/chat?version=3&context=preset");
    expect(land(routes.playground({ agent: "a1", version: "draft" }))).toBe("/agents/a1/chat?version=draft");
    // Without an agent there is no server target (the client redirect picks one).
    expect(land(routes.playground({ version: 3, context: "preset" }))).toBeNull();
  });
});

describe("named presets (SL.10)", () => {
  beforeEach(() => window.localStorage.clear());
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllEnvs();
  });

  it("saves, lists by name, replaces by name and deletes per agent", () => {
    expect(listPresets("a1")).toEqual([]);
    expect(savePreset("a1", " gold ", { tier: "gold" }, 2)).toMatchObject({ name: "gold", value: { tier: "gold" }, version: 2 });
    savePreset("a1", "Bronze", { tier: "bronze" }, "draft");
    savePreset("a2", "other", { tier: "x" }, null);
    expect(listPresets("a1").map((preset) => preset.name)).toEqual(["Bronze", "gold"]);
    savePreset("a1", "gold", { tier: "gold", seats: 3 }, null);
    expect(listPresets("a1")).toHaveLength(2);
    expect(listPresets("a1").find((preset) => preset.name === "gold")).toMatchObject({ value: { tier: "gold", seats: 3 }, version: null });
    expect(deletePreset("a1", "Bronze")).toBe(true);
    expect(listPresets("a1").map((preset) => preset.name)).toEqual(["gold"]);
    expect(listPresets("a2").map((preset) => preset.name)).toEqual(["other"]);
  });

  it("stores one credential-scoped studio.* key per agent, never the credential", () => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer tenant-a");
    savePreset("a1", "p", { a: 1 }, null);
    expect(Object.keys(window.localStorage)).toEqual([presetsKey("a1")]);
    expect(presetsKey("a1")).toMatch(/^studio\.requestContextPresets\.v1\.c[0-9a-f]{16}:a1$/);
    expect(presetsKey("a1").startsWith(PRESETS_PREFIX)).toBe(true);
    expect(window.localStorage.getItem(presetsKey("a1"))).not.toContain("tenant-a");
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer tenant-b");
    expect(listPresets("a1")).toEqual([]);
  });

  it("latestPreset is the most recently saved one", () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date("2026-11-14T10:00:00Z"));
      savePreset("a1", "b", { v: 1 }, null);
      vi.setSystemTime(new Date("2026-11-14T11:00:00Z"));
      savePreset("a1", "a", { v: 2 }, null);
      expect(latestPreset("a1")?.name).toBe("a");
      vi.setSystemTime(new Date("2026-11-14T12:00:00Z"));
      savePreset("a1", "b", { v: 3 }, null);
      expect(latestPreset("a1")).toMatchObject({ name: "b", value: { v: 3 } });
    } finally {
      vi.useRealTimers();
    }
    expect(latestPreset("none")).toBeNull();
  });

  it("migrates the old saved value into a preset named Saved, once, keeping the old key", () => {
    saveContext("a1", { tier: "silver" }, 1);
    const presets = listPresets("a1");
    expect(presets).toEqual([expect.objectContaining({ name: MIGRATED_PRESET_NAME, value: { tier: "silver" }, version: 1 })]);
    expect(JSON.parse(window.localStorage.getItem(presetsKey("a1")) ?? "{}")).toMatchObject({ migrated: true });
    // Idempotent: reading again imports nothing more.
    expect(listPresets("a1")).toHaveLength(1);
    // Deleting "Saved" sticks: the old key is still there but is never imported again.
    expect(deletePreset("a1", MIGRATED_PRESET_NAME)).toBe(true);
    expect(loadSavedContext("a1")).not.toBeNull();
    expect(listPresets("a1")).toEqual([]);
    expect(latestPreset("a1")).toBeNull();
  });

  it("migration merges into presets saved before it and writes nothing for an agent without the old key", () => {
    expect(listPresets("fresh")).toEqual([]);
    expect(window.localStorage.getItem(presetsKey("fresh"))).toBeNull();
    window.localStorage.setItem(presetsKey("a1"), JSON.stringify({ presets: [{ name: "mine", value: { a: 1 }, version: null, saved_at: "x" }] }));
    saveContext("a1", { tier: "gold" }, null);
    expect(listPresets("a1").map((preset) => preset.name)).toEqual(["mine", "Saved"]);
    expect(listPresets("a1").map((preset) => preset.name)).toEqual(["mine", "Saved"]);
  });

  it("a failed migration write is retried on the next read, without duplicates", () => {
    saveContext("a1", { tier: "gold" }, null);
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementationOnce(() => {
      throw new Error("QuotaExceededError");
    });
    expect(listPresets("a1").map((preset) => preset.name)).toEqual(["Saved"]);
    expect(window.localStorage.getItem(presetsKey("a1"))).toBeNull();
    setItem.mockRestore();
    expect(listPresets("a1").map((preset) => preset.name)).toEqual(["Saved"]);
    expect(JSON.parse(window.localStorage.getItem(presetsKey("a1")) ?? "{}").presets).toHaveLength(1);
  });

  it("ignores corrupt records and entries, and reports storage that throws", () => {
    window.localStorage.setItem(presetsKey("a1"), "{nope");
    expect(listPresets("a1")).toEqual([]);
    window.localStorage.setItem(
      presetsKey("a1"),
      JSON.stringify({ migrated: true, presets: [{ name: "", value: {} }, { name: "ok", value: { a: 1 }, version: -1 }, { name: "ok", value: { b: 2 } }, { name: "bad", value: [1] }] }),
    );
    expect(listPresets("a1")).toEqual([{ name: "ok", value: { a: 1 }, version: null, saved_at: "" }]);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    expect(savePreset("a1", "new", { a: 1 }, null)).toBeNull();
    expect(deletePreset("a1", "ok")).toBe(false);
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    expect(listPresets("a1")).toEqual([]);
  });

  it("validates preset names", () => {
    expect(presetNameError("  ")).toBe("Enter a name");
    expect(presetNameError("x".repeat(61))).toMatch(/at most 60/);
    expect(presetNameError(" ok ")).toBeNull();
    expect(savePreset("a1", " ", { a: 1 }, null)).toBeNull();
    expect(listPresets("a1")).toEqual([]);
  });
});
