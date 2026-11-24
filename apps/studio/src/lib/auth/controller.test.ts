// The page-level rules: one renew then a login redirect on a 401, no loop right after a login, the loopback dev bearer
// untouched, and config resolution. The OIDC server is the in-process test provider.
import { webcrypto } from "node:crypto";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { startTestProvider, type TestProvider } from "@/test/oidcProvider";
import { resolveAuthConfig } from "./config";
import { installAuthFetch, oidc, resetAuthForTests, setGiveUpHandler } from "./controller";
import { getTokens, setTokens } from "./session";
import { authorizationValue } from "../credentials";
import { runtimeSettings, runtimeConfigScript } from "../runtimeConfigScript";

let provider: TestProvider;
const realFetch = globalThis.fetch;
const assign = vi.fn();

beforeAll(async () => {
  if (!globalThis.crypto?.subtle) vi.stubGlobal("crypto", webcrypto);
  provider = await startTestProvider();
});
afterAll(async () => {
  vi.unstubAllGlobals();
  await provider.close();
});
beforeEach(() => {
  resetAuthForTests();
  window.__EAP_RUNTIME_CONFIG__ = { auth: { mode: "zitadel", issuer: provider.issuer, clientId: "studio-client", projectId: "proj-1" } };
  window.fetch = realFetch;
  Object.defineProperty(window, "location", { configurable: true, value: { ...window.location, origin: "https://studio.example.test", pathname: "/agents", search: "", hostname: "studio.example.test", assign } });
  assign.mockReset();
  provider.failRefresh = false;
});
afterEach(() => {
  delete window.__EAP_RUNTIME_CONFIG__;
  window.fetch = realFetch;
});

async function signIn() {
  const client = oidc()!;
  await client.beginLogin("/");
  const callback = await provider.authorize(assign.mock.calls.at(-1)![0]);
  await client.completeLogin(new URL(callback).search);
  window.sessionStorage.removeItem("eap.oidc.lastLogin"); // the login was a while ago
  assign.mockReset();
}

/** An API that accepts only the access token named `valid`. */
function fakeApi(valid: () => string) {
  const calls: string[] = [];
  const apiFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (!url.includes("/api/")) return realFetch(input, init);
    const auth = new Headers(init?.headers).get("authorization") ?? "";
    calls.push(auth);
    return new Response("{}", { status: auth === `Bearer ${valid()}` ? 200 : 401 });
  });
  window.fetch = apiFetch as unknown as typeof fetch;
  return { calls, apiFetch };
}

describe("credentials under zitadel", () => {
  it("sends the in-memory access token as the bearer", async () => {
    await signIn();
    expect(authorizationValue()).toBe(`Bearer ${getTokens()!.accessToken}`);
  });
  it("sends nothing before sign-in, and the loopback dev bearer still wins on a loopback page", async () => {
    expect(authorizationValue()).toBeUndefined();
    const bearer = ["Bearer", "dev:studio:operator"].join(" ");
    Object.defineProperty(window, "location", { configurable: true, value: { ...window.location, hostname: "127.0.0.1" } });
    window.__EAP_RUNTIME_CONFIG__ = { devBearer: bearer };
    setTokens({ accessToken: "t", expiresAt: Date.now() + 1000 });
    expect(authorizationValue()).toBe(bearer);
  });
});

