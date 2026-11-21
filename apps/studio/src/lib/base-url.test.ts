import { afterEach, describe, expect, it, vi } from "vitest";
import { resolveBaseUrl } from "./base-url";

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
