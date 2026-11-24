// The PKCE flow against an in-process test OIDC provider (a real HTTP server; see src/test/oidcProvider.ts). The CI has no
// ZITADEL container and no real browser: the client's redirect is followed by hand, as the browser would after the sign-in.
import { webcrypto } from "node:crypto";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { startTestProvider, type TestProvider } from "@/test/oidcProvider";
import { createOidcClient, safeReturnTo, AuthFlowError } from "./oidc";
import { getTokens, setTokens } from "./session";
import type { StudioAuthConfig } from "./config";

let provider: TestProvider;
const ORIGIN = "https://studio.example.test";

function memoryStorage() {
  const map = new Map<string, string>();
  return {
    map,
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    removeItem: (k: string) => void map.delete(k),
  };
}

function setup(now = () => Date.now()) {
  const storage = memoryStorage();
  const navigated: string[] = [];
  const config: StudioAuthConfig = { mode: "zitadel", issuer: provider.issuer, clientId: "studio-client", projectId: "proj-1" };
  const client = createOidcClient({ config, fetch: globalThis.fetch, storage, origin: ORIGIN, navigate: (u) => navigated.push(u), now });
  return { client, storage, navigated };
}

/** Sign in end to end: begin, follow the authorize redirect, complete at the callback. */
async function signIn(s: ReturnType<typeof setup>, returnTo = "/agents?tab=x") {
  await s.client.beginLogin(returnTo);
  const callback = await provider.authorize(s.navigated.at(-1)!);
  expect(callback.startsWith(`${ORIGIN}/auth/callback?`)).toBe(true);
  return s.client.completeLogin(new URL(callback).search);
}

beforeAll(async () => {
  if (!globalThis.crypto?.subtle) vi.stubGlobal("crypto", webcrypto);
  provider = await startTestProvider();
});
afterAll(async () => {
  vi.unstubAllGlobals();
  await provider.close();
});
beforeEach(() => {
  setTokens(null);
  provider.log.tokenForms.length = 0;
  provider.failRefresh = false;
});
afterEach(() => setTokens(null));

describe("authorization code + PKCE", () => {
  it("sends the S256 challenge, the project audience and role scopes, and no secret", async () => {
    const s = setup();
    await s.client.beginLogin("/");
    const q = new URL(s.navigated[0]).searchParams;
    expect(q.get("response_type")).toBe("code");
    expect(q.get("client_id")).toBe("studio-client");
    expect(q.get("redirect_uri")).toBe(`${ORIGIN}/auth/callback`);
    expect(q.get("code_challenge_method")).toBe("S256");
    expect(q.get("code_challenge")).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(q.get("scope")).toContain("urn:zitadel:iam:org:project:id:proj-1:aud");
    expect(q.get("scope")).toContain("offline_access");
    expect(q.has("client_secret")).toBe(false);
  });

  it("completes the flow: the code is exchanged with the verifier, tokens are in memory only, and it returns to the page asked for", async () => {
    const s = setup();
    expect(await signIn(s, "/agents?tab=x")).toBe("/agents?tab=x");
    expect(getTokens()?.accessToken).toMatch(/^access-/);
    expect(getTokens()?.refreshToken).toMatch(/^refresh-/);
    const form = provider.log.tokenForms[0];
    expect(form.get("grant_type")).toBe("authorization_code");
    expect(form.get("code_verifier")).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(form.get("client_id")).toBe("studio-client");
    // No token reached storage: only the login-in-flight values were ever written, and they are gone.
    expect([...s.storage.map.values()].join()).not.toMatch(/access-|refresh-/);
    expect(s.storage.map.has("eap.oidc.pending")).toBe(false);
  });

  it("refuses a callback whose state does not match, a second use of the same pending login, and an error response", async () => {
    const s = setup();
    await s.client.beginLogin("/");
    const callback = new URL(await provider.authorize(s.navigated[0]));
    callback.searchParams.set("state", "forged");
    await expect(s.client.completeLogin(callback.search)).rejects.toBeInstanceOf(AuthFlowError);
    expect(getTokens()).toBeNull();
    await expect(s.client.completeLogin(callback.search)).rejects.toThrow(/does not match/);
    await expect(s.client.completeLogin("?error=access_denied&error_description=No+way")).rejects.toThrow("No way");
  });

  it("a code replayed against the token endpoint is refused by the provider", async () => {
    const s = setup();
    await s.client.beginLogin("/");
    const callback = await provider.authorize(s.navigated[0]);
    const search = new URL(callback).search;
    const pending = s.storage.getItem("eap.oidc.pending")!;
    await s.client.completeLogin(search);
    s.storage.setItem("eap.oidc.pending", pending); // an attacker replaying the same callback
    await expect(s.client.completeLogin(search)).rejects.toThrow(/refused the sign-in code/);
  });

  it("only a same-origin path is a return target", () => {
    for (const bad of ["https://evil.example", "//evil.example", "/\\evil", "javascript:alert(1)", "/auth/callback?code=1", "", null, undefined]) {
      expect(safeReturnTo(bad as string | null)).toBe("/");
    }
    expect(safeReturnTo("/workflows/abc?x=1")).toBe("/workflows/abc?x=1");
  });
});

describe("silent renew and sign-out", () => {
  it("renews with the rotating refresh token, and one renew at a time", async () => {
    const s = setup();
    await signIn(s);
    const first = getTokens()!;
    const [a, b] = await Promise.all([s.client.renew(), s.client.renew()]);
    expect(a && b).toBe(true);
    expect(provider.log.tokenForms.filter((f) => f.get("grant_type") === "refresh_token")).toHaveLength(1);
    const second = getTokens()!;
    expect(second.accessToken).not.toBe(first.accessToken);
    expect(second.refreshToken).not.toBe(first.refreshToken);
    expect(second.idToken).toBeTruthy();
    expect(await s.client.renew()).toBe(true); // the new refresh token works; the old one would not
  });

  it("a refused renew clears the tokens", async () => {
    const s = setup();
    await signIn(s);
    provider.failRefresh = true;
    expect(await s.client.renew()).toBe(false);
    expect(getTokens()).toBeNull();
  });

  it("a network failure keeps the tokens", async () => {
    const s = setup();
    await signIn(s);
    const broken = createOidcClient({
      config: { mode: "zitadel", issuer: provider.issuer, clientId: "studio-client", projectId: "p" },
      fetch: () => Promise.reject(new TypeError("network")), storage: s.storage, origin: ORIGIN, navigate: () => undefined,
    });
    expect(await broken.renew()).toBe(false);
    expect(getTokens()).not.toBeNull();
  });

  it("logout forgets the tokens and ends the session with the id token hint", async () => {
    const s = setup();
    await signIn(s);
    const idToken = getTokens()!.idToken!;
    await s.client.logout();
    expect(getTokens()).toBeNull();
    const url = new URL(s.navigated.at(-1)!);
    expect(url.pathname).toBe("/oidc/v1/end_session");
    expect(url.searchParams.get("id_token_hint")).toBe(idToken);
    expect(url.searchParams.get("post_logout_redirect_uri")).toBe(`${ORIGIN}/`);
  });

  it("a login redirect just made is remembered so a 401 right after it cannot loop", async () => {
    let t = 1_000_000;
    const s = setup(() => t);
    expect(s.client.justLoggedIn()).toBe(false);
    await s.client.beginLogin("/");
    expect(s.client.justLoggedIn()).toBe(true);
    t += 20_000;
    expect(s.client.justLoggedIn()).toBe(false);
  });
});