describe("a 401 renews once, then goes to the login", () => {
  it("renews and repeats the call with the new token", async () => {
    await signIn();
    const before = getTokens()!.accessToken;
    const api = fakeApi(() => getTokens()!.accessToken); // the API accepts whatever token is current after the renew
    installAuthFetch();
    const response = await window.fetch("/api/v1/agents", { headers: { Authorization: "Bearer stale" } });
    expect(response.status).toBe(200);
    expect(api.calls).toHaveLength(2);
    expect(api.calls[0]).toBe("Bearer stale");
    expect(api.calls[1]).toBe(`Bearer ${getTokens()!.accessToken}`);
    expect(getTokens()!.accessToken).not.toBe(before);
    expect(assign).not.toHaveBeenCalled();
  });

  it("redirects to the login when the renew is refused, once", async () => {
    await signIn();
    provider.failRefresh = true;
    fakeApi(() => "never");
    installAuthFetch();
    const response = await window.fetch("/api/v1/agents", { headers: { Authorization: "Bearer x" } });
    expect(response.status).toBe(401);
    await vi.waitFor(() => expect(assign).toHaveBeenCalledTimes(1));
    expect(String(assign.mock.calls[0][0])).toContain("/oauth/v2/authorize");
    expect(getTokens()).toBeNull();
  });

  it("does not loop: a 401 right after a login stops with a message", async () => {
    const client = oidc()!;
    await client.beginLogin("/");
    const callback = await provider.authorize(assign.mock.calls.at(-1)![0]);
    await client.completeLogin(new URL(callback).search); // the login redirect was just made
    const gaveUp = vi.fn();
    setGiveUpHandler(gaveUp);
    fakeApi(() => "never");
    installAuthFetch();
    assign.mockReset();
    await window.fetch("/api/v1/agents", { headers: { Authorization: "Bearer x" } });
    expect(gaveUp).toHaveBeenCalledWith(expect.stringContaining("refused your session"));
    expect(assign).not.toHaveBeenCalled();
  });

  it("leaves a non-API 401 and an unauthenticated page alone", async () => {
    fakeApi(() => "never");
    installAuthFetch();
    await window.fetch("/api/v1/agents"); // no tokens yet: nothing to renew
    expect(assign).not.toHaveBeenCalled();
  });
});

describe("resolving the runtime settings", () => {
  const env = { EAP_STUDIO_AUTH: "zitadel", EAP_STUDIO_ZITADEL_CLIENT_ID: "cid", EAP_STUDIO_ZITADEL_PROJECT_ID: "pid" };

  it("is access by default and identical to today: no auth settings reach the browser", () => {
    for (const e of [{}, { ...env, EAP_STUDIO_AUTH: "access" }] as Record<string, string>[]) {
      const s = runtimeSettings("studio.example.test", { EAP_STUDIO_LOOPBACK_API_URL: "http://127.0.0.1:18000", ...e });
      expect(s.oidc).toBe(false);
      expect(s.config).toEqual({ loopbackApiUrl: "http://127.0.0.1:18000" });
    }
  });

  it("publishes the OIDC settings under zitadel, with the default issuer", () => {
    const s = runtimeSettings("durable.example-org.ai", env);
    expect(s.oidc).toBe(true);
    expect(s.config.auth).toEqual({ mode: "zitadel", issuer: "https://auth.example-org.ai", clientId: "cid", projectId: "pid" });
    expect(JSON.stringify(s.config)).not.toMatch(/secret/i);
  });

  it("the loopback page keeps the dev bearer and starts no login", () => {
    const bearer = ["Bearer", "dev:studio:operator"].join(" ");
    const s = runtimeSettings("127.0.0.1:13000", { ...env, EAP_STUDIO_LOOPBACK_DEV_BEARER: bearer });
    expect(s.oidc).toBe(false);
    expect(s.config.devBearer).toBe(bearer);
    expect(s.config.auth).toBeUndefined();
    // ...but through the public host the bearer is never published and the login runs.
    const pub = runtimeSettings("durable.example-org.ai", { ...env, EAP_STUDIO_LOOPBACK_DEV_BEARER: bearer });
    expect(pub.config.devBearer).toBeUndefined();
    expect(pub.oidc).toBe(true);
  });

  it("a half-set switch stays on access instead of locking everyone out", () => {
    const missing: string[][] = [];
    expect(resolveAuthConfig({ EAP_STUDIO_AUTH: "zitadel", EAP_STUDIO_ZITADEL_CLIENT_ID: "cid" }, false, (m) => missing.push(m))).toBeUndefined();
    expect(missing).toEqual([["EAP_STUDIO_ZITADEL_PROJECT_ID"]]);
  });

  it("the inline script cannot be closed by a value", () => {
    expect(runtimeConfigScript({ loopbackApiUrl: "</script><b>" })).not.toContain("</script>");
  });
});
