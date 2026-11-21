import { describe, expect, it } from "vitest";
import { resolveBaseUrl } from "./runs";

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
