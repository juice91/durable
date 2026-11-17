import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { deleteSecret, listSecrets, putSecret, secretRef } from "./secrets";
import { parseSecretRequest } from "./approvals";

const fetchMock = vi.fn();
beforeEach(() => {
  vi.stubGlobal("fetch", fetchMock);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1");
});
afterEach(() => {
  fetchMock.mockReset();
  vi.unstubAllGlobals();
});
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("secrets client", () => {
  it("lists names", async () => {
    fetchMock.mockResolvedValue(json([{ name: "slack-token", created_at: "a", updated_at: "b" }]));
    expect(await listSecrets()).toEqual([{ name: "slack-token", created_at: "a", updated_at: "b" }]);
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/api\/v1\/secrets$/);
  });
  it("puts a value as JSON to the encoded name, 204", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 204 }));
    await putSecret("slack-token", "fake-value-123");
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/v1\/secrets\/slack-token$/);
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body)).toEqual({ value: "fake-value-123" });
  });
  it("throws a readable error without the value on 422 secret_name_invalid", async () => {
    fetchMock.mockResolvedValue(json({ detail: { code: "secret_name_invalid" } }, 422));
    const error = await putSecret("Bad Name", "fake-value-123").catch((e) => e);
    expect(error).toBeInstanceOf(Error);
    expect(error.message).not.toContain("fake-value-123");
    expect(error.message.length).toBeGreaterThan(0);
  });
  it("deletes, 204, and rejects a 404", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await deleteSecret("slack-token");
    expect(fetchMock.mock.calls[0][1].method).toBe("DELETE");
    fetchMock.mockResolvedValueOnce(json({ detail: { code: "secret_not_found" } }, 404));
    await expect(deleteSecret("gone")).rejects.toThrow();
  });
  it("builds a ref", () => expect(secretRef("slack-token")).toBe("secret:slack-token"));
});

describe("parseSecretRequest", () => {
  it("accepts secrets__request with a name", () => {
    expect(parseSecretRequest("secrets__request", { name: "slack-token", purpose: "post alerts" })).toEqual({ name: "slack-token", purpose: "post alerts" });
    expect(parseSecretRequest("secrets__request", { name: "x" })).toEqual({ name: "x", purpose: "" });
  });
  it("rejects other tools and bad args", () => {
    expect(parseSecretRequest("architect__publish_agent", { name: "x" })).toBeNull();
    expect(parseSecretRequest("secrets__request", { purpose: "p" })).toBeNull();
    expect(parseSecretRequest("secrets__request", null)).toBeNull();
  });
});
