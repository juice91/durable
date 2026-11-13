import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  RequestContextValidationError,
  clearSavedContext,
  getRequestContext,
  loadSavedContext,
  mapContextErrors,
  normalizeContext,
  parseContextJson,
  playgroundHref,
  saveContext,
  savedContextKey,
  validateRequestContext,
} from "@/lib/requestContext";
import { routes } from "@/lib/routes";

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

  it("builds the playground link", () => {
    // One literal URL per helper, so a builder bug cannot hide behind builder-vs-builder equality.
    expect(playgroundHref("a 1")).toBe("/playground?agent=a+1");
    expect(playgroundHref("a1", 3, true)).toBe(routes.playground({ agent: "a1", version: 3, context: "preset" }));
    expect(playgroundHref("a1", "draft")).toBe(routes.playground({ agent: "a1", version: "draft" }));
    // An empty agent id gives the plain route, without a version or preset flag.
    expect(playgroundHref("", 3, true)).toBe("/playground");
  });
});
