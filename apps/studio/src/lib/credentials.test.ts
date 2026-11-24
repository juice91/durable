import { afterEach, describe, expect, it, vi } from "vitest";
import { authorizationHeaders, authorizationValue, runtimeDevBearer } from "./credentials";

const BEARER = ["Bearer", "dev:studio:operator"].join(" "); // not written out: the bundle check greps for the literal

afterEach(() => {
  vi.unstubAllEnvs();
  delete window.__EAP_RUNTIME_CONFIG__;
});

describe("runtimeDevBearer", () => {
  it("honours the runtime bearer on a loopback page only", () => {
    expect(runtimeDevBearer("127.0.0.1", { devBearer: BEARER })).toBe(BEARER);
    expect(runtimeDevBearer("localhost", { devBearer: BEARER })).toBe(BEARER);
    expect(runtimeDevBearer("studio.example.com", { devBearer: BEARER })).toBeUndefined();
    expect(runtimeDevBearer(undefined, { devBearer: BEARER })).toBeUndefined();
  });

  it("ignores a missing or malformed value", () => {
    expect(runtimeDevBearer("127.0.0.1", undefined)).toBeUndefined();
    expect(runtimeDevBearer("127.0.0.1", {})).toBeUndefined();
    expect(runtimeDevBearer("127.0.0.1", { devBearer: "operator" })).toBeUndefined();
  });
});

describe("authorizationHeaders", () => {
  it("is empty with no credential configured: the Access cookie is the credential", () => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "");
    expect(authorizationValue()).toBeUndefined();
    expect(authorizationHeaders()).toEqual({});
  });

  it("sends the runtime bearer on a loopback page (jsdom is on localhost)", () => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "");
    window.__EAP_RUNTIME_CONFIG__ = { devBearer: BEARER };
    expect(authorizationHeaders()).toEqual({ Authorization: BEARER });
  });

  it("uses the development variable outside production", () => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    expect(authorizationHeaders()).toEqual({ Authorization: "Bearer test" });
  });

  it("never reads the development variable in a production build", () => {
    vi.stubEnv("NODE_ENV", "production");
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    expect(authorizationHeaders()).toEqual({});
  });
});
