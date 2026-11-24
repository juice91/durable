import { afterEach, describe, expect, it, vi } from "vitest";
import { chooseBaseUrl, parseLoopbackApiUrl, resolveBaseUrl } from "./base-url";

describe("resolveBaseUrl", () => {
  it("keeps the local default when the variable is unset", () => {
    expect(resolveBaseUrl(undefined)).toBe("http://localhost:8000");
  });

  it("is same-origin (relative /api/v1/... paths) when the variable is empty", () => {
    expect(resolveBaseUrl("")).toBe("");
    expect(resolveBaseUrl("  ")).toBe("");
    expect(`${resolveBaseUrl("")}/api/v1/runs/r1/stream`).toBe("/api/v1/runs/r1/stream");
  });

  it("uses an explicit URL without a trailing slash", () => {
    expect(resolveBaseUrl("http://127.0.0.1:18000")).toBe("http://127.0.0.1:18000");
    expect(resolveBaseUrl("http://127.0.0.1:18000/")).toBe("http://127.0.0.1:18000");
  });
});

describe("module-level BASE_URL", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("is '' in lib/api.ts and lib/runs.ts when NEXT_PUBLIC_EAP_API_URL is empty", async () => {
    vi.stubEnv("NEXT_PUBLIC_EAP_API_URL", "");
    vi.resetModules();
    const api = await import("./api");
    const runs = await import("./runs");
    expect(api.BASE_URL).toBe("");
    expect(runs.BASE_URL).toBe("");
  });

  it("keeps the local default in both when the variable is unset", async () => {
    vi.stubEnv("NEXT_PUBLIC_EAP_API_URL", undefined as unknown as string);
    vi.resetModules();
    delete process.env.NEXT_PUBLIC_EAP_API_URL;
    const api = await import("./api");
    expect(api.BASE_URL).toBe("http://localhost:8000");
  });
});

describe("chooseBaseUrl (SSH tunnel: a loopback page calls the API forward directly)", () => {
  const runtime = { loopbackApiUrl: "http://127.0.0.1:18000" };

  it("uses the runtime loopback API URL on a loopback origin", () => {
    for (const hostname of ["127.0.0.1", "localhost"]) {
      expect(chooseBaseUrl({ raw: "", hostname, runtime })).toBe("http://127.0.0.1:18000");
    }
  });

  it("stays same-origin on the public host and on the server", () => {
    expect(chooseBaseUrl({ raw: "", hostname: "durable.example.com", runtime })).toBe("");
    expect(chooseBaseUrl({ raw: "", runtime })).toBe("");
  });

  it("falls back to the build setting when no runtime value is published", () => {
    expect(chooseBaseUrl({ raw: "", hostname: "127.0.0.1" })).toBe("");
    expect(chooseBaseUrl({ raw: undefined, hostname: "localhost", runtime: { loopbackApiUrl: "" } })).toBe("http://localhost:8000");
  });

  it("never honours a non-loopback runtime value, even on a loopback page", () => {
    for (const bad of ["https://evil.example", "http://10.0.0.5:18000", "http://127.0.0.1.evil.test", "http://127.0.0.1:18000/x", "http://u:p@127.0.0.1:18000", "javascript:1"]) {
      expect(parseLoopbackApiUrl(bad)).toBeUndefined();
      expect(chooseBaseUrl({ raw: "", hostname: "127.0.0.1", runtime: { loopbackApiUrl: bad } })).toBe("");
    }
  });

  it("normalizes a trailing slash", () => {
    expect(parseLoopbackApiUrl("http://localhost:18000/")).toBe("http://localhost:18000");
  });
});
